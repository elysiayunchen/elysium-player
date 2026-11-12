# -*- coding: utf-8 -*-
"""Elysium 边缘测试（boundary / edge-case 回归）。

关注点不是「功能能不能用」，而是**越界与畸形输入**：
  A 路由 / 静态资源：路径遍历、超长 URL、Host 伪造（DNS rebinding）
  B 本地曲库：id 路径逃逸、绝对路径、UNC、junction 逃逸、中文/空/超长
  C Range：倒序、越界、尾部、多段、超长数字
  D 请求体：空 / null / 数组 / 非法 JSON / 深嵌套 / Content-Length 说谎 / 超大
  E store：非法 op、脏类型注入后其它接口是否被带崩、长度与范围校验
  F data.json：截断 / 空 / 非 dict / BOM / 只读 / 超大垃圾
  G 纯函数：版本号、文件名清洗、模板、歌单解析、同步打包
  H 写盘：歌词文件名逃逸、超长名、非法目标盘
  I K 歌作品：id 逃逸、长度边界、谎报 Content-Length
  J 并发 / 半开连接：数据不脏、不阻塞

用法：
    python verify_edge.py                 # 全部
    python verify_edge.py --only=A,B      # 只跑指定组
    python verify_edge.py --keep          # 保留临时目录
环境：ELYSIUM_HOME 指向临时目录，复制 web/providers，绝不触碰真实 data.json。
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []
SERVER_LOGS = []
TMP_ROOT = None


# --------------------------------------------------------------------------
# 记录
# --------------------------------------------------------------------------
def record(group, name, verdict, expect, actual):
    RESULTS.append({"group": group, "name": name, "verdict": verdict,
                    "expect": expect, "actual": actual})
    mark = {"PASS": "[PASS] ", "FAIL": "[FAIL] ", "WARN": "[WARN] ", "SKIP": "[SKIP] "}[verdict]
    print("%s%-8s %s | 期望 %s | 实际 %s" % (mark, group, name, expect, actual), flush=True)


def ok(group, name, cond, expect, actual):
    record(group, name, "PASS" if cond else "FAIL", expect, actual)
    return bool(cond)


def warn(group, name, cond, expect, actual):
    """cond=True 表示符合安全预期（PASS）；否则记为 WARN 而不是 FAIL。"""
    record(group, name, "PASS" if cond else "WARN", expect, actual)
    return bool(cond)


# --------------------------------------------------------------------------
# 隔离环境
# --------------------------------------------------------------------------
def mkhome(tag):
    base = os.path.join(TMP_ROOT, tag)
    home = os.path.join(base, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        dst = os.path.join(home, item)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(REPO, item), dst,
                        ignore=shutil.ignore_patterns("__pycache__"))
    upd = os.path.join(REPO, "update.json")
    if os.path.isfile(upd):
        shutil.copy2(upd, os.path.join(home, "update.json"))
    return base, home


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def spawn(home, port=None, env_extra=None):
    port = port or free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
    env.update(env_extra or {})
    log_path = os.path.join(home, "..", "server.log")
    logf = open(log_path, "wb")
    proc = subprocess.Popen([PY, "main.py", "--serve", "--port=%d" % port],
                            cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    # 子进程已继承句柄；父进程这份必须关掉，否则 Windows 上文件一直被占用，
    # 收尾时 rmtree 删不掉临时目录（每跑一次留一个垃圾目录）。
    logf.close()
    base = "http://127.0.0.1:%d" % port
    t0 = time.time()
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return {"proc": proc, "port": port, "base": base, "log": log_path,
                    "home": home, "boot": round(time.time() - t0, 2)}
        except OSError:
            time.sleep(0.2)
    try:
        tail = open(log_path, "rb").read().decode("utf-8", "replace")[-1200:]
    except OSError:
        tail = ""
    return {"proc": proc, "port": port, "base": base, "log": log_path,
            "home": home, "boot": None, "dead": tail}


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


def log_text(env):
    try:
        with open(env["log"], "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def tracebacks(text):
    """服务端未捕获异常统计（ThreadingHTTPServer 会把 handler 异常打到 stderr）。"""
    out = {}
    for m in re.finditer(r"Traceback \(most recent call last\):(.*?)(?=\n\S|\Z)", text, re.S):
        block = m.group(1)
        names = re.findall(r"^(\w+(?:Error|Exception)):", block, re.M)
        key = names[-1] if names else "Traceback"
        out[key] = out.get(key, 0) + 1
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http(env, method, path, body=b"", headers=None, timeout=6,
         host=None, version="HTTP/1.1", no_host=False):
    hs = {"Connection": "close"}
    if not no_host:
        hs["Host"] = host if host is not None else "127.0.0.1:%d" % env["port"]
    if method in ("POST", "PUT", "PATCH") or body:
        hs["Content-Length"] = str(len(body))
    hs.update(headers or {})
    head = "%s %s %s\r\n" % (method, path, version)
    head += "".join("%s: %s\r\n" % (k, v) for k, v in hs.items())
    data = head.encode("latin-1", "replace") + b"\r\n" + body
    try:
        s = socket.create_connection(("127.0.0.1", env["port"]), timeout=timeout)
    except OSError as e:
        return None, b"", "connect: %s" % e
    s.settimeout(timeout)
    buf = b""
    try:
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 12_000_000:
                return None, buf, "response too large"
    except socket.timeout:
        return None, buf, "timeout"
    except OSError as e:
        return None, buf, "socket: %s" % e
    finally:
        try:
            s.close()
        except OSError:
            pass
    if not buf:
        return None, b"", "empty response"
    head_part, _, payload = buf.partition(b"\r\n\r\n")
    m = re.match(rb"HTTP/1\.[01] (\d{3})", head_part)
    return (int(m.group(1)) if m else None), payload, ""


def jpost(env, path, obj, **kw):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    kw.setdefault("headers", {})["Content-Type"] = "application/json"
    return http(env, "POST", path, body, **kw)


def alive(env):
    st, _, err = http(env, "GET", "/api/lyric/now", timeout=4)
    return st == 200 and not err


def jbody(payload):
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except Exception:                                       # noqa: BLE001
        return None


def q(s):
    import urllib.parse
    return urllib.parse.quote(s, safe="")


# --------------------------------------------------------------------------
# 测试音频
# --------------------------------------------------------------------------
FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413


def make_music(music, outside):
    os.makedirs(music, exist_ok=True)
    os.makedirs(outside, exist_ok=True)
    p = os.path.join(music, "晴天.mp3")
    with open(p, "wb") as f:
        f.write(FRAME * 400)                    # ~166KB，够做 Range
    # FLAC magic：让扩展名与内容不一致，验证解析不炸
    with open(os.path.join(music, "fake.flac"), "wb") as f:
        f.write(b"not-a-flac" * 100)
    # 子目录 + 非音频干扰
    sub = os.path.join(music, "专辑A")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "七里香.mp3"), "wb") as f:
        f.write(FRAME * 30)
    with open(os.path.join(sub, "cover.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0junk")
    # 目录外的同扩展名文件：任何情况下都不该被 /api/local/file 读到
    with open(os.path.join(outside, "secret.mp3"), "wb") as f:
        f.write(FRAME * 10)
    return p


# --------------------------------------------------------------------------
# A 路由 / 静态
# --------------------------------------------------------------------------
def group_A(env):
    g = "A"
    st, body, err = http(env, "GET", "/")
    ok(g, "首页可访问", st == 200 and b"<html" in body.lower(), "200 + html", "%s / %dB" % (st, len(body)))

    st, body, err = http(env, "GET", "/api/not-exist")
    ok(g, "未知 API 返回 404 JSON", st == 404 and jbody(body) is not None, "404 json", "%s %s" % (st, (jbody(body) or {}).get("error")))

    leaks = []
    for path, label in [
        ("/../main.py", "单层 .."),
        ("/%2e%2e/main.py", "编码 .."),
        ("/..%5c..%5cmain.py", "反斜杠 .."),
        ("/web/../../main.py", "web 向上两级"),
        ("/C:/codex_workspace/Elysium/main.py", "Windows 绝对路径"),
        ("//etc/passwd", "双斜杠 unix 路径"),
        ("/%00", "NUL"),
        ("/%2e%2e%2f%2e%2e%2fmain.py", "全编码遍历"),
    ]:
        st, body, err = http(env, "GET", path)
        leak = b"def main(" in body or b"class Store" in body or b"[defaults]" in body
        if leak or st is None or (st and st >= 500):
            leaks.append("%s: st=%s err=%s leak=%s" % (label, st, err, leak))
    ok(g, "静态资源拒绝路径遍历", not leaks, "无源码泄漏/无 5xx", leaks or "全部 404")

    # 前缀匹配缺陷：webroot 是 .../web，.../webX 也以它开头
    os.makedirs(os.path.join(env["home"], "webX"), exist_ok=True)
    with open(os.path.join(env["home"], "webX", "leak.txt"), "w", encoding="utf-8") as f:
        f.write("SIBLING-SECRET")
    st, body, err = http(env, "GET", "/../webX/leak.txt")
    warn(g, "静态目录前缀匹配（webX 兄弟目录）", b"SIBLING-SECRET" not in body,
         "不可读兄弟目录", "st=%s leak=%s" % (st, b"SIBLING-SECRET" in body))

    for host, want, label in [
        ("evil.example.com", 403, "伪造 Host"),
        ("127.0.0.1.evil.com", 403, "后缀伪装 Host"),
        ("localhost", 200, "localhost"),
        ("127.0.0.1", 200, "无端口 127.0.0.1"),
        ("[::1]:%d" % env["port"], 200, "IPv6 回环"),
    ]:
        st, body, err = http(env, "GET", "/api/lyric/now", host=host)
        ok(g, "Host 校验：" + label, st == want, want, "%s %s" % (st, err))

    st, body, err = http(env, "GET", "/api/lyric/now", version="HTTP/1.0", no_host=True)
    ok(g, "HTTP/1.0 无 Host 被拒", st == 403, "403", str(st))

    st, body, err = http(env, "GET", "/" + "a" * 8000)
    ok(g, "8KB 超长 URL 不崩", st is not None and st < 500, "< 500", "st=%s err=%s" % (st, err))
    ok(g, "超长 URL 后服务仍可用", alive(env), "服务存活", "alive=%s" % alive(env))

    st, body, err = http(env, "HEAD", "/")
    ok(g, "HEAD 未实现但返回 501", st == 501, "501（本地应用不使用 HEAD）", str(st))

    st, body, err = http(env, "GET", "/", version="HTTP/9.9")
    ok(g, "非法协议版本不崩", alive(env) and (st is None or st < 500),
       "关连接或 <500，服务存活", "st=%s err=%s alive=%s" % (st, err, alive(env)))


# --------------------------------------------------------------------------
# B 本地曲库路径
# --------------------------------------------------------------------------
def setup_local(env, music, outside):
    st, body, _ = jpost(env, "/api/store", {"op": "settings", "payload": {
        "download_dir": music, "local_dirs": [outside]}})
    return st == 200


def group_B(env, music, outside):
    g = "B"
    setup_local(env, music, outside)
    # junction：outside 挂到 music/link 下，realpath 必须识破
    link = os.path.join(music, "link")
    if not os.path.exists(link):
        subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                       capture_output=True, shell=False)
    junction_ok = os.path.isdir(link)

    good = "晴天.mp3"
    cases = [
        (good, 200, "合法文件"),
        ("@0/" + good, 200, "显式 0 号根"),
        ("专辑A/七里香.mp3", 200, "子目录"),
        ("@1/secret.mp3", 200, "第二根合法"),
        ("./" + good, 404, "点段 ./"),
        ("专辑A/../" + good, 404, "内部 .."),
        ("../music/" + good, 404, "向上穿越"),
        ("..%2f..%2fmain.py", 404, "编码穿越"),
        ("C:/Windows/win.ini", 404, "Windows 绝对路径"),
        ("C:%5CWindows%5Cwin.ini", 404, "Windows 反斜杠绝对路径"),
        ("%5C%5C%3F%5CC%3A%5CWindows%5Cwin.ini", 404, "UNC 扩展路径"),
        ("/etc/passwd", 404, "unix 绝对路径"),
        ("", 404, "空 p"),
        ("@9/secret.mp3", 404, "越界根序号"),
        ("@-1/secret.mp3", 404, "负数根序号"),
        ("@abc/secret.mp3", 404, "非数字根序号"),
        (good + "%00.png", 404, "NUL 截断"),
        (good + " ", 200, "尾随空格（Windows 路径归一化，等价同一文件）"),
        ("link/secret.mp3", 404, "junction 目录逃逸"),
        ("a" * 9000 + ".mp3", 404, "9KB 超长 id"),
        ("CON.mp3", 404, "Windows 设备名"),
        ("晴天.MP3", 200, "扩展名大小写"),
    ]
    bad = []
    for p, want, label in cases:
        st, body, err = http(env, "GET", "/api/local/file?p=" + q(p), timeout=8)
        if st != want:
            bad.append("%s: want %s got %s (%s)" % (label, want, st, err))
    ok(g, "本地文件 id 越界全部拒绝", not bad, "状态码符合预期", bad or "%d 例全部符合" % len(cases))
    ok(g, "路径边界测试后服务存活", alive(env), "存活", "alive=%s" % alive(env))
    if junction_ok:
        st, body, _ = http(env, "GET", "/api/local/list?refresh=1", timeout=20)
        data = jbody(body) or {}
        items = (data.get("data") or {}).get("items") or []
        leaked = [i for i in items if "secret" in json.dumps(i, ensure_ascii=False)]
        warn(g, "扫描是否跟随 junction 列出目录外文件", not leaked,
             "列表不含目录外文件", "命中 %d 条" % len(leaked))
    else:
        record(g, "junction 逃逸用例", "SKIP", "创建 junction", "创建失败（权限）")


# --------------------------------------------------------------------------
# C Range
# --------------------------------------------------------------------------
def group_C(env, music, outside):
    g = "C"
    setup_local(env, music, outside)
    size = os.path.getsize(os.path.join(music, "晴天.mp3"))
    url = "/api/local/file?p=" + q("晴天.mp3")
    cases = [
        ("bytes=0-0", 206, 1, "首字节"),
        ("bytes=-1", 206, 1, "尾字节"),
        ("bytes=0-", 206, size, "开区间"),
        ("bytes=%d-" % size, 416, None, "起点越界"),
        ("bytes=5-3", 416, None, "倒序"),
        ("bytes=-0", 416, None, "尾部 0 字节"),
        ("bytes=abc", 200, size, "非法单位"),
        ("bytes=0-1,5-6", 206, 2, "多段只取第一段"),
        ("bytes=0-999999999999999999", 206, size, "超大结束位"),
        ("bytes=0000000001-0002", 206, 2, "前导零"),
        ("items=0-1", 200, size, "非 bytes 单位"),
        ("bytes=1-%d" % (size + 999), 206, size - 1, "结束位溢出"),
    ]
    bad, ev = [], []
    for rng, want_st, want_len, label in cases:
        st, body, err = http(env, "GET", url, headers={"Range": rng})
        good = st == want_st and (want_len is None or len(body) == want_len)
        if not good:
            bad.append("%s: want %s/%s got %s/%s (%s)" % (label, want_st, want_len, st, len(body), err))
        ev.append("%s=%s/%s" % (label, st, len(body)))
    ok(g, "Range 边界", not bad, "状态码与长度符合 RFC7233", bad or "; ".join(ev))

    st, body, err = http(env, "GET", url, headers={"Range": "bytes=" + "9" * 5000 + "-"})
    ok(g, "5000 位 Range 数字不崩", st is not None and st < 500, "< 500",
       "st=%s len=%s err=%s" % (st, len(body), err))
    ok(g, "Range 边界后服务存活", alive(env), "存活", "alive=%s" % alive(env))


# --------------------------------------------------------------------------
# D 请求体
# --------------------------------------------------------------------------
def group_D(env):
    g = "D"
    cases = [
        (b"", "空 body"),
        (b"null", "null"),
        (b"[]", "数组"),
        (b'"str"', "字符串"),
        (b"123", "数字"),
        (b"{bad json", "非法 JSON"),
        (b"\xff\xfe\x00\x00", "二进制垃圾"),
        (b'{"op":"settings","payload":{"x":', "截断 JSON"),
        (json.dumps({"a": None}, ensure_ascii=False).encode(), "普通对象"),
    ]
    bad = []
    for body, label in cases:
        st, resp, err = http(env, "POST", "/api/store", body,
                             headers={"Content-Type": "application/json"})
        if st is None or st >= 500:
            bad.append("%s -> %s %s" % (label, st, err))
        elif st == 200:
            j = jbody(resp) or {}
            if j.get("ok") is not False and label not in ("普通对象",):
                bad.append("%s -> 静默成功 %s" % (label, str(j)[:80]))
        elif st not in (400, 422):
            bad.append("%s -> 意外状态 %s" % (label, st))
    ok(g, "畸形请求体不 5xx / 不静默成功", not bad, "400/ok:false", bad or "全部受控")

    deep = b'{"a":' * 3000 + b"1" + b"}" * 3000
    st, resp, err = http(env, "POST", "/api/store", deep,
                         headers={"Content-Type": "application/json"})
    ok(g, "3000 层深嵌套不崩", st is not None and st < 500, "< 500", "st=%s %s" % (st, err))

    st, resp, err = http(env, "POST", "/api/store", b'{"op":"settings"}',
                         headers={"Content-Length": "-1"})
    ok(g, "负 Content-Length 不崩", st is not None and st < 500, "< 500", "st=%s %s" % (st, err))

    st, resp, err = http(env, "POST", "/api/store", b"{}",
                         headers={"Content-Length": "abc"})
    ok(g, "非数字 Content-Length 不崩", st is not None and st < 500, "< 500", "st=%s %s" % (st, err))

    st, resp, err = http(env, "POST", "/api/store", b'{"op":"load"}',
                         headers={"Content-Length": "500"})
    ok(g, "Content-Length 谎报（大）不崩", st is not None and st < 500, "< 500", "st=%s %s" % (st, err))

    # 2MB 合法 JSON：没有 body 上限，记录为 WARN 级资源面
    pad = json.dumps({"op": "settings", "payload": {"pad": "x" * (2 * 1024 * 1024)}}).encode()
    t0 = time.time()
    st, resp, err = http(env, "POST", "/api/store", pad, timeout=30)
    dt = time.time() - t0
    warn(g, "2MB body 被接受（无大小上限）", st == 200, "建议 413/上限",
         "st=%s %.2fs data.json=%.1fMB" % (st, dt,
                                           os.path.getsize(os.path.join(env["home"], "Elysium", "data.json")) / 1e6))
    ok(g, "超大 body 后服务存活", alive(env), "存活", "alive=%s" % alive(env))


# --------------------------------------------------------------------------
# E store 语义 / 脏数据
# --------------------------------------------------------------------------
def group_E(env):
    g = "E"
    checks = [
        ({"op": "settings", "payload": []}, "settings payload 为数组"),
        ({"op": "settings", "payload": "oops"}, "settings payload 为字符串"),
        ({"op": "settings", "payload": 123}, "settings payload 为数字"),
        ({"op": "fav_toggle", "payload": {"id": {"a": 1}}}, "fav id 为对象"),
        ({"op": "fav_toggle", "payload": ["x", 1]}, "fav payload 为数组"),
        ({"op": "playlist_reorder", "payload": {"id": "pl_none", "order": [True, False, 1.5, -3, 999]}},
         "reorder order 混合类型"),
        ({"op": "taste_genres", "payload": {"ids": "kuwo"}}, "ids 为字符串"),
        ({"op": "taste_note", "payload": {"text": "x" * 10000}}, "note 10KB"),
        ({"op": "history_add", "payload": {"q": "y" * 1000000, "platform": "kuwo"}}, "history q 1MB"),
        ({"op": "playlist_create", "payload": {"name": "n" * 10000}}, "歌单名 10KB"),
        ({"op": "definitely-not-an-op", "payload": None}, "未知 op"),
        ({"op": None, "payload": None}, "op 为 null"),
    ]
    bad = []
    for payload, label in checks:
        st, resp, err = http(env, "POST", "/api/store", json.dumps(payload).encode(), timeout=20)
        j = jbody(resp) or {}
        if st is None or st >= 500:
            bad.append("%s -> %s %s" % (label, st, err))
        elif j.get("ok") is False and "not" in label:
            pass
    ok(g, "store 畸形 payload 不 5xx", not bad, "< 500", bad or "全部受控")
    ok(g, "store 畸形 payload 后服务存活", alive(env), "存活", "alive=%s" % alive(env))

    # 未知 op 静默成功：语义上应该 400，但至少不破坏数据
    st, resp, _ = http(env, "POST", "/api/store", json.dumps({"op": "rm-rf", "payload": None}).encode())
    warn(g, "未知 op 静默成功", (jbody(resp) or {}).get("ok") is False,
         "ok:false / 400", "ok=%s" % (jbody(resp) or {}).get("ok"))

    # 关键：脏 settings 持久化后，其它只读接口是否被带崩
    dirty = [
        ({"cache_limit_mb": "abc"}, "/api/cache", "cache_limit_mb 非数字"),
        ({"cache_min_plays": None}, "/api/cache", "cache_min_plays 为 null"),
        ({"local_dirs": "notalist"}, "/api/local/list", "local_dirs 为字符串"),
        ({"download_dir": 12345}, "/api/local/list", "download_dir 为数字"),
        ({"volume": {"x": 1}}, "/api/store", "volume 为对象"),
    ]
    for payload, probe, label in dirty:
        st, _, _ = jpost(env, "/api/store", {"op": "settings", "payload": payload})
        st2, body2, err2 = http(env, "GET", probe, timeout=15)
        ok(g, "脏设置注入后 %s" % label, st2 is not None and st2 < 500,
           "%s 仍 < 500" % probe, "st=%s err=%s" % (st2, err2))
        if st2 is None or st2 >= 500:
            # 立刻恢复，避免污染后续用例
            jpost(env, "/api/store", {"op": "settings", "payload": {
                "cache_limit_mb": 2048, "cache_min_plays": 2,
                "local_dirs": [], "download_dir": env["home"], "volume": 0.8}})

    # 范围校验
    jpost(env, "/api/store", {"op": "settings", "payload": {"cache_limit_mb": -5, "volume": 99}})
    st, body, _ = http(env, "GET", "/api/cache", timeout=10)
    j = jbody(body) or {}
    data = j.get("data") or {}
    warn(g, "设置无范围校验（limit=-5 / volume=99）",
         data.get("limit", 0) >= 0, "拒绝或钳制到合法范围",
         "limit=%s volume=%s" % (data.get("limit"), data.get("volume")))
    jpost(env, "/api/store", {"op": "settings", "payload": {
        "cache_limit_mb": 2048, "volume": 0.8, "download_dir": env["home"], "local_dirs": []}})


# --------------------------------------------------------------------------
# F data.json 损坏恢复
# --------------------------------------------------------------------------
def group_F():
    g = "F"
    cases = [
        (b'{"settings":{', "截断 JSON", True),
        (b"", "空文件", False),
        (b"[]", "数组", True),
        (b"null", "null", False),
        (b'"str"', "字符串", True),
        (b"\xef\xbb\xbf" + json.dumps({"settings": {"theme": "light"},
                                       "junk_marker": 1}).encode(), "UTF-8 BOM", False),
        (b'{"settings":"oops"}', "settings 是字符串", False),
        (b'{"favorites":"oops","playlists":{},"history":null}', "集合字段类型错", False),
        (b"{" + b'"junk":"' + b"x" * 3_000_000 + b'"}', "3MB 未知键", False),
    ]
    no_backup = []
    for blob, label, need_bak in cases:
        base, home = mkhome("f_" + re.sub(r"\W+", "_", label))
        ddir = os.path.join(home, "Elysium")
        os.makedirs(ddir, exist_ok=True)
        with open(os.path.join(ddir, "data.json"), "wb") as f:
            f.write(blob)
        env = spawn(home)
        try:
            up = env.get("boot") is not None and alive(env)
            stored = http(env, "GET", "/api/store", timeout=8)
            st = stored[0]
            ok(g, "data.json %s 启动恢复" % label, up and st == 200,
               "可启动且 /api/store 200",
               "boot=%ss store=%s dead=%s" % (env.get("boot"), st, bool(env.get("dead"))))
            if not up:
                record(g, "data.json %s 可继续写入" % label, "SKIP", "200", "启动已失败")
            else:
                st2, _, _ = jpost(env, "/api/store", {"op": "settings", "payload": {"volume": 0.5}})
                ok(g, "data.json %s 可继续写入" % label, st2 == 200, "200", str(st2))
            files = os.listdir(ddir)
            backups = [x for x in files if "data.json" in x and x != "data.json"]
            if label == "UTF-8 BOM" and up:
                st3, b3, _ = http(env, "GET", "/api/store", timeout=8)
                theme = ((jbody(b3) or {}).get("settings") or {}).get("theme")
                ok(g, "BOM 文件的内容被保留", theme == "light", "theme=light，不回落默认",
                   "theme=%s" % theme)
            if need_bak and not backups:
                no_backup.append(label)
        finally:
            kill(env)
    warn(g, "损坏的 data.json 是否留备份", not no_backup,
         "被判定为损坏时另存 .bak（当前直接回落默认值，下次写入即覆盖原文件）",
         "无备份：%s" % "、".join(no_backup))

    # 只读
    base, home = mkhome("f_readonly")
    ddir = os.path.join(home, "Elysium")
    os.makedirs(ddir, exist_ok=True)
    p = os.path.join(ddir, "data.json")
    with open(p, "wb") as f:
        f.write(json.dumps({"settings": {}}).encode())
    try:
        os.chmod(p, 0o444)
    except OSError:
        pass
    env = spawn(home)
    try:
        up = env.get("boot") is not None
        alive_ok = alive(env) if up else False
        ok(g, "data.json 只读仍能启动", up and alive_ok, "可启动",
           "boot=%ss alive=%s" % (env.get("boot"), alive_ok))
    finally:
        kill(env)
        try:
            os.chmod(p, 0o666)
        except OSError:
            pass


# --------------------------------------------------------------------------
# G 纯函数
# --------------------------------------------------------------------------
def isolated(code, timeout=25):
    """在独立进程里跑一段代码；用于会病态回溯、不能在主进程里等的用例。"""
    try:
        p = subprocess.run([PY, "-c", code], capture_output=True, timeout=timeout, cwd=REPO)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return "timeout", "", ""


def group_G(env, music, outside):
    g = "G"
    setup_local(env, music, outside)
    import main
    import importer
    import sync_engine
    main.STORE.data["settings"]["download_dir"] = music
    main.STORE.data["settings"]["local_dirs"] = [outside]

    # 版本号
    ver_cases = [
        ("v1.4.2-beta", (1, 4, 2)),
        ("1.4", (1, 4)),
        ("", (0,)),
        ("v", (0,)),
        ("最新版", (0,)),
        ("v1.4.2.3", (1, 4, 2, 3)),
        ("v01.004.2", (1, 4, 2)),
        ("1.4.2-rc1", (1, 4, 2)),
    ]
    bad = []
    for raw, want in ver_cases:
        try:
            got = main._ver_tuple(raw)
        except Exception as e:                              # noqa: BLE001
            got = "EXC:%s" % e
        if got != want:
            bad.append("%r -> %s (want %s)" % (raw, got, want))
    ok(g, "版本号解析", not bad, "数字段元组", bad or "8 例符合")

    try:
        got = main._ver_tuple("9" * 5000)
        ok(g, "5000 位版本号不抛异常", isinstance(got, tuple), "tuple", str(got)[:40])
    except Exception as e:                                  # noqa: BLE001
        warn(g, "5000 位版本号不抛异常", False, "tuple（仅影响更新检查）", "EXC %s" % e)

    # safe_name / 模板
    name_cases = [
        ("../../evil", "../_evil 不允许含分隔符"),
        ("..", "unknown"),
        ("...", "unknown"),
        ("  .hidden  ", "hidden"),
        ("a" * 500, "≤150 字符"),
        ("歌/曲\\名:test*?\"<>|", "分隔符替换"),
        ("tab\tnew\nline", "控制字符替换"),
        ("emoji 🎵", "保留"),
        ("CON", "Windows 设备名（当前未过滤）"),
    ]
    for raw, label in name_cases:
        got = main.safe_name(raw)
        if label.startswith("../"):
            good = not re.search(r'[\\/]', got)      # 无分隔符即不可穿越，中间 .. 无害
        elif label == "unknown":
            good = got == "unknown"
        elif label.startswith("≤"):
            good = len(got) <= 150
        elif label.startswith("分隔符"):
            good = not re.search(r'[\\/:*?"<>|]', got)
        elif label.startswith("控制"):
            good = "\t" not in got and "\n" not in got
        elif label == "保留":
            good = "🎵" in got
        elif label.startswith("Windows 设备名"):
            good = got.upper() not in ("CON", "PRN", "AUX", "NUL")   # 未过滤 → WARN
            label = "Windows 保留名（当前未过滤，写盘会失败）"
        elif label.startswith("hidden"):
            good = not got.startswith(".")
        else:
            good = bool(got)
        warn(g, "文件名清洗：" + label, good, "安全文件名", repr(got)[:60])

    # 模板
    got = main.render_template("{name}/{artist}", {"name": "../../x", "artist": "y"})
    warn(g, "模板中的路径分隔符被清洗", "/" not in got and "\\" not in got, "无分隔符", repr(got))
    got = main.render_template("{unknown}-{name}", {"name": "n"})
    ok(g, "未知占位符原样保留", "{unknown}" in got, "保留 {unknown}", repr(got))
    got = main.render_template(None, {"name": "a", "artist": "b"})
    ok(g, "空模板回落默认", got == "a - b", "a - b", repr(got))

    # local_resolve
    idx_bad = [None, "", "..", "../secret.mp3", "/etc/passwd", "C:/Windows/win.ini",
               "@99/secret.mp3", "@abc/x.mp3", "a/../../secret.mp3", "晴天.mp3%00.mp3"]
    bad = [x for x in idx_bad if main.local_resolve(x)]
    ok(g, "local_resolve 拒绝越界", not bad, "全部 None", bad or "9 例全部拒绝")
    ok(g, "local_resolve 放行合法", main.local_resolve("晴天.mp3") is not None,
       "返回绝对路径", main.local_resolve("晴天.mp3"))

    # 歌单解析
    txt_cases = [
        ("", 0, "空文本"),
        ("   \n\n\t\n", 0, "纯空白"),
        ("晴天 - 周杰伦\n七里香 - 周杰伦", 2, "两条"),
        ("晴天 - 周杰伦\r\n七里香 - 周杰伦\r\n", 2, "CRLF"),
        ("#EXTM3U\n#EXTINF:213,晴天 - 周杰伦\n/xx/晴天.mp3", 1, "m3u8"),
        ("title,artist\n晴天,周杰伦", 1, "CSV 表头"),
        ("1\n2\n3.14\n", 0, "纯数字行"),
        ("晴天\n" * 20000, 1, "2 万行同一首（去重）"),
        ("\x00\x01\x02", 1, "控制字符"),
        ("晴天 - 周杰伦 - 叶惠美", 1, "多个分隔符"),
        ("- 周杰伦", 1, "只有歌手名"),
    ]
    bad = []
    t0 = time.time()
    for text, want, label in txt_cases:
        try:
            got = len(importer.parse_text(text))
        except Exception as e:                              # noqa: BLE001
            got = "EXC:%s" % e
        if got != want:
            bad.append("%s -> %s (want %s)" % (label, got, want))
    dt = time.time() - t0
    ok(g, "歌单文本解析边界", not bad, "条数符合", bad or "11 例符合 %.2fs" % dt)

    # 单行超长：实测 20KB 1.3s / 100KB 33s —— 疑似 O(n²)，用独立进程确认 1MB 超时
    code = ("import os,sys;os.environ['ELYSIUM_HOME']=r'%s';"
            "sys.path.insert(0,r'%s');import importer;"
            "importer.parse_text('a'*(1024*1024));print('ok')"
            % (os.environ.get("ELYSIUM_HOME", ""), REPO))
    rc, out, err = isolated(code, timeout=25)
    ok(g, "1MB 单行解析不病态回溯", rc != "timeout", "2s 内返回",
       "超时 >25s（实测 20KB=1.3s、100KB=33s，O(n²)）" if rc == "timeout"
       else "rc=%s %s" % (rc, (out or err)[-80:]))

    # 链接识别
    link_cases = [
        ("", "text", "空"),
        ("晴天 - 周杰伦", "text", "纯文本"),
        ("https://music.163.com/playlist?id=123456", "link", "网易云链接"),
        ("分享歌单 https://y.qq.com/n/ryqq/playlist/123 看看吧", "link", "分享文案"),
        ("javascript:alert(1)", "text", "伪协议"),
        ("file:///C:/secret", "text", "file 协议"),
        ("https://evil.com/music.163.com/playlist?id=1", "text", "域名伪装"),
        ("http://" + "a" * 8000, "text", "8KB URL"),
    ]
    bad = []
    for raw, want, label in link_cases:
        try:
            got = importer.parse_input(raw).get("mode")
        except Exception as e:                              # noqa: BLE001
            got = "EXC:%s" % e
        if got != want:
            bad.append("%s -> %s (want %s)" % (label, got, want))
    ok(g, "导入链接识别边界", not bad, "mode 符合", bad or "8 例符合")

    # 同步打包：本地曲目不上行
    sync_engine.bind(main.STORE, main.VERSION)
    main.STORE.data["favorites"] = [
        {"kind": "song", "id": "1", "platform": "kuwo", "name": "在线", "artist": "a"},
        {"kind": "song", "id": "C:\\Music\\x.mp3", "platform": "local", "name": "本地", "artist": "a"},
        {"kind": "song", "id": "@1/y.mp3", "platform": "local", "name": "本地2", "artist": "a"},
    ]
    main.STORE.data["last_track"] = {"id": "C:\\Music\\x.mp3", "platform": "local"}
    pack = sync_engine.pack()
    blob = json.dumps(pack, ensure_ascii=False)
    ok(g, "同步包剔除本地曲目", "C:\\\\Music" not in blob and "local" not in blob,
       "无本地路径 / local 条目", "len=%d 含local=%s" % (len(blob), "local" in blob))

    # 远端脏数据 diff 不崩
    bad = []
    for remote, label in [(None, "None"), ([], "数组"), ("x", "字符串"),
                          ({"favorites": "oops"}, "favorites 字符串"),
                          ({"playlists": [{"id": 1}, None, "x"]}, "playlists 混合"),
                          ({"last_track": "x"}, "last_track 字符串")]:
        try:
            d = sync_engine.diff(remote)
            if not isinstance(d, dict):
                bad.append("%s -> %s" % (label, type(d)))
        except sync_engine.DavError:
            pass                                    # 明确的格式错误 = 可接受的拒绝
        except Exception as e:                      # noqa: BLE001
            bad.append("%s -> EXC %s" % (label, e))
    warn(g, "同步 diff 对远端脏数据健壮", not bad, "不抛非预期异常", bad or "6 例通过")

# --------------------------------------------------------------------------
# H 写盘（歌词）
# --------------------------------------------------------------------------
def group_H(env):
    g = "H"
    root = os.path.dirname(os.path.join(env["home"]))
    ddir = os.path.join(root, "lyric_out")
    os.makedirs(ddir, exist_ok=True)
    jpost(env, "/api/store", {"op": "settings", "payload": {"download_dir": ddir}})

    def find_lrc():
        hits = []
        for r, dirs, files in os.walk(root):
            for f in files:
                if f.endswith(".lrc"):
                    hits.append(os.path.join(r, f))
        return hits

    st, body, err = jpost(env, "/api/save-lyric", {"name": "../../evil", "artist": "x", "content": "hi"})
    j = jbody(body) or {}
    hits = find_lrc()
    escaped = [p for p in hits if not p.startswith(ddir)]
    ok(g, "歌词文件名不逃逸", not escaped, "全部落在下载目录",
       "st=%s 落点=%s" % (st, [os.path.relpath(p, root) for p in hits]))

    st, body, _ = jpost(env, "/api/save-lyric", {"name": "", "artist": "", "content": ""})
    j = jbody(body) or {}
    ok(g, "空歌名回落 unknown", str((j.get("data") or {}).get("path", "")).endswith("unknown.lrc"),
       "unknown.lrc", str((j.get("data") or {}).get("path"))[-40:])
    p = (j.get("data") or {}).get("path")
    if p and os.path.isfile(p):
        ok(g, "空内容写出 0 字节 .lrc", os.path.getsize(p) == 0, "0", os.path.getsize(p))

    st, body, _ = jpost(env, "/api/save-lyric", {"name": "n" * 500, "artist": "a", "content": "x"})
    j = jbody(body) or {}
    base = os.path.basename((j.get("data") or {}).get("path") or "")
    ok(g, "500 字歌名被截断", 0 < len(base) <= 160, "≤160 字节文件名", "%d 字符" % len(base))

    jpost(env, "/api/store", {"op": "settings", "payload": {"download_dir": "Z:\\nope\\dir"}})
    st, body, err = jpost(env, "/api/save-lyric", {"name": "a", "artist": "b", "content": "x"})
    j = jbody(body) or {}
    ok(g, "非法目标盘返回 ok:false", j.get("ok") is False, "ok:false",
       "st=%s ok=%s err=%s" % (st, j.get("ok"), str(j.get("error"))[:60]))
    jpost(env, "/api/store", {"op": "settings", "payload": {"download_dir": ddir}})


# --------------------------------------------------------------------------
# I K 歌作品
# --------------------------------------------------------------------------
def group_I(env):
    g = "I"
    cases = [
        ("/api/works/file?id=../../main.py", 400, "file id 穿越"),
        ("/api/works/file?id=" + "a" * 5000, 400, "file id 超长"),
        ("/api/works/file?id=", 400, "file id 空"),
        ("/api/works/file?id=abc-def", 404, "不存在但合法 id"),
    ]
    bad = []
    for path, want, label in cases:
        st, body, err = http(env, "GET", path)
        if st != want:
            bad.append("%s: want %s got %s" % (label, want, st))
    ok(g, "作品 id 边界", not bad, "400/404", bad or "4 例符合")

    st, body, _ = http(env, "POST", "/api/works/upload", b"x" * 10,
                       headers={"Content-Length": "43"})
    ok(g, "上传 43 字节被拒", (jbody(body) or {}).get("ok") is False, "ok:false",
       "st=%s %s" % (st, (jbody(body) or {}).get("error")))

    st, body, _ = http(env, "POST", "/api/works/upload", b"",
                       headers={"Content-Length": "1000"}, timeout=10)
    ok(g, "声明 1000 实发 0 被拒", (jbody(body) or {}).get("ok") is False, "ok:false",
       "st=%s %s" % (st, (jbody(body) or {}).get("error")))

    st, body, _ = http(env, "POST", "/api/works/upload", b"",
                       headers={"Content-Length": str(201 * 1024 * 1024)}, timeout=10)
    ok(g, "201MB 声明被拒", (jbody(body) or {}).get("ok") is False, "ok:false",
       "st=%s %s" % (st, str((jbody(body) or {}).get("error"))[:40]))

    st, body, _ = http(env, "POST", "/api/works/delete",
                       json.dumps({"id": "../../x"}).encode(),
                       headers={"Content-Type": "application/json"})
    ok(g, "删除作品 id 非法被拒", (jbody(body) or {}).get("ok") is False, "ok:false",
       "st=%s %s" % (st, (jbody(body) or {}).get("error")))


# --------------------------------------------------------------------------
# J 并发 / 半开连接
# --------------------------------------------------------------------------
def group_J(env):
    g = "J"
    errors = []

    def worker(n):
        for i in range(40):
            try:
                st, _, e = jpost(env, "/api/store", {"op": "settings", "payload": {"volume": (n * 40 + i) % 100 / 100.0}}, timeout=15)
                if st != 200:
                    errors.append("st=%s %s" % (st, e))
                st2, _, e2 = http(env, "GET", "/api/store", timeout=15)
                if st2 != 200:
                    errors.append("get=%s %s" % (st2, e2))
            except Exception as e:                          # noqa: BLE001
                errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    dt = time.time() - t0
    ok(g, "12 线程 × 80 次读写", not errors, "全部 200",
       "%d 错误 %s / %.1fs" % (len(errors), errors[:3], dt))

    dpath = os.path.join(env["home"], "Elysium", "data.json")
    try:
        with open(dpath, "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        ok(g, "并发后 data.json 仍可解析", isinstance(data, dict), "dict", type(data).__name__)
    except Exception as e:                                  # noqa: BLE001
        ok(g, "并发后 data.json 仍可解析", False, "dict", "EXC %s" % e)

    # 半开连接：连上不发数据，正常请求不该被拖死
    slow = []
    for _ in range(8):
        s = socket.create_connection(("127.0.0.1", env["port"]), timeout=3)
        slow.append(s)
    t0 = time.time()
    st, _, err = http(env, "GET", "/api/lyric/now", timeout=6)
    dt = time.time() - t0
    ok(g, "8 个半开连接不阻塞正常请求", st == 200 and dt < 3, "200 且 <3s",
       "st=%s %.2fs" % (st, dt))
    for s in slow:
        try:
            s.close()
        except OSError:
            pass

    # 快速连接/立即关闭
    bad = 0
    for _ in range(100):
        try:
            s = socket.create_connection(("127.0.0.1", env["port"]), timeout=2)
            s.sendall(b"GET /api/lyric/now HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         b"\x01\x00\x00\x00\x00\x00\x00\x00")
            s.close()
        except OSError:
            bad += 1
    ok(g, "100 次 RST 连接后服务存活", alive(env), "存活", "连接失败 %d 次" % bad)


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def report(envs, elapsed):
    groups = {}
    for r in RESULTS:
        groups.setdefault(r["group"], []).append(r)
    lines = ["# Elysium 边缘测试报告", "",
             "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "- 用时：%.1fs" % elapsed,
             "- Python：%s" % sys.version.split()[0],
             "- 范围：本地 HTTP API（畸形请求 / 越界 / 极限）、data.json 鲁棒性、纯函数边界；"
             "不含前端 UI 交互与真实上游接口", "", "## 汇总", "",
             "| 组 | 覆盖 | PASS | FAIL | WARN | SKIP |", "| --- | --- | --- | --- | --- | --- |"]
    total = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for g in sorted(groups):
        rs = groups[g]
        cnt = {k: sum(1 for r in rs if r["verdict"] == k) for k in total}
        for k in total:
            total[k] += cnt[k]
        lines.append("| %s | %d | %d | %d | %d | %d |" % (g, len(rs), cnt["PASS"], cnt["FAIL"], cnt["WARN"], cnt["SKIP"]))
    lines.append("| **合计** | **%d** | **%d** | **%d** | **%d** | **%d** |"
                 % (len(RESULTS), total["PASS"], total["FAIL"], total["WARN"], total["SKIP"]))
    lines += ["", "**结论**：%d 项符合预期，%d 项存在真实缺陷，%d 项可改进但非致命。"
              % (total["PASS"], total["FAIL"], total["WARN"])]
    for g in sorted(groups):
        lines += ["", "## %s 组" % g, "", "| 结果 | 用例 | 期望 | 实际 |", "| --- | --- | --- | --- |"]
        for r in groups[g]:
            lines.append("| %s | %s | %s | %s |" % (r["verdict"], r["name"],
                                                    str(r["expect"]).replace("|", "\\|"),
                                                    str(r["actual"]).replace("|", "\\|")[:300]))
    lines += ["", "## 服务端未捕获异常", ""]
    all_tb = {}
    for env in envs:
        if not env:
            continue
        for k, v in tracebacks(log_text(env)).items():
            all_tb[k] = all_tb.get(k, 0) + v
    if all_tb:
        for k, v in sorted(all_tb.items(), key=lambda x: -x[1]):
            lines.append("- `%s` × %d" % (k, v))
    else:
        lines.append("无")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
def main():
    global TMP_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}

    def want(g):
        return not only or g in only

    TMP_ROOT = tempfile.mkdtemp(prefix="elysium_edge_")
    t_start = time.time()
    envs = []
    base, home = mkhome("main")
    music = os.path.join(base, "music")
    outside = os.path.join(base, "outside")
    make_music(music, outside)
    env = spawn(home)
    envs.append(env)
    if env.get("dead"):
        print("[FATAL] 服务没起来：\n" + env["dead"])
        return 2
    print("[env] port=%s boot=%ss tmp=%s" % (env["port"], env["boot"], TMP_ROOT), flush=True)

    try:
        if want("A"):
            group_A(env)
        if want("B"):
            group_B(env, music, outside)
        if want("C"):
            group_C(env, music, outside)
        if want("D"):
            group_D(env)
        if want("E"):
            group_E(env)
        if want("H"):
            group_H(env)
        if want("I"):
            group_I(env)
        if want("J"):
            group_J(env)
        if want("G"):
            # 纯函数用独立 STORE，避免把上面造出来的脏数据带进断言
            base2, home2 = mkhome("func")
            os.environ["ELYSIUM_HOME"] = home2
            prev = os.getcwd()
            os.chdir(REPO)
            try:
                group_G(env, music, outside)
            finally:
                os.chdir(prev)
    finally:
        kill(env)

    if want("F"):
        group_F()

    elapsed = time.time() - t_start
    rep = report(envs, elapsed)
    out = os.path.join(REPO, "edge_report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(rep)
    print("\n" + rep)
    print("[report] " + out)
    if not args.keep:
        # 刚 kill 的服务进程在 Windows 上可能还短暂占着日志 / 缓存文件，
        # 一次删不掉就重试几下，别把临时目录留下来。
        for _ in range(6):
            shutil.rmtree(TMP_ROOT, ignore_errors=True)
            if not os.path.exists(TMP_ROOT):
                break
            time.sleep(0.4)
        if os.path.exists(TMP_ROOT):
            print("[warn] 临时目录没删干净：%s" % TMP_ROOT)
    else:
        print("[keep] " + TMP_ROOT)
    fails = sum(1 for r in RESULTS if r["verdict"] == "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
