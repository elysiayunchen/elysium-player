# -*- coding: utf-8 -*-
"""Elysium UI 回归自检。

覆盖 _art/UI_OPTIMIZATION_2026-11-09.md 里 Stage A / B 的验收项：
  T1 实测规格分片探测 —— 40 条全部拿到结果，无永久骨架
  T2 幂等短路 —— 已有实测结果后重渲染不再重复请求 /api/probe
  T3 换源索引 —— 推荐卡不得出现指向 state.items 的「换源」按钮
  T4 兜底态 —— 拦截 /api/probe 后规格列落成静态「待测」，行不得标 dead
  T5 截图 —— 深/浅主题首页与搜索页

用法：
    python verify_ui.py [--base http://127.0.0.1:18777] [--out _art]
依赖：selenium + 本机 Chrome
"""
import argparse
import json
import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

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
    o.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    d = webdriver.Chrome(options=o)
    d.set_window_size(1440, 900)
    d.set_page_load_timeout(45)
    return d


def js(d, script, *args):
    return d.execute_script(script, *args)


# -------------------------------------------------- 独立实例（全新数据目录）
# 用于验证「全新安装」路径：默认主题、老用户偏好不被默认值覆盖。
# ELYSIUM_HOME 同时充当资源根与数据根（打包版约定），所以 web/ 也要放进去。
def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_fresh_instance(home=None):
    """起一个独立实例，数据目录全新。返回 (base_url, home_dir, process)。"""
    import shutil
    import socket
    import subprocess
    import tempfile

    repo = os.path.dirname(os.path.abspath(__file__))
    if home is None:
        home = tempfile.mkdtemp(prefix="elysium_fresh_")
    # web/ 必须也在资源根下，否则静态资源 404
    dst = os.path.join(home, "web")
    if not os.path.isdir(dst):
        shutil.copytree(os.path.join(repo, "web"), dst)
    port = _free_port()
    env = dict(os.environ)
    env["ELYSIUM_HOME"] = home
    logf = open(os.path.join(home, "_server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(repo, "main.py"), "--serve", "--port=%d" % port],
        cwd=repo, env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(40):                      # 等服务起来
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return base, home, proc
        except OSError:
            time.sleep(0.25)
    stop_instance(proc)
    return None


def stop_instance(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup_fresh(home):
    import shutil
    try:
        shutil.rmtree(home, ignore_errors=True)
    except Exception:
        pass


def store_theme_on(base):
    import urllib.request
    with urllib.request.urlopen(base + "/api/store", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))["settings"]["theme"]


def set_theme_api_on(base, v):
    import urllib.request
    req = urllib.request.Request(
        base + "/api/store", method="POST",
        data=json.dumps({"op": "settings", "payload": {"theme": v}}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


SPEC_JS = """
const specs = [...document.querySelectorAll('.tr-spec')];
const pick = (s) => s.querySelector('.chip');
return {
  specs: specs.length,
  skel:   specs.filter(s => { const c = pick(s); return c && c.classList.contains('probing'); }).length,
  wait:   specs.filter(s => { const c = pick(s); return c && c.classList.contains('wait'); }).length,
  dead:   specs.filter(s => { const c = pick(s); return c && c.classList.contains('dead'); }).length,
  real:   specs.filter(s => { const c = pick(s); return c && (c.classList.contains('flac')||c.classList.contains('hi')||c.classList.contains('lo')); }).length,
  bare:   specs.filter(s => !pick(s)).length,
  rows:   document.querySelectorAll('.tr').length,
};
"""


def wait_specs(d, timeout=120, poll=0.5):
    """等到规格列不再有骨架、也不再是空格子。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        st = js(d, SPEC_JS)
        key = (st["specs"], st["skel"], st["bare"])
        if key != last:
            print("      spec: rows=%d specs=%d skel=%d bare=%d wait=%d dead=%d real=%d"
                  % (st["rows"], st["specs"], st["skel"], st["bare"], st["wait"], st["dead"], st["real"]))
            last = key
        if st["specs"] and not st["skel"] and not st["bare"]:
            return st
        time.sleep(poll)
    return js(d, SPEC_JS)


def probe_req_count(d):
    n = 0
    try:
        for e in d.get_log("performance"):
            msg = json.loads(e["message"])["message"]
            if msg.get("method") == "Network.requestWillBeSent":
                if "/api/probe" in msg["params"]["request"]["url"]:
                    n += 1
    except Exception:
        pass
    return n


def do_search(d, kw):
    box = d.find_element(By.ID, "kw")
    box.clear()
    box.send_keys(kw)
    box.send_keys(Keys.ENTER)
    end = time.time() + 40
    while time.time() < end:
        if d.find_elements(By.CSS_SELECTOR, ".tr"):
            return True
        time.sleep(0.4)
    return False


def click_more(d):
    btns = d.find_elements(By.CSS_SELECTOR, '[data-act="more"]')
    if not btns:
        return False
    btns[0].click()
    return True


def set_theme(d, theme):
    """截图前显式定主题，避免受本机设置/媒体查询影响。"""
    js(d, "document.documentElement.dataset.theme=%s; return 1" % json.dumps(theme))
    time.sleep(0.4)


def shoot(d, path, theme=None):
    if theme:
        set_theme(d, theme)
    cur = js(d, "return document.documentElement.dataset.theme")
    d.save_screenshot(path)
    print("      shot -> %s  (theme=%s)" % (path, cur))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18777")
    ap.add_argument("--out", default="_art")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    d = make_driver()
    try:
        # ---------------------------------------------------------- T1 分片探测
        d.get(a.base + "/")
        time.sleep(2.5)
        check("T0 首页渲染", bool(d.find_elements(By.CSS_SELECTOR, ".shelf")), "")

        ok = do_search(d, "起风了")
        check("T0 搜索出结果", ok, "")
        time.sleep(1.5)
        st = wait_specs(d, timeout=180)
        check("T1 全部行拿到实测规格", st["bare"] == 0 and st["skel"] == 0,
              "rows=%d specs=%d skel=%d bare=%d real=%d wait=%d"
              % (st["rows"], st["specs"], st["skel"], st["bare"], st["real"], st["wait"]))
        check("T1 行数越过旧 32 条缺口", st["rows"] > 32, "rows=%d" % st["rows"])
        check("T1 有真实探测结果", st["real"] > 0, "real=%d" % st["real"])
        shoot(d, os.path.join(a.out, "_v_search_dark.png"), theme="dark")

        # ---------------------------------------------------------- T2 幂等短路
        # 主题按钮只换变量不重渲染，这里用「切 tab → 切回」触发真正的 render()
        probe_req_count(d)                       # 先把历史日志清空作为基线
        d.find_element(By.CSS_SELECTOR, '[data-act="stab"][data-tab="artist"]').click()
        time.sleep(1.2)
        d.find_element(By.CSS_SELECTOR, '[data-act="stab"][data-tab="song"]').click()
        time.sleep(2.5)
        new_req = probe_req_count(d)
        check("T2 重渲染不再重复探测", new_req == 0, "新增 /api/probe 请求 %d 次" % new_req)

        # ---------------------------------------------------------- T3 换源索引
        swap_reco = js(d, "return document.querySelectorAll('.card.reco [data-act=\"swap\"]').length")
        swap_rows = js(d, "return document.querySelectorAll('.tr [data-act=\"swap\"]').length")
        check("T3 推荐卡不生成换源按钮", swap_reco == 0, "reco=%d" % swap_reco)
        print("      (列表行换源按钮 %d 个，属正常：仅失效歌曲才给)" % swap_rows)

        # ---------------------------------------------------------- T5 首页截图
        js(d, "document.querySelector('[data-nav=\"home\"]')?.click(); "
              "window.scrollTo(0,0); return 1")
        time.sleep(2.5)
        shoot(d, os.path.join(a.out, "_v_home_dark.png"), theme="dark")
        shoot(d, os.path.join(a.out, "_v_home_light.png"), theme="light")
    finally:
        try:
            d.quit()
        except Exception:
            pass

    # ------------------------------------------------------------- T4 兜底态
    d2 = make_driver()
    try:
        d2.execute_cdp_cmd("Network.enable", {})
        d2.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*/api/probe*"]})
        d2.get(a.base + "/")
        time.sleep(2.0)
        do_search(d2, "起风了")
        time.sleep(8.0)
        st = js(d2, SPEC_JS)
        check("T4 拦截探测后无骨架残留", st["skel"] == 0,
              "skel=%d wait=%d bare=%d" % (st["skel"], st["wait"], st["bare"]))
        check("T4 落成静态待测态", st["wait"] == st["specs"] and st["specs"] > 0,
              "wait=%d specs=%d" % (st["wait"], st["specs"]))
        dead_rows = js(d2, "return document.querySelectorAll('.tr.dead').length")
        check("T4 未证实的行不标 dead", dead_rows == 0, "dead rows=%d" % dead_rows)
        shoot(d2, os.path.join(a.out, "_v_pending.png"), theme="dark")
    finally:
        try:
            d2.quit()
        except Exception:
            pass

    # ------------------------------------------------------- T5 探测中态外观
    d3 = make_driver()
    try:
        d3.execute_cdp_cmd("Network.enable", {})
        d3.execute_cdp_cmd("Network.emulateNetworkConditions",
                           {"offline": False, "latency": 6000,
                            "downloadThroughput": 400000, "uploadThroughput": 400000})
        d3.get(a.base + "/")
        time.sleep(3.0)
        do_search(d3, "起风了")
        time.sleep(9.0)
        info = js(d3, """
          const c = document.querySelector('.chip.probing');
          if (!c) return null;
          const r = c.getBoundingClientRect();
          return {w: Math.round(r.width), h: Math.round(r.height),
                  text: c.textContent.trim(), bg: getComputedStyle(c).backgroundColor};
        """)
        check("T5 探测中出现文案 chip", bool(info) and info.get("text") == "检测中",
              str(info))
        check("T5 chip 未被通用骨架撑成方块", bool(info) and info["h"] < 30,
              "height=%s（通用 .skel 会撑到 54px）" % (info["h"] if info else "?"))
        shoot(d3, os.path.join(a.out, "_v_detecting.png"), theme="dark")
    finally:
        try:
            d3.quit()
        except Exception:
            pass

    # --------------------------------------------- T6 设置写入顺序（主题连点）
    import urllib.request
    def store_theme():
        with urllib.request.urlopen(a.base + "/api/store", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))["settings"]["theme"]

    def set_theme_api(v):
        req = urllib.request.Request(
            a.base + "/api/store", method="POST",
            data=json.dumps({"op": "settings", "payload": {"theme": v}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()

    orig = store_theme()
    d4 = make_driver()
    try:
        d4.get(a.base + "/")
        time.sleep(2.5)
        btn = d4.find_element(By.ID, "themeBtn")
        for _ in range(3):                       # 连点三次，制造乱序窗口
            btn.click()
            time.sleep(0.12)
        time.sleep(2.0)
        ui_theme = js(d4, "return document.documentElement.dataset.theme")
        disk_theme = store_theme()
        check("T6 连点后落盘值 = 最终界面状态", ui_theme == disk_theme,
              "ui=%s disk=%s（orig=%s）" % (ui_theme, disk_theme, orig))
    finally:
        try:
            d4.quit()
        except Exception:
            pass
        set_theme_api(orig)                      # 测试不改用户的主题偏好
        print("      已恢复主题偏好 -> %s" % orig)

    # ------------------------------------- T7/T8 默认主题一致性（全新数据目录）
    # 起一个独立实例：ELYSIUM_HOME 指向临时目录，走「全新安装」路径。
    # 只验证默认主题，不触碰真实用户数据。
    fresh = start_fresh_instance()
    if fresh is None:
        check("T7 全新实例可启动", False, "起不来独立实例")
    else:
        fbase, fhome, fproc = fresh
        try:
            d5 = make_driver()
            try:
                d5.get(fbase + "/")
                time.sleep(2.5)
                fresh_theme = js(d5, "return document.documentElement.dataset.theme")
                fresh_bg = js(d5, "return getComputedStyle(document.body).backgroundColor")
                check("T7 全新安装默认深色", fresh_theme == "dark",
                      "theme=%s bg=%s" % (fresh_theme, fresh_bg))
                # 首屏必须直接是深色底，不能是浅色或透明（浅色即闪一下）
                nums = fresh_bg[fresh_bg.find("(") + 1: fresh_bg.find(")")].split(",")
                bg_rgb = [int(float(x)) for x in nums[:3]]
                check("T7 首屏即深色底、无浅色闪现", sum(bg_rgb) < 120,
                      "body 背景=%s（深色应接近 rgb(8,8,11)）" % fresh_bg)

                # T9 货架横向滚动条必须是自绘的 6px。
                # 一旦 .shelf-row 上出现 scrollbar-width / scrollbar-color，
                # Chromium 会改用系统原生滚动条（Edge/WebView2 里 10~15px、常显），
                # ::-webkit-scrollbar 那套「平时透明、hover 显形」全部失效。
                shelf = js(d5, """
                    const host = document.createElement('div');
                    host.innerHTML = '<div class="shelf-row">' + Array.from({length: 20}, () =>
                      '<div style="width:150px;height:80px"></div>').join('') + '</div>';
                    document.body.appendChild(host.firstChild);
                    const s = document.querySelector('.shelf-row');
                    const r = {bar: s.offsetHeight - s.clientHeight,
                               sbw: getComputedStyle(s).scrollbarWidth,
                               can: s.scrollWidth > s.clientWidth};
                    s.remove();
                    return r;
                """)
                check("T9 货架滚动条为自绘 6px（非系统原生）",
                      shelf and shelf["bar"] == 6 and shelf["sbw"] == "auto" and shelf["can"],
                      "bar=%s scrollbar-width=%s 可横滚=%s" % (
                          shelf and shelf["bar"], shelf and shelf["sbw"], shelf and shelf["can"]))

                # T10 口味画像的风格选择器不得被按钮样式裁掉。
                # 容器原来叫 .gpick，跟里面按钮的 .gpick 撞了名：按钮那条
                # height:26px 会作用到容器上，把八百多像素的选择器整块裁成一条缝。
                # 用 320px 窄容器模拟侧栏：40 个标签必然排成多行，
                # 容器若被 height 规则锁死，scrollHeight 就会远大于 offsetHeight。
                gp = js(d5, """
                    const host = document.createElement('div');
                    host.className = 'gpick-box';
                    host.style.width = '320px';
                    host.innerHTML = Array.from({length: 40}, (_, i) =>
                      '<button class="gpick">风格' + i + '</button>').join('');
                    document.body.appendChild(host);
                    const btn = host.querySelector('.gpick');
                    const r = {boxH: host.offsetHeight, scrollH: host.scrollHeight,
                               btnH: btn.offsetHeight, n: host.children.length};
                    host.remove();
                    return r;
                """)
                check("T10 风格选择器容器不被按钮样式裁切",
                      bool(gp) and gp["n"] == 40 and gp["boxH"] > 130
                      and gp["scrollH"] <= gp["boxH"] + 2 and gp["btnH"] <= 30,
                      "容器高=%s 内容高=%s 按钮高=%s（窄容器下 40 个标签应排成 5 行以上）" % (
                          gp and gp["boxH"], gp and gp["scrollH"], gp and gp["btnH"]))
            finally:
                try:
                    d5.quit()
                except Exception:
                    pass

            # 老用户：磁盘里已存 light，重启后必须仍是 light（默认值不得覆盖用户选择）
            set_theme_api_on(fbase, "light")
            stop_instance(fproc)
            fbase, fhome, fproc = start_fresh_instance(fhome)
            kept = store_theme_on(fbase)
            check("T8 老用户已选主题不被默认值覆盖", kept == "light",
                  "重启后 theme=%s（应为 light）" % kept)

            # T11 「换一批」必须真的换。
            # 后端按轮次在候选池里滑窗口，是纯本地运算：卡片集合不变、卡内曲目全换，
            # 而且第二轮起不该再等上游（原来缓存键按当日种子锁死，点一百次都是同一份）。
            # 上游不通时首次就拿不到曲目 —— 那是网络问题不是回归，跳过不判失败。
            def styles_page(refresh):
                # 参数跟前端保持一致（cards=12&per=6）：不同参数会走不同的缓存键，
                # 测试用 8 会让每一次首轮都重新组装，测出来的耗时不代表真实体验。
                url = (fbase + "/api/styles?cards=12&per=6" + ("&refresh=1" if refresh else ""))
                t0 = time.time()
                with urllib.request.urlopen(url, timeout=180) as r:
                    return json.loads(r.read().decode("utf-8")), time.time() - t0

            def card_sig(d):
                out = []
                for c in ((d.get("data") or {}).get("cards") or []):
                    out.append(["%s/%s" % (s.get("name") or "", s.get("artist") or "")
                                for s in (c.get("list") or [])])
                return out

            p0, t0 = styles_page(False)
            sig0 = card_sig(p0)
            if not sig0:
                print("      (跳过 T11：上游没返回曲目，无法验证换批)")
            else:
                p1, t1 = styles_page(True)
                sig1 = card_sig(p1)
                same = sum(1 for a, b in zip(sig0, sig1) if a == b)
                check("T11 换一批后卡片内容真的变了", bool(sig1) and same == 0,
                      "卡片 %d 张，与上轮完全相同 %d 张" % (len(sig1), same))
                check("T11 换批不再等上游（走本地轮转）", t1 < max(2.0, t0 * 0.5),
                      "首轮 %.2fs / 换批 %.2fs" % (t0, t1))
        finally:
            stop_instance(fproc)
            cleanup_fresh(fhome)

    print("\n---- 汇总 ----")
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("  OK   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    print("  %d/%d 通过" % (ok_n, len(RESULTS)))
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
