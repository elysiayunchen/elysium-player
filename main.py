# -*- coding: utf-8 -*-
"""
FlacMusic - 无损音乐桌面客户端

架构：
    本地 ThreadingHTTPServer（127.0.0.1 随机端口）
        /                  静态前端（web/）
        /api/rpc           转发到 flac.music.hi.cn/ajax.php（绕开 CORS）
        /api/store         本地用户数据（收藏 / 历史 / 下载记录 / 设置）
        /api/downloads*    下载队列（并发、速度、取消、重试、自动降级）
        /api/save-lyric    保存 .lrc
    pywebview 开窗指向本地服务。

用户数据：%APPDATA%\\FlacMusic\\data.json
"""

import collections
import concurrent.futures as _cf
import hashlib
import http.client
import importer                      # 歌单导入（规则外置 + 三桶判定），见 providers/
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "Elysium"
APP_TITLE = "Elysium - 在线音乐"
VERSION = "1.4.0"
LEGACY_NAME = "FlacMusic"
UPSTREAM = "https://flac.music.hi.cn/"
API_URL = UPSTREAM + "ajax.php?act="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PLATFORMS = ("kuwo", "wyy")
UPSTREAM_HOST = "flac.music.hi.cn"
_TLS = threading.local()          # 每线程一条上游长连接


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def resource_dir():
    home = os.environ.get("ELYSIUM_HOME")      # Android / 自定义根目录
    if home:
        return home
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    root = os.environ.get("ELYSIUM_HOME") or os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(root, APP_NAME)
    os.makedirs(d, exist_ok=True)
    # 从旧名字迁移一次
    old = os.path.join(root, LEGACY_NAME)
    new_data = os.path.join(d, "data.json")
    old_data = os.path.join(old, "data.json")
    if not os.path.isfile(new_data) and os.path.isfile(old_data):
        try:
            import shutil
            shutil.copy2(old_data, new_data)
        except Exception:
            pass
    return d


def default_download_dir():
    music = os.path.join(os.path.expanduser("~"), "Music")
    if not os.path.isdir(music):
        music = data_dir()
    return os.path.join(music, APP_NAME)


DATA_FILE = os.path.join(data_dir(), "data.json")
DEFAULT_STORE = {
    "settings": {
        "platform": "kuwo",
        "quality": "best",             # 下载音质：best / 320 / 128
        "play_quality": "auto",        # 在线播放音质：auto / best / 320 / 128 / flac
        "download_dir": default_download_dir(),
        "concurrency": 2,
        "name_template": "{name} - {artist}",
        "dup_policy": "rename",     # rename / overwrite / skip
        "write_tags": True,
        "save_lyric": False,
        "theme": "dark",            # light / dark / auto（默认深色）
        "volume": 80,
        "cache_limit_mb": 2048,     # 试听缓存上限（当天）
        "cache_min_plays": 2,       # 当天听满几次进当天缓存
        "cache_week_plays": 5,      # 7 天内听满几次进长期缓存
        "cache_keep_days": 3,       # 长期缓存几天没听就清掉
        "auto_cache": True,         # 听满后自动缓存
        "mode": "order",            # order / one / shuffle
        "last_track": None,         # 上次播放，用于续播
        "personalized": True,       # 个性化推荐总开关
    },
    "favorites": [],                # 收藏：{kind: song|album|artist, ...}
    "history": [],                  # 搜索历史
    "recent": [],                   # 最近播放（去重，最多 60 首）
    "downloads": [],
    "plays": {},                    # 当天收听计数 {key: {day, count, cached}}
    "feedback": {},                 # 显式反馈 {platform:id: {verdict, at}}
    "taste_note": "",               # 用户自述口味（旧字段，保留兼容，前端已不再暴露）
    "taste_genres": [],             # 用户手动选中的风格 id 列表
    "playlists": [],                # 用户自建歌单 [{id, name, created_at, updated_at, items:[...]}]
}


# --------------------------------------------------------------------------
# 本地存储
# --------------------------------------------------------------------------
def fav_key(f):
    """收藏唯一键：类型 + id + 平台。旧数据没有 kind，按 song 处理。"""
    return ((f.get("kind") or "song"), str(f.get("id")), f.get("platform"))


def pl_item_key(it):
    """歌单内单曲唯一键：平台 + id。与收藏的三元组不同 —— 歌单只收 song。"""
    return (str(it.get("platform") or ""), str(it.get("id") or ""))


def pl_item_of(src):
    """把任意曲目对象转成歌单条目：引用 + 冗余兜底字段。

    只留显示必需的信息，冗余是为了歌单列表不必回源就能渲染标题。
    """
    src = src or {}
    out = {
        "platform": str(src.get("platform") or ""),
        "id": str(src.get("id") or ""),
        "name": str(src.get("name") or ""),
        "artist": str(src.get("artist") or ""),
    }
    for k in ("album_name", "cover", "duration", "album_id", "artist_id"):
        v = src.get(k)
        if v not in (None, ""):
            out[k] = v
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return json.loads(json.dumps(DEFAULT_STORE))
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return json.loads(json.dumps(DEFAULT_STORE))
        merged = json.loads(json.dumps(DEFAULT_STORE))
        for k, v in (raw or {}).items():
            if k == "settings" and isinstance(v, dict):
                merged["settings"].update(v)
            elif isinstance(merged.get(k), list) and isinstance(v, list):
                merged[k] = v
            elif isinstance(merged.get(k), dict) and isinstance(v, dict):
                # plays / feedback 都是 dict。之前只恢复 list，导致每次启动
                # 收听计数和反馈全被清空 —— 口味画像的数字因此永远对不上。
                merged[k] = v
        self._fix_paths(merged)
        return merged

    def _fix_paths(self, data):
        """修正从别处带来的失效路径。

        data.json 里存着下载目录的绝对路径。别人分享数据文件、
        或用户换了机器 / 换了用户名，这份路径就指到不存在的目录了 ——
        下载会静默失败。这里在加载时校验并回落到本机默认目录。
        """
        want = data["settings"].get("download_dir")
        if want and not os.path.isdir(want):
            fallback = default_download_dir()
            try:
                os.makedirs(fallback, exist_ok=True)
            except Exception:
                pass
            data["settings"]["download_dir"] = fallback

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def apply(self, op, payload):
        with self.lock:
            if op == "load":
                return self.data
            if op == "settings":
                self.data["settings"].update(payload or {})
            elif op == "history_add":
                q = (payload or {}).get("q", "").strip()
                plat = (payload or {}).get("platform", "kuwo")
                if q:
                    items = [h for h in self.data["history"]
                             if not (h.get("q") == q and h.get("platform") == plat)]
                    items.insert(0, {"q": q, "platform": plat, "at": int(time.time())})
                    self.data["history"] = items[:40]
            elif op == "history_clear":
                self.data["history"] = []
            elif op == "fav_toggle":
                item = dict(payload or {})
                item["kind"] = item.get("kind") or "song"
                key = fav_key(item)
                favs = self.data["favorites"]
                idx = next((i for i, f in enumerate(favs) if fav_key(f) == key), -1)
                if idx >= 0:
                    favs.pop(idx)
                    active = False
                else:
                    item["added"] = int(time.time())
                    favs.insert(0, item)
                    active = True
                self.save()
                return {"favorites": self.data["favorites"], "active": active}
            elif op == "fav_remove":
                key = fav_key(payload or {})
                self.data["favorites"] = [f for f in self.data["favorites"]
                                          if fav_key(f) != key]
            elif op == "download_add":
                self.data["downloads"].insert(0, payload or {})
                self.data["downloads"] = self.data["downloads"][:300]
            elif op == "downloads_clear":
                self.data["downloads"] = []
            elif op == "favorites_clear":
                kind = (payload or {}).get("kind")
                self.data["favorites"] = ([] if not kind
                                          else [f for f in self.data["favorites"]
                                                if (f.get("kind") or "song") != kind])
            elif op == "recent_add":
                item = dict(payload or {})
                if not item.get("id"):
                    self.data["recent"] = self.data["recent"] or []
                else:
                    recent = self.data.setdefault("recent", [])
                    recent[:] = [r for r in recent
                                 if not (r.get("platform") == item.get("platform")
                                         and str(r.get("id")) == str(item.get("id")))]
                    item["played_at"] = int(time.time())
                    recent.insert(0, item)
                    del recent[60:]
            elif op == "recent_clear":
                self.data["recent"] = []
            elif op == "feedback_set":
                item = dict(payload or {})
                plat, sid = item.get("platform"), str(item.get("id") or "")
                verdict = (item.get("verdict") or "").strip()
                if plat and sid:
                    key = "%s:%s" % (plat, sid)
                    fb = self.data.setdefault("feedback", {})
                    if verdict in ("", "none"):
                        fb.pop(key, None)
                    else:
                        fb[key] = {"verdict": verdict, "at": int(time.time()),
                                   "name": item.get("name"), "artist": item.get("artist")}
            elif op == "feedback_clear":
                self.data["feedback"] = {}
            elif op == "taste_note":
                self.data["taste_note"] = str((payload or {}).get("text") or "")[:280]
            elif op == "taste_genres":
                ids = (payload or {}).get("ids")
                valid = {g["id"] for g in genre_list()["genres"]}
                picked = [str(x) for x in (ids or []) if str(x) in valid]
                self.data["taste_genres"] = list(dict.fromkeys(picked))[:24]
            elif op == "playlist_create":
                self._pl_create(payload or {})
            elif op == "playlist_rename":
                self._pl_rename(payload or {})
            elif op == "playlist_delete":
                self._pl_delete(payload or {})
            elif op == "playlist_add":
                self._pl_add(payload or {})
            elif op == "playlist_remove":
                self._pl_remove(payload or {})
            elif op == "playlist_reorder":
                self._pl_reorder(payload or {})
            self.save()
            return self.data

    # ------------------------------------------------------------ 用户歌单
    # 与 favorites 完全独立：加歌到歌单不写 favorites，反之亦然。
    def _pls(self):
        ls = self.data.setdefault("playlists", [])
        return ls if isinstance(ls, list) else []

    def _pl_find(self, pid):
        for pl in self._pls():
            if pl.get("id") == pid:
                return pl
        return None

    def _pl_create(self, payload):
        name = str(payload.get("name") or "").strip()[:60]
        if not name:
            return
        now = int(time.time())
        # id 带上时间戳 + 随机后缀，避免同毫秒建两个歌单撞 id
        pid = "pl_%d_%s" % (now, os.urandom(3).hex())
        entry = {"id": pid, "name": name, "created_at": now,
                 "updated_at": now, "items": []}
        # 导入歌单的来源元数据：纯展示用，不参与 items 结构，也不影响去重键。
        # 只收白名单字段，避免把整个预览结果或用户本机路径写进 data.json。
        src = payload.get("source")
        if isinstance(src, dict):
            allowed = ("mode", "platform", "label", "url", "filename",
                       "name", "partial", "note", "rules_version", "imported_at")
            clean = {k: src[k] for k in allowed if src.get(k) not in (None, "")}
            if clean:
                entry["source"] = clean
        self._pls().append(entry)

    def _pl_rename(self, payload):
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        name = str(payload.get("name") or "").strip()[:60]
        if not name:
            return
        pl["name"] = name
        pl["updated_at"] = int(time.time())

    def _pl_delete(self, payload):
        pid = payload.get("id")
        self.data["playlists"] = [p for p in self._pls() if p.get("id") != pid]

    def _pl_add(self, payload):
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        items = pl.setdefault("items", [])
        srcs = payload.get("items")
        if srcs is None:
            srcs = [payload.get("item")] if payload.get("item") else []
        added = 0
        for src in srcs:
            it = pl_item_of(src)
            if not it["platform"] or not it["id"]:
                continue
            key = pl_item_key(it)
            if any(pl_item_key(x) == key for x in items):
                continue                       # 已在歌单里，不重复加
            it["added_at"] = int(time.time())
            items.append(it)
            added += 1
        if added:
            pl["updated_at"] = int(time.time())

    def _pl_remove(self, payload):
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        key = (str(payload.get("platform") or ""), str(payload.get("sid") or ""))
        pl["items"] = [x for x in pl.get("items", []) if pl_item_key(x) != key]
        pl["updated_at"] = int(time.time())

    def _pl_reorder(self, payload):
        """按新顺序重排。order 是旧索引数组，元素不足则追加剩下的。"""
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        items = pl.get("items", [])
        order = payload.get("order") or []
        idx = [i for i in order if isinstance(i, int) and 0 <= i < len(items)]
        seen = set(idx)
        idx += [i for i in range(len(items)) if i not in seen]
        pl["items"] = [items[i] for i in idx]
        pl["updated_at"] = int(time.time())


STORE = Store(DATA_FILE)


# --------------------------------------------------------------------------
# 上游接口
# --------------------------------------------------------------------------
def upstream_keepalive(act, params):
    """POST 一次 ajax.php，复用长连接。只给「同一关键词连续翻几十页」的场景用
    （歌手页）。上游首个请求要付 ~4s 的 TLS 握手，逐页新开连接时每页都重付
    （实测 6.5s/页 → 复用后 1.4s/页）。线程本地各持一条连接：服务端是多线程的，
    跨线程共用 socket 会串包。

    其余请求（搜索 / 探测 / 取直链）仍走一次性的 urllib：那些是零散请求，
    长连接留着只会在上游那边堆一堆空闲连接，反而拖慢后面的探测。
    """
    body = urllib.parse.urlencode(
        {k: "" if v is None else v for k, v in (params or {}).items()}).encode()
    path = "/ajax.php?act=" + act
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": UPSTREAM,
        "Origin": UPSTREAM.rstrip("/"),
        "Accept": "application/json, text/plain, */*",
    }
    conn = getattr(_TLS, "up", None)
    for attempt in (0, 1):                       # 连接被对端关掉时重连一次
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=30)
                _TLS.up = conn
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            if r.status != 200:
                raise RuntimeError(f"上游 HTTP {r.status}")
            payload = json.loads(raw.decode("utf-8", "replace"))
            if payload.get("code") != 0:
                raise RuntimeError(payload.get("msg") or f"接口错误 {payload.get('code')}")
            return payload.get("data")
        except RuntimeError:
            raise
        except (http.client.HTTPException, OSError, ValueError):
            try:
                conn.close()
            except Exception:                    # noqa: BLE001
                pass
            conn = None
            _TLS.up = None
            if attempt:
                raise RuntimeError("上游连接失败")
    raise RuntimeError("上游无响应")


