# -*- coding: utf-8 -*-
"""Elysium 本地曲库 / 系统媒体集成 / 睡眠定时 UI 回归自检。

覆盖验收项：
  T1 侧栏「本地音乐」入口 —— 点进去能扫出下载目录里的文件
  T2 本地行形态 —— 规格 chip 直接显示真实格式（无「检测中」）、来源徽章「本地」、无下载按钮
  T3 本地播放 —— <audio> 直连 /api/local/file，封面走 /api/local/cover
  T4 系统媒体集成 —— navigator.mediaSession 元数据与 playbackState 就位
  T5 桌面歌词中继 —— 播放时 /api/lyric/now 收到当前曲目
  T6 睡眠定时 —— 设置项存在且可切换（本会话级）
  T7 桌面歌词开关 —— 无宿主环境优雅降级（toast + 不勾选）
  T8 页面无未捕获异常

用法：
    python verify_local.py
依赖：selenium + 本机 Chrome（独立实例：ELYSIUM_HOME 指向临时目录，不碰真实数据）
"""
import argparse
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _jpg_bytes():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def make_music(dir_):
    """三首带标签的测试音频（能被 mutagen 读出）+ 一个非音频干扰项。

    mp3 写 ~1500 帧（约 40s）：太短会在断言间隙播完，playing 落回 False。
    """
    os.makedirs(dir_, exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413            # 128k/44.1k MPEG-1 L3 静音帧
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC

    def mp3(name, title, artist, album, cover=False):
        p = os.path.join(dir_, name)
        with open(p, "wb") as f:
            f.write(frame * 1500)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TALB(encoding=3, text=album))
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="",
                          data=_jpg_bytes()))
        tags.save(p)

    mp3("晴天.mp3", "晴天", "周杰伦", "叶惠美", cover=True)
    mp3("七里香.mp3", "七里香", "周杰伦", "七里香")

    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6
    streaminfo += struct.pack(">Q", (44100 << 44) | (1 << 41) | (15 << 36)) + b"\x00" * 16
    p = os.path.join(dir_, "泡沫.flac")
    with open(p, "wb") as f:
        f.write(b"fLaC" + b"\x80" + len(streaminfo).to_bytes(3, "big") + streaminfo)
    from mutagen.flac import FLAC
    m = FLAC(p)
    m["title"] = "泡沫"; m["artist"] = "邓紫棋"; m["album"] = "Xposed"
    m.save()
    return dir_


