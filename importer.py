# -*- coding: utf-8 -*-
"""歌单导入：本地文件 / 文本（主通道）、在线链接（次通道）、可选增强通道。

设计要点（与 _art/STAGE_CF_DESIGN_2026-11-09.md 的歌单决策保持一致）：
  * 只产出「曲目候选」，绝不直接写用户数据 —— 预览是 dry-run，落盘由
    main.py 的 /api/import/commit 复用 playlist_create / playlist_add 完成。
  * 三桶判定：L1 歌名+作者都严格对上 → 自动纳入；L2 歌名对上作者对不上 →
    声明出来交用户勾选；L3 歌名也对不上 → 同样声明，绝不静默丢弃。
  * 严格判定复用主程序注入的 _billed_as / fold_text / search_all，
    本模块不复制一套判定逻辑，避免两套口径漂移。

主程序启动时调用 bind(...) 注入依赖；本模块不 import main，无循环依赖。
"""
import concurrent.futures as _cf
import html as _html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_BOUND = {}
_RULES_CACHE = {"at": 0.0, "data": None}

_TASKS = {}
_TASKS_LOCK = threading.Lock()
_TASK_TTL = 1800                 # 结果保留 30 分钟，够前端取走即可
_CAND_CACHE = {}
_CAND_LOCK = threading.Lock()
_CAND_MAX = 600

_HTTP_TIMEOUT = 20
_EXT_RE = r"\.(?:mp3|flac|wav|m4a|ape|ogg|wma|aac|mp4|kgm)"
_LINE_NO_RE = re.compile(r"^\s*\d+\s+")
# 单行长度上限。正常歌单条目不会超过 2KB（Windows 路径上限才 260），
# 超过它的基本是误贴的长文本 / base64 / 二进制 —— 交给下面的正则会病态回溯。
_MAX_LINE = 2048
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff<>\"'）)】\]]+", re.I)


def bind(**kw):
    """注入主程序能力：search_all / billed_as / fold_text / resource_dir。"""
    _BOUND.update(kw)


# --------------------------------------------------------------------------
# 规则（外置，可热更新；缺失或损坏时降级到内置默认，不让主程序起不来）
# --------------------------------------------------------------------------
DEFAULT_MANIFEST = {
    "rules_version": "2026-11-09.1",
    "client_version": "2.2.0-beta2",
    "update_url": "https://cdn.jsdelivr.net/gh/elysiayunchen/elysium-player@main/providers/manifest.json",
    "update_url_mirrors": [],
    "check_on_start": False,
    "providers": ["netease", "qqmusic", "kugou"],
}

DEFAULT_RULES = {
    "netease": {
        "id": "netease", "label": "网易云音乐", "version": "2026-11-09.1",
        "hosts": ["music.163.com", "163cn.tv", "y.music.163.com"],
        "patterns": [r"playlist\?id=(\d+)", r"#/playlist\?id=(\d+)", r"/m/playlist\?id=(\d+)"],
        "endpoint": "https://music.163.com/api/playlist/detail?id={id}",
        "referer": "https://music.163.com/",
        "probe": {"input": "https://music.163.com/playlist?id=3778678", "min_tracks": 100},
        "auth": {"optional": False},
    },
    "qqmusic": {
        "id": "qqmusic", "label": "QQ 音乐", "version": "2026-11-09.1",
        "hosts": ["y.qq.com", "c.y.qq.com", "i.y.qq.com", "c6.y.qq.com"],
        "patterns": [r"ryqq/playlist/(\d+)", r"[?&]id=(\d+)", r"disstid=(\d+)"],
        "endpoint": ("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
                     "?type=1&utf8=1&onlysong=0&disstid={id}&format=jsonp"),
        "referer": "https://y.qq.com/",
        "probe": {"input": "https://y.qq.com/n/ryqq/playlist/9044196528", "min_tracks": 100},
        "auth": {"optional": False},
    },
    "kugou": {
        "id": "kugou", "label": "酷狗音乐", "version": "2026-11-09.1",
        "hosts": ["kugou.com", "m.kugou.com", "t1.kugou.com"],
        "patterns": [r"special/single/(\d+)", r"songlist/(gcid_[A-Za-z0-9]+)",
                     r"global_specialid=(collection_[\w-]+)", r"[?&](?:id|listid|src_cid)=(\d+)"],
        "endpoint": "",          # 直接抓分享页，端点由链接本身决定
        "referer": "https://www.kugou.com/",
        "probe": {"input": "https://www.kugou.com/yy/special/single/546903.html", "min_tracks": 20},
        "auth": {"optional": True, "note": "新版 gcid 链接免登录只给前 10 首；带凭据走增强通道可取全量"},
    },
}