def upstream(act, params):
    body = urllib.parse.urlencode(
        {k: "" if v is None else v for k, v in (params or {}).items()}).encode()
    req = urllib.request.Request(API_URL + act, data=body, method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": UPSTREAM,
        "Origin": UPSTREAM.rstrip("/"),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or f"接口错误 {payload.get('code')}")
    return payload.get("data")


# --------------------------------------------------------------------------
# 第二通道（备用站）：只在主站失败/返回空时接手，目前仅覆盖网易云
# --------------------------------------------------------------------------
GD_BASE = "https://music-api.gdstudio.xyz/api.php"
STATION_NAME = {"hi": "主站", "gd": "备用站"}


def gd_rpc(types, **kw):
    qs = {"types": types}
    qs.update({k: v for k, v in kw.items() if v is not None})
    url = GD_BASE + "?" + urllib.parse.urlencode(qs)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://music.gdstudio.xyz/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def gd_search(keyword, page=1, size=20):
    """备用站搜索，结构对齐主站。它不给时长和封面，先留空由探测定夺。"""
    arr = gd_rpc("search", source="netease", name=keyword, count=size, pages=page) or []
    if not isinstance(arr, list):
        return {"list": [], "total": None}
    items = []
    for it in arr:
        items.append({
            "id": str(it.get("id") or ""),
            "name": it.get("name") or "",
            "artist": " / ".join(it.get("artist") or []) if isinstance(it.get("artist"), list) else str(it.get("artist") or ""),
            "album_name": it.get("album") or "",
            "album_id": "",
            "duration": 0,
            "cover": "",
            "minfo": [{"format": "flac", "bitrate": "999"},
                      {"format": "mp3", "bitrate": "320"},
                      {"format": "mp3", "bitrate": "128"}],
            "time": None,
            "sign": None,
            "station": "gd",
        })
    return {"list": items, "total": None}


def search_any(platform, keyword, page=1, size=20, keepalive=False):
    """主站优先；主站报错或给空表时，网易云换备用站再试一次。

    keepalive=True 用于歌手页那种「一个词连翻几十页」的连续请求。
    """
    call = upstream_keepalive if keepalive else upstream
    try:
        data = call("search", {"platform": platform, "keyword": keyword,
                               "page": page, "size": size})
        lst = (data or {}).get("list") or []
        if lst:
            for it in lst:
                it["station"] = "hi"
            return data
    except Exception:  # noqa: BLE001
        lst = []
    if platform == "wyy":
        try:
            alt = gd_search(keyword, page, size)
            if alt.get("list"):
                return alt
        except Exception:  # noqa: BLE001
            pass
    return {"list": [], "total": None}


# --------------------------------------------------------------------------
# 跨源折叠：同一首歌在两个渠道各有一份时，只留一行，另一份塞进 alts
# --------------------------------------------------------------------------
# 副标题特征词：酷我的曲名常带「江南-《你很》电影片头曲」这种尾巴，
# 网易云只有「江南」。不剥掉尾巴就折不拢，会变成两行。
_DESC_HINT = (
    "主题曲", "主题歌", "片头曲", "片尾曲", "插曲", "印象曲", "推广曲", "宣传曲",
    "原声", "ost", "live", "现场", "演唱会", "伴奏", "instrumental", "翻唱",
    "cover", "remix", "demo", "版", "feat", "原唱", "演奏",
)
_PUNCT_RE = re.compile(r"[\s\u3000·・,，、。.!！?？'\"“”‘’()（）\[\]【】《》〈〉<>~\-_/&+*#:：;；|]+")


def _core_title(name):
    """曲名主干：只有尾巴更像「副标题 / 用途说明」时才切掉。"""
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    s = s.replace("（", "(").replace("）", ")")
    parts = re.split(r"\s*[-–—]\s*", s, maxsplit=1)
    if len(parts) == 2:
        head, tail = parts[0].strip(), parts[1].strip()
        low = tail.lower()
        if head and (tail.startswith("《") or any(h in low for h in _DESC_HINT)):
            s = head
    return s


def _fold_text(text):
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC", str(text or "")).lower())


def _main_artist(artist):
    """合唱署名取第一顺位：'林俊杰&五月天' 的主艺人是林俊杰。

    只用于折叠判定与专辑去重，不参与「记名歌手」归类，归类仍用完整署名。
    """
    s = str(artist or "")
    s = re.split(r"\s*(?:/|&|、|,|，|;|；|\bfeat\b|\bft\b|\bwith\b)\s*", s,
                 maxsplit=1, flags=re.I)[0]
    return _fold_text(s)


def fold_key(item):
    return (_fold_text(_core_title(item.get("name"))), _main_artist(item.get("artist")))


# 折叠后带进前端的字段：换源时要能直接播，sign/time 必须留着
_ALT_FIELDS = ("platform", "id", "name", "artist", "artist_id", "album_id",
               "album_name", "duration", "cover", "pic_url", "minfo", "time", "sign")


def fold_items(items, prefer="kuwo"):
    """同曲名 + 同主艺人 + 时长接近 → 折成一条，其余进 alts。

    代表条目优先取 prefer 源（云选的酷我），位置保持首次出现的名次，
    所以不重复的歌照旧互补，不会因为折叠被打乱顺序。
    """
    groups, index = [], {}
    for it in items:
        key = fold_key(it)
        dur = int(it.get("duration") or 0)
        hit = None
        for gi in index.get(key, ()):
            g = groups[gi]
            d0 = int(g["rep"].get("duration") or 0)
            if not dur or not d0 or abs(dur - d0) <= 4:
                hit = g
                break
        if hit is None:
            groups.append({"rep": it, "alts": []})
            index.setdefault(key, []).append(len(groups) - 1)
        else:
            hit["alts"].append(it)

    out = []
    for g in groups:
        rep, alts = g["rep"], g["alts"]
        same = [x for x in [rep] + alts if x.get("platform") == prefer]
        if same and same[0] is not rep:
            rep, alts = same[0], [x for x in [rep] + alts if x is not same[0]]
        if alts:
            rep = dict(rep)
            rep["alts"] = [{k: a.get(k) for k in _ALT_FIELDS} for a in alts]
        out.append(rep)
    return out


def _interleave(buckets):
    """各源轮流取一条。折叠前后都用它排，避免前半屏全是同一个来源。"""
    merged = []
    buckets = [list(b) for b in buckets]
    while any(buckets):
        for b in buckets:
            if b:
                merged.append(b.pop(0))
    return merged


def search_all(keyword, page=1, size=20, platforms=None):
    """多源聚合：并发向各平台各搜一遍，折叠重复项，不重复的互补。

    返回的每条都自带 platform；同一首在另一源也有时，另存 alts。
    """
    plats = [p for p in (platforms or PLATFORMS) if p in PLATFORMS]
    if not plats:
        plats = list(PLATFORMS)

    def one(p):
        try:
            d = search_any(p, keyword, page, size)
            return [normalize_item(i, p) for i in ((d or {}).get("list") or [])]
        except Exception:  # noqa: BLE001
            return []

    buckets = []
    with _cf.ThreadPoolExecutor(max_workers=len(plats)) as ex:
        for lst in ex.map(one, plats):
            buckets.append(list(lst))

    # 只要有一个源这一页还是满的，就说明后面还有
    has_more = any(len(b) >= int(size or 20) for b in buckets)
    merged = fold_items(_interleave(buckets))
    return {"list": merged, "total": len(merged), "has_more": has_more,
            "sources": {p: sum(1 for i in merged if i.get("platform") == p) for p in plats},
            "empty": [p for p, b in zip(plats, buckets) if not b]}



def gd_geturl(sid, fmt, br):
    want = str(br or "128")
    order = [want] + [x for x in ("999", "320", "128") if x != want]
    for b in order:
        try:
            j = gd_rpc("url", source="netease", id=sid, br=b)
            u = (j or {}).get("url")
            if u:
                return u
        except Exception:  # noqa: BLE001
            continue
    return None


def normalize_item(item, platform):
    out = dict(item or {})
    out["platform"] = platform
    out["id"] = str(out.get("id", ""))
    out["duration"] = int(out.get("duration") or 0)
    out["cover"] = out.get("pic_url") or ""
    minfo = out.get("minfo") or []
    for m in minfo:
        try:
            m["bitrate"] = str(m.get("bitrate"))
        except Exception:
            m["bitrate"] = "0"
    out["minfo"] = minfo
    return out


def normalize_list(data, platform):
    data = data or {}
    items = [normalize_item(i, platform) for i in (data.get("list") or [])]
    return {"list": items, "total": data.get("total"), "info": data.get("info")}


_RPC_CACHE = {}
_RPC_LOCK = threading.Lock()

# 歌手页深翻页：上限按「够全但不无限」定。酷我到 17 页以后基本只剩重复，
# 20 页 / 源在这台机器上实测 2~4 秒（长连接 + 3 线程并发翻页）。
ARTIST_PAGES = int(os.environ.get("ELYSIUM_ARTIST_PAGES") or 20)
ARTIST_MAX = 600
_PAGE_CACHE = {}
_PAGE_LOCK = threading.Lock()
_ARTIST_CACHE = {}
# 翻页专用线程池：线程不销毁，线程本地的那条长连接就能一直复用，
# 第二次打开歌手页不必再付一次 TLS 握手的钱。
_CRAWL_POOL = _cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="crawl")



def cached(key, ttl, fn):
    """同一请求 60 秒内只打一次上游；上游限流时也能顶住翻页。"""
    now = time.time()
    with _RPC_LOCK:
        hit = _RPC_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _RPC_LOCK:
        if len(_RPC_CACHE) > 400:
            for k in sorted(_RPC_CACHE, key=lambda k: _RPC_CACHE[k][0])[:200]:
                _RPC_CACHE.pop(k, None)
        _RPC_CACHE[key] = (now, val)
    return val


def _artist_key(name):
    """记名歌手归一化：只做等价折叠，绝不做包含匹配、不剥装饰后缀。

    「周杰伦♚」「周杰伦 (翻唱)」这类带后缀的名字与「周杰伦」不相等，
    不能被并进同一个歌手。
    """
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    return re.sub(r"\s+", " ", s).lower()


def _artist_page_cache(plat, name, page):
    """歌手页翻页缓存：慢在握手，同一页 10 分钟内不再打上游。"""
    key = (plat, name, page)
    now = time.time()
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    d = search_any(plat, name, page, 20, keepalive=True)
    lst = [normalize_item(i, plat) for i in ((d or {}).get("list") or [])]
    with _PAGE_LOCK:
        if len(_PAGE_CACHE) > 800:
            for k in sorted(_PAGE_CACHE, key=lambda k: _PAGE_CACHE[k][0])[:400]:
                _PAGE_CACHE.pop(k, None)
        _PAGE_CACHE[key] = (now, lst)
    return lst


def _crawl_artist(plat, name, max_pages, workers=3, cap=ARTIST_MAX):
    """把某个源上这位歌手的召回一路翻到底。

    老版本的 `if len(chunk) < 20: break` 是「只有几十首」的真凶 ——
    酷我经常某页只返 19 条，被当成末页，第 2~3 页就收工了。
    现在只认「整批都是空页」为枯竭。
    """
    ranked, seen = [], set()
    for start in range(1, max_pages + 1, workers):
        batch = list(range(start, min(start + workers, max_pages + 1)))
        futs = [(p, _CRAWL_POOL.submit(_artist_page_cache, plat, name, p)) for p in batch]
        empty = 0
        for _p, f in futs:
            try:
                lst = f.result()
            except Exception:  # noqa: BLE001
                lst = []
            if not lst:
                empty += 1
                continue
            for it in lst:
                k = str(it.get("id") or "")
                if not k or k in seen:
                    continue
                seen.add(k)
                ranked.append(it)
        if empty == len(batch):          # 这一批全空 → 已经翻到枯竭
            break
        if len(ranked) >= cap:
            break
    return ranked


