# -*- coding: utf-8 -*-
"""展开页（SP 式全屏 Now Playing）验收：桌面 + 手机真机视口。

现有 verify_ui.py / verify_karaoke.py 全部跑 1440×900，窄视口从未覆盖 ——
「打开展开栏后歌词滑不到底、看不到 K 歌按键」正是这样漏网的。本脚本用 CDP
Emulation.setDeviceMetricsOverride 精确模拟视口（headless Chrome 直接
set_window_size(390,844) 会被最小窗宽顶成 504，媒体查询根本不触发）。

断言：
  A1 展开层铺满视口、遮住播放条
  A2 歌词是页面唯一滚动容器（外层不滚，消掉嵌套滚动）
  A3 歌词能滚到最后一行且该行完整可见
  A4 所有交互键在视口内且 elementFromPoint 命中
  A5 桌面三段式：中间播放组绝对居中、左右组靠边
  A6 音质 chip 可达、qmenu 层级压过展开层
  A7 队列视图可切换并切回歌词
  A8 无未捕获的严重异常

用法：python verify_np.py            （跑完自动清理隔离目录）
依赖：selenium + 本机 Chrome；用带 selenium 的解释器跑，例如
      C:/Users/19645/miniconda3/python.exe verify_np.py
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_gapless import spawn                       # noqa: E402
from selenium import webdriver                        # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


PROBE_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const out = {errors: []};
  try {
    /* A0：静态骨架必须先正常渲染（防止「改动把 app.js 跑崩 / 页面白屏」被误判通过） */
    out.home = {
      kw: !!document.getElementById('kw'),
      shell: !!document.querySelector('.shell'),
      player: !!document.querySelector('.player'),
      view: !!document.getElementById('view'),
    };
    const orig = window.rpc;
    const lrc = Array.from({length: 42}, (_, i) =>
      `[00:${String(Math.floor(i / 2)).padStart(2, '0')}.${i % 2 ? '50' : '00'}]第 ${i + 1} 行歌词内容测试`).join('\n');
    window.rpc = (fn, p) => fn === 'getlyric' ? Promise.resolve({lyric: lrc}) : orig(fn, p);
    state.playing = {id: 'probe', name: '探针测试曲', artist: 'Elysia',
                     platform: 'local', cover: ''};
    state.currentQuality = {format: 'flac', bitrate: 2000};
    state.playList = [state.playing];
    state.playIndex = 0;
    openNp('lyric');
    await new Promise(r => setTimeout(r, 450));

    const rc = el => {
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return {top: +b.top.toFixed(1), bottom: +b.bottom.toFixed(1),
              left: +b.left.toFixed(1), right: +b.right.toFixed(1),
              h: +b.height.toFixed(1), w: +b.width.toFixed(1)};
    };
    const layer = document.getElementById('npDrawer');
    const main = document.getElementById('npBody');
    const box = document.getElementById('npLyricBody');
    const dock = document.querySelector('.np-dock');
    const lr = layer.getBoundingClientRect();

    out.vp = {w: innerWidth, h: innerHeight};
    out.layer = {
      rect: rc(layer), pos: getComputedStyle(layer).position,
      z: getComputedStyle(layer).zIndex,
      top: getComputedStyle(layer).top,
    };
    out.main = {sh: main.scrollHeight, ch: main.clientHeight,
                ov: getComputedStyle(main).overflowY};
    out.lyric = {sh: box.scrollHeight, ch: box.clientHeight,
                 ov: getComputedStyle(box).overflowY,
                 maxH: getComputedStyle(box).maxHeight,
                 lines: box.querySelectorAll('.lyric-line').length};

    // 歌词滚到底：最后一行必须完整落在容器可视区内
    box.scrollTop = box.scrollHeight;
    await new Promise(r => setTimeout(r, 80));
    const lines = box.querySelectorAll('.lyric-line');
    const last = lines[lines.length - 1];
    const lastR = last ? last.getBoundingClientRect() : null;
    const boxR = box.getBoundingClientRect();
    out.lastLine = lastR ? {
      top: +lastR.top.toFixed(1), bottom: +lastR.bottom.toFixed(1),
      boxTop: +boxR.top.toFixed(1), boxBottom: +boxR.bottom.toFixed(1),
      scrollTop: box.scrollTop,
      fullyVisible: lastR.top >= boxR.top && lastR.bottom <= boxR.bottom,
    } : null;

    // 所有可见交互键：在视口内 + 命中测试
    const sels = ['.np-ico', '.np-play', '.np-spec', '#npSeekBar', '#npVol'];
    const seen = [];
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        if (!el.getClientRects().length) continue;
        if (sel === '#npVol' && getComputedStyle(el).display === 'none') continue;
        const r = el.getBoundingClientRect();
        const x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;
        const t = document.elementFromPoint(x, y);
        seen.push({
          sel: sel, id: el.id || '', label: el.getAttribute('title') || '',
          inview: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
          hit: !!(t && (t === el || el.contains(t))),
          top: +r.top.toFixed(1), bottom: +r.bottom.toFixed(1),
        });
      }
    }
    out.keys = seen;

    // 桌面三段式：中间组中心 ≈ 视口中心
    const kc = document.querySelector('.np-kc');
    const kl = document.querySelector('.np-kl');
    const kr = document.querySelector('.np-kr');
    if (kc && kl && kr) {
      const c = kc.getBoundingClientRect(), l = kl.getBoundingClientRect(), r = kr.getBoundingClientRect();
      const pb = document.getElementById('npPlay');
      const p = pb ? pb.getBoundingClientRect() : null;
      out.three = {
        centerX: +((c.left + c.right) / 2).toFixed(1),
        vpCenter: +(innerWidth / 2).toFixed(1),
        leftX: +l.left.toFixed(1), rightX: +r.right.toFixed(1), vpW: innerWidth,
        playCenterX: p ? +((p.left + p.right) / 2).toFixed(1) : null,
        centerKeys: kc.querySelectorAll('button').length,
        leftKeys: kl.querySelectorAll('button').length,
        rightKeys: kr.querySelectorAll('button').length,
      };
    }

    // 展开层确实盖住原播放条
    const under = document.elementFromPoint(innerWidth / 2, innerHeight - 18);
    out.coversPlayer = !!(under && under.closest('.np-layer'));
    out.dock = rc(dock);

    // 音质菜单层级：手工摆一个可见菜单，验证它压在展开层之上
    const qm = document.getElementById('qmenu');
    qm.innerHTML = '<button data-act="pick-q">无损 FLAC</button>';
    qm.hidden = false;
    qm.style.left = '60px'; qm.style.top = '120px';
    const btn = qm.querySelector('button');
    const br = btn.getBoundingClientRect();
    const bt = document.elementFromPoint((br.left + br.right) / 2, (br.top + br.bottom) / 2);
    out.quality = {
      chip: rc(document.getElementById('npSpec')),
      chipAct: document.getElementById('npSpec').dataset.act,
      qz: getComputedStyle(qm).zIndex, lz: getComputedStyle(layer).zIndex,
      menuOnTop: !!(bt && (bt === btn || btn.contains(bt))),
    };
    qm.hidden = true;

    // 队列视图 → 切回歌词
    document.getElementById('npQueueBtn').click();
    await new Promise(r => setTimeout(r, 80));
    out.queue = {
      shown: !!document.querySelector('.np-queue'),
      rows: document.querySelectorAll('.np-queue .qrow').length,
      tab: state.npTab,
    };
    document.getElementById('npQueueBtn').click();
    await new Promise(r => setTimeout(r, 80));
    out.backToLyric = !!document.getElementById('npLyricBody');

    // K 歌入口仍在且可达
    const kar = document.querySelector('[data-act="karaoke"]');
    out.karaoke = !!kar;
  } catch (e) { out.errors.push(String(e)); }
  done(out);
})();
"""


