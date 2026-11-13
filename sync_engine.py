# -*- coding: utf-8 -*-
"""多端同步：把收藏 / 歌单 / 播放进度推到用户自己的 WebDAV（坚果云 / NAS）。

设计约束（与项目一贯的哲学一致）：
  * **数据主权在用户**：不经过任何自建服务，凭据只存本机 data.json，
    打包上行时也只发数据本体，绝不带凭据。
  * **不静默合并**：pull 只做 dry-run diff，逐项声明「远端新增 / 可补条目 /
    同名歌单分歧」，用户勾选后才落盘 —— 与导入歌单同一套判据。
  * **设置类不同步**：音量、主题、下载目录这些换台机器本就该不一样。
  * **本地曲目不同步**：platform=local 的 id 是本机绝对路径，换机没有意义，
    上行前统一剔掉（顺带把磁盘结构挡在包外）。

主程序启动时 bind(store, version) 注入依赖；本模块不 import main，无循环依赖。
"""
import base64
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse

SYNC_FILE = "elysium-sync.json"
FORMAT_VERSION = 1
TIMEOUT = 10                    # 同步不该让界面干等，超时就明说

_LOCK = threading.Lock()
_BOUND = {}
_REMOTE_CACHE = {"at": 0.0, "data": None}   # 给 commit 用，省一次重复 GET
_CACHE_TTL = 300


def bind(store, version=""):
    _BOUND["store"] = store
    _BOUND["version"] = version


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _settings():
    return (_BOUND["store"].data.get("settings") or {})


def _conf():
    wd = _settings().get("webdav")
    wd = wd if isinstance(wd, dict) else {}
    d = str(wd.get("dir") or "Elysium").strip().strip("/") or "Elysium"
    return {
        "url": str(wd.get("url") or "").strip(),
        "user": str(wd.get("user") or "").strip(),
        "pass": str(wd.get("pass") or ""),
        "dir": d,
        "auto": bool(wd.get("auto")),
    }


def _device():
    try:
        name = socket.gethostname() or "Elysium"
    except Exception:                                   # noqa: BLE001
        name = "Elysium"
    return name[:40]


def status():
    c = _conf()
    st = _settings().get("webdav")
    st = st if isinstance(st, dict) else {}
    return {
        "configured": bool(c["url"] and c["user"]),
        "url": c["url"],
        "user": c["user"],
        "has_pass": bool(c["pass"]),
        "dir": c["dir"],
        "auto": c["auto"],
        "device": _device(),
        "file": SYNC_FILE,
        "last_push": int(st.get("last_push") or 0),
        "last_pull": int(st.get("last_pull") or 0),
        "remote": st.get("remote") or None,      # 上次看到的远端包元信息
        "pending": _AUTO["pending"],             # 启动时探到的「远端有新内容」，只是提示
    }


def configure(payload):
    """存凭据。密码传空串时保留原值 —— 前端拿不到明文密码，不能因此被清掉。"""
    payload = payload if isinstance(payload, dict) else {}
    with _BOUND["store"].lock:
        cur = _settings().get("webdav")
        wd = dict(cur) if isinstance(cur, dict) else {}
        if "url" in payload:
            wd["url"] = str(payload.get("url") or "").strip()
        if "user" in payload:
            wd["user"] = str(payload.get("user") or "").strip()
        if "pass" in payload:
            pw = str(payload.get("pass") or "")
            if pw:
                wd["pass"] = pw
        if "dir" in payload:
            wd["dir"] = str(payload.get("dir") or "Elysium").strip().strip("/") or "Elysium"
        if "auto" in payload:
            wd["auto"] = bool(payload.get("auto"))
        _BOUND["store"].data["settings"]["webdav"] = wd
        _BOUND["store"].save()
    _REMOTE_CACHE["data"] = None
    return status()


# --------------------------------------------------------------------------
# WebDAV 客户端（纯标准库，跟着项目一贯的零依赖风格）
# --------------------------------------------------------------------------
class DavError(Exception):
    pass