ENHANCE_ENDPOINT = "https://playlistout-api.lengxiqwq.com/api/v1/resolve"


def _rules_dir():
    fn = _BOUND.get("resource_dir")
    base = fn() if fn else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "providers")


def load_rules(force=False):
    """读 providers/*.json。任何异常都返回内置默认 + degraded 标记。"""
    now = time.time()
    if not force and _RULES_CACHE["data"] and now - _RULES_CACHE["at"] < 30:
        return _RULES_CACHE["data"]
    manifest = dict(DEFAULT_MANIFEST)
    rules = {k: dict(v) for k, v in DEFAULT_RULES.items()}
    degraded = []
    d = _rules_dir()
    try:
        mp = os.path.join(d, "manifest.json")
        if os.path.isfile(mp):
            with open(mp, "r", encoding="utf-8") as f:
                ext = json.load(f)
            if isinstance(ext, dict):
                manifest.update({k: v for k, v in ext.items() if k in DEFAULT_MANIFEST})
        for pid in manifest.get("providers") or list(DEFAULT_RULES):
            rp = os.path.join(d, pid + ".json")
            if not os.path.isfile(rp):
                degraded.append(pid + ":规则文件缺失")
                continue
            with open(rp, "r", encoding="utf-8") as f:
                ext = json.load(f)
            if not isinstance(ext, dict) or not ext.get("hosts"):
                degraded.append(pid + ":规则文件不完整")
                continue
            merged = dict(DEFAULT_RULES.get(pid, {}))
            merged.update(ext)
            rules[pid] = merged
    except Exception as e:                                  # noqa: BLE001
        degraded.append("规则加载失败: %s" % e)
    out = {"manifest": manifest, "rules": rules, "degraded": degraded,
           "dir": d, "loaded_at": int(now)}
    _RULES_CACHE["data"] = out
    _RULES_CACHE["at"] = now
    return out


def update_check():
    """notify-only：只比对本地规则版本与可选远端 manifest，绝不自动下载或替换。

    主址不通时按 update_url_mirrors 顺序兜底（jsDelivr 多 CDN，谁先通用谁）。
    """
    r = load_rules()
    m = r["manifest"]
    out = {"client_version": m.get("client_version"),
           "rules_version": m.get("rules_version"),
           "degraded": r["degraded"],
           "remote": "not_configured"}
    url = str(m.get("update_url") or "").strip()
    if not url:
        return out
    chain = [url] + [str(u).strip() for u in (m.get("update_url_mirrors") or [])
                     if str(u).strip() and str(u).strip() != url]
    j, last_err = None, None
    for u in chain:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except Exception as e:                              # noqa: BLE001
            last_err = e
    if j is None:
        out["remote"] = "unavailable"
        out["error"] = str(last_err)[:160]
        return out
    rv = str((j or {}).get("rules_version") or "")
    out["remote"] = {"rules_version": rv,
                     "client_version": str((j or {}).get("client_version") or ""),
                     "has_update": bool(rv and rv != m.get("rules_version"))}
    return out


