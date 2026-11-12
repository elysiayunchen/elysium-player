# -*- coding: utf-8 -*-
"""网络自检面板验收：/api/diag 六跳 + /api/diag/tone 测试音 + 设置抽屉按钮交互。

背景：真机 beta2 报「播放不了音频」。桌面侧全链路已验证健康（见
verify_mobile_play.py），问题只能在手机环境（网络/WebView/焦点）。这组接口把
手机变成自己的诊断仪：逐跳指认断点 + 本地测试音分流「网络问题 vs 本地音频链路」。

断言：
  A1 /api/diag 六跳全部 ok（真实网络）
  A2 /api/diag/tone 是 3 秒 WAV（RIFF 头 + 时长）
  A3 设置抽屉两个按钮存在且可点
  A4 「开始自检」渲染逐跳结果（≥6 行 ✓）
  A5 「播放测试音」主 <audio> 真实推进（currentTime>1s, duration≈3s）
  A6 手机 390 视口同样全绿
  A7 无未捕获严重异常

用法：python verify_diag.py
"""
import json
import struct
import sys
import tempfile
import shutil

sys.path.insert(0, r"D:\codex_workspace\Elysium")
from verify_gapless import spawn  # noqa: E402
from verify_mobile_play import run as run_page  # noqa: E402  # 复用驱动与探针外壳
from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402

import urllib.request

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


PROBE_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const out = {errors: [], jsErrors: []};
  window.__errs = [];
  window.addEventListener('error', e => window.__errs.push(String(e.message)));
  // 开设置抽屉
  document.getElementById('settingsBtn').click();
  await new Promise(r => setTimeout(r, 300));
  out.btns = {
    diag: !!document.getElementById('diagBtn'),
    tone: !!document.getElementById('diagToneBtn'),
  };
  // A4：跑自检
  document.getElementById('diagBtn').click();
  await new Promise(r => setTimeout(r, 200));      // 按钮先进入「检测中」态
  const t0 = Date.now();
  while (Date.now() - t0 < 60000) {
    const rows = document.querySelectorAll('#diagOut .diag-row');
    if (rows.length >= 6 && document.getElementById('diagCopy')) break;
    await new Promise(r => setTimeout(r, 400));
  }
  out.rows = Array.from(document.querySelectorAll('#diagOut .diag-row')).map(el => ({
    bad: el.classList.contains('bad'),
    name: (el.querySelector('.diag-name') || {}).textContent || '',
    detail: ((el.nextElementSibling || {}).textContent || '').slice(0, 60),
  }));
  // A5：播放测试音（走主 <audio>）
  document.getElementById('diagToneBtn').click();
  await new Promise(r => setTimeout(r, 2200));
  const a = audio();
  out.tone = {
    paused: a.paused, t: +(a.currentTime || 0).toFixed(2),
    dur: +(a.duration || 0).toFixed(2),
    src: (a.currentSrc || a.src || '').slice(-24),
    error: a.error ? a.error.code : null,
  };
  out.notifTitle = (document.querySelector('.np-title, #npTitle') || {}).textContent
    || (state.playing && state.playing.name) || '';
  out.jsErrors = window.__errs.slice(0, 5);
  done(out);
})();
"""


def main():
    bd = tempfile.mkdtemp(prefix="elysium_diag_")
    proc = None
    try:
        proc, base, home = spawn(bd)
        # A1: /api/diag 六跳
        with urllib.request.urlopen(base + "/api/diag", timeout=90) as r:
            data = json.loads(r.read().decode())
        hops = data["data"]["hops"]
        for h in hops:
            print("  hop %-28s %s %5dms %s" % (h["name"], "OK " if h["ok"] else "BAD", h["ms"], h["detail"][:70]))
        check("A1 /api/diag 六跳全部 ok", len(hops) == 6 and all(h["ok"] for h in hops),
              "; ".join("%s:%s" % (h["name"], h["detail"][:40]) for h in hops if not h["ok"]))
        # A2: /api/diag/tone
        with urllib.request.urlopen(base + "/api/diag/tone", timeout=10) as r:
            wav = r.read()
            ctype = r.headers.get("Content-Type")
        riff = wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
        rate, = struct.unpack("<I", wav[24:28])
        dlen, = struct.unpack("<I", wav[40:44])
        dur = dlen / 2 / rate
        check("A2 /api/diag/tone 是 3 秒 WAV", riff and 2.9 < dur <= 3.05
              and ctype == "audio/wav", "%s ctype=%s dur=%.2f" % (wav[:12], ctype, dur))
        # A3–A7: 页面交互（手机 + 桌面）
        for tag, w, h, android in (("手机390", 390, 844, True), ("桌面1440", 1440, 900, False)):
            r = run_page(base, 0, w, h, android) if False else None  # 外壳不用，见下
            o = Options()
            ua = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36") if android else \
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
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
                d.execute_script("window.__tonePort = arguments[0];", 0)
                res = d.execute_async_script(PROBE_JS)
                sev = [e["message"] for e in d.get_log("browser")
                       if e.get("level") == "SEVERE" and "favicon" not in e.get("message", "")]
                res["severe"] = sev[:4]
            finally:
                d.quit()
            print("\n===== %s =====" % tag)
            print(json.dumps(res, ensure_ascii=False, indent=1)[:1200])
            pre = "A3-6" if android else "桌面"
            check("%s 设置抽屉按钮齐全" % tag, res.get("btns", {}).get("diag") and res.get("btns", {}).get("tone"))
            rows = res.get("rows") or []
            check("%s 自检渲染六跳全绿" % tag, len(rows) == 6 and not any(x["bad"] for x in rows),
                  json.dumps([x for x in rows if x["bad"]], ensure_ascii=False)[:200])
            t = res.get("tone") or {}
            check("%s 测试音真实推进（%s）" % (tag, "主<audio>"),
                  (not t.get("paused")) and t.get("t", 0) > 1.0 and 2.9 < t.get("dur", 0) <= 3.05
                  and not t.get("error"), json.dumps(t, ensure_ascii=False))
            check("%s 无严重异常" % tag, not res.get("severe") and not res.get("jsErrors"),
                  json.dumps({"severe": res.get("severe"), "js": res.get("jsErrors")}, ensure_ascii=False)[:200])
    finally:
        try:
            if proc:
                proc.terminate()
        except Exception:
            pass
        shutil.rmtree(bd, ignore_errors=True)
    bad = [x for x in RESULTS if not x[1]]
    print("\n%d/%d PASS" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