class WebDAV:
    def __init__(self, url, user, password):
        p = urllib.parse.urlsplit(url if "//" in url else "https://" + url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise DavError("服务器地址不对，应形如 https://dav.jianguoyun.com/dav")
        self.scheme = p.scheme
        self.host = p.hostname
        self.port = p.port
        self.base = (p.path or "").rstrip("/")
        token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
        self.auth = "Basic " + token

    def _connect(self):
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT)

    def call(self, method, rel, body=None, headers=None):
        path = self.base + "/" + rel.strip("/")
        h = {
            "Authorization": self.auth,
            "User-Agent": "Elysium/" + str(_BOUND.get("version") or ""),
            "Accept": "*/*",
        }
        h.update(headers or {})
        conn = None
        try:
            conn = self._connect()
            conn.request(method, urllib.parse.quote(path, safe="/%:@!$&'()*+,;=~"), body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        except ssl.SSLError as e:
            raise DavError("证书校验失败：%s" % e)
        except socket.timeout:
            raise DavError("连接超时（%d 秒），检查地址 / 网络 / 代理" % TIMEOUT)
        except OSError as e:
            raise DavError("连不上服务器：%s" % e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass

    def mkcol(self, rel):
        st, _ = self.call("MKCOL", rel)
        # 201 新建成功；405 已存在；409 父目录不存在
        return st in (201, 405)

    def ensure_dir(self, rel):
        """逐级建目录：/dav/A/B 里的 A、B 谁缺补谁。"""
        parts = [x for x in rel.strip("/").split("/") if x]
        cur = ""
        for part in parts:
            cur = (cur + "/" + part) if cur else part
            if not self.mkcol(cur):
                return False
        return True

    def get(self, rel):
        st, data = self.call("GET", rel)
        if st == 404:
            return None
        if st != 200:
            raise DavError(_status_hint(st, "读取"))
        return data

    def put(self, rel, data):
        st, _ = self.call("PUT", rel, body=data,
                          headers={"Content-Type": "application/json; charset=utf-8"})
        if st not in (200, 201, 204):
            raise DavError(_status_hint(st, "上传"))
        return True


def _status_hint(code, what):
    if code in (401, 403):
        return "%s失败：账号或应用密码不对（%d）" % (what, code)
    if code == 404:
        return "%s失败：远端路径不存在（404）" % what
    if code == 507:
        return "%s失败：远端空间不足（507）" % what
    if code >= 500:
        return "%s失败：服务端错误（%d）" % (what, code)
    return "%s失败：HTTP %d" % (what, code)


def _client():
    c = _conf()
    if not (c["url"] and c["user"] and c["pass"]):
        raise DavError("还没配置同步：填好服务器地址、账号与应用密码")
    return WebDAV(c["url"], c["user"], c["pass"]), c


def _remote_path(c):
    return "%s/%s" % (c["dir"], SYNC_FILE)


# --------------------------------------------------------------------------
# 打包 / 清洗
# --------------------------------------------------------------------------
FAV_FIELDS = ("kind", "id", "platform", "name", "artist", "album_name",
              "album_id", "artist_id", "cover", "duration", "added")
PL_FIELDS = ("id", "name", "created_at", "updated_at")
ITEM_FIELDS = ("id", "platform", "name", "artist", "album_name", "album_id",
               "artist_id", "cover", "duration", "time", "sign")


def _is_local(x):
    return isinstance(x, dict) and x.get("platform") == "local"


def _wash(src, fields):
    if not isinstance(src, dict):
        return {}
    out = {k: src[k] for k in fields if src.get(k) not in (None, "")}
    if src.get("minfo"):
        out["minfo"] = [{k: m[k] for k in ("format", "bitrate", "size") if m.get(k) not in (None, "")}
                        for m in src["minfo"] if isinstance(m, dict)]
    return out


def _clean_fav(f):
    if not isinstance(f, dict) or _is_local(f):
        return None
    out = _wash(f, FAV_FIELDS)
    out["kind"] = out.get("kind") or "song"
    return out if out.get("id") else None


def _clean_playlist(p):
    if not isinstance(p, dict):
        return None
    items = [x for x in (_clean_item(i) for i in (p.get("items") or [])) if x] \
        if isinstance(p.get("items"), list) else []
    out = _wash(p, PL_FIELDS)
    out["items"] = items
    return out if out.get("id") and out.get("name") else None


def _clean_item(i):
    if not isinstance(i, dict) or _is_local(i) or not i.get("id"):
        return None
    return _wash(i, ITEM_FIELDS)


def _int_or(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_last_track(lt):
    if not isinstance(lt, dict) or not isinstance(lt.get("item"), dict) \
            or _is_local(lt.get("item")):
        return None
    item = _clean_item(lt.get("item"))
    if not item:
        return None
    return {
        "item": item,
        "quality": lt.get("quality") if isinstance(lt.get("quality"), dict) else None,
        "position": max(0, _int_or(lt.get("position"))),
        "duration": max(0, _int_or(lt.get("duration"))),
        "at": max(0, _int_or(lt.get("at"))),
    }


def pack():
    """上行包：只带要同步的三类数据，凭据与本机路径一律不进。"""
    d = _BOUND["store"].data
    favs = [x for x in (_clean_fav(f) for f in (d.get("favorites") or [])) if x]
    pls = [x for x in (_clean_playlist(p) for p in (d.get("playlists") or [])) if x]
    lt = _clean_last_track((d.get("settings") or {}).get("last_track"))
    return {
        "v": FORMAT_VERSION,
        "app": "Elysium",
        "client": str(_BOUND.get("version") or ""),
        "device": _device(),
        "pushed_at": int(time.time()),
        "favorites": favs,
        "playlists": pls,
        "last_track": lt,
    }


# --------------------------------------------------------------------------
# diff（dry-run，一个字节都不写）
# --------------------------------------------------------------------------
def _fav_key(f):
    if not isinstance(f, dict):
        return ("", "", None)
    return ((f.get("kind") or "song"), str(f.get("id")), f.get("platform"))


def _item_key(i):
    if not isinstance(i, dict):
        return ("", "")
    return (str(i.get("platform") or ""), str(i.get("id") or ""))


def _dicts(seq):
    """只留对象项：远端包被同步盘截断 / 手改后可能混进 null 和字符串。"""
    return [x for x in (seq or []) if isinstance(x, dict)] if isinstance(seq, list) else []


def _same_content(a, b):
    ka = sorted(_item_key(x) for x in _dicts(a.get("items")))
    kb = sorted(_item_key(x) for x in _dicts(b.get("items")))
    return ka == kb


def diff(remote):
    """把远端包和本机数据比一比，只描述差异，不改任何本地数据。"""
    if not isinstance(remote, dict):
        raise DavError("远端的 elysium-sync.json 不是有效对象（可能被同步盘截断）")
    store = _BOUND["store"]
    loc = store.data
    loc_favs = _dicts(loc.get("favorites"))
    loc_pls = _dicts(loc.get("playlists"))
    loc_lt = (loc.get("settings") or {}).get("last_track")

    # ---- 收藏：只认「远端多了什么」，本地的一律不动（不静默删）
    have = {_fav_key(f) for f in loc_favs}
    fav_add = [f for f in _dicts(remote.get("favorites"))
               if _fav_key(f) not in have]

    # ---- 歌单：按 id 对齐，同名不同 id 单独列冲突
    by_id = {p.get("id"): p for p in loc_pls}
    name_to_local = {}
    for p in loc_pls:
        name_to_local.setdefault(str(p.get("name") or ""), []).append(p)

    pl_new, pl_fill, pl_conflict = [], [], []
    for rp in _dicts(remote.get("playlists")):
        rid = rp.get("id")
        local = by_id.get(rid)
        if local is not None:
            have_items = {_item_key(x) for x in _dicts(local.get("items"))}
            extra = [x for x in _dicts(rp.get("items")) if _item_key(x) not in have_items]
            if extra:
                pl_fill.append({"id": rid, "name": rp.get("name"),
                                "add": len(extra), "total": len(_dicts(rp.get("items"))),
                                "items": extra})
            continue
        same_name = name_to_local.get(str(rp.get("name") or "")) or []
        twin = next((x for x in same_name if _same_content(x, rp)), None)
        if twin is not None:
            continue                    # 同内容不同 id：也算过了，不用打扰用户
        if same_name:
            pl_conflict.append({
                "name": rp.get("name"),
                "remote": {"id": rid, "count": len(_dicts(rp.get("items"))),
                           "items": _dicts(rp.get("items"))},
                "local": {"id": same_name[0].get("id"),
                          "count": len(_dicts(same_name[0].get("items")))},
            })
            continue
        pl_new.append({"id": rid, "name": rp.get("name"),
                       "count": len(_dicts(rp.get("items"))), "items": _dicts(rp.get("items"))})

    # ---- 进度：at 大者胜；只是告知，用户勾了才切
    lt_take = None
    rlt = remote.get("last_track")
    if isinstance(rlt, dict) and rlt.get("item"):
        rat = int(rlt.get("at") or 0)
        lat = int((loc_lt or {}).get("at") or 0)
        same_track = (loc_lt and _item_key(loc_lt.get("item") or {})
                      == _item_key(rlt.get("item") or {}))
        if rat > lat:
            lt_take = {"item": rlt.get("item"), "position": rlt.get("position"),
                       "duration": rlt.get("duration"), "at": rat,
                       "move": abs(int(rlt.get("position") or 0) - int((loc_lt or {}).get("position") or 0))
                       if same_track else None}
        elif same_track and int((loc_lt or {}).get("position") or 0) > int(rlt.get("position") or 0) + 30:
            lt_take = None              # 本地听得更靠后，不往回退

    return {
        "remote": {"device": remote.get("device"), "client": remote.get("client"),
                   "pushed_at": int(remote.get("pushed_at") or 0),
                   "counts": {"favorites": len(remote.get("favorites") or []),
                              "playlists": len(remote.get("playlists") or [])}},
        "favorites": fav_add,
        "playlists": {"new": pl_new, "fill": pl_fill, "conflicts": pl_conflict},
        "last_track": lt_take,
        "empty": not (fav_add or pl_new or pl_fill or pl_conflict or lt_take),
    }


# --------------------------------------------------------------------------
# 对外动作
# --------------------------------------------------------------------------
def test():
    """连一下、建好目录、看远端有没有包。不改本地数据。"""
    with _LOCK:
        dav, c = _client()
        if not dav.ensure_dir(c["dir"]):
            raise DavError("建不了远端目录「%s」，检查应用密码是否有写权限" % c["dir"])
        raw = dav.get(_remote_path(c))
        if raw is None:
            return {"ok": True, "exists": False, "dir": c["dir"]}
        try:
            remote = json.loads(raw.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            raise DavError("远端文件不是本应用的同步包（解析失败）")
        return {"ok": True, "exists": True, "dir": c["dir"],
                "remote": {"device": remote.get("device"),
                           "client": remote.get("client"),
                           "pushed_at": int(remote.get("pushed_at") or 0),
                           "counts": {"favorites": len(remote.get("favorites") or []),
                                      "playlists": len(remote.get("playlists") or [])}}}


def push():
    with _LOCK:
        dav, c = _client()
        if not dav.ensure_dir(c["dir"]):
            raise DavError("建不了远端目录「%s」" % c["dir"])
        data = pack()
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        dav.put(_remote_path(c), body)
        now = int(time.time())
        with _BOUND["store"].lock:
            cur_wd = _settings().get("webdav")
            wd = dict(cur_wd) if isinstance(cur_wd, dict) else {}
            wd["last_push"] = now
            wd["remote"] = {"device": data["device"], "pushed_at": now,
                            "counts": {"favorites": len(data["favorites"]),
                                       "playlists": len(data["playlists"])}}
            _BOUND["store"].data["settings"]["webdav"] = wd
            _BOUND["store"].save()
        return {"pushed_at": now, "favorites": len(data["favorites"]),
                "playlists": len(data["playlists"]), "bytes": len(body),
                "device": data["device"]}


def pull():
    """只读：取远端包算 diff，本地零写入。"""
    with _LOCK:
        dav, c = _client()
        raw = dav.get(_remote_path(c))
        if raw is None:
            raise DavError("远端还没有同步包，先在这台机器上「上传」一次")
        try:
            remote = json.loads(raw.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            raise DavError("远端文件不是本应用的同步包（解析失败）")
        if int(remote.get("v") or 0) > FORMAT_VERSION:
            raise DavError("远端包格式版本比本机新（v%s），先升级应用" % remote.get("v"))
        _REMOTE_CACHE["data"] = remote
        _REMOTE_CACHE["at"] = time.time()
        _REMOTE_CACHE["path"] = _remote_path(c)
        out = diff(remote)
        with _BOUND["store"].lock:
            cur_wd = _settings().get("webdav")
            wd = dict(cur_wd) if isinstance(cur_wd, dict) else {}
            wd["last_pull"] = int(time.time())
            _BOUND["store"].data["settings"]["webdav"] = wd
            _BOUND["store"].save()
        return out


def commit(payload):
    """把用户勾上的部分落盘。没勾的一律不动 —— 与导入歌单同一套规矩。"""
    payload = payload if isinstance(payload, dict) else {}
    remote = _REMOTE_CACHE.get("data")
    if not remote:
        raise DavError("预览已过期，重新拉取一次")
    # 远端包可能被同步盘截断 / 被手改：落盘前必须走与打包同一套清洗，
    # 否则一个数字或字符串会作为「收藏」进 data.json，之后 pack()/taste 全崩。
    raw_favs = remote.get("favorites")
    raw_pls = remote.get("playlists")
    rfavs = [x for x in (_clean_fav(f) for f in (raw_favs if isinstance(raw_favs, list) else [])) if x]
    rpls = [x for x in (_clean_playlist(p) for p in (raw_pls if isinstance(raw_pls, list) else [])) if x]
    changes = {"favorites": 0, "playlists_new": 0, "playlist_items": 0,
               "conflicts": 0, "last_track": False}

    with _BOUND["store"].lock:
        data = _BOUND["store"].data

        # 收藏：并集，按 key 去重
        if payload.get("favorites"):
            loc = [f for f in (data.get("favorites") or []) if isinstance(f, dict)]
            have = {_fav_key(f) for f in loc}
            add = [f for f in rfavs if _fav_key(f) not in have]
            if add:
                data["favorites"] = add + loc
                changes["favorites"] = len(add)

        # 歌单
        raw_new = payload.get("playlist_new")
        raw_fill = payload.get("playlist_fill")
        want_new = {x for x in (raw_new if isinstance(raw_new, list) else []) if isinstance(x, str)}
        want_fill = {x for x in (raw_fill if isinstance(raw_fill, list) else []) if isinstance(x, str)}
        picks = payload.get("conflicts") if isinstance(payload.get("conflicts"), dict) else {}
        pls = [p for p in (data.get("playlists") or []) if isinstance(p, dict)]
        data["playlists"] = pls
        by_id = {p.get("id"): p for p in pls}

        for rp in rpls:
            rid = rp.get("id")
            if rid in want_new and rid not in by_id:
                now = int(time.time())
                pls.append({
                    "id": rid,
                    "name": str(rp.get("name") or "未命名")[:60],
                    "created_at": _int_or(rp.get("created_at"), now),
                    "updated_at": _int_or(rp.get("updated_at"), now),
                    "items": [i for i in (rp.get("items") or []) if not _is_local(i)],
                    "source": {"mode": "sync"},
                })
                changes["playlists_new"] += 1
            elif rid in want_fill and rid in by_id:
                target = by_id[rid]
                have = {_item_key(x) for x in (target.get("items") or [])}
                add = [x for x in (rp.get("items") or [])
                       if _item_key(x) not in have and not _is_local(x)]
                if add:
                    target["items"] = list(target.get("items") or []) + add
                    target["updated_at"] = int(time.time())
                    changes["playlist_items"] += len(add)

        # 同名不同 id：三选一（local / remote / both）
        for rp in rpls:
            name = str(rp.get("name") or "")
            choice = picks.get(name)
            if choice not in ("remote", "both"):
                continue
            local = next((p for p in pls if str(p.get("name") or "") == name
                          and p.get("id") != rp.get("id")), None)
            items = [i for i in (rp.get("items") or []) if not _is_local(i)]
            if choice == "remote" and local is not None:
                local["items"] = items
                local["updated_at"] = int(time.time())
                changes["conflicts"] += 1
            elif choice == "both":
                now = int(time.time())
                pls.append({
                    "id": rp.get("id") or ("pl_%d_%s" % (now, os.urandom(3).hex())),
                    "name": (name + "（远端）")[:60],
                    "created_at": _int_or(rp.get("created_at"), now),
                    "updated_at": now,
                    "items": items,
                    "source": {"mode": "sync"},
                })
                changes["conflicts"] += 1

        # 进度：只在用户勾了、且远端确实更新时切
        if payload.get("last_track") and isinstance(remote.get("last_track"), dict):
            rlt = _clean_last_track(remote.get("last_track"))
            cur = (data.get("settings") or {}).get("last_track")
            cur = cur if isinstance(cur, dict) else {}
            if rlt and _int_or(rlt.get("at")) >= _int_or(cur.get("at")):
                data["settings"]["last_track"] = rlt
                changes["last_track"] = True

        _BOUND["store"].save()

    _REMOTE_CACHE["data"] = None       # 只允许提交一次，避免重复并集
    return changes


def auto_push_guard():
    """自动模式的最小间隔：5 分钟内不重复推，避免频繁改动打爆远端。"""
    st = _settings().get("webdav")
    st = st if isinstance(st, dict) else {}
    if not st.get("auto"):
        return False
    return (time.time() - int(st.get("last_push") or 0)) >= 300


# --------------------------------------------------------------------------
# 自动模式（默认关）
# 只自动**上传**；拉取永远只算 diff 并亮提示，绝不自动改本地数据 ——
# 「不静默合并」这条底线不能因为自动模式破了。
# --------------------------------------------------------------------------
_AUTO = {"dirty": False, "booted": False, "pending": None}


def touch():
    """本地数据有改动：留给自动模式判断要不要上传。"""
    _AUTO["dirty"] = True


def pending():
    return _AUTO["pending"]


def start_auto():
    threading.Thread(target=_auto_loop, name="sync-auto", daemon=True).start()


def _auto_loop():
    time.sleep(25)                      # 避开启动期的取数与更新检查
    while True:
        try:
            c = _conf()
            if c["auto"] and c["url"] and c["user"] and c["pass"]:
                if not _AUTO["booted"]:
                    _AUTO["booted"] = True
                    _boot_peek()
                if _AUTO["dirty"] and auto_push_guard():
                    _AUTO["dirty"] = False
                    push()
        except Exception:               # noqa: BLE001
            pass                        # 自动模式出错不打扰人，手动操作会给明确报错
        time.sleep(45)


def _boot_peek():
    """启动后看一眼远端有没有新东西 —— 只记进 pending，不落盘。"""
    try:
        d = pull()
        if d.get("empty"):
            _AUTO["pending"] = None
            return
        pl = d.get("playlists") or {}
        _AUTO["pending"] = {
            "favorites": len(d.get("favorites") or []),
            "playlists": len(pl.get("new") or []) + len(pl.get("fill") or []),
            "conflicts": len(pl.get("conflicts") or []),
            "last_track": bool(d.get("last_track")),
            "at": int(time.time()),
        }
    except Exception:                   # noqa: BLE001
        pass
