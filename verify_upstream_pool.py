# -*- coding: utf-8 -*-
"""上游站点池验收：故障转移 / 熔断半开 / 逐层兜底 / manifest 热应用。

背景：主站是社区单机（2026-11-17 实测无 CF 无备援），站点池把「故障转移 +
熔断 + 健康探测 + 路由表热更新」收敛到 upstream() / search_any 两个咽喉。
本套件用本地 stub 伪装三类上游（ajax.php / GD api.php / 平台直连搜索），
全程不碰真实上游 —— CI 可收（verify.yml core job）。

断言：
  V1 RPC stations 池快照（内置 5 站全在，活动=hi）
  V2 主站 happy path（条目归一化正确）
  V3 主站网络失败 → 同请求内换站应答 + 连败 2 次熔断
  V4 业务错误（code!=0）不触发熔断，且搜索掉到酷我直连层
  V5 冷却到期半开恢复（主站复活后回到首选）
  V6 hi 全空 → wyy 落到备用站（gd）
  V7 备用站也空 → wyy 走网易直连 / kuwo 走酷我直连（MUSIC_ 前缀剥掉）
  V8 manifest 热应用路由表（内存替换 hi 层）+ providers 指纹不变
  V9 非法路由表（http 明文 / kind 越权 / 缺 id）被拒，保留 last-known-good
  V10 /api/diag 七跳含「站点池」跳且全 ok

用法：python verify_upstream_pool.py
依赖：仅标准库（stub 全本地）
"""
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
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_gapless as _vg  # noqa: E402
from verify_gapless import REPO, PY, check, note, _free_port  # noqa: E402

RESULTS = _vg.RESULTS          # check() 写入 verify_gapless 的全局列表


