# -*- coding: utf-8 -*-
"""移动端播放实测：真实 /api/media 代理链路（非 mock），手机视口 + Android 平台。

背景：真机 beta2 报「播放不了音频」。桌面链路逐项审计全部通过（bind 无断点、
Range/206 正确、autoplay 放行、CA 有 chaquopy 兜底）。本脚本把「移动端」两种
解读里的本地可测者钉死：≤480px 手机布局 + IS_ANDROID=true + 引擎关闭（Android
默认路径），走 startTrack → /api/media → 服务端代理 → 本地直链音频文件的完整链，
断言 timeupdate 真的在走、无未捕获异常。

音频不 mock：用 /api/media?url=http://127.0.0.1:<port>/tone.wav 直链模式，
服务器按 _proxy_stream 原样转发 —— 与真机在线播放同一条服务器代码路径，
只是把上游 CDN 换成本地 http 文件服务（排除外网变量）。

用法：python verify_mobile_play.py [desktop|mobile|all]
依赖：selenium + 本机 Chrome（miniconda base 的 python 有 selenium）。
"""
import http.server
import json
import math
import struct
import sys
import threading
import wave

sys.path.insert(0, r"D:\codex_workspace\Elysium")
from verify_gapless import spawn  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402

REPO = r"D:\codex_workspace\Elysium"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def make_tone_wav(path, seconds=6.0, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for i in range(int(rate * seconds)):
            v = int(28000 * math.sin(2 * math.pi * 440 * i / rate))
            w.writeframesraw(struct.pack("<h", v))


class ToneServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def start_tone_server():
    import os
    import tempfile
    d = tempfile.mkdtemp(prefix="elysium_tone_")
    wav = os.path.join(d, "tone.wav")
    make_tone_wav(wav)
    hs = ToneServer(("127.0.0.1", 0), lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=d, **kw))
    port = hs.server_address[1]
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    return hs, port


PROBE_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const out = {errors: [], jsErrors: []};
  window.__errs = [];
  window.addEventListener('error', e => window.__errs.push(String(e.message)));
  // 真实链路入口：playAt → ensureMinfo（有 minfo 直接过）→ qualityRank →
  // startTrack → mediaSrc（此处换直链，排除上游变量）→ /api/media 代理
  window.mediaSrc = () => '/api/media?url=' +
    encodeURIComponent('http://127.0.0.1:' + window.__tonePort + '/tone.wav') +
    '&key=mt&ext=wav';
  const item = {id: 'probe1', platform: 'kuwo', name: '探针曲', artist: 't',
                cover: '',
                minfo: [{format: 'wav', bitrate: 1411}, {format: 'mp3', bitrate: 128}]};
  state.playing = item;
  try {
    await playAt([item], 0, {source: 'probe'});
  } catch (e) {
    out.startErr = String(e && e.message);
  }
  await new Promise(r => setTimeout(r, 3000));
  const a = audio();
  out.after = {
    paused: a.paused, ended: a.ended,
    t: +(a.currentTime || 0).toFixed(2),
    dur: +(a.duration || 0).toFixed(2),
    readyState: a.readyState, networkState: a.networkState,
    src: (a.currentSrc || a.src || '').slice(0, 60),
    error: a.error ? (a.error.code + ':' + (a.error.message || '')) : null,
  };
  out.jsErrors = window.__errs.slice(0, 5);
  out.toast = (() => {
    const t = document.querySelector('#toasts .toast:last-child');
    return t ? t.textContent.slice(0, 80) : '';
  })();
  done(out);
})();
"""


def run(base, tone_port, w, h, android):
    o = Options()
    ua = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36") if android else \
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    for a in ("--headless=new", "--disable-gpu", "--window-size=%d,%d" % (w, h),
              "--lang=zh-CN", "--autoplay-policy=no-user-gesture-required",
              "--mute-audio", "--user-agent=" + ua):
        o.add_argument(a)
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    try:
        d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1, "mobile": android})
        d.get(base + "/")
        d.execute_script("window.__tonePort = arguments[0];", tone_port)
        r = d.execute_async_script(PROBE_JS)
        sev = [e["message"] for e in d.get_log("browser")
               if e.get("level") == "SEVERE"
               and "favicon" not in e.get("message", "")]
        r["severe"] = sev[:5]
        return r
    finally:
        d.quit()


def main():
    import shutil
    import tempfile
    import time
    bd = tempfile.mkdtemp(prefix="elysium_mp_")
    hs, tone_port = start_tone_server()
    proc = None
    try:
        proc, base, home = spawn(bd)
        cases = [("手机390", 390, 844, True), ("桌面1440", 1440, 900, False)]
        for tag, w, h, android in cases:
            r = run(base, tone_port, w, h, android)
            print("\n===== %s =====" % tag)
            print(json.dumps(r, ensure_ascii=False, indent=1)[:900])
            a = r.get("after") or {}
            ok = (not r.get("startErr") and not a.get("paused") and a.get("t", 0) > 2.0
                  and a.get("dur", 0) > 5.0 and not a.get("error")
                  and not r.get("severe") and not r.get("jsErrors"))
            check("%s 播放链路走通（timeupdate>2s 无错误）" % tag, ok,
                  json.dumps({"after": a, "err": r.get("startErr"),
                              "severe": r.get("severe"), "js": r.get("jsErrors"),
                              "toast": r.get("toast")}, ensure_ascii=False)[:300])
    finally:
        try:
            if proc:
                proc.terminate()
        except Exception:
            pass
        hs.shutdown()
        shutil.rmtree(bd, ignore_errors=True)
    bad = [x for x in RESULTS if not x[1]]
    print("\n%d/%d PASS" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