# --------------------------------------------------------------------------
# 输入识别
# --------------------------------------------------------------------------
def parse_input(text):
    """从任意文本里抠出 URL / 平台 / 歌单 id；没有 URL 就当本地文本歌单。"""
    raw = str(text or "").strip()
    m = _URL_RE.search(raw)
    if not m:
        return {"mode": "text", "url": "", "platform": "", "id": "",
                "text": raw, "label": "本地文本"}
    url = m.group(0).rstrip(".,;，。；")
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    rules = load_rules()["rules"]
    for pid, r in rules.items():
        if not any(host == h or host.endswith("." + h) for h in (r.get("hosts") or [])):
            continue
        for pat in (r.get("patterns") or []):
            hit = re.search(pat, url, re.I)
            if hit:
                return {"mode": "link", "url": url, "platform": pid,
                        "id": hit.group(1), "text": raw,
                        "label": r.get("label") or pid}
        # 短链：识别到平台但没抠出 id，跟随一次跳转再试
        return {"mode": "link", "url": url, "platform": pid, "id": "",
                "text": raw, "label": r.get("label") or pid}
    return {"mode": "text", "url": url, "platform": "", "id": "",
            "text": raw, "label": "本地文本"}


def _http_text(url, referer="", headers=None, timeout=_HTTP_TIMEOUT):
    h = {"User-Agent": UA, "Referer": referer or url}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), resp.geturl()


def _json_unescape(s):
    """页面里常是 JSON 字符串（含 \\uXXXX），还原成真正的中文。"""
    try:
        return json.loads('"' + str(s).replace('"', '\\"') + '"')
    except Exception:                                       # noqa: BLE001
        return str(s)


def _track(name, artist, raw="", duration=0):
    return {"name": str(name or "").strip(), "artist": str(artist or "").strip(),
            "raw": str(raw or "").strip(), "duration": int(duration or 0)}


def _looks_path(s):
    """这行像文件路径 / 文件名吗（m3u8、KGL 的每一行都是）。"""
    t = str(s or "").strip().replace("\\", "/")
    return (t.startswith("/") or t.startswith("./") or t.startswith("../")
            or bool(re.match(r"^[A-Za-z]:/", t))
            or bool(re.search(_EXT_RE + r"$", t, re.I)))


def _path_stem(s):
    """文件名行 -> 纯曲名：剥目录、剥音频扩展名、砍掉 KGL 行尾元数据。

    只对「看起来是路径 / 文件名」的行剥目录 —— 否则「AC/DC - Back in Black」
    这类带斜杠的歌名会被砍成后半截。
    """
    t = str(s or "").strip().replace("\\", "/")
    # 完整 KGL 行是「歌手 - 歌名.mp3 . 大小 码率 ... 歌词路径 ...」：
    # 第一处音频扩展名之后的都是元数据。限制 400 字符内，避免长行回溯。
    m = re.search(_EXT_RE, t, re.I)
    if m and m.start() <= 400:
        t = t[:m.end()]
    if _looks_path(t):
        t = t.rsplit("/", 1)[-1]
    t = re.sub(r"^[A-Za-z]:", "", t).strip()
    t = re.sub(_EXT_RE + r"$", "", t, flags=re.I).strip()
    return t