def rpc(base, fn, params=None, timeout=15):
    body = json.dumps({"fn": fn, "params": params or {}}).encode()
    req = urllib.request.Request(base + "/api/rpc", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def control(port, **kw):
    qs = urllib.parse.urlencode(kw)
    with urllib.request.urlopen("http://127.0.0.1:%d/control?%s" % (port, qs), timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fingerprint(root):
    h = hashlib.sha256()
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            h.update(os.path.relpath(p, root).replace("\\", "/").encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


# --------------------------------------------------------------------------
# 本地 stub：P1 只伺服 hi 主站；P2 伺服 hi2 / gd / dne / dkw / manifest /
# control / audio。behavior 是线程共享字典，/control 改它即可切换形态。
# --------------------------------------------------------------------------
class Stub(BaseHTTPRequestHandler):
    behavior = {"hi": "ok", "hi2": "ok", "gd": "ok", "dne": "ok", "dkw": "ok",
                "manifest": "empty"}
    p2 = 0                      # P2 端口（getUrl 直链指向它）
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ajax_payload(self, who):
        """who 站点当前形态 → (http_code, body_dict)。"""
        mode = self.behavior.get(who, "ok")
        if mode == "net500":
            return 500, {"error": "stub outage"}
        if mode == "biz":
            return 200, {"code": 7, "msg": "接口错误 7"}
        if mode == "empty":
            return 200, {"code": 0, "data": {"list": [], "total": 0}}
        act = urllib.parse.urlparse(self.path).query
        act = urllib.parse.parse_qs(act).get("act", [""])[0]
        if act == "getUrl":
            return 200, {"code": 0, "data": {
                "url": "http://127.0.0.1:%d/audio.mp3" % self.p2,
                "format": "mp3", "bitrate": 320, "duration": 240, "songid": 123}}
        return 200, {"code": 0, "data": {"list": [{
            "id": 123, "name": "晴天", "artist": "测试歌手", "album_name": "测试专辑",
            "album_id": 9, "duration": 240,
            "pic_url": "http://127.0.0.1:%d/cover.jpg" % self.p2,
            "minfo": [{"format": "mp3", "bitrate": "320"}],
            "time": 1790000000, "sign": "deadbeef"}], "total": 1}}

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/ajax.php":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            who = qs.get("who", ["hi"])[0] if "who" in qs else \
                ("hi2" if self.headers.get("Referer", "").endswith("/hi2") else "hi")
            code, payload = self._ajax_payload(who)
            self._json(payload, code)
            return
        self._json({"error": "not found"}, 404)

    def _search_station(self, who):
        mode = self.behavior.get(who, "ok")
        if mode == "empty":
            return []
        if mode == "net500":
            self._json({"error": "stub outage"}, 500)
            return None
        return "ok"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/control":
            with self.lock:
                for k, v in qs.items():
                    self.behavior[k] = v[0]
            self._json({"ok": True, "behavior": dict(self.behavior)})
            return
        if path == "/ajax.php":                       # hi2（与 hi 同形不同端口）
            code, payload = self._ajax_payload("hi2")
            self._json(payload, code)
            return
        if path == "/gd/api.php":
            out = self._search_station("gd")
            if out == "ok":
                out = [{"id": "9001", "name": "晴天(gd)", "artist": ["GD歌手"],
                        "album": "GD专辑"}]
            if out is not None:
                self._json(out)
            return
        if path == "/ne/api/search/get/web":
            out = self._search_station("dne")
            if out == "ok":
                out = {"result": {"songCount": 1, "songs": [{
                    "id": 7001, "name": "晴天(ne)", "artists": [{"name": "NE歌手"}],
                    "album": {"id": 77, "name": "NE专辑"}, "duration": 241000}]}}
            if out is not None:
                self._json(out)
            return
        if path == "/kw/r.s":
            out = self._search_station("dkw")
            if out == "ok":
                out = ("{'abslist': [{'SONGNAME': '晴天(kw)', 'ARTIST': 'KW歌手', "
                       "'ALBUMNAME': 'KW专辑', 'ALBUMID': '88', "
                       "'MUSICRID': 'MUSIC_6001', 'DURATION': '239'}], 'TOTAL': '1'}")
                body = out.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        if path == "/manifest.json":
            mode = self.behavior.get("manifest", "empty")
            if mode == "m1":
                b = "http://127.0.0.1:%d" % self.p2
                self._json({"stations": [
                    {"id": "m1", "name": "镜像站", "kind": "hi",
                     "host": "127.0.0.1:%d" % self.p2, "base": b}]})
            elif mode == "invalid":
                self._json({"stations": [
                    {"id": "BAD ID", "name": "x", "kind": "hi",
                     "host": "a.b", "base": "http://a.b"},
                    {"id": "m2", "name": "y", "kind": "gd",
                     "host": "c.d", "base": "https://c.d"},
                    {"name": "z", "kind": "hi", "host": "e.f", "base": "https://e.f"}]})
            else:
                self._json({"stations": []})
            return
        if path.startswith("/audio"):
            body = b"\xff\xfb" + b"\x00" * 62          # mp3 帧头 + 填充
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)


def serve_stub(port, handler):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def spawn_pool(base_dir, stations, port1, port2):
    """verify_gapless.spawn 的池化版：注入 ELYSIUM_STATIONS + 快熔断参数，
    并在启动前把 home 内 manifest.json 的 update_url 指向本地 stub
    （巡检线程的远端路由表拉取全程离线）。"""
    home = os.path.join(base_dir, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        dst = os.path.join(home, item)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(REPO, item), dst,
                        ignore=shutil.ignore_patterns("__pycache__"))
    mf = os.path.join(home, "providers", "manifest.json")
    with open(mf, encoding="utf-8") as f:
        m = json.load(f)
    m["update_url"] = "http://127.0.0.1:%d/manifest.json" % port2
    m["update_url_mirrors"] = []
    with open(mf, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    port = _free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port),
               ELYSIUM_STATIONS=json.dumps(stations),
               ELYSIUM_POOL_TRIP="2", ELYSIUM_POOL_COOLDOWN="3")
    logf = open(os.path.join(base_dir, "server.log"), "wb")
    proc = subprocess.Popen([PY, "main.py", "--serve", "--port=%d" % port], cwd=REPO,
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/api/cache", timeout=1).read(64)
            return proc, base, home
        except Exception:
            time.sleep(0.25)
    logf.flush()
    print(open(os.path.join(base_dir, "server.log"), "rb").read().decode("utf-8", "replace")[:1500])
    proc.terminate()
    raise SystemExit("isolated server never came up")


def main():
    bd = tempfile.mkdtemp(prefix="elysium_pool_")
    port1, port2 = _free_port(), _free_port()

    class H2(Stub):
        p2 = port2

    class H1(Stub):
        p2 = port2

    srv1 = srv2 = proc = None
    try:
        srv1 = serve_stub(port1, H1)
        srv2 = serve_stub(port2, H2)
        stations = [
            {"id": "hi", "name": "主站A", "kind": "hi",
             "host": "127.0.0.1:%d" % port1, "base": "http://127.0.0.1:%d" % port1},
            {"id": "hi2", "name": "主站B", "kind": "hi",
             "host": "127.0.0.1:%d" % port2, "base": "http://127.0.0.1:%d/hi2" % port2},
            {"id": "gd", "name": "备用站", "kind": "gd",
             "base": "http://127.0.0.1:%d/gd/api.php" % port2},
            {"id": "dne", "name": "网易直连", "kind": "direct_netease",
             "base": "http://127.0.0.1:%d/ne" % port2},
            {"id": "dkw", "name": "酷我直连", "kind": "direct_kuwo",
             "base": "http://127.0.0.1:%d/kw" % port2},
        ]
        proc, base, home = spawn_pool(bd, stations, port1, port2)

        # V1: 池快照
        out = rpc(base, "stations")
        snap = out["data"]["stations"]
        ids = [s["id"] for s in snap]
        kinds = {s["id"]: s["kind"] for s in snap}
        check("V1 池快照 5 站齐全",
              out.get("ok") and len(snap) == 5 and
              kinds == {"hi": "hi", "hi2": "hi", "gd": "gd",
                        "dne": "direct_netease", "dkw": "direct_kuwo"},
              "ids=%s" % ids)
        check("V1 活动=hi", out["data"]["active"] == "hi", "active=%r" % out["data"]["active"])

        # V2: 主站 happy path
        out = rpc(base, "search_one", {"platform": "kuwo", "keyword": "晴天2", "page": 1, "size": 20})
        lst = (out.get("data") or {}).get("list") or []
        ok2 = out.get("ok") and lst and lst[0].get("station") == "hi" \
            and lst[0].get("platform") == "kuwo" and str(lst[0].get("id")) == "123" \
            and isinstance(lst[0].get("minfo"), list)
        check("V2 主站搜索走 hi 且条目归一化", ok2, json.dumps(out, ensure_ascii=False)[:160])

        # V3: 主站网络失败 → 同请求换站 + 连败 2 次熔断
        control(port1, hi="net500")
        st1 = rpc(base, "search_one", {"platform": "kuwo", "keyword": "晴天3a", "page": 1, "size": 20})
        st2 = rpc(base, "search_one", {"platform": "kuwo", "keyword": "晴天3b", "page": 1, "size": 20})
        s1 = ((st1.get("data") or {}).get("list") or [{}])[0].get("station")
        s2 = ((st2.get("data") or {}).get("list") or [{}])[0].get("station")
        snap = {s["id"]: s for s in rpc(base, "stations")["data"]["stations"]}
        check("V3 网络失败同请求换到 hi2", s1 == "hi2" and s2 == "hi2", "s1=%r s2=%r" % (s1, s2))
        check("V3 连败 2 次主站熔断", snap["hi"]["tripped"] is True and snap["hi"]["last_err"] != "",
              "hi=%s" % json.dumps(snap["hi"], ensure_ascii=False)[:120])

        # V4: 业务错误不熔断 + 掉到酷我直连层（hi 挂网络故障、hi2 回业务错）
        control(port1, hi="net500")
        control(port2, hi2="biz")
        time.sleep(0.2)
        out = rpc(base, "search_one", {"platform": "kuwo", "keyword": "晴天4", "page": 1, "size": 20})
        lst = (out.get("data") or {}).get("list") or []
        snap = {s["id"]: s for s in rpc(base, "stations")["data"]["stations"]}
        check("V4 业务错误掉到酷我直连",
              lst and lst[0].get("station") == "dkw" and lst[0].get("id") == "6001",
              "station=%r id=%r" % (lst[0].get("station") if lst else None,
                                    lst[0].get("id") if lst else None))
        check("V4 业务错误未触发熔断", snap["hi2"]["tripped"] is False,
              "hi2=%s" % json.dumps(snap["hi2"], ensure_ascii=False)[:120])

        # V5: 冷却到期半开恢复（hi 已在 V3 熔断，冷却 3s；巡检探针也会补账）
        control(port1, hi="ok")
        control(port2, hi2="ok")
        deadline = time.time() + 10
        recovered = False
        while time.time() < deadline:
            snap = {s["id"]: s for s in rpc(base, "stations")["data"]["stations"]}
            if not snap["hi"]["tripped"]:
                recovered = True
                break
            time.sleep(0.3)
        out = rpc(base, "search_one", {"platform": "kuwo", "keyword": "晴天5", "page": 1, "size": 20})
        s5 = ((out.get("data") or {}).get("list") or [{}])[0].get("station")
        check("V5 冷却到期半开回主站", recovered and s5 == "hi", "recovered=%s s5=%r" % (recovered, s5))

        # V6: hi 全空 → wyy 落到备用站
        control(port1, hi="empty")
        control(port2, hi2="empty")
        out = rpc(base, "search_one", {"platform": "wyy", "keyword": "晴天6", "page": 1, "size": 20})
        lst = (out.get("data") or {}).get("list") or []
        dbg = {s["id"]: (s["tripped"], s["last_err"][:40]) for s in rpc(base, "stations")["data"]["stations"]}
        ok6 = lst and lst[0].get("station") == "gd" and str(lst[0].get("id")) == "9001" \
            and isinstance(lst[0].get("artist"), str) and "GD" in lst[0]["artist"]
        check("V6 hi 全空 wyy 落备用站", ok6,
              json.dumps(out, ensure_ascii=False)[:160] + " pool=" + json.dumps(dbg, ensure_ascii=False))

        # V7: 备用站也空 → 直连层
        control(port2, gd="empty")
        out = rpc(base, "search_one", {"platform": "wyy", "keyword": "晴天7", "page": 1, "size": 20})
        lst = (out.get("data") or {}).get("list") or []
        ok7a = lst and lst[0].get("station") == "dne" and str(lst[0].get("id")) == "7001" \
            and isinstance(lst[0].get("duration"), int) and lst[0]["duration"] == 241 \
            and isinstance(lst[0].get("minfo"), list) and len(lst[0]["minfo"]) == 3
        out = rpc(base, "search_one", {"platform": "kuwo", "keyword": "晴天7b", "page": 1, "size": 20})
        lst = (out.get("data") or {}).get("list") or []
        ok7b = lst and lst[0].get("station") == "dkw" and lst[0].get("name") == "晴天(kw)" \
            and lst[0].get("duration") == 239
        check("V7 wyy 走网易直连（时长 ms→s）", ok7a, json.dumps(lst[:1], ensure_ascii=False)[:160])
        check("V7 kuwo 走酷我直连（剥 MUSIC_ 前缀）", ok7b, json.dumps(lst[:1], ensure_ascii=False)[:160])

        # 恢复正常形态（后续诊断跳要走真实 stub 链路）
        control(port1, hi="ok")
        control(port2, hi2="ok", gd="ok", dne="ok", dkw="ok")

        # V8: manifest 热应用路由表 + providers 指纹不变
        control(port2, manifest="m1")
        fp_before = fingerprint(os.path.join(home, "providers"))
        ref = rpc(base, "pool_refresh")
        time.sleep(0.3)
        snap = rpc(base, "stations")["data"]
        ids = [s["id"] for s in snap["stations"]]
        fp_after = fingerprint(os.path.join(home, "providers"))
        check("V8 路由表内存应用（hi 层换成 m1）",
              ref.get("data", ref).get("applied") is True and "m1" in ids
              and "hi" not in ids and "hi2" not in ids and snap["active"] == "m1",
              "ref=%s ids=%s" % (json.dumps(ref, ensure_ascii=False)[:120], ids))
        check("V8 providers 指纹不变（notify-only 不写盘）", fp_before == fp_after)

        # V9: 非法路由表被拒，保留 last-known-good
        control(port2, manifest="invalid")
        ref = rpc(base, "pool_refresh")
        snap = {s["id"]: s for s in rpc(base, "stations")["data"]["stations"]}
        check("V9 非法路由表被拒",
              ref.get("data", ref).get("applied") is False and "m1" in snap and snap["m1"]["kind"] == "hi",
              "ref=%s" % json.dumps(ref, ensure_ascii=False)[:160])
        control(port2, manifest="empty")
        ref = rpc(base, "pool_refresh")
        snap = {s["id"]: s for s in rpc(base, "stations")["data"]["stations"]}
        check("V9 空路由表保留原表", "m1" in snap, "ref=%s" % json.dumps(ref, ensure_ascii=False)[:120])
        control(port2, manifest="empty")

        # V10: /api/diag 七跳含站点池跳（全程走 stub，离线）
        with urllib.request.urlopen(base + "/api/diag", timeout=60) as r:
            diag = json.loads(r.read().decode("utf-8", "replace"))
        hops = diag["data"]["hops"]
        names = [h["name"] for h in hops]
        check("V10 诊断七跳含站点池且全 ok",
              len(hops) == 7 and any("站点池" in n for n in names) and all(h["ok"] for h in hops),
              "; ".join("%s:%s" % (h["name"], h["detail"][:40]) for h in hops if not h["ok"]))

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
        for srv in (srv1, srv2):
            if srv is not None:
                srv.shutdown()
        if "--keep" not in sys.argv:
            shutil.rmtree(bd, ignore_errors=True)

    ok = sum(1 for _n, good, _d in RESULTS if good)
    print("\n== 结果：%d pass / %d fail ==" % (ok, len(RESULTS) - ok))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
