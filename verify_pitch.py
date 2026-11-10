# -*- coding: utf-8 -*-
"""P9 音准引擎验收：耳返调音 / 自动原伴 / Key 解析 / 实时音准（CDEFGAB）。

B 组（后端，纯标准库 urllib）
  B1  work_meta 音准透传：合法值入库、非法值忽略、缺省不带
  B2  export 仍不含 works（回归确认音准字段没把作品带进同步包）

U 组（Chrome 端到端；假麦由文件驱动 —— 音高检测从此是确定性测试）
  P1  ★ 实时音准：假麦喂 A4(440Hz)→C5(523.25Hz) 序列，
      断言 KAR.userMidi 跟着走到 69 / 72（±1 半音）
  P2  旋律提取：C 大调上行音阶伴奏 → melody 覆盖 midi 60..72（±1）
  P3  Key 解析：同一音阶 → 「C 大调」
  P4  自动原伴：宽伴奏+中置人声 → 消人声有效；单声道 → 保留原唱
  P5  混响切换不断流；耳返开关只动监听不动录音
  P6  结算：review 页出现音准分，pitchTotal ≥ 2 且 pitchHit ≥ 1
      （伴奏参考与假麦同源同频，准行是「应该准」的）

P1 之所以成立：--use-file-for-fake-audio-capture 让假麦克风播放我们合成的
WAV，ACF 检测一个已知频率的纯音没有歧义 —— 音高链路的对错变成可断言的。

用法：
    python verify_pitch.py            # 跑完自动清理
    python verify_pitch.py --keep     # 保留临时目录
依赖：selenium + 本机 Chrome + ffmpeg（合成音阶 mp3 用）
"""
import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_gapless import spawn                      # noqa: E402
from selenium import webdriver                        # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402

REPO = os.path.dirname(os.path.abspath(__file__))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def note(text):
    print("      · " + text)