def _split_stem(stem):
    """「A - B」拆成 (歌名, 歌手)。默认按 KGL 的「歌手 - 歌名」；
    反过来的解释交给 alt + 匹配阶段投票，这里不猜。"""
    parts = re.split(r"\s+[-–—]\s+|\s*[-–—]\s*", stem, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[1].strip(), parts[0].strip()
    return str(stem or "").strip(), ""


# --------------------------------------------------------------------------
# A 通道：本地文件 / 文本
# --------------------------------------------------------------------------
def parse_text(text, filename=""):
    """解析 .kgl / .txt / .csv / .m3u8 / 纯文本「歌名 - 歌手」列表。

    两个已知坑（都来自真实样本）：
      1) 完整 .kgl 每行开头有 `0 ` 之类的序号，不剥掉会把歌手解析成「0 邓紫棋」；
      2) 分隔符方向有歧义：KGL 是「歌手 - 歌名」，TuneMyMusic 文本是「歌名 - 歌手」。
         这里保留 alt 解释，由匹配阶段用「严格命中」自动裁决，不猜、不问用户。
    """
    raw = str(text or "")
    fname = str(filename or "").lower()
    body = _html.unescape(raw)                 # .kgl 的 XML / JSON 都带 &#34; 转义

    lines = []
    xml_names = re.findall(r"<FileName[^>]*>(.*?)</FileName>", body, re.I | re.S)
    if xml_names:
        lines = xml_names
    else:
        # CSV 表头：只用来定位列，不参与曲目
        head = None
        for ln in body.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if head is None and re.search(r"(title|song|name|曲名|歌曲)", ln, re.I) \
                    and re.search(r"(artist|singer|艺人|歌手)", ln, re.I) and ("," in ln or "\t" in ln):
                head = re.split(r"[,\t]", ln)
                continue
            lines.append(ln)
        if head is not None:
            ti = next((i for i, h in enumerate(head)
                       if re.search(r"title|song|name|曲名|歌曲", h, re.I)), 0)
            ai = next((i for i, h in enumerate(head)
                       if re.search(r"artist|singer|艺人|歌手", h, re.I)), 1)
            tracks = []
            for ln in lines:
                if len(ln) > _MAX_LINE:
                    continue
                cols = re.split(r"[,\t]", ln)
                if len(cols) <= max(ti, ai):
                    continue
                t = _track(cols[ti].strip().strip('"'), cols[ai].strip().strip('"'), ln)
                if t["name"]:
                    tracks.append(_with_alt(t, "name-first"))
            return _dedup(tracks)

    tracks = []
    pending = None          # #EXTINF 的元数据，等下一行的文件名来配对
    for raw_ln in lines:
        if len(raw_ln) > _MAX_LINE:
            # 超长单行不是歌单条目：放进去会让下面那条正则病态回溯
            # （实测 20KB=1.3s、100KB=33s、1MB>120s），导入预览直接卡死。
            continue
        ln = _LINE_NO_RE.sub("", raw_ln.strip())
        if not ln:
            continue
        if ln.startswith("#"):
            m = re.match(r"#EXTINF\s*:\s*(-?\d+)?\s*,\s*(.*)", ln, re.I)
            if m:
                title = m.group(2).strip()
                if title:
                    name, artist = _split_stem(_path_stem(title))
                    pending = {"name": name, "artist": artist,
                               "dur": max(0, int(m.group(1) or 0)), "raw": ln}
            continue                    # #EXTM3U / #EXTGRP 等指令一律跳过
        if re.fullmatch(r"[\d\s.,:]+", ln):
            continue
        if pending is not None and _looks_path(ln):
            # m3u8 用「#EXTINF 一行 + 文件名一行」描述一首歌。两行各出一条
            # 会重复，而且文件名那条还会带着目录（/xx/晴天）；用 EXTINF 的
            # 元数据 + 文件行的上下文合成一条。
            t = _track(pending["name"] or _path_stem(ln), pending["artist"],
                       pending["raw"], pending["dur"])
            pending = None
        else:
            if pending is not None:
                # EXTINF 后面不是文件行（畸形 m3u8）：它自己成一条，别静默丢
                tracks.append(_with_alt(
                    _track(pending["name"], pending["artist"],
                           pending["raw"], pending["dur"]), "artist-first"))
                pending = None
            name, artist = _split_stem(_path_stem(ln))
            t = _track(name, artist, ln)
        tracks.append(_with_alt(t, "artist-first"))
    return _dedup(tracks)


def _with_alt(t, order):
    if order == "artist-first" and t["name"] and t["artist"]:
        t["alt"] = {"name": t["artist"], "artist": t["name"]}
        t["order"] = order
    elif order == "name-first" and t["name"] and t["artist"]:
        t["alt"] = {"name": t["artist"], "artist": t["name"]}
        t["order"] = order
    else:
        t["alt"] = None
        t["order"] = order
    return t


def _dedup(tracks):
    seen, out = set(), []
    for t in tracks:
        k = (t["name"].lower(), t["artist"].lower())
        if not t["name"] or k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


# --------------------------------------------------------------------------
# B 通道：在线链接
# --------------------------------------------------------------------------
def _fetch_netease(pid_url, info):
    r = load_rules()["rules"].get("netease") or {}
    pid = info["id"]
    if not pid:
        resolved, final = _http_text(info["url"], r.get("referer", ""))
        hit = re.search(r"playlist\?id=(\d+)", final or "", re.I) or \
              re.search(r"playlist\?id=(\d+)", resolved[:20000], re.I)
        if not hit:
            raise ValueError("无法从短链里解析出网易云歌单 ID")
        pid = hit.group(1)
    body, _ = _http_text(r.get("endpoint", "").format(id=pid), r.get("referer", ""))
    j = json.loads(body)
    res = (j or {}).get("result") or {}
    tracks = []
    for t in (res.get("tracks") or []):
        ars = [a.get("name") for a in (t.get("artists") or t.get("ar") or []) if a.get("name")]
        tracks.append(_track(t.get("name"), " / ".join(ars),
                             duration=int(t.get("dt") or 0) // 1000))
    return {"name": res.get("name") or "", "tracks": _dedup(tracks),
            "partial": False, "note": "", "platform": "netease"}


def _fetch_qqmusic(info):
    r = load_rules()["rules"].get("qqmusic") or {}
    pid = info["id"]
    if not pid:
        _, final = _http_text(info["url"], r.get("referer", ""))
        hit = re.search(r"(?:ryqq/playlist/|disstid=|id=)(\d+)", final or "", re.I)
        if not hit:
            raise ValueError("无法从短链里解析出 QQ 音乐歌单 ID")
        pid = hit.group(1)
    body, _ = _http_text(r.get("endpoint", "").format(id=pid), r.get("referer", ""))
    j = json.loads(re.sub(r"^\w+\(", "", body.strip()).rstrip(");"))
    cd = ((j or {}).get("cdlist") or [{}])[0]
    tracks = []
    for t in (cd.get("songlist") or []):
        ars = [s.get("name") for s in (t.get("singer") or []) if s.get("name")]
        tracks.append(_track(t.get("songname"), " / ".join(ars),
                             duration=int(t.get("interval") or 0)))
    return {"name": cd.get("dissname") or "", "tracks": _dedup(tracks),
            "partial": False, "note": "", "platform": "qqmusic"}


def _fetch_kugou(info, enhance=None):
    r = load_rules()["rules"].get("kugou") or {}
    url = info["url"]
    # C 增强通道：用户主动开启并填了凭据，才走第三方解析（默认完全不联网到它）
    if enhance and enhance.get("enabled") and enhance.get("token") and enhance.get("userid"):
        q = urllib.parse.urlencode({"q": url})
        hdr = {"Authorization": "Bearer " + str(enhance["token"]),
               "X-Kugou-Userid": str(enhance["userid"])}
        body, _ = _http_text(ENHANCE_ENDPOINT + "?" + q, "", headers=hdr, timeout=25)
        j = json.loads(body)
        res = ((j or {}).get("data") or {}).get("result") or {}
        tracks = [_track(t.get("title"), t.get("artist"),
                         duration=int(t.get("durationMs") or 0) // 1000)
                  for t in (res.get("tracks") or [])]
        mode = (res.get("retrieval") or {}).get("mode") or "full"
        return {"name": res.get("name") or "", "tracks": _dedup(tracks),
                "partial": mode != "full",
                "note": "" if mode == "full" else "增强通道仍只返回预览，检查凭据是否有效",
                "platform": "kugou"}
    body, final = _http_text(url, r.get("referer", ""))
    un = _html.unescape(body)
    pairs = re.findall(r'"songname"\s*:\s*"(.*?)"\s*,\s*"(?:author_name|singer(?:name)?)"\s*:\s*"(.*?)"',
                       un, re.I)
    tracks = []
    for n, a in pairs:
        t = _track(_json_unescape(n), _json_unescape(a))
        if t["name"]:
            tracks.append(t)
    tracks = _dedup(tracks)
    is_new = ("gcid_" in (final or url)) or ("/songlist/" in (final or url))
    partial = is_new and len(tracks) <= 10
    note = ""
    if partial:
        note = ("该分享链接免登录只返回前 %d 首预览。要完整导入，请在酷狗客户端"
                "「播放列表 → 菜单 → 导出列表」导出 .kgl 文件，用「本地文件」通道导入。" % len(tracks))
    return {"name": "", "tracks": tracks, "partial": partial,
            "note": note, "platform": "kugou"}


def fetch_online(info, enhance=None):
    p = info.get("platform")
    if p == "netease":
        return _fetch_netease(info.get("url", ""), info)
    if p == "qqmusic":
        return _fetch_qqmusic(info)
    if p == "kugou":
        return _fetch_kugou(info, enhance)
    raise ValueError("不支持的平台：" + str(p))


# --------------------------------------------------------------------------
# 三桶判定
# --------------------------------------------------------------------------
def _fold(s):
    fn = _BOUND.get("fold_text")
    if fn:
        return fn(s)
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or ""))).lower()


def _title_eq(a, b):
    """歌名严格相等：只做等价折叠（NFKC / 去标点 / 小写），不做包含、不剥后缀。"""
    return bool(a) and _fold(a) == _fold(b)


def _artist_eq(cand, want):
    """作者严格相等：复用主程序的 _billed_as（拆分合唱署名后逐个严格比对）。"""
    if not want:
        return False
    fn = _BOUND.get("billed_as")
    if fn:
        return bool(fn({"artist": cand}, want))
    parts = re.split(r"[/／&、,，;；]", str(cand or ""))
    k = _fold(want)
    return any(_fold(p) == k for p in parts if p.strip())


def _candidates(title):
    key = title.strip().lower()
    if not key:
        return []
    with _CAND_LOCK:
        if key in _CAND_CACHE:
            return _CAND_CACHE[key]
    fn = _BOUND.get("search_all")
    if not fn:
        raise RuntimeError("导入模块未绑定搜索能力")
    try:
        d = fn(title, 1, 20) or {}
    except Exception:                                        # noqa: BLE001
        d = {}
    out = []
    for c in (d.get("list") or []):
        out.append(c)
        for a in (c.get("alts") or []):
            out.append(a)
    with _CAND_LOCK:
        if len(_CAND_CACHE) >= _CAND_MAX:
            # 只淘汰最旧的三分之一，别整表清空：2000 首的歌单超过上限时，
            # clear() 会把刚取回的候选全抖掉，后面每条都重复回源。
            for k in list(_CAND_CACHE)[:_CAND_MAX // 3]:
                _CAND_CACHE.pop(k, None)
        _CAND_CACHE[key] = out
    return out


def _slim(c):
    keys = ("platform", "id", "name", "artist", "album_name", "duration", "cover",
            "pic_url", "minfo", "time", "sign")
    return {k: c.get(k) for k in keys if c.get(k) not in (None, "")}


def _strict_hit(title, artist):
    """这条解释能否产生 L1（歌名 + 作者都严格命中）。"""
    if not title or not artist:
        return False
    try:
        cands = _candidates(title)
    except Exception:                                        # noqa: BLE001
        return False
    return any(_title_eq(c.get("name"), title)
               and _artist_eq(c.get("artist"), artist) for c in cands)


def _choose_order(tracks):
    """整批投票决定分隔符方向，而不是逐条乱猜。

    KGL 是「歌手 - 歌名」，TuneMyMusic 文本是「歌名 - 歌手」。若逐条尝试两种
    解释，会出现「某人 - 这首歌根本不存在」被猜成歌名『某人』而误命中别的歌，
    甚至可能误进 L1 —— 那等于擅自合并，违反精密筛选的约定。
    这里抽样前 8 条，哪种解释的严格命中多就整批采用哪种；平票（含都 0）
    保持默认方向。
    """
    sample = [t for t in tracks if t.get("alt")][:8]
    if not sample:
        return "normal"
    # 两种解释 × 抽样 8 条 = 最多 16 次上游检索：串行跑能把进度条钉在 0，
    # 并发 4 路（与 classify 的匹配并发一致）缩到 1/4 时间。
    with _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="vote") as ex:
        futs = [(ex.submit(_strict_hit, t["name"], t["artist"]),
                 ex.submit(_strict_hit, t["alt"]["name"], t["alt"]["artist"]))
                for t in sample]
        normal = swap = 0
        for fn_, fs in futs:
            try:
                normal += 1 if fn_.result() else 0
            except Exception:                                    # noqa: BLE001
                pass
            try:
                swap += 1 if fs.result() else 0
            except Exception:                                    # noqa: BLE001
                pass
    return "swap" if swap > normal else "normal"


def classify_one(idx, t, cancel=None):
    """单条曲目的三桶判定。方向已由整批投票定好，这里只按一种解释判定。"""
    base = {"index": idx, "raw": t.get("raw") or "",
            "name": t.get("name") or "", "artist": t.get("artist") or "",
            "duration": t.get("duration") or 0}
    hits, exact = [], []
    if cancel is None or not cancel.is_set():
        try:
            cands = _candidates(base["name"]) if base["name"] else []
        except Exception:                                    # noqa: BLE001
            cands = []
        hits = [c for c in cands if _title_eq(c.get("name"), base["name"])]
        exact = [c for c in hits
                 if _artist_eq(c.get("artist"), base["artist"])]

    def by_duration(c):
        return (abs(int(c.get("duration") or 0) - int(base["duration"] or 0))
                if base["duration"] else 0)

    out = dict(base)
    if exact:
        out.update({"tier": "l1", "chosen": True,
                    "item": _slim(sorted(exact, key=by_duration)[0]),
                    "candidates": []})
    elif hits:
        out.update({"tier": "l2", "chosen": False, "item": None, "selected": 0,
                    "candidates": [_slim(c) for c in
                                   sorted(hits, key=by_duration)[:4]]})
    else:
        out.update({"tier": "l3", "chosen": False, "item": None,
                    "candidates": [], "selected": 0})
    return out


def classify(tracks, progress=None, cancel=None, workers=4):
    """并发匹配；保持原始顺序，逐条上报进度，可取消。"""
    order = _choose_order(tracks)
    effective = []
    for t in tracks:
        if order == "swap" and t.get("alt"):
            t = dict(t)
            t["name"], t["artist"] = t["alt"]["name"], t["alt"]["artist"]
        effective.append(t)
    total = len(effective)
    results = [None] * total
    with _cf.ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="import") as pool:
        futs = {pool.submit(classify_one, i, t, cancel): i
                for i, t in enumerate(effective)}
        done = 0
        for f in _cf.as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except Exception as e:                           # noqa: BLE001
                results[i] = {"index": i, "tier": "l3", "chosen": False,
                              "item": None, "candidates": [], "selected": 0,
                              "name": (effective[i] or {}).get("name", ""),
                              "artist": (effective[i] or {}).get("artist", ""),
                              "raw": (effective[i] or {}).get("raw", ""),
                              "error": str(e)[:120]}
            done += 1
            if progress:
                progress(done, total)
            if cancel is not None and cancel.is_set():
                for other in futs:
                    other.cancel()
                break
    results = [r for r in results if r is not None]
    buckets = {"l1": [], "l2": [], "l3": []}
    for r in results:
        buckets[r.get("tier", "l3")].append(r)
    return {"buckets": buckets, "total": total, "matched": len(results),
            "order": order}