def artist_page(params):
    """歌手页：只收录上游明确记名到该歌手的作品（口径：仅以记名歌手归）。

    上游搜索是「关键词相关性」召回：搜「周杰伦」会带出他作词作曲给别人
    唱的歌（蔡依林《布拉格广场》、李玟《刀马旦》）和综艺学员翻唱。这些歌
    的 artist / artist_id 是真实演唱者，必须按它归，不能因为搜索词命中就
    塞进周杰伦名下。

    判据优先级（按源各自判，跨源不做 ID 互串）：
      1) artist_id 精确相等（前端点歌手卡片时带上，最权威）
      2) 本平台内 artist 名严格相等者里出现最多的 artist_id
      3) 平台不给 artist_id 时，只认 artist 名严格相等
    任何情况下都不做「一条都不匹配就把整批结果全收」的兜底。

    曲目量：双源并发深翻页后合并，所以「总曲目」是抓全的量，不是首页
    那几十条。前端按滚动分批渲染，接口本身一次给全。
    """
    name = str(params.get("name") or params.get("keyword") or "").strip()
    if not name:
        raise ValueError("缺少歌手名")
    plat0 = params.get("platform") if params.get("platform") in PLATFORMS else "kuwo"
    max_pages = max(1, min(ARTIST_PAGES, int(params.get("pages") or ARTIST_PAGES)))
    wanted_aid = str(params.get("artist_id") or "").strip()
    cache_key = (name, plat0, wanted_aid, max_pages)
    now = time.time()
    with _PAGE_LOCK:
        hit = _ARTIST_CACHE.get(cache_key)
        if hit and now - hit[0] < 600:
            return hit[1]

    plats = [plat0] + [p for p in PLATFORMS if p != plat0]

    def aid_of(it):
        return str(it.get("artist_id") or "").strip()

    def pick(items, aid):
        """→ (采用的 artist_id, 只含该记名歌手的曲目)"""
        if not aid:
            exact = [it for it in items
                     if _artist_key(it.get("artist")) == _artist_key(name) and aid_of(it)]
            if exact:
                aid = collections.Counter(aid_of(it) for it in exact).most_common(1)[0][0]
        if aid:
            return aid, [it for it in items if aid_of(it) == aid]
        return "", [it for it in items
                    if _artist_key(it.get("artist")) == _artist_key(name)]

    with _cf.ThreadPoolExecutor(max_workers=len(plats)) as ex:
        crawled = dict(zip(plats, ex.map(
            lambda p: _crawl_artist(p, name, max_pages), plats)))

    buckets, aids, per_src = [], {}, {}
    for p in plats:
        aid, matched = pick(crawled.get(p) or [],
                            wanted_aid if p == plat0 else "")
        aids[p] = aid
        per_src[p] = len(matched)
        if matched:
            buckets.append(matched)

    songs = fold_items(_interleave(buckets))          # 折叠：同一首只留一行
    songs = songs[:ARTIST_MAX]

    # 专辑：按「专辑名 + 主艺人」去重，两个源各有一份时只留一张卡
    albums, seen = [], {}
    for it in _interleave(buckets):
        alb_id = str(it.get("album_id") or "")
        alb_name = it.get("album_name") or ""
        if not alb_id or not alb_name:
            continue
        key = (_fold_text(_core_title(alb_name)), _main_artist(it.get("artist")))
        if key in seen:
            card = seen[key]
            if it.get("platform") == plat0 and card.get("platform") != plat0:
                # 换成主源的 id，原来那个降为备选（打不开时兜底）
                card.setdefault("alts", []).append({"id": card.get("id"),
                                                    "platform": card.get("platform")})
                card.update({"id": alb_id, "platform": plat0,
                             "cover": it.get("cover") or card.get("cover") or ""})
            elif card.get("platform") == it.get("platform"):
                card.setdefault("alts", []).append({"id": alb_id,
                                                    "platform": it.get("platform")})
            continue
        card = {"id": alb_id, "name": alb_name,
                "cover": it.get("cover") or "",
                "artist": it.get("artist") or name,
                "platform": it.get("platform") or plat0, "kind": "album"}
        seen[key] = card
        albums.append(card)

    reasons = []
    if not songs:
        reasons.append("该歌手在两个来源都没有记名作品")
    elif not (crawled.get(plat0) or []):
        reasons.append(f"{'酷我' if plat0 == 'kuwo' else '网易云'}未收录该歌手，以下来自另一来源")

    data = {"name": name, "platform": plat0, "artist_id": aids.get(plat0, ""),
            "platforms": [p for p in plats if per_src.get(p)],
            "fallback": not per_src.get(plat0),
            "list": songs, "albums": albums, "total": len(songs),
            "count": len(songs), "per_source": per_src,
            "scanned": max_pages, "more": max_pages < ARTIST_PAGES,
            "reason": " · ".join(reasons)}
    with _PAGE_LOCK:
        _ARTIST_CACHE[cache_key] = (now, data)
    return data



RPC_TABLE = {
    "search": lambda p: cached(
        ("search", p.get("keyword", ""), p.get("page", 1), p.get("size", 20)), 45,
        lambda: search_all(p.get("keyword", ""), p.get("page", 1), p.get("size", 20))),
    "search_one": lambda p: normalize_list(
        cached(("search1", p.get("platform", "kuwo"), p.get("keyword", ""),
                p.get("page", 1), p.get("size", 20)), 45,
               lambda: search_any(p.get("platform", "kuwo"), p.get("keyword", ""),
                                  p.get("page", 1), p.get("size", 20))),
        p.get("platform", "kuwo")),
    "song": lambda p: normalize_list(
        upstream("song", {"platform": p.get("platform", "kuwo"), "id": p.get("id")}),
        p.get("platform", "kuwo")),
    "album": lambda p: normalize_list(
        cached(("album", p.get("platform", "kuwo"), p.get("id")), 300,
               lambda: upstream("album", {"platform": p.get("platform", "kuwo"),
                                          "id": p.get("id")})),
        p.get("platform", "kuwo")),
    "playlist": lambda p: normalize_list(
        upstream("playlist", {"platform": p.get("platform", "kuwo"),
                              "id": p.get("id"), "page": p.get("page", 1),
                              "size": p.get("size", 100)}),
        p.get("platform", "kuwo")),
    "artist": lambda p: artist_page(p),
    "geturl": lambda p: upstream("getUrl", {"platform": p.get("platform", "kuwo"),
                                            "songid": p.get("id"),
                                            "format": p.get("format"),
                                            "bitrate": p.get("bitrate"),
                                            "time": p.get("time"),
                                            "sign": p.get("sign")}),
    "getlyric": lambda p: upstream("getLyric", {"platform": p.get("platform", "kuwo"),
                                                "songid": p.get("id"),
                                                "time": p.get("time"),
                                                "sign": p.get("sign")}),
}


# --------------------------------------------------------------------------
# 口味画像 + 跨源推荐（全部本地，不依赖任何推荐服务）
# 借鉴 Spotify 客户端的「信号层 + 编排层」：显式反馈、口味画像、
# 按 seed 生成候选、预览后入列、可一键关闭。召回/排序不做（在服务端）。
# --------------------------------------------------------------------------
FEEDBACK_NEGATIVE = ("dislike", "exclude", "hide")


def _artist_of(obj):
    return re.sub(r"\s+", " ", str((obj or {}).get("artist") or "")).strip()


def _billed_as(obj, name):
    """曲目的记名歌手里是否含 name —— 严格相等，不做子串包含。

    '周杰伦 / 温岚' 这类合唱算（署名里有他）；'周杰伦♚'、'周杰伦(翻唱)'
    这类带后缀/山寨名不算。仅作词作曲参与的歌，上游记名是真实演唱者，
    自然被排除。
    """
    parts = re.split(r"[/／&、,，;；]|\s+(?:feat\.?|ft\.?|with)\s+",
                     _artist_of(obj), flags=re.I)
    k = _artist_key(name)
    return bool(k) and any(_artist_key(p) == k for p in parts if p.strip())


def _play_counts_by_artist():
    """每位歌手的真实收听次数（来自 plays 记录，不是加权分）。

    之前画像页显示的数字是「收藏×3 + 最近播放×2 + 反馈±4」这种加权分，
    跟用户理解的「听了几遍」对不上。这里按 song_key 归到记名歌手，
    数 plays 里每天的收听次数 —— 换音质不重复计，只在听完时计。
    """
    plays = STORE.data.get("plays") or {}
    cnt, cover = {}, {}
    for rec in plays.values():
        if not isinstance(rec, dict):
            continue
        days = rec.get("days") or {}
        n = sum(int(v or 0) for v in days.values())
        if n <= 0:
            continue
        meta = rec.get("meta") or {}
        name = _artist_of(meta)
        if not name:
            continue
        cnt[name] = cnt.get(name, 0) + n
        if not cover.get(name):
            cover[name] = meta.get("cover") or ""
    return cnt, cover


def taste_profile():
    """口味画像。

    两个口径严格分开，别再混：
      plays   —— 真实收听次数（听完才计，按歌不按音质）
      score   —— 排序用的亲和度（收藏/最近播放/显式反馈加权）
    页面上给用户看的必须是 plays，score 只用来排顺序。
    """
    data = STORE.data
    favs = data.get("favorites") or []
    recent = data.get("recent") or []
    fb = data.get("feedback") or {}
    score, sample = {}, {}

    def bump(name, w, it=None):
        n = str(name or "").strip()
        if not n:
            return
        score[n] = score.get(n, 0) + w
        if it and n not in sample:
            sample[n] = it.get("cover") or ""

    for f in favs:
        kind = f.get("kind") or "song"
        bump(f.get("name") if kind == "artist" else _artist_of(f), 3, f)
    for i, r in enumerate(recent[:30]):
        bump(_artist_of(r), 1 if i > 8 else 2, r)
    for v in fb.values():
        name = str(v.get("artist") or "").strip()
        if not name:
            continue
        if v.get("verdict") in FEEDBACK_NEGATIVE:
            score[name] = score.get(name, 0) - 4
        elif v.get("verdict") in ("like", "approve"):
            score[name] = score.get(name, 0) + 4

    plays_by_artist, play_cover = _play_counts_by_artist()

    # 出现在画像里的歌手 = 有收听记录 ∪ 有收藏 ∪ 有反馈
    names = set(plays_by_artist) | set(score)
    artists = []
    for n in names:
        sc = score.get(n, 0)
        pl = plays_by_artist.get(n, 0)
        if sc <= 0 and pl <= 0:
            continue
        artists.append({
            "name": n,
            "plays": pl,                     # 真实收听次数
            "score": sc,                     # 亲和度，仅用于排序
            "cover": play_cover.get(n) or sample.get(n, ""),
        })
    # 排序：先按真实收听次数，再按亲和度；都没听过但收藏过的排最后
    artists.sort(key=lambda a: (-a["plays"], -a["score"], a["name"]))

    total_plays = sum(plays_by_artist.values())
    distinct_songs = len({k for k, v in (STORE.data.get("plays") or {}).items()
                          if isinstance(v, dict) and sum(int(x or 0) for x in (v.get("days") or {}).values()) > 0})

    return {
        "artists": artists,
        "excluded": [k for k, v in fb.items() if v.get("verdict") in FEEDBACK_NEGATIVE],
        "liked": [k for k, v in fb.items() if v.get("verdict") in ("like", "approve")],
        "listened": total_plays,            # 真实收听总次数
        "songs": distinct_songs,            # 听过的不同曲目数
        "favorites": len(favs),
        "enabled": bool(data["settings"].get("personalized", True)),
        "genres": list(data.get("taste_genres") or []),
    }


_RECO_CACHE = {"at": 0, "pool": None, "seeds": []}
_RECO_ROUND = 0


# --------------------------------------------------------------------------
# 风格分类（本地内置，不依赖任何外部分类服务）
#
# 上游搜索结果里没有 genre 字段（实测只有 id/name/artist/album/minfo 等），
# 所以风格标签只能自己建：每个风格带一组代表歌手，用歌手去上游检索，
# 命中后严格按记名歌手校验。这样「摇滚」卡片里出来的就是真唱摇滚的歌手的歌，
# 不是名字里带「摇滚」两个字的蹭流歌曲。
#
# 命名沿用 Apple Music / Spotify 的通用习惯（用户认得出来），
# 但只保留用户真正常听的中日 + 欧美主流风格，不铺几千条细分。
# 分类体系调研见 _art/STYLE_TAXONOMY.md
# --------------------------------------------------------------------------
GENRES = [
    # (id, 中文名, 英文名, 分组, 代表歌手)
    ("pop", "流行", "Pop", "流行",
     ["周杰伦", "蔡依林", "林俊杰", "陈奕迅", "Taylor Swift", "Ed Sheeran"]),
    ("citypop", "都市流行", "City Pop", "流行",
     ["竹内まりや", "山下達郎", "Michael Jackson", "陈淑桦"]),
    ("mandopop", "华语流行", "Mandopop", "流行",
     ["林俊杰", "薛之谦", "张惠妹", "李荣浩"]),
    ("rock", "摇滚", "Rock", "摇滚",
     ["五月天", "朴树", "窦唯", "Coldplay", "Oasis", "Radiohead"]),
    ("indierock", "独立摇滚", "Indie Rock", "摇滚",
     ["落日飞车", "痛仰", "万能青年旅店", "Phum Viphurit"]),
    ("punk", "朋克", "Punk", "摇滚",
     ["新裤子", "脑浊", "Ramones", "Sex Pistols"]),
    ("folk", "民谣", "Folk", "民谣与根源",
     ["赵雷", "陈粒", "马頔", "Bob Dylan", "Fleet Foxes"]),
    ("acoustic", "原声", "Acoustic", "民谣与根源",
     ["陈绮贞", "苏打绿", "Iron & Wine"]),
    ("country", "乡村", "Country", "民谣与根源",
     ["Taylor Swift", "陈淑桦", "Kacey Musgraves"]),
    ("electronic", "电子", "Electronic", "电子与舞曲",
     ["Daft Punk", "Kraftwerk", "陈粒", "Hyukoh"]),
    ("house", "浩室", "House", "电子与舞曲",
     ["Avicii", "Frank Ocean", "Peggy Gou"]),
    ("futurebass", "Future Bass", "Future Bass", "电子与舞曲",
     ["Hatsune Miku", "Kotoko", "m-flo"]),
    ("techno", "Techno", "Techno", "电子与舞曲",
     ["Charlotte de Witte", "Amelie Lens"]),
    ("dnb", "Drum & Bass", "Drum & Bass", "电子与舞曲",
     ["Netsky", "Noisia"]),
    ("hiphop", "嘻哈", "Hip-Hop", "说唱与嘻哈",
     ["法老", "Higher Brothers", "Kendrick Lamar", "Eminem"]),
    ("trap", "Trap", "Trap", "说唱与嘻哈",
     ["Travis Scott", "Future"]),
    ("rnb", "R&B", "R&B", "R&B 与灵魂",
     ["方大同", "陶喆", "The Weeknd", "SZA"]),
    ("soul", "灵魂乐", "Soul", "R&B 与灵魂",
     ["James Brown", "Aretha Franklin"]),
    ("funk", "放克", "Funk", "R&B 与灵魂",
     ["Jamiroquai", "Vulfpeck"]),
    ("metal", "金属", "Metal", "金属与朋克",
     ["Metallica", "林志炫", "Ghost", "Dimmu Borgir"]),
    ("hardrock", "硬摇滚", "Hard Rock", "金属与朋克",
     ["Guns N' Roses", "崔健"]),
    ("jazz", "爵士", "Jazz", "爵士与蓝调",
     ["Miles Davis", "Chet Baker", "小野丽莎"]),
    ("blues", "蓝调", "Blues", "爵士与蓝调",
     ["B.B. King", "Gary Clark Jr."]),
    ("classical", "古典", "Classical", "古典与轻音乐",
     ["久石让", "坂本龙一", "Ludovico Einaudi", "Yiruma"]),
    ("ambient", "氛围音乐", "Ambient", "古典与轻音乐",
     ["Sigur Rós", "Ólafur Arnalds", "Nujabes"]),
    ("piano", "纯钢琴", "Piano", "古典与轻音乐",
     ["Yiruma", "久石让", "Joe Hisaishi"]),
    ("game", "游戏音乐", "Game OST", "二次元与游戏",
     ["米津玄师", "Aimer", "Yoasobi", "澤野弘之"]),
    ("anime", "动画歌曲", "Anime", "二次元与游戏",
     ["LiSA", "Mrs. GREEN APPLE", "RADWIMPS", "宇多田ヒカル"]),
    ("jpop", "J-POP", "J-Pop", "二次元与游戏",
     ["米津玄师", "Aimer", "宇多田ヒカル", "藤井風", "Ado"]),
    ("kpop", "K-POP", "K-Pop", "二次元与游戏",
     ["NewJeans", "BTS", "aespa"]),
    ("vocaloid", "虚拟歌手", "Vocaloid", "二次元与游戏",
     ["初音ミク", "Hatsune Miku", "Kagamine Rin"]),
    ("guofeng", "国风", "Chinese Style", "国风与东方",
     ["周杰伦", "李荣浩", "毛不易"]),
    ("gufeng", "古风", "Guofeng", "国风与东方",
     ["河图", "银临", "HITA", "Finale"]),
    ("chinese", "中国风", "Chinese Traditional", "国风与东方",
     ["邓丽君", "费玉清", "周杰伦"]),
    ("world", "世界音乐", "World", "世界音乐",
     ["Of Monsters and Men", "Shovels & Rope"]),
    ("latin", "拉丁", "Latin", "世界音乐",
     ["Bad Bunny", "Daddy Yankee", "J Balvin"]),
    ("reggae", "雷鬼", "Reggae", "世界音乐",
     ["Bob Marley", "Toots and the Maroons"]),
    ("lofi", "Lo-Fi", "Lo-Fi", "电子与舞曲",
     ["Nujabes", "Tom Misch", "Idealism"]),
    ("chill", "Chill", "Chill", "电子与舞曲",
     ["Bonobo", "Khruangbin"]),
    ("soundtrack", "影视原声", "Soundtrack", "古典与轻音乐",
     ["久石让", "Hans Zimmer", "千と千尋の神話"]),
]


