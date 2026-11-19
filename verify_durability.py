# -*- coding: utf-8 -*-
"""Elysium 数据可靠性回归（健康镜像 / 撤销快照 / 原子写）。

覆盖四个新机制：
  A save 成功后生成 data.json.good 健康镜像
  B 主文件被外部写坏 / 截断 / 删掉后，启动自动从 .good 恢复，收藏不丢
  C 破坏性操作前留 .undo；undo 端点能把数据恢复回来；非破坏性操作不覆盖快照
  D 脱敏导出 / 正常启动路径不被新字段带崩；无快照时 undo 明确返回失败

用法：
    python verify_durability.py
环境：ELYSIUM_HOME 指向临时目录，复制 web/providers，绝不触碰真实 data.json。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TMP = None
RESULTS = []


def record(name, cond, expect, actual):
    RESULTS.append(bool(cond))
    mark = "[PASS] " if cond else "[FAIL] "
    print("%s%-42s | 期望 %s | 实际 %s" % (mark, name, expect, actual), flush=True)


def mkhome(tag):
    home = os.path.join(TMP, tag, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        shutil.copytree(os.path.join(REPO, item), os.path.join(home, item),
                        ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    if os.path.isfile(os.path.join(REPO, "update.json")):
        shutil.copy2(os.path.join(REPO, "update.json"), os.path.join(home, "update.json"))
    return home


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def spawn(home):
    port = free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
    logf = open(os.path.join(home, "..", "server.log"), "wb")
    proc = subprocess.Popen([PY, "main.py", "--serve", "--port=%d" % port],
                            cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    logf.close()
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return {"proc": proc, "port": port, "home": home}
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server failed to boot: " +
                       open(os.path.join(home, "..", "server.log"), "rb").read().decode("utf-8", "replace")[-1000:])


def kill(env):
    if not env:
        return
    try:
        env["proc"].terminate()
        env["proc"].wait(timeout=5)
    except Exception:                                       # noqa: BLE001
        try:
            env["proc"].kill()
        except Exception:                                   # noqa: BLE001
            pass


def http(env, method, path, body=b"", headers=None, timeout=8):
    hs = {"Connection": "close", "Host": "127.0.0.1:%d" % env["port"]}
    hs.update(headers or {})
    if method in ("POST", "PUT") or body:
        hs["Content-Length"] = str(len(body))
    raw = ("%s %s HTTP/1.1\r\n" % (method, path)).encode()
    raw += "".join("%s: %s\r\n" % (k, v) for k, v in hs.items()).encode("latin-1", "replace")
    raw += b"\r\n" + body
    s = socket.create_connection(("127.0.0.1", env["port"]), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(raw)
    buf = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    head, _, payload = buf.partition(b"\r\n\r\n")
    st = int(head.split()[1])
    return st, payload


def jpost(env, path, obj):
    return http(env, "POST", path, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                {"Content-Type": "application/json"})


def jget(env, path):
    st, payload = http(env, "GET", path)
    try:
        return st, json.loads(payload.decode("utf-8", "replace"))
    except Exception:                                       # noqa: BLE001
        return st, None


def jpost_json(env, path, obj):
    """POST 并把响应解析成 dict，返回 (status, dict|None)。"""
    st, payload = jpost(env, path, obj)
    try:
        return st, json.loads(payload.decode("utf-8", "replace"))
    except Exception:                                       # noqa: BLE001
        return st, None


def dd(home):
    return os.path.join(home, "Elysium")


def add_fav(env, name):
    return jpost(env, "/api/store", {"op": "fav_toggle", "payload": {
        "kind": "song", "id": name, "name": name}})


def fav_names(env):
    _, data = jget(env, "/api/store")
    return [f.get("name") for f in (data or {}).get("favorites", [])]


def group_A_good_mirror():
    home = mkhome("a_good")
    env = spawn(home)
    try:
        add_fav(env, "晴天")
        p = os.path.join(dd(home), "data.json.good")
        record("A1 save 后生成 .good 健康镜像", os.path.isfile(p),
               "文件存在", "存在" if os.path.isfile(p) else "缺失")
        if os.path.isfile(p):
            good = json.load(open(p, encoding="utf-8"))
            record("A2 .good 内容与当前数据一致", "晴天" in [f.get("name") for f in good.get("favorites", [])],
                   "含晴天", str([f.get("name") for f in good.get("favorites", [])]))
    finally:
        kill(env)


def group_B_corrupt_recover():
    for label, corrupt in (
        ("截断", lambda p: open(p, "wb").write(b'{"settings":{')),
        ("清空", lambda p: open(p, "wb").write(b"")),
        ("删文件", lambda p: os.remove(p)),
    ):
        home = mkhome("b_" + label)
        env = spawn(home)
        try:
            add_fav(env, "晴天")
        finally:
            kill(env)
        p = os.path.join(dd(home), "data.json")
        corrupt(p)
        env = spawn(home)                      # 重启：主文件坏，触发自动恢复
        try:
            names = fav_names(env)
            record("B 主文件%s后收藏仍在" % label, "晴天" in names,
                   "含晴天", str(names))
            # 恢复后主文件应被重写为健康数据
            main = json.load(open(p, encoding="utf-8"))
            record("B 主文件%s后已重建" % label, "晴天" in [f.get("name") for f in main.get("favorites", [])],
                   "含晴天", str([f.get("name") for f in main.get("favorites", [])]))
        finally:
            kill(env)


def group_C_undo():
    home = mkhome("c_undo")
    env = spawn(home)
    try:
        for n in ("晴天", "稻香", "夜曲"):
            add_fav(env, n)
        record("C0 初始三个收藏", len(fav_names(env)) == 3, "3", str(fav_names(env)))

        # 非破坏性操作不应产生 undo
        jpost(env, "/api/store", {"op": "settings", "payload": {"volume": 55}})
        record("C1 非破坏性操作无 undo", not os.path.isfile(os.path.join(dd(home), "data.json.undo")),
               "无 .undo", "有" if os.path.isfile(os.path.join(dd(home), "data.json.undo")) else "无")

        st, data = jpost_json(env, "/api/store", {"op": "favorites_clear", "payload": {}})
        record("C2 清空收藏返回成功", st == 200 and (data or {}).get("ok") is not False,
               "200/ok", "%s/%s" % (st, (data or {}).get("ok")))
        record("C3 清空后收藏为空", len(fav_names(env)) == 0, "0", str(fav_names(env)))
        record("C4 破坏性操作生成 .undo", os.path.isfile(os.path.join(dd(home), "data.json.undo")),
               "有 .undo", "有" if os.path.isfile(os.path.join(dd(home), "data.json.undo")) else "无")

        _, boot = jget(env, "/api/store")
        record("C5 GET 带 _has_undo 标记", boot.get("_has_undo") is True,
               "True", str(boot.get("_has_undo")))

        st, data = jpost_json(env, "/api/store", {"op": "undo", "payload": {}})
        record("C6 undo 返回成功", st == 200 and (data or {}).get("ok") is not False,
               "200/ok", "%s/%s" % (st, (data or {}).get("ok")))
        names = fav_names(env)
        record("C7 undo 恢复三个收藏", set(names) == {"晴天", "稻香", "夜曲"},
               "三首齐全", str(names))
    finally:
        kill(env)


def group_D_guard():
    home = mkhome("d_guard")
    env = spawn(home)
    try:
        st, data = jpost_json(env, "/api/store", {"op": "undo", "payload": {}})
        record("D1 无快照时 undo 明确失败", st == 200 and (data or {}).get("ok") is False,
               "ok:false", "%s/%s" % (st, (data or {}).get("ok")))
        st, data = jget(env, "/api/store/export")
        body = json.dumps(data)
        record("D2 脱敏导出不含内部字段",
               "_has_undo" not in body and "_data_path" not in body,
               "无内部字段", "ok" if "_has_undo" not in body else "泄漏 _has_undo")
    finally:
        kill(env)


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="elysium_durability_")
    try:
        for grp in (group_A_good_mirror, group_B_corrupt_recover,
                    group_C_undo, group_D_guard):
            print("\n== %s ==" % grp.__name__, flush=True)
            grp()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(RESULTS)
    total = len(RESULTS)
    print("\n结果：%d/%d PASS" % (passed, total), flush=True)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
