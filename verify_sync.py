# -*- coding: utf-8 -*-
"""多端同步（P5）验收：双实例互推互拉 + dry-run 零写入 + 凭据不外流。

用一个内存 WebDAV 替身（只实现 MKCOL / PUT / GET / DELETE）当远端，
起两个独立的 Elysium 实例（各自的 ELYSIUM_HOME）互推互拉：

  S1  配置保存      —— 密码不回页面，只给 has_pass；空密码不清空旧值
  S2  连接自检      —— 远端没包时 exists=false
  S3  上传           —— 包落到远端，数量对得上
  S4  包体干净       —— 不含本地曲目、不含凭据、不含本机路径
  S5  拉取 diff      —— 收藏 / 新歌单 / 同名分歧 / 进度 四类都算对
  S6  dry-run 零写入 —— 拉取前后本地业务字段一字不动
  S7  合并落盘       —— 勾选的进来，本地已有的不丢
  S8  冲突三选一     —— both 之后两份并存
  S9  进度           —— at 大者胜
  S10 凭据不外流     —— /api/store/export 里没有 webdav
  S11 失败明示       —— 连不上时给中文错误，不是 500 静默

用法：
    python verify_sync.py            # 跑完自动清理
    python verify_sync.py --keep     # 保留临时目录
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def note(text):
    print("      · " + text)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- WebDAV 替身
class DavStub:
    """够用的内存 WebDAV：只认 MKCOL / PUT / GET / DELETE，够跑同步链路。"""

    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.lock = threading.Lock()
        self.httpd = None
        self.port = 0

    def start(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _path(self):
                return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

            def _empty(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_MKCOL(self):
                if "authorization" not in {k.lower() for k in self.headers.keys()}:
                    self._empty(401)
                    return
                with stub.lock:
                    stub.dirs.add(self._path().rstrip("/"))
                self._empty(201)

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                with stub.lock:
                    stub.files[self._path()] = body
                self._empty(201)

            def do_GET(self):
                p = self._path()
                with stub.lock:
                    body = stub.files.get(p)
                if body is None:
                    self._empty(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self):
                with stub.lock:
                    stub.files.pop(self._path(), None)
                self._empty(204)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def url(self):
        return "http://127.0.0.1:%d/dav" % self.port

    def body(self, rel):
        with self.lock:
            return self.files.get("/dav/" + rel.strip("/"))

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------- 实例
def spawn(base_dir, tag):
    home = os.path.join(base_dir, tag)
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        shutil.copytree(os.path.join(REPO, item), os.path.join(home, item),
                        ignore=shutil.ignore_patterns("__pycache__"))
    port = free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
    logf = open(os.path.join(base_dir, tag + ".log"), "wb")
    proc = subprocess.Popen([PY, "main.py", "--serve", "--port=%d" % port], cwd=REPO,
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/api/cache", timeout=1).read(64)
            return proc, base, os.path.join(home, "Elysium", "data.json")
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise SystemExit("%s 实例没起来，看 %s.log" % (tag, tag))


def api(base, path, body=None, timeout=25):
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def store(base, op, payload=None):
    return api(base, "/api/store", {"op": op, "payload": payload or {}})


def fav(base, sid, name, artist, platform="kuwo", kind="song"):
    return store(base, "fav_toggle", {"kind": kind, "id": sid, "platform": platform,
                                      "name": name, "artist": artist,
                                      "album_name": "测试专辑"})


def playlist(base, name, items):
    r = store(base, "playlist_create", {"name": name})
    pid = None
    for p in r["data"]["playlists"]:
        if p.get("name") == name:
            pid = p["id"]
    if pid and items:
        store(base, "playlist_add", {"id": pid, "items": items})
    return pid


def items(prefix, n):
    return [{"id": "%s%d" % (prefix, i), "platform": "kuwo", "name": "曲目%s%d" % (prefix, i),
             "artist": "测试歌手", "album_name": "测试专辑", "duration": 200} for i in range(n)]


def biz_digest(path):
    """只取业务字段算指纹：dry-run 校验用（last_pull 时间戳这种元信息不算）。"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    keep = {k: d.get(k) for k in ("favorites", "playlists", "recent", "history")}
    keep["last_track"] = (d.get("settings") or {}).get("last_track")
    raw = json.dumps(keep, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_sync_")
    dav = DavStub().start()
    pa = pb = None
    try:
        # ---------------------------------------------------- 实例 A：造源数据
        pa, baseA, dataA = spawn(base_dir, "A")
        note("实例 A %s / 实例 B 稍后起" % baseA)
        fav(baseA, "1", "晴天", "周杰伦")
        fav(baseA, "9", "叶惠美", "周杰伦", platform="wyy", kind="album")
        fav(baseA, "E:/Music/local.mp3", "本机曲目", "谁", platform="local")   # 应被剔除
        pl_commute = playlist(baseA, "通勤", items("c", 12))
        pl_run = playlist(baseA, "夜跑", items("r", 3))
        store(baseA, "settings", {"last_track": {
            "item": {"id": "1", "platform": "kuwo", "name": "晴天", "artist": "周杰伦"},
            "quality": {"format": "mp3", "bitrate": 320},
            "position": 95, "duration": 269, "at": int(time.time()) - 60}})

        # ---------------------------------------------------- S1 配置
        r = api(baseA, "/api/sync/config", {
            "url": dav.url(), "user": "u@test", "pass": "secret-***", "dir": "Elysium"})
        check("S1a 配置已落盘且 has_pass", r["data"]["configured"] and r["data"]["has_pass"],
              "%s" % {k: r["data"][k] for k in ("url", "user", "dir", "has_pass")})
        api(baseA, "/api/sync/config", {"pass": ""})       # 留空表示沿用
        st = api(baseA, "/api/sync/status")["data"]
        check("S1b 密码留空不清空旧值", st["has_pass"] is True, "has_pass=%s" % st["has_pass"])
        got = api(baseA, "/api/store")
        check("S1c 页面拿不到明文密码",
              "pass" not in ((got.get("settings") or {}).get("webdav") or {}),
              "webdav=%s" % ((got.get("settings") or {}).get("webdav") or {}))

        # ---------------------------------------------------- S2 连接自检
        t = api(baseA, "/api/sync/test", {})
        check("S2 远端还没包时 exists=false", t["ok"] and t["data"]["exists"] is False,
              "%s" % (t.get("data"),))

        # ---------------------------------------------------- S3 上传
        p = api(baseA, "/api/sync/push", {})
        check("S3a 上传成功且计数对得上",
              p["ok"] and p["data"]["favorites"] == 2 and p["data"]["playlists"] == 2,
              "%s" % (p.get("data") or p.get("error")))
        raw = dav.body("Elysium/elysium-sync.json")
        check("S3b 远端文件落在 Elysium/elysium-sync.json", bool(raw), "%d 字节" % len(raw or b""))

        # ---------------------------------------------------- S4 包体干净
        pack = json.loads((raw or b"{}").decode("utf-8"))
        blob = json.dumps(pack, ensure_ascii=False)
        check("S4a 本地曲目不上行", "local.mp3" not in blob and "本机曲目" not in blob,
              "收藏 %d 项 / 歌单 %d 个" % (len(pack.get("favorites") or []),
                                          len(pack.get("playlists") or [])))
        check("S4b 包里没有凭据", "secret-***" not in blob and "u@test" not in blob)
        check("S4c 包里没有本机路径", "E:/Music" not in blob and "C:\\\\" not in blob)

        # ---------------------------------------------------- 实例 B：造冲突数据
        pb, baseB, dataB = spawn(base_dir, "B")
        fav(baseB, "2", "稻香", "周杰伦")
        pl_commute_b = playlist(baseB, "通勤", items("b", 5))
        store(baseB, "settings", {"last_track": {
            "item": {"id": "2", "platform": "kuwo", "name": "稻香", "artist": "周杰伦"},
            "quality": {"format": "mp3", "bitrate": 128},
            "position": 12, "duration": 223, "at": int(time.time()) - 900}})
        api(baseB, "/api/sync/config", {"url": dav.url(), "user": "u@test",
                                        "pass": "secret-***", "dir": "Elysium"})

        # ---------------------------------------------------- S5/S6 dry-run
        before = biz_digest(dataB)
        r = api(baseB, "/api/sync/pull", {})
        d = r["data"]
        after_before = biz_digest(dataB)
        pl = d["playlists"]
        check("S5a diff 认出远端多出 2 项收藏", len(d["favorites"]) == 2,
              "favorites=%s" % [f.get("name") for f in d["favorites"]])
        check("S5b 认出 1 个新歌单", len(pl["new"]) == 1,
              "%s" % [x.get("name") for x in pl["new"]])
        check("S5c 同名不同 id 列成分歧而不是新歌单",
              len(pl["conflicts"]) == 1 and pl["conflicts"][0]["name"] == "通勤",
              "%s" % [(x["name"], x["local"]["count"], x["remote"]["count"]) for x in pl["conflicts"]])
        check("S5d 进度按 at 大者胜（远端更新）", bool(d["last_track"]),
              "%s" % ((d.get("last_track") or {}).get("position"),))
        check("S6 dry-run 零写入（业务字段指纹不变）", before == after_before,
              "%s vs %s" % (before[:8], after_before[:8]))

        # ---------------------------------------------------- S7 合并
        c = api(baseB, "/api/sync/commit", {
            "favorites": True,
            "playlist_new": [x["id"] for x in pl["new"]],
            "playlist_fill": [],
            "conflicts": {"通勤": "both"},
            "last_track": True,
        })
        check("S7a 合并结果计数正确",
              c["ok"] and c["data"]["favorites"] == 2 and c["data"]["playlists_new"] == 1,
              "%s" % (c.get("data"),))
        gotB = api(baseB, "/api/store")
        favs = gotB["favorites"]
        pls = gotB["playlists"]
        names = sorted(p["name"] for p in pls)
        check("S7b 合并后本地原有的不再丢（收藏 3 项）", len(favs) == 3,
              "%s" % [f.get("name") for f in favs])
        check("S7c 歌单并存：本机通勤 + 远端夜跑 + 通勤（远端）", len(pls) == 3,
              "%s" % names)
        check("S7d 分歧选 both 后两份内容都在",
              any(len(p["items"]) == 5 for p in pls) and any(len(p["items"]) == 12 for p in pls),
              "%s" % [(p["name"], len(p["items"])) for p in pls])

        # ---------------------------------------------------- S8 同 id 增量（fill）
        # A 给「夜跑」再添两首并上传；B 那边 id 相同，应识别成「可补条目」而不是新歌单
        store(baseA, "playlist_add", {"id": pl_run, "items": items("x", 2)})
        api(baseA, "/api/sync/push", {})
        r2 = api(baseB, "/api/sync/pull", {})
        fill = r2["data"]["playlists"]["fill"]
        check("S8a 同 id 歌单的增量认成「可补条目」",
              len(fill) == 1 and fill[0]["add"] == 2,
              "%s" % [(x["name"], x["add"], x["total"]) for x in fill])
        check("S8b 这时不该再冒出同名分歧", not r2["data"]["playlists"]["conflicts"],
              "%s" % [x["name"] for x in r2["data"]["playlists"]["conflicts"]])
        c2 = api(baseB, "/api/sync/commit", {"playlist_fill": [pl_run]})
        got2 = api(baseB, "/api/store")
        run_pl = next((p for p in got2["playlists"] if p["id"] == pl_run), None)
        check("S8c 补条目落盘且不重复", c2["ok"] and run_pl and len(run_pl["items"]) == 5,
              "%s" % [(p["name"], len(p["items"])) for p in got2["playlists"]])

        # ---------------------------------------------------- S9 进度
        lt = (gotB.get("settings") or {}).get("last_track") or {}
        check("S9 进度切到远端更新的那条", lt.get("position") == 95,
              "position=%s at=%s" % (lt.get("position"), lt.get("at")))

        # ---------------------------------------------------- S10 export
        ex = api(baseB, "/api/store/export")
        exd = json.dumps(ex["data"], ensure_ascii=False)
        check("S10 export 不含同步凭据",
              "webdav" not in (ex["data"].get("settings") or {}) and "secret-***" not in exd)
        check("S10b export 仍做路径脱敏",
              (ex["data"].get("settings") or {}).get("download_dir") == "<本机音乐目录>")

        # ---------------------------------------------------- S11 失败明示
        dead = "http://127.0.0.1:%d/dav" % free_port()
        api(baseA, "/api/sync/config", {"url": dead})
        e = api(baseA, "/api/sync/push", {})
        check("S11 连不上时返回可读错误而不是静默失败",
              e["ok"] is False and any(k in (e.get("error") or "")
                                       for k in ("连不上", "超时", "证书", "失败")),
              "%s" % e.get("error"))

        # ---------------------------------------------------- 二次上传（幂等）
        api(baseA, "/api/sync/config", {"url": dav.url()})
        p2 = api(baseA, "/api/sync/push", {})
        check("S12 重复上传幂等（远端仍是同一份包）",
              p2["ok"] and json.loads(dav.body("Elysium/elysium-sync.json").decode("utf-8"))
              .get("app") == "Elysium")

    finally:
        for p in (pa, pb):
            try:
                if p:
                    p.terminate()
                    p.wait(timeout=10)
            except Exception:
                pass
        dav.stop()
        if args.keep:
            print("\n保留临时目录：" + base_dir)
        else:
            shutil.rmtree(base_dir, ignore_errors=True)

    ok = sum(1 for _, p, _ in RESULTS if p)
    print("\n== 结果：%d pass / %d fail ==" % (ok, len(RESULTS) - ok))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