def run(base, w, h, android):
    o = Options()
    for a in ("--headless=new", "--disable-gpu", "--window-size=%d,%d" % (w, h),
              "--lang=zh-CN", "--autoplay-policy=no-user-gesture-required", "--mute-audio"):
        o.add_argument(a)
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    try:
        d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1, "mobile": android})
        d.get(base + "/")
        time.sleep(1.6)
        if android:
            d.execute_script("document.documentElement.setAttribute('data-platform','android');")
        r = d.execute_async_script(PROBE_JS)
        errs = [e["message"] for e in d.get_log("browser")
                if e.get("level") == "SEVERE"
                and not any(n in e.get("message", "") for n in
                            ("favicon", "/api/local/cover", "/api/probe", "/api/store"))]
        r["browserErrors"] = errs
        return r
    finally:
        d.quit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    bd = tempfile.mkdtemp(prefix="elysium_np_")
    proc = None
    try:
        proc, base, _ = spawn(bd)
        cases = [
            ("手机 390x844", 390, 844, True),
            ("小屏 360x640", 360, 640, True),
            ("桌面 1440x900", 1440, 900, False),
        ]
        for title, w, h, android in cases:
            r = run(base, w, h, android)
            print("\n===== %s =====" % title)
            print(json.dumps({k: v for k, v in r.items() if k != "keys"},
                             ensure_ascii=False, indent=2)[:1400])
            tag = title.split()[0]

            check("%s A0 首页静态骨架正常" % tag,
                  all(r.get("home", {}).values()),
                  str(r.get("home")))
            check("%s A1 展开层铺满 / 盖住播放条" % tag,
                  not r.get("errors") and r["layer"]["pos"] == "fixed"
                  and r["layer"]["rect"]["w"] >= r["vp"]["w"]
                  and r.get("coversPlayer"),
                  "rect=%s top=%s covers=%s" % (r["layer"]["rect"],
                                                r["layer"]["top"], r.get("coversPlayer")))
            check("%s A2 歌词是唯一滚动容器" % tag,
                  r["main"]["sh"] <= r["main"]["ch"] + 2
                  and r["lyric"]["ov"] == "auto"
                  and r["lyric"]["sh"] > r["lyric"]["ch"]
                  and r["lyric"]["lines"] >= 40,
                  "外层 %d/%d 歌词 %d/%d 行数=%d" % (
                      r["main"]["sh"], r["main"]["ch"], r["lyric"]["sh"],
                      r["lyric"]["ch"], r["lyric"]["lines"]))
            check("%s A3 歌词能滚到最后一行" % tag,
                  bool(r.get("lastLine") and r["lastLine"]["fullyVisible"]),
                  str(r.get("lastLine")))
            bad = [k for k in r["keys"] if not k["inview"] or not k["hit"]]
            check("%s A4 交互键全在屏内且可点" % tag,
                  len(r["keys"]) >= 9 and not bad,
                  "共 %d 键，异常=%s" % (len(r["keys"]), bad[:3]))
            t = r.get("three", {})
            check("%s A5 播放键居中 / 三段左右对称" % tag,
                  t and t.get("playCenterX") is not None
                  and abs(t["playCenterX"] - t["vpCenter"]) <= 2
                  and abs(t["centerX"] - t["vpCenter"]) <= 2
                  and t["centerKeys"] == 3
                  and t["leftKeys"] == t["rightKeys"]
                  and t["leftX"] < t["vpW"] * 0.15
                  and t["rightX"] > t["vpW"] * 0.85,
                  str(t))
            q = r.get("quality", {})
            check("%s A6 音质 chip 与菜单层级" % tag,
                  q.get("chipAct") == "pq" and q.get("menuOnTop")
                  and int(q.get("qz") or 0) > int(q.get("lz") or 0),
                  "qz=%s lz=%s onTop=%s" % (q.get("qz"), q.get("lz"), q.get("menuOnTop")))
            check("%s A7 队列视图可切回" % tag,
                  r.get("queue", {}).get("shown") and r.get("backToLyric")
                  and r.get("karaoke"),
                  "queue=%s back=%s karaoke=%s" % (
                      r.get("queue"), r.get("backToLyric"), r.get("karaoke")))
            check("%s A8 无严重异常" % tag,
                  not r.get("errors") and not r.get("browserErrors"),
                  str(r.get("errors") or r.get("browserErrors"))[:180])
    finally:
        try:
            if proc:
                proc.terminate()
                proc.wait(timeout=10)
        except Exception:
            pass
        if args.keep:
            print("\n保留临时目录：" + bd)
        else:
            shutil.rmtree(bd, ignore_errors=True)

    ok = sum(1 for _, p, _ in RESULTS if p)
    print("\n== 结果：%d pass / %d fail ==" % (ok, len(RESULTS) - ok))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