def genre_list():
    """前端拿风格清单用。分组顺序固定，方便 UI 折叠。"""
    order, out = [], []
    for gid, name, en, group, seeds in GENRES:
        gid = gid.strip()
        if group not in order:
            order.append(group)
        out.append({"id": gid, "name": name, "en": en, "group": group,
                    "seeds": list(seeds)})
    return {"groups": order, "genres": out}


def genre_by_id(gid):
    for g in genre_list()["genres"]:
        if g["id"] == gid:
            return g
    return None


def _day_seed():
    """当日确定性种子：同一天同一风格拿到同一批歌，跨天自动换批。

    不用随机数 —— 刷新页面就变一批的话，用户会以为推荐在乱跳。
    """
    day = time.strftime("%Y-%m-%d")
    return int(hashlib.md5(day.encode("utf-8")).hexdigest()[:8], 16)


def _style_weight():
    """每个风格当前的权重：用户在该风格下歌手的收听次数 + 用户手动选中的加成。"""
    plays_by_artist, _ = _play_counts_by_artist()
    picked = set(STORE.data.get("taste_genres") or [])
    w = {}
    for g in genre_list()["genres"]:
        n = sum(plays_by_artist.get(s, 0) for s in g["seeds"])
        if g["id"] in picked:
            n += 6          # 用户明确选过的风格，优先级抬高
        w[g["id"]] = n
    return w


# 全风格卡片的基础缓存：卡片元数据 + 每张卡的完整候选池（不含轮次窗口）。
# 「换一批」不重建这份东西，只从池子里重新切窗口 —— 所以换批是纯切片，
# 既不碰上游，也不重复跑去重/记名校验。
_STYLE_BASE = {"at": 0, "data": None, "key": ""}
_STYLE_ROUND = 0
# 上游「按歌手搜一页」的结果缓存：seed -> (取回时间, 曲目列表)
_STYLE_SEEDS = {}
_STYLE_SEED_TTL = 900
# 空结果只挡 60 秒：上游抖一下不该让这个歌手被永久判死，
# 但也不能每次换批都为一个已经空了的种子再等一次超时。
_STYLE_SEED_EMPTY_TTL = 60
_STYLE_SEED_EMPTY = {}


def _seed_search(seed):
    """带缓存的上游检索。失败时退回上一次的成功结果，不把空列表写进长期缓存。"""
    now = time.time()
    hit = _STYLE_SEEDS.get(seed)
    if hit and now - hit[0] < _STYLE_SEED_TTL:
        return hit[1]
    miss = _STYLE_SEED_EMPTY.get(seed)
    if not hit and miss and now - miss < _STYLE_SEED_EMPTY_TTL:
        return []
    try:
        d = search_all(seed, 1, 20)
    except Exception:      # noqa: BLE001
        return hit[1] if hit else []
    lst = d.get("list") or []
    if lst:
        _STYLE_SEEDS[seed] = (now, lst)
        _STYLE_SEED_EMPTY.pop(seed, None)
    else:
        _STYLE_SEED_EMPTY[seed] = now
    return lst


def _window_of(pool, per, jitter=0, round_=0):
    """从候选池里切一段。纯本地运算，换批就靠它。"""
    if per <= 0:
        return pool
    if len(pool) <= per:
        # 池子还没装满一窗（小众风格经记名校验后常常只剩五六首）。
        # 这时从池子里取窗口永远是同一份 —— 用户点「换一批」会觉得按钮坏了。
        # 轮转一下顺序：歌就这么多是实话，但至少首曲和排列是新的。
        if round_ and len(pool) > 1:
            k = round_ % len(pool)
            pool = pool[k:] + pool[:k]
        return pool[:per]
    span = len(pool) - per + 1
    start = (jitter + round_ * per) % span
    return pool[start:start + per]


def style_playlists(limit=8, per=6, round_=0):
    """SP 式「全风格卡片歌单」。

    卡片 = 风格；卡片内 = 该风格下的真实曲目。
    排序：用户口味加权高的排前面，但每天轮换位次，
    保证「每天打开看到不同的风格组合」，同时同一天稳定不变。

    round_ 是「换一批」的轮次：卡片集合与候选池按「当日种子」定，
    换批只改每张卡窗口的起点 —— 缓存里存的是池子，不是切好的结果，
    所以连点十次「换一批」也只是十次切片，没有一次上游请求。
    """
    key = "%s|%s|%s" % (limit, per, _day_seed())
    now = time.time()
    b = _STYLE_BASE
    base = None
    if b["data"] is not None and b["key"] == key and now - b["at"] < 1800:
        base = b["data"]
    if base is None:
        w = _style_weight()
        seed = _day_seed()
        gs = genre_list()["genres"]

        # 轮换：用当日种子给每个风格算一个伪随机位次，
        # 权重高的整体往前挪一点，而不是硬按权重排死。
        scored = []
        for i, g in enumerate(gs):
            jitter = int(hashlib.md5(("%s|%s" % (seed, g["id"])).encode()).hexdigest()[:6], 16)
            scored.append((g, w.get(g["id"], 0), jitter))
        scored.sort(key=lambda x: (-x[1], x[2]))

        hot = [x for x in scored if x[1] > 0]
        cold = [x for x in scored if x[1] <= 0]
        order = hot + cold if hot else cold

        # 并发检索各风格的曲目：串行 8 张卡要 30s 以上。
        # 这里把所有 (风格, 歌手) 组合摊平后一次性并发取回，
        # 再按风格归组 —— 8 风格 × 3 seed = 24 个请求一起打，而不是 8×3 轮串行。
        import concurrent.futures as _cf
        head = order[:limit]

        jobs = []                       # (entry, seed)
        for entry in head:
            for s in _genre_seeds(entry[0]):
                jobs.append((entry, s))

        fetched = {}

        def grab(job):
            entry, seed = job
            return job, _seed_search(seed)

        if jobs:
            with _cf.ThreadPoolExecutor(max_workers=12) as ex:
                for job, lst in ex.map(grab, jobs):
                    fetched.setdefault(job[0][0]["id"], []).append((job[1], lst))

        base = []
        for entry in head:
            g, weight, jitter = entry
            # window=False：这里只攒候选池，切窗口留给下面统一做。
            pool = _assemble_genre(g, jitter, fetched.get(g["id"], []), per, 0,
                                   window=False)
            if not pool:
                continue
            base.append({"genre": g["id"], "name": g["name"], "en": g["en"],
                         "group": g["group"], "weight": weight,
                         "jitter": jitter, "pool": pool})
        if base:
            _STYLE_BASE.update(at=now, data=base, key=key)

    # 换批只走到这里：池子已经在了，按轮次切一段即可。
    cards = []
    for c in base:
        cards.append({"genre": c["genre"], "name": c["name"], "en": c["en"],
                      "group": c["group"], "weight": c["weight"],
                      "list": _window_of(c["pool"], per, c["jitter"], round_)})
    return {"cards": cards, "day": time.strftime("%Y-%m-%d"),
            "genres": genre_list()["genres"], "round": round_}


def _genre_seeds(g):
    """该风格用于检索的歌手：风格自带代表歌手 + 用户手动选中时的偏好歌手。"""
    seeds = list(g["seeds"])
    if g["id"] in set(STORE.data.get("taste_genres") or []):
        plays_by_artist, _ = _play_counts_by_artist()
        top = sorted(plays_by_artist.items(), key=lambda x: -x[1])[:2]
        seeds = [s for s, _ in top] + seeds
    return seeds[:3]


def _assemble_genre(g, jitter, got, per, round_=0, window=True):
    """把各 seed 的检索结果合成一个风格歌单。

    严格按记名歌手校验（_billed_as），只认真唱这首歌的人；
    同平台 id 去重 + 同歌名同歌手去重（跨平台同名歌只留一条）。

    轮转取歌：每个 seed 轮流出一首，避免整张卡都是同一个人的歌
    （上游按歌手搜索，命中结果天然集中在该歌手身上）。
    """
    pairs = list(got)
    if not pairs:
        return []
    # 当日抖动：换一个 seed 起点，歌会换但风格不变
    off = jitter % len(pairs)
    pairs = pairs[off:] + pairs[:off]

    # 每个 seed 先过一遍记名校验与去重
    buckets = []
    for seed, lst in pairs:
        seen, titles, keep = set(), set(), []
        for it in lst:
            key = "%s:%s" % (it.get("platform"), it.get("id"))
            if key in seen or not _billed_as(it, seed):
                continue
            tk = "%s|%s" % (re.sub(r"\s+", "", it.get("name") or "").lower(),
                            re.sub(r"\s+", "", _artist_of(it)).lower())
            if tk in titles:
                continue
            seen.add(key)
            titles.add(tk)
            keep.append(it)
        buckets.append(keep)

    out, seen, seen_title = [], set(), set()
    i = 0
    # 先交错排到足够长（最多 per*3），再按轮次取一段窗口。
    # 只取 per 首的话「换一批」永远只能换出同样的前几首 —— 候选池明明有几十首。
    want = per if per <= 0 else per * 3
    while len(out) < want and any(buckets):
        b = buckets[i % len(buckets)]
        if b:
            it = b.pop(0)
            key = "%s:%s" % (it.get("platform"), it.get("id"))
            tk = "%s|%s" % (re.sub(r"\s+", "", it.get("name") or "").lower(),
                            re.sub(r"\s+", "", _artist_of(it)).lower())
            if key not in seen and tk not in seen_title:
                seen.add(key)
                seen_title.add(tk)
                out.append(it)
        i += 1
    if not window:
        return out                    # 完整候选池，调用方按轮次自己切
    if len(out) <= per or per <= 0:
        return out[:per] if per > 0 else out
    # 窗口起点同时受当日抖动与「换一批」轮次影响：同一天里每点一次都是新的组合，
    # 但都是本地切片，没有任何上游请求。
    start = (jitter + round_ * per) % (len(out) - per + 1)
    return out[start:start + per]


def _genre_items(g, per, jitter):
    """兼容入口：单风格取歌（供其它地方复用）。"""
    pairs = []
    for s in _genre_seeds(g):
        try:
            d = search_all(s, 1, 20)
        except Exception:      # noqa: BLE001
            continue
        pairs.append((s, d.get("list") or []))
    return _assemble_genre(g, jitter, pairs, per)


def _reco_page(prof, limit):
    """从候选池里切一页。

    池子存的是完整候选（不是某一页），所以「换一批」和「显示全部」
    都只是换窗口 —— 不发请求、不等上游，点下去立刻就有。
    """
    pool = _RECO_CACHE.get("pool") or []
    limit = max(1, limit)
    span = max(1, len(pool) - limit + 1)
    off = 0
    if len(pool) > limit:
        off = (_RECO_ROUND * limit) % span
    page = pool[off:off + limit]
    return {"list": page, "seeds": _RECO_CACHE.get("seeds") or [], "disabled": False,
            "profile": prof, "pool": len(pool), "round": _RECO_ROUND, "limit": limit}


