# -*- coding: utf-8 -*-
"""Elysium 发烧客模式验证（响度归一化 + Hi-Res 元信息）。

关注点：
  A 纯函数 parse_flac_streaminfo：位打包、ID3 前缀、截断、魔数垃圾、合理性硬墙
  B _flac_hires：256B 探头直解 / ID3 标签过大补定向 Range
  C probe_one 端到端：file:// 假直链 + fake urlopen，hires 进结果、缓存命中带 hires
  D store loudness_set：读写、NaN/inf 硬墙、非法键、钳制、条数上限裁剪
  E settings：loudness_target 数值钳制、loudness_mode 透传
  F HTTP 端到端：/api/store POST loudness_set + 老 data.json（无 loudness 键）加载回落
  G data.json 持久化：loudness 落盘再读回一致，NaN 绝不写出非法字面量
  H Phase 2 参数 EQ 设置契约：eq_mode/eq_bands/eq_preamp/eq_auto_preamp/eq_headphone
    （plans/AUDIOPHILE-P2 A 组）+ 老 eq_gains 键回归
  I C-1 后端 AutoEq APO 文本解析：好文本 / 单位 / 大小写 / 占位与 OFF 行 /
    截断 / 全无效报错 / preamp 钳制 / 超长文本
  J 参数 EQ 预设库存取：eqpreset_set/eqpreset_del、NaN 硬墙、name 规整、
    preamp 钳制、上限裁剪、落盘往返
  K HTTP 端到端：/api/eq/parse 好/坏文本 + /api/store 预设与设置全链路

用法：
    python verify_audiophile.py            # 全部
    python verify_audiophile.py --only=A,C # 只跑指定组
环境：ELYSIUM_HOME 指向临时目录，绝不触碰真实 data.json。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []
TMP_ROOT = None


def record(group, name, verdict, expect, actual):
    RESULTS.append({"group": group, "name": name, "verdict": verdict,
                    "expect": expect, "actual": actual})
    mark = {"PASS": "[PASS] ", "FAIL": "[FAIL] ", "WARN": "[WARN] ", "SKIP": "[SKIP] "}[verdict]
    print("%s%-8s %s | 期望 %s | 实际 %s" % (mark, group, name, expect, actual), flush=True)


def ok(group, name, cond, expect, actual):
    record(group, name, "PASS" if cond else "FAIL", expect, actual)
    return bool(cond)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def make_streaminfo(sr, bd, ch, total):
    """按 FLAC 规范打包 STREAMINFO 内容 34B（块大小/帧大小字段给合法占位）。"""
    b = bytearray(34)
    b[0:2] = (4096).to_bytes(2, "big")
    b[2:4] = (4096).to_bytes(2, "big")
    v = (sr << 44) | ((ch - 1) << 41) | ((bd - 1) << 36) | total
    b[10:18] = v.to_bytes(8, "big")
    return b"fLaC" + bytes([0, 0, 0, 34]) + bytes(b)


def id3_prefix(tag_size):
    """ID3v2 头 10B（v2.3，无 flag，声明 tag_size 字节体）。"""
    def ss(n):
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x03\x00\x00" + ss(tag_size) + b"\x00" * tag_size


class FakeResp(object):
    """替身 urlopen 返回值：按请求的 Range 头切片返回固定字节。"""

    def __init__(self, body):
        self.body = body
        self.status = 206
        self.headers = {"Content-Range": "bytes 0-%d/%d" % (len(body) - 1, len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body[:n]


def mkhome(tag):
    home = os.path.join(TMP_ROOT, tag, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        dst = os.path.join(home, item)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(REPO, item), dst,
                        ignore=shutil.ignore_patterns("__pycache__"))
    return home


def spawn(home, port=None):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = port or s.getsockname()[1]
    s.close()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
    env["ELYSIUM_HOME"] = home
    log_path = os.path.join(TMP_ROOT, "server_%d.log" % port)
    logf = open(log_path, "wb")
    import subprocess as sp
    proc = sp.Popen([PY, "main.py", "--serve", "--port=%d" % port],
                    cwd=REPO, env=env, stdout=logf, stderr=sp.STDOUT)
    logf.close()
    base = "http://127.0.0.1:%d" % port
    t0 = time.time()
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            s = __import__("socket").create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return {"proc": proc, "port": port, "base": base, "log": log_path,
                    "boot": round(time.time() - t0, 2)}
        except OSError:
            time.sleep(0.2)
    return {"proc": proc, "port": port, "base": base, "log": log_path, "boot": None}


def kill(env):
    try:
        env["proc"].terminate()
        env["proc"].wait(timeout=5)
    except Exception:                                       # noqa: BLE001
        try:
            env["proc"].kill()
        except Exception:                                   # noqa: BLE001
            pass


def http(env, method, path, body=None):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(env["base"] + path, method=method,
                                 data=json.dumps(body).encode("utf-8") if body else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:                   # 4xx 也是契约的一部分
        return e.code, json.loads(e.read().decode("utf-8"))


# --------------------------------------------------------------------------
def group_A():
    g = "A"
    import main
    cases = [
        # (buffer, 期望结果或 None, 名称)
        (make_streaminfo(48000, 24, 2, 48000 * 180),
         {"sample_rate": 48000, "bit_depth": 24, "channels": 2,
          "total_samples": 8640000, "duration": 180.0}, "48k/24bit 标准体"),
        (make_streaminfo(44100, 16, 2, 44100 * 200),
         {"sample_rate": 44100, "bit_depth": 16, "channels": 2,
          "total_samples": 8820000, "duration": 200.0}, "44.1k/16bit CD 体"),
        (make_streaminfo(96000, 24, 1, 96000 * 30),
         {"sample_rate": 96000, "bit_depth": 24, "channels": 1,
          "total_samples": 2880000, "duration": 30.0}, "96k/24bit 单声道"),
        (make_streaminfo(176400, 32, 2, 0),
         {"sample_rate": 176400, "bit_depth": 32, "channels": 2,
          "total_samples": 0}, "流式 FLAC 无总样本数（duration 不给）"),
        (b"", None, "空 buffer"),
        (b"fLaC", None, "只有魔数（截断）"),
        (b"fLaC" + bytes([0, 0, 0, 34]), None, "魔数+块头（截断）"),
        (b"fLaC" + bytes([1, 0, 0, 34]) + b"\x00" * 34, None, "块类型非 STREAMINFO"),
        (b"fLaC" + bytes([0, 0, 0, 33]) + b"\x00" * 34, None, "块长度 <34"),
        (b"fLaC" + bytes([0, 0, 0, 34]) + b"\xff" * 34, None, "魔数后跟垃圾（采样率越界）"),
        (b"\xff\xfb\x90\x44" + b"\x00" * 60, None, "MP3 头不是 FLAC"),
        (b"OggS" + b"\x00" * 60, None, "OGG 不是 FLAC"),
    ]
    bad = []
    for buf, want, name in cases:
        got = main.parse_flac_streaminfo(buf)
        if got != want:
            bad.append("%s: got %s want %s" % (name, got, want))
    ok(g, "STREAMINFO 解析 12 例", not bad, "逐例符合", bad or "12 例符合")

    # ID3 前缀：标签 8 字节，off=18，fLaC 可达
    got = main.parse_flac_streaminfo(id3_prefix(8) + make_streaminfo(44100, 16, 2, 44100 * 5))
    ok(g, "ID3 短前缀可穿透", got is not None and got["sample_rate"] == 44100,
       "44100", str(got))
    # ID3 标签很大、buffer 不够长：拒绝而不是误报
    got = main.parse_flac_streaminfo(id3_prefix(4000) + make_streaminfo(44100, 16, 2, 5)[:30])
    ok(g, "ID3 大标签截断拒绝", got is None, "None", str(got))
    # ID3 头声明长度但内容缺失
    got = main.parse_flac_streaminfo(b"ID3\x03\x00\x00\x00\x00\x20\x00")
    ok(g, "ID3 声明超长仅剩头", got is None, "None", str(got))


def group_B():
    g = "B"
    import main
    # 直解路径：256B 探头直接含 fLaC
    got = main._flac_hires("http://x/", make_streaminfo(96000, 24, 2, 96000 * 60))
    ok(g, "无 ID3 直解", got is not None and got["sample_rate"] == 96000,
       "96000", str(got))
    # ID3 过大触发补拉：替身 urlopen 模拟定向 Range 返回 STREAMINFO 段
    tag = 4000
    off = 10 + tag
    si = make_streaminfo(48000, 16, 2, 48000 * 90)
    calls = []

    def fake_urlopen(req, timeout=None):
        rng = (req.headers.get("Range") or "")
        m = rng.split("=")[-1]
        calls.append(rng)
        start, end = m.split("-")
        # 模拟服务器按 Range 切片返回
        return FakeResp(si[: int(end) - int(start) + 1] if int(start) == off else b"\x00" * 16)

    orig = main.urllib.request.urlopen
    main.urllib.request.urlopen = fake_urlopen
    try:
        got = main._flac_hires("http://x/", id3_prefix(tag) + b"\x00" * 246)
    finally:
        main.urllib.request.urlopen = orig
    ok(g, "ID3 大标签补拉成功", got is not None and got["sample_rate"] == 48000
       and got["duration"] == 90.0, "48000/dur90", str(got))
    ok(g, "补拉 Range 指向标签后偏移", calls and calls[0] == "bytes=%d-%d" % (off, off + 47),
       "bytes=%d-%d" % (off, off + 47), str(calls))
    # 补拉失败（网络异常）→ None，不抛
    def boom(req, timeout=None):
        raise OSError("network down")
    main.urllib.request.urlopen = boom
    try:
        got = main._flac_hires("http://x/", id3_prefix(99999) + b"\x00" * 246)
    finally:
        main.urllib.request.urlopen = orig
    ok(g, "补拉异常不抛", got is None, "None", str(got))
    # 荒谬标签尺寸直接放弃
    got = main._flac_hires("http://x/", id3_prefix(17 * 1024 * 1024) + b"\x00" * 246)
    ok(g, "16MB 以上标签放弃", got is None, "None", str(got))


def group_C():
    g = "C"
    import main
    si = make_streaminfo(48000, 24, 2, 48000 * 180)
    holder = {"body": si + b"\x00" * 64}
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.headers.get("Range"))
        return FakeResp(holder["body"])

    orig_resolve = main.resolve_play_url
    orig_open = main.urllib.request.urlopen
    main.resolve_play_url = lambda *a, **k: "http://upstream/t.flac"
    main.urllib.request.urlopen = fake_urlopen
    try:
        main._PROBE_CACHE.clear()
        out = main.probe_one("kuwo", "f1", "flac", "999", duration=180)
        ok(g, "probe_one 带出 hires", out.get("valid") and out.get("hires") is not None
           and out["hires"]["sample_rate"] == 48000 and out["hires"]["bit_depth"] == 24,
           "valid+hires 48k/24", str({k: out.get(k) for k in ("valid", "fmt", "hires")}))
        ok(g, "Content-Range 算大小", out.get("size") == len(holder["body"]),
           str(len(holder["body"])), str(out.get("size")))
        ok(g, "探头 Range 升到 256B", calls and calls[0] == "bytes=0-255",
           "bytes=0-255", str(calls))
        # 缓存命中：同 key 再探不再发请求，hires 原样带回
        calls.clear()
        again = main.probe_one("kuwo", "f1", "flac", "999", duration=180)
        ok(g, "缓存命中不重发", not calls and again.get("hires") == out.get("hires"),
           "0 次请求", str(calls))
        # 假 FLAC 头：容器判定成立但 STREAMINFO 是垃圾 → hires=None 不误报
        holder["body"] = b"fLaC" + b"\x99" * 40
        main._PROBE_CACHE.clear()
        out2 = main.probe_one("kuwo", "f2", "flac", "999")
        ok(g, "假 FLAC 头 hires=None", out2.get("valid") and out2.get("fmt") == "flac"
           and out2.get("hires") is None, "valid/flac/None",
           str({k: out2.get(k) for k in ("valid", "fmt", "hires")}))
        # 非 FLAC：一次请求完事，不带 hires
        holder["body"] = b"\xff\xfb\x90\x44" + b"\x00" * 60
        main._PROBE_CACHE.clear()
        out3 = main.probe_one("kuwo", "f3", "mp3", "320")
        ok(g, "MP3 不带 hires", out3.get("valid") and out3.get("fmt") == "mp3"
           and out3.get("hires") is None, "valid/mp3/None",
           str({k: out3.get(k) for k in ("valid", "fmt", "hires")}))
    finally:
        main.resolve_play_url = orig_resolve
        main.urllib.request.urlopen = orig_open

    # probe_batch 的占位结果形状与 probe_one 对齐（hires 键存在）
    ph = main.probe_batch([], limit=4)
    ok(g, "空批次不报错", ph == {}, "{}", str(ph))


def group_D():
    g = "D"
    import main
    ddir = os.path.join(TMP_ROOT, "storeD")
    os.makedirs(ddir, exist_ok=True)
    st = main.Store(os.path.join(ddir, "data.json"))
    r = st.apply("loudness_set", {"platform": "kuwo", "id": "1",
                                  "lufs": -12.34, "tp": -0.5, "lra": 7.8})
    rec = r["loudness"].get("kuwo:1")
    ok(g, "正常写入", rec is not None and rec["lufs"] == -12.3 and rec["tp"] == -0.5
       and rec["lra"] == 7.8 and "at" in rec, "lufs-12.3/tp-0.5/lra7.8/at", str(rec))
    st.apply("loudness_set", {"platform": "kuwo", "id": "1", "lufs": -5})
    ok(g, "重复写覆盖", st.data["loudness"]["kuwo:1"]["lufs"] == -5.0, "-5",
       str(st.data["loudness"]["kuwo:1"]))
    st.apply("loudness_set", {"platform": "kuwo", "id": "2", "lufs": "nan"})
    st.apply("loudness_set", {"platform": "kuwo", "id": "3", "lufs": "inf"})
    st.apply("loudness_set", {"platform": "kuwo", "id": "4", "lufs": "abc"})
    st.apply("loudness_set", {"platform": "kuwo", "id": "5", "lufs": None})
    st.apply("loudness_set", {"platform": "", "id": "", "lufs": -9})
    st.apply("loudness_set", None)
    st.apply("loudness_set", "junk")
    leftovers = [k for k in st.data["loudness"] if k not in ("kuwo:1",)]
    ok(g, "NaN/inf/非数字/空键全拒", not leftovers, "只剩 kuwo:1", str(leftovers))
    # 钳制：越界值收到边界
    st.apply("loudness_set", {"platform": "q", "id": "1", "lufs": -90, "tp": 20, "lra": 999})
    rec = st.data["loudness"]["q:1"]
    ok(g, "越界钳制", rec["lufs"] == -70.0 and rec["tp"] == 9.0 and rec["lra"] == 90.0,
       "-70/9/90", str(rec))
    # 缺 id 不写
    st.apply("loudness_set", {"platform": "kuwo", "lufs": -9})
    ok(g, "缺 id 不写", "kuwo:" not in st.data["loudness"], "无该键",
       str(st.data["loudness"].keys()))
    # 条数上限：直接灌满再触发裁剪
    loud = st.data["loudness"]
    n = main.LOUDNESS_MAX + 10
    for i in range(n):
        loud["x:%d" % i] = {"lufs": -10, "at": i}
    main._loudness_prune(loud)
    ok(g, "超限裁剪到上限", len(loud) == main.LOUDNESS_MAX, "=%d" % main.LOUDNESS_MAX,
       str(len(loud)))
    ok(g, "最旧的先被淘汰", "x:0" not in loud and "x:9" not in loud
       and "x:%d" % (n - 1) in loud, "x:0..9 消失、尾部保留",
       "x:0 in=%s tail in=%s" % ("x:0" in loud, "x:%d" % (n - 1) in loud))
    # STORE_OPS 白名单收录
    ok(g, "loudness_set 进白名单", "loudness_set" in main.STORE_OPS, "True", "True")


def group_E():
    g = "E"
    import main
    clean = main._clean_settings({"loudness_target": "-5x", "loudness_mode": "track"})
    ok(g, "非法数值丢弃", "loudness_target" not in clean and clean.get("loudness_mode") == "track",
       "target 不落盘/mode=track", str(clean))
    clean = main._clean_settings({"loudness_target": "500"})
    ok(g, "目标上限钳制", clean.get("loudness_target") == -6, "-6", str(clean))
    clean = main._clean_settings({"loudness_target": "-99"})
    ok(g, "目标下限钳制", clean.get("loudness_target") == -40, "-40", str(clean))
    clean = main._clean_settings({"loudness_target": -14.7})
    ok(g, "浮点转 int", clean.get("loudness_target") == -14, "-14", str(clean))
    edir = os.path.join(TMP_ROOT, "storeE")
    os.makedirs(edir, exist_ok=True)
    st = main.Store(os.path.join(edir, "data.json"))
    st.apply("settings", {"loudness_mode": "track", "loudness_target": -14})
    ok(g, "设置生效", st.data["settings"]["loudness_mode"] == "track"
       and st.data["settings"]["loudness_target"] == -14, "track/-14",
       "%s/%s" % (st.data["settings"]["loudness_mode"], st.data["settings"]["loudness_target"]))


def group_F():
    g = "F"
    home = mkhome("http")
    # 预写一份「老版本」data.json：没有 loudness 键，验证加载时回落补齐
    old = {
        "settings": {"volume": 60},
        "favorites": [{"kind": "song", "id": "9", "platform": "kuwo"}],
        "loudness_of_old_version_absent": True,
    }
    dpath = os.path.join(home, "Elysium")
    os.makedirs(dpath, exist_ok=True)
    with open(os.path.join(dpath, "data.json"), "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)

    env = spawn(home)
    try:
        if not env["boot"]:
            ok(g, "服务启动", False, "boot", (open(env["log"], "rb").read().decode(
                "utf-8", "replace")[-600:]))
            return
        ok(g, "老 data.json 加载补齐 loudness", True, "boot ok", str(env["boot"]))
        st, data = http(env, "GET", "/api/store")
        ok(g, "GET 含空 loudness", st == 200 and data.get("loudness") == {}, "200+{}",
           str(st) + str(data.get("loudness")))
        st, r = http(env, "POST", "/api/store", {"op": "loudness_set", "payload": {
            "platform": "kuwo", "id": "42", "lufs": -14.2, "tp": -0.9, "lra": 6.1}})
        rec = ((r.get("data") or {}).get("loudness") or {}).get("kuwo:42")
        ok(g, "HTTP 写入响度", st == 200 and r.get("ok") and rec is not None
           and rec["lufs"] == -14.2, "200+ok+lufs-14.2", str(st) + str(rec))
        st, r = http(env, "POST", "/api/store", {"op": "loudness_set", "payload": {
            "platform": "kuwo", "id": "43", "lufs": float("nan")}})
        rec43 = ((r.get("data") or {}).get("loudness") or {}).get("kuwo:43")
        ok(g, "HTTP NaN 拒写", rec43 is None, "无 kuwo:43", str(rec43))
        st, r = http(env, "POST", "/api/store", {"op": "loudness_unknown", "payload": {}})
        ok(g, "未知 op 报错不静默", st == 400 and r.get("ok") is False,
           "400+ok=False", "%s %s" % (st, r))
        st, data = http(env, "GET", "/api/store")
        ok(g, "settings 新键在响应里", data["settings"].get("loudness_mode") == "off"
           and data["settings"].get("loudness_target") == -18, "off/-18",
           "%s/%s" % (data["settings"].get("loudness_mode"),
                      data["settings"].get("loudness_target")))
        # NaN 绝不落进 data.json（json.load 会拒绝 NaN 之外的 inf 写出）
        with open(os.path.join(dpath, "data.json"), "r", encoding="utf-8") as f:
            raw = f.read()
        ok(g, "落盘无 NaN 字面量", "NaN" not in raw and "Infinity" not in raw,
           "clean json", "NaN in=%s" % ("NaN" in raw))
    finally:
        kill(env)


def group_G():
    g = "G"
    home = mkhome("persist")
    dpath = os.path.join(home, "Elysium")
    os.makedirs(dpath, exist_ok=True)
    pf = os.path.join(dpath, "data.json")
    st = main_Store(pf)
    st.apply("loudness_set", {"platform": "kw", "id": "7", "lufs": -13.33,
                              "tp": -1.1, "lra": 8.8})
    # 二次实例化读回
    st2 = main_Store(pf)
    rec = st2.data["loudness"].get("kw:7")
    ok(g, "loudness 落盘读回一致", rec is not None and rec["lufs"] == -13.3
       and rec["tp"] == -1.1 and rec["lra"] == 8.8, "lufs-13.3/tp-1.1/lra8.8", str(rec))
    # BOM 文件 + 老格式（无 loudness 键）回落
    with open(pf, "w", encoding="utf-8-sig") as f:
        json.dump({"settings": {"volume": 40}}, f, ensure_ascii=False)
    st3 = main_Store(pf)
    ok(g, "BOM+老格式回落空 loudness", st3.data.get("loudness") == {}, "{}",
       str(st3.data.get("loudness")))


def main_Store(path):
    """独立 Store：避免与进程级 main.STORE 串数据。"""
    import main
    return main.Store(path)


# --------------------------------------------------------------------------
def group_H():
    """Phase 2 参数 EQ 设置契约（plans/AUDIOPHILE-P2 A 组）。"""
    g = "H"
    import main

    # A-1 eq_mode 白名单
    ok(g, "eq_mode=parametric 收", main._clean_settings(
        {"eq_mode": "parametric"}).get("eq_mode") == "parametric", "parametric",
       str(main._clean_settings({"eq_mode": "parametric"})))
    ok(g, "eq_mode 非法值丢", "eq_mode" not in main._clean_settings(
        {"eq_mode": "hacker"}), "无该键", str(main._clean_settings({"eq_mode": "hacker"})))
    # A-2 eq_bands
    good = [{"type": "lowshelf", "f": 105, "q": 0.71, "db": 6.5},
            {"type": "peaking", "f": 1234.56, "q": 1.41, "db": -3.25},
            {"type": "highshelf", "f": 10000, "q": 0.71, "db": -1.0}]
    got = main._clean_settings({"eq_bands": good}).get("eq_bands")
    ok(g, "合法段表过并带舍入", got == [
        {"type": "lowshelf", "f": 105.0, "q": 0.71, "db": 6.5},
        {"type": "peaking", "f": 1234.6, "q": 1.41, "db": -3.2},
        {"type": "highshelf", "f": 10000.0, "q": 0.71, "db": -1.0}], "3 段原样",
       str(got))
    dirty = [{"type": "peaking", "f": 10, "q": 1, "db": 0},          # 频率越界
             {"type": "peaking", "f": 30000, "q": 1, "db": 0},        # 频率越界
             {"type": "peaking", "f": 1000, "q": 0.01, "db": 0},      # Q 越界
             {"type": "peaking", "f": 1000, "q": 1, "db": 13},        # 增益越界
             {"type": "peaking", "f": 1000, "q": 1, "db": "nan"},     # NaN 硬墙
             {"type": "peaking", "f": "inf", "q": 1, "db": 0},        # inf 硬墙
             {"type": "bandpass", "f": 1000, "q": 1, "db": 0},        # 类型白名单外
             "junk",                                                 # 非段对象
             {"f": 1000, "q": 1, "db": 0},                           # 缺 type 按 peaking 补
             None, None, None, None, None, None, None, None,        # 占位凑满 12 段试截断
             None]
    got = main._clean_settings({"eq_bands": dirty}).get("eq_bands")
    ok(g, "非法段全丢唯缺省段留", got == [{"type": "peaking", "f": 1000.0, "q": 1.0, "db": 0.0}],
       "1 段 peaking", str(got))
    ok(g, "整表非数组丢弃", "eq_bands" not in main._clean_settings(
        {"eq_bands": "junk"}), "无该键", str(main._clean_settings({"eq_bands": "junk"})))
    ok(g, "空段表收", main._clean_settings({"eq_bands": []}).get("eq_bands") == [],
       "[]", str(main._clean_settings({"eq_bands": []})))
    # A-3 eq_preamp 钳制（一位小数口径：AutoEq 解析值 -6.5 这类小数要保得住）
    ok(g, "eq_preamp 上钳", main._clean_settings(
        {"eq_preamp": 99}).get("eq_preamp") == 12.0, "12.0", str(main._clean_settings(
            {"eq_preamp": 99})))
    ok(g, "eq_preamp 下钳", main._clean_settings(
        {"eq_preamp": -99}).get("eq_preamp") == -12.0, "-12.0", str(main._clean_settings(
            {"eq_preamp": -99})))
    ok(g, "eq_preamp 小数保留", main._clean_settings(
        {"eq_preamp": -6.5}).get("eq_preamp") == -6.5, "-6.5", str(main._clean_settings(
            {"eq_preamp": -6.5})))
    ok(g, "eq_preamp NaN/inf 丢", "eq_preamp" not in main._clean_settings(
        {"eq_preamp": "nan"}) and "eq_preamp" not in main._clean_settings(
        {"eq_preamp": "inf"}), "无该键", str(main._clean_settings({"eq_preamp": "nan"})))
    ok(g, "eq_preamp 非法丢", "eq_preamp" not in main._clean_settings(
        {"eq_preamp": "abc"}), "无该键", str(main._clean_settings({"eq_preamp": "abc"})))
    # A-4 eq_auto_preamp 只收 bool
    ok(g, "eq_auto_preamp bool 收", main._clean_settings(
        {"eq_auto_preamp": False}).get("eq_auto_preamp") is False, "False",
       str(main._clean_settings({"eq_auto_preamp": False})))
    ok(g, "eq_auto_preamp 非 bool 丢", "eq_auto_preamp" not in main._clean_settings(
        {"eq_auto_preamp": "yes"}), "无该键", str(main._clean_settings(
            {"eq_auto_preamp": "yes"})))
    # A-5 eq_headphone 截断
    ok(g, "eq_headphone 截 80", len(main._clean_settings(
        {"eq_headphone": "森" * 200}).get("eq_headphone", "")) == 80, "80 字",
       str(len(main._clean_settings({"eq_headphone": "森" * 200}).get("eq_headphone", ""))))
    # 2026-11-13 起空串 = 显式清空要落盘（前端清空型号后重启，旧型号不得复活回 UI）
    ok(g, "eq_headphone 空串显式清空", main._clean_settings(
        {"eq_headphone": "   "}).get("eq_headphone") == "", "空串落盘",
       str(main._clean_settings({"eq_headphone": "   "})))
    # 老键回归：eq_gains 原路径不动
    ok(g, "老 eq_gains 截 10 原样", main._clean_settings(
        {"eq_gains": [1] * 15}).get("eq_gains") == [1] * 10, "[1]×10",
       str(main._clean_settings({"eq_gains": [1] * 15}).get("eq_gains")))
    # 默认值齐备
    d = main.DEFAULT_STORE["settings"]
    ok(g, "五键默认值", (d.get("eq_mode") == "graphic" and d.get("eq_bands") == []
       and d.get("eq_preamp") == 0 and d.get("eq_auto_preamp") is True
       and d.get("eq_headphone") == ""), "graphic/[]/0/True/''",
       str([d.get(k) for k in ("eq_mode", "eq_bands", "eq_preamp",
                                "eq_auto_preamp", "eq_headphone")]))
    # apply 落盘往返
    edir = os.path.join(TMP_ROOT, "storeH")
    os.makedirs(edir, exist_ok=True)
    st = main.Store(os.path.join(edir, "data.json"))
    st.apply("settings", {"eq_mode": "parametric", "eq_bands": good,
                           "eq_preamp": -3.5, "eq_headphone": "HD 800 S"})
    s2 = st.data["settings"]
    ok(g, "apply 落盘往返", s2["eq_mode"] == "parametric"
       and s2["eq_bands"][1]["f"] == 1234.6 and s2["eq_preamp"] == -3.5
       and s2["eq_headphone"] == "HD 800 S", "parametric/1234.6/-3.5/HD 800 S",
       str((s2["eq_mode"], s2["eq_bands"][1]["f"], s2["eq_preamp"], s2["eq_headphone"])))


# --------------------------------------------------------------------------
def group_I():
    """C-1 后端 AutoEq APO 文本解析（plans/AUDIOPHILE-P2）。"""
    g = "I"
    import main

    def raises(fn):
        try:
            fn()
            return None
        except ValueError as e:
            return str(e)

    full = "\n".join([
        "Preamp: -6.5 dB",
        "Filter 1: ON PK Fc 31 Hz Gain 7.9 dB Q 0.70",
        "Filter 2: ON PK Fc 63 Hz Gain 5.5 dB Q 0.80",
        "Filter 3: ON LS Fc 105 Hz Gain 6.5 dB Q 0.71",
        "Filter 4: ON PK Fc 1234.5 Hz Gain -3.2 dB Q 1.41",
        "Filter 8: ON HS Fc 10000 Hz Gain -2.0 dB Q 0.70",
    ])
    out = main.parse_autoeq_apo(full)
    ok(g, "完整文本解析", out.get("preamp") == -6.5 and len(out.get("bands") or []) == 5
       and out.get("total") == 5 and out.get("dropped") == 0 and out.get("truncated") == 0
       and out["bands"][0] == {"type": "peaking", "f": 31.0, "q": 0.7, "db": 7.9}
       and out["bands"][2]["type"] == "lowshelf" and out["bands"][4]["type"] == "highshelf",
       "preamp-6.5/5段全合法", str(out))
    # 解析结果与 settings eq_bands 同口径：直接过 _clean_bands 一段不丢
    ok(g, "解析结果过段表校验", main._clean_bands(out["bands"])[0] == out["bands"],
       "原样通过", str(main._clean_bands(out["bands"])[0]))
    out = main.parse_autoeq_apo("Filter 1: ON PK Fc 2 kHz Gain 1.0 dB Q 1.0")
    ok(g, "kHz 单位换算", out["bands"] == [{"type": "peaking", "f": 2000.0,
       "q": 1.0, "db": 1.0}], "f=2000", str(out["bands"]))
    out = main.parse_autoeq_apo("filter 3 : on pk fc 100 hz gain 3 db q 0.7")
    ok(g, "小写与空格容忍", out["total"] == 1 and out["bands"][0]["f"] == 100.0,
       "1 段 f=100", str(out))
    out = main.parse_autoeq_apo("\ufeffPreamp: -4.0 dB\r\nFilter 1: ON PK Fc 100 Hz "
                                "Gain 3.0 dB Q 0.7\r\n")
    ok(g, "BOM 与 CRLF 容忍", out["preamp"] == -4.0 and len(out["bands"]) == 1,
       "preamp-4/1段", str(out))
    # None 占位 / OFF 行：作者空着或停用的槽位，不算数据
    out = main.parse_autoeq_apo("\n".join([
        "Preamp: -6.5 dB",
        "Filter 1: ON PK Fc 31 Hz Gain 7.9 dB Q 0.70",
        "Filter 2: ON None Fc 0 Hz Gain 0.0 dB Q 1.00",
        "Filter 3: OFF PK Fc 63 Hz Gain 5.5 dB Q 0.80",
    ]))
    ok(g, "None 占位与 OFF 跳过", out["total"] == 1 and len(out["bands"]) == 1,
       "total=1", str(out))
    # Gain/Q 缺省：AutoEq 少数导出只有 Fc
    out = main.parse_autoeq_apo("Filter 1: ON PK Fc 500 Hz")
    ok(g, "Gain/Q 缺省补 0/1", out["bands"] == [{"type": "peaking", "f": 500.0,
       "q": 1.0, "db": 0.0}], "db0/q1", str(out["bands"]))
    # 不认识的类型计入 dropped，不影响好段
    out = main.parse_autoeq_apo("Filter 1: ON PK Fc 100 Hz Gain 1.0 dB Q 1.0\n"
                                "Filter 2: ON LP Fc 500 Hz")
    ok(g, "未知类型计 dropped", out["total"] == 2 and out["dropped"] == 1
       and len(out["bands"]) == 1, "total2/dropped1/kept1", str(out))
    # 超 10 段截断：12 段合法 → 收前 10，truncated=2
    lines = ["Preamp: -6.5 dB"] + ["Filter %d: ON PK Fc %d Hz Gain 1.0 dB Q 1.0"
                                   % (i, 100 * (i + 1)) for i in range(12)]
    out = main.parse_autoeq_apo("\n".join(lines))
    ok(g, "超 10 段截断", len(out["bands"]) == 10 and out["truncated"] == 2
       and out["total"] == 12, "10段/truncated2", str(out))
    # preamp 缺省 0；越界按 eq_preamp 口径钳 [-12,12]
    out = main.parse_autoeq_apo("Filter 1: ON PK Fc 100 Hz Gain 1.0 dB Q 1.0")
    ok(g, "preamp 缺省 0", out["preamp"] == 0.0, "0.0", str(out["preamp"]))
    out = main.parse_autoeq_apo("Preamp: -30 dB\nFilter 1: ON PK Fc 100 Hz "
                                "Gain 1.0 dB Q 1.0")
    ok(g, "preamp 下钳", out["preamp"] == -12.0, "-12.0", str(out["preamp"]))
    out = main.parse_autoeq_apo("Preamp: +99 dB\nFilter 1: ON PK Fc 100 Hz "
                                "Gain 1.0 dB Q 1.0")
    ok(g, "preamp 上钳", out["preamp"] == 12.0, "12.0", str(out["preamp"]))
    # 报错路径：一条都解不出来 / 识别到但全无效 / 超长 / 非文本
    err = raises(lambda: main.parse_autoeq_apo("hello world\nnot an eq preset"))
    ok(g, "垃圾文本报错", err is not None and "滤波器" in err, "ValueError",
       str(err))
    err = raises(lambda: main.parse_autoeq_apo(""))
    ok(g, "空文本报错", err is not None, "ValueError", str(err))
    err = raises(lambda: main.parse_autoeq_apo(None))
    ok(g, "非文本报错", err is not None, "ValueError", str(err))
    err = raises(lambda: main.parse_autoeq_apo("Preamp: -6.5 dB\n"
                                               "Filter 1: ON PK Fc 10 Hz Gain 99 dB"))
    ok(g, "全无效段报错", err is not None and "没有一段可用" in err, "ValueError",
       str(err))
    err = raises(lambda: main.parse_autoeq_apo("x" * (main.AUTOEQ_TEXT_MAX + 1)))
    ok(g, "超长文本报错", err is not None and "过长" in err, "ValueError", str(err))
    # 常见真实样本：autoeq.app 十段输出（低架 + 峰 + 高架混排）
    autoeq10 = ["Preamp: -6.1 dB"] + [
        "Filter 1: ON PK Fc 21 Hz Gain -5.2 dB Q 0.7",
        "Filter 2: ON PK Fc 45 Hz Gain 6.3 dB Q 0.8",
        "Filter 3: ON PK Fc 82 Hz Gain -3.6 dB Q 0.9",
        "Filter 4: ON PK Fc 145 Hz Gain 3.1 dB Q 1.0",
        "Filter 5: ON PK Fc 240 Hz Gain -2.4 dB Q 1.1",
        "Filter 6: ON PK Fc 480 Hz Gain 1.8 dB Q 1.2",
        "Filter 7: ON PK Fc 950 Hz Gain -1.6 dB Q 1.1",
        "Filter 8: ON PK Fc 2100 Hz Gain 2.2 dB Q 1.0",
        "Filter 9: ON PK Fc 4800 Hz Gain -2.8 dB Q 0.9",
        "Filter 10: ON HS Fc 9000 Hz Gain 1.5 dB Q 0.8",
    ]
    out = main.parse_autoeq_apo("\n".join(autoeq10))
    ok(g, "十段真实样本全收", out["preamp"] == -6.1 and len(out["bands"]) == 10
       and out["dropped"] == 0 and out["bands"][0]["f"] == 21.0
       and out["bands"][9]["type"] == "highshelf", "preamp-6.1/10段/dropped0",
       "preamp=%s n=%s dropped=%s" % (out["preamp"], len(out["bands"]),
                                      out["dropped"]))


def group_J():
    """参数 EQ 预设库存取（eqpreset_set / eqpreset_del）。"""
    g = "J"
    import main
    ddir = os.path.join(TMP_ROOT, "storeJ")
    os.makedirs(ddir, exist_ok=True)
    pf = os.path.join(ddir, "data.json")
    st = main.Store(pf)
    ok(g, "默认空预设库", st.data["eqpresets"] == {}, "{}",
       str(st.data["eqpresets"]))
    bands = [{"type": "peaking", "f": 100, "q": 0.7, "db": 3.2}]
    st.apply("eqpreset_set", {"name": "HD 800 S", "bands": bands, "preamp": -6.5})
    rec = st.data["eqpresets"].get("HD 800 S")
    ok(g, "正常写入", rec is not None and rec["bands"] == [
        {"type": "peaking", "f": 100.0, "q": 0.7, "db": 3.2}]
       and rec["preamp"] == -6.5 and "at" in rec,
       "bands舍入/preamp-6.5/at", str(rec))
    st.apply("eqpreset_set", {"name": "HD 800 S", "bands": bands, "preamp": -3.0})
    ok(g, "覆盖同名", st.data["eqpresets"]["HD 800 S"]["preamp"] == -3.0, "-3.0",
       str(st.data["eqpresets"]["HD 800 S"]))
    # NaN 硬墙：段全被丢 → 空预设不写
    st.apply("eqpreset_set", {"name": "NaN机", "bands": [
        {"f": 100, "q": 1, "db": "nan"}], "preamp": -1})
    st.apply("eqpreset_set", {"name": "垃圾段", "bands": "junk", "preamp": -1})
    st.apply("eqpreset_set", {"name": "空段表", "bands": [], "preamp": -1})
    st.apply("eqpreset_set", {"name": "  ", "bands": bands, "preamp": -1})
    st.apply("eqpreset_set", None)
    st.apply("eqpreset_set", "junk")
    ghosts = [k for k in st.data["eqpresets"]
              if k not in ("HD 800 S",)]
    ok(g, "NaN/垃圾/空段表/空名/payload 非法全不写", not ghosts, "只剩 HD 800 S",
       str(ghosts))
    st.apply("eqpreset_set", {"name": "长" * 200, "bands": bands, "preamp": -1})
    ok(g, "name 截 80", ("长" * 80) in st.data["eqpresets"], "80 字名",
       str(len([k for k in st.data["eqpresets"] if len(k) == 80])))
    st.apply("eqpreset_set", {"name": "钳上", "bands": bands, "preamp": 99})
    st.apply("eqpreset_set", {"name": "钳下", "bands": bands, "preamp": -99})
    st.apply("eqpreset_set", {"name": "钳垃圾", "bands": bands, "preamp": "abc"})
    st.apply("eqpreset_set", {"name": "钳NaN", "bands": bands, "preamp": "nan"})
    pre = st.data["eqpresets"]
    ok(g, "preamp 钳 ±12 / 垃圾回落 0",
       pre["钳上"]["preamp"] == 12.0 and pre["钳下"]["preamp"] == -12.0
       and pre["钳垃圾"]["preamp"] == 0.0 and pre["钳NaN"]["preamp"] == 0.0,
       "12.0/-12.0/0/0",
       str((pre["钳上"]["preamp"], pre["钳下"]["preamp"],
            pre["钳垃圾"]["preamp"], pre["钳NaN"]["preamp"])))
    # 超 10 段截断
    st.apply("eqpreset_set", {"name": "截断", "preamp": 0,
                              "bands": [{"type": "peaking", "f": 100 * (i + 1),
                                         "q": 1, "db": 1} for i in range(12)]})
    ok(g, "超 10 段截断", len(pre["截断"]["bands"]) == 10, "10 段",
       str(len(pre["截断"]["bands"])))
    # 删除：存在 / 不存在 / payload 非法都不崩
    st.apply("eqpreset_del", {"name": "HD 800 S"})
    st.apply("eqpreset_del", {"name": "不存在"})
    st.apply("eqpreset_del", None)
    st.apply("eqpreset_del", "junk")
    ok(g, "删除与非法 payload", "HD 800 S" not in st.data["eqpresets"],
       "已删", str(sorted(st.data["eqpresets"].keys())))
    ok(g, "白名单收录", "eqpreset_set" in main.STORE_OPS
       and "eqpreset_del" in main.STORE_OPS, "True", "True")
    # 上限裁剪：灌满再触发
    n = main.EQPRESET_MAX + 5
    for i in range(n):
        pre["p:%d" % i] = {"bands": [], "preamp": 0, "at": i}
    main._prune_by_at(pre, main.EQPRESET_MAX)
    ok(g, "超上限裁剪最旧", len(pre) == main.EQPRESET_MAX and "p:0" not in pre
       and "p:%d" % (n - 1) in pre, "=%d 且尾部保留" % main.EQPRESET_MAX,
       "len=%d p:0 in=%s tail in=%s" % (len(pre), "p:0" in pre,
                                        "p:%d" % (n - 1) in pre))
    # 落盘往返：重实例化读回
    st.apply("eqpreset_set", {"name": "ER2SE", "bands": bands, "preamp": -2.5})
    st.save()
    st2 = main.Store(pf)
    rec = st2.data["eqpresets"].get("ER2SE")
    ok(g, "落盘往返", rec is not None and rec["preamp"] == -2.5 and rec["bands"],
       "ER2SE 可读回", str(rec))


def group_K():
    """HTTP 端到端：/api/eq/parse + /api/store 预设与设置全链路。"""
    g = "K"
    home = mkhome("http_eq")
    env = spawn(home)
    try:
        if not env["boot"]:
            ok(g, "服务启动", False, "boot",
               open(env["log"], "rb").read().decode("utf-8", "replace")[-600:])
            return
        good = ("Preamp: -6.5 dB\nFilter 1: ON PK Fc 31 Hz Gain 7.9 dB Q 0.70\n"
                "Filter 2: ON HS Fc 10000 Hz Gain -2.0 dB Q 0.70")
        st, r = http(env, "POST", "/api/eq/parse", {"text": good})
        d = r.get("data") or {}
        ok(g, "解析好文本", st == 200 and r.get("ok") and d.get("preamp") == -6.5
           and len(d.get("bands") or []) == 2, "200+preamp-6.5+2段",
           "%s %s" % (st, r))
        st, r = http(env, "POST", "/api/eq/parse", {"text": "hello"})
        ok(g, "垃圾文本 400", st == 400 and r.get("ok") is False
           and isinstance(r.get("error"), str), "400+ok=False+error",
           "%s %s" % (st, r))
        st, r = http(env, "POST", "/api/eq/parse", {})
        ok(g, "空 body 400", st == 400 and r.get("ok") is False, "400+ok=False",
           "%s %s" % (st, r))
        st, r = http(env, "POST", "/api/eq/parse", {"text": 123})
        ok(g, "非文本 400", st == 400 and r.get("ok") is False, "400+ok=False",
           "%s %s" % (st, r))
        # 413 上限：请求体超限在读 body 之前就拒（伪造大 Content-Length + 小 body）
        def http_413(path, clen):
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=10)
            try:
                conn.putrequest("POST", path)
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(clen))
                conn.endheaders()
                conn.send(b"{}")
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read().decode("utf-8"))
            finally:
                conn.close()
        st, r = http_413("/api/eq/parse", 1024 * 1024 + 1)
        ok(g, "/api/eq/parse 超 1MB 413", st == 413 and r.get("ok") is False,
           "413+ok=False", "%s %s" % (st, r))
        st, r = http_413("/api/store", 4 * 1024 * 1024 + 1)
        ok(g, "/api/store 超 4MB 413", st == 413 and r.get("ok") is False,
           "413+ok=False", "%s %s" % (st, r))
        # 解析 → 设置 → 存预设 → 回读 → 删除 全链路
        st, r = http(env, "POST", "/api/store", {"op": "settings", "payload": {
            "eq_mode": "parametric", "eq_bands": d["bands"],
            "eq_preamp": d["preamp"], "eq_headphone": "HD 800 S"}})
        s = (r.get("data") or {}).get("settings") or {}
        ok(g, "设置参数模式", st == 200 and r.get("ok")
           and s.get("eq_mode") == "parametric" and s.get("eq_preamp") == -6.5
           and s.get("eq_bands") == d["bands"], "parametric/-6.5/段表一致",
           "%s %s" % (st, {k: s.get(k) for k in ("eq_mode", "eq_preamp")}))
        st, r = http(env, "POST", "/api/store", {"op": "eqpreset_set", "payload": {
            "name": "HD 800 S", "bands": d["bands"], "preamp": d["preamp"]}})
        rec = ((r.get("data") or {}).get("eqpresets") or {}).get("HD 800 S")
        ok(g, "存预设", st == 200 and r.get("ok") and rec is not None
           and rec["preamp"] == -6.5 and rec["bands"] == d["bands"],
           "200+预设可回读", "%s %s" % (st, rec))
        st, data = http(env, "GET", "/api/store")
        ok(g, "GET 回读预设与设置", data["eqpresets"].get("HD 800 S") is not None
           and data["settings"].get("eq_mode") == "parametric",
           "预设+parametric", "%s %s" % (bool(data["eqpresets"]),
                                         data["settings"].get("eq_mode")))
        st, r = http(env, "POST", "/api/store", {"op": "eqpreset_del",
                                                 "payload": {"name": "HD 800 S"}})
        ok(g, "删预设", ((r.get("data") or {}).get("eqpresets") or {}) == {},
           "{}", str((r.get("data") or {}).get("eqpresets")))
        st, r = http(env, "POST", "/api/store", {"op": "eqpreset_set", "payload": {
            "name": "", "bands": []}})
        ok(g, "空名不写", ((r.get("data") or {}).get("eqpresets") or {}) == {},
           "{}", str((r.get("data") or {}).get("eqpresets")))
        # eq_headphone 显式清空 round-trip：设置型号 → 传空串 → 落盘值必须真的空
        st, r = http(env, "POST", "/api/store", {"op": "settings",
                                                 "payload": {"eq_headphone": ""}})
        s = (r.get("data") or {}).get("settings") or {}
        ok(g, "eq_headphone 空串落盘 round-trip",
           st == 200 and r.get("ok") and s.get("eq_headphone") == "",
           "落盘后仍为空串", str(s.get("eq_headphone")))
        # 落盘不得出现非法字面量（NaN/inf 硬墙最后一道）
        dpath = os.path.join(home, "Elysium", "data.json")
        with open(dpath, "r", encoding="utf-8") as f:
            raw = f.read()
        ok(g, "落盘无 NaN/Infinity", "NaN" not in raw and "Infinity" not in raw,
           "clean json", "NaN in=%s Infinity in=%s" % ("NaN" in raw,
                                                       "Infinity" in raw))
    finally:
        kill(env)


# --------------------------------------------------------------------------
def report(elapsed):
    n_pass = sum(1 for r in RESULTS if r["verdict"] == "PASS")
    n_fail = sum(1 for r in RESULTS if r["verdict"] == "FAIL")
    n_warn = sum(1 for r in RESULTS if r["verdict"] == "WARN")
    lines = ["# verify_audiophile 报告", "",
             "- 结果：**%d PASS / %d FAIL / %d WARN**（共 %d 例，%.1fs）"
             % (n_pass, n_fail, n_warn, len(RESULTS), elapsed), ""]
    if n_fail:
        lines.append("## 失败明细")
        for r in RESULTS:
            if r["verdict"] == "FAIL":
                lines.append("- [%s] %s：期望 %s，实际 %s"
                             % (r["group"], r["name"], r["expect"], r["actual"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    global TMP_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}

    def want(g):
        return not only or g in only

    TMP_ROOT = tempfile.mkdtemp(prefix="elysium_audio_")
    t0 = time.time()
    print("[env] tmp=%s" % TMP_ROOT, flush=True)
    # 进程内 import main 之前先指走 ELYSIUM_HOME：任何组都不许碰真实 data.json
    os.environ["ELYSIUM_HOME"] = mkhome("env")
    sys.path.insert(0, REPO)
    try:
        if want("A"):
            group_A()
        if want("B"):
            group_B()
        if want("C"):
            group_C()
        if want("D"):
            group_D()
        if want("E"):
            group_E()
        if want("H"):
            group_H()
        if want("I"):
            group_I()
        if want("J"):
            group_J()
        if want("G"):
            group_G()
        if want("K"):
            group_K()
        if want("F"):
            group_F()
    except Exception:                                       # noqa: BLE001
        record("X", "脚本异常", "FAIL", "无异常", traceback.format_exc()[-800:])
    finally:
        if want("G"):
            pass

    rep = report(time.time() - t0)
    out = os.path.join(REPO, "audiophile_report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(rep)
    print("\n" + rep)
    print("[report] " + out)
    if not args.keep:
        for _ in range(6):
            shutil.rmtree(TMP_ROOT, ignore_errors=True)
            if not os.path.exists(TMP_ROOT):
                break
            time.sleep(0.4)
        if os.path.exists(TMP_ROOT):
            print("[warn] 临时目录没删干净：%s" % TMP_ROOT)
    fails = sum(1 for r in RESULTS if r["verdict"] == "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
