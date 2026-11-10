# -*- coding: utf-8 -*-
"""本地曲库管理（P7）验收：多目录 / id 兼容 / 越界防护 / 增量 / 大库异步。

  L1 多目录       —— 额外目录加进来后两个目录的文件都在列表里
  L2 id 兼容      —— 0 号根（下载目录）的 id 仍是纯相对路径，老收藏不会失效
  L3 越界防护     —— @1/../ 之类穿目录的 id 一律拒绝
  L4 目录去重     —— 同一目录重复加、把下载目录当额外目录，都不该生效
  L5 增量签名     —— 签名没变走缓存；改动文件后重扫能看到变化
  L6 移除只解关联 —— 移除后列表不再含该目录，文件本身还在
  L7 大库异步     —— >800 文件时先秒级返回文件名占位，标签后台补齐
  L8 坏文件兜底   —— 假 mp3 不中断整个扫描，退化成文件名

用法：
    python verify_lib.py            # 跑完自动清理
    python verify_lib.py --keep     # 保留临时目录
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

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


def mk_mp3(path, title, freq=440, dur=0.5):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=%d:duration=%s:sample_rate=44100" % (freq, dur),
         "-ac", "1", "-b:a", "32k", "-metadata", "title=" + title,
         "-metadata", "artist=本机歌手", path], check=True)


def spawn(base_dir):
    home = os.path.join(base_dir, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        shutil.copytree(os.path.join(REPO, item), os.path.join(home, item),
                        ignore=shutil.ignore_patterns("__pycache__"))
    port = free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
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
    print(open(os.path.join(base_dir, "server.log"), "rb").read().decode("utf-8", "replace")[:1200])
    proc.terminate()
    raise SystemExit("isolated server never came up")


def api(base, path, body=None):
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def local_list(base, refresh=False):
    return api(base, "/api/local/list" + ("?refresh=1" if refresh else ""))["data"]


def head_status(base, path):
    """只看状态码：越界请求要 4xx，不能真把文件吐出来。"""
    try:
        with urllib.request.urlopen(base + path, timeout=15) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_lib_")
    dl = os.path.join(base_dir, "downloads")
    extra = os.path.join(base_dir, "my-music")
    big = os.path.join(base_dir, "big")
    mk_mp3(os.path.join(dl, "a440.mp3"), "下载曲")
    mk_mp3(os.path.join(extra, "sub", "b880.mp3"), "曲库曲")
    # 假 mp3：标签解析必失败，用来验证「单文件坏不中断整库」
    with open(os.path.join(extra, "broken.mp3"), "wb") as f:
        f.write(b"not really an mp3, just bytes" * 40)

    proc = None
    try:
        proc, base, home = spawn(base_dir)
        api(base, "/api/store", {"op": "settings", "payload": {"download_dir": dl}})

        # ---------------------------------------------------- L1 单目录基线
        d = local_list(base, refresh=True)
        check("L1a 默认只扫下载目录", [x["id"] for x in d["items"]] == ["a440.mp3"],
              "%s" % [x["id"] for x in d["items"]])
        check("L1b 下载目录文件的 id 是纯相对路径（老收藏兼容）",
              d["items"][0]["id"] == "a440.mp3", d["items"][0]["id"])
        check("L1c 根目录清单只有一个", d["roots"] == [dl], "%s" % d["roots"])

        # ---------------------------------------------------- L4 去重（加目录前先试重复）
        api(base, "/api/store", {"op": "settings", "payload": {"local_dirs": [dl, dl]}})
        d = local_list(base, refresh=True)
        check("L4a 下载目录当额外目录加也不会重复扫",
              len([x for x in d["items"] if x["id"].endswith("a440.mp3")]) == 1,
              "%s" % [x["id"] for x in d["items"]])

        # ---------------------------------------------------- L1 多目录
        api(base, "/api/store", {"op": "settings", "payload": {"local_dirs": [extra]}})
        d = local_list(base, refresh=True)
        ids = sorted(x["id"] for x in d["items"])
        check("L1d 两个目录的文件都在列表里", len(ids) == 3, "%s" % ids)
        check("L1e 额外目录的 id 带 @1/ 前缀",
              any(i.startswith("@1/") for i in ids), "%s" % ids)
        note("列表：%s · 根：%s" % (ids, d["roots"]))
        check("L8 坏文件不中断扫描，退化成文件名",
              any(x["name"] == "broken" for x in d["items"]),
              "%s" % [x["name"] for x in d["items"]])

        # ---------------------------------------------------- L3 越界
        st, _ = head_status(base, "/api/local/file?p=" + urllib.parse.quote("@1/../downloads/a440.mp3"))
        check("L3a @n/../ 穿目录被拒", st >= 400, "status=%s" % st)
        st, _ = head_status(base, "/api/local/file?p=" + urllib.parse.quote("@9/whatever.mp3"))
        check("L3b 不存在的根序号被拒", st >= 400, "status=%s" % st)
        st, n = head_status(base, "/api/local/file?p=" + urllib.parse.quote("@1/sub/b880.mp3"))
        check("L3c 合法 id 能正常取到流", st == 200 and n > 0, "status=%s bytes=%s" % (st, n))

        # ---------------------------------------------------- L5 增量
        d1 = local_list(base)
        check("L5a 签名没变时走缓存", d1.get("cached") is True, "cached=%s" % d1.get("cached"))
        later = time.time() + 5
        os.utime(os.path.join(dl, "a440.mp3"), (later, later))
        d2 = local_list(base, refresh=True)
        check("L5b 改动文件后重扫不再是缓存", d2.get("cached") is False,
              "cached=%s" % d2.get("cached"))

        # ---------------------------------------------------- L6 移除只解关联
        api(base, "/api/store", {"op": "settings", "payload": {"local_dirs": []}})
        d3 = local_list(base, refresh=True)
        check("L6a 移除后列表不再含该目录",
              not any("b880" in x["id"] for x in d3["items"]),
              "%s" % [x["id"] for x in d3["items"]])
        check("L6b 移除目录不删文件", os.path.isfile(os.path.join(extra, "sub", "b880.mp3")))

        # ---------------------------------------------------- L7 大库异步
        # 一个模板复制 N 份：标签都是「大库曲」，文件名各不相同 —— 正好能区分
        # 「首批的文件名占位」和「补完标签后的真名字」。
        tpl = os.path.join(base_dir, "tpl.mp3")
        mk_mp3(tpl, "大库曲")
        os.makedirs(big, exist_ok=True)
        for i in range(1200):
            shutil.copyfile(tpl, os.path.join(big, "file-%04d.mp3" % i))
        api(base, "/api/store", {"op": "settings", "payload": {"local_dirs": [big]}})
        t0 = time.time()
        d4 = local_list(base, refresh=True)
        first_ms = (time.time() - t0) * 1000
        sc = d4.get("scanning") or {}
        check("L7a 大库首批返回不带标签读（列表已可用）",
              len(d4["items"]) == 1201 and d4["items"][0]["duration"] in (0, None),
              "%d 项 · 首批 %.0fms" % (len(d4["items"]), first_ms))
        check("L7b 首批是秒级（< 3000ms）", first_ms < 3000, "%.0fms" % first_ms)
        check("L7c 返回时带扫描进度", "running" in sc and sc.get("total") == 1201,
              "%s" % sc)

        # 轮询到扫完，再看标签有没有补上
        done, waited = False, 0.0
        while waited < 120:
            time.sleep(0.6)
            waited += 0.6
            d5 = local_list(base)
            sc = d5.get("scanning") or {}
            if not sc.get("running"):
                done = True
                break
        named = [x for x in (d5["items"] if done else []) if x["name"] == "大库曲"]
        check("L7d 后台把标签补齐了", done and len(named) == 1200,
              "完成=%s 带标签=%d 耗时 %.1fs" % (done, len(named), waited))
        check("L7e 补完标签后列表仍是 1201 项", done and len(d5["items"]) == 1201,
              "%d 项" % len(d5.get("items") or []))

    finally:
        try:
            if proc:
                proc.terminate()
                proc.wait(timeout=10)
        except Exception:
            pass
        if args.keep:
            print("\n保留临时目录：" + base_dir)
        else:
            shutil.rmtree(base_dir, ignore_errors=True)

    ok = sum(1 for _, p, _ in RESULTS if p)
    print("\n== 结果：%d pass / %d fail ==" % (ok, len(RESULTS) - ok))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