def recommend_candidates(limit=12, fresh=False):
    """用 top 歌手当 seed，跨源搜相似曲目；排除已收藏/已排除/最近听过。

    候选池只建一份（不按 limit 分开建），首页 12 条和「显示全部」26 条
    都从同一个池子里切 —— 否则点展开会命中 12 条的缓存，看着像按钮失灵。

    fresh=True 是「换一批」：池子里还有没露过面的候选就立刻换一页，
    只有池子本身不够长时才真的回上游。之前是无条件清缓存重取 ——
    用户点完盯着不动的页面等十几秒，这就是「加载很缓慢」的来源。
    """
    now = time.time()
    c = _RECO_CACHE
    global _RECO_ROUND
    prof = taste_profile()
    if not prof["enabled"]:
        return {"list": [], "seeds": [], "disabled": True, "profile": prof}

    pool = c.get("pool")
    pool_ok = bool(pool) and now - c.get("at", 0) < 300
    if pool_ok and len(pool) > limit:
        if fresh:
            _RECO_ROUND += 1
        return _reco_page(prof, limit)

    seeds = [a["name"] for a in prof["artists"][:4]]
    if not seeds:                       # 冷启动：退回最近播放里的歌手
        seeds = list(dict.fromkeys(
            _artist_of(r) for r in (STORE.data.get("recent") or []) if _artist_of(r)))[:3]

    banned = set(prof["excluded"])
    seen = set(banned)
    for r in (STORE.data.get("recent") or [])[:40]:
        seen.add("%s:%s" % (r.get("platform"), r.get("id")))
    for f in (STORE.data.get("favorites") or []):
        if (f.get("kind") or "song") == "song":
            seen.add("%s:%s" % (f.get("platform"), f.get("id")))

    out, seen_titles = [], set()
    target = max(48, limit * 2)         # 池子要够长，换批才有东西可换

    def title_key(name, artist):
        n = str(name or "")
        n = re.sub(r"[-－]\s*《[^》]*》.*$", "", n)          # 去掉 -《电影》片尾曲 之类
        n = re.sub(r"[（(][^）)]*(电影|电视剧|主题曲|片尾曲|插曲)[^）)]*[）)]", "", n)
        return re.sub(r"\s+", "", n).lower() + "|" + re.sub(r"\s+", "", artist or "").lower()

    for s in seeds[:4]:
        try:
            d = _seed_search(s)
        except Exception:  # noqa: BLE001
            continue
        for it in (d or []):
            key = "%s:%s" % (it.get("platform"), it.get("id"))
            if key in seen:
                continue
            a = re.sub(r"\s+", "", _artist_of(it))
            if not _billed_as(it, s):      # 只认记名歌手，别把相关性召回的歌混进来
                continue
            # 同一首歌在两个平台各来一条 → 只留一条，避免列表看着乱
            tk = title_key(it.get("name"), a)
            if tk in seen_titles:
                continue
            seen_titles.add(tk)
            it["reco_seed"] = s
            out.append(it)
            seen.add(key)
            if len(out) >= target:
                break
        if len(out) >= target:
            break
    _RECO_CACHE.update(at=now, pool=out, seeds=seeds)
    return _reco_page(prof, limit)


# --------------------------------------------------------------------------
# 音质挑选（含降级顺序）
# --------------------------------------------------------------------------
def _ibr(m):
    try:
        return int(m.get("bitrate") or 0)
    except (TypeError, ValueError):
        return 0


def quality_candidates(item, want):
    """返回按优先级排列的候选音质列表；目标缺失时依次降级。"""
    minfo = item.get("minfo") or []
    if not minfo:
        return []
    flacs = sorted((m for m in minfo if m.get("format") == "flac"), key=_ibr, reverse=True)
    mp3s = sorted((m for m in minfo if m.get("format") == "mp3"), key=_ibr, reverse=True)

    if want in (None, "", "best", "flac"):
        first = flacs or mp3s
    elif want in ("2000", "646"):
        hit = [m for m in flacs if _ibr(m) == int(want)]
        first = hit or flacs or mp3s
    elif want in ("320", "128"):
        hit = [m for m in mp3s if _ibr(m) == int(want)]
        first = hit or [m for m in mp3s if _ibr(m) <= int(want)] or mp3s or flacs
    else:
        first = flacs or mp3s

    ordered, seen = [], set()
    for m in list(first) + flacs + mp3s:
        key = (m.get("format"), str(m.get("bitrate")))
        if key not in seen:
            seen.add(key)
            ordered.append(m)
    return ordered


def pick_quality(item, want):
    cands = quality_candidates(item, want)
    return cands[0] if cands else None