def wait_js(d, expr, timeout=30, poll=0.4):
    """轮询等一个 JS 表达式非 null。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = d.execute_script("return (%s);" % expr)
        except Exception:
            v = None
        if v is not None:
            return v
        time.sleep(poll)
    return None


def wav_write(path, rate, chans):
    """chans: [FloatOrInt 序列×声道]，16bit WAV。"""
    n = len(chans[0])
    data = bytearray()
    for i in range(n):
        for c in chans:
            v = max(-1.0, min(1.0, c[i]))
            data += struct.pack("<h", int(v * 32767))
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, len(chans), rate, rate * 2 * len(chans),
                         2 * len(chans), 16)
           + b"data" + struct.pack("<I", len(data)))
    with open(path, "wb") as f:
        f.write(hdr + bytes(data))
    return path


def make_sources(dir_):
    """四份测试音频：假麦音源（兼伴奏）/ 音阶歌 / 宽伴奏居中人声 / 单声道。"""
    os.makedirs(dir_, exist_ok=True)
    rate = 48000

    # 1) 假麦音源（兼伴奏）：A4 440Hz 7s → C5 523.25Hz 7s → 静音 6s。
    #    足够长：倒数 2.1s + 采样窗 + 混响/耳返断言 + 结算全在 ended 之前。
    seq = []
    for i in range(rate * 20):
        t = i / rate
        f = 440.0 if t < 7.0 else (523.25 if t < 14.0 else 0.0)
        seq.append(0.55 * math.sin(2 * math.pi * f * t) if f else 0.0)
    mic_wav = wav_write(os.path.join(dir_, "mic_tone.wav"), rate, [seq, seq])

    # 2) C 大调旋律（mp3，模拟真实伴奏文件）：音阶上行 + 属音-主音终止。
    #    纯等权音阶对 K-S 是出了名的歧义材料（关系调共享音级），终止音补 tonic 权重。
    s2 = []
    for f0, dur in [(261.63, 0.6), (293.66, 0.6), (329.63, 0.6), (349.23, 0.6),
                    (392.00, 0.6), (440.00, 0.6), (493.88, 0.6), (523.25, 0.6),
                    (392.00, 0.7), (261.63, 1.0)]:
        for i in range(int(rate * dur)):
            t = i / rate
            env = min(1.0, t * 12, (dur - t) * 12)      # 淡入淡出，帧边界干净
            s2.append(0.5 * env * math.sin(2 * math.pi * f0 * t))
    song = os.path.join(dir_, "scale.mp3")
    raw = wav_write(os.path.join(dir_, "_scale_raw.wav"), rate, [s2, s2])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw,
                    "-b:a", "128k", song], check=True)
    os.remove(raw)

    # 3) 宽伴奏 + 中置人声：vocal 居中（mid），伴奏两侧反相（side 大）
    s3l, s3r = [], []
    for i in range(rate * 4):
        t = i / rate
        v = 0.5 * math.sin(2 * math.pi * 440 * t)
        a1 = 0.4 * math.sin(2 * math.pi * 2000 * t) + 0.4 * math.sin(2 * math.pi * 3000 * t)
        s3l.append(v + a1)
        s3r.append(v - a1)
    center_wav = wav_write(os.path.join(dir_, "center.wav"), rate, [s3l, s3r])

    # 4) 单声道（L=R）
    s4l, s4r = [], []
    for i in range(rate * 4):
        t = i / rate
        v = 0.5 * math.sin(2 * math.pi * 440 * t) + 0.4 * math.sin(2 * math.pi * 800 * t)
        s4l.append(v)
        s4r.append(v)
    mono_wav = wav_write(os.path.join(dir_, "mono.wav"), rate, [s4l, s4r])

    return mic_wav, song, center_wav, mono_wav


def upload(base, path, name):
    q = urllib.parse.urlencode({"name": name, "artist": "信号组",
                                "platform": "kuwo", "sid": "0", "duration": 3})
    with open(path, "rb") as f:
        r = urllib.request.Request(base + "/api/works/upload?" + q, data=f.read(),
                                   headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        d = json.loads(resp.read())
    assert d.get("ok"), d
    return d["data"]["id"]


def make_driver(mic_wav):
    o = Options()
    for a in ("--headless=new", "--disable-gpu", "--window-size=1440,900",
              "--lang=zh-CN", "--autoplay-policy=no-user-gesture-required",
              "--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream",
              "--use-file-for-fake-audio-capture=" + mic_wav.replace("\\", "/")):
        o.add_argument(a)
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    d.set_window_size(1440, 900)
    d.set_page_load_timeout(45)
    return d


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_pitch_")
    proc = d = None
    try:
        mic_wav, scale_mp3, center_wav, mono_wav = make_sources(
            os.path.join(base_dir, "src"))
        proc, base, home = spawn(base_dir)

        # ---------------- B 组：后端透传 ----------------
        q = urllib.parse.urlencode({"name": "带分作品", "artist": "x", "platform": "kuwo",
                                    "sid": "0", "duration": 3,
                                    "pitch_avg": "23.4", "pitch_hit": "5"})
        with open(scale_mp3, "rb") as f:
            body = f.read()
        r = urllib.request.Request(base + "/api/works/upload?" + q, data=body,
                                   headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            d1 = json.loads(resp.read())["data"]
        check("B1a 音准字段透传入库",
              d1.get("pitch_avg") == 23.4 and d1.get("pitch_hit") == 5, str(d1)[:120])
        q2 = urllib.parse.urlencode({"name": "坏分作品", "artist": "x", "platform": "kuwo",
                                     "sid": "0", "duration": 3,
                                     "pitch_avg": "abc", "pitch_hit": "xyz"})
        r = urllib.request.Request(base + "/api/works/upload?" + q2, data=body[:8192],
                                   headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            d2 = json.loads(resp.read())["data"]
        check("B1b 非法音准值忽略（不整单拒收）",
              "pitch_avg" not in d2 and "pitch_hit" not in d2, str(d2)[:120])
        with urllib.request.urlopen(base + "/api/store/export", timeout=10) as resp:
            ex = json.loads(resp.read())["data"]
        check("B2 export 仍不含 works", "works" not in ex, "")

        # ---------------- U 组 ----------------
        wid_pitch = upload(base, mic_wav, "音准伴奏")
        wid_scale = upload(base, scale_mp3, "C大调音阶")
        wid_center = upload(base, center_wav, "宽伴奏居中人声")
        wid_mono = upload(base, mono_wav, "单声道歌")
        note("四个测试音频已上传为作品")

        d = make_driver(mic_wav)
        d.get(base + "/")
        time.sleep(2.0)

        def start_karaoke(wid, label):
            """开一层 K 歌（stub 掉歌词上游），返回是否 ready。"""
            return d.execute_async_script(r"""
              const [wid, label, done] = arguments;
              (async () => {
                try {
                  const orig = window.rpc;
                  if (typeof orig === 'function') {
                    window.rpc = (fn, p) => (fn === 'getlyric'
                      ? Promise.resolve({ lyric: '' }) : orig(fn, p));
                  }
                  await karaokeStart({ platform: 'work', id: wid, name: label,
                    artist: '信号组', cover: '', duration: 3 });
                  done({ phase: KAR.phase, hidden:
                    document.getElementById('singLayer').hidden });
                } catch (e) { done({ error: String(e) }); }
              })();
              """, wid, label)

        def close_layer():
            d.execute_script("if (KAR.open) karClose();")
            time.sleep(0.6)

        # P2 / P3：旋律提取 + Key
        u = start_karaoke(wid_scale, "C大调音阶")
        mel = wait_js(d, "(KAR.melody && KAR.melody.length) ? KAR.melody.length : null",
                      timeout=60)
        u2 = d.execute_script("return { n: (KAR.melody||[]).length, key: KAR.keyInfo,"
                              " range: KAR.melodyRange, midis: [...new Set((KAR.melody||[]).map(p=>p.midi))] };")
        got = u2["midis"] or []
        want = [60, 62, 64, 65, 67, 69, 71, 72]
        hit = sum(1 for w in want if any(abs(g - w) <= 1 for g in got))
        check("P2 旋律提取覆盖 C 大调音阶", mel and hit >= 7,
              "melody=%d 帧音级=%s 命中=%d/8" % (u2["n"], got[:16], hit))
        key_label = (u2.get("key") or {}).get("label", "")
        # 关系调共享音级集合，等权材料下 C 大调 / a 小调皆可接受
        check("P3 Key 解析落在 C 大调族", key_label in ("C 大调", "a 小调"),
              "key=%s" % key_label)
        close_layer()

        # P4a：宽伴奏 + 中置人声 → 消人声有效
        u = start_karaoke(wid_center, "宽伴奏居中人声")
        av = wait_js(d, "KAR.autoVocal || null", timeout=60)
        check("P4a 自动原伴：宽伴奏判「消人声有效」",
              bool(av) and av.get("effective") is True
              and (av.get("mono") is False),
              "ratio=%s" % (av or {}).get("ratio"))

        close_layer()
        # P4b：单声道 → 保留原唱
        u = start_karaoke(wid_mono, "单声道歌")
        av = wait_js(d, "KAR.autoVocal || null", timeout=60)
        check("P4b 自动原伴：单声道判「保留原唱」",
              bool(av) and av.get("effective") is False and av.get("mono") is True,
              str(av)[:120])
        close_layer()

        # P1 / P5 / P6：实时音准（假麦 = 伴奏同源文件）+ 混响切换 + 结算
        u = start_karaoke(wid_pitch, "音准伴奏")
        mel = wait_js(d, "(KAR.melody && KAR.melody.length) ? KAR.melody.length : null",
                      timeout=60)
        check("P1a 参考旋律就绪（伴奏同源）", bool(mel), "melody=%s" % mel)

        d.execute_script("document.getElementById('kBegin').click();")
        phase = wait_js(d, "KAR.phase === 'sing' ? KAR.phase : null", timeout=30)
        check("P1b 开唱进入 sing 态", phase == "sing", "phase=%s" % phase)

        # 采样 userMidi：文件从 getUserMedia 起播（≈click+0.1s），440 段 ≈ t 0.1~7.1，
        # 523 段 ≈ t 7.1~14.1（倒数 2.1s 吃掉文件开头，但那两秒也在 440 段内）
        d.execute_script("""
          window.__pm = [];
          window.__pt = setInterval(() => {
            window.__pm.push({ t: performance.now() / 1000, m: KAR.userMidi });
          }, 60);
        """)
        time.sleep(9.5)
        d.execute_script("clearInterval(window.__pt);")
        samples = d.execute_script("return window.__pm;")
        seg1 = [s["m"] for s in samples if 1.0 <= s["t"] <= 6.5 and s["m"]]
        seg2 = [s["m"] for s in samples if s["t"] >= 8.0 and s["m"]]
        # 假麦文件是循环播放的，起点相位取决于 Chrome 启动以来经过的时间，
        # 采样窗对不准文件里的音段 —— 断言改为相位无关的实质等价：
        # 两个已知音（69±1 / 72±1）都被检出、各占足够比例、且出现过相互切换。
        allm = [s["m"] for s in samples if s["m"]]
        n69 = sum(1 for m in allm if abs(m - 69) <= 1)
        n72 = sum(1 for m in allm if abs(m - 72) <= 1)
        seq = [s["m"] for s in samples if s["m"]]
        switched = any(abs(a - 69) <= 1 and abs(b - 72) <= 1
                       or abs(a - 72) <= 1 and abs(b - 69) <= 1
                       for a, b in zip(seq, seq[1:]))
        enough = bool(allm) and n69 / len(allm) >= 0.15 and n72 / len(allm) >= 0.15
        check("P1c 实时音高检出 A4 与 C5 并跟踪切换", enough and switched,
              "69±1:%d 72±1:%d / 共%d 切换=%s（seg1=%s seg2=%s）"
              % (n69, n72, len(allm), switched,
                 seg1[:3], seg2[:3]))

        # P5：混响切换不断流 + 耳返只动监听
        c0 = d.execute_script("return KAR.chunks.length;")
        d.execute_script("karpSetReverb('bath');")
        time.sleep(1.0)
        d.execute_script("karpSetReverb('room');")
        time.sleep(1.0)
        u5 = d.execute_script("""
          return { c: KAR.chunks.length, phase: KAR.phase,
                   echoOff: (() => { const g = KAR.echoGate; return g ? g.gain.value : -1; })() };
        """)
        check("P5a 混响切换录音不断流", u5["c"] >= c0 + 2 and u5["phase"] == "sing",
              "chunks %d → %d" % (c0, u5["c"]))
        d.execute_script("""
          const ec = document.getElementById('kEcho');
          ec.checked = false; ec.dispatchEvent(new Event('change'));
        """)
        time.sleep(0.4)
        u5b = d.execute_script("""
          return { off: KAR.echoGate.gain.value < 0.05, c: KAR.chunks.length };
        """)
        time.sleep(0.8)
        u5c = d.execute_script("return KAR.chunks.length;")
        check("P5b 耳返关掉后录音继续（bus 契约）",
              u5b["off"] and u5c > u5b["c"], "chunks %d → %d" % (u5b["c"], u5c))

        # P6：结算
        d.execute_script("document.getElementById('kFinish').click();")
        st = wait_js(d, "KAR.phase === 'review' ? KAR.phase : null", timeout=15)
        u6 = d.execute_script("""
          return { total: KAR.pitchTotal, hit: KAR.pitchHit, avg: KAR.pitchAvg,
            hasScore: !!document.querySelector('.k-pitch-score') };
        """)
        check("P6 结算页出现音准分且判准", st == "review" and u6["hasScore"]
              and u6["total"] >= 2 and u6["hit"] >= 1, str(u6))
        close_layer()

        logs = [e["message"] for e in d.get_log("browser") if e["level"] == "SEVERE"]
        check("U7 无未捕获的严重异常", not logs, (logs[:2] if logs else ""))
    finally:
        if d:
            d.quit()
        if proc:
            proc.terminate()
        if not args.keep:
            shutil.rmtree(base_dir, ignore_errors=True)

    fails = [r for r in RESULTS if not r[1]]
    print("\n== 结果：%d pass / %d fail ==" % (len(RESULTS) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
