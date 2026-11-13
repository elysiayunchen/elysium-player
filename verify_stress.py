# -*- coding: utf-8 -*-
"""Elysium 压力测试 + 逻辑测试（不依赖真实上游接口）。

和 verify_edge.py 的区别：
  * verify_edge 关注「畸形请求不崩」；本脚本关注「状态机与数据语义是否正确」
    —— 写入读回、边界钳制、重复/越界操作、并发线性一致性、脏数据启动。
  * 所有响应都用 strict JSON 解析（NaN / Infinity 直接判失败）：浏览器
    JSON.parse 不接受这两个字面量，Python 的 json.loads 却默认接受 ——
    这也是 data.json 里混进一个 NaN 就能让整页数据加载失败的原因。

分组：
  S1 设置：数值边界钳制 / 非有限值 / 非法类型 / eq 清洗 / 严格 JSON
  S2 store：收藏、历史、最近播放、歌单 CRUD/重排、响度、EQ 预设、作品
  S3 收听记录：计数、去重、无限时长、并发同曲
  S4 下载队列：畸形入参、未知任务操作、快照形状
  S5 接口模糊：GET/POST 全端点畸形输入，不 5xx、响应严格 JSON
  S6 脏数据启动：data.json 里各种错类型，所有聚合接口仍可用
  S7 并发/浸泡：混合读写、并发收藏/歌单、突发连接、5MB 请求体
  S8 重启/导出：落盘数据可重载、导出脱敏、无重复键
  S9 静态资源/前端源码：资源 200、HTML 无重复 id、JS 语法检查

用法：
    python verify_stress.py                # 全部
    python verify_stress.py --only=S1,S2   # 指定组
    python verify_stress.py --keep         # 保留临时目录
环境：ELYSIUM_HOME 指向临时目录，复制 web/providers，不碰真实数据。
"""
import argparse
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []
TMP_ROOT = None

SETTING_RULES = {
    "volume": (0, 100),
    "concurrency": (1, 4),
    "cache_limit_mb": (0, 1024 * 1024),
    "cache_min_plays": (1, 100),
    "cache_week_plays": (1, 1000),
    "cache_keep_days": (1, 365),
    "lrc_offset_ms": (-10000, 10000),
    "crossfade_ms": (0, 2000),
    "loudness_target": (-40, -6),
}


# --------------------------------------------------------------------------
# 结果记录
# --------------------------------------------------------------------------
def record(group, name, verdict, expect, actual):
    RESULTS.append({"group": group, "name": name, "verdict": verdict,
                    "expect": expect, "actual": actual})
    mark = {"PASS": "[PASS] ", "FAIL": "[FAIL] ", "WARN": "[WARN] ", "SKIP": "[SKIP] "}[verdict]
    print("%s%-4s %s | 期望 %s | 实际 %s" % (mark, group, name, expect, actual), flush=True)


def ok(group, name, cond, expect, actual):
    record(group, name, "PASS" if cond else "FAIL", expect, actual)
    return bool(cond)


def warn(group, name, cond, expect, actual):
    record(group, name, "PASS" if cond else "WARN", expect, actual)
    return bool(cond)


# --------------------------------------------------------------------------
# 严格 JSON（拒绝 NaN / Infinity，与浏览器 JSON.parse 同口径）
# --------------------------------------------------------------------------
class NonFiniteJSON(ValueError):
    pass


def _bad_const(x):
    raise NonFiniteJSON("非法的 JSON 常量 %s" % x)


def strict_loads(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw, parse_constant=_bad_const)