def sniff_ext(url, fallback="bin"):
    """接口标的 format 常常不准（128k 给 m4a），靠 magic 定后缀。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-15"})
        with urllib.request.urlopen(req, timeout=20) as r:
            head = r.read(16)
    except Exception:
        return fallback
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3" or (head[:1] and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a" if head[8:12] in (b"M4A ", b"mp42", b"isom") else "aac"
    if head[:3] == b"Ogg":
        return "ogg"
    return fallback


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s or "")).strip(" .")
    return s[:150] or "unknown"


def render_template(tpl, item, fmt="", bitrate=""):
    text = tpl or "{name} - {artist}"
    values = {
        "name": item.get("name") or "",
        "artist": item.get("artist") or "",
        "album": item.get("album_name") or "",
        "platform": item.get("platform") or "",
        "format": fmt or "",
        "bitrate": str(bitrate or ""),
        "id": str(item.get("id") or ""),
    }
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    text = re.sub(r"\s*-\s*$", "", text)
    return safe_name(text.strip(" -"))


def target_path(folder, base, ext, policy):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{base}.{ext}")
    if not os.path.exists(path):
        return path
    if policy == "overwrite":
        return path
    if policy == "skip":
        return None
    i = 2
    while os.path.exists(os.path.join(folder, f"{base} ({i}).{ext}")):
        i += 1
    return os.path.join(folder, f"{base} ({i}).{ext}")


# --------------------------------------------------------------------------
# 标签写入
# --------------------------------------------------------------------------
def fetch_bytes(url, limit=4 * 1024 * 1024):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": UPSTREAM})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(limit)
        return data or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# 试听缓存
# --------------------------------------------------------------------------
AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "bin": "application/octet-stream",
}


class MediaCache:
    """两层试听缓存。

    today/：当天听满 N 次的歌，跨天清空。
    long/ ：最近 7 天听满 5 次以上的歌，3 天没再听就自动删。
    """

    def __init__(self, root):
        self.root = root
        self.today_root = os.path.join(root, "today")
        self.long_root = os.path.join(root, "long")
        self.index_path = os.path.join(self.long_root, "index.json")
        os.makedirs(self.today_root, exist_ok=True)
        os.makedirs(self.long_root, exist_ok=True)
        self.lock = threading.RLock()
        self.warming = set()
        self._day = None
        self._purge_legacy()
        self.roll()
        self.evict_long()

    def _purge_legacy(self):
        """清掉旧版平铺目录（cache/<日期>/ 或 cache/ku/）。"""
        for name in list(os.listdir(self.root)):
            if name in ("today", "long"):
                continue
            full = os.path.join(self.root, name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass

    # -- 天目录 ---------------------------------------------------------
    def roll(self):
        day = time.strftime("%Y-%m-%d")
        if day == self._day:
            return day
        with self.lock:
            self._day = day
            os.makedirs(os.path.join(self.today_root, day), exist_ok=True)
        for name in list(os.listdir(self.today_root)):
            full = os.path.join(self.today_root, name)
            if os.path.isdir(full) and name != day:
                shutil.rmtree(full, ignore_errors=True)
        self._clean_parts(self.today_root)
        return day

    @staticmethod
    def _clean_parts(base):
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith(".part"):
                    try:
                        os.remove(os.path.join(dirpath, f))
                    except OSError:
                        pass

    @staticmethod
    def safe_key(raw):
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw))[:120] or "x"

    @staticmethod
    def _ext(ext):
        return re.sub(r"[^a-z0-9]", "", (ext or "bin").lower())[:5] or "bin"

    def path_today(self, key, ext):
        k = self.safe_key(key)
        return os.path.join(self.today_root, self.roll(), k[:2], k + "." + self._ext(ext))

    def path_long(self, key, ext):
        k = self.safe_key(key)
        return os.path.join(self.long_root, k[:2], k + "." + self._ext(ext))

    # -- 长时缓存索引 ---------------------------------------------------
    def load_index(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def save_index(self, idx):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_path)

    def _scan(self, base, key):
        k = self.safe_key(key)
        d = os.path.join(base, k[:2])
        if not os.path.isdir(d):
            return None
        for name in os.listdir(d):
            if name.startswith(k + ".") and not name.endswith(".part"):
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.path.getsize(p) > 1024:
                    return p
        return None

    def find(self, key, tier="any"):
        if tier in ("any", "today"):
            p = self._scan(os.path.join(self.today_root, self.roll()), key)
            if p:
                try:
                    os.utime(p, None)
                except OSError:
                    pass
                return p
            if tier == "today":
                return None
        p = self._scan(self.long_root, key)
        if p:
            self.touch_long(key)          # 长期缓存被用到就续命
        return p

    def touch_long(self, key):
        with self.lock:
            idx = self.load_index()
            if key in idx:
                idx[key]["last_at"] = int(time.time())
                self.save_index(idx)

    def evict_long(self, days=3):
        """长期缓存：超过 N 天没听过就删掉。"""
        cutoff = time.time() - days * 86400
        removed = 0
        with self.lock:
            idx = self.load_index()
            for k in [k for k, v in idx.items() if int(v.get("last_at") or 0) < cutoff]:
                f = idx[k].get("file") or self._scan(self.long_root, k)
                if f:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                idx.pop(k, None)
                removed += 1
            if removed:
                self.save_index(idx)
        self._clean_parts(self.long_root)
        return removed

    # -- 统计 -----------------------------------------------------------
    def _sum(self, base):
        total = count = 0
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith((".part", ".json")):
                    continue
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                    count += 1
                except OSError:
                    pass
        return total, count

    def stat(self):
        t_size, t_count = self._sum(os.path.join(self.today_root, self.roll()))
        l_size, l_count = self._sum(self.long_root)
        return {"size": t_size + l_size, "count": t_count + l_count,
                "today_size": t_size, "today_count": t_count,
                "long_size": l_size, "long_count": l_count}

    def enforce(self, limit_mb):
        limit = max(64, int(limit_mb or 2048)) * 1024 * 1024
        st = self.stat()
        if st["size"] <= limit:
            return
        entries = []
        for base in (self.long_root, os.path.join(self.today_root, self.roll())):
            for dirpath, _, files in os.walk(base):
                for f in files:
                    if f.endswith((".part", ".json")):
                        continue
                    p = os.path.join(dirpath, f)
                    try:
                        entries.append((os.path.getmtime(p), os.path.getsize(p), p))
                    except OSError:
                        pass
        entries.sort()
        total = st["size"]
        for _, size, p in entries:
            if total <= limit:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass

    def clear(self, tier="all"):
        if tier in ("all", "today"):
            shutil.rmtree(os.path.join(self.today_root, self.roll()), ignore_errors=True)
            os.makedirs(os.path.join(self.today_root, self.roll()), exist_ok=True)
        if tier in ("all", "long"):
            for name in list(os.listdir(self.long_root)):
                full = os.path.join(self.long_root, name)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
            try:
                os.remove(self.index_path)
            except OSError:
                pass
        return self.stat()

    # -- 落盘 -----------------------------------------------------------
    def warm(self, key, url, ext, tier="today", meta=None):
        key = self.safe_key(key)
        if self.find(key, tier) or key in self.warming:
            return False
        # 已经在另一层存着：直接挪过去，不重复下载
        if tier == "long":
            src = self._scan(os.path.join(self.today_root, self.roll()), key)
            if src:
                dst = self.path_long(key, os.path.splitext(src)[1].lstrip("."))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.replace(src, dst)
                    self._remember_long(key, dst, meta)
                    return True
                except OSError:
                    pass
        self.warming.add(key)

        def run():
            tmp = (self.path_long(key, ext) if tier == "long" else self.path_today(key, ext)) + ".part"
            final = tmp[:-5]
            try:
                os.makedirs(os.path.dirname(tmp), exist_ok=True)
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": UPSTREAM})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(tmp, final)
                if tier == "long":
                    self._remember_long(key, final, meta)
                self.enforce(STORE.data["settings"].get("cache_limit_mb", 2048))
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            finally:
                self.warming.discard(key)

        threading.Thread(target=run, daemon=True).start()
        return True

    def _remember_long(self, key, path, meta=None):
        with self.lock:
            idx = self.load_index()
            idx[key] = {"file": path, "last_at": int(time.time()), "meta": meta or {}}
            self.save_index(idx)


CACHE = MediaCache(os.path.join(data_dir(), "cache"))


def resolve_play_url(platform, sid, fmt, br, sign_time=None, sign=None, name=None):
    """拿到可播放直链。主站签名过期就重搜换签；主站整条挂掉时网易云转备用站。"""
    def call(t, s):
        data = upstream("getUrl", {"platform": platform, "songid": sid, "format": fmt,
                                   "bitrate": br, "time": t, "sign": s})
        return (data or {}).get("url")

    def backup():
        if platform == "wyy":
            return gd_geturl(sid, fmt, br)
        return None

    try:
        url = call(sign_time, sign)
        if url:
            return url
    except Exception as e:  # noqa: BLE001
        if "过期" not in str(e):
            alt = backup()
            if alt:
                return alt
            raise

    if name:
        try:
            data = upstream("search", {"platform": platform, "keyword": name, "page": 1, "size": 20})
            for cand in (data or {}).get("list") or []:
                if str(cand.get("id")) == str(sid):
                    url = call(cand.get("time"), cand.get("sign"))
                    if url:
                        return url
        except Exception:  # noqa: BLE001
            pass

    alt = backup()
    if alt:
        return alt
    raise RuntimeError("解析直链失败")


# --------------------------------------------------------------------------
# 音质实测：不信上游声明的 format，只信真实字节
# --------------------------------------------------------------------------
_MAGIC = (
    (b"fLaC", "flac", "FLAC"),
    (b"OggS", "ogg", "OGG"),
    (b"RIFF", "wav", "WAV"),
    (b"ID3", "mp3", "MP3"),
    (b"\xff\xfb", "mp3", "MP3"),
    (b"\xff\xf3", "mp3", "MP3"),
    (b"\xff\xf2", "mp3", "MP3"),
)


def sniff_container(head):
    """按文件头判容器。fLaC 才算真无损，别的都不许标 FLAC。"""
    if not head:
        return "", ""
    for sig, fmt, label in _MAGIC:
        if head.startswith(sig):
            return fmt, label
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a", "M4A"
    if head.startswith(b"\xff\xf1") or head.startswith(b"\xff\xf9"):
        return "aac", "AAC"
    return "bin", "未知"


_PROBE_CACHE = {}
_PROBE_LOCK = threading.Lock()


def probe_one(platform, sid, fmt, br, sign_time=None, sign=None, duration=0, name=None):
    """探一首歌：只拉 1 字节判真格式 + 读 Content-Range 拿大小算真实码率。

    返回 valid/fmt/label/size/kbps/container，全程只消耗几百字节。
    """
    key = ("%s|%s|%s|%s" % (platform, sid, fmt, br))
    now = time.time()
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    out = {"valid": False, "fmt": "", "label": "", "size": 0, "kbps": 0,
           "container": "", "declared": str(fmt or "").lower(), "reason": ""}
    try:
        url = resolve_play_url(platform, sid, fmt, br, sign_time, sign, name)
    except Exception as e:  # noqa: BLE001
        out["reason"] = "解析失败: %s" % e
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = (now, out)
        return out
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Range": "bytes=0-15", "Referer": UPSTREAM,
            "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=12) as r:
            head = r.read(16)
            cr = r.headers.get("Content-Range") or ""
            size = 0
            if "/" in cr:
                try:
                    size = int(cr.rsplit("/", 1)[1])
                except Exception:
                    size = 0
            if not size:
                try:
                    size = int(r.headers.get("Content-Length") or 0)
                except Exception:
                    size = 0
            if r.status in (200, 206) and head:
                out["valid"] = True
                out["size"] = size
                fmt2, label = sniff_container(head)
                out["fmt"], out["label"] = fmt2, label
                out["container"] = fmt2
                if size and duration:
                    try:
                        out["kbps"] = int(round(size * 8 / float(duration) / 1000))
                    except Exception:
                        out["kbps"] = 0
                out["mismatch"] = (out["declared"] == "flac" and fmt2 not in ("flac", ""))
    except Exception as e:  # noqa: BLE001
        out["reason"] = "不可达: %s" % e
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = (now, out)
        if len(_PROBE_CACHE) > 800:
            for k in sorted(_PROBE_CACHE, key=lambda k: _PROBE_CACHE[k][0])[:200]:
                _PROBE_CACHE.pop(k, None)
    return out


def probe_batch(items, limit=64, workers=8, deadline=20.0):
    """批量探测：并发跑，分片由前端控制（一次 12 条），别让整页等太久。

    带一个软截止：上游偶发挂住时，一批里只要有一条卡到超时，ex.map 就会
    拖着整批不回，前端的格子永远停在「检测中」。所以按完成顺序收，
    到点就返回已拿到的，没赶上的标成待测（前端有对应静态态）。
    """
    todo = []
    for it in (items or [])[:limit]:
        plat, sid = it.get("platform"), it.get("id")
        if not plat or not sid:
            continue
        q = it.get("_probeq") or {}
        todo.append((it, plat, sid, q.get("format") or "mp3", str(q.get("bitrate") or "320"),
                     it.get("time"), it.get("sign"), int(it.get("duration") or 0),
                     it.get("name")))
    out = {}
    if not todo:
        return out

    def run(t):
        it, plat, sid, fmt, br, st, sg, dur, nm = t
        res = probe_one(plat, sid, fmt, br, st, sg, dur, nm)
        res["key"] = "%s|%s|%s|%s" % (plat, sid, fmt, br)
        return (plat, str(sid), res)

    ex = _cf.ThreadPoolExecutor(max_workers=max(1, min(8, workers)))
    futs = {ex.submit(run, t): (t[1], str(t[2])) for t in todo}
    try:
        for fut in _cf.as_completed(futs, timeout=deadline):
            try:
                plat, sid, res = fut.result()
            except Exception:  # noqa: BLE001
                continue
            out["%s:%s" % (plat, sid)] = res
    except _cf.TimeoutError:
        for fut, (plat, sid) in futs.items():
            if not fut.done():
                fut.cancel()
                out["%s:%s" % (plat, sid)] = {
                    "valid": False, "unresolved": True, "reason": "探测超时，可稍后重试",
                    "fmt": "", "label": "", "size": 0, "kbps": 0, "container": "",
                    "declared": "", "mismatch": False}
    finally:
        ex.shutdown(wait=False)
    # 没进结果的（请求异常 / 被取消）也补一个待测态，别留空格子
    for t in todo:
        k = "%s:%s" % (t[1], str(t[2]))
        out.setdefault(k, {"valid": False, "unresolved": True, "reason": "探测未返回结果",
                           "fmt": "", "label": "", "size": 0, "kbps": 0, "container": "",
                           "declared": "", "mismatch": False})
    return out


def play_key(item, quality=None):
    """缓存文件身份：同一首歌不同音质是不同文件。"""
    q = quality or {}
    return "%s_%s_%s_%s" % (item.get("platform"), item.get("id"),
                            q.get("bitrate") or "", q.get("format") or "")


def song_key(item):
    """收听计数身份：只认歌曲本身，切换音质不算换了一首。"""
    return "%s_%s" % (item.get("platform"), item.get("id"))


def merge_legacy_plays(plays, key, plat, sid, day):
    """旧版把收听次数按音质分开存（play_key），升级后合并到 song_key。

    不合并的话用户切一次音质、或升级到这版，历史收听次数会凭空归零，
    缓存规则看起来就像坏了。
    """
    prefix = "%s_%s_" % (plat, sid)
    merged = None
    for old in [k for k in list(plays) if k.startswith(prefix) and k != key]:
        r = _migrate_play_record(plays.pop(old), day)
        if merged is None:
            merged = r
            continue
        for d, n in (r.get("days") or {}).items():
            merged["days"][d] = int(merged["days"].get(d) or 0) + int(n)
        merged["last_at"] = max(int(merged.get("last_at") or 0), int(r.get("last_at") or 0))
        merged["cached_today"] = bool(merged.get("cached_today") or r.get("cached_today"))
        merged["cached_long"] = bool(merged.get("cached_long") or r.get("cached_long"))
    return merged


def cache_track(item, quality, tier="today"):
    """把一首歌整首拉进缓存。签名过期会自动重搜换新签名。"""
    plat, sid = item.get("platform"), item.get("id")
    if not plat or not sid:
        return False
    fmt = (quality or {}).get("format") or "mp3"
    br = str((quality or {}).get("bitrate") or "320")
    key = play_key(item, quality)
    if CACHE.find(key, tier):
        return False
    try:
        url = resolve_play_url(plat, sid, fmt, br,
                               item.get("time"), item.get("sign"), item.get("name"))
    except Exception:
        return False
    meta = {"id": item.get("id"), "name": item.get("name"),
            "artist": item.get("artist"), "platform": plat}
    return CACHE.warm(key, url, fmt, tier=tier, meta=meta)


def _migrate_play_record(rec, day):
    """兼容旧版 {day,count,cached} 结构。"""
    if not isinstance(rec, dict):
        return {"days": {}, "last_at": 0, "cached_today": False, "cached_long": False}
    if "days" in rec:
        rec.setdefault("days", {})
        rec.setdefault("cached_today", rec.get("cached", False))
        rec.setdefault("cached_long", False)
        rec.setdefault("last_at", 0)
        return rec
    days = {}
    if rec.get("day") == day and rec.get("count"):
        days[day] = int(rec["count"])
    return {"days": days, "last_at": int(time.time()) if days else 0,
            "cached_today": bool(rec.get("cached")), "cached_long": False}


def record_play(payload):
    """记一次收听：只有听完才计数。

    当天听完 N 次  → 进当天缓存（跨天清空）
    7 天内听完 >M 次 → 进长期缓存（3 天没听就清）
    """
    item = (payload or {}).get("item") or {}
    quality = (payload or {}).get("quality") or {}
    completed = bool((payload or {}).get("completed"))
    plat, sid = item.get("platform"), item.get("id")
    if not plat or not sid:
        return {"ok": False, "error": "缺少曲目信息"}

    key = song_key(item)                  # 计数按「歌」，换音质不重置
    cache_key = play_key(item, quality)   # 缓存文件仍按音质分开
    day = time.strftime("%Y-%m-%d")
    st = STORE.data["settings"]
    need_today = max(1, int(st.get("cache_min_plays") or 2))
    need_week = max(1, int(st.get("cache_week_plays") or 5))
    auto = st.get("auto_cache", True) is not False

    with STORE.lock:
        plays = STORE.data.setdefault("plays", {})
        if key not in plays:
            legacy = merge_legacy_plays(plays, key, plat, sid, day)
            if legacy:
                plays[key] = legacy
        rec = _migrate_play_record(plays.get(key), day)
        days = rec["days"]
        if completed:
            days[day] = int(days.get(day) or 0) + 1
            rec["last_at"] = int(time.time())
            rec["meta"] = {"id": item.get("id"), "name": item.get("name"),
                           "artist": item.get("artist"), "platform": plat}
            recent = STORE.data.setdefault("recent", [])
            recent[:] = [r for r in recent
                         if not (r.get("platform") == plat and str(r.get("id")) == str(sid))]
            entry = dict(item)
            entry["platform"] = plat
            entry["played_at"] = int(time.time())
            recent.insert(0, entry)
            del recent[60:]
            rec["quality"] = {"format": quality.get("format"), "bitrate": quality.get("bitrate")}
        # 只保留最近 7 天
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        rec["days"] = {d: n for d, n in days.items() if d >= cutoff}
        plays[key] = rec
        STORE.save()

    today_count = int(rec["days"].get(day) or 0)
    week_count = sum(int(n) for n in rec["days"].values())

    triggered = None
    if completed and auto:
        if today_count >= need_today and not rec.get("cached_today"):
            if cache_track(item, quality, "today"):
                triggered = "today"
                with STORE.lock:
                    STORE.data["plays"][key]["cached_today"] = True
                    STORE.save()
        if week_count > need_week and not rec.get("cached_long"):
            if cache_track(item, quality, "long"):
                triggered = triggered or "long"
                with STORE.lock:
                    STORE.data["plays"][key]["cached_long"] = True
                    STORE.save()
    elif rec.get("cached_long"):
        CACHE.touch_long(cache_key)           # 长期缓存续命（按音质文件）

    CACHE.evict_long(int(st.get("cache_keep_days") or 3))

    return {"ok": True, "data": {
        "key": key, "day": day,
        "today_count": today_count, "week_count": week_count,
        "need_today": need_today, "need_week": need_week,
        "cached_today": bool(rec.get("cached_today")),
        "cached_long": bool(rec.get("cached_long")),
        "triggered": triggered,
    }}


def write_tags(path, item, lyric=None, cover=None):
    """把标题/歌手/专辑/封面/歌词写进标签。失败一律忽略，不影响下载结果。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        name = item.get("name") or ""
        artist = item.get("artist") or ""
        album = item.get("album_name") or ""

        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(path)
            if name:
                f["title"] = name
            if artist:
                f["artist"] = artist
            if album:
                f["album"] = album
            if lyric:
                f["lyrics"] = lyric
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.data = cover
                f.add_picture(pic)
            f.save()

        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC, TALB, TIT2, TPE1, USLT, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            if name:
                tags.delall("TIT2")
                tags.add(TIT2(encoding=3, text=name))
            if artist:
                tags.delall("TPE1")
                tags.add(TPE1(encoding=3, text=artist))
            if album:
                tags.delall("TALB")
                tags.add(TALB(encoding=3, text=album))
            if lyric:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric))
            if cover:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            tags.save(path)

        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            m = MP4(path)
            if name:
                m["\xa9nam"] = [name]
            if artist:
                m["\xa9ART"] = [artist]
            if album:
                m["\xa9alb"] = [album]
            if lyric:
                m["\xa9lyr"] = [lyric]
            if cover:
                m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            m.save()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 下载队列
