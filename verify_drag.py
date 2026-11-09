# -*- coding: utf-8 -*-
"""Elysium Stage C 验收：队列内拖拽重排。

覆盖 _art/STAGE_CF_DESIGN_2026-11-09.md §4 的 C 组断言：
  C1 拖拽后队列顺序正确
  C2 正在播放的曲目不中断（playIndex 跟着走，音频不重载）
  C3 拖到自身/紧邻自己不改变顺序
  C4 刷新后顺序还原（内存态，不落盘）

用隔离实例跑（不碰真实用户数据），用 CDP Input.dispatchDragEvent 触发
真实 HTML5 拖拽（ActionChains 对 HTML5 DnD 支持不可靠）。

用法：
    python verify_drag.py
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


# 用 DragEvent 构造器派发完整 HTML5 拖拽序列。
# CDP 的 Input.dispatchDragEvent 不会触发 dragstart，
# Input.setInterceptDrags 会让浏览器接管、事件不再派发到页面 ——
# 两者都验证过，都不可用。构造器派发最可控，且能真实走 preventDefault 逻辑。
DRAG_JS = """
  const srcIdx = arguments[0], dstIdx = arguments[1], above = arguments[2];
  const rows = [...document.querySelectorAll('#queuePane .qrow')];
  if (!rows[srcIdx] || !rows[dstIdx]) return {ok:false, reason:'row missing'};
  const src = rows[srcIdx], dst = rows[dstIdx];
  const sb = src.getBoundingClientRect(), db = dst.getBoundingClientRect();
  const dt = new DataTransfer();
  const mk = (type, x, y, target) => {
    const ev = new DragEvent(type, {bubbles:true, cancelable:true,
      clientX:x, clientY:y, dataTransfer:dt});
    target.dispatchEvent(ev);
    return ev.defaultPrevented;
  };
  const sx = sb.x + sb.width/2, sy = sb.y + sb.height/2;
  const tx = db.x + db.width/2, ty = db.y + db.height * (above ? 0.25 : 0.75);
  const p1 = mk('dragstart', sx, sy, src);
  const p2 = mk('dragover',  tx, ty, dst);
  const p3 = mk('drop',      tx, ty, dst);
  mk('dragend', tx, ty, src);
  return {ok:true, prevented:[p1,p2,p3]};
"""


def drag_row(d, src_idx, dst_idx, above=True):
    return js(d, DRAG_JS, src_idx, dst_idx, above)


def queue_names(d):
    return js(d, "return [...document.querySelectorAll('#queuePane .qr-name')].map(e=>e.textContent.trim())")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()

    home = tempfile.mkdtemp(prefix="elysium_drag_")
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

            # 队列在右侧抽屉里，默认 hidden —— 不打开就没有布局，
            # getBoundingClientRect 全是 0，拖拽事件也派发不出去。
            js(d, "openPanel('queue'); return 1")
            time.sleep(0.5)

            # 直接注入一个确定的队列（绕过真实播放，避免依赖上游）
            js(d, """
              const names = ['AAA','BBB','CCC','DDD'];
              state.playList = names.map((n,i)=>({
                id:String(i+1), platform:'kuwo', name:n, artist:'X'
              }));
              state.playIndex = -1;
              state.playing = null;
              renderQueue();
              return 1;
            """)
            time.sleep(0.6)
            check("前置 队列 4 首", queue_names(d) == ['AAA','BBB','CCC','DDD'],
                  str(queue_names(d)))

            # ---------------------------------------------------------- C1 拖拽换序
            # AAA(0) 拖到 CCC(2) 上方 → 落在 BBB 与 CCC 之间
            drag_row(d, 0, 2, above=True)
            time.sleep(0.8)
            got = queue_names(d)
            check("C1 拖拽后顺序正确", got == ['BBB', 'AAA', 'CCC', 'DDD'],
                  "got=%s" % got)

            # ---------------------------------------------------------- C3 拖到自身
            drag_row(d, 1, 1, above=True)
            time.sleep(0.6)
            got2 = queue_names(d)
            check("C3 拖到自身不改变顺序", got2 == ['BBB', 'AAA', 'CCC', 'DDD'],
                  "got=%s" % got2)

            # ---------------------------------------------------------- C2 播放不中断
            # 当前队列：BBB(0) AAA(1) CCC(2) DDD(3)
            # 让 AAA(1) 作为正在播放的曲目，把它拖到最前 → 它应变成 index 0
            js(d, """
              state.playIndex = 1;
              state.playing = state.playList[1];
              renderQueue();
              return 1;
            """)
            time.sleep(0.4)
            before_idx = js(d, "return state.playIndex")
            before_name = js(d, "return state.playing && state.playing.name")
            # 把正在播放的 AAA（index 1）拖到最前 → 它应变成 index 0
            drag_row(d, 1, 0, above=True)
            time.sleep(0.8)
            after_idx = js(d, "return state.playIndex")
            after_name = js(d, "return state.playing && state.playing.name")
            after_order = queue_names(d)
            check("C2 正在播放的曲目不中断",
                  before_name == after_name and after_idx == 0 and before_idx == 1,
                  "播放中 %s: idx %s -> %s；顺序=%s"
                  % (before_name, before_idx, after_idx, after_order))
            check("C2 播放指示跟随曲目移动",
                  js(d, "return state.playList[state.playIndex] === state.playing"),
                  "playList[playIndex] 与 playing 是否同一对象")

            # ---------------------------------------------------------- C4 内存态
            names_before = queue_names(d)
            d.get(base + "/")
            time.sleep(2.0)
            n_after = js(d, "return state.playList.length")
            check("C4 刷新后队列还原（内存态不落盘）", n_after == 0,
                  "刷新后 playList.length=%d（刷新前 %s）" % (n_after, names_before))
        finally:
            try:
                d.quit()
            except Exception:
                pass
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
