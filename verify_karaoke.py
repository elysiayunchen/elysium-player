# -*- coding: utf-8 -*-
"""K 歌（P8）验收：后端作品端点 + 全屏演唱层 + 「唱完保存」端到端闭环。

B 组（后端，纯标准库 urllib）
  B1  空列表          B2  raw WAV 上传返回 id      B3  meta 字段与大小
  B4  落盘字节一致    B5  回放 200 / RIFF / wav    B6  Range 206
  B7  非法 id 一律 400（路径穿越）                B8  小于 WAV 头拒收
  B9  export 不含 works                          B10 /api/store 非法 id 不入库
  B11 删除后列表空、文件清干净                    B12 重复删除幂等

U 组（Chrome 假麦端到端）
  U1  K 歌层 CSS：fixed / z-index 70 / 铺满视口 / 默认 hidden
  U2  明暗主题背景与文字色可区分
  U3  打开层 → ready 态                          U4  倒数 → sing 态
  U5  录音中：chunks 累积、麦克风电平、进度、监听总线在跑
  U5b 开唱后倒数字清场 + 音准轨几何居中（旧版偏右 51px）
  U6  karStop → review 态、WAV blob 生成
  U7  ★ 保存（raw octet-stream）→ 列表 +1 → 文件非静音 / 双声道 16bit / 时长正确
  U8  作品页渲染                                 U9  作品回放走 /api/works/file
  U10 关闭后 AudioContext / <audio> / 麦克风全释放
  U12 耳返延迟自检面板（假麦下出结果/明确失败原因，不校数字）
  U11 无未捕获的严重异常

U7 是这套自检存在的理由：保存若误用 post()（固定 JSON.stringify），
Blob 会变成 "{}"，后端按 Content-Length 只读到 2 字节直接拒，闭环立刻断。

用法：
    python verify_karaoke.py            # 跑完自动清理
    python verify_karaoke.py --keep     # 保留临时目录（看 server.log / 作品文件）
依赖：selenium + 本机 Chrome（需支持 --use-fake-device-for-media-stream）
"""
import argparse
import glob
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_gapless import spawn                      # noqa: E402
from selenium import webdriver                       # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def note(text):
    print("      · " + text)