# --------------------------------------------------------------------------
# 预览任务（dry-run，不写任何用户数据）
# --------------------------------------------------------------------------
def _cleanup():
    now = time.time()
    with _TASKS_LOCK:
        for k in [k for k, v in _TASKS.items() if now - v.get("at", 0) > _TASK_TTL]:
            _TASKS.pop(k, None)


def start_preview(payload):
    _cleanup()
    p = payload or {}
    text = str(p.get("text") or "")
    filename = str(p.get("filename") or "")
    enhance = p.get("enhance") or {}
    if not text.strip():
        raise ValueError("内容为空")
    info = parse_input(text)
    tid = "imp_%d_%s" % (int(time.time() * 1000), os.urandom(3).hex())
    task = {"state": "running", "total": 0, "done": 0, "at": time.time(),
            "cancel": threading.Event(), "result": None, "error": "",
            "input": info}
    with _TASKS_LOCK:
        _TASKS[tid] = task

    def progress(done, total):
        task["done"], task["total"] = done, total

    def run():
        try:
            if info["mode"] == "link":
                if not info["platform"]:
                    raise ValueError("这个链接不是网易云 / QQ 音乐 / 酷狗的歌单链接")
                got = fetch_online(info, enhance)
                tracks = got["tracks"]
                source = {"mode": "link", "platform": info["platform"],
                          "label": info["label"], "url": info["url"],
                          "name": got.get("name") or "", "partial": got.get("partial"),
                          "note": got.get("note") or ""}
            else:
                tracks = parse_text(text, filename)
                source = {"mode": "file" if filename else "text",
                          "platform": "", "label": "本地文件" if filename else "粘贴文本",
                          "filename": filename, "name": "", "partial": False, "note": ""}
            if not tracks:
                raise ValueError("没有解析到任何曲目，检查文件内容或编码（建议 UTF-8）")
            if len(tracks) > 2000:
                raise ValueError("一次最多导入 2000 首，请拆分文件（当前 %d 首）" % len(tracks))
            task["total"] = len(tracks)
            res = classify(tracks, progress=progress, cancel=task["cancel"], workers=4)
            res["source"] = source
            res["rules_version"] = load_rules()["manifest"].get("rules_version")
            if task["cancel"].is_set():
                task["state"] = "cancelled"
            else:
                task["result"] = res
                task["state"] = "done"
        except Exception as e:                               # noqa: BLE001
            task["error"] = str(e)[:300]
            task["state"] = "error"

    threading.Thread(target=run, daemon=True,
                     name="import-" + tid[-6:]).start()
    return {"task_id": tid, "input": {k: info[k] for k in ("mode", "platform", "label", "url")}}