# --------------------------------------------------------------------------
# 隔离实例
# --------------------------------------------------------------------------
def mkhome(tag, data=None):
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
    if data is not None:
        ddir = os.path.join(home, "Elysium")
        os.makedirs(ddir, exist_ok=True)
        with open(os.path.join(ddir, "data.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    return base, home


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def spawn(home, tag="srv"):
    port = free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
    log_path = os.path.join(home, "..", tag + ".log")
    logf = open(log_path, "wb")
    proc = subprocess.Popen([PY, "main.py", "--serve", "--port=%d" % port],
                            cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    logf.close()
    envd = {"proc": proc, "port": port, "base": "http://127.0.0.1:%d" % port,
            "home": home, "log": log_path}
    t0 = time.time()
    for _ in range(150):
        if proc.poll() is not None:
            break
        try:
            st, _h, raw, _e = request(envd, "GET", "/api/cache", timeout=1)
            if st == 200:
                envd["boot"] = round(time.time() - t0, 2)
                return envd
        except OSError:
            pass
        time.sleep(0.2)
    tail = ""
    try:
        tail = open(log_path, "rb").read().decode("utf-8", "replace")[-1500:]
    except OSError:
        pass
    envd["dead"] = tail
    return envd


def kill(env):
    if not env or not env.get("proc"):
        return
    try:
        env["proc"].terminate()
        env["proc"].wait(timeout=6)
    except Exception:                                    # noqa: BLE001
        try:
            env["proc"].kill()
        except Exception:                                # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def request(env, method, path, body=None, raw=None, headers=None, timeout=30):
    hdrs = {"Host": "127.0.0.1:%d" % env["port"]}
    if headers:
        hdrs.update(headers)
    if raw is None and body is not None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    if raw is not None:
        hdrs.setdefault("Content-Length", str(len(raw)))
    conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=timeout)
    t0 = time.time()
    try:
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data, time.time() - t0
    finally:
        conn.close()


def get_store(env):
    st, hd, raw, el = request(env, "GET", "/api/store")
    if st != 200:
        raise RuntimeError("GET /api/store -> %s" % st)
    return strict_loads(raw), hd, raw, el


def op(env, name, payload=None):
    st, hd, raw, el = request(env, "POST", "/api/store",
                              {"op": name, "payload": payload})
    return st, strict_loads(raw), el


def reset_store(env):
    for name in ("favorites_clear", "history_clear", "recent_clear", "downloads_clear"):
        op(env, name, {})
    data, _h, _r, _e = get_store(env)
    for pl in list(data.get("playlists") or []):
        op(env, "playlist_delete", {"id": pl.get("id")})
    op(env, "settings", {"personalized": True, "auto_cache": False,
                         "download_dir": os.path.join(env["home"], "dl")})
    os.makedirs(os.path.join(env["home"], "dl"), exist_ok=True)


def alive(env):
    try:
        st, _h, _r, _e = request(env, "GET", "/api/cache", timeout=2)
        return st == 200
    except Exception:                                    # noqa: BLE001
        return False


def json_ok(env, label, method, path, body=None, raw=None, timeout=30):
    """发请求并做「不 5xx / JSON 可严格解析」两项硬检查。"""
    try:
        st, hd, data, el = request(env, method, path, body=body, raw=raw, timeout=timeout)
    except Exception as e:                                # noqa: BLE001
        return None, "EXC %s: %s" % (type(e).__name__, e)
    if st >= 500:
        return None, "HTTP %d" % st
    ctype = (hd.get("Content-Type") or "").lower()
    if "json" in ctype and data:
        try:
            return strict_loads(data), None
        except Exception as e:                            # noqa: BLE001
            return None, "非严格 JSON: %s" % e
    return {"_raw_len": len(data), "_ctype": ctype}, None


# --------------------------------------------------------------------------
# S1 设置
# --------------------------------------------------------------------------
def group_s1(env):
    data, _h, _r, _e = get_store(env)
    ok("S1", "设置基线可读", isinstance(data.get("settings"), dict),
       "settings 为 dict", type(data.get("settings")).__name__)

    # 数值边界：低于 min → min；高于 max → max；非法 → 保持原值
    for key, (lo, hi) in SETTING_RULES.items():
        st, d, _el = op(env, "settings", {key: lo - 1})
        cur = d.get("data", {}).get("settings", {}).get(key)
        ok("S1", "%s 下越界钳制" % key, st == 200 and d.get("ok") and cur == lo,
           "%d" % lo, cur)
        st, d, _el = op(env, "settings", {key: hi + 1})
        cur = d.get("data", {}).get("settings", {}).get(key)
        ok("S1", "%s 上越界钳制" % key, st == 200 and d.get("ok") and cur == hi,
           "%d" % hi, cur)
        st, d, _el = op(env, "settings", {key: "abc"})
        cur = d.get("data", {}).get("settings", {}).get(key)
        ok("S1", "%s 非法值保留旧值" % key, st == 200 and d.get("ok") and cur == hi,
           "%d" % hi, cur)
        # 字符串形式的大数：float("1e999") = inf → int() 会 OverflowError
        st, d, _el = op(env, "settings", {key: "1e999"})
        cur = d.get("data", {}).get("settings", {}).get(key)
        ok("S1", "%s 1e999 不拖垮整次写入" % key, st == 200 and d.get("ok") and cur == hi,
           "ok + 保留 %d" % hi, "status=%s ok=%s cur=%s" % (st, d.get("ok"), cur))
        # 其它键必须照常写入（不能因为一个坏值把整个 payload 丢掉）
        st, d, _el = op(env, "settings", {key: "1e999", "volume": 33})
        vol = d.get("data", {}).get("settings", {}).get("volume")
        ok("S1", "%s 坏值不影响同批合法键" % key, st == 200 and d.get("ok") and vol == 33,
           "volume=33", vol)
        op(env, "settings", {key: hi})

    # JSON 非标准字面量 Infinity / NaN（Python 服务端会收）
    before, _h, _r, _e = get_store(env)
    prev_vol = before.get("settings", {}).get("volume")
    for bad in (float("inf"), float("nan")):
        st, d, _el = op(env, "settings", {"volume": bad, "cache_keep_days": 5})
        vol = d.get("data", {}).get("settings", {}).get("volume")
        keep = d.get("data", {}).get("settings", {}).get("cache_keep_days")
        ok("S1", "设置值 %r 被丢弃且不 poison" % bad,
           st == 200 and d.get("ok") and vol == prev_vol and keep == 5,
           "ok，volume 保持 %s，cache_keep_days=5" % prev_vol,
           "vol=%s keep=%s" % (vol, keep))

    # eq_gains / eq_bands / eq_preamp
    st, d, _el = op(env, "settings", {"eq_gains": [0, float("nan"), float("inf"), "x", 3]})
    gains = d.get("data", {}).get("settings", {}).get("eq_gains")
    ok("S1", "eq_gains 非有限值被过滤", st == 200 and d.get("ok") and gains == [0, 3],
       "[0, 3]", gains)
    st, d, _el = op(env, "settings", {"eq_bands": [
        {"type": "peaking", "f": 100, "q": 1, "db": 2},
        {"type": "peaking", "f": float("nan"), "q": 1, "db": 2},
        {"type": "bogus", "f": 100, "q": 1, "db": 2},
        {"type": "peaking", "f": 20000, "q": 10, "db": -12},
    ], "eq_preamp": "1e999"})
    bands = d.get("data", {}).get("settings", {}).get("eq_bands")
    preamp = d.get("data", {}).get("settings", {}).get("eq_preamp")
    ok("S1", "eq_bands 只留合法段", st == 200 and d.get("ok")
       and len(bands or []) == 2 and preamp == 0.0,
       "2 段 + preamp 不变", "bands=%s preamp=%s" % (len(bands or []), preamp))

    # 类型错误
    st, d, _el = op(env, "settings", {"download_dir": {"x": 1}})
    dd = d.get("data", {}).get("settings", {}).get("download_dir")
    ok("S1", "download_dir 非字符串被拒", st == 200 and d.get("ok") and isinstance(dd, str),
       "保持字符串", "%r (%s)" % (dd, type(dd).__name__))
    st, d, _el = op(env, "settings", {"name_template": 5})
    nt = d.get("data", {}).get("settings", {}).get("name_template")
    ok("S1", "name_template 非字符串被拒", st == 200 and d.get("ok") and isinstance(nt, str),
       "保持字符串", "%r" % (nt,))
    st, d, _el = op(env, "settings", {"webdav": [1, 2]})
    wd = d.get("data", {}).get("settings", {}).get("webdav")
    ok("S1", "webdav 非对象被拒", st == 200 and d.get("ok") and not isinstance(wd, list),
       "不是数组", type(wd).__name__)
    r, err = json_ok(env, "", "GET", "/api/sync/status")
    ok("S1", "脏 webdav 后 sync/status 仍 200", r is not None and err is None,
       "200 + 严格 JSON", err or "ok")

    # 非对象 payload
    st, hd, raw, el = request(env, "POST", "/api/store",
                              {"op": "settings", "payload": [1, 2, 3]})
    d = strict_loads(raw)
    ok("S1", "settings 非对象 payload 受控", st == 200 and d.get("ok") is False,
       "200 ok:false", "status=%s ok=%s" % (st, d.get("ok")))

    # 未知键 + NaN：不能落进 data.json
    st, d, _el = op(env, "settings", {"weird_key": float("nan"), "another": 1})
    s = d.get("data", {}).get("settings", {})
    ok("S1", "未知键 NaN 被丢弃", st == 200 and d.get("ok")
       and "weird_key" not in s and s.get("another") == 1,
       "只留 another=1", "weird_key in settings=%s" % ("weird_key" in s))
    d, _h, raw, _e = get_store(env)
    try:
        strict_loads(raw)
        strict = True
    except Exception:
        strict = False
    ok("S1", "脏设置后 /api/store 仍是严格 JSON", strict, "无 NaN/Infinity", strict)


# --------------------------------------------------------------------------
# S2 store 语义
# --------------------------------------------------------------------------
def group_s2(env):
    reset_store(env)

    # ---- 收藏
    a = {"kind": "song", "platform": "kuwo", "id": "s1", "name": "A"}
    b = {"kind": "song", "platform": "kuwo", "id": "s2", "name": "B"}
    st, d, _el = op(env, "fav_toggle", a)
    ok("S2", "收藏新增", d.get("data", {}).get("active") is True
       and len(d.get("data", {}).get("favorites") or []) == 1, "active=true / 1 条",
       d.get("data", {}).get("active"))
    st, d, _el = op(env, "fav_toggle", a)
    ok("S2", "收藏再次点击取消", d.get("data", {}).get("active") is False
       and not (d.get("data", {}).get("favorites") or []), "active=false / 0 条",
       d.get("data", {}).get("active"))
    op(env, "fav_toggle", a)
    op(env, "fav_toggle", b)
    st, d, _el = op(env, "fav_toggle", {"kind": "song", "platform": "kuwo", "name": "ghost"})
    cur, _h, _r, _e = get_store(env)
    favs = cur.get("favorites") or []
    ok("S2", "无 id 收藏被拒且不留幽灵", d.get("ok") is False and len(favs) == 2,
       "ok:false / 仍 2 条", "ok=%s n=%s" % (d.get("ok"), len(favs)))
    st, d, _el = op(env, "fav_remove", {"kind": "song", "platform": "kuwo", "id": "s1"})
    ok("S2", "fav_remove 精确删除", len(d.get("data", {}).get("favorites") or []) == 1,
       "1 条", len(d.get("data", {}).get("favorites") or []))
    op(env, "fav_toggle", {"kind": "album", "platform": "kuwo", "id": "al1", "name": "AL"})
    st, d, _el = op(env, "favorites_clear", {"kind": "song"})
    fs = d.get("data", {}).get("favorites") or []
    ok("S2", "favorites_clear 按类型", len(fs) == 1 and fs[0].get("kind") == "album",
       "只留 album", [(x.get("kind"), x.get("id")) for x in fs])
    op(env, "favorites_clear", {})

    # ---- 搜索历史
    for i in range(45):
        op(env, "history_add", {"q": "kw%d" % i, "platform": "kuwo" if i % 2 else "wyy"})
    data, _h, _r, _e = get_store(env)
    hist = data.get("history") or []
    ok("S2", "历史记录上限 40", len(hist) == 40, "40", len(hist))
    ok("S2", "历史最新在前", hist and hist[0].get("q") == "kw44", "kw44", hist and hist[0].get("q"))
    op(env, "history_add", {"q": "kw44", "platform": "kuwo"})
    data, _h, _r, _e = get_store(env)
    hist = data.get("history") or []
    ok("S2", "历史同词去重", len(hist) == 40 and hist[0].get("q") == "kw44",
       "40 条 / kw44 置顶", "%s / %s" % (len(hist), hist and hist[0].get("q")))
    op(env, "history_clear", {})

    # ---- 最近播放
    for i in range(65):
        op(env, "recent_add", {"platform": "kuwo", "id": "r%d" % i, "name": "R%d" % i})
    data, _h, _r, _e = get_store(env)
    recent = data.get("recent") or []
    ok("S2", "最近播放上限 60", len(recent) == 60, "60", len(recent))
    ok("S2", "最近播放最新在前", recent[0].get("id") == "r64", "r64",
       recent and recent[0].get("id"))
    op(env, "recent_add", {"platform": "kuwo", "id": "r64", "name": "R64"})
    data, _h, _r, _e = get_store(env)
    recent = data.get("recent") or []
    ids = [x.get("id") for x in recent]
    ok("S2", "最近播放同曲去重", len(recent) == 60 and ids.count("r64") == 1,
       "60 条 / r64 唯一", "%s / %s" % (len(recent), ids.count("r64")))
    op(env, "recent_add", {"name": "no-id"})
    data, _h, _r, _e = get_store(env)
    ok("S2", "recent_add 无 id 不写入", len(data.get("recent") or []) == 60,
       "60", len(data.get("recent") or []))
    op(env, "recent_clear", {})

    # ---- 反馈 / 口味
    op(env, "feedback_set", {"platform": "kuwo", "id": "f1", "verdict": "like", "artist": "A"})
    data, _h, _r, _e = get_store(env)
    ok("S2", "feedback_set 写入", (data.get("feedback") or {}).get("kuwo:f1", {}).get("verdict") == "like",
       "like", (data.get("feedback") or {}).get("kuwo:f1"))
    op(env, "feedback_set", {"platform": "kuwo", "id": "f1", "verdict": "none"})
    data, _h, _r, _e = get_store(env)
    ok("S2", "feedback verdict=none 删除", "kuwo:f1" not in (data.get("feedback") or {}),
       "已删除", "kuwo:f1" in (data.get("feedback") or {}))
    op(env, "taste_note", {"text": "x" * 500})
    data, _h, _r, _e = get_store(env)
    ok("S2", "口味备注截断 280", len(data.get("taste_note") or "") == 280, "280",
       len(data.get("taste_note") or ""))
    op(env, "taste_genres", {"ids": ["pop", "pop", "nope", 123, None]})
    data, _h, _r, _e = get_store(env)
    ok("S2", "口味风格过滤/去重", data.get("taste_genres") == ["pop"], "['pop']",
       data.get("taste_genres"))

    # ---- 歌单
    st, d, _el = op(env, "playlist_create", {"name": "   "})
    ok("S2", "空名歌单不创建", len(d.get("data", {}).get("playlists") or []) == 0,
       "0", len(d.get("data", {}).get("playlists") or []))
    st, d, _el = op(env, "playlist_create", {"name": "N" * 100})
    pls = d.get("data", {}).get("playlists") or []
    pid = pls[-1]["id"] if pls else ""
    ok("S2", "歌单名截断 60", len(pls[-1]["name"]) == 60 if pls else False, "60",
       len(pls[-1]["name"]) if pls else 0)
    op(env, "playlist_rename", {"id": pid, "name": "  "})
    data, _h, _r, _e = get_store(env)
    pl = next((p for p in data["playlists"] if p["id"] == pid), {})
    ok("S2", "歌单空名重命名不改", len(pl.get("name") or "") == 60, "保持 60",
       len(pl.get("name") or ""))
    op(env, "playlist_rename", {"id": pid, "name": "我的歌单"})
    items = [{"platform": "kuwo", "id": str(i), "name": "T%d" % i,
              "artist": "A", "duration": 200 + i} for i in range(1, 4)]
    op(env, "playlist_add", {"id": pid, "items": items + items})
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ok("S2", "歌单批量去重", len(pl["items"]) == 3, "3",
       [x.get("id") for x in pl["items"]])
    op(env, "playlist_add", {"id": pid, "items": [
        {"platform": "", "id": "x"}, {"platform": "kuwo", "id": ""}, "junk"]})
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ok("S2", "歌单拒绝缺平台/id 条目", len(pl["items"]) == 3, "3", len(pl["items"]))

    # 重排：合法 / 重复索引 / 越界 / 非整数
    op(env, "playlist_reorder", {"id": pid, "order": [2, 0, 1]})
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ok("S2", "歌单合法重排", [x["id"] for x in pl["items"]] == ["3", "1", "2"],
       "['3','1','2']", [x["id"] for x in pl["items"]])
    op(env, "playlist_reorder", {"id": pid, "order": [1, 1, 0]})
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ids = [x["id"] for x in pl["items"]]
    ok("S2", "重排重复索引不复制/不丢曲目",
       len(ids) == 3 and sorted(ids) == ["1", "2", "3"], "3 条且集合不变", ids)
    op(env, "playlist_reorder", {"id": pid, "order": [99, -1, "x", True, 2]})
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ids = [x["id"] for x in pl["items"]]
    ok("S2", "重排越界/非整数被忽略", len(ids) == 3 and sorted(ids) == ["1", "2", "3"],
       "3 条且集合不变", ids)
    op(env, "playlist_reorder", {"id": pid, "order": []})
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ok("S2", "重排空 order 不改顺序", len(pl["items"]) == 3, "3", len(pl["items"]))

    # 1000 首导入耗时（大批量 dedupe 不能 O(n²) 到不可用）
    big = [{"platform": "kuwo", "id": "big%d" % i, "name": "B%d" % i} for i in range(1000)]
    t0 = time.time()
    st, d, el = op(env, "playlist_add", {"id": pid, "items": big})
    dt = time.time() - t0
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid)
    ok("S2", "1000 首导入 <5s 且全量入库", st == 200 and d.get("ok") and len(pl["items"]) == 1003 and dt < 5,
       "1003 条 / <5s", "%d 条 / %.2fs" % (len(pl["items"]), dt))

    # 删除歌单
    op(env, "playlist_delete", {"id": pid})
    data, _h, _r, _e = get_store(env)
    ok("S2", "删除歌单", all(p["id"] != pid for p in data["playlists"]), "已删除",
       [p["id"] for p in data["playlists"]])

    # ---- 作品（store 直写路径）
    st, d, _el = op(env, "work_add", {"id": "abc123", "name": "N" * 300,
                                      "artist": "A", "platform": "kuwo",
                                      "duration": 1e999, "pitch_avg": float("nan")})
    works = d.get("data", {}).get("works") or []
    w = works[0] if works else {}
    ok("S2", "work_add 清洗长度/非有限值", st == 200 and d.get("ok")
       and len(w.get("name") or "") == 120 and "pitch_avg" not in w,
       "name=120 / 无 pitch_avg", "name=%d / pitch_avg=%s" % (len(w.get("name") or ""),
                                                              w.get("pitch_avg")))
    st, d, _el = op(env, "work_add", {"id": "../evil", "name": "x"})
    works = d.get("data", {}).get("works") or []
    ok("S2", "work_add 非法 id 被拒", d.get("ok") and len(works) == 1, "仍 1 条", len(works))
    op(env, "work_remove", {"id": "abc123"})
    data, _h, _r, _e = get_store(env)
    ok("S2", "work_remove", not (data.get("works") or []), "0", len(data.get("works") or []))

    # ---- 并发收藏：偶数次切换后必须回到未收藏，且无重复
    fid = "conc1"
    def toggle():
        for _ in range(20):
            op(env, "fav_toggle", {"kind": "song", "platform": "kuwo", "id": fid, "name": "C"})
    ths = [threading.Thread(target=toggle) for _ in range(10)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    data, _h, _r, _e = get_store(env)
    hits = [f for f in data.get("favorites") or [] if f.get("id") == fid]
    ok("S2", "并发 200 次切换后状态一致", len(hits) == 0, "0 条", len(hits))

    # ---- 并发去重添加：8 线程各加同一批 100 首 → 恰好 100
    op(env, "playlist_create", {"name": "conc"})
    data, _h, _r, _e = get_store(env)
    pid2 = data["playlists"][-1]["id"]
    batch = [{"platform": "kuwo", "id": "c%d" % i, "name": "C%d" % i} for i in range(100)]
    def add_batch():
        for _ in range(3):
            op(env, "playlist_add", {"id": pid2, "items": batch})
    ths = [threading.Thread(target=add_batch) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    data, _h, _r, _e = get_store(env)
    pl = next(p for p in data["playlists"] if p["id"] == pid2)
    ids = [x.get("id") for x in pl["items"]]
    ok("S2", "并发添加不产生重复", len(ids) == 100 and len(set(ids)) == 100,
       "100 条 / 100 唯一", "%d / %d" % (len(ids), len(set(ids))))


# --------------------------------------------------------------------------
# S3 收听记录
# --------------------------------------------------------------------------
def group_s3(env):
    reset_store(env)
    item = {"platform": "kuwo", "id": "play1", "name": "P", "artist": "A", "duration": 180}
    counts = []
    for _ in range(3):
        st, hd, raw, el = request(env, "POST", "/api/play",
                                  {"item": item, "quality": {"format": "mp3", "bitrate": "320"},
                                   "completed": True})
        d = strict_loads(raw)
        counts.append(d.get("data", {}).get("today_count"))
    ok("S3", "连续收听计数 1/2/3", counts == [1, 2, 3], "[1,2,3]", counts)
    data, _h, _r, _e = get_store(env)
    rec = (data.get("plays") or {}).get("kuwo_play1") or {}
    ok("S3", "plays 落盘 meta", rec.get("meta", {}).get("name") == "P", "P",
       rec.get("meta", {}).get("name"))
    ok("S3", "recent 同步更新", (data.get("recent") or [{}])[0].get("id") == "play1",
       "play1", (data.get("recent") or [{}])[0].get("id"))

    # 未听完不计数
    st, hd, raw, el = request(env, "POST", "/api/play",
                              {"item": item, "completed": False})
    d = strict_loads(raw)
    ok("S3", "未听完不计数", d.get("data", {}).get("today_count") == 3, "3",
       d.get("data", {}).get("today_count"))

    # 无限时长（Python json 接受 Infinity）
    st, hd, raw, el = request(env, "POST", "/api/play", raw=(
        '{"item":{"platform":"kuwo","id":"inf1","name":"I","duration":Infinity},'
        '"quality":{},"completed":true}').encode())
    d = strict_loads(raw)
    ok("S3", "时长 Infinity 不崩溃且计数", st == 200 and d.get("ok") is True,
       "ok:true", "status=%s %s" % (st, d.get("ok") if isinstance(d, dict) else d))
    # quality 传字符串
    st, hd, raw, el = request(env, "POST", "/api/play",
                              {"item": {"platform": "kuwo", "id": "q1", "name": "Q"},
                               "quality": "best", "completed": True})
    d = strict_loads(raw)
    ok("S3", "quality 非对象不崩溃", st == 200 and d.get("ok") is True, "ok:true",
       "status=%s ok=%s" % (st, d.get("ok")))
    # item 不是对象
    st, hd, raw, el = request(env, "POST", "/api/play",
                              {"item": "song", "completed": True})
    d = strict_loads(raw)
    ok("S3", "item 非对象受控", st == 200 and d.get("ok") is False, "ok:false",
       "status=%s ok=%s" % (st, d.get("ok")))

    # 并发 40 次同一首
    def one_play():
        request(env, "POST", "/api/play",
                {"item": {"platform": "kuwo", "id": "concplay", "name": "C",
                          "duration": 100},
                 "quality": {}, "completed": True})
    ths = [threading.Thread(target=one_play) for _ in range(40)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    data, _h, _r, _e = get_store(env)
    rec = (data.get("plays") or {}).get("kuwo_concplay") or {}
    day = time.strftime("%Y-%m-%d")
    n = (rec.get("days") or {}).get(day)
    ok("S3", "并发 40 次全部计入", n == 40, "40", n)

    # 大量不同曲目（data.json 仍严格 JSON）
    for i in range(120):
        request(env, "POST", "/api/play",
                {"item": {"platform": "kuwo", "id": "bulk%d" % i, "name": "B%d" % i,
                          "artist": "A", "duration": 60}, "completed": True})
    data, _h, raw, _e = get_store(env)
    try:
        strict_loads(raw)
        strict = True
    except Exception:
        strict = False
    ok("S3", "120 首批量记录后仍是严格 JSON", strict, "无 NaN/Infinity", strict)
    st, hd, raw, el = request(env, "GET", "/api/stats/summary")
    d = strict_loads(raw)
    ok("S3", "统计接口可读且总量 ≥120", st == 200 and (d.get("data", {}).get("plays_7d") or 0) >= 122,
       "≥122", d.get("data", {}).get("plays_7d"))


# --------------------------------------------------------------------------
# S4 下载队列
# --------------------------------------------------------------------------
def group_s4(env):
    st, hd, raw, el = request(env, "POST", "/api/downloads/add", {})
    d = strict_loads(raw)
    ok("S4", "空请求不建任务", st == 200 and d.get("ok") is False, "ok:false",
       "ok=%s" % d.get("ok"))
    st, hd, raw, el = request(env, "POST", "/api/downloads/add",
                              {"items": [{"name": "no id"}, {"platform": "kuwo"}]})
    d = strict_loads(raw)
    ok("S4", "无平台/id 不建任务", st == 200 and d.get("ok") is False, "ok:false",
       "ok=%s" % d.get("ok"))
    st, hd, raw, el = request(env, "POST", "/api/downloads/add", {"items": {"a": 1}})
    d = strict_loads(raw)
    ok("S4", "items 非数组受控", st == 200 and d.get("ok") is False, "ok:false",
       "ok=%s" % d.get("ok"))
    st, hd, raw, el = request(env, "GET", "/api/downloads")
    d = strict_loads(raw)
    ok("S4", "下载快照形状", st == 200 and isinstance(d.get("data", {}).get("tasks"), list)
       and isinstance(d.get("data", {}).get("concurrency"), int),
       "tasks[] + concurrency", d.get("data"))
    for path in ("cancel", "retry", "remove"):
        st, hd, raw, el = request(env, "POST", "/api/downloads/" + path, {"id": "nope"})
        d = strict_loads(raw)
        ok("S4", "%s 未知任务返回 ok:false" % path,
           st == 200 and d.get("ok") is False, "ok:false", "ok=%s" % d.get("ok"))


# --------------------------------------------------------------------------
# S5 接口模糊
# --------------------------------------------------------------------------
GET_PATHS = [
    "/api/store", "/api/store/export", "/api/works", "/api/downloads",
    "/api/stats/summary", "/api/taste", "/api/genres", "/api/cache",
    "/api/sync/status", "/api/import/status", "/api/lyric/now",
    "/api/local/list", "/api/local/file", "/api/local/cover",
    "/api/media", "/api/works/file", "/api/does-not-exist",
]
QUERY_FUZZ = [
    "", "?p=", "?p=%00", "?p=..%2f..%2f..%2fetc%2fpasswd",
    "?p=" + "a" * 4000, "?p=%E7%81%AB" * 300, "?p=-1", "?p=1e999",
    "?p=a&p=b", "?p=%zz", "?id=../evil", "?key=x&url=file:///etc/passwd",
]
POST_FUZZ = [
    ("/api/store", {}),
    ("/api/store", {"op": "nope", "payload": {}}),
    ("/api/store", {"op": "settings", "payload": None}),
    ("/api/eq/parse", {}),
    ("/api/eq/parse", {"text": "Filter 1: ON PK Fc 100 Hz Gain 3 dB Q 1"}),
    ("/api/works/delete", {"id": "../evil"}),
    ("/api/import/preview", {}),
    ("/api/import/preview", {"text": ""}),
    ("/api/import/cancel", {"id": "x"}),
    ("/api/import/commit", {}),
    ("/api/feedback", {}),
    ("/api/taste_note", {"text": None}),
    ("/api/taste_genres", {"ids": "pop"}),
    ("/api/probe", {"items": []}),
    ("/api/probe", {"items": "x"}),
    ("/api/play", {}),
    ("/api/lyric/now", {}),
    ("/api/downloads/cancel", {}),
    ("/api/downloads/retry", {}),
    ("/api/downloads/remove", {}),
    ("/api/cache/clear", {"tier": "bogus"}),
    ("/api/save-lyric", {"name": "n", "artist": "a", "content": ""}),
    ("/api/update/download", {"url": "file:///etc/passwd"}),
    ("/api/rpc", {"fn": "nope", "params": {}}),
    ("/api/rpc", {"fn": "getlyric", "params": {}}),
    ("/api/rpc", {"fn": "getlyric", "params": []}),
    ("/api/sync/config", {}),
    ("/api/sync/test", {}),
    ("/api/sync/pull", {}),
]
BODY_VARIANTS = [
    None, {}, [], 123, "string", True,
    {"x": "A" * 100000},
    {"a": [{"b": [{"c": [1, 2, 3]}]}]},
]


def group_s5(env):
    fails = []
    n = 0
    for path in GET_PATHS:
        for qs in QUERY_FUZZ:
            n += 1
            _r, err = json_ok(env, "", "GET", path + qs, timeout=15)
            if err:
                fails.append("%s%s -> %s" % (path, qs[:40], err))
    ok("S5", "GET 端点畸形 query 不 5xx/不脏 JSON", not fails,
       "%d 例全部受控" % n, "fail=%d %s" % (len(fails), fails[:3]))

    fails = []
    n = 0
    for path, base in POST_FUZZ:
        variants = BODY_VARIANTS if path in ("/api/store", "/api/play", "/api/probe",
                                             "/api/feedback", "/api/save-lyric") else [base]
        if base not in variants:
            variants = [base] + variants
        for body in variants:
            n += 1
            _r, err = json_ok(env, "", "POST", path, body=body, timeout=20)
            if err:
                fails.append("%s %s -> %s" % (path, repr(body)[:40], err))
    ok("S5", "POST 端点畸形 body 不 5xx/不脏 JSON", not fails,
       "%d 例全部受控" % n, "fail=%d %s" % (len(fails), fails[:3]))
    ok("S5", "模糊测试后服务存活", alive(env), "alive", alive(env))
    data, _h, raw, _e = get_store(env)
    try:
        strict_loads(raw)
        strict = True
    except Exception:
        strict = False
    ok("S5", "模糊测试后 data.json 仍是严格 JSON", strict, "ok", strict)


# --------------------------------------------------------------------------
# S6 脏数据启动
# --------------------------------------------------------------------------
DIRTY = {
    "settings": {
        "cache_min_plays": "abc", "cache_week_plays": None,
        "download_dir": {"x": 1}, "webdav": [1, 2],
        "eq_gains": [0, float("nan"), 3], "volume": "loud",
    },
    "favorites": [1, None, "x", {"kind": "song", "id": "ok", "platform": "kuwo"}],
    "playlists": [1, {"id": "p1", "name": "N", "items": [2, {"platform": "kuwo", "id": "1"}]}],
    "recent": [1, {"platform": "kuwo", "id": "r"}],
    "daily": {"2026-11-13": 5, "2026-11-12": {"n": "x", "s": float("inf")}},
    "plays": {
        "k1": {"days": {"2026-11-13": "7"}, "meta": 5},
        "k2": {"days": [1], "meta": {"artist": "A", "cover": 2}},
        "k3": {"days": {"2026-11-13": 2}, "meta": {"artist": "B", "name": "S"}},
    },
    "hours": [],
    "works": [1],
    "downloads": [{"x": 1}],
    "feedback": {"a": 1, "b": {"artist": "A", "verdict": "like"}},
    "taste_genres": "pop",
    "loudness": {},
    "eqpresets": {"p": {"bands": [{"f": 100, "q": 1, "db": 1}]}},
}


def group_s6():
    base, home = mkhome("dirty", DIRTY)
    env = spawn(home, "dirty")
    if env.get("dead"):
        ok("S6", "脏 data.json 可启动", False, "启动", env["dead"][-300:])
        return
    ok("S6", "脏 data.json 可启动", True, "启动", "boot=%ss" % env.get("boot"))
    try:
        for name, path in (("store", "/api/store"), ("stats", "/api/stats/summary"),
                           ("taste", "/api/taste"), ("cache", "/api/cache"),
                           ("sync", "/api/sync/status"), ("works", "/api/works")):
            _r, err = json_ok(env, "", "GET", path)
            ok("S6", "%s 接口对脏数据健壮" % name, err is None, "200 + 严格 JSON", err or "ok")
        st, hd, raw, el = request(env, "POST", "/api/play",
                                  {"item": {"platform": "kuwo", "id": "new", "name": "N",
                                            "duration": 10}, "completed": True})
        d = strict_loads(raw)
        ok("S6", "脏设置下收听仍可记录", d.get("ok") is True, "ok:true",
           "ok=%s" % d.get("ok"))
        st, d, _el = op(env, "fav_toggle",
                        {"kind": "song", "platform": "kuwo", "id": "new", "name": "N"})
        ok("S6", "脏收藏列表仍可追加", st == 200 and d.get("ok") is True, "ok:true",
           "ok=%s" % d.get("ok"))
        st, hd, raw, el = request(env, "GET", "/api/store/export")
        json_ok(env, "", "GET", "/api/store/export")
        ok("S6", "脏数据导出仍脱敏/可解析", st == 200, "200", st)
        data, _h, raw, _e = get_store(env)
        try:
            strict_loads(raw)
            strict = True
        except Exception:
            strict = False
        ok("S6", "脏数据 API 响应严格 JSON", strict, "无 NaN/Infinity", strict)
        ok("S6", "启动时把非法下载目录回落", isinstance(
            data.get("settings", {}).get("download_dir"), str),
           "字符串", type(data.get("settings", {}).get("download_dir")).__name__)
    finally:
        kill(env)


# --------------------------------------------------------------------------
# S7 并发 / 浸泡
# --------------------------------------------------------------------------
def group_s7(env):
    reset_store(env)
    errors = []
    lock = threading.Lock()

    def bump(exc):
        with lock:
            errors.append(exc)

    def worker(wid):
        for i in range(50):
            try:
                if i % 5 == 0:
                    st, _h, raw, _e = request(env, "GET", "/api/store")
                    if st != 200:
                        bump("GET store %s" % st)
                    else:
                        strict_loads(raw)
                elif i % 5 == 1:
                    st, hd, raw, _e = request(env, "POST", "/api/store", {
                        "op": "settings", "payload": {"volume": (wid * 7 + i) % 101}})
                    if st != 200:
                        bump("settings %s" % st)
                    strict_loads(raw)
                elif i % 5 == 2:
                    st, hd, raw, _e = request(env, "POST", "/api/store", {
                        "op": "fav_toggle",
                        "payload": {"kind": "song", "platform": "kuwo",
                                    "id": "w%d_%d" % (wid, i), "name": "X"}})
                    if st != 200:
                        bump("fav %s" % st)
                    strict_loads(raw)
                elif i % 5 == 3:
                    st, hd, raw, _e = request(env, "POST", "/api/store", {
                        "op": "history_add", "payload": {"q": "q%d_%d" % (wid, i)}})
                    strict_loads(raw)
                else:
                    st, hd, raw, _e = request(env, "GET", "/api/downloads")
                    if st != 200:
                        bump("downloads %s" % st)
                    strict_loads(raw)
            except Exception as e:                        # noqa: BLE001
                bump("%s: %s" % (type(e).__name__, e))

    ths = [threading.Thread(target=worker, args=(w,)) for w in range(16)]
    t0 = time.time()
    [t.start() for t in ths]
    [t.join() for t in ths]
    dt = time.time() - t0
    ok("S7", "16 线程 × 50 次混合读写", not errors, "0 错误",
       "%d 错误 %s / %.1fs" % (len(errors), errors[:3], dt))

    # 并发同键设置：最后落盘值必须是某次完整写入，不能是半截
    vals = []
    def set_vol(v):
        for _ in range(20):
            op(env, "settings", {"volume": v})
            vals.append(v)
    ths = [threading.Thread(target=set_vol, args=(v,)) for v in (10, 20, 30, 40)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    data, _h, _r, _e = get_store(env)
    ok("S7", "并发设置写入是完整值", data["settings"].get("volume") in vals,
       "10/20/30/40 之一", data["settings"].get("volume"))

    # 100 个并发 GET
    res = []
    def get_one():
        try:
            st, _h, raw, _e = request(env, "GET", "/api/store")
            strict_loads(raw)
            res.append(st)
        except Exception as e:                            # noqa: BLE001
            res.append(str(e))
    ths = [threading.Thread(target=get_one) for _ in range(100)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    ok("S7", "100 并发读全部成功", res.count(200) == 100, "100×200",
       "ok=%d err=%d" % (res.count(200), 100 - res.count(200)))

    # 5MB 请求体：store 端点应 413 快速拒绝
    big = b"{" + b'"x":"' + b"A" * (5 * 1024 * 1024) + b'"}'
    t0 = time.time()
    st, hd, raw, el = request(env, "POST", "/api/store", raw=big, timeout=20)
    ok("S7", "5MB 写入被 413 拒绝且不卡", st == 413 and el < 5, "413 / <5s",
       "%s / %.2fs" % (st, el))
    ok("S7", "大请求后服务存活", alive(env), "alive", alive(env))

    # 突发 RST 连接（模拟切歌/关窗）不拖垮服务
    bad = 0
    for _ in range(120):
        try:
            s = socket.create_connection(("127.0.0.1", env["port"]), timeout=2)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.sendall(b"GET /api/store HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            s.close()
        except OSError:
            bad += 1
    ok("S7", "120 次半开/RST 连接不拖垮服务", alive(env), "alive", "alive=%s bad=%d" % (alive(env), bad))

    # data.json 仍可严格解析
    data, _h, raw, _e = get_store(env)
    try:
        strict_loads(raw)
        strict = True
    except Exception:
        strict = False
    ok("S7", "并发后 data.json 严格 JSON", strict, "ok", strict)
    # 收藏无重复键
    keys = [(f.get("kind"), f.get("id"), f.get("platform"))
            for f in data.get("favorites") or []]
    ok("S7", "并发后收藏无重复键", len(keys) == len(set(keys)), "唯一",
       "%d / %d" % (len(keys), len(set(keys))))


# --------------------------------------------------------------------------
# S8 重启 / 导出
# --------------------------------------------------------------------------
def group_s8(env):
    # 造可识别的数据
    op(env, "favorites_clear", {})
    op(env, "fav_toggle", {"kind": "song", "platform": "kuwo", "id": "persist", "name": "P"})
    op(env, "playlist_create", {"name": "持久歌单"})
    data, _h, _r, _e = get_store(env)
    pid = data["playlists"][-1]["id"]
    op(env, "playlist_add", {"id": pid, "items": [
        {"platform": "kuwo", "id": "1", "name": "One"},
        {"platform": "wyy", "id": "2", "name": "Two"}]})
    op(env, "settings", {"volume": 42})
    op(env, "history_add", {"q": "persist", "platform": "kuwo"})

    # 导出脱敏
    st, hd, raw, el = request(env, "GET", "/api/store/export")
    d = strict_loads(raw)
    export = d.get("data") or {}
    ok("S8", "导出成功", st == 200 and d.get("ok") is not False, "200", st)
    ok("S8", "导出不含 webdav/works", "webdav" not in (export.get("settings") or {})
       and "works" not in export, "已剔除", [k for k in ("webdav", "works") if k in export])
    ok("S8", "导出下载目录脱敏",
       (export.get("settings") or {}).get("download_dir") == "<本机音乐目录>",
       "<本机音乐目录>", (export.get("settings") or {}).get("download_dir"))

    # 重启
    kill(env)
    env2 = spawn(env["home"], "restart")
    ok("S8", "重启成功", not env2.get("dead"), "启动", env2.get("dead") or "boot=%ss" % env2.get("boot"))
    if env2.get("dead"):
        return env
    try:
        data, _h, raw, _e = get_store(env2)
        ok("S8", "收藏跨重启保留",
           any(f.get("id") == "persist" for f in data.get("favorites") or []),
           "persist", [f.get("id") for f in data.get("favorites") or []])
        pl = next((p for p in data.get("playlists") or [] if p.get("name") == "持久歌单"), None)
        ok("S8", "歌单跨重启保留且 2 首", pl is not None and len(pl.get("items") or []) == 2,
           "持久歌单 / 2 首", (pl or {}).get("name"))
        ok("S8", "设置跨重启保留", data.get("settings", {}).get("volume") == 42, "42",
           data.get("settings", {}).get("volume"))
        ok("S8", "历史跨重启保留",
           any(h.get("q") == "persist" for h in data.get("history") or []),
           "persist", [h.get("q") for h in data.get("history") or []])
        # 落盘文件本身严格 JSON
        path = data.get("_data_path") or os.path.join(env2["home"], "data.json")
        txt = open(path, "r", encoding="utf-8-sig").read()
        bad_tokens = [t for t in ("NaN", "Infinity", "-Infinity") if t in txt]
        ok("S8", "data.json 无非法 JSON 字面量", not bad_tokens, "无", bad_tokens)
        # 唯一键
        keys = [(f.get("kind"), f.get("id"), f.get("platform"))
                for f in data.get("favorites") or []]
        ok("S8", "落盘收藏无重复键", len(keys) == len(set(keys)), "唯一",
           "%d / %d" % (len(keys), len(set(keys))))
        return env2
    finally:
        pass


# --------------------------------------------------------------------------
# S9 静态资源 / 前端源码
# --------------------------------------------------------------------------
def group_s9(env):
    assets = ["/", "/index.html", "/app.js", "/style.css", "/window-chrome.css",
              "/window-chrome.js", "/lyrics.html", "/logo.png"]
    fails = []
    for a in assets:
        st, hd, raw, el = request(env, "GET", a)
        if st != 200 or not raw:
            fails.append("%s -> %s/%dB" % (a, st, len(raw)))
    ok("S9", "静态资源全部 200", not fails, "8/8", fails or "8/8")

    # index.html 引用的本地资源都能取到
    st, hd, raw, el = request(env, "GET", "/index.html")
    html = raw.decode("utf-8", "replace")
    refs = re.findall(r'(?:src|href)="(/[^"]+)"', html)
    refs = [r for r in dict.fromkeys(refs) if not r.startswith("/api/")]
    miss = []
    for r in refs:
        st, _h, _b, _e = request(env, "GET", r)
        if st != 200:
            miss.append("%s->%s" % (r, st))
    ok("S9", "HTML 引用的本地资源可达", not miss, "全部 200", miss or "%d 个全通" % len(refs))

    # 重复 id：会导致 getElementById 只命中第一个，手风琴/抽屉开关错位
    ids = re.findall(r'\bid="([^"]+)"', html)
    dup = sorted({x for x in ids if ids.count(x) > 1})
    ok("S9", "index.html 无重复 id", not dup, "无重复", dup or "无")

    # JS 语法检查（node 可用时）
    node = shutil.which("node")
    if not node:
        warn("S9", "JS 语法检查", False, "node 可用", "未安装 node")
    else:
        bad = []
        for f in ("web/app.js", "web/window-chrome.js", "web/lyrics.html"):
            p = os.path.join(REPO, f)
            if f.endswith(".js"):
                r = subprocess.run([node, "--check", p], capture_output=True, text=True)
                if r.returncode != 0:
                    bad.append("%s: %s" % (f, (r.stderr or "").strip().splitlines()[:2]))
            else:
                # 内联脚本粗略检查：抽出 <script> 块交给 node --check
                src = open(p, encoding="utf-8").read()
                blocks = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
                for i, b in enumerate(blocks):
                    tmp = os.path.join(TMP_ROOT, "inline_%d.js" % i)
                    open(tmp, "w", encoding="utf-8").write(b)
                    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
                    if r.returncode != 0:
                        bad.append("%s#%d: %s" % (f, i, (r.stderr or "").strip().splitlines()[:2]))
        ok("S9", "前端 JS 语法检查", not bad, "node --check 通过", bad or "通过")


# --------------------------------------------------------------------------
# S10 上游连接池 / 歌词缓存（beta5 新增路径的单元级验证）
# --------------------------------------------------------------------------
def group_s10():
    import importlib
    import http.client as hc
    home = os.path.join(TMP_ROOT, "unit", "home")
    os.makedirs(home, exist_ok=True)
    old_home = os.environ.get("ELYSIUM_HOME")
    os.environ["ELYSIUM_HOME"] = home
    for mod in ("main",):
        sys.modules.pop(mod, None)
    main = importlib.import_module("main")
    try:
        # ---- 连接池：池里旧连接被对端关掉 → 自动换新重试一次
        made = []

        class FakeResp:
            def __init__(self, body, status=200):
                self.body, self.status = body, status

            def read(self):
                return self.body

        class FakeConn:
            def __init__(self, host=None, timeout=None):
                self.host, self.closed = host, False
                self.fail = False
                made.append(self)

            def request(self, *a, **kw):
                if self.fail:
                    raise OSError("connection reset")

            def getresponse(self):
                return FakeResp(json.dumps({"code": 0, "data": {"ok": 1}}).encode())

            def close(self):
                self.closed = True

        old_conn = hc.HTTPSConnection
        hc.HTTPSConnection = FakeConn
        try:
            dead = FakeConn()
            dead.fail = True
            main._UP_IDLE = __import__("queue").LifoQueue(maxsize=8)
            main._UP_IDLE.put_nowait(dead)
            got = main.upstream("search", {"keyword": "x"})
            ok("S10", "连接池坏连接自动重试", got == {"ok": 1},
               "{'ok':1}", got)
            ok("S10", "坏连接被关闭", dead.closed, "closed", dead.closed)
            ok("S10", "成功连接归还池", main._UP_IDLE.qsize() == 1, "1",
               main._UP_IDLE.qsize())

            # 业务错误：HTTP 层已完整收包，连接应归还（还能继续用）
            class BizConn(FakeConn):
                def getresponse(self):
                    return FakeResp(json.dumps({"code": -3, "msg": "签名过期"}).encode())

            main._UP_IDLE = __import__("queue").LifoQueue(maxsize=8)
            hc.HTTPSConnection = BizConn
            try:
                main.upstream("getUrl", {"id": "1"})
                raised = False
            except RuntimeError as e:
                raised = "签名过期" in str(e)
            ok("S10", "业务错误原样上抛", raised, "RuntimeError 签名过期", raised)
            ok("S10", "业务错误后连接仍归还", main._UP_IDLE.qsize() == 1, "1",
               main._UP_IDLE.qsize())

            # 非 200 / 非 JSON
            class Http500(FakeConn):
                def getresponse(self):
                    return FakeResp(b"oops", 500)

            main._UP_IDLE = __import__("queue").LifoQueue(maxsize=8)
            hc.HTTPSConnection = Http500
            try:
                main.upstream("song", {"id": "1"})
                e1 = ""
            except RuntimeError as e:
                e1 = str(e)
            ok("S10", "上游 HTTP 500 明确报错", "500" in e1, "含 500", e1)

            class BadJson(FakeConn):
                def getresponse(self):
                    return FakeResp(b"<html>")

            main._UP_IDLE = __import__("queue").LifoQueue(maxsize=8)
            hc.HTTPSConnection = BadJson
            try:
                main.upstream("song", {"id": "1"})
                e2 = ""
            except RuntimeError as e:
                e2 = str(e)
            ok("S10", "上游非 JSON 明确报错", "JSON" in e2, "含 JSON", e2)
        finally:
            hc.HTTPSConnection = old_conn
            main._UP_IDLE = __import__("queue").LifoQueue(maxsize=8)

        # ---- 歌词缓存：按歌名找到候选、双键缓存、空结果不缓存
        main._LYRIC_CACHE.clear()
        calls = {"search": 0, "lyric": 0, "lyric_text": "[00:01]晴"}

        def fake_upstream(act, params):
            if act == "search":
                calls["search"] += 1
                return {"list": [{"id": "9", "name": "晴天", "artist": "周杰伦",
                                  "time": "t", "sign": "s"}]}
            if act == "getLyric":
                calls["lyric"] += 1
                return calls["lyric_text"]
            raise AssertionError(act)

        old_up = main.upstream
        main.upstream = fake_upstream
        try:
            r1 = main.lyric_lookup({"platform": "kuwo", "name": "晴天",
                                    "artist": "周杰伦"})
            c1 = dict(calls)
            r2 = main.lyric_lookup({"platform": "kuwo", "name": "晴天",
                                    "artist": "周杰伦"})
            c2 = dict(calls)
            ok("S10", "本地条目按歌名取词", r1 == "[00:01]晴", "[00:01]晴", r1)
            ok("S10", "歌词二次命中缓存", c1 == c2 and c2["lyric"] == 1,
               "不再打上游", "%s -> %s" % (c1, c2))
            r3 = main.lyric_lookup({"platform": "kuwo", "id": "9",
                                    "time": "t", "sign": "s"})
            ok("S10", "同曲 id 键共享缓存", r3 == r1 and calls["lyric"] == 1,
               "不再打上游", calls["lyric"])

            # 空歌词不缓存：上游恢复后还能再取
            main._LYRIC_CACHE.clear()
            calls.update(search=0, lyric=0, lyric_text="")
            a = main.lyric_lookup({"platform": "kuwo", "name": "晴天", "artist": "周杰伦"})
            b = main.lyric_lookup({"platform": "kuwo", "name": "晴天", "artist": "周杰伦"})
            ok("S10", "空歌词不缓存", a == "" and b == "" and calls["lyric"] == 2,
               "两次都打上游", calls["lyric"])

            # dict 形状 {"lrc": ...}
            main._LYRIC_CACHE.clear()
            calls.update(search=0, lyric=0)
            main.upstream = lambda act, p: (
                {"list": [{"id": "9", "name": "晴天", "artist": "周杰伦",
                           "time": "t", "sign": "s"}]} if act == "search"
                else {"lrc": "[00:02]x"})
            r = main.lyric_lookup({"platform": "kuwo", "name": "晴天", "artist": "周杰伦"})
            ok("S10", "dict 形状歌词取 lrc", r == "[00:02]x", "[00:02]x", r)

            # 找不到候选 → 空，且不写入缓存（下次还会再找）
            main._LYRIC_CACHE.clear()
            n = {"search": 0}
            def no_cand(act, p):
                if act == "search":
                    n["search"] += 1
                    return {"list": []}
                raise AssertionError(act)
            main.upstream = no_cand
            z1 = main.lyric_lookup({"platform": "kuwo", "name": "不存在", "artist": ""})
            z2 = main.lyric_lookup({"platform": "kuwo", "name": "不存在", "artist": ""})
            ok("S10", "无候选返回空且不缓存", z1 == z2 == "" and n["search"] == 2,
               "两次搜索", n["search"])

            # 缓存上限：超 300 条裁掉最旧
            main._LYRIC_CACHE.clear()
            for i in range(305):
                main._lyric_cache_put(("k", i), "x")
            ok("S10", "歌词缓存有上限", len(main._LYRIC_CACHE) <= 300,
               "≤300", len(main._LYRIC_CACHE))
        finally:
            main.upstream = old_up

        # ---- 直链缓存：同键 3 分钟内复用
        main._RPC_CACHE.clear()
        cnt = {"n": 0}
        old_res = main._resolve_play_url
        main._resolve_play_url = lambda *a, **k: (
            cnt.__setitem__("n", cnt["n"] + 1) or {"url": "http://x/%d" % cnt["n"]})
        try:
            u1 = main.resolve_play_url("kuwo", "1", "mp3", "320", "t", "s", "n")
            u2 = main.resolve_play_url("kuwo", "1", "mp3", "320", "t", "s", "n")
            u3 = main.resolve_play_url("kuwo", "1", "mp3", "320", "t", "s", "n2")
            ok("S10", "同直链请求复用缓存", u1 == u2 and cnt["n"] == 2,
               "第 1/2 次同值，不同 name 才重取", "n=%s %s/%s" % (cnt["n"], u1, u3))
        finally:
            main._resolve_play_url = old_res
    finally:
        # 恢复环境，避免影响后续组（本组一般最后跑）
        if old_home is None:
            os.environ.pop("ELYSIUM_HOME", None)
        else:
            os.environ["ELYSIUM_HOME"] = old_home


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def report(elapsed):
    groups = {}
    for r in RESULTS:
        g = groups.setdefault(r["group"], {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0})
        g[r["verdict"]] += 1
    lines = ["# Elysium 压力/逻辑测试报告", "",
             "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "- 用时：%.1fs" % elapsed,
             "- Python：%s" % sys.version.split()[0],
             "- 范围：本地 HTTP API + data.json + 并发；不含真实上游接口与 UI", "",
             "## 汇总", "", "| 组 | 覆盖 | PASS | FAIL | WARN | SKIP |",
             "| --- | --- | --- | --- | --- | --- |"]
    totals = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for g in sorted(groups):
        c = groups[g]
        n = sum(c.values())
        for k in totals:
            totals[k] += c[k]
        lines.append("| %s | %d | %d | %d | %d | %d |" % (
            g, n, c["PASS"], c["FAIL"], c["WARN"], c["SKIP"]))
    n = sum(totals.values())
    lines.append("| **合计** | **%d** | **%d** | **%d** | **%d** | **%d** |" % (
        n, totals["PASS"], totals["FAIL"], totals["WARN"], totals["SKIP"]))
    lines += ["", "**结论**：%d 项符合预期，%d 项失败，%d 项可改进。" % (
        totals["PASS"], totals["FAIL"], totals["WARN"]), "", "## 明细", "",
        "| 结果 | 组 | 用例 | 期望 | 实际 |", "| --- | --- | --- | --- | --- |"]
    for r in RESULTS:
        lines.append("| %s | %s | %s | %s | %s |" % (
            r["verdict"], r["group"], r["name"].replace("|", "\\|"),
            str(r["expect"]).replace("|", "\\|"),
            str(r["actual"]).replace("|", "\\|")))
    return "\n".join(lines)


def main():
    global TMP_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}

    def want(g):
        return not only or g in only

    TMP_ROOT = tempfile.mkdtemp(prefix="elysium_stress_")
    t0 = time.time()
    base, home = mkhome("main")
    env = spawn(home, "main")
    if env.get("dead"):
        print("[FATAL] 服务没起来：\n" + env["dead"])
        return 2
    print("[env] port=%s boot=%ss tmp=%s" % (env["port"], env.get("boot"), TMP_ROOT),
          flush=True)
    # 下载目录必须落在临时 home 里：默认值指向用户真实 Music/Elysium，
    # save-lyric / 更新下载会往真实目录写文件。
    dl = os.path.join(home, "dl")
    os.makedirs(dl, exist_ok=True)
    op(env, "settings", {"download_dir": dl})
    try:
        if want("S1"):
            group_s1(env)
        if want("S2"):
            group_s2(env)
        if want("S3"):
            group_s3(env)
        if want("S4"):
            group_s4(env)
        if want("S5"):
            group_s5(env)
        if want("S6"):
            group_s6()
        if want("S7"):
            group_s7(env)
        if want("S8"):
            env = group_s8(env)
        if want("S9"):
            group_s9(env)
        if want("S10"):
            group_s10()
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()
        record("XX", "未捕获异常", "FAIL", "无", traceback.format_exc(limit=3))
    finally:
        kill(env)

    elapsed = time.time() - t0
    rep = report(elapsed)
    out = os.path.join(REPO, "stress_report.md")
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
    else:
        print("[keep] " + TMP_ROOT)
    fails = sum(1 for r in RESULTS if r["verdict"] == "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
