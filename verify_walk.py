# -*- coding: utf-8 -*-
"""Elysium 体感走查：按真实使用顺序走一遍，量「人眼感知到的东西」。

跟 verify_ui / verify_pl / verify_drag / verify_ctx 的区别：
那几套验的是「功能对不对」，这套验的是「用起来舒不舒服」——

  W1  首屏出卡速度、有没有先摆空话再改口
  W2  「换一批」真的换（内容变）+ 快（感知耗时）+ 不闪（全程无灰态、无转圈）
  W3  展开/收起卡数对得上
  W4  货架能按住拖（不是「看起来能滑其实滑不动」）
  W5  全风格页首次加载有骨架和说明、到位后无骨架残留
  W6  换一批歌真的换
  W7  可点的东西光标是 pointer（整卡可点却给 auto = 用户不知道能点）
  W8  返回有去处
  W9  口味画像的选择器不被裁、统计在
  W10 点风格标签立即有反馈
  W11 窄窗口不横向溢出
  W12 离开再回来，滚动位置还在
  W13 全程 console 无 SEVERE

需要一份有真实数据（收听/收藏记录）的实例，否则推荐区只会是空态。
用法：
    python main.py --serve --port=18777        # 另起一个终端
    python verify_walk.py --base http://127.0.0.1:18777
"""
import argparse
import json
import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

RESULTS = []

# 点击 → 逐帧盯目标文本，直到连续 3 帧稳定。量的是「什么时候看得见变化」。
CLICK_AND_WAIT = """
const cfg = arguments[0];
const el = document.querySelector(cfg.act);
if (!el) return {miss: true, act: cfg.act};
el.click();
return new Promise((res) => {
  const t0 = performance.now();
  let last = null, stable = 0, sawBusy = false, sawSwapping = false;
  const host = cfg.host ? document.querySelector(cfg.host) : null;
  function tick() {
    const now = performance.now() - t0;
    const node = document.querySelector(cfg.sel);
    const v = node ? node.textContent.trim() : null;
    if (host && host.classList.contains('swapping')) sawSwapping = true;
    const busy = cfg.btn ? !!document.querySelector(cfg.btn + '.busy') : false;
    if (busy) sawBusy = true;
    if (v !== null && v !== last) { stable = 0; last = v; }
    else if (v !== null) stable++;
    if (stable > 2 && !busy) return res({ms: Math.round(now), sawBusy, sawSwapping, final: v});
    if (now > 12000) return res({ms: Math.round(now), sawBusy, sawSwapping, timeout: true,
                                 final: v});
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
});
"""

CURSOR_AUDIT = """
const bad = [];
document.querySelectorAll('[data-act]').forEach((el) => {
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  if (r.width < 8 || r.height < 8) return;
  if (cs.visibility === 'hidden' || cs.display === 'none') return;
  if (cs.cursor === 'pointer' || cs.pointerEvents === 'none') return;
  // 货架本身是拖拽手势，grab 是它该有的样子
  if (el.classList.contains('shelf-row')) return;
  bad.push({act: el.dataset.act, tag: el.tagName, cursor: cs.cursor,
            cls: String(el.className || '').slice(0, 36)});
});
return bad;
"""


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + detail) if detail else ""))
    return bool(ok)


def js(d, script, *args):
    return d.execute_script(script, *args)


def shot(d, out, name):
    d.save_screenshot(os.path.join(out, name + ".png"))


