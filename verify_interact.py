# -*- coding: utf-8 -*-
"""Elysium 前端交互压测（真实 Chrome，桌面 + 390px 移动视口）。

覆盖「用户真点得到」的路径与边缘：
  U1 启动 / 导航循环：无 JS 异常、路由稳定、侧栏计数正确
  U2 搜索：空词、超长词、XSS 词、上游不可达的就地错误态与重试
  U3 搜索结果渲染：注入 200 行、HTML 注入不执行、收藏连点（并发串行化）
  U4 歌单：新建/取消/重命名/右键加入/删除、空名与超长名
  U5 设置抽屉：主题/音量/开关落盘、Esc/遮罩关闭、快速开关、互斥
  U6 播放器：本地测试音播放、连点播放/切歌/进度、队列拖拽重排
  U7 键盘：空格/方向键/L/Q/M//、输入框内不误触、Esc 关全部浮层
  U8 移动端 390×844：无横向溢出、侧栏抽屉、NP 控件命中、长文本
  U9 空数据首装：首页/收藏/本地/作品空态、无报错
  U10 大数据渲染：120 收藏 / 500 首歌单 / 200 搜索结果卡顿上限
  U11 刷新持久化：主题/音量/收藏跨刷新保留

用法（需要 selenium + 本机 Chrome）：
    C:/Users/19645/miniconda3/python.exe verify_interact.py
    ... --only=U1,U2 --keep
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
import traceback
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
RESULTS = []
TMP_ROOT = None

from selenium import webdriver                              # noqa: E402
from selenium.webdriver.chrome.options import Options       # noqa: E402
from selenium.webdriver.common.by import By                  # noqa: E402
from selenium.webdriver.common.keys import Keys              # noqa: E402
from selenium.webdriver.common.action_chains import ActionChains    # noqa: E402
from selenium.webdriver.chrome.service import Service              # noqa: E402
import glob                                                        # noqa: E402


def _cached_driver():
    """用 Selenium 缓存里的 chromedriver，绕开 Selenium Manager 联网。

    本机 Selenium Manager 走外网查驱动会卡约 5 分钟（然后才回退缓存），
    测试本身几十秒的事。缓存路径是按版本分的，取版本号最大的一个。
    """
    base = os.path.expanduser("~/.cache/selenium/chromedriver/win64")
    paths = glob.glob(os.path.join(base, "*", "chromedriver.exe"))

    def key(p):
        name = os.path.basename(os.path.dirname(p))
        parts = []
        for x in name.split("."):
            try:
                parts.append(int(x))
            except ValueError:
                parts.append(0)
        return parts
    paths.sort(key=key, reverse=True)
    return Service(executable_path=paths[0]) if paths else None


# --------------------------------------------------------------------------
def record(group, name, verdict, expect, actual):
    RESULTS.append({"group": group, "name": name, "verdict": verdict,
                    "expect": expect, "actual": actual})
    mark = {"PASS": "[PASS] ", "FAIL": "[FAIL] ", "WARN": "[WARN] ", "SKIP": "[SKIP] "}[verdict]
    print("%s%-3s %s | 期望 %s | 实际 %s" % (mark, group, name, expect, actual), flush=True)


def ok(group, name, cond, expect, actual):
    record(group, name, "PASS" if cond else "FAIL", expect, actual)
    return bool(cond)


def warn(group, name, cond, expect, actual):
    record(group, name, "PASS" if cond else "WARN", expect, actual)
    return bool(cond)


# --------------------------------------------------------------------------
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def seed_data(home):
    """预置数据：120 收藏、2 歌单（500/3 首）、60 最近播放、少量统计。

    全部用 platform=diag：前端 mediaSrc 短路到 /api/diag/tone，不碰上游；
    测试里再把这些 key 预填进 PROBE，避免行渲染后触发 /api/probe 真实探测。
    """
    dl = os.path.join(home, "music")
    os.makedirs(dl, exist_ok=True)
    now = int(time.time())
    songs = [{"kind": "song", "platform": "diag", "id": "lib%d" % i,
              "name": "收藏曲目 %d" % i, "artist": "歌手%d" % (i % 7),
              "album_name": "专辑%d" % (i % 5), "duration": 180 + i,
              "added": now - i} for i in range(120)]
    albums = [{"kind": "album", "platform": "diag", "id": "al%d" % i,
               "name": "收藏专辑 %d" % i, "artist": "歌手%d" % i, "added": now}
              for i in range(3)]
    artists = [{"kind": "artist", "platform": "diag", "id": "ar%d" % i,
                "name": "收藏歌手 %d" % i, "artist_id": "aid%d" % i,
                "cover": "", "added": now} for i in range(3)]
    big_items = [{"platform": "diag", "id": "big%d" % i, "name": "大歌单曲目 %d" % i,
                  "artist": "歌手%d" % (i % 9), "duration": 200 + i} for i in range(500)]
    small_items = [{"platform": "diag", "id": "sm%d" % i, "name": "小歌单曲目 %d" % i,
                    "artist": "A", "duration": 100 + i} for i in range(3)]
    recent = [{"platform": "diag", "id": "rec%d" % i, "name": "最近播放 %d" % i,
               "artist": "歌手%d" % (i % 4), "duration": 150 + i,
               "played_at": now - i * 60} for i in range(60)]
    plays = {}
    daily = {}
    for i in range(10):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        daily[d] = {"n": 5 + i, "s": 900 + i * 60}
        plays["diag_p%d" % i] = {
            "days": {d: 2}, "last_at": now - i * 86400,
            "meta": {"id": "p%d" % i, "platform": "diag", "name": "播放 %d" % i,
                     "artist": "歌手%d" % i},
        }
    data = {
        "settings": {
            "personalized": False, "auto_check_update": False,
            "auto_cache": False, "download_dir": dl,
            "theme": "dark", "volume": 60, "engine_v2": False, "gapless": False,
        },
        "favorites": songs + albums + artists,
        "playlists": [
            {"id": "pl_big", "name": "大歌单", "created_at": now, "updated_at": now,
             "items": big_items},
            {"id": "pl_small", "name": "<b>小歌单</b>", "created_at": now,
             "updated_at": now, "items": small_items},
        ],
        "recent": recent,
        "history": [{"q": "历史%d" % i, "platform": "kuwo", "at": now - i} for i in range(5)],
        "plays": plays, "daily": daily,
        "hours": {str(i): i for i in range(24)},
        "works": [], "downloads": [], "feedback": {}, "loudness": {},
        "eqpresets": {}, "taste_genres": [], "taste_note": "",
    }
    os.makedirs(os.path.join(home, "Elysium"), exist_ok=True)
    with open(os.path.join(home, "Elysium", "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def spawn(home, tag="ui"):
    port = _free_port()
    env = dict(os.environ, ELYSIUM_HOME=home, FLACMUSIC_PORT=str(port))
    logf = open(os.path.join(home, "..", tag + ".log"), "wb")
    proc = subprocess.Popen([sys.executable, "main.py", "--serve", "--port=%d" % port],
                            cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    logf.close()
    base = "http://127.0.0.1:%d" % port
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/api/cache", timeout=1).read(64)
            return {"proc": proc, "base": base, "home": home, "port": port}
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise SystemExit("server did not start")


def http_json(base, path, body=None, timeout=15):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def mkhome(tag, with_data=True):
    home = os.path.join(TMP_ROOT, tag, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):
        shutil.copytree(os.path.join(REPO, item), os.path.join(home, item),
                        ignore=shutil.ignore_patterns("__pycache__"))
    if with_data:
        seed_data(home)
    return home


# --------------------------------------------------------------------------
# Selenium
# --------------------------------------------------------------------------
ERROR_JS = r"""
window.__errors = [];
window.addEventListener('error', function (e) {
  window.__errors.push('error: ' + (e.message || (e.error && e.error.message) || e.error));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__errors.push('rejection: ' + ((e.reason && e.reason.message) || e.reason));
});
"""


def make_driver(mobile=False):
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1440,900")
    o.add_argument("--lang=zh-CN")
    o.add_argument("--autoplay-policy=no-user-gesture-required")
    o.add_argument("--mute-audio")
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    svc = _cached_driver()
    d = webdriver.Chrome(service=svc, options=o) if svc else webdriver.Chrome(options=o)
    d.set_page_load_timeout(60)
    d.set_script_timeout(60)
    d.execute_cdp_cmd("Network.enable", {})
    d.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
        "*flac.music.hi.cn*", "*music.163.com*", "*126.net*", "*kuwo.cn*",
        "*kugou.com*", "*y.qq.com*", "*qqmusic*",
    ]})
    d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": ERROR_JS})
    if mobile:
        set_mobile(d)
    return d


def set_mobile(d, width=390, height=844):
    d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 2, "mobile": True})
    d.execute_script("window.dispatchEvent(new Event('resize'));")


def set_desktop(d):
    d.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    d.execute_script("window.dispatchEvent(new Event('resize'));")


def js(d, code, *args):
    return d.execute_script(code, *args)


def async_js(d, code, *args, timeout=20):
    d.set_script_timeout(timeout)
    return d.execute_async_script(code, *args)


def api_json(d, url, body=None):
    if body is None:
        code = "const done=arguments[arguments.length-1]; fetch(arguments[0]).then(r=>r.json()).then(v=>done(v));"
        return async_js(d, code, url)
    code = ("const done=arguments[arguments.length-1];"
            "fetch(arguments[0],{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify(arguments[1])}).then(r=>r.json()).then(v=>done(v));")
    return async_js(d, code, url, body)


def wait_js(d, expr, timeout=10, poll=0.1):
    end = time.time() + timeout
    last = None
    script = expr if expr.lstrip().startswith("return") else "return (%s);" % expr
    while time.time() < end:
        try:
            last = d.execute_script(script)
            if last:
                return last
        except Exception:
            pass
        time.sleep(poll)
    return last


def click_js(d, sel):
    return d.execute_script(
        "const e=document.querySelector(arguments[0]); if(!e) return false; e.click(); return true;", sel)


def errors(d):
    try:
        return js(d, "return window.__errors || [];") or []
    except Exception:
        return []


def take_errors(d, label, expect_none=True):
    errs = errors(d)
    if expect_none:
        ok("U?", label, not errs, "0 JS 异常", errs[:3])
    return errs


def console_severe(d):
    out = []
    try:
        for e in d.get_log("browser"):
            if e.get("level") == "SEVERE" and e.get("source") == "javascript":
                out.append(e.get("message"))
    except Exception:
        pass
    return out


def boot_ok(d, base):
    d.get(base)
    if not wait_js(d, "return !!(typeof state !== 'undefined' && state.version && document.querySelector('#view .page'));", 20):
        return False
    # 预填 PROBE：种子/注入的 diag 曲目都不触发真实探测
    js(d, r"""
      const ids = [];
      (state.favs||[]).forEach(x => ids.push(x));
      (state.playlists||[]).forEach(p => (p.items||[]).forEach(x => ids.push(x)));
      (state.recent||[]).forEach(x => ids.push(x));
      ids.forEach(it => { if (it && it.platform) PROBE.set(pk(it), {at: Date.now(), unresolved: true}); });
    """)
    return True


def prep_items(d, items):
    js(d, r"""
      (arguments[0] || []).forEach(it => PROBE.set(pk(it), {at: Date.now(), unresolved: true}));
    """, items)


def set_route(d, name, tag=None):
    js(d, """
      state.route = {name: arguments[0]};
      if (arguments[1]) state.route.id = arguments[1];
      render(true);
    """, name, tag)


# --------------------------------------------------------------------------
# U1 启动 / 导航
# --------------------------------------------------------------------------
def group_u1(d, base, env):
    ok("U1", "首屏启动完成", boot_ok(d, base), "state.version + .page", js(d, "return state.version"))
    sev = console_severe(d)
    ok("U1", "启动无 JS SEVERE", not sev, "0", sev[:3])
    ok("U1", "启动无未捕获异常", not errors(d), "0", errors(d)[:3])
    ok("U1", "侧栏收藏计数=126", js(d, "return $('#libCount').textContent") == "126",
       "126", js(d, "return $('#libCount').textContent"))
    ok("U1", "侧栏歌单计数=2", js(d, "return $('#plCount').textContent") == "2",
       "2", js(d, "return $('#plCount').textContent"))

    # 逐个主分区
    routes = [("home", "home"), ("search", "search"), ("library", "library"),
              ("local", "local"), ("works", "works")]
    bad = []
    for nav, route in routes:
        click_js(d, '[data-nav="%s"]' % nav)
        got = wait_js(d, "return state.route.name;", 5)
        if got != route:
            bad.append("%s->%s" % (nav, got))
    ok("U1", "五个主分区导航", not bad, "全部命中", bad or "5/5")
    # 设置走抽屉
    click_js(d, '[data-nav="settings"]')
    ok("U1", "设置入口打开抽屉", js(d, "return !$('#settingsDrawer').hidden"), "visible",
       js(d, "return !$('#settingsDrawer').hidden"))
    press_esc(d)

    # 快速切换 30 次
    t0 = time.time()
    js(d, """
      const seq=['home','search','library','local','works','home'];
      (async()=>{ for(let i=0;i<30;i++){ document.querySelector('[data-nav="'+seq[i%seq.length]+'"]').click(); await new Promise(r=>setTimeout(r,15)); } })();
    """)
    time.sleep(1.2)
    ok("U1", "30 次快速导航不抛错", not errors(d), "0", errors(d)[:3])
    ok("U1", "快速导航后路由稳定", js(d, "return !!state.route.name"), "有路由",
       js(d, "return state.route.name"))


def press_esc(d):
    # 焦点在 body 才能触发全局快捷键；输入框内 Esc 只 blur
    try:
        d.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        js(d, "document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));")


# --------------------------------------------------------------------------
# U2 搜索
# --------------------------------------------------------------------------
def group_u2(d):
    # 确定性模拟「上游不可达」：只替换页面的 rpc 入口，后端与页面错误态
    # 都按真实路径走，不受外网速度影响。
    js(d, "window.__origRpc = rpc; rpc = async () => { throw new Error('模拟网络失败'); };")
    click_js(d, '[data-nav="search"]')
    time.sleep(0.3)
    # 空词：不应离开搜索页 / 不应报错
    js(d, "$('#kw').value=''; $('#searchForm').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));")
    time.sleep(0.3)
    ok("U2", "空关键词不发起搜索", js(d, "return state.route.name") == "search"
       and not js(d, "return !!state.q.keyword"), "留在搜索页 / 无 keyword",
       "%s / %r" % (js(d, "return state.route.name"), js(d, "return state.q.keyword")))

    # 超长关键词 + XSS 载荷（上游被 CDP 阻断 → 就地错误态）
    payload = '<img src=x onerror="window.__xss=1">' + "长" * 800
    js(d, "$('#kw').value=arguments[0]; $('#searchForm').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));", payload)
    got = wait_js(d, "return !!(state.q && (state.q.error || state.items.length));", 25)
    ok("U2", "搜索失败/成功有终态", bool(got), "error 或 items", js(d, "return {err:state.q.error, n:(state.items||[]).length, loading:state.loading}"))
    ok("U2", "搜索错误是页面内联态", js(d, "return document.body.innerText.indexOf('搜索失败') >= 0 || (state.items||[]).length>0"),
       "页面含「搜索失败」或结果", js(d, "return (state.q.error||'').slice(0,40)"))
    ok("U2", "XSS 关键词未执行", js(d, "return typeof window.__xss === 'undefined'"),
       "undefined", js(d, "return window.__xss"))
    # 标题按文本渲染，标签不生效
    h1 = js(d, "return ($('#view h1')||{}).textContent || ''")
    ok("U2", "XSS 关键词按文本显示", "<img" in h1, "文本含 <img", h1[:40])
    ok("U2", "超长关键词不撑破布局",
       js(d, "return document.documentElement.scrollWidth <= window.innerWidth + 1"),
       "无横向溢出", "%s/%s" % (js(d, "return document.documentElement.scrollWidth"),
                             js(d, "return window.innerWidth")))
    # 重试按钮存在且可点
    if js(d, "return !!document.querySelector('[data-act=search-retry]')"):
        click_js(d, "[data-act=search-retry]")
        time.sleep(0.5)
        ok("U2", "搜索重试不抛错", not errors(d), "0", errors(d)[:3])
    else:
        warn("U2", "搜索重试按钮", False, "存在", "本次搜索成功，无重试按钮")
    # 清空按钮
    js(d, "$('#kw').value='abc'; $('#kw').dispatchEvent(new Event('input',{bubbles:true}));")
    click_js(d, "#kwClear")
    ok("U2", "清空按钮清空输入", js(d, "return $('#kw').value") == "",
       "空", js(d, "return $('#kw').value"))
    js(d, "rpc = window.__origRpc;")


# --------------------------------------------------------------------------
# U3 搜索结果 + 收藏并发
# --------------------------------------------------------------------------
def make_track(i, name=None):
    return {"platform": "diag", "id": "inj%d" % i,
            "name": name or "注入曲目 %d" % i, "artist": "注入歌手 %d" % i,
            "album_name": "注入专辑", "duration": 120 + i,
            "minfo": [{"format": "mp3", "bitrate": "320"}], "time": "1", "sign": "x"}


def group_u3(d):
    items = [make_track(i) for i in range(200)]
    prep_items(d, items)
    js(d, """
      state.items = arguments[0];
      state.q = {keyword:'注入测试', page:1, total:arguments[0].length, more:false};
      state.route = {name:'search'};
      render(true);
    """, items)
    rows = js(d, "return document.querySelectorAll('#view .tr').length")
    ok("U3", "200 行搜索结果渲染", rows == 200, "200", rows)
    ok("U3", "200 行渲染无 JS 异常", not errors(d), "0", errors(d)[:3])

    # HTML 注入名不执行
    html_items = [make_track(900 + i, '<img src=x onerror="window.__xss2=1">歌%d' % i)
                  for i in range(5)]
    prep_items(d, html_items)
    js(d, "state.items=arguments[0]; state.route={name:'search'}; state.q={keyword:'x',page:1,total:5}; render(true);", html_items)
    time.sleep(0.3)
    ok("U3", "曲名 HTML 未执行", js(d, "return typeof window.__xss2 === 'undefined'"),
       "undefined", js(d, "return window.__xss2"))
    ok("U3", "曲名按文本渲染",
       js(d, "return (document.querySelector('#view .tr-name')||{}).textContent||''").startswith("<img"),
       "文本以 <img 开头", js(d, "return (document.querySelector('#view .tr-name')||{}).textContent||''")[:30])

    # 并发收藏 30 首（不等待），响应乱序不能丢
    clear_favs(d)
    items = [make_track(1000 + i) for i in range(30)]
    prep_items(d, items)
    js(d, "state.items=arguments[0]; state.route={name:'search'}; state.q={keyword:'x',page:1,total:30}; render(true);", items)
    time.sleep(0.2)
    js(d, """
      document.querySelectorAll('#view .tr-act [data-act=fav]').forEach((b) => b.click());
    """)
    t0 = time.time()
    wait_js(d, "return state.favs.length >= 30;", 15)
    time.sleep(0.5)
    ui_n = js(d, "return (state.favs||[]).length")
    server_n = len(api_json(d, "/api/store").get("favorites") or [])
    ok("U3", "30 个并发收藏全落盘", server_n == 30, "server=30", "server=%s ui=%s" % (server_n, ui_n))
    ok("U3", "并发收藏后 UI 与服务端一致", ui_n == server_n, "一致",
       "ui=%s server=%s" % (ui_n, server_n))
    # 再连点一次全部取消
    js(d, "document.querySelectorAll('#view .tr-act [data-act=fav]').forEach((b) => b.click());")
    wait_js(d, "return (state.favs||[]).length === 0;", 15)
    time.sleep(0.3)
    ui_n2 = js(d, "return (state.favs||[]).length")
    server_n2 = len(api_json(d, "/api/store").get("favorites") or [])
    ok("U3", "再连点 30 次全部取消", server_n2 == 0 and ui_n2 == 0, "0/0",
       "ui=%s server=%s" % (ui_n2, server_n2))
    # 恢复种子收藏，后面的收藏库/大数据/刷新用例要用
    seed_favorites(d)


def clear_favs(d):
    api_json(d, "/api/store", {"op": "favorites_clear", "payload": {}})
    data = api_json(d, "/api/store")
    js(d, "state.favs = arguments[0] || []; renderSide(); render();",
       data.get("favorites") or [])
    time.sleep(0.3)


def seed_favorites(d, n=120):
    items = [{"kind": "song", "platform": "diag", "id": "lib%d" % i,
              "name": "收藏曲目 %d" % i, "artist": "歌手%d" % (i % 7),
              "album_name": "专辑%d" % (i % 5), "duration": 180 + i} for i in range(n)]
    prep_items(d, items)
    js(d, "window.__seedItems = arguments[0];", items)
    async_js(d, r"""
      const done = arguments[arguments.length - 1];
      (async () => {
        for (const it of window.__seedItems) {
          await fetch('/api/store', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({op:'fav_toggle', payload: Object.assign({kind:'song'}, it)})});
        }
        const d = await fetch('/api/store').then(r=>r.json());
        state.favs = d.favorites || [];
        renderSide(); render();
        done(state.favs.length);
      })();
    """, timeout=40)
    time.sleep(0.2)


# --------------------------------------------------------------------------
# U4 歌单
# --------------------------------------------------------------------------
def group_u4(d):
    set_route(d, "home")
    n0 = js(d, "return (state.playlists||[]).length")
    click_js(d, "#plAdd")
    ok("U4", "新建歌单弹窗出现", js(d, "return !$('#askLayer').hidden"), "visible",
       js(d, "return !$('#askLayer').hidden"))
    js(d, "$('#askInput').value='交互测试歌单'; $('#askOk').click();")
    for _ in range(50):
        n1 = js(d, "return (state.playlists||[]).length")
        if n1 > n0:
            break
        time.sleep(0.1)
    ok("U4", "新建歌单落盘", n1 == n0 + 1, n0 + 1, n1)
    new = js(d, "return state.playlists[state.playlists.length-1]") or {}
    ok("U4", "新建后进入歌单页", js(d, "return state.route.name") == "playlist"
       and js(d, "return state.route.id") == new.get("id"), "playlist/新 id",
       "%s/%s" % (js(d, "return state.route.name"), js(d, "return state.route.id")))

    # 取消新建
    click_js(d, "#plAdd")
    js(d, "$('#askCancel').click();")
    time.sleep(0.3)
    ok("U4", "取消新建不增加歌单",
       js(d, "return (state.playlists||[]).length") == n1, n1,
       js(d, "return (state.playlists||[]).length"))

    # Esc 关闭输入浮层
    click_js(d, "#plAdd")
    d.find_element(By.ID, "askInput").send_keys(Keys.ESCAPE)
    time.sleep(0.3)
    ok("U4", "Esc 关闭新建歌单浮层",
       js(d, "return $('#askLayer').hidden && (state.playlists||[]).length === arguments[0]", n1),
       "hidden / 数量不变", "%s / %s" % (js(d, "return $('#askLayer').hidden"),
                                       js(d, "return (state.playlists||[]).length")))

    # 重命名
    click_js(d, "#plAdd")
    js(d, "$('#askInput').value='待改名'; $('#askOk').click();")
    for _ in range(50):
        if js(d, "return (state.playlists||[]).length") == n1 + 1:
            break
        time.sleep(0.1)
    pid = js(d, "return state.playlists[state.playlists.length-1].id")
    click_js(d, '[data-act="pl-rename"]')
    js(d, "$('#askInput').value='改名后的歌单'; $('#askOk').click();")
    time.sleep(0.5)
    name = js(d, "return (state.playlists.find(p=>p.id===arguments[0])||{}).name", pid)
    ok("U4", "歌单重命名", name == "改名后的歌单", "改名后的歌单", name)

    # 右键加入歌单（用已有大歌单作为目标）
    set_route(d, "library")
    time.sleep(0.4)
    row = d.find_element(By.CSS_SELECTOR, "#view .tr")
    d.execute_script("arguments[0].dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:200,clientY:200}));", row)
    ok("U4", "右键曲目弹出菜单", js(d, "return !$('#ctxmenu').hidden"), "visible",
       js(d, "return !$('#ctxmenu').hidden"))
    click_js(d, '[data-act="ctx-add-pl"]')
    ok("U4", "加入歌单选择器出现", js(d, "return document.querySelectorAll('#ctxmenu [data-act=pl-pick]').length") >= 2,
       "≥2 个歌单", js(d, "return document.querySelectorAll('#ctxmenu [data-act=pl-pick]').length"))
    # 选第一个（大歌单）
    before = js(d, "return state.playlists.find(p=>p.id==='pl_big').items.length")
    js(d, "document.querySelector('#ctxmenu [data-act=pl-pick]').click();")
    time.sleep(0.8)
    after = js(d, "return state.playlists.find(p=>p.id==='pl_big').items.length")
    ok("U4", "右键加入歌单", after == before + 1, before + 1, after)
    # 重复加同一首：去重
    d.execute_script("arguments[0].dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:200,clientY:200}));", row)
    click_js(d, '[data-act="ctx-add-pl"]')
    js(d, "document.querySelector('#ctxmenu [data-act=pl-pick]').click();")
    time.sleep(0.8)
    after2 = js(d, "return state.playlists.find(p=>p.id==='pl_big').items.length")
    ok("U4", "重复加入同一首去重", after2 == after, after, after2)

    # 删除歌单（confirm 对话框）
    d.execute_script("window.confirm = () => true;")
    set_route(d, "playlist", pid)
    time.sleep(0.3)
    click_js(d, '[data-act="pl-delete"]')
    time.sleep(0.7)
    ok("U4", "删除歌单", js(d, "return !state.playlists.some(p=>p.id===arguments[0])", pid),
       "已删除", js(d, "return state.playlists.some(p=>p.id===arguments[0])", pid))


# --------------------------------------------------------------------------
# U5 设置抽屉
# --------------------------------------------------------------------------
def group_u5(d):
    state = js(d, "return {theme:document.documentElement.dataset.theme, vol:state.settings.volume}")
    open_settings(d)
    ok("U5", "设置抽屉打开", js(d, "return !$('#settingsDrawer').hidden"), "visible",
       js(d, "return !$('#settingsDrawer').hidden"))
    # 主题双向
    click_js(d, "#themeBtn")
    time.sleep(0.2)
    t1 = js(d, "return document.documentElement.dataset.theme")
    click_js(d, "#themeBtn")
    time.sleep(0.2)
    t2 = js(d, "return document.documentElement.dataset.theme")
    ok("U5", "主题按钮双向切换", t1 != state["theme"] and t2 == state["theme"],
       "切走再切回", "%s->%s->%s" % (state["theme"], t1, t2))
    # 快速点 10 次，最终与落盘一致
    js(d, "for(let i=0;i<10;i++) $('#themeBtn').click();")
    time.sleep(1.0)
    theme = js(d, "return document.documentElement.dataset.theme")
    saved = (api_json(d, "/api/store").get("settings") or {}).get("theme")
    ok("U5", "快速切主题落盘一致", theme == saved, "一致", "%s/%s" % (theme, saved))
    # 音量（滑杆在 input 时即时生效，change 时才落盘）
    js(d, "$('#vol').value=0; $('#vol').dispatchEvent(new Event('input',{bubbles:true})); $('#vol').dispatchEvent(new Event('change',{bubbles:true}));")
    time.sleep(0.3)
    js(d, "$('#vol').value=100; $('#vol').dispatchEvent(new Event('input',{bubbles:true})); $('#vol').dispatchEvent(new Event('change',{bubbles:true}));")
    time.sleep(0.8)
    vol = (api_json(d, "/api/store").get("settings") or {}).get("volume")
    ok("U5", "音量 0→100 落盘", vol == 100, "100", vol)
    # 开关：个性化
    js(d, "$('#setPersonalized').checked=false; $('#setPersonalized').dispatchEvent(new Event('change',{bubbles:true}));")
    time.sleep(0.5)
    pers = (api_json(d, "/api/store").get("settings") or {}).get("personalized")
    ok("U5", "设置开关落盘", pers is False, "false", pers)
    # Esc 关闭
    press_esc(d)
    time.sleep(0.3)
    ok("U5", "Esc 关闭设置抽屉", js(d, "return $('#settingsDrawer').hidden"), "hidden",
       js(d, "return !$('#settingsDrawer').hidden"))
    ok("U5", "Esc 关闭后遮罩归位", js(d, "return $('#drawerMask').hidden"), "hidden",
       js(d, "return !$('#drawerMask').hidden"))
    # 遮罩关闭
    open_settings(d)
    js(d, "$('#drawerMask').click();")
    time.sleep(0.3)
    ok("U5", "遮罩关闭设置抽屉", js(d, "return $('#settingsDrawer').hidden"), "hidden",
       js(d, "return !$('#settingsDrawer').hidden"))
    # 快速开关 20 次
    js(d, "for(let i=0;i<20;i++){ openSettings(); $('#settingsDrawer').hidden=true; }")
    time.sleep(0.5)
    ok("U5", "20 次快速开关设置不抛错", not errors(d), "0", errors(d)[:3])
    # 互斥：设置 → 队列 → NP
    open_settings(d)
    click_js(d, "#panelBtn")
    time.sleep(0.2)
    ok("U5", "设置→队列互斥", js(d, "return $('#settingsDrawer').hidden && !$('#panelDrawer').hidden"),
       "只开队列", js(d, "return {s:$('#settingsDrawer').hidden,p:$('#panelDrawer').hidden}"))
    js(d, "openNp('lyric')")
    time.sleep(0.2)
    ok("U5", "队列→NP 互斥", js(d, "return $('#panelDrawer').hidden && !$('#npDrawer').hidden"),
       "只开 NP", js(d, "return {p:$('#panelDrawer').hidden,n:$('#npDrawer').hidden}"))
    press_esc(d)
    time.sleep(0.2)
    ok("U5", "Esc 关闭 NP", js(d, "return $('#npDrawer').hidden"), "hidden",
       js(d, "return !$('#npDrawer').hidden"))


def open_settings(d):
    js(d, "openSettings();")


# --------------------------------------------------------------------------
# U6 播放器
# --------------------------------------------------------------------------
def group_u6(d):
    items = [make_track(2000 + i) for i in range(5)]
    prep_items(d, items)
    js(d, "state.items=arguments[0]; state.route={name:'search'}; state.q={keyword:'x',page:1,total:5}; render(true);", items)
    time.sleep(0.3)
    js(d, "document.querySelector('#view .tr [data-act=play]').click();")
    got = wait_js(d, "return !!(state.playing && audio().src) && audio().paused === false;", 12)
    ok("U6", "点击行开始播放", bool(got), "playing & !paused",
       js(d, "return {playing:!!state.playing,paused:audio().paused,ready:audio().readyState,src:audio().src.slice(0,60)}"))
    ok("U6", "播放无媒体错误", js(d, "return !audio().error"), "null",
       js(d, "return audio().error && audio().error.code"))

    # 连点播放/暂停 20 次
    js(d, "for(let i=0;i<20;i++) $('#playBtn').click();")
    time.sleep(1.0)
    ok("U6", "20 次连点播放不抛错", not errors(d), "0", errors(d)[:3])
    # 连点下一首 10 次
    js(d, "for(let i=0;i<10;i++) $('#nextBtn').click();")
    time.sleep(1.5)
    idx = js(d, "return state.playIndex")
    ok("U6", "连续切歌后索引合法", isinstance(idx, int) and 0 <= idx < len(items),
       "0..4", idx)
    ok("U6", "连续切歌后有在播曲目", bool(js(d, "return state.playing && state.playList.length===5")),
       "playing/5", js(d, "return {p:!!state.playing,n:state.playList.length}"))

    # 进度条真实指针点击（pointerdown 驱动，不是 click）
    try:
        seek_el = d.find_element(By.ID, "seekBar")
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", seek_el)
        for _ in range(5):
            ActionChains(d).move_to_element(seek_el).click().perform()
        time.sleep(0.5)
        ok("U6", "进度条连续点击不抛错", not errors(d), "0", errors(d)[:3])
        ok("U6", "进度条点击后仍是有限时间",
           js(d, "return isFinite(audio().currentTime) && audio().currentTime >= 0"),
           "finite/≥0", js(d, "return audio().currentTime"))
    except Exception as e:                                # noqa: BLE001
        warn("U6", "进度条可点击", False, "可点击", e)

    # 模式循环（3 档，点 6 次回到原点）
    js(d, "for(let i=0;i<6;i++) $('#modeBtn').click();")
    time.sleep(0.2)
    ok("U6", "模式循环回到顺序播放", js(d, "return state.mode") == "order", "order",
       js(d, "return state.mode"))

    # 队列拖拽：把第 1 行拖到第 4 行之后
    js(d, "openNp('queue')")
    time.sleep(0.4)
    order0 = js(d, "return state.playList.map(x=>x.id)")
    js(d, r"""
      const rows = document.querySelectorAll('#queuePane .qrow');
      if (rows.length >= 4) {
        const dt = new DataTransfer();
        rows[0].dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
        const r = rows[3].getBoundingClientRect();
        rows[3].dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt,clientY:r.bottom-1}));
        rows[3].dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt,clientY:r.bottom-1}));
        rows[0].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
      }
    """)
    time.sleep(0.4)
    order1 = js(d, "return state.playList.map(x=>x.id)")
    ok("U6", "队列拖拽重排生效", order1 != order0 and len(order1) == len(order0), "顺序改变/长度不变",
       "%s -> %s" % (order0[:4], order1[:4]))
    ok("U6", "拖拽后播放索引仍指向当前曲", js(d, "return state.playList[state.playIndex] === state.playing"),
       "同对象", js(d, "return {i:state.playIndex,ok:state.playList[state.playIndex]===state.playing}"))
    # 拖到自身：无变化
    before = js(d, "return state.playList.map(x=>x.id)")
    js(d, r"""
      const rows=document.querySelectorAll('#queuePane .qrow');
      const dt=new DataTransfer();
      rows[1].dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
      rows[1].dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));
      rows[1].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
    """)
    time.sleep(0.3)
    after = js(d, "return state.playList.map(x=>x.id)")
    ok("U6", "拖到自身不改顺序", after == before, "不变", "%s -> %s" % (before[:3], after[:3]))
    press_esc(d)


# --------------------------------------------------------------------------
# U7 键盘
# --------------------------------------------------------------------------
def start_local_play(d, base_id=4000):
    items = [make_track(base_id + i) for i in range(3)]
    prep_items(d, items)
    js(d, """
      state.items = arguments[0]; state.route = {name:'search'};
      state.q = {keyword:'键盘测试', page:1, total:arguments[0].length};
      render(true);
    """, items)
    time.sleep(0.3)
    js(d, "playAt(arguments[0], 0);", items)
    wait_js(d, "return !!(state.playing && !audio().paused);", 15)
    time.sleep(0.3)


def group_u7(d):
    set_route(d, "home")
    start_local_play(d)
    time.sleep(0.3)
    body = d.find_element(By.TAG_NAME, "body")
    # 输入框内按 M：不静音、不触发全局
    js(d, "$('#kw').focus()")
    muted0 = js(d, "return audio().muted")
    d.find_element(By.ID, "kw").send_keys("m")
    time.sleep(0.2)
    ok("U7", "输入框内 M 不触发全局静音", js(d, "return audio().muted") == muted0,
       muted0, js(d, "return audio().muted"))
    js(d, "$('#kw').blur()")

    # / 聚焦搜索
    body.send_keys("/")
    time.sleep(0.2)
    ok("U7", "/ 聚焦搜索框", js(d, "return document.activeElement && document.activeElement.id") == "kw",
       "kw", js(d, "return document.activeElement && document.activeElement.id"))
    js(d, "$('#kw').blur()")

    # L / Q 打开 NP 的对应 tab
    body.send_keys("l")
    time.sleep(0.2)
    ok("U7", "L 打开展开页歌词", js(d, "return !$('#npDrawer').hidden && state.npTab==='lyric'"),
       "np lyric", js(d, "return {h:$('#npDrawer').hidden,t:state.npTab}"))
    press_esc(d)
    body.send_keys("q")
    time.sleep(0.2)
    ok("U7", "Q 打开展开页队列", js(d, "return !$('#npDrawer').hidden && state.npTab==='queue'"),
       "np queue", js(d, "return {h:$('#npDrawer').hidden,t:state.npTab}"))
    press_esc(d)

    # 方向键调音量
    v0 = js(d, "return Number(state.settings.volume)")
    body.send_keys(Keys.ARROW_UP)
    body.send_keys(Keys.ARROW_UP)
    time.sleep(0.2)
    v1 = js(d, "return Number($('#vol').value)")
    ok("U7", "↑↑ 调音量 +10", v1 == min(100, v0 + 10), min(100, v0 + 10), v1)
    body.send_keys(Keys.ARROW_DOWN)
    time.sleep(0.2)
    v2 = js(d, "return Number($('#vol').value)")
    ok("U7", "↓ 调音量 -5", v2 == max(0, v1 - 5), max(0, v1 - 5), v2)
    # 空格播放/暂停
    state0 = js(d, "return audio().paused")
    body.send_keys(Keys.SPACE)
    time.sleep(0.5)
    ok("U7", "空格切换播放态", js(d, "return audio().paused") != state0, "切换",
       "%s->%s" % (state0, js(d, "return audio().paused")))
    ok("U7", "键盘操作无异常", not errors(d), "0", errors(d)[:3])


# --------------------------------------------------------------------------
# U8 移动端
# --------------------------------------------------------------------------
def group_u8(d, base):
    set_mobile(d)
    d.get(base)
    ok("U8", "390 视口启动", wait_js(d, "return window.innerWidth === 390 && !!state.version;", 20),
       "390 + boot", js(d, "return {w:window.innerWidth,v:state.version}"))
    js(d, r"""
      const ids=[]; (state.favs||[]).forEach(x=>ids.push(x));
      (state.playlists||[]).forEach(p=>(p.items||[]).forEach(x=>ids.push(x)));
      (state.recent||[]).forEach(x=>ids.push(x));
      ids.forEach(it=>{ if(it&&it.platform) PROBE.set(pk(it),{at:Date.now(),unresolved:true}); });
    """)
    time.sleep(0.5)
    ok("U8", "移动端无横向溢出",
       js(d, "return document.documentElement.scrollWidth <= window.innerWidth + 1"),
       "≤391", "%s/%s" % (js(d, "return document.documentElement.scrollWidth"),
                         js(d, "return window.innerWidth")))
    # 侧栏抽屉
    ok("U8", "侧栏默认收起", js(d, "return !document.querySelector('.side').classList.contains('open')"),
       "closed", js(d, "return document.querySelector('.side').classList.contains('open')"))
    click_js(d, "#navBtn")
    time.sleep(0.3)
    ok("U8", "菜单按钮打开侧栏", js(d, "return document.querySelector('.side').classList.contains('open')"),
       "open", js(d, "return document.querySelector('.side').classList.contains('open')"))
    ok("U8", "侧栏打开显示遮罩", js(d, "return !$('#drawerMask').hidden"), "visible",
       js(d, "return !$('#drawerMask').hidden"))
    press_esc(d)
    time.sleep(0.3)
    ok("U8", "Esc 关侧栏且遮罩归位",
       js(d, "return !document.querySelector('.side').classList.contains('open') && $('#drawerMask').hidden"),
       "closed + mask hidden",
       "%s / %s" % (js(d, "return document.querySelector('.side').classList.contains('open')"),
                    js(d, "return $('#drawerMask').hidden")))
    click_js(d, "#navBtn")
    time.sleep(0.3)
    # 点导航后收起
    click_js(d, '[data-nav="library"]')
    time.sleep(0.4)
    ok("U8", "点侧栏导航后收起", js(d, "return !document.querySelector('.side').classList.contains('open')"),
       "closed", js(d, "return document.querySelector('.side').classList.contains('open')"))
    ok("U8", "移动端收藏库无横向溢出",
       js(d, "return document.documentElement.scrollWidth <= window.innerWidth + 1"),
       "≤391", js(d, "return document.documentElement.scrollWidth"))
    # 打开 NP：点播放条标题（移动端点标题展开）
    np_item = make_track(3100, "移动端测试曲目")
    prep_items(d, [np_item])
    js(d, """
      // 歌词走外部上游，移动布局用例不依赖网络：替换为本地占位实现
      window.__origLyric = loadNpLyric;
      loadNpLyric = async () => {
        const b = $('#npLyricBody');
        if (b) b.innerHTML = '<div class="ll">测试歌词第一行</div><div class="ll">测试歌词第二行</div>';
      };
      state.playing = arguments[0]; state.playList = [arguments[0]]; state.playIndex = 0;
      paintPlayer(state.playing); renderQueue();
    """, np_item)
    time.sleep(0.3)
    js(d, "const m=document.querySelector('.player .pl-meta'); if(m) m.click();")
    time.sleep(0.5)
    vis = js(d, "return !$('#npDrawer').hidden")
    ok("U8", "移动端可打开展开页", vis, "visible", vis)
    if vis:
        # 关键控件在视口内且 elementFromPoint 命中
        bad = js(d, r"""
          const sels=['#npDrawer #npPlay','#npDrawer [data-act=np-prev]','#npDrawer [data-act=np-next]',
                      '#npDrawer #npSeekBar','#npDrawer [data-act=nptab]','#npDrawer #npFav',
                      '#npDrawer [data-act=karaoke]'];
          const out=[];
          sels.forEach(s=>{
            const e=document.querySelector(s); if(!e){out.push(s+':miss');return;}
            const r=e.getBoundingClientRect();
            if(r.width<8||r.height<8||r.bottom>innerHeight+1||r.top<-1||r.right>innerWidth+1||r.left<-1){
              out.push(s+':rect');return;
            }
            const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
            if(!(hit===e||e.contains(hit)||hit.contains(e))) out.push(s+':covered');
          });
          return out;
        """)
        ok("U8", "展开页控件全部可命中", not bad, "6/6", bad or "6/6")
        # 歌词唯一滚动容器
        meta = js(d, r"""
          const b=document.querySelector('#npLyricBody');
          const cs=b?getComputedStyle(b):null;
          return {exists:!!b, overflowY:cs?cs.overflowY:'', h:b?b.clientHeight:0,
                  sh:b?b.scrollHeight:0, bodyOverflow:getComputedStyle(document.body).overflowY};
        """)
        ok("U8", "歌词区可滚动且页面不嵌套滚动",
           meta.get("exists") and meta.get("overflowY") in ("auto", "scroll")
           and meta.get("bodyOverflow") != "scroll", "lyric auto / body 不滚", meta)
        # 关闭
        press_esc(d)
        time.sleep(0.2)
        ok("U8", "Esc 关闭展开页", js(d, "return $('#npDrawer').hidden"), "hidden",
           js(d, "return !$('#npDrawer').hidden"))
    # 设置抽屉宽度不溢出
    open_settings(d)
    time.sleep(0.3)
    w = js(d, "return $('#settingsDrawer').getBoundingClientRect().width")
    ok("U8", "移动端设置抽屉不超宽", w <= 390 + 1, "≤391", w)
    press_esc(d)
    # 长标题不撑破播放条
    long_item = make_track(3000, "超长歌名" * 40)
    prep_items(d, [long_item])
    js(d, "state.playing=arguments[0]; state.playList=[arguments[0]]; state.playIndex=0; paintPlayer(state.playing);", long_item)
    time.sleep(0.3)
    ok("U8", "超长歌名不横向溢出",
       js(d, "return document.documentElement.scrollWidth <= window.innerWidth + 1"),
       "≤391", js(d, "return document.documentElement.scrollWidth"))
    set_desktop(d)


# --------------------------------------------------------------------------
# U9 空数据首装
# --------------------------------------------------------------------------
def group_u9():
    home = mkhome("fresh", with_data=False)
    env = spawn(home, "fresh")
    # 默认下载目录指向真实 Music/Elysium：先改成临时目录，避免空态用例扫描用户曲库
    dl = os.path.join(home, "dl")
    os.makedirs(dl, exist_ok=True)
    http_json(env["base"], "/api/store", {"op": "settings",
                                          "payload": {"download_dir": dl,
                                                      "personalized": False}})
    d = make_driver()
    try:
        ok("U9", "空白首装可启动", boot_ok(d, env["base"]), "boot",
           js(d, "return state.version"))
        for nav, route in (("library", "library"), ("local", "local"), ("works", "works")):
            click_js(d, '[data-nav="%s"]' % nav)
            wait_js(d, "return state.route.name === arguments[0];", 5)
            time.sleep(0.5)
            txt = js(d, "return ($('#view')||{}).innerText || ''")
            ok("U9", "%s 空态文案" % route, ("空" in txt or "还没有" in txt or "没有" in txt),
               "含空态提示", txt[:40].replace("\n", " "))
        ok("U9", "空数据无 JS 异常", not errors(d), "0", errors(d)[:3])
        ok("U9", "空首页不放全屏空列表",
           js(d, "return document.querySelector('#view .page') && document.querySelector('#view .page').getBoundingClientRect().height < innerHeight*2"),
           "页面高度合理", js(d, "return document.querySelector('#view .page').getBoundingClientRect().height"))
    finally:
        try:
            d.quit()
        except Exception:
            pass
        env["proc"].terminate()


# --------------------------------------------------------------------------
# U10 大数据渲染耗时
# --------------------------------------------------------------------------
def group_u10(d):
    set_route(d, "library")
    t0 = time.time()
    got = wait_js(d, "return document.querySelectorAll('#view .tr').length >= 120;", 10)
    dt = time.time() - t0
    ok("U10", "120 条收藏渲染 <3s", bool(got) and dt < 3, "<3s", "%.2fs" % dt)
    set_route(d, "playlist", "pl_big")
    t0 = time.time()
    got = wait_js(d, "return document.querySelectorAll('#view .tr').length >= 500;", 10)
    dt = time.time() - t0
    ok("U10", "500 首歌单渲染 <4s", bool(got) and dt < 4, "<4s", "%.2fs" % dt)
    # 滚动到底部
    js(d, "$('#view').scrollTop = $('#view').scrollHeight;")
    time.sleep(0.5)
    ok("U10", "500 行滚动到底不抛错", not errors(d), "0", errors(d)[:3])
    ok("U10", "500 行渲染后无横向溢出",
       js(d, "return document.documentElement.scrollWidth <= window.innerWidth + 1"),
       "无溢出", "%s/%s" % (js(d, "return document.documentElement.scrollWidth"),
                         js(d, "return window.innerWidth")))


# --------------------------------------------------------------------------
# U11 刷新持久化
# --------------------------------------------------------------------------
def group_u11(d, base):
    # 设一个确定值
    js(d, "postSettings({theme:'light'}); applyTheme('light');")
    time.sleep(0.8)
    d.get(base)
    wait_js(d, "return !!state.version;", 20)
    ok("U11", "刷新后主题保留", js(d, "return document.documentElement.dataset.theme") == "light",
       "light", js(d, "return document.documentElement.dataset.theme"))
    ok("U11", "刷新后收藏保留", js(d, "return (state.favs||[]).length") >= 120,
       "≥120", js(d, "return (state.favs||[]).length"))
    ok("U11", "刷新后歌单保留", js(d, "return (state.playlists||[]).length") >= 2,
       "≥2", js(d, "return (state.playlists||[]).length"))
    ok("U11", "刷新无 JS 异常", not errors(d), "0", errors(d)[:3])


# --------------------------------------------------------------------------
def report(elapsed):
    groups = {}
    for r in RESULTS:
        g = groups.setdefault(r["group"], {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0})
        g[r["verdict"]] += 1
    lines = ["# Elysium 前端交互压测报告", "",
             "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "- 用时：%.1fs" % elapsed,
             "- 环境：Chrome headless / 1440×900 + 390×844（CDP 视口）",
             "- 说明：上游域名由 CDP 阻断，所有联网路径按「不可达」测降级；"
             "diag 平台走本地测试音，播放链路不依赖网络。", "",
             "## 汇总", "", "| 组 | 覆盖 | PASS | FAIL | WARN | SKIP |",
             "| --- | --- | --- | --- | --- | --- |"]
    totals = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for g in sorted(groups):
        c = groups[g]
        n = sum(c.values())
        for k in totals:
            totals[k] += c[k]
        lines.append("| %s | %d | %d | %d | %d | %d |" % (g, n, c["PASS"], c["FAIL"], c["WARN"], c["SKIP"]))
    n = sum(totals.values())
    lines.append("| **合计** | **%d** | **%d** | **%d** | **%d** | **%d** |" % (
        n, totals["PASS"], totals["FAIL"], totals["WARN"], totals["SKIP"]))
    lines += ["", "**结论**：%d 项通过，%d 项失败，%d 项可改进。" % (
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

    TMP_ROOT = tempfile.mkdtemp(prefix="elysium_ui_")
    t0 = time.time()
    home = mkhome("main")
    env = spawn(home, "main")
    d = make_driver()
    try:
        if not boot_ok(d, env["base"]):
            record("XX", "首屏启动", "FAIL", "state.version + .page", "boot 超时")
        if want("U1"):
            group_u1(d, env["base"], env)
        if want("U2"):
            group_u2(d)
        if want("U3"):
            group_u3(d)
        if want("U4"):
            group_u4(d)
        if want("U5"):
            group_u5(d)
        if want("U6"):
            group_u6(d)
        if want("U7"):
            group_u7(d)
        if want("U8"):
            group_u8(d, env["base"])
        if want("U9"):
            group_u9()
        if want("U10"):
            group_u10(d)
        if want("U11"):
            group_u11(d, env["base"])
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()
        record("XX", "未捕获异常", "FAIL", "无", traceback.format_exc(limit=3))
    finally:
        try:
            d.quit()
        except Exception:
            pass
        env["proc"].terminate()

    elapsed = time.time() - t0
    rep = report(elapsed)
    out = os.path.join(REPO, "interact_report.md")
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
    return 1 if any(r["verdict"] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
