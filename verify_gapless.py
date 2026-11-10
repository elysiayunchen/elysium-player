# -*- coding: utf-8 -*-
"""播放引擎 2.0（P4）验收：双元素无缝播放 / 交叉淡入 / EQ。

P4.0 spike 只回答了「能不能接管」，这个脚本回答「接管之后对不对」：

  G0 引擎已接管      —— ENG.on / 两个元素 / AudioContext 在跑
  G1 单曲出声        —— 接管后 analyser 有真实信号（不是静音假阳性）
  G2 无缝切换        —— 曲末自动接下一首，静音空洞 < 40ms，state 也确实换了曲
  G3 不串音          —— 过渡结束后只有一个元素在播
  G4 新曲在响        —— 切换后 peak 正常、currentTime 前进
  G5 交叉淡入        —— crossfade 2s 时两元素同时出声的窗口 > 900ms
  G6 EQ 生效         —— 500Hz 段拉到 -12dB，440Hz 正弦 peak 明显下降
  G7 EQ 开关不断音    —— 关掉后 peak 回升且有声
  G8 引擎可退         —— 关掉引擎后只剩一个 <audio>，原生路径仍能出声
  G9 无严重异常

用法：
    python verify_gapless.py            # 跑完自动清理
    python verify_gapless.py --keep     # 保留临时目录
依赖：selenium + 本机 Chrome + ffmpeg
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

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def note(text):
    print("      · " + text)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_music(dir_):
    """两首 3 秒正弦：能测出真实信号，静音帧测不了「有没有声」。"""
    os.makedirs(dir_, exist_ok=True)
    specs = [("a440.mp3", 440, 3.0, "测试音 A", "信号组"),
             ("b880.mp3", 880, 3.0, "测试音 B", "信号组")]
    for name, freq, dur, title, artist in specs:
        p = os.path.join(dir_, name)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=%d:duration=%s:sample_rate=44100" % (freq, dur),
             "-ac", "1", "-b:a", "128k",
             "-metadata", "title=" + title, "-metadata", "artist=" + artist, p],
            check=True)
    return dir_


def spawn(base_dir):
    home = os.path.join(base_dir, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):          # ELYSIUM_HOME 兼作资源根（打包版约定）
        dst = os.path.join(home, item)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(REPO, item), dst,
                        ignore=shutil.ignore_patterns("__pycache__"))
    port = _free_port()
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
    print(open(os.path.join(base_dir, "server.log"), "rb").read().decode("utf-8", "replace")[:1500])
    proc.terminate()
    raise SystemExit("isolated server never came up")


def make_driver():
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1440,900")
    o.add_argument("--lang=zh-CN")
    o.add_argument("--autoplay-policy=no-user-gesture-required")
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    d.set_window_size(1440, 900)
    d.set_page_load_timeout(45)
    return d


# ---------------------------------------------------------------- 页面内探针
# 把 analyser 插进 ENG.master 与 destination 之间：单路输出，音量不变。
PROBE_JS = r"""
window.__g = (() => {
  const ctx = ENG.ctx;
  const an = ctx.createAnalyser();
  an.fftSize = 2048;
  try { ENG.master.disconnect(); } catch (e) {}
  ENG.master.connect(an);
  an.connect(ctx.destination);

  function peak() {
    const buf = new Float32Array(an.fftSize);
    an.getFloatTimeDomainData(buf);
    let p = 0;
    for (let i = 0; i < buf.length; i++) p = Math.max(p, Math.abs(buf[i]));
    return p;
  }

  // 连续采样，返回最长「静音」毫秒数与「两个元素同时出声」的毫秒数
  async function watch(ms, thr) {
    thr = thr === undefined ? 0.02 : thr;
    const buf = new Float32Array(an.fftSize);
    const t0 = performance.now();
    let lastLoud = t0, maxGap = 0, overlap = 0, samples = 0;
    while (performance.now() - t0 < ms) {
      an.getFloatTimeDomainData(buf);
      let p = 0;
      for (let i = 0; i < buf.length; i++) p = Math.max(p, Math.abs(buf[i]));
      const now = performance.now();
      samples++;
      if (p >= thr) lastLoud = now;
      else maxGap = Math.max(maxGap, now - lastLoud);
      const live = ENG.els.filter((el) => !el.paused && el.currentTime > 0).length;
      if (live >= 2) overlap += 10;
      await new Promise(r => setTimeout(r, 10));
    }
    return { maxGap: maxGap, overlap: overlap, samples: samples };
  }

  return { peak: peak, watch: watch, ctx: () => ENG.ctx.state };
})();
true;
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_gapless_")
    music = make_music(os.path.join(base_dir, "music"))
    proc = d = None
    try:
        proc, base, home = spawn(base_dir)
        d = make_driver()
        d.get(base + "/")
        time.sleep(2.2)

        # 下载目录指到测试音频，扫出两首
        d.execute_script(
            "return fetch('/api/store',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({op:'settings',payload:{download_dir:%s}})}).then(r=>r.json())"
            % json.dumps(music))
        time.sleep(0.3)
        files = d.execute_script(
            "return fetch('/api/local/list?refresh=1').then(r=>r.json()).then(j=>"
            "(j.data&&j.data.items||[]))")
        if not files or len(files) < 2:
            raise SystemExit("测试音频没被扫出来：%r" % (files,))
        note("扫出测试音频 %d 个" % len(files))

        # ---------------------------------------------------------- G0 引擎接管
        st = d.execute_script("return {on: ENG.on, n: ENG.els.length, ctx: ENG.ctx && ENG.ctx.state,"
                              " eq: ENG.eq.length, cross: ENG.cross};")
        check("G0a 引擎 2.0 已接管（桌面默认开）", st["on"] is True, "%s" % (st,))
        check("G0b 两个 <audio> 都已进图", st["n"] == 2, "元素数=%s" % st["n"])
        check("G0c EQ 链 10 段已建好", st["eq"] == 10, "段数=%s" % st["eq"])
        d.execute_script("return ENG.ctx.resume()")
        time.sleep(0.3)
        d.execute_script(PROBE_JS)
        note("AudioContext：%s" % d.execute_script("return __g.ctx()"))

        # ---------------------------------------------------------- G1 单曲出声
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            await playAt(%s.slice(0, 2), 0);
            await new Promise(r => setTimeout(r, 800));
            done({peak: __g.peak(), t: audio().currentTime, paused: audio().paused});
          })();
        """ % json.dumps(files))
        check("G1 接管后单曲正常出声", r["peak"] > 0.03 and r["t"] > 0.3,
              "peak=%.4f t=%.2fs" % (r["peak"], r["t"]))

        # ---------------------------------------------------------- G2/G3/G4 无缝切换
        # 从当前曲末前取样，覆盖「a 结束 → b 接上」的整个窗口
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            const w = __g.watch(2200, 0.02);
            const pr = await w;
            const after = {
              idx: state.playIndex,
              id: state.playing && state.playing.id,
              peak: __g.peak(),
              t: audio().currentTime,
              live: ENG.els.filter(el => !el.paused).length,
              slot: ENG.slot,
            };
            await new Promise(r => setTimeout(r, 400));
            after.peak2 = __g.peak();
            after.t2 = audio().currentTime;
            done(Object.assign({maxGap: pr.maxGap}, after));
          })();
        """)
        note("切换窗口最大静音空洞：%.0fms" % r["maxGap"])
        check("G2 曲末无缝接上下一首（空洞 < 40ms）", r["maxGap"] < 40,
              "maxGap=%.0fms" % r["maxGap"])
        check("G2b 状态确实切到了下一首", str(r["id"]) == str(files[1]["id"]),
              "playing=%s playList[1]=%s" % (r["id"], files[1]["id"]))
        check("G3 过渡结束后只有一个元素在播（不串音）", r["live"] == 1, "在播元素=%s" % r["live"])
        check("G4 新曲在响且进度在走",
              r["peak2"] > 0.03 and r["t2"] > r["t"], "peak=%.4f t %.2f->%.2f"
              % (r["peak2"], r["t"], r["t2"]))

        # ---------------------------------------------------------- G5 交叉淡入
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            audio().pause();
            state.settings.crossfade_ms = 2000;
            ENG.cross = engCrossSeconds();
            await playAt(state.playList, 0);
            const log = [];
            const iv = setInterval(() => {
              log.push([+(audio().currentTime || 0).toFixed(2), ENG.pre ? 1 : 0,
                        +ENG.cross.toFixed(2), ENG.slot,
                        ENG.els.map(el => el.paused ? 0 : 1).join('')]);
            }, 200);
            const pr = await __g.watch(2600, 0.02);
            clearInterval(iv);
            audio().pause();
            done(Object.assign(pr, {log: JSON.stringify(log)}));
          })();
        """)
        note("crossfade 采样 [当前时间, 已预载, 交叉时长, 槽位, 元素播放态]：%s" % r["log"])
        check("G5 crossfade 2s 时两首确实叠着响（重叠 > 900ms）", r["overlap"] > 900,
              "重叠≈%dms 空洞=%.0fms" % (r["overlap"], r["maxGap"]))
        check("G5b 交叉全程无静音（空洞 < 40ms）", r["maxGap"] < 40, "maxGap=%.0fms" % r["maxGap"])

        # ---------------------------------------------------------- G6/G7 EQ
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            state.settings.crossfade_ms = 0;
            ENG.cross = engCrossSeconds();
            state.settings.eq_enabled = true;
            state.settings.eq_preset = 'custom';
            const flat = [0,0,0,0,0,0,0,0,0,0];
            state.settings.eq_gains = flat.slice();
            engApplyEq(true);
            await playAt(state.playList, 0);
            audio().currentTime = 0.8;
            await new Promise(r => setTimeout(r, 500));
            const base = __g.peak();
            // 只把 500Hz 段拉到 -12dB：440Hz 落在它的通带里，应该听得出塌下去
            const cut = flat.slice(); cut[4] = -12;
            state.settings.eq_gains = cut;
            engApplyEq();
            await new Promise(r => setTimeout(r, 600));
            const low = __g.peak();
            const liveAfterCut = !audio().paused;
            state.settings.eq_gains = flat.slice();
            engApplyEq();
            await new Promise(r => setTimeout(r, 600));
            const back = __g.peak();
            state.settings.eq_enabled = false;
            engApplyEq();
            await new Promise(r => setTimeout(r, 400));
            const off = __g.peak();
            audio().pause();
            done({base: base, low: low, back: back, off: off, live: liveAfterCut});
          })();
        """)
        drop = (r["base"] - r["low"]) / max(r["base"], 1e-6)
        check("G6 EQ 500Hz 段 -12dB 后 440Hz 明显衰减（> 20%）", drop > 0.2,
              "peak %.2f -> %.2f（-%.0f%%）" % (r["base"], r["low"], drop * 100))
        check("G6b 段值复位后响应回到原位", r["back"] > r["low"] * 1.15,
              "peak %.2f -> %.2f" % (r["low"], r["back"]))
        check("G7 关掉 EQ 后仍有声（不是把输出关没了）", r["off"] > 0.03 and r["live"],
              "peak=%.2f" % r["off"])

        # ---------------------------------------------------------- G8 引擎可退
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            audio().pause();
            state.settings.engine_v2 = false;
            await engRebuild();
            const n = document.querySelectorAll('audio').length;
            const on = ENG.on;
            await playAt(state.playList, 0);
            await new Promise(r => setTimeout(r, 700));
            const el = document.getElementById('audio');
            const out = {
              n: n, on: on, t: el ? el.currentTime : -1,
              paused: el ? el.paused : true, src: el ? !!el.src : false,
            };
            el.pause();
            done(out);
          })();
        """)
        check("G8a 关掉引擎后只剩一个 <audio> 且不再接管", r["n"] == 1 and r["on"] is False,
              "元素数=%s ENG.on=%s" % (r["n"], r["on"]))
        check("G8b 原生路径仍能播放（有 src、进度在走）",
              r["src"] and r["t"] > 0.3 and not r["paused"],
              "t=%.2f src=%s" % (r["t"], r["src"]))

        # ---------------------------------------------------------- G9 异常
        # 测试用的 mp3 没内嵌封面，/api/local/cover 必然 404 —— 夹具噪音，不算代码问题
        noise = ("favicon", "/api/local/cover")
        errs = [e for e in d.get_log("browser")
                if e.get("level") == "SEVERE"
                and not any(n in e.get("message", "") for n in noise)]
        check("G9 无未捕获的严重异常", not errs,
              "; ".join(e["message"][:120] for e in errs[:3]))

    finally:
        try:
            if d:
                d.quit()
        except Exception:
            pass
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
