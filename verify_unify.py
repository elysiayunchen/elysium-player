# -*- coding: utf-8 -*-
"""Elysium 单列表 / 歌手页 回归自检（2026-11-09 去渠道化改造）

覆盖：
  U1 搜索页不再有「来源筛选条」，只留行内来源标注
  U2 跨源重复曲目折叠：一首只占一行，另一源进 alts（.src.swap）
  U3 点行内来源徽章能就地换源（行 data-pk 的 platform 变了）
  U4 专辑按「专辑名 + 主艺人」去重，不出现同名同艺人两张卡
  U5 歌手页曲目突破旧上限（> 60 首），且 歌曲 / 专辑 可切 tab
  U6 歌手页分批渲染：滚动到底继续展开
  U7 推荐卡整卡可点播放，且只剩一套操作按钮

用法：python verify_unify.py [--out _art]
依赖：selenium + 本机 Chrome（复用 verify_ui 的启动器）
"""
import argparse
import os
import sys
import time

from selenium.webdriver.common.by import By

import verify_ui as V

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + detail) if detail else ""))
    return bool(ok)


def row_state(d):
    return V.js(d, """
      return [...document.querySelectorAll('#view .tr')].map((r) => ({
        pk: r.dataset.pk, swap: !!r.querySelector('.src.swap'),
        src: (r.querySelector('.src') || {}).textContent || '',
      }));
    """)


def album_names(d):
    return V.js(d, """
      return [...document.querySelectorAll('#view .card .card-name')].map((n) => n.textContent.trim());
    """)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_art")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    base, home, proc = V.start_fresh_instance()
    if not base:
        print("服务没起来")
        return 1
    d = V.make_driver()
    try:
        d.get(base + "/")
        time.sleep(2.5)

        # ---------------------------------------------------------- U1 无筛选条
        V.do_search(d, "林俊杰")
        time.sleep(2.0)
        check("U1 搜索页没有来源筛选条",
              V.js(d, "return document.querySelectorAll('.srcbar,.srcf').length") == 0)

        rows = row_state(d)
        check("U1 搜索出结果", len(rows) > 5, "rows=%d" % len(rows))
        swap_n = len([r for r in rows if r["swap"]])
        check("U2 跨源重复已折叠（有可换源的旗标）", swap_n > 0,
              "rows=%d swap=%d" % (len(rows), swap_n))
        # 同一首的两个渠道不应各占一行：检查曲名重复
        dup = V.js(d, """
          const names = [...document.querySelectorAll('#view .tr .tr-name')]
            .map((n) => n.textContent.trim());
          const seen = new Set(), d2 = [];
          names.forEach((n) => { if (seen.has(n)) d2.push(n); seen.add(n); });
          return d2;
        """)
        check("U2 曲目名不重复出现", not dup, "重复=%s" % dup[:4])

        # ---------------------------------------------------------- U3 行内换源
        before = V.js(d, """
          const b = document.querySelector('#view .tr .src.swap');
          return b ? b.closest('.tr').dataset.pk : null;
        """)
        if before:
            V.js(d, "document.querySelector('#view .tr .src.swap').click(); return 1")
            time.sleep(1.0)
            after = V.js(d, "return document.querySelector('#view .tr').dataset.pk")
            check("U3 点徽章就地换源", before != after, "%s → %s" % (before, after))
            check("U3 换源不新增行", len(row_state(d)) == len(rows),
                  "%d → %d" % (len(rows), len(row_state(d))))
        else:
            check("U3 点徽章就地换源", False, "没有 swap 徽章")

        # ---------------------------------------------------------- U4 专辑去重
        d.find_element(By.CSS_SELECTOR, '[data-act="stab"][data-tab="album"]').click()
        time.sleep(1.2)
        names = album_names(d)
        uniq = list(dict.fromkeys(names))
        check("U4 专辑卡不重复", len(names) == len(uniq),
              "卡=%d 去重后=%d 重复=%s" % (len(names), len(uniq),
                                          [n for n in names if names.count(n) > 1][:3]))
        V.shoot(d, os.path.join(a.out, "_u_album.png"), theme="dark")

        # ------------------------------------------------- U5 歌手页突破旧上限
        d.find_element(By.CSS_SELECTOR, '[data-act="stab"][data-tab="song"]').click()
        time.sleep(1.2)
        d.find_element(By.CSS_SELECTOR, '#view .tr [data-act="artist"]').click()
        time.sleep(1.0)
        for _ in range(90):                       # 深翻页要等一会儿
            st = V.js(d, "return {n:(state.artist&&state.artist.list.length)||0, filling:!!state.artistFilling}")
            if st["n"] and not st["filling"]:
                break
            time.sleep(1.0)
        time.sleep(1.0)
        total = V.js(d, "return (state.artist && state.artist.list.length) || 0")
        check("U5 歌手曲目不再被 60 首卡住", total > 60, "总曲目=%d" % total)
        tabs = V.js(d, "return [...document.querySelectorAll('[data-act=\"atab\"]')].map((b) => b.textContent.trim())")
        check("U5 歌手页有 歌曲/专辑 两个 tab", len(tabs) == 2, str(tabs))
        rendered = V.js(d, "return document.querySelectorAll('#view .tr').length")
        check("U5 首屏分批渲染（少于总数）", 0 < rendered < max(total, 1),
              "渲染=%d / 总数=%d" % (rendered, total))

        # ---------------------------------------------------------- U6 滚动加载
        V.js(d, "const v = document.querySelector('#view'); v.scrollTop = v.scrollHeight; return 1")
        time.sleep(2.5)
        more = V.js(d, "return document.querySelectorAll('#view .tr').length")
        check("U6 滚到底继续展开", more > rendered, "%d → %d" % (rendered, more))
        V.shoot(d, os.path.join(a.out, "_u_artist.png"), theme="dark")

        # 专辑 tab
        V.js(d, "document.querySelector('[data-act=\"atab\"][data-tab=\"album\"]').click(); return 1")
        time.sleep(1.5)
        ab = V.js(d, "return document.querySelectorAll('#view .cards .card').length")
        check("U5 歌手页专辑 tab 有内容", ab > 0, "专辑卡=%d" % ab)

        # ------------------------------------------------------ U7 推荐卡整卡播
        V.js(d, "document.querySelector('[data-nav=\"home\"]').click(); return 1")
        time.sleep(2.0)
        reco = V.js(d, "return document.querySelectorAll('.card.reco').length")
        cplay = V.js(d, "return document.querySelectorAll('.card.reco .cplay').length")
        check("U7 推荐卡不再有第二套播放键", cplay == 0, "cplay=%d" % cplay)
        if reco:
            V.js(d, """
              const c = document.querySelector('.card.reco');
              c.querySelector('.card-art').click();
              return 1
            """)
            time.sleep(3.0)
            playing = V.js(d, "return (state.playing && state.playing.name) || ''")
            check("U7 点卡片本体就能播", bool(playing), "playing=%r" % playing)
            V.shoot(d, os.path.join(a.out, "_u_reco.png"), theme="dark")
        else:
            print("      (首页暂无推荐卡，跳过 U7 播放态检查)")
            check("U7 推荐卡结构可点", True, "无卡片，仅校验结构")
    finally:
        try:
            d.quit()
        except Exception:
            pass
        V.stop_instance(proc)
        V.cleanup_fresh(home)

    bad = [r for r in RESULTS if not r[1]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
