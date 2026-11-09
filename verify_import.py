# -*- coding: utf-8 -*-
"""Elysium 歌单导入验收（Stage Import）。

覆盖三层，全部在隔离实例里跑，不碰真实用户数据：
  U  单元层：规则加载、链接/文本识别、KGL/CSV/m3u8 解析、三桶判定
  A  接口层：preview 为 dry-run、三桶守恒、commit 落盘、收藏独立、重启保留
  I  界面层：打开导入抽屉 → 解析 → 三桶分区 → 勾选 → 提交 → 侧栏出现歌单

用法：
    python verify_import.py
    python verify_import.py --no-ui      # 环境没有 Chrome 时只跑单元 + 接口
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

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import importer  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)[:260]) if detail else ""))
    return bool(ok)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(base, path, body=None, timeout=90):
    if body is None:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    data = json.dumps(body).encode("utf-8")
    rq = urllib.request.Request(base + path, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def start_instance(home, port=None):
    port = port or free_port()
    env = dict(os.environ)
    env["ELYSIUM_HOME"] = home
    logf = open(os.path.join(home, "_server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "main.py"), "--serve", "--port=%d" % port],
        cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return base, proc, logf
        except OSError:
            time.sleep(0.25)
    proc.terminate()
    logf.close()
    return None, proc, logf


def stop(proc, logf):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                        # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                    # noqa: BLE001
            pass
    try:
        logf.close()
    except Exception:                                        # noqa: BLE001
        pass


def wait_task(base, tid, limit=180):
    for _ in range(limit):
        s = req(base, "/api/import/status?id=" + tid)["data"]
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.5)
    return {"state": "timeout"}


# --------------------------------------------------------------------------
def unit_tests():
    r = importer.load_rules(force=True)
    check("U1 规则加载无降级", not r["degraded"], r["degraded"])
    check("U2 三家规则齐全", all(p in r["rules"] for p in
                            ("netease", "qqmusic", "kugou")), list(r["rules"]))

    cases = [
        ("https://music.163.com/playlist?id=3778678", "link", "netease", "3778678"),
        ("https://music.163.com/#/playlist?id=2756674066", "link", "netease", "2756674066"),
        ("https://y.qq.com/n/ryqq/playlist/9044196528", "link", "qqmusic", "9044196528"),
        ("https://www.kugou.com/yy/special/single/546903.html", "link", "kugou", "546903"),
        ("https://www.kugou.com/songlist/gcid_3zr52qfrzaz06a/", "link", "kugou", "gcid_3zr52qfrzaz06a"),
        ("分享歌单 https://music.163.com/playlist?id=3778678 一起听", "link", "netease", "3778678"),
        ("邓紫棋 - 泡沫\n筷子兄弟 - 小苹果", "text", "", ""),
    ]
    ok = True
    for text, mode, plat, pid in cases:
        g = importer.parse_input(text)
        ok &= (g["mode"], g["platform"], g["id"]) == (mode, plat, pid)
    check("U3 链接 / 分享文案 / 文本识别", ok)

    full = ("0 邓紫棋 - 泡沫.mp3 . 4186975 258000 36542b F:\\KuGou\\Lyric\\x.krc 128000 0\n"
            "0 筷子兄弟 - 小苹果.mp3 . 3382542 211000 fcd49 F:\\KuGou\\Lyric\\y.krc 128000 0")
    t = importer.parse_text(full, "list.kgl")
    check("U4 完整 KGL 行首序号不污染歌手",
          [(x["name"], x["artist"]) for x in t] == [("泡沫", "邓紫棋"), ("小苹果", "筷子兄弟")],
          [(x["name"], x["artist"]) for x in t])
    check("U5 KGL 保留 alt 解释", all(x.get("alt") for x in t))
    check("U6 简化 KGL",
          [(x["name"], x["artist"]) for x in importer.parse_text(
              "邓紫棋 - 泡沫.mp3\n筷子兄弟 - 小苹果.mp3", "a.kgl")]
          == [("泡沫", "邓紫棋"), ("小苹果", "筷子兄弟")])
    check("U7 CSV 列映射",
          [(x["name"], x["artist"]) for x in importer.parse_text(
              "title,artist,album\nHong Kong,The Weeknd,After Hours", "a.csv")]
          == [("Hong Kong", "The Weeknd")])
    check("U8 m3u8 去重",
          len(importer.parse_text("#EXTM3U\n#EXTINF:258,邓紫棋 - 泡沫\n邓紫棋 - 泡沫.mp3", "a.m3u8")) == 1)
    check("U9 XML 形态 KGL",
          len(importer.parse_text('<List><FileName>邓紫棋 - 泡沫.mp3</FileName></List>', "a.kgl")) == 1)

    # 方向投票：整批定一次方向，不许逐条猜（曾把「某人 - 不存在的歌」猜成歌名『某人』）
    importer.bind(
        fold_text=lambda s: str(s or "").strip().lower(),
        billed_as=lambda o, n: str(n or "").strip().lower()
        == str((o or {}).get("artist") or "").strip().lower(),
        search_all=lambda kw, p=1, s=20: {"list": [
            {"platform": "kuwo", "id": "1", "name": "泡沫", "artist": "邓紫棋"}]}
        if "泡沫" in kw else {"list": []})
    kgl = [{"name": "泡沫", "artist": "邓紫棋", "alt": {"name": "邓紫棋", "artist": "泡沫"}}]
    text = [{"name": "邓紫棋", "artist": "泡沫", "alt": {"name": "泡沫", "artist": "邓紫棋"}}]
    check("U10 方向投票 KGL=normal / 文本=swap",
          importer._choose_order(kgl) == "normal" and importer._choose_order(text) == "swap")
    a = importer.classify_one(0, {"name": "泡沫", "artist": "邓紫棋", "raw": "邓紫棋 - 泡沫"})
    b = importer.classify_one(1, {"name": "泡沫", "artist": "孙燕姿", "raw": "孙燕姿 - 泡沫"})
    c = importer.classify_one(2, {"name": "不存在的歌", "artist": "某人", "raw": "某人 - 不存在的歌"})
    check("U11 L1 严格命中自动纳入", a["tier"] == "l1" and a["chosen"] is True)
    check("U12 L2 作者对不上默认不选", b["tier"] == "l2" and b["chosen"] is False)
    check("U13 L3 未找到不丢弃", c["tier"] == "l3" and c["chosen"] is False)


def api_tests(base, home):
    check("A1 /api/store 可用", "playlists" in req(base, "/api/store"))
    u = req(base, "/api/update/check")["data"]
    check("A2 版本检查只报版本不下载",
          u.get("rules_version") == "2026-11-09.1" and u.get("remote") == "not_configured", u)

    fav_before = len(req(base, "/api/store")["favorites"])
    text = "邓紫棋 - 泡沫.mp3\n孙燕姿 - 我不难过.mp3\n某人 - 这首歌根本不存在.mp3"
    st = req(base, "/api/import/preview", {"text": text, "filename": "demo.kgl"})["data"]
    tid = st["task_id"]
    check("A3 预览返回 task_id", bool(tid))
    check("A4 预览阶段不写任何歌单", len(req(base, "/api/store")["playlists"]) == 0)
    res = wait_task(base, tid)
    check("A5 预览任务完成", res["state"] == "done", (res["state"], res.get("error")))
    if res["state"] != "done":
        return None
    b = res["result"]["buckets"]
    total = sum(len(v) for v in b.values())
    check("A6 三桶总数守恒（不静默丢弃）", total == 3,
          {k: len(v) for k, v in b.items()})
    check("A7 L1 默认纳入 / L2 L3 默认不选",
          all(x["chosen"] for x in b["l1"])
          and all(not x["chosen"] for x in b["l2"] + b["l3"]))
    check("A8 未匹配曲目进 L3 而非被误判",
          any("这首歌根本不存在" in x["name"] for x in b["l3"]),
          {k: [x["name"] for x in v] for k, v in b.items()})
    check("A9 每条都有终态字段",
          all("tier" in x and "item" in x and "raw" in x
              for v in b.values() for x in v))
    src = res["result"]["source"]
    check("A10 来源元数据不含本机绝对路径",
          home.replace("\\", "/") not in json.dumps(src, ensure_ascii=False), src)
    l1_items = [x["item"] for x in b["l1"] if x.get("item")]
    check("A11 L1 只产出可播放音源",
          all(x.get("platform") in ("kuwo", "wyy") for x in l1_items),
          [x.get("platform") for x in l1_items])

    c = req(base, "/api/import/commit", {"name": "空", "items": []})
    check("A12 空勾选被拒且不建空歌单",
          c.get("ok") is False and len(req(base, "/api/store")["playlists"]) == 0, c)

    # 伪造一个 QQ 音乐原生 id：必须被服务端挡掉，不能变成僵尸曲目
    bad = req(base, "/api/import/commit", {
        "name": "伪造音源", "items": [{"platform": "qqmusic", "id": "abc", "name": "x"}]})
    check("A13 非本地音源条目被挡",
          bad.get("ok") is False and len(req(base, "/api/store")["playlists"]) == 0, bad)

    if l1_items:
        c2 = req(base, "/api/import/commit", {
            "name": "验收导入", "items": l1_items[:1],
            "source": {"mode": "file", "label": "本地文件", "filename": "demo.kgl"}})
        check("A14 commit 落盘", c2.get("ok") and c2["data"]["added"] == 1, c2)
        store = req(base, "/api/store")
        pl = store["playlists"][0]
        check("A15 歌单带 source 元数据", bool(pl.get("source")), pl.get("source"))
        check("A16 收藏完全独立", len(store["favorites"]) == fav_before)
        check("A17 歌单条目只含可播放音源",
              all(i["platform"] in ("kuwo", "wyy") for i in pl["items"]))
        return pl["id"]
    return None


def ui_tests(base):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1440,900")
    o.add_argument("--lang=zh-CN")
    d = webdriver.Chrome(options=o)
    d.set_page_load_timeout(45)
    try:
        d.get(base + "/")
        time.sleep(2.5)
        d.find_element(By.ID, "plImport").click()
        time.sleep(0.6)
        check("I1 导入抽屉打开",
              d.find_element(By.ID, "importDrawer").get_attribute("hidden") is None)
        ta = d.find_element(By.ID, "impText")
        ta.clear()
        ta.send_keys("邓紫棋 - 泡沫.mp3\n孙燕姿 - 我不难过.mp3\n某人 - 这首歌根本不存在.mp3")
        d.find_element(By.CSS_SELECTOR, '[data-imp="start"]').click()

        for _ in range(120):
            tiers = d.find_elements(By.CSS_SELECTOR, ".imp-tier")
            if tiers:
                break
            time.sleep(0.5)
        texts = [t.text for t in d.find_elements(By.CSS_SELECTOR, ".imp-tier")]
        check("I2 三桶分区渲染", len(texts) >= 2, texts)
        picks = d.find_elements(By.CSS_SELECTOR, '[data-imp-field="pick"]')
        check("I3 每条都有勾选框", len(picks) == 3, len(picks))
        check("I4 L2/L3 默认未勾选",
              all(not p.is_selected() for p in picks
                  if p.get_attribute("data-tier") in ("l2", "l3")),
              [(p.get_attribute("data-tier"), p.is_selected()) for p in picks])

        # 改歌单名 → 点一个候选 → 名字必须还在（重绘回收字段）
        name_box = d.find_element(By.ID, "impName")
        name_box.clear()
        name_box.send_keys("云端通勤")
        cands = d.find_elements(By.CSS_SELECTOR, '[data-imp="cand"]')
        if cands:
            cands[0].click()
            time.sleep(0.4)
        check("I5 重绘后歌单名不丢",
              d.find_element(By.ID, "impName").get_attribute("value") == "云端通勤",
              d.find_element(By.ID, "impName").get_attribute("value"))

        # 勾一个 L2，确认「用户明确选择」才加入
        l2 = d.find_elements(By.CSS_SELECTOR, '[data-imp-field="pick"][data-tier="l2"]')
        chosen_l2 = 0
        if l2:
            l2[0].click()
            time.sleep(0.4)
            chosen_l2 = 1
        # 接口层已经建过一个歌单，这里用「新增差集」判断，不能假设总数恒为 1
        before_ids = {p["id"] for p in req(base, "/api/store")["playlists"]}
        d.find_element(By.CSS_SELECTOR, '[data-imp="commit"]').click()
        time.sleep(2.0)
        store = req(base, "/api/store")
        fresh = [p for p in store["playlists"] if p["id"] not in before_ids]
        check("I6 提交后新增 1 个歌单", len(fresh) == 1,
              [(p["name"], len(p["items"])) for p in store["playlists"]])
        if fresh:
            pl = fresh[0]
            check("I7 歌单名来自界面", pl["name"] == "云端通勤", pl["name"])
            check("I8 L1 自动纳入 + 用户勾选的 L2 一并进入",
                  len(pl["items"]) >= 1 + chosen_l2, len(pl["items"]))
            check("I9 未找到 / 未勾选条目未被擅自合并",
                  not any("这首歌根本不存在" in (i.get("name") or "")
                          for i in pl["items"]),
                  [i.get("name") for i in pl["items"]])
        side = d.execute_script(
            "return document.querySelectorAll('#sidePlaylists .side-item').length")
        check("I10 侧栏出现新歌单", side >= 1, side)
    finally:
        try:
            d.quit()
        except Exception:                                    # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true", help="跳过 selenium 界面验收")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()

    unit_tests()

    home = tempfile.mkdtemp(prefix="elysium_vimp_")
    shutil.copytree(os.path.join(REPO, "web"), os.path.join(home, "web"))
    shutil.copytree(os.path.join(REPO, "providers"), os.path.join(home, "providers"))
    base, proc, logf = start_instance(home, a.port or None)
    if not base:
        check("实例启动", False, "服务起不来")
        shutil.rmtree(home, ignore_errors=True)
        return 1
    try:
        api_tests(base, home)
        if not a.no_ui:
            try:
                ui_tests(base)
            except Exception as e:                           # noqa: BLE001
                check("界面验收环境", False, "%s: %s" % (type(e).__name__, str(e)[:200]))

        # 重启持久化
        stop(proc, logf)
        base2, proc2, logf2 = start_instance(home)
        try:
            if base2:
                pls = req(base2, "/api/store")["playlists"]
                check("A18 重启后歌单保留",
                      len(pls) >= 1 and all("source" in p for p in pls),
                      [(p.get("name"), len(p.get("items") or [])) for p in pls])
        finally:
            stop(proc2, logf2)
    finally:
        stop(proc, logf)
        shutil.rmtree(home, ignore_errors=True)

    print("\n---- 汇总 ----")
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAIL  %s  %s" % (name, detail))
    print("  %d/%d 通过" % (ok_n, len(RESULTS)))
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
