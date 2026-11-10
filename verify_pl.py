# -*- coding: utf-8 -*-
"""Elysium Stage F 验收：用户自建歌单。

覆盖 _art/STAGE_CF_DESIGN_2026-11-09.md §4 的 F 组断言：
  F1 建歌单 → 侧栏出现、落盘
  F2 加歌 → 歌单详情页能渲染曲目、落盘
  F3 加歌不写 favorites（两套数据独立）
  F4 改名 / 删除单曲 / 删除歌单
  F5 重启后数据保留（持久化）

用隔离实例跑（--home 指向临时目录），不碰真实用户数据。
用法：
    python verify_pl.py --port 18779
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
import urllib.request

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + detail) if detail else ""))
    return bool(ok)


def make_driver():
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1440,900")
    o.add_argument("--lang=zh-CN")
    d = webdriver.Chrome(options=o)
    d.set_window_size(1440, 900)
    d.set_page_load_timeout(45)
    return d


def js(d, script, *args):
    return d.execute_script(script, *args)


def api(base, op, payload=None):
    req = urllib.request.Request(
        base + "/api/store", method="POST",
        data=json.dumps({"op": op, "payload": payload or {}}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def store(base):
    with urllib.request.urlopen(base + "/api/store", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_instance(home, repo):
    port = free_port()
    env = dict(os.environ)
    env["ELYSIUM_HOME"] = home
    logf = open(os.path.join(home, "_server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(repo, "main.py"), "--serve", "--port=%d" % port],
        cwd=repo, env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return base, proc
        except OSError:
            time.sleep(0.25)
    proc.terminate()
    return None, proc


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# 产品早已用自绘输入浮层 askLayer 替代原生 prompt()：
# 覆盖 window.prompt 不会再有任何效果，这里走真实 UI 路径。
CREATE_JS = """
  document.getElementById('plAdd').click();
  document.getElementById('askInput').value = '深夜通勤';
  document.getElementById('askOk').click();
  return 1;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", default="_art")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    home = tempfile.mkdtemp(prefix="elysium_pl_")
    shutil.copytree(os.path.join(a.repo, "web"), os.path.join(home, "web"))
    base, proc = start_instance(home, a.repo)
    if not base:
        check("实例启动", False, "起不来")
        shutil.rmtree(home, ignore_errors=True)
        return 1

    try:
        d = make_driver()
        try:
            d.get(base + "/")
            time.sleep(2.5)

            # ---------------------------------------------------------- F1 建歌单
            fav_before = len(store(base)["favorites"])
            js(d, CREATE_JS)
            time.sleep(1.5)
            pls = store(base)["playlists"]
            check("F1 建歌单落盘", len(pls) == 1 and pls[0]["name"] == "深夜通勤",
                  "playlists=%s" % [(p["name"], len(p["items"])) for p in pls])
            side_n = js(d, "return document.querySelectorAll('#sidePlaylists .side-item').length")
            check("F1 侧栏出现歌单", side_n == 1, "side items=%d" % side_n)
            pid = pls[0]["id"] if pls else ""

            # ---------------------------------------------------------- F2 加歌
            api(base, "playlist_add", {"id": pid, "items": [
                {"platform": "kuwo", "id": "111", "name": "Midnight",
                 "artist": "大森元貴", "album_name": "Midnight", "duration": 214},
                {"platform": "wyy", "id": "222", "name": "灰色", "artist": "大森元貴"},
            ]})
            d.get(base + "/")
            time.sleep(2.0)
            # 侧栏点进歌单
            js(d, "document.querySelector('#sidePlaylists .side-item').click(); return 1")
            time.sleep(1.5)
            rows = js(d, "return document.querySelectorAll('#view .tr').length")
            titles = js(d, "return [...document.querySelectorAll('#view .tr-name')].map(e=>e.textContent.trim())")
            check("F2 歌单详情页渲染曲目", rows == 2, "rows=%d titles=%s" % (rows, titles))
            check("F2 曲目标题正确", titles == ["Midnight", "灰色"], "titles=%s" % titles)

            # ---------------------------------------------------------- F3 两套独立
            fav_now = len(store(base)["favorites"])
            check("F3 加歌不写 favorites", fav_now == fav_before,
                  "fav_before=%d fav_now=%d" % (fav_before, fav_now))
            api(base, "fav_toggle", {"kind": "song", "platform": "kuwo",
                                     "id": "111", "name": "Midnight"})
            pl_after = len(store(base)["playlists"][0]["items"])
            check("F3 收藏不写歌单", pl_after == 2, "playlist items=%d" % pl_after)

            # ---------------------------------------------------------- F4 改名/删歌/删单
            api(base, "playlist_rename", {"id": pid, "name": "通勤路"})
            check("F4 改名生效", store(base)["playlists"][0]["name"] == "通勤路",
                  store(base)["playlists"][0]["name"])
            api(base, "playlist_remove", {"id": pid, "platform": "kuwo", "sid": "111"})
            left = [i["id"] for i in store(base)["playlists"][0]["items"]]
            check("F4 移除单曲", left == ["222"], "items=%s" % left)
            api(base, "playlist_delete", {"id": pid})
            check("F4 删除歌单", len(store(base)["playlists"]) == 0,
                  "playlists=%d" % len(store(base)["playlists"]))

            # ---------------------------------------------------------- F5 持久化
            api(base, "playlist_create", {"name": "重启后还在"})
            api(base, "playlist_add", {"id": store(base)["playlists"][0]["id"],
                                       "item": {"platform": "kuwo", "id": "333",
                                                "name": "Persistent", "artist": "X"}})
            d.save_screenshot(os.path.join(a.out, "_v_pl_side.png"))
        finally:
            try:
                d.quit()
            except Exception:
                pass

        # 重启实例验证持久化
        stop(proc)
        base2, proc2 = start_instance(home, a.repo)
        try:
            pls2 = store(base2)["playlists"]
            check("F5 重启后数据保留",
                  len(pls2) == 1 and pls2[0]["name"] == "重启后还在"
                  and len(pls2[0]["items"]) == 1
                  and pls2[0]["items"][0]["name"] == "Persistent",
                  json.dumps(pls2, ensure_ascii=False)[:180])
        finally:
            stop(proc2)

    finally:
        stop(proc)
        shutil.rmtree(home, ignore_errors=True)

    print("\n---- 汇总 ----")
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("  OK   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    print("  %d/%d 通过" % (ok_n, len(RESULTS)))
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