# --------------------------------------------------------------------------
class DownloadManager:
    """并发下载队列：自动降级、速度统计、取消、重试。"""

    MAX_WORKERS = 4

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.tasks = {}
        self.order = []
        self.q = queue.Queue()
        self.running = 0
        self._started = False

    def _ensure_workers(self):
        with self.lock:
            if self._started:
                return
            self._started = True
            for i in range(self.MAX_WORKERS):
                threading.Thread(target=self._worker, name=f"dl-{i}", daemon=True).start()

    def _worker(self):
        while True:
            tid = self.q.get()
            task = None
            try:
                with self.lock:
                    task = self.tasks.get(tid)
                    if not task or task["state"] != "queued":
                        continue
                    limit = max(1, int(self.store.data["settings"].get("concurrency") or 2))
                    waited = 0.0
                    while self.running >= limit:
                        self.lock.release()
                        try:
                            time.sleep(0.2)
                            waited += 0.2
                        finally:
                            self.lock.acquire()
                        task = self.tasks.get(tid)
                        if not task or task["state"] != "queued" or waited > 300:
                            break
                    if not task or task["state"] != "queued":
                        continue
                    task["state"] = "running"
                    task["started"] = time.time()
                    self.running += 1
                self._run(task)
            except Exception as e:  # noqa: BLE001
                if task:
                    with self.lock:
                        task["state"] = "error"
                        task["error"] = str(e)
            finally:
                with self.lock:
                    t = self.tasks.get(tid)
                    if t and t["state"] in ("done", "error", "cancelled", "skipped", "running"):
                        if t.get("started") and t["state"] != "queued":
                            self.running = max(0, self.running - 1)
                    try:
                        self.q.task_done()
                    except ValueError:
                        pass

    def _resolve(self, item, want):
        """按降级顺序解析直链，返回 (url, quality, ext)。"""
        last_err = None
        for q in quality_candidates(item, want):
            data = None
            try:
                data = upstream("getUrl", {
                    "platform": item.get("platform", "kuwo"),
                    "songid": item.get("id"),
                    "format": q["format"], "bitrate": q["bitrate"],
                    "time": item.get("time"), "sign": item.get("sign"),
                })
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "过期" in str(e) and self._refresh_sign(item):
                    try:
                        data = upstream("getUrl", {
                            "platform": item.get("platform", "kuwo"),
                            "songid": item.get("id"),
                            "format": q["format"], "bitrate": q["bitrate"],
                            "time": item.get("time"), "sign": item.get("sign"),
                        })
                    except Exception as e2:  # noqa: BLE001
                        last_err = e2
                        continue
            url = (data or {}).get("url")
            if url:
                return url, q, sniff_ext(url, q["format"])
        raise RuntimeError(f"所有音质都解析失败：{last_err}")

    def _refresh_sign(self, item):
        try:
            data = upstream("search", {"platform": item.get("platform", "kuwo"),
                                       "keyword": item.get("name", ""),
                                       "page": 1, "size": 20})
            for cand in (data or {}).get("list") or []:
                if str(cand.get("id")) == str(item.get("id")):
                    item["time"] = cand.get("time")
                    item["sign"] = cand.get("sign")
                    return True
        except Exception:
            pass
        return False

    def _run(self, task):
        item = task["item"]
        settings = self.store.data["settings"]
        want = task.get("want_quality") or settings.get("quality") or "best"
        folder = task.get("dir") or settings.get("download_dir") or default_download_dir()

        try:
            url, q, ext = self._resolve(item, want)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                task.update(state="error", error=str(e), speed=0)
            return

        base = render_template(settings.get("name_template"), item, q["format"], q["bitrate"])
        policy = settings.get("dup_policy") or "rename"
        path = target_path(folder, base, ext, policy)
        if path is None:
            with self.lock:
                task.update(state="skipped", error="同名文件已存在（设置为跳过）", speed=0)
            return

        with self.lock:
            task.update(path=path, filename=os.path.basename(path), format=ext,
                        bitrate=str(q.get("bitrate")), quality_label=q["format"])

        tmp = path + ".part"
        got, total = 0, 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": UPSTREAM})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                with self.lock:
                    task["total"] = total
                with open(tmp, "wb") as f:
                    while True:
                        if task.get("cancel"):
                            raise InterruptedError("已取消")
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        with self.lock:
                            task["got"] = got
                            now = time.time()
                            task["_samples"].append((now, got))
                            task["_samples"] = [s for s in task["_samples"] if now - s[0] <= 3]
            os.replace(tmp, path)

            lyric = None
            if settings.get("save_lyric") or settings.get("write_tags"):
                try:
                    lyric = upstream("getLyric", {"platform": item.get("platform", "kuwo"),
                                                  "songid": item.get("id"),
                                                  "time": item.get("time"),
                                                  "sign": item.get("sign")})
                except Exception:
                    lyric = None
            if settings.get("save_lyric") and lyric:
                try:
                    with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as f:
                        f.write(lyric)
                except OSError:
                    pass
            if settings.get("write_tags"):
                cover = fetch_bytes(item.get("cover") or "") if item.get("cover") else None
                write_tags(path, item, lyric, cover)

            with self.lock:
                task.update(state="done", got=got, total=got or total,
                            finished=time.time(), speed=0)
            self.store.apply("download_add", {
                "name": item.get("name", ""),
                "artist": item.get("artist", ""),
                "album": item.get("album_name", ""),
                "path": path,
                "filename": os.path.basename(path),
                "format": ext,
                "bitrate": str(q.get("bitrate") or ""),
                "size": got,
                "platform": item.get("platform", ""),
                "cover": item.get("cover", ""),
                "at": int(time.time()),
            })
        except InterruptedError:
            self._cleanup(tmp)
            with self.lock:
                task.update(state="cancelled", error="已取消", speed=0)
        except Exception as e:  # noqa: BLE001
            self._cleanup(tmp)
            with self.lock:
                task.update(state="error", error=str(e), speed=0)

    @staticmethod
    def _cleanup(tmp):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    def add(self, item, want=None, dir_=None):
        self._ensure_workers()
        tid = uuid.uuid4().hex[:12]
        task = {
            "id": tid, "item": item,
            "name": item.get("name", ""), "artist": item.get("artist", ""),
            "album": item.get("album_name", ""), "cover": item.get("cover", ""),
            "platform": item.get("platform", ""),
            "want_quality": want, "dir": dir_,
            "state": "queued", "got": 0, "total": 0, "speed": 0,
            "error": None, "path": None, "filename": None,
            "format": None, "bitrate": None, "quality_label": None,
            "created": time.time(), "finished": None, "started": None,
            "_samples": [],
        }
        with self.lock:
            self.tasks[tid] = task
            self.order.append(tid)
        self.q.put(tid)
        return tid

    def add_many(self, items, want=None, dir_=None):
        return [self.add(it, want, dir_) for it in items]

    def cancel(self, tid):
        with self.lock:
            task = self.tasks.get(tid)
            if not task:
                return False
            if task["state"] == "queued":
                task.update(state="cancelled", error="已取消")
                return True
            if task["state"] == "running":
                task["cancel"] = True
                return True
            return False

    def retry(self, tid):
        with self.lock:
            task = self.tasks.get(tid)
            if not task or task["state"] not in ("error", "cancelled", "skipped"):
                return False
            task.update(state="queued", error=None, got=0, total=0, speed=0,
                        cancel=False, started=None, finished=None, _samples=[])
        self.q.put(tid)
        return True

    def remove(self, tid):
        with self.lock:
            task = self.tasks.get(tid)
            if task and task["state"] in ("running", "queued"):
                task["cancel"] = True
                return False
            self.tasks.pop(tid, None)
            if tid in self.order:
                self.order.remove(tid)
        return True

    def snapshot(self):
        out = []
        with self.lock:
            for tid in list(self.order):
                t = self.tasks.get(tid)
                if not t:
                    continue
                speed = 0
                if t["state"] == "running" and t["_samples"]:
                    first, last = t["_samples"][0], t["_samples"][-1]
                    dt = last[0] - first[0]
                    if dt > 0.4:
                        speed = (last[1] - first[1]) / dt
                out.append({
                    "id": tid, "name": t["name"], "artist": t["artist"],
                    "album": t["album"], "cover": t["cover"], "platform": t["platform"],
                    "state": t["state"], "got": t["got"], "total": t["total"],
                    "speed": speed if t["state"] == "running" else 0,
                    "error": t["error"], "path": t["path"], "filename": t["filename"],
                    "format": t["format"], "bitrate": t["bitrate"],
                    "created": t["created"], "finished": t["finished"],
                    "eta": int((t["total"] - t["got"]) / speed) if speed > 4096 and t["total"] else None,
                    "age": time.time() - t["created"],
                })
        return out


