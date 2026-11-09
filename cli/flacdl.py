#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flac.music.hi.cn 逆向得到的接口客户端

接口（同源，POST，application/x-www-form-urlencoded）：
  ajax.php?act=search   platform, keyword, page, size
  ajax.php?act=song     platform, id
  ajax.php?act=album    platform, id
  ajax.php?act=playlist platform, id, page, size
  ajax.php?act=getUrl   platform, songid, format, bitrate, time, sign
  ajax.php?act=getLyric platform, songid, time, sign

sign/time 由搜索/解析接口随每条歌曲一起返回，绑定 (songid, time)，服务端校验，
无法离线伪造（内部有密钥）。所以流程固定为：先拿到条目 -> 再用它的 time/sign 换直链。

仅用于个人学习与技术研究，音乐版权归原作者所有。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://flac.music.hi.cn/"
API = BASE + "ajax.php?act="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PLATFORMS = {"kuwo": "酷我", "wyy": "网易云"}
QUALITY = [("flac", "2000"), ("flac", "646"), ("mp3", "320"), ("mp3", "128")]


def _post(act, **params):
    body = urllib.parse.urlencode({k: "" if v is None else v for k, v in params.items()}).encode()
    req = urllib.request.Request(API + act, data=body, method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": BASE,
        "Origin": BASE.rstrip("/"),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    if payload.get("code") != 0:
        raise RuntimeError(f"{act} 失败: {payload.get('msg')} (code={payload.get('code')})")
    return payload.get("data")


def parse_link(text):
    """把酷我 / 网易云链接解析成 (platform, type, id)。"""
    s = text.strip()
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    try:
        u = urllib.parse.urlparse(s)
    except ValueError:
        return None
    host = (u.netloc or "").lower()
    qs = urllib.parse.parse_qs(u.query)
    path = urllib.parse.unquote(u.path or "")
    frag = (u.fragment or "").lstrip("#")

    if host.endswith("kuwo.cn"):
        if qs.get("albumid") and qs["albumid"][0].isdigit():
            return "kuwo", "album", qs["albumid"][0]
        for t, pat in (("album", r"/album_detail/(\d+)"), ("playlist", r"/playlist_detail/(\d+)"),
                       ("song", r"/play_detail/(\d+)"), ("song", r"/yinyue/(\d+)")):
            m = re.search(pat, path)
            if m:
                return "kuwo", t, m.group(1)
        return None

    if host == "music.163.com" or host.endswith(".music.163.com"):
        # 网易云是 hash 路由：id 在 #/xxx?id=... 里，要连 # 后的 query 一起解析
        target, tqs = path, qs
        if frag:
            if "?" in frag:
                fpath, fquery = frag.split("?", 1)
                tqs = urllib.parse.parse_qs(fquery)
                target = fpath
            else:
                target = frag
        for t, pat in (("album", r"(?:^|/)(?:m/)?album(?:/(\d+))?$"),
                       ("playlist", r"(?:^|/)(?:m/)?playlist(?:/(\d+))?$"),
                       ("song", r"(?:^|/)(?:m/)?song(?:/(\d+))?$")):
            m = re.search(pat, target.rstrip("/"))
            if m:
                sid = m.group(1) or (tqs.get("id") or qs.get("id") or [None])[0]
                if sid and str(sid).isdigit():
                    return "wyy", t, str(sid)
        return None
    return None


def search(keyword, platform="kuwo", page=1, size=20):
    return _post("search", platform=platform, keyword=keyword, page=page, size=size)


def fetch_page(kind, platform, id_, page=1, size=200):
    if kind == "album":
        return _post("album", platform=platform, id=id_)
    if kind == "playlist":
        return _post("playlist", platform=platform, id=id_, page=page, size=size)
    return _post("song", platform=platform, id=id_)


def get_url(item, platform, fmt, bitrate):
    data = _post("getUrl", platform=platform, songid=item["id"], format=fmt,
                 bitrate=bitrate, time=item["time"], sign=item["sign"])
    return data["url"]


def get_lyric(item, platform):
    return _post("getLyric", platform=platform, songid=item["id"],
                 time=item["time"], sign=item["sign"])


def pick(item, want):
    """挑音质。flac 码率各平台不统一（酷我 2000，网易云 646/943/1485…），不能写死。"""
    minfo = item.get("minfo") or []
    if not minfo:
        return None

    def ibr(m):
        try:
            return int(m.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    flacs = sorted((m for m in minfo if m.get("format") == "flac"), key=ibr, reverse=True)
    mp3s = sorted((m for m in minfo if m.get("format") == "mp3"), key=ibr, reverse=True)

    if want in ("best", "flac"):
        return flacs[0] if flacs else (mp3s[0] if mp3s else None)
    if want in ("2000", "646"):          # 指定 FLAC 码率：等值优先，否则取最高
        return next((m for m in flacs if ibr(m) == int(want)), flacs[0] if flacs else None)
    if want in ("320", "128"):
        return (next((m for m in mp3s if ibr(m) == int(want)), None)
                or next((m for m in mp3s if ibr(m) <= int(want)), None)
                or (mp3s[-1] if mp3s else None))
    return None


def sniff_ext(url, fallback="bin"):
    """真实封装经常和接口标的 format 不一致（128k 常给 m4a/aac），靠 magic 判定后缀。"""
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
        brand = head[8:12]
        return "m4a" if brand in (b"M4A ", b"mp42", b"isom") else "aac"
    if head[:3] == b"Ogg":
        return "ogg"
    return fallback


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(url, dest, name=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": BASE})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        os.makedirs(dest, exist_ok=True)
        cd = r.headers.get("Content-Disposition") or ""
        filename = name or re.findall(r"filename=\"?([^\";]+)", cd)
        filename = filename[0] if isinstance(filename, list) else (filename or f"audio_{int(time.time())}")
        path = os.path.join(dest, filename)
        got, last = 0, 0.0
        with open(path, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and time.time() - last > 0.3:
                    pct = got * 100 / total
                    sys.stdout.write(f"\r  {pct:5.1f}%  {human(got)}/{human(total)}")
                    sys.stdout.flush()
                    last = time.time()
        sys.stdout.write(f"\r  100.0%  {human(got)}" + " " * 20 + "\n")
    return path, got


def safe(s):
    return re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip() or "unknown"


def cmd_search(a):
    hit = parse_link(a.keyword)
    if hit:
        platform, kind, id_ = hit
        data = fetch_page(kind, platform, id_)
        return show(data, platform, kind)
    data = search(a.keyword, a.platform, a.page, a.size)
    print(f"[{PLATFORMS.get(a.platform, a.platform)}] “{a.keyword}” 共 {data.get('total')} 条")
    return show(data, a.platform, "search")


def show(data, platform, kind):
    items = data.get("list") or []
    info = data.get("info")
    if info:
        print(f"专辑/歌单：{info.get('name')} — {info.get('artist', '')}")
    for i, it in enumerate(items, 1):
        brs = "/".join(f"{m['format']}{m['bitrate']}" for m in (it.get("minfo") or []))
        dur = it.get("duration") or 0
        print(f"{i:>3}. {it.get('name')} — {it.get('artist')}  [{brs}]  "
              f"{int(dur)//60}:{int(dur)%60:02d}  id={it.get('id')}")
    if not items:
        print("（无结果）")
    return items


def cmd_download(a):
    hit = parse_link(a.target)
    if hit:
        platform, kind, id_ = hit
        data = fetch_page(kind, platform, id_)
        items = (data.get("list") or [])
        if kind == "song":
            items = items[:1]
    else:
        platform = a.platform
        items = search(a.target, platform, 1, max(a.pick, 20)).get("list") or []
        if a.pick:
            items = [items[a.pick - 1]] if len(items) >= a.pick else []
    if not items:
        print("没有拿到歌曲条目")
        return 1

    for it in items:
        q = pick(it, a.quality)
        if not q:
            print(f"跳过 {it.get('name')}：没有 {a.quality} 音质")
            continue
        name = safe(f"{it.get('name')} - {it.get('artist')}")
        print(f"→ {it.get('name')} — {it.get('artist')}  [{q['format']} {q['bitrate']}k]")
        try:
            url = get_url(it, platform, q["format"], str(q["bitrate"]))
        except Exception as e:
            print(f"  解析失败：{e}")
            continue
        ext = sniff_ext(url, q["format"])
        if ext != q["format"]:
            print(f"  注意：接口标 {q['format']}，实际封装是 {ext}")
        name = f"{name}.{ext}"
        print(f"  直链：{url.split('?')[0][:100]}...")
        try:
            path, size = download(url, a.out, name)
            print(f"  已保存：{path}  ({human(size)})")
        except Exception as e:
            print(f"  下载失败：{e}")
            continue
        if a.lyric:
            try:
                lrc = get_lyric(it, platform)
                lp = os.path.splitext(path)[0] + ".lrc"
                with open(lp, "w", encoding="utf-8") as f:
                    f.write(lrc)
                print(f"  歌词：{lp}")
            except Exception as e:
                print(f"  歌词失败：{e}")
        if a.pick:  # 精准指定单曲时只下这一首
            break
    return 0


def main():
    ap = argparse.ArgumentParser(description="flac.music.hi.cn 接口客户端（学习/研究用途）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="搜索关键词，或直接贴酷我/网易云链接")
    s.add_argument("keyword")
    s.add_argument("-p", "--platform", default="kuwo", choices=list(PLATFORMS))
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--size", type=int, default=20)
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("download", help="下载：关键词 / 链接 / 搜索结果序号")
    d.add_argument("target")
    d.add_argument("-p", "--platform", default="kuwo", choices=list(PLATFORMS))
    d.add_argument("-q", "--quality", default="best",
                   choices=["best", "flac", "2000", "646", "320", "128"])
    d.add_argument("-n", "--pick", type=int, default=0, help="只下搜索结果第 N 条")
    d.add_argument("-o", "--out", default="downloads")
    d.add_argument("--lyric", action="store_true", help="同时保存 .lrc 歌词")
    d.set_defaults(func=cmd_download)

    a = ap.parse_args()
    if a.cmd == "search":
        cmd_search(a)
        return 0
    return cmd_download(a)


if __name__ == "__main__":
    sys.exit(main())