def req(base, path, data=None, headers=None, raw=False):
    r = urllib.request.Request(
        base + path, data=data,
        headers=headers or {"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        body = resp.read()
        return (resp.status, dict(resp.headers), body) if raw else json.loads(body)


def jpost(base, path, payload):
    return req(base, path, data=json.dumps(payload).encode("utf-8"),
               headers={"Content-Type": "application/json"})


def wav_bytes(seconds=3.0, rate=44100, freq=440):
    """立体声 16bit 正弦：既是 K 歌伴奏夹具，也是「录音非静音」的信号基准。"""
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<hh", v, v)
    head = (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
            + b"data" + struct.pack("<I", len(frames)))
    return head + bytes(frames)


def wav_peak(body):
    """读 16bit PCM 峰值，判断录音不是「一整段静音文件」。"""
    if len(body) < 44 or body[:4] != b"RIFF":
        return -1.0
    n = struct.unpack_from("<I", body, 40)[0]
    data = body[44:44 + n]
    if len(data) < 2:
        return 0.0
    vals = struct.unpack("<%dh" % (len(data) // 2), data[:len(data) // 2 * 2])
    return max(abs(v) for v in vals) / 32768.0


def wait_js(driver, expr, timeout=25):
    end = time.time() + timeout
    last = None
    # 括号包住：多行 IIFE 以换行开头时，`return\n(function...)` 会被 ASI
    # 当成 `return;`，轮询永远拿不到结果。
    script = expr if expr.lstrip().startswith("return") else "return (%s)" % expr
    while time.time() < end:
        try:
            last = driver.execute_script(script)
        except Exception:
            last = None
        if last:
            return last
        time.sleep(0.2)
    return last


def make_driver():
    o = Options()
    for a in ("--headless=new", "--disable-gpu", "--window-size=1440,900",
              "--lang=zh-CN", "--autoplay-policy=no-user-gesture-required",
              "--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream"):
        o.add_argument(a)
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    d.set_window_size(1440, 900)
    d.set_page_load_timeout(45)
    d.set_script_timeout(60)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_kar_")
    proc = None
    d = None
    wid = ""
    new_rows = []
    try:
        proc, base, home = spawn(base_dir)
        wav = wav_bytes()

        # ---------------------------------------------------------- B 组后端
        st = req(base, "/api/works")
        check("B1 空作品列表", st == {"ok": True, "data": []}, str(st)[:90])

        q = ("?name=%E6%B5%8B%E8%AF%95%E4%BC%B4%E5%A5%8F"
             "&artist=%E4%BF%A1%E5%8F%B7%E7%BB%84&platform=work"
             "&sid=seed&duration=3")
        st = req(base, "/api/works/upload" + q, data=wav)
        wid = (st.get("data") or {}).get("id", "")
        check("B2 上传 raw WAV 返回 id", bool(st.get("ok")) and bool(wid),
              "id=%s size=%d" % (wid, len(wav)))

        rows = req(base, "/api/works").get("data") or []
        m = rows[0] if rows else {}
        need = ("id", "name", "artist", "platform", "sid", "cover",
                "duration", "created", "size")
        check("B3 meta 字段齐全且 size 与上传一致",
              all(k in m for k in need) and m.get("size") == len(wav),
              "name=%s size=%s" % (m.get("name"), m.get("size")))

        # data_dir() = ELYSIUM_HOME/APP_NAME，不 hardcode 应用目录名：
        # 在隔离 home 里递归找，顺便证明作品没写到别处去。
        hits = glob.glob(os.path.join(home, "**", wid + ".wav"), recursive=True)
        p = hits[0] if hits else ""
        disk = open(p, "rb").read() if p else b""
        check("B4 落盘字节与上传完全一致", disk == wav, "路径=%s" % p)

        code, hdr, body = req(base, "/api/works/file?id=" + wid, raw=True)
        check("B5 回放 200 + RIFF + audio/wav",
              code == 200 and body[:4] == b"RIFF"
              and "audio/wav" in hdr.get("Content-Type", ""),
              "status=%s type=%s" % (code, hdr.get("Content-Type")))

        r = urllib.request.Request(base + "/api/works/file?id=" + wid,
                                   headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            part, cr, rc = resp.read(), resp.headers.get("Content-Range"), resp.status
        check("B6 Range 206 且长度精确", rc == 206 and len(part) == 100,
              "status=%s range=%s bytes=%d" % (rc, cr, len(part)))

        blocked = []
        for bad in ("..%2fdata", "abc%2f..%2f..%2fetc", "A_B", ""):
            try:
                urllib.request.urlopen(base + "/api/works/file?id=" + bad, timeout=10)
                blocked.append(False)
            except urllib.error.HTTPError as e:
                blocked.append(e.code == 400)
        check("B7 非法 id（含路径穿越）一律 400", all(blocked), str(blocked))

        st = req(base, "/api/works/upload?name=x", data=b"{}")
        check("B8 小于 WAV 头（44B）拒收且不生成条目",
              st.get("ok") is False and len(req(base, "/api/works")["data"]) == 1,
              str(st)[:90])

        exp = req(base, "/api/store/export")
        check("B9 export 不含 works（作品不同步不外流）",
              "works" not in (exp.get("data") or {}), str(exp.get("data", {}).keys())[:120])

        jpost(base, "/api/store", {"op": "work_add", "payload": {"id": "../evil", "name": "x"}})
        check("B10 work_add 非法 id 被白名单挡掉",
              len(req(base, "/api/works")["data"]) == 1, "列表长度应仍为 1")

        # ---------------------------------------------------------- U 组前端
        d = make_driver()
        d.get(base + "/")
        time.sleep(2.0)

        u1 = d.execute_script(r"""
        return (() => {
          const layer = document.getElementById('singLayer');
          const wasHidden = layer.hidden;
          layer.hidden = false;
          const cs = getComputedStyle(layer);
          const rc = layer.getBoundingClientRect();
          const out = {
            wasHidden: wasHidden, pos: cs.position, z: cs.zIndex, disp: cs.display,
            w: rc.width, h: rc.height, vw: innerWidth, vh: innerHeight,
            bg: cs.backgroundColor,
            micIcon: !!document.getElementById('i-mic'),
            nav: !!document.querySelector('[data-nav="works"]'),
            ctlBg: getComputedStyle(document.getElementById('kCtl')).backgroundColor,
            linesH: document.getElementById('kLines').getBoundingClientRect().height,
            ctlH: document.getElementById('kCtl').getBoundingClientRect().height,
          };
          layer.hidden = wasHidden;
          return out;
        })();
        """)
        check("U1 K 歌层 CSS：fixed / z70 / 铺满 / 默认 hidden / 控件有尺寸",
              u1["wasHidden"] and u1["pos"] == "fixed" and u1["z"] == "70"
              and u1["disp"] == "flex" and u1["w"] >= u1["vw"]
              and u1["h"] >= u1["vh"] and u1["micIcon"] and u1["nav"]
              and u1["linesH"] > 0 and u1["ctlH"] > 0,
              "pos=%s z=%s disp=%s %dx%d/%dx%d lines=%d ctl=%d"
              % (u1["pos"], u1["z"], u1["disp"], u1["w"], u1["h"],
                 u1["vw"], u1["vh"], u1["linesH"], u1["ctlH"]))

        u2 = d.execute_script(r"""
        return (() => {
          const html = document.documentElement;
          const orig = html.getAttribute('data-theme');
          const layer = document.getElementById('singLayer');
          const out = {};
          for (const t of ['dark', 'light']) {
            html.setAttribute('data-theme', t);
            out[t] = {
              bg: getComputedStyle(layer).backgroundColor,
              ink: getComputedStyle(document.querySelector('.k-title')).color,
            };
          }
          html.setAttribute('data-theme', orig || 'dark');
          return out;
        })();
        """)
        check("U2 明暗两套主题背景/文字色可区分",
              u2["dark"]["bg"] != u2["dark"]["ink"]
              and u2["light"]["bg"] != u2["light"]["ink"]
              and u2["dark"]["bg"] != u2["light"]["bg"],
              "dark=%s/%s light=%s/%s" % (u2["dark"]["bg"], u2["dark"]["ink"],
                                          u2["light"]["bg"], u2["light"]["ink"]))

        d.execute_script("window.__kItem = arguments[0];", {
            "platform": "work", "id": wid, "name": "测试伴奏",
            "artist": "信号组", "cover": "", "duration": 3,
        })
        u3 = d.execute_async_script(r"""
        const done = arguments[arguments.length - 1];
        (async () => {
          const orig = window.rpc;
          if (typeof orig === 'function') {
            window.rpc = (fn, p) => (fn === 'getlyric'
              ? Promise.resolve({ lyric: '' }) : orig(fn, p));
          }
          try {
            await karaokeStart(window.__kItem);
            done({
              hidden: document.getElementById('singLayer').hidden,
              phase: KAR.phase, ready: !!document.querySelector('.k-ready'),
              beginH: document.getElementById('kBegin')
                ? document.getElementById('kBegin').getBoundingClientRect().height : 0,
              title: document.getElementById('kTitle').textContent,
              stubbed: typeof orig === 'function',
            });
          } catch (e) { done({ error: String(e) }); }
        })();
        """)
        check("U3 打开 K 歌层 → ready 态、开始按钮有尺寸",
              not u3.get("error") and u3.get("hidden") is False
              and u3.get("phase") == "ready" and u3.get("ready")
              and u3.get("beginH", 0) >= 36,
              str(u3)[:160])

        d.execute_script("document.getElementById('kBegin').click();")
        phase = wait_js(d, "KAR.phase === 'sing' ? KAR.phase : null", timeout=30)
        check("U4 假麦授权 → 倒数 → sing 态", phase == "sing",
              "phase=%s（拿不到麦克风会停在 ready）" % phase)

        # Chrome 假麦是断续 beep，瞬时采样会撞上静音段 —— 开采样窗口取峰值
        d.execute_script(r"""
        window.__karSamples = [];
        window.__karTimer = setInterval(() => {
          window.__karSamples.push({
            mic: KAR.micLevel || 0,
            width: parseFloat(document.getElementById('kMicLvl').style.width) || 0,
          });
        }, 40);
        """)
        time.sleep(1.5)
        d.execute_script("clearInterval(window.__karTimer);")
        samples = d.execute_script("return window.__karSamples;")
        mic_max = max([s["mic"] for s in samples] or [0])
        width_max = max([s["width"] for s in samples] or [0])
        u5 = d.execute_script(r"""
        return ({
          phase: KAR.phase,
          chunks: KAR.chunks.length,
          frames: KAR.chunks.reduce((n, c) => n + c[0].length, 0),
          busPeak: (() => {
            let p = 0;
            for (const c of KAR.chunks) {
              for (const v of c[0]) p = Math.max(p, Math.abs(v));
            }
            return p;
          })(),
          mic: parseFloat(document.getElementById('kMicLvl').style.width) || 0,
          prog: parseFloat(document.getElementById('kProgFill').style.width) || 0,
          t: KAR.el ? KAR.el.currentTime : -1,
          ctxState: KAR.ctx ? KAR.ctx.state : '',
          singing: document.getElementById('singLayer').classList.contains('sing'),
          finish: !document.getElementById('kFinish').hidden,
        });
        """)
        check("U5 混录总线在跑：chunks / 电平 / 进度 / AudioContext",
              u5["phase"] == "sing" and u5["chunks"] > 0 and u5["frames"] > 0
              and u5["busPeak"] > 0.01 and mic_max > 0 and width_max > 0
              and u5["prog"] > 0 and u5["t"] > 0 and u5["ctxState"] == "running"
              and u5["singing"] and u5["finish"],
              "chunks=%d frames=%d busPeak=%.3f micMax=%.3f 电平条峰值=%.1f%% prog=%.1f%% t=%.2f ctx=%s"
              % (u5["chunks"], u5["frames"], u5["busPeak"], mic_max, width_max,
                 u5["prog"], u5["t"], u5["ctxState"]))

        # 演唱态两个回归点：① 倒数的数字必须在开唱时清掉（旧版「1」留在舞台整首歌，
        # 还占着 flex 把歌词压到底部）；② 音准轨必须几何居中（旧版左侧 88px 音名槽
        # 把整条轨顶偏 51px，手机 390 宽上肉眼可见）。
        u5b = d.execute_script(r"""
        const bar = document.querySelector('.kp-bar').getBoundingClientRect();
        return {
          stage: (document.getElementById('kStage').innerHTML || '').trim(),
          center: bar.left + bar.width / 2,
          vw: innerWidth,
        };
        """)
        check("U5b 开唱后倒数字清场、音准轨几何居中",
              u5b["stage"] == "" and abs(u5b["center"] - u5b["vw"] / 2) <= 1.5,
              "stage=%r railCenter=%.1f vw/2=%.1f"
              % (u5b["stage"][:20], u5b["center"], u5b["vw"] / 2))

        d.execute_script("karStop();")
        u6 = d.execute_script(r"""
        return ({
          phase: KAR.phase,
          size: KAR.blob ? KAR.blob.size : 0,
          type: KAR.blob ? KAR.blob.type : '',
          duration: KAR.duration,
          review: !!document.querySelector('.k-review'),
          saveDisabled: (() => {
            const b = document.querySelector('[data-act="k-save"]');
            return b ? b.disabled : true;
          })(),
        });
        """)
        check("U6 唱完了 → review 态、WAV blob 已拼好、保存按钮可用",
              u6["phase"] == "review" and u6["size"] > 44
              and u6["type"] == "audio/wav" and u6["review"]
              and not u6["saveDisabled"] and u6["duration"] >= 1,
              "size=%d type=%s duration=%ds" % (u6["size"], u6["type"], u6["duration"]))

        # ★ 关键：karSave 必须发 raw octet-stream，走 post() 会只发 2 字节
        d.execute_script("document.querySelector('[data-act=\"k-save\"]').click();")
        saved = wait_js(d, "(!KAR.open && state.worksLoaded && state.works.length > 0)"
                        " ? state.works.length : null", timeout=25)
        all_rows = req(base, "/api/works").get("data") or []
        new_rows = [w for w in all_rows if w.get("id") != wid]
        check("U7 ★ 保存作品：raw 上传成功、列表 +1、K 歌层自动关闭",
              bool(saved) and len(new_rows) == 1 and KAR_CLOSED(d),
              "saved=%s 现有=%d 新增=%d" % (saved, len(all_rows), len(new_rows)))

        if new_rows:
            w = new_rows[0]
            code, hdr, body = req(base, "/api/works/file?id=" + w["id"], raw=True)
            peak = wav_peak(body)
            check("U7b 落盘录音不是静音文件且为 RIFF",
                  code == 200 and body[:4] == b"RIFF" and peak > 0.01,
                  "peak=%.4f bytes=%d" % (peak, len(body)))
            if len(body) >= 44:
                ch = struct.unpack_from("<H", body, 22)[0]
                rate = struct.unpack_from("<I", body, 24)[0]
                bits = struct.unpack_from("<H", body, 34)[0]
                check("U7c WAV 头：双声道 / 16bit / 采样率有效",
                      ch == 2 and bits == 16 and rate >= 8000,
                      "ch=%d bits=%d rate=%d" % (ch, bits, rate))
            check("U7d 作品时长取录制时长（不含回顾停留）",
                  w.get("duration", 0) >= 1, "duration=%s" % w.get("duration"))

        u8 = d.execute_async_script(r"""
        const done = arguments[arguments.length - 1];
        (async () => {
          await loadWorks(true);   // 真实路径：进作品页从服务端拉全量，而非只看内存里新增的一条
          go({ name: 'works' }); render();
          const cards = document.querySelectorAll('.wcard');
          const foot = document.querySelector('.wcard-foot');
          const a = document.querySelector('.wcard-acts a[download]');
          done({
            cards: cards.length,
            footDisplay: foot ? getComputedStyle(foot).display : '',
            footH: foot ? foot.getBoundingClientRect().height : 0,
            href: a ? a.getAttribute('href') : '',
            heading: document.querySelector('.vhead h1')
              ? document.querySelector('.vhead h1').textContent : '',
          });
        })();
        """)
        check("U8 我的作品页渲染：卡片 / 脚注 / 导出链接",
              u8["cards"] >= 2 and u8["footDisplay"] == "flex"
              and u8["footH"] > 0 and "/api/works/file" in u8["href"],
              "cards=%d foot=%s/%d heading=%s" % (u8["cards"], u8["footDisplay"],
                                                  u8["footH"], u8["heading"]))

        u9 = d.execute_async_script(r"""
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            await playAt(state.works.map(workAsTrack), 0, { source: '我的作品' });
            await new Promise(r => setTimeout(r, 800));
            const a = audio();
            done({ src: a.src, t: a.currentTime, paused: a.paused });
            try { a.pause(); } catch (e) {}
          } catch (e) { done({ error: String(e) }); }
        })();
        """)
        check("U9 作品可回放：走 /api/works/file 且进度在走",
              not u9.get("error") and "/api/works/file" in u9.get("src", "")
              and u9.get("t", 0) > 0, str(u9)[:180])

        u10 = KAR_CLOSED(d)
        check("U10 关闭后 AudioContext / <audio> / 麦克风全释放",
              u10 and d.execute_script(
                  "return !document.getElementById('kAudio')"
                  " && document.getElementById('singLayer').hidden"
                  " && KAR.ctx === null && KAR.resume === null;"),
              "层已收起且无孤儿节点")

        # U12 耳返延迟自检面板：假麦下只需证明链路能出结果（结果行或明确失败原因），
        # 数字本身在桌面假麦上无意义（假麦 beep 与输出脉冲互不相干）——真机数字靠 diag 面板实测。
        # U9 回放的作品还在主 <audio> 里响；自检入口会正确拒绝「边放边测」，
        # 用例先暂停，测的是自检链路本身。
        d.execute_script("const a = audio && audio(); if (a) a.pause();")
        d.execute_script("document.getElementById('settingsBtn').click();")
        has_btn = wait_js(d, "!!document.getElementById('diagMicBtn')", timeout=10)
        check("U12a 耳返延迟自检入口在 diag 面板", bool(has_btn), "diagMicBtn=%s" % has_btn)
        d.execute_script("document.getElementById('diagMicBtn').click();")
        # 注意：wait_js 拼 "return " + expr，表达式首行必须与 return 同行
        # （换行触发 JS 的 ASI，变成空 return，永远拿到 null —— U12b 第一次挂就是它）
        res = wait_js(d, r"""(function () {
  const t = document.getElementById('diagOut').textContent;
  return (t.includes('往返延迟') || t.includes('没测成')) ? t : null;
})()""", timeout=35)
        check("U12b 延迟自检出结果或给出明确失败原因",
              bool(res) and ("往返延迟" in res or "没测成" in res), str(res)[:160])

        noise = ("favicon", "/api/local/cover", "/api/probe")
        errs = [e for e in d.get_log("browser")
                if e.get("level") == "SEVERE"
                and not any(n in e.get("message", "") for n in noise)]
        check("U11 无未捕获的严重异常", not errs,
              "; ".join(e["message"][:120] for e in errs[:3]))

        # ---------------------------------------------------------- 收尾清理
        for w in [x["id"] for x in new_rows] + [wid]:
            jpost(base, "/api/works/delete", {"id": w})
        left = req(base, "/api/works").get("data") or []
        files = (glob.glob(os.path.join(home, "**", "*.wav"), recursive=True)
                 + glob.glob(os.path.join(home, "**", "*.part"), recursive=True))
        check("B11 删除后列表空且磁盘无残留", not left and not files,
              "列表=%d 文件=%s" % (len(left), files))

        st = jpost(base, "/api/works/delete", {"id": wid})
        check("B12 重复删除幂等（文件已不在也算成功）", st.get("ok") is True, str(st)[:80])

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


def KAR_CLOSED(d):
    """K 歌层已收起且 KAR 状态归零（保存成功后 karClose 应已跑过）。"""
    return bool(d.execute_script(
        "return typeof KAR !== 'undefined' && KAR.open === false"
        " && KAR.phase === '' && KAR.micStream === null;"))


if __name__ == "__main__":
    sys.exit(main())