def status(tid):
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return {"state": "missing", "error": "任务不存在或已过期"}
        return {"state": t["state"], "done": t["done"], "total": t["total"],
                "error": t["error"], "result": t["result"] if t["state"] == "done" else None}


def cancel(tid):
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return False
        t["cancel"].set()
        if t["state"] == "running":
            t["state"] = "cancelled"
        return True


# --------------------------------------------------------------------------
# 接口追踪：探针自检（按需触发，不阻塞启动）
# --------------------------------------------------------------------------
def health():
    r = load_rules(force=True)
    out = {"rules_version": r["manifest"].get("rules_version"),
           "degraded": r["degraded"], "providers": []}
    for pid, rule in r["rules"].items():
        probe = rule.get("probe") or {}
        item = {"id": pid, "label": rule.get("label") or pid,
                "version": rule.get("version"), "ok": False,
                "tracks": 0, "error": ""}
        try:
            info = parse_input(probe.get("input") or "")
            got = fetch_online(info)
            n = len(got["tracks"])
            item["tracks"] = n
            item["ok"] = n >= int(probe.get("min_tracks") or 1)
            if not item["ok"]:
                item["error"] = "只取到 %d 首（阈值 %s）" % (n, probe.get("min_tracks"))
        except Exception as e:                               # noqa: BLE001
            item["error"] = str(e)[:160]
        out["providers"].append(item)
    out["ok"] = all(p["ok"] for p in out["providers"])
    return out