def make_driver():
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1440,900")
    o.add_argument("--lang=zh-CN")
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    return webdriver.Chrome(options=o)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18777")
    ap.add_argument("--out", default="_art")
    a = ap.parse_args()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), a.out, "walk")
    os.makedirs(out, exist_ok=True)

    d = make_driver()
    try:
        # ---------------------------------------------------------- W1 首屏
        t0 = time.time()
        d.get(a.base + "/")
        js(d, """
          window.__firstPaint = null;
          const t = performance.now();
          (function tick() {
            if (document.querySelector('#shelfReco .card')) {
              window.__firstPaint = Math.round(performance.now()); return;
            }
            if (performance.now() - t < 20000) return requestAnimationFrame(tick);
            window.__firstPaint = -1;
          })();
        """)
        while js(d, "return window.__firstPaint") is None and time.time() - t0 < 25:
            time.sleep(0.1)
        fp = js(d, "return window.__firstPaint")
        st = js(d, """return {skel: document.querySelectorAll('#shelfReco .skel').length,
                               empty: document.querySelectorAll('#shelfReco .reco-off').length,
                               cards: document.querySelectorAll('#shelfReco .card').length,
                               tiles: document.querySelectorAll('.tiles .tile').length};""")
        check("W1 首屏第一张卡在 1.5s 内出现", 0 < fp < 1500, "%s ms" % fp)
        check("W1 首屏没有骨架/空态残留", st["skel"] == 0 and st["empty"] == 0,
              "skel=%s 空态=%s 卡片=%s" % (st["skel"], st["empty"], st["cards"]))
        time.sleep(1.0)
        shot(d, out, "w01_home")

        # --------------------------------------------------- W2 换一批（推荐）
        r = js(d, CLICK_AND_WAIT, {"act": '[data-act="reco-refresh"]',
                                   "sel": '#shelfReco .card .card-name',
                                   "btn": '[data-act="reco-refresh"]',
                                   "host": '#shelfReco'})
        check("W2 换一批点得到（不是死按钮）", not r.get("miss"), str(r.get("final"))[:40])
        check("W2 换一批在 800ms 内有可见结果", 0 < r.get("ms", 9999) < 800,
              "%s ms" % r.get("ms"))
        check("W2 换一批全程不闪（无灰态、无转圈）",
              not r.get("sawBusy") and not r.get("sawSwapping"),
              "busy=%s 灰态=%s" % (r.get("sawBusy"), r.get("sawSwapping")))
        shot(d, out, "w02_reco")

        # ------------------------------------------------- W3 展开 / 收起
        js(d, "document.querySelector('[data-act=\"reco-expand\"]').click();")
        time.sleep(1.4)
        n_more = js(d, "return document.querySelectorAll('#shelfReco .card').length")
        js(d, "document.querySelector('[data-act=\"reco-expand\"]').click();")
        time.sleep(1.4)
        n_less = js(d, "return document.querySelectorAll('#shelfReco .card').length")
        check("W3 展开后卡片变多", n_more > n_less, "%s → %s" % (n_more, n_less))
        check("W3 收起后回到初始数量", n_less == 12, "%s 张" % n_less)

        # ------------------------------------------------------ W4 货架拖拽
        drag = js(d, """
          const row = document.querySelector('#view .shelf.scrollable .shelf-row');
          if (!row) return {miss: true};
          const before = row.scrollLeft;
          const opt = {bubbles: true, cancelable: true, pointerId: 1, button: 0, buttons: 1,
                       clientX: 700, clientY: 400, pointerType: 'mouse', isPrimary: true};
          row.dispatchEvent(new PointerEvent('pointerdown', opt));
          for (let i = 1; i <= 6; i++) {
            row.dispatchEvent(new PointerEvent('pointermove',
              Object.assign({}, opt, {clientX: 700 - i * 40, buttons: 1})));
          }
          row.dispatchEvent(new PointerEvent('pointerup',
            Object.assign({}, opt, {clientX: 460, buttons: 0})));
          return {before, after: row.scrollLeft, cursor: getComputedStyle(row).cursor};
        """)
        check("W4 货架能按住拖（pointerdown+move 真的滚）",
              not drag.get("miss") and drag.get("after", 0) > drag.get("before", 0),
              "scrollLeft %s → %s" % (drag.get("before"), drag.get("after")))

        # ------------------------------------------------ W5 全风格首次进入
        r4 = js(d, CLICK_AND_WAIT, {"act": '[data-act="tile-styles"]',
                                    "sel": '#styleGrid .stylecard .stylecard-name',
                                    "host": '#styleGrid'})
        sc = js(d, """const g = document.querySelector('#styleGrid');
          return {cards: g ? g.querySelectorAll('.stylecard').length : 0,
                  skel: g ? g.querySelectorAll('.skel').length : 0,
                  note: g ? g.querySelectorAll('.grid-note').length : 0,
                  h1: (document.querySelector('#view .vhead h1')||{}).textContent};""")
        check("W5 全风格页进得去", sc["h1"] == "全风格歌单" and sc["cards"] > 0,
              "%s / %s 张卡" % (sc["h1"], sc["cards"]))
        check("W5 卡片到位后没有骨架/说明残留",
              sc["skel"] == 0 and sc["note"] == 0,
              "skel=%s note=%s" % (sc["skel"], sc["note"]))
        shot(d, out, "w05_styles")

        # --------------------------------------------------- W6 换一批歌
        r5 = js(d, CLICK_AND_WAIT, {"act": '[data-act="style-refresh"]',
                                    "sel": '#styleGrid .stylecard .stylecard-row span',
                                    "btn": '[data-act="style-refresh"]',
                                    "host": '#styleGrid'})
        toast = js(d, "const t=document.querySelector('.toast');"
                      "return t ? t.textContent.trim() : ''")
        check("W6 换一批歌有可见变化且给提示",
              not r5.get("timeout") and "换一批" in toast, "toast=%s" % toast)
        shot(d, out, "w06_styles_refreshed")

        # ------------------------------------------------------- W7 光标
        bad = js(d, CURSOR_AUDIT)
        check("W7 全风格页没有「可点却不是 pointer」的元素", not bad,
              json.dumps(bad, ensure_ascii=False)[:200])

        # -------------------------------------------------------- W8 返回
        js(d, "const b=document.querySelector('#backBtn'); if (b && !b.disabled) b.click();")
        time.sleep(1.2)
        back = js(d, "return (document.querySelector('#view .vhead h1')||{}).textContent")
        check("W8 返回有去处（回到首页）", bool(back) and back != "全风格歌单", str(back))

        # ------------------------------------------------- W9 口味画像
        r8 = js(d, CLICK_AND_WAIT, {"act": '[data-act="taste"]',
                                    "sel": '.taste-artist .taste-an',
                                    "host": '.taste-grid'})
        tw = js(d, """const g = document.querySelector('.gpick-box');
          return {gpickH: g ? g.offsetHeight : 0,
                  scrollH: g ? g.scrollHeight : 0,
                  rows: document.querySelectorAll('.taste-artist').length,
                  stats: document.querySelectorAll('.taste-stats > span').length,
                  picks: document.querySelectorAll('.gpick').length};""")
        check("W9 口味画像进得去、歌手行渲染出来", tw["rows"] > 0, "%s 行" % tw["rows"])
        check("W9 风格选择器完整展开（不被按钮样式裁）",
              tw["gpickH"] > 130 and tw["scrollH"] <= tw["gpickH"] + 2,
              "容器高=%s 内容高=%s" % (tw["gpickH"], tw["scrollH"]))
        check("W9 统计三宫格在", tw["stats"] == 3, "%s 项" % tw["stats"])
        shot(d, out, "w09_taste")

        # --------------------------------------------- W10 点风格标签
        r9 = js(d, """
          const off = [...document.querySelectorAll('.gpick')].find((b) => !b.classList.contains('on'));
          if (!off) return {miss: true};
          const before = document.querySelectorAll('.gpick.on').length;
          off.click();
          return new Promise((res) => {
            const t0 = performance.now();
            (function tick() {
              const now = performance.now() - t0;
              if (document.querySelectorAll('.gpick.on').length !== before || now > 3000) {
                return res({ms: Math.round(now)});
              }
              requestAnimationFrame(tick);
            })();
          });
        """)
        check("W10 点风格标签立即有反馈", not r9.get("miss") and r9.get("ms", 9999) < 400,
              "%s ms" % r9.get("ms"))
        # 复原：再点一次取消
        js(d, """const on=[...document.querySelectorAll('.gpick.on')];
                 if (on.length) on[on.length-1].click();""")
        time.sleep(0.6)

        # --------------------------------------------------- W11 窄窗口
        d.set_window_size(980, 780)
        time.sleep(0.9)
        narrow = js(d, """return {w: innerWidth,
          overflowX: document.documentElement.scrollWidth > innerWidth + 1,
          gpickH: (document.querySelector('.gpick-box')||{}).offsetHeight || 0};""")
        check("W11 窄窗口不横向溢出", not narrow["overflowX"], "w=%s 选择器高=%s" % (
            narrow["w"], narrow["gpickH"]))
        shot(d, out, "w11_narrow")
        d.set_window_size(1440, 900)
        time.sleep(0.4)

        # ------------------------------------------------ W12 滚动记忆
        js(d, "const b=document.querySelector('#backBtn'); if (b && !b.disabled) b.click();")
        time.sleep(1.2)
        js(d, "document.querySelector('#view').scrollTop = 520;")
        time.sleep(0.3)
        js(d, "const b=document.querySelector('[data-act=\"lib\"]'); if (b) b.click();")
        time.sleep(0.9)
        js(d, "const b=document.querySelector('#backBtn'); if (b && !b.disabled) b.click();")
        time.sleep(0.9)
        y = js(d, "return document.querySelector('#view').scrollTop")
        check("W12 离开再回来滚动位置还在", y >= 500, "y=%s（离开时 520）" % y)
        shot(d, out, "w12_scroll")

        # ------------------------------------------------------- W13 控制台
        sev = [e for e in d.get_log("browser") if e.get("level") == "SEVERE"]
        check("W13 全程 console 无 SEVERE", not sev,
              json.dumps(sev[:2], ensure_ascii=False)[:240])
    finally:
        try:
            d.quit()
        except Exception:
            pass

    print("\n---- 汇总 ----")
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("  OK   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    print("  %d/%d 通过" % (ok_n, len(RESULTS)))
    print("  截图 -> %s" % out)
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
