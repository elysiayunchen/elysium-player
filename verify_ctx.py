# -*- coding: utf-8 -*-
"""Elysium Stage D 验收：右键菜单（仅新增「加入歌单…」）。

覆盖 _art/STAGE_CF_DESIGN_2026-11-09.md §4 的 D 组断言：
  D1 右键单曲弹出菜单，且只有「加入歌单…」一个动作
  D2 「加入歌单…」打开歌单选择面板
  D3 选择歌单后曲目真的加进去（落盘）
  D4 菜单在视口右下角时向内翻转，不溢出
  D5 点外部 / Esc 能关闭
  D6 右键不在可加对象上时不弹菜单（保留浏览器默认菜单）

用隔离实例跑（不碰真实用户数据）。
用法：
    python verify_ctx.py
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


def finish(a):
    print("\n---- 汇总 ----")
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("  OK   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    print("  %d/%d 通过" % (ok_n, len(RESULTS)))
    return 0 if ok_n == len(RESULTS) else 1


# 在指定坐标派发 contextmenu
CTX_JS = """
  const x = arguments[0], y = arguments[1], sel = arguments[2];
  const el = document.querySelector(sel);
  if (!el) return false;
  el.dispatchEvent(new MouseEvent('contextmenu', {
    bubbles:true, cancelable:true, clientX:x, clientY:y}));
  return true;
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", default="_art")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    home = tempfile.mkdtemp(prefix="elysium_ctx_")
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

            # 准备：建两个歌单 + 注入一个确定的曲目列表
            api(base, "playlist_create", {"name": "深夜通勤"})
            api(base, "playlist_create", {"name": " workouts "})
            js(d, """
              state.list = [
                {id:'1', platform:'kuwo', name:'AAA', artist:'X', album_name:'Alb1'},
                {id:'2', platform:'kuwo', name:'BBB', artist:'X', album_name:'Alb1'},
              ];
              state.items = state.list;
              state.tabs.search = 'song';
              state.q = {keyword:'t', page:1, total:2};
              state.route = {name:'search'};
              return 1;
            """)
            time.sleep(0.4)
            # 前端的 state.playlists 必须同步 —— 面板只认 state，不读接口
            js(d, "return refreshStore()")
            time.sleep(1.0)
            js(d, "render(); return 1")
            time.sleep(0.6)
            n_pl = js(d, "return state.playlists.length")
            check("前置 前端歌单已同步", n_pl == 2, "state.playlists=%d" % n_pl)
            time.sleep(1.0)
            rows = js(d, "return document.querySelectorAll('#view .tr').length")
            check("前置 列表渲染 2 行", rows == 2, "rows=%d" % rows)

            # ---------------------------------------------------------- D1 弹出菜单
            has_el = js(d, "return !!document.querySelector('#ctxmenu')")
            if not has_el:
                check("D1 右键菜单容器存在", False, "#ctxmenu 不存在")
                d.save_screenshot(os.path.join(a.out, "_v_ctx.png"))
                return finish(a)
            js(d, CTX_JS, 400, 300, "#view .tr .tr-name")
            time.sleep(0.6)
            vis = js(d, "return !document.querySelector('#ctxmenu').hidden")
            acts = js(d, "return [...document.querySelectorAll('#ctxmenu button')].map(b=>b.dataset.act)")
            check("D1 右键单曲弹出菜单", vis, "visible=%s" % vis)
            check("D1 菜单只有「加入歌单…」一个动作", acts == ['ctx-add-pl'], "acts=%s" % acts)

            # ---------------------------------------------------------- D2 选择面板
            js(d, "document.querySelector('#ctxmenu button[data-act=ctx-add-pl]').click(); return 1")
            time.sleep(0.5)
            picks = js(d, "return [...document.querySelectorAll('#ctxmenu button[data-act=pl-pick]')].map(b=>b.dataset.id)")
            names = js(d, "return [...document.querySelectorAll('#ctxmenu .qm-t')].map(e=>e.textContent.trim())")
            check("D2 打开歌单选择面板", len(picks) == 2, "picks=%d names=%s" % (len(picks), names))

            # ---------------------------------------------------------- D3 真的加进去
            # 右键的是第 1 行（AAA），所以只应加这一首
            pid = picks[0]
            js(d, "document.querySelector('#ctxmenu button[data-act=pl-pick]').click(); return 1")
            time.sleep(1.5)
            pl = [p for p in store(base)["playlists"] if p["id"] == pid][0]
            items = [(i["platform"], i["id"], i["name"]) for i in pl["items"]]
            check("D3 加入歌单落盘（只加被右键那一首）",
                  len(pl["items"]) == 1 and pl["items"][0]["name"] == "AAA",
                  "items=%s" % items)

            # ---------------------------------------------------------- D4 边界翻转
            js(d, CTX_JS, 1435, 895, "#view .tr .tr-name")
            time.sleep(0.6)
            box = js(d, """
              const m = document.querySelector('#ctxmenu');
              if (m.hidden) return null;
              const r = m.getBoundingClientRect();
              return {l:Math.round(r.left), t:Math.round(r.top),
                      r:Math.round(r.right), b:Math.round(r.bottom),
                      vw:window.innerWidth, vh:window.innerHeight};
            """)
            check("D4 右下角时菜单不溢出视口",
                  box is not None and box["r"] <= box["vw"] and box["b"] <= box["vh"]
                  and box["l"] >= 0 and box["t"] >= 0,
                  "box=%s" % box)

            # ---------------------------------------------------------- D5 关闭
            js(d, "document.body.click(); return 1")
            time.sleep(0.4)
            closed_out = js(d, "return document.querySelector('#ctxmenu').hidden")
            check("D5 点外部关闭", closed_out, "hidden=%s" % closed_out)

            js(d, CTX_JS, 400, 300, "#view .tr .tr-name")
            time.sleep(0.5)
            from selenium.webdriver.common.keys import Keys
            d.find_element("tag name", "body").send_keys(Keys.ESCAPE)
            time.sleep(0.4)
            closed_esc = js(d, "return document.querySelector('#ctxmenu').hidden")
            check("D5 Esc 关闭", closed_esc, "hidden=%s" % closed_esc)

            # ---------------------------------------------------------- D6 非目标不弹
            js(d, CTX_JS, 700, 700, "#view .vhead")
            time.sleep(0.5)
            not_shown = js(d, "return document.querySelector('#ctxmenu').hidden")
            check("D6 非可加对象上不弹菜单", not_shown, "hidden=%s" % not_shown)

            d.save_screenshot(os.path.join(a.out, "_v_ctx.png"))
        finally:
            try:
                d.quit()
            except Exception:
                pass
    finally:
        stop(proc)
        shutil.rmtree(home, ignore_errors=True)

    return finish(a)


if __name__ == "__main__":
    sys.exit(main())