DM = DownloadManager(STORE)


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "FlacMusic/" + VERSION

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- 音频代理 / 缓存 ---------------------------------------------------
    def _serve_media(self, key, url, ext):
        """命中今天的缓存就直接读盘，否则原样转发上游（含 Range），不落盘。"""
        target = CACHE.find(key)
        if target:
            self._serve_cached(target, ext)
            return
        self._proxy_stream(url, ext)

    def _serve_cached(self, path, ext):
        try:
            size = os.path.getsize(path)
        except OSError:
            self._send(404, {"error": "cache miss"})
            return
        start, end = 0, size - 1
        header = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", header)
        partial = False
        if m and (m.group(1) or m.group(2)):
            partial = True
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:                                   # bytes=-N 取尾部
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */" + str(size))
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", AUDIO_MIME.get((ext or "bin").lower(),
                                                       "application/octet-stream"))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _proxy_stream(self, url, ext):
        """纯转发：把客户端的 Range 原样带给上游，206 就回 206，支持随时拖动。"""
        mime = AUDIO_MIME.get((ext or "bin").lower(), "application/octet-stream")
        headers = {"User-Agent": UA, "Referer": UPSTREAM}
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=45)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                self.send_response(416)
                self.send_header("Content-Range", e.headers.get("Content-Range") or "bytes */*")
                self.end_headers()
                return
            self._send(502, {"error": "上游取流出错: %s" % e})
            return
        except Exception as e:  # noqa: BLE001
            self._send(502, {"error": "上游取流出错: %s" % e})
            return

        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type") or mime)
        for h in ("Content-Length", "Content-Range"):
            if resp.headers.get(h):
                self.send_header(h, resp.headers[h])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _static(self, path):
        rel = (path.lstrip("/") or "index.html").split("?")[0].split("#")[0]
        target = os.path.normpath(os.path.join(resource_dir(), "web", rel))
        webroot = os.path.normpath(os.path.join(resource_dir(), "web"))
        if not target.startswith(webroot) or not os.path.isfile(target):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/store":
            data = STORE.apply("load", None)
            data["_data_path"] = DATA_FILE
            data["_version"] = VERSION
            self._send(200, data)
            return
        if path == "/api/import/status":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, {"ok": True,
                             "data": importer.status((q.get("id") or [""])[0])})
            return
        if path == "/api/providers/health":
            # 按需自检：三个探针各取一个固定公开歌单，失败只标记降级，不影响主流程
            self._send(200, {"ok": True, "data": importer.health()})
            return
        if path == "/api/update/check":
            # notify-only：只比版本号，绝不自动下载 / 替换文件
            self._send(200, {"ok": True, "data": importer.update_check()})
            return
        if path == "/api/store/export":
            # 导出脱敏副本：去掉含用户名的本机绝对路径，
            # 分享给别人时不会泄漏磁盘结构与用户名。
            data = json.loads(json.dumps(STORE.apply("load", None)))
            data.pop("_data_path", None)
            data.pop("_version", None)
            if data.get("settings"):
                data["settings"]["download_dir"] = "<本机音乐目录>"
            self._send(200, {"ok": True, "data": data})
            return
        if path == "/api/downloads":
            self._send(200, {"ok": True, "data": {
                "tasks": DM.snapshot(),
                "concurrency": STORE.data["settings"].get("concurrency", 2)}})
            return
        if path == "/api/media":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
            url = (qs.get("url") or [""])[0]
            ext = (qs.get("ext") or ["bin"])[0]
            if not url:
                sid = (qs.get("sid") or [""])[0]
                if not key or not sid:
                    self._send(400, {"error": "缺少参数"})
                    return
                try:
                    url = resolve_play_url(qs.get("platform", ["kuwo"])[0], sid,
                                           qs.get("fmt", ["mp3"])[0], qs.get("br", ["320"])[0],
                                           (qs.get("time") or [None])[0], (qs.get("sign") or [None])[0],
                                           (qs.get("name") or [None])[0])
                except Exception as e:  # noqa: BLE001
                    self._send(502, {"error": f"解析失败: {e}"})
                    return
            if not key:
                key = MediaCache.key_of(url)
            self._serve_media(key, url, ext)
            return
        if path == "/api/taste":
            self._send(200, {"ok": True, "data": taste_profile()})
            return
        if path == "/api/genres":
            self._send(200, {"ok": True, "data": genre_list()})
            return
        if path == "/api/styles":
            global _STYLE_ROUND
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (q.get("refresh") or [""])[0] == "1":
                _STYLE_ROUND += 1
            try:
                self._send(200, {"ok": True, "data": style_playlists(
                    int((q.get("cards") or [8])[0]),
                    int((q.get("per") or [6])[0]),
                    _STYLE_ROUND)})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/recommend":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fresh = (q.get("refresh") or [""])[0] == "1"
            try:
                self._send(200, {"ok": True, "data": recommend_candidates(
                    int((q.get("limit") or [12])[0]), fresh=fresh)})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/cache":
            st = CACHE.stat()
            day = time.strftime("%Y-%m-%d")
            plays = STORE.data.get("plays") or {}
            heard = 0
            for v in plays.values():
                if isinstance(v, dict) and int((v.get("days") or {}).get(day) or 0) > 0:
                    heard += 1
            cfg = STORE.data["settings"]
            st.update({
                "day": day,
                "heard": heard,
                "min_plays": int(cfg.get("cache_min_plays", 2)),
                "week_plays": int(cfg.get("cache_week_plays", 5)),
                "keep_days": int(cfg.get("cache_keep_days", 3)),
                "limit": int(cfg.get("cache_limit_mb", 2048)),
            })
            self._send(200, {"ok": True, "data": st})
            return
        if path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        self._static(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/rpc":
                body = self._json_body()
                fn = body.get("fn")
                if fn not in RPC_TABLE:
                    self._send(400, {"error": f"未知方法 {fn}"})
                    return
                self._send(200, {"ok": True, "data": RPC_TABLE[fn](body.get("params") or {})})
                return

            if path == "/api/store":
                body = self._json_body()
                self._send(200, {"ok": True, "data": STORE.apply(body.get("op"), body.get("payload"))})
                return

            if path == "/api/import/preview":
                # dry-run：只解析 + 三桶判定，绝不写用户数据
                self._send(200, {"ok": True,
                                 "data": importer.start_preview(self._json_body())})
                return

            if path == "/api/import/cancel":
                tid = str((self._json_body() or {}).get("id") or "")
                self._send(200, {"ok": True, "data": {"cancelled": importer.cancel(tid)}})
                return

            if path == "/api/import/commit":
                body = self._json_body() or {}
                # 只接受本项目能播放的两个音源；QQ / 酷狗的原生 id 绝不能进歌单，
                # 否则会出现一条永远播不了、也换不了源的僵尸曲目。
                items = [x for x in (body.get("items") or [])
                         if isinstance(x, dict) and x.get("platform") in PLATFORMS
                         and str(x.get("id") or "").strip()]
                if not items:
                    self._send(200, {"ok": False, "error": "没有勾选任何可导入的曲目"})
                    return
                target = str(body.get("target_id") or "").strip()
                if target and not any(p.get("id") == target
                                      for p in STORE.data.get("playlists") or []):
                    self._send(200, {"ok": False, "error": "目标歌单不存在"})
                    return
                if not target:
                    name = str(body.get("name") or "").strip()[:60] or "导入歌单"
                    src = body.get("source") if isinstance(body.get("source"), dict) else {}
                    src = dict(src)
                    src["imported_at"] = int(time.time())
                    STORE.apply("playlist_create", {"name": name, "source": src})
                    pls = STORE.data.get("playlists") or []
                    target = pls[-1]["id"] if pls else ""
                if not target:
                    self._send(200, {"ok": False, "error": "歌单创建失败"})
                    return
                pl = next((p for p in STORE.data["playlists"]
                           if p.get("id") == target), None)
                before = len((pl or {}).get("items") or [])
                STORE.apply("playlist_add", {"id": target, "items": items})
                pl = next((p for p in STORE.data["playlists"]
                           if p.get("id") == target), None)
                self._send(200, {"ok": True, "data": {
                    "id": target, "name": (pl or {}).get("name") or "",
                    "added": len((pl or {}).get("items") or []) - before,
                    "total": len((pl or {}).get("items") or [])}})
                return

            if path == "/api/feedback":
                body = self._json_body()
                self._send(200, {"ok": True,
                                 "data": STORE.apply("feedback_set", body)})
                _RECO_CACHE.update(at=0, data=None, pool=None)   # 反馈变了，候选立即作废
                return

            if path == "/api/taste_note":
                self._send(200, {"ok": True, "data": STORE.apply(
                    "taste_note", self._json_body())})
                _RECO_CACHE.update(at=0, data=None, pool=None)
                return
            if path == "/api/taste_genres":
                self._send(200, {"ok": True, "data": STORE.apply(
                    "taste_genres", self._json_body())})
                _RECO_CACHE.update(at=0, data=None, pool=None)
                return

            if path == "/api/probe":
                body = self._json_body()
                items = body.get("items") or []
                limit = int(body.get("limit") or 64)
                try:
                    data = probe_batch(items, limit=limit,
                                       workers=int(body.get("workers") or 8))
                    self._send(200, {"ok": True, "data": data})
                except Exception as e:  # noqa: BLE001
                    self._send(200, {"ok": False, "error": str(e)})
                return

            if path == "/api/play":
                self._send(200, record_play(self._json_body()))
                return

            if path in ("/api/download", "/api/downloads/add"):
                body = self._json_body()
                items = body.get("items")
                if not items:
                    one = body.get("item") or body
                    items = [one] if one.get("id") else []
                if not items:
                    self._send(200, {"ok": False, "error": "缺少歌曲信息"})
                    return
                ids = DM.add_many(items, body.get("quality"), body.get("dir"))
                self._send(200, {"ok": True, "data": {"ids": ids}})
                return

            if path in ("/api/downloads/cancel", "/api/downloads/retry", "/api/downloads/remove"):
                body = self._json_body()
                tid = body.get("id")
                fn = {"cancel": DM.cancel, "retry": DM.retry,
                      "remove": DM.remove}[path.rsplit("/", 1)[1]]
                self._send(200, {"ok": bool(fn(tid))})
                return

            if path == "/api/cache/clear":
                body = {}
                try:
                    body = self._json_body()
                except Exception:
                    pass
                st = CACHE.clear((body or {}).get("tier") or "all")
                self._send(200, {"ok": True, "data": st})
                return

            if path == "/api/save-lyric":
                body = self._json_body()
                folder = STORE.data["settings"].get("download_dir") or default_download_dir()
                base = render_template(STORE.data["settings"].get("name_template"),
                                       {"name": body.get("name"), "artist": body.get("artist")})
                os.makedirs(folder, exist_ok=True)
                path_lrc = os.path.join(folder, base + ".lrc")
                with open(path_lrc, "w", encoding="utf-8") as f:
                    f.write(body.get("content") or "")
                self._send(200, {"ok": True, "data": {"path": path_lrc}})
                return

            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(200, {"ok": False, "error": str(e)})


def port_busy(port):
    """端口是否已经被别的进程占着。

    Windows 上 SO_REUSEADDR 允许同端口重复绑定，不查就会静默起第二个实例：
    两个进程各持一份完整 store，谁后写谁覆盖磁盘，设置会莫名其妙回滚。
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def serve(port=0):
    if port and port_busy(port):
        raise OSError("端口 %d 已被占用（Elysium 可能已经在运行）" % int(port))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


# --------------------------------------------------------------------------
# pywebview 桥
# --------------------------------------------------------------------------
class Bridge:
    def choose_dir(self):
        import webview
        win = webview.windows[0] if webview.windows else None
        if not win:
            return ""
        result = win.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=STORE.data["settings"].get("download_dir") or default_download_dir())
        if not result:
            return ""
        path = result[0] if isinstance(result, (list, tuple)) else result
        STORE.apply("settings", {"download_dir": path})
        return path

    def open_path(self, path):
        try:
            if path and os.path.isdir(path):
                os.startfile(path)  # noqa: S606
                return True
            if path and os.path.isfile(path):
                os.startfile(os.path.dirname(path))  # noqa: S606
                return True
        except Exception:
            return False
        return False

    def reveal(self, path):
        return self.open_path(path)

    # ---- 窗口壳 API（只服务 window-chrome.js）------------------------------
    def win_material_state(self):
        """前端据此决定主题底色是否半透明（L3 系统材质 / L2 CSS 玻璃层）。"""
        return dict(_CHROME_STATE)

    def win_minimize(self):
        try:
            import webview
            if webview.windows:
                webview.windows[0].minimize()
                return True
        except Exception:
            pass
        return False

    def win_toggle_maximize(self):
        try:
            import ctypes
            import webview
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return False
            hwnd = _chrome_hwnd()
            zoomed = bool(hwnd and ctypes.windll.user32.IsZoomed(
                ctypes.c_void_p(hwnd)))
            if zoomed:
                win.restore()
            else:
                win.maximize()
            return not zoomed
        except Exception:
            return False

    def win_is_maximized(self):
        try:
            import ctypes
            hwnd = _chrome_hwnd()
            return bool(hwnd and ctypes.windll.user32.IsZoomed(
                ctypes.c_void_p(hwnd)))
        except Exception:
            return False

    def win_close(self):
        try:
            import webview
            if webview.windows:
                webview.windows[0].destroy()
                return True
        except Exception:
            pass
        return False

    def win_set_theme(self, dark=True):
        """前端切主题时同步 DWM 暗色，并把 Acrylic 重申回来。"""
        hwnd = _chrome_hwnd()
        if not hwnd:
            return False
        _dwm_set(hwnd, 20, 1 if dark else 0)
        if _CHROME_STATE.get("acrylic"):
            _dwm_set(hwnd, 38, 3)
        return True


def startup_bg():
    """窗口首帧底色必须跟用户上次选的主题一致。

    pywebview 的 background_color 只在 WebView2 还没画出第一帧时露出来，
    写死浅色的话，深色主题用户每次启动都会先闪一下白底 —— 打包成 exe 后特别明显。
    跟随系统（auto）时读注册表里的 AppsUseLightTheme。
    """
    theme = (STORE.data.get("settings") or {}).get("theme") or "dark"
    if theme == "auto":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                theme = "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        except OSError:
            theme = "dark"
    return "#fbfbfd" if theme == "light" else "#08080b"


# --------------------------------------------------------------------------
# 窗口壳（无边框 + Win11 系统材质）
#
# 这一段是 Windows 桌面宿主专属，APK / --serve 浏览器调试不会执行。
# 全部都是「能力增强」：拿不到句柄、DWM 不支持、材质被环境变量关闭时
# 一律静默降级到前端 CSS 壳，不影响启动、播放与下载。
# --------------------------------------------------------------------------
_CHROME_STATE = {"platform": "desktop", "acrylic": False, "material": False}


def _chrome_hwnd():
    """原生窗口句柄；拿不到返回 0。winforms 是 Form.Handle.ToInt32()。"""
    try:
        import webview
        win = webview.windows[0] if webview.windows else None
        if win is None or getattr(win, "native", None) is None:
            return 0
        return int(win.native.Handle.ToInt32())
    except Exception:
        return 0


def _dwm_set(hwnd, attr, value):
    """DwmSetWindowAttribute 的防御式封装，返回是否成功（HRESULT==0）。"""
    if not hwnd or os.name != "nt":
        return False
    try:
        import ctypes
        v = ctypes.c_int(int(value))
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(int(attr)),
            ctypes.byref(v), ctypes.sizeof(v))
        return hr == 0
    except Exception:
        return False


def _apply_window_chrome():
    """窗口 ready 后补齐 ZCode 式质感，由 webview.start(func=...) 调用。

    38 = DWMWA_SYSTEMBACKDROP_TYPE, 3 = DWMSBT_TRANSIENTWINDOW (Desktop Acrylic)
    33 = DWMWA_WINDOW_CORNER_PREFERENCE, 2 = DWMWCP_ROUND
    20 = DWMWA_USE_IMMERSIVE_DARK_MODE
     2 = DWMWA_NCRENDERING_POLICY

    pywebview 自己在 update_title_bar_theme() 里会写 38=2(Mica)/1(NONE)，
    所以必须等窗口创建完再覆盖成 Acrylic。系统主题变化事件可能再写回 Mica，
    那依然有材质、界面不受影响；前端每次切主题也会经 win_set_theme 重申。
    """
    hwnd = _chrome_hwnd()
    if not hwnd:
        return
    _CHROME_STATE["acrylic"] = _dwm_set(hwnd, 38, 3)
    _dwm_set(hwnd, 33, 2)
    _dwm_set(hwnd, 20, 0 if startup_bg() == "#fbfbfd" else 1)
    if os.environ.get("ELYSIUM_WINDOW_MATERIAL") == "off":
        _dwm_set(hwnd, 38, 1)          # DWMSBT_NONE：一键回退，便于发布排查
        _CHROME_STATE["acrylic"] = False
    _CHROME_STATE["material"] = bool(_CHROME_STATE["acrylic"])
    if _CHROME_STATE["material"]:
        # transparent=True 时 pywebview 跳过自己的 shadow 分支，这里补上
        # 1px 非客户区扩展，让 DWM 照样画窗口阴影。
        try:
            import ctypes

            class _MARGINS(ctypes.Structure):
                _fields_ = [("cxLeftWidth", ctypes.c_int),
                            ("cxRightWidth", ctypes.c_int),
                            ("cyTopHeight", ctypes.c_int),
                            ("cyBottomHeight", ctypes.c_int)]

            m = _MARGINS(1, 1, 1, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                ctypes.c_void_p(hwnd), ctypes.byref(m))
            _dwm_set(hwnd, 2, 2)
        except Exception:
            pass


def main():
    # 导入模块不 import main（避免循环依赖），严格判定统一复用这里的函数，
    # 保证「歌单导入」与「换源 / 歌手归类」是同一套口径，不会两套标准漂移。
    importer.bind(search_all=search_all, billed_as=_billed_as,
                  fold_text=_fold_text, resource_dir=resource_dir)
    args = sys.argv[1:]
    port_arg = 0
    for a in args:
        if a.startswith("--port="):
            port_arg = int(a.split("=", 1)[1])
    if os.environ.get("FLACMUSIC_PORT"):
        port_arg = int(os.environ["FLACMUSIC_PORT"])
    headless = "--browser" in args or "--serve" in args
    try:
        httpd, port = serve(port_arg)
    except OSError as e:
        print("[%s] 启动失败：%s" % (APP_NAME, e), flush=True)
        raise SystemExit(2)
    try:
        with open(os.path.join(data_dir(), "port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except OSError:
        pass
    url = f"http://127.0.0.1:{port}/"
    if headless:
        print(f"[{APP_NAME}] 本地服务已启动：{url}", flush=True)
        if "--browser" in args:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    import webview
    # 窗口壳：无系统边框 + WebView2 透明背景，让 Win11 Desktop Acrylic 透出来。
    # easy_drag=False 是关键：开启后 pywebview 会给整个窗口加 mousedown，
    # 列表拖拽、进度条、文本选择会被窗口移动吞掉；改由标题栏的
    # .pywebview-drag-region 精确承担拖拽（等价于 ZCode 的 app-region:drag）。
    # ELYSIUM_WINDOW_MATERIAL=off 可一键回到不透明窗口，便于发布排查。
    material_on = os.environ.get("ELYSIUM_WINDOW_MATERIAL") != "off"
    webview.create_window(APP_TITLE, url, width=1240, height=820,
                          min_size=(960, 640), background_color=startup_bg(),
                          js_api=Bridge(), text_select=True,
                          frameless=True, easy_drag=False,
                          transparent=material_on, shadow=True)
    webview.start(_apply_window_chrome, debug="--debug" in args)


if __name__ == "__main__":
    main()


def serve_android(port=18810):
    """Chaquopy 入口：Java 侧在后台线程调用，WebView 加载 http://127.0.0.1:<port>/"""
    os.environ["FLACMUSIC_PORT"] = str(port)
    sys.argv = ["main.py", "--serve"]
    main()