def spawn(base_dir, music):
    home = os.path.join(base_dir, "home")
    os.makedirs(home, exist_ok=True)
    for item in ("web", "providers"):                     # ELYSIUM_HOME 兼作资源根（打包版约定）
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args()

    base_dir = tempfile.mkdtemp(prefix="elysium_verify_local_")
    music = make_music(os.path.join(base_dir, "music"))
    proc, base, home = None, None, None
    d = None
    try:
        proc, base, home = spawn(base_dir, music)
        d = make_driver()
        d.get(base + "/")
        time.sleep(2.5)

        # 需要把下载目录指到测试音乐目录（独立实例的默认目录是临时 HOME 下）
        d.execute_script(
            "return fetch('/api/store',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({op:'settings',payload:{download_dir:%s}})}).then(r=>r.json())"
            % (repr(music).replace("'", '"'),))
        time.sleep(0.5)

        # T1 本地入口
        nav = d.find_elements(By.CSS_SELECTOR, '#snav .snav-i[data-nav="local"]')
        if not check("T1a 侧栏「本地音乐」入口存在", len(nav) == 1):
            raise SystemExit(1)
        nav[0].click()
        time.sleep(1.2)
        rows = d.find_elements(By.CSS_SELECTOR, "#view .tr")
        check("T1b 扫出 3 个本地文件", len(rows) == 3, len(rows))

        # T2 本地行形态
        chips = [e.text for e in d.find_elements(By.CSS_SELECTOR, "#view .tr .chip")]
        check("T2a 规格 chip 无「检测中」", all("检测中" not in c for c in chips), chips)
        check("T2b FLAC/MP3 真实格式直出", any("FLAC" in c for c in chips) and any("MP3" in c for c in chips), chips)
        badges = [e.text for e in d.find_elements(By.CSS_SELECTOR, "#view .tr .src")]
        check("T2c 来源徽章为「本地」", badges and all(b == "本地" for b in badges), badges)
        check("T2d 本地行没有下载按钮",
              not d.find_elements(By.CSS_SELECTOR, '#view .tr [data-act="dl"]'))

        # T3 本地播放（点行 = 用户手势）：点名点带内嵌封面的《晴天》
        target = None
        for r in rows:
            if "晴天" in r.text:
                target = r
                break
        if not check("T3-0 找到《晴天》行", target is not None):
            raise SystemExit(1)
        target.click()
        time.sleep(1.5)
        src = d.execute_script("const a=document.getElementById('audio');return a.src||a.currentSrc;")
        check("T3a audio 直连 /api/local/file", "/api/local/file" in (src or ""), src)
        art = d.execute_script(
            "const i=document.querySelector('#plArt img');return i?i.getAttribute('src'):'';")
        check("T3b 封面走 /api/local/cover", "/api/local/cover" in (art or ""), art)

        # T4 系统媒体集成（桌面分支：mediaSession）
        ms = d.execute_script(
            "const m=navigator.mediaSession;return {t:m.metadata?m.metadata.title:'',s:m.playbackState,"
            "artists:m.metadata?m.metadata.artist:''};")
        check("T4a SMTC 元数据标题", ms.get("t") == "晴天", ms)
        check("T4b playbackState=playing", ms.get("s") == "playing", ms)

        # T5 桌面歌词中继收到当前曲目
        time.sleep(0.8)
        ln = d.execute_script(
            "return fetch('/api/lyric/now').then(r=>r.json()).then(j=>j.data);")
        check("T5 歌词中继收到曲目", (ln or {}).get("title") == "晴天"
              and (ln or {}).get("playing") is True, ln)

        # T6 睡眠定时
        d.execute_script(
            "document.querySelector('#settingsBtn').click();")
        time.sleep(0.8)
        sel = d.find_elements(By.ID, "setSleep")
        if check("T6a 睡眠定时设置项存在", len(sel) == 1):
            d.execute_script(
                "document.getElementById('setSleep').value='15';"
                "document.getElementById('setSleep').dispatchEvent(new Event('change'));")
            time.sleep(0.3)
            toasts = " ".join(e.text for e in d.find_elements(By.CSS_SELECTOR, ".toasts .toast"))
            check("T6b 定时生效提示", "15 分钟后暂停" in toasts, toasts)
            d.execute_script(
                "document.getElementById('setSleep').value='off';"
                "document.getElementById('setSleep').dispatchEvent(new Event('change'));")

        # T7 桌面歌词开关（浏览器无 pywebview 宿主 → 优雅降级）
        lw = d.find_elements(By.ID, "setLyricWin")
        if check("T7a 桌面歌词开关存在（桌面端）", len(lw) == 1):
            lw[0].click()
            time.sleep(0.5)
            checked = d.execute_script("return document.getElementById('setLyricWin').checked;")
            check("T7b 无宿主时不勾选（静默降级）", checked is False, checked)

        # T8 未捕获异常
        time.sleep(1.0)
        bad = []
        for e in d.get_log("browser"):
            msg = e.get("message", "")
            if e.get("level") == "SEVERE" and "Uncaught" in msg:
                bad.append(msg[:200])
        check("T8 无未捕获异常", not bad, bad)

        d.save_screenshot(os.path.join(base_dir, "local_view.png"))
    finally:
        if d:
            d.quit()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    n_ok = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n== 结果：%d pass / %d fail ==  (产物在 %s)" % (
        n_ok, len(RESULTS) - n_ok, base_dir if args.keep else "(临时目录，退出后清理)"))
    if not args.keep:
        shutil.rmtree(base_dir, ignore_errors=True)
    sys.exit(0 if n_ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
