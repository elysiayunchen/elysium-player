# -*- coding: utf-8 -*-
"""P4.0 播放引擎 spike：MediaElementSource 接管 <audio> 的可行性验证。

要回答的问题只有一个——把现有单 `<audio>` 接进 Web Audio 图（为了 gapless / EQ），
会不会把已经跑通的东西撞坏。逐项验证：

  S1 接管后仍有声       —— createMediaElementSource 之后 analyser 有真实信号（非静音帧）
  S2 接管后 SMTC 完好   —— navigator.mediaSession 元数据 / playbackState / actionHandler 仍在
  S3 接管后 Range 拖动   —— /api/local/file 带 Range 回 206，页面 seek 后位置与信号都恢复
  S4 双 audio 交替可行   —— A 播完切 B，测量「A ended → B 出声」的间隔（gapless 的时间预算）
  S5 静音空洞可测量      —— 用 analyser 采样检测切换瞬间有没有断音，作为 P4 验收的尺子

用法：
    python verify_engine.py            # 跑完自动清理
    python verify_engine.py --keep     # 保留临时目录
依赖：selenium + 本机 Chrome + ffmpeg（生成可测信号的正弦测试音频）
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
    """两首正弦测试音频：能测出真实信号，静音帧测不了「有没有声」。"""
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
# 一次性注入：建 Web Audio 图、提供「峰值采样」与「空洞检测」两个工具函数。
PROBE_JS = r"""
window.__spike = (() => {
  const ctx = new AudioContext();
  const an = ctx.createAnalyser();
  an.fftSize = 2048;
  an.connect(ctx.destination);
  const srcs = new WeakMap();

  // 把 <audio> 接进图（同一元素只能接一次，重复接抛异常）
  function attach(el) {
    if (srcs.has(el)) return true;
    const s = ctx.createMediaElementSource(el);
    s.connect(an);
    srcs.set(el, s);
    return true;
  }

  function peak() {
    const buf = new Float32Array(an.fftSize);
    an.getFloatTimeDomainData(buf);
    let p = 0;
    for (let i = 0; i < buf.length; i++) p = Math.max(p, Math.abs(buf[i]));
    return p;
  }

  // 连续采样 peak，返回最大空洞（连续「静音」的毫秒数）
  // thr 默认 0.01：正弦信号远高于它，真静音远低于它
  async function gapProbe(ms, thr) {
    thr = thr === undefined ? 0.01 : thr;
    const buf = new Float32Array(an.fftSize);
    const step = 10;                    // 每 10ms 采一次
    const t0 = performance.now();
    let lastLoud = t0, maxGap = 0, samples = 0;
    while (performance.now() - t0 < ms) {
      an.getFloatTimeDomainData(buf);
      let p = 0;
      for (let i = 0; i < buf.length; i++) p = Math.max(p, Math.abs(buf[i]));
      const now = performance.now();
      samples++;
      if (p >= thr) lastLoud = now;
      else maxGap = Math.max(maxGap, now - lastLoud);
      await new Promise(r => setTimeout(r, step));
    }
    return { maxGap: maxGap, samples: samples, elapsed: performance.now() - t0 };
  }

  return {
    ctxState: () => ctx.state,
    resume: () => ctx.resume(),
    attach: attach,
    peak: peak,
    gapProbe: gapProbe,
    now: () => ctx.currentTime,
  };
})();
true;
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_spike_")
    music = make_music(os.path.join(base_dir, "music"))
    proc = d = None
    try:
        proc, base, home = spawn(base_dir)
        d = make_driver()
        d.get(base + "/")
        time.sleep(2.0)

        # 引擎 2.0 默认已经把 <audio> 接进 Web Audio 了，而本脚本要验证的是
        # 「一个干净的 <audio> 能不能被接管」。先关引擎退回原生单元素，再开始。
        d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            state.settings.engine_v2 = false;
            await engRebuild();
            done(ENG.on);
          })();
        """)
        time.sleep(0.4)

        # 把下载目录（本地曲库扫描源）指到测试音频目录
        d.execute_script(
            "return fetch('/api/store',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({op:'settings',payload:{download_dir:%s}})}).then(r=>r.json())"
            % json.dumps(music))
        time.sleep(0.3)
        files = d.execute_script(
            "return fetch('/api/local/list?refresh=1').then(r=>r.json()).then(j=>"
            "(j.data&&j.data.items||[]).map(x=>x.id))")
        if not files or len(files) < 2:
            raise SystemExit("测试音频没被扫出来：%r" % (files,))
        note("扫出测试音频 %d 个" % len(files))

        d.execute_script(PROBE_JS)
        note("Web Audio 上下文：%s" % d.execute_script("return __spike.ctxState()"))
        d.execute_script("return __spike.resume()")

        # ---------------------------------------------------------- S1 接管后仍有声
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            const a = document.getElementById('audio');
            const src0 = '/api/local/file?p=' + encodeURIComponent(%s);
            const before = __spike.peak();               // 接管前的基线（应接近 0）
            __spike.attach(a);
            a.src = src0;
            a.volume = 1;
            await a.play();
            await new Promise(r => setTimeout(r, 700));
            const p1 = __spike.peak();
            const t1 = a.currentTime;
            a.pause();
            done({before: before, peak: p1, t: t1, paused: a.paused});
          })();
        """ % json.dumps(files[0]))
        check("S1a 接管前无信号（基线干净）", r["before"] < 0.02, "peak=%.4f" % r["before"])
        check("S1b MediaElementSource 接管后仍有真实信号", r["peak"] > 0.05,
              "peak=%.4f @ t=%.2fs" % (r["peak"], r["t"]))
        check("S1c 接管后播放在走（currentTime 前进）", r["t"] > 0.3, "t=%.2f" % r["t"])

        # ---------------------------------------------------------- S2 SMTC 完好
        # app.js 的元数据是 JS 侧写入的（mediaPushMeta / mediaPushTick），
        # 这里照它同样的写法写一遍再读回——检验 Web Audio 接管有没有把这条链路撞坏。
        # 注意：OS 级 SMTC 浮层本身没法在 headless 里看，这一项只证明 API 链路完好，
        # 真机观感列在 spike 结论的「需人工确认」里。
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            const a = document.getElementById('audio');
            a.src = '/api/local/file?p=' + encodeURIComponent(%s);
            a.volume = 1;
            let err = null, wrote = null, read = null, handlerOk = [];
            try {
              await a.play();
              navigator.mediaSession.metadata = new MediaMetadata({
                title: '接管后元数据', artist: '信号组', album: 'spike',
                artwork: [],
              });
              wrote = true;
              navigator.mediaSession.playbackState = a.paused ? 'paused' : 'playing';
              const md = navigator.mediaSession.metadata;
              read = { title: md.title, artist: md.artist, state: navigator.mediaSession.playbackState };
              for (const k of ['play','pause','nexttrack','previoustrack','seekto',
                               'seekforward','seekbackward','stop']) {
                try {
                  navigator.mediaSession.setActionHandler(k, () => {});
                  handlerOk.push(k);
                } catch (e) { /* 内核不认这个动作 */ }
              }
            } catch (e) { err = String(e && e.message || e); }
            const playing = !a.paused;
            a.pause();
            done({err: err, wrote: wrote, read: read, handlers: handlerOk,
                  playing: playing, elemState: a.readyState});
          })();
        """ % json.dumps(files[0]))
        check("S2a 接管后可写入 mediaSession 元数据无异常", r["err"] is None and r["wrote"] is True,
              "err=%s" % r["err"])
        check("S2b 元数据读回一致（title/state）",
              bool(r["read"] and r["read"].get("title") == "接管后元数据"
                   and r["read"].get("state") in ("playing", "paused")),
              "%s" % (r["read"],))
        check("S2c actionHandler 仍可注册（8 个动作）", len(r["handlers"]) >= 6,
              "%d 个：%s" % (len(r["handlers"]), ",".join(r["handlers"])))
        check("S2d 接管后元素仍在真实播放", r["playing"] is True and r["elemState"] >= 2,
              "readyState=%s" % r["elemState"])

        # ---------------------------------------------------------- S3 Range 拖动
        range_probe = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          const a = document.getElementById('audio');
          (async () => {
            const r = {};
            try {
              // 直接对同源端点发 Range 请求，确认服务端仍回 206
              const target = a.src;
              const res = await fetch(target, { headers: { Range: 'bytes=1000-1999' } });
              r.status = res.status;
              r.cr = res.headers.get('Content-Range');
              r.len = res.headers.get('Content-Length');
              await res.arrayBuffer();
            } catch (e) { r.err = String(e); }
            done(r);
          })();
        """)
        check("S3a /api/local/file 带 Range 回 206", range_probe["status"] == 206,
              "status=%s cr=%s len=%s" % (range_probe["status"], range_probe["cr"], range_probe["len"]))

        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            const a = document.getElementById('audio');
            await a.play();
            await new Promise(r => setTimeout(r, 300));
            const before = a.currentTime;
            a.currentTime = 1.8;                       // 接管后拖动
            await new Promise(r => setTimeout(r, 600));
            // 有些元素 seek 后需要一次 play 才能继续出声
            if (a.paused) { try { await a.play(); } catch (e) {} }
            await new Promise(r => setTimeout(r, 300));
            const after = a.currentTime;
            const peak = __spike.peak();
            a.pause();
            done({before: before, after: after, peak: peak, dur: a.duration});
          })();
        """)
        check("S3b 接管后 seek 生效（位置到位）",
              r["after"] >= 1.8 and r["after"] > r["before"], "t %.2f -> %.2f / dur=%.2f"
              % (r["before"], r["after"], r["dur"] or 0))
        check("S3c 接管后 seek 之后仍有声", r["peak"] > 0.05, "peak=%.4f" % r["peak"])

        # ---------------------------------------------------------- S4 双 audio 交替
        # 关键测量：A 播完那一刻到 B 出声之间的间隔 —— gapless 的时间预算
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            // 建 B 通道（脱离文档树也行，但为了贴近真实实现放进 DOM）
            let b = document.getElementById('audioB');
            if (!b) {
              b = document.createElement('audio');
              b.id = 'audioB';
              b.preload = 'auto';
              document.body.appendChild(b);
            }
            __spike.attach(b);
            b.volume = 1;
            b.src = '/api/local/file?p=' + encodeURIComponent(%s);
            await b.play().catch(() => {});
            await new Promise(r => setTimeout(r, 200));
            b.pause(); b.currentTime = 0;

            const a = document.getElementById('audio');
            a.src = '/api/local/file?p=' + encodeURIComponent(%s);
            a.volume = 1;
            await a.play();
            // 并行采样静音空洞，覆盖 A 结束 → B 接上的整个窗口
            const probe = __spike.gapProbe(3800, 0.01);
            // A 结束就切 B（理想情况：提前在 A 剩 0.05s 时切，这里先测最笨的 ended 切）
            const tEnded = await new Promise(res => {
              a.addEventListener('ended', () => res(performance.now()), { once: true });
            });
            const tBStart = performance.now();
            b.currentTime = 0;
            await b.play();
            const pr = await probe;
            b.pause();
            done({
              lagMs: tBStart - tEnded,
              maxGap: pr.maxGap,
              samples: pr.samples,
              aEnded: a.ended,
              bTime: b.currentTime,
            });
          })();
        """ % (json.dumps(files[1]), json.dumps(files[0])))
        check("S4a 双 audio 可同时存在并交替播放", r["bTime"] > 0, "b.time=%.2f" % r["bTime"])
        note("ended 监听滞后：%.0fms（JS 事件回调本身的开销）" % r["lagMs"])
        note("切换窗口最大静音空洞：%.0fms（当前 ended 触发方式的实测值）" % r["maxGap"])
        check("S4b 静音空洞可被测量（尺子可用）", r["samples"] > 100, "samples=%d" % r["samples"])

        # ---------------------------------------------------------- S5 提前切换能压到多小
        # A 剩 ~120ms 时启动 B：证明 gapless 的实现路径可行，测出剩余空洞
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            const a = document.getElementById('audio');
            const b = document.getElementById('audioB');
            a.src = '/api/local/file?p=' + encodeURIComponent(%s);
            b.src = '/api/local/file?p=' + encodeURIComponent(%s);
            a.currentTime = 0; a.volume = 1;
            await a.play();
            const probe = __spike.gapProbe(3600, 0.01);
            await new Promise(res => {
              const tick = setInterval(() => {
                const d = a.duration;
                if (d && (d - a.currentTime) <= 0.12) {
                  clearInterval(tick);
                  b.currentTime = 0;
                  b.play().then(res).catch(res);
                }
              }, 8);
            });
            const pr = await probe;
            a.pause(); b.pause();
            done({maxGap: pr.maxGap, samples: pr.samples});
          })();
        """ % (json.dumps(files[0]), json.dumps(files[1])))
        note("提前 120ms 启动下一首后的最大空洞：%.0fms" % r["maxGap"])
        improved = r["maxGap"] < 400
        check("S5 提前切换能显著缩小空洞（gapless 路径成立）", improved,
              "maxGap=%.0fms" % r["maxGap"])

        # ---------------------------------------------------------- S5b 尺子标定
        # 全场 0ms 太空，得先证明这把尺子真能量出断音：
        # 人为造一个 300ms 的静音间隔，看它报不报得出来。
        r = d.execute_async_script(r"""
          const done = arguments[arguments.length-1];
          (async () => {
            const a = document.getElementById('audio');
            a.src = '/api/local/file?p=' + encodeURIComponent(%s);
            a.currentTime = 0;
            await a.play();
            await new Promise(r => setTimeout(r, 300));
            const probe = __spike.gapProbe(1200, 0.01);
            await new Promise(r => setTimeout(r, 300));
            a.pause();                                   // 人为断 300ms
            await new Promise(r => setTimeout(r, 300));
            await a.play();
            const pr = await probe;
            a.pause();
            done({maxGap: pr.maxGap});
          })();
        """ % json.dumps(files[0]))
        check("S5b 尺子标定：人造 300ms 断音能被测出", 200 < r["maxGap"] < 600,
              "maxGap=%.0fms（应≈300）" % r["maxGap"])

        # ---------------------------------------------------------- 异常收集
        errs = [e for e in d.get_log("browser")
                if e.get("level") == "SEVERE" and "favicon" not in e.get("message", "")]
        check("S6 无未捕获的严重异常", not errs,
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
