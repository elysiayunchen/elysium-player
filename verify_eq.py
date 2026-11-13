# -*- coding: utf-8 -*-
"""发烧客 Phase 2 前端验收：参数 EQ 引擎 + AutoEq 导入 + Hi-Res 徽标（B/C/D/E 组）。

断言（对应 plans/AUDIOPHILE-P2 验收节）：
  A 组 老用户回归    —— 两模式合并为单一交互页（无模式切换/双面板）：biquad 缺省 identity、
                        前级节点在图、老键 eq_gains 不被碰、预设=快速起手
  B 组 引擎实测      —— 加/删/改段实时反映到 BiquadFilter（type/frequency/Q/gain 实测）、
                        十段推子（0.1dB 拖/填/滚轮微调）、
                        自动前级 = −max(0, maxBandDb)、手动前级步进、350ms 防抖落盘、10 段上限
  C 组 AutoEq 导入   —— 好/坏文本走 POST /api/eq/parse、导入后段表重配引擎 + 存档案库、
                        档案一键切回、删档案
  D 组 Hi-Res 徽标   —— 只认实测 STREAMINFO：96k/24 显示、44.1k/16 不显示、无数据不显示
  E 组 收尾          —— 无 SEVERE console 异常、eq_gains 老键全程不被写动

用法：python verify_eq.py            （跑完自动清理隔离目录）
依赖：selenium + 本机 Chrome；用带 selenium 的解释器跑，例如
      C:/Users/19645/miniconda3/python.exe verify_eq.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_gapless import spawn, make_driver      # noqa: E402
from selenium.webdriver.common.by import By        # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


def note(text):
    print("      · " + text)


def js(driver, script, *args):
    return driver.execute_script(script, *args)


def jsa(driver, script, *args):
    """execute_async_script 的尾部补 done 参数。
    done 必须声明在脚本之前：无 await 的脚本里 done() 是同步调用，
    追加在尾部会踩 const 暂时性死区 → ReferenceError → 脚本超时。"""
    return driver.execute_async_script(
        "const done = arguments[arguments.length-1];\n" + script, *args)


def click_real(driver, sel, label):
    """真实点击路径 + elementFromPoint 命中测试（验收铁律：JS .click() 是盲区）。"""
    r = jsa(driver, r"""
      (async () => {
        const el = document.querySelector(arguments[0]);
        if (!el) { done({found: false}); return; }
        el.scrollIntoView({block: 'center'});
        await new Promise(r => setTimeout(r, 120));
        const b = el.getBoundingClientRect();
        const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
        done({found: true, hitOk: !!(hit && (hit === el || el.contains(hit) || (hit && hit.contains(el))))});
      })();
    """, sel)
    if not r.get("found"):
        return False, "元素不存在"
    if not r.get("hitOk"):
        return False, "elementFromPoint 未命中（被遮挡/出视口）"
    driver.find_element(By.CSS_SELECTOR, sel).click()
    return True, ""


def last_toast(driver):
    return js(driver, "const t=document.querySelectorAll('#toasts .toast');"
                      "return t.length ? t[t.length-1].textContent : '';")


def store(driver):
    return js(driver, "return fetch('/api/store').then(r=>r.json()).then(d=>d.data||d);")


def open_settings(driver):
    # 顶栏 #settingsBtn 在普通浏览器语境（无 pywebview）下是 0×0 隐藏的，
    # 走侧栏「设置」导航项 —— 浏览器里唯一真实的用户路径
    ok, why = click_real(driver, '.snav-i[data-nav="settings"]', "侧栏设置")
    if not ok:
        return False, why
    time.sleep(0.5)
    return True, ""


# 好文本：AutoEq 导出的典型 ParametricEQ.txt（2 段 + 前级；含 kHz 与 LS 类型）
GOOD_APO = ("Preamp: -6.5 dB\n"
            "Filter 1: ON PK Fc 105 Hz Gain 6.5 dB Q 0.71\n"
            "Filter 2: ON LS Fc 1.2 kHz Gain -3.0 dB Q 0.70\n"
            "Filter 3: ON PK Fc 4.1 kHz Gain 2.5 dB Q 1.10\n")
BAD_APO = "这不是一份 APO 配置，随便写的乱文本 12345"


def main():
    base_dir = tempfile.mkdtemp(prefix="elysium_eq_")
    proc = d = None
    try:
        proc, base, home = spawn(base_dir)
        d = make_driver()
        d.get(base + "/")
        time.sleep(2.0)

        # 引擎 2.0 必须在（图形/参数 EQ 都挂在引擎图上）；桌面默认开，双保险再确认一次
        jsa(d, r"""
          (async () => {
            state.settings.engine_v2 = true;
            await engRebuild();
            done(ENG.on);
          })();
        """)
        time.sleep(0.4)
        if not js(d, "return ENG.on;"):
            raise SystemExit("引擎 2.0 没起来，验收没法做")

        # ================================================== A 组：老用户回归
        ok, why = open_settings(d)
        if not ok:
            raise SystemExit("设置抽屉打不开：" + why)
        r = js(d, r"""
          const seg = document.querySelector('#eqModeSeg');
          const g = document.querySelector('#eqGraphicPane');
          const p = document.querySelector('#eqPane');
          const faders = document.querySelectorAll('#eqBands input[data-gi]');
          const curve = document.getElementById('eqCurve');
          const presetOpts = [...document.querySelectorAll('#setEqPreset option')].map(o => o.value);
          return {
            segGone: seg === null, gGone: g === null,
            unified: !!p,
            faders: faders.length,
            curve: !!curve,
            presetOpts,
            preamp: !!ENG.preamp, preampGain: ENG.preamp ? ENG.preamp.gain.value : null,
            eq0: { type: ENG.eq[0].type, f: ENG.eq[0].frequency.value, q: ENG.eq[0].Q.value, g: ENG.eq[0].gain.value },
            mode: state.settings.eq_mode,
          };
        """)
        check("A1 单一交互页：推子主控 + 曲线联动（无模式切换）",
              r["segGone"] and r["gGone"] and r["unified"] and r["faders"] == 10 and r["curve"], "%s" % r)
        check("A2 预设下拉照旧渲染（平直/低音增强/…/自定义）",
              {"flat", "bass", "vocal", "classical", "pop", "rock", "custom"} <= set(r["presetOpts"]),
              "%s" % r["presetOpts"])
        check("A3 前级节点已入图且增益 1（EQ 关闭直通）",
              r["preamp"] and abs((r["preampGain"] or 0) - 1) < 1e-6,
              "preamp=%s gain=%s" % (r["preamp"], r["preampGain"]))
        check("A4 biquad 照旧 peaking / 31Hz / Q=1 / gain 0",
              r["eq0"]["type"] == "peaking" and abs(r["eq0"]["f"] - 31) < 0.1
              and abs(r["eq0"]["q"] - 1.0) < 1e-6 and abs(r["eq0"]["g"]) < 1e-6,
              "%s" % r["eq0"])
        st = store(d)
        check("A5 迁移不发生在全零数据上（eq_mode 仍为缺省）",
              st["settings"].get("eq_mode") in (None, "graphic"),
              "%s" % st["settings"].get("eq_mode"))

        # 开 EQ + 预设「低音增强」→ 段表生效，bands 等价为 peaking/Q1（预设 = 快速起手）
        js(d, r"""
          const cb = document.querySelector('#setEqOn');
          cb.checked = true;
          cb.dispatchEvent(new Event('change', {bubbles: true}));
          const sel = document.querySelector('#setEqPreset');
          sel.value = 'bass';
          sel.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(0.4)
        r = js(d, r"""
          return { pre: ENG.preamp.gain.value,
                   gains: ENG.eq.map(b => b.gain.value),
                   preset: state.settings.eq_preset,
                   bands: state.settings.eq_bands.map(b => [b.f, b.db]),
                   fader31: document.querySelector('#eqBands input[data-gi="0"]').value,
                   fader62: document.querySelector('#eqBands input[data-gi="1"]').value,
                   faderLab62: document.querySelector('#eqBands input[data-gn="1"]').value };
        """)
        check("A6 开 EQ + 预设「低音增强」→ 31/62Hz 实测 +6/+5dB，自动前级 ≈ −6dB",
              abs(r["gains"][0] - 6) < 0.3 and abs(r["gains"][1] - 5) < 0.3
              and r["preset"] == "bass" and abs(r["pre"] - 10 ** (-6 / 20)) < 0.02,
              "%s" % r)
        check("A6b 预设落引擎后推子镜像显示（31/62Hz → +6/+5）",
              r["fader31"] == "6" and r["fader62"] == "5" and r["faderLab62"] == "5.0",
              "%s" % r)

        # 推子来回拉一个来回：+3 → 引擎实测 +3，清 0 → 段被删除（identity）
        js(d, r"""
          const inp = document.querySelector('#eqBands input[data-gi="4"]');
          inp.value = 3;
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        time.sleep(0.3)
        r = js(d, r"""
          return { n: (state.settings.eq_bands || []).filter(b => b.f === 500).length,
                   db500: (state.settings.eq_bands.find(b => b.f === 500) || {}).db,
                   g: ENG.eq.filter(b => Math.abs(b.frequency.value - 500) < 1)[0]?.gain.value };
        """)
        check("A7 推子 500Hz → +3：段表/引擎同步",
              r["n"] == 1 and abs(r["db500"] - 3) < 0.01 and abs((r["g"] or 0) - 3) < 0.3, "%s" % r)
        js(d, r"""
          const inp = document.querySelector('#eqBands input[data-gi="4"]');
          inp.value = 0;
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        time.sleep(0.3)
        r = js(d, "return { n: (state.settings.eq_bands || []).filter(b => b.f === 500).length };")
        check("A7b 推子回 0 → 500Hz 段被删除（恒等 = 不存在）",
              r["n"] == 0, "%s" % r)
        js(d, r"""
          const cb = document.querySelector('#setEqOn');
          cb.checked = false;
          cb.dispatchEvent(new Event('change', {bubbles: true}));
          const sel = document.querySelector('#setEqPreset');
          sel.value = 'flat';
          sel.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(0.3)

        # ================================================== B 组：引擎实测（单一交互页）
        # B 组起点：EQ 开启（直通空态）—— A6 刚复位过，这里把它打开
        js(d, r"""
          const cb = document.querySelector('#setEqOn');
          cb.checked = true;
          cb.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(0.3)
        r = js(d, r"""
          return {
            eq0: { type: ENG.eq[0].type, f: ENG.eq[0].frequency.value, g: ENG.eq[0].gain.value },
            rows: state.settings.eq_bands.length,
            empty: !!document.querySelector('#eqSelBar .eq-empty'),
            selbarVisible: !!document.querySelector('#eqSelBar'),
            segGone: document.querySelector('#eqModeSeg') === null,
          };
        """)
        check("B1 EQ 开启 + 段表空 → 直通（identity + 空选择条）",
              r["rows"] == 0 and r["empty"] and r["selbarVisible"] and r["segGone"], "%s" % r)
        check("B2 EQ 开启后 BiquadFilter 全 identity（gain 0）",
              r["eq0"]["type"] == "peaking" and abs(r["eq0"]["g"]) < 1e-6, "%s" % r["eq0"])

        ok, why = click_real(d, "#eqAddBand", "加一段")
        check("B4 「加一段」真实可达且命中", ok, why)
        time.sleep(0.3)
        r = js(d, r"""
          const b = state.settings.eq_bands[0];
          return { rows: state.settings.eq_bands.length,
                   sel: state.eqSel,
                   fIn: document.querySelector('#eqF')?.value,
                   qIn: document.querySelector('#eqQ')?.value,
                   dbIn: document.querySelector('#eqDb')?.value,
                   editing: document.getElementById('eqCurve')?.classList.contains('eq-editing'),
                   handles: EQ_HANDLES.length,
                   band: b, eq0: { type: ENG.eq[0].type, f: ENG.eq[0].frequency.value, q: ENG.eq[0].Q.value } };
        """)
        check("B5 加一段默认 peaking/1kHz/Q1，新段直接选中且曲线有圆点",
              r["rows"] == 1 and r["sel"] == 0 and r["band"]["type"] == "peaking"
              and abs(r["band"]["f"] - 1000) < 0.1 and abs(r["band"]["q"] - 1) < 1e-6
              and r["fIn"] == "1000" and r["qIn"] == "1.00" and r["dbIn"] == "0.0"
              and r["handles"] == 1,
              "%s" % r)
        check("B6 引擎重配到该段（f=1000 Q=1 peaking）",
              r["eq0"]["type"] == "peaking" and abs(r["eq0"]["f"] - 1000) < 0.1
              and abs(r["eq0"]["q"] - 1) < 1e-6, "%s" % r["eq0"])

        # 拖增益到 +6：读数、引擎增益、自动前级三处都要跟上
        js(d, r"""
          const inp = document.querySelector('#eqDb');
          inp.value = 6;
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        time.sleep(0.35)      # setTargetAtTime(0.03) 收敛 + 读数更新
        r = js(d, r"""
          return { g: ENG.eq[0].gain.value, pre: ENG.preamp.gain.value,
                   txt: document.querySelector('#eqPreVal').textContent,
                   out: document.querySelector('#eqDb').value };
        """)
        want_pre = 10 ** (-6 / 20)
        check("B7 拖增益 +6 → biquad gain 实测 ≈6", abs(r["g"] - 6) < 0.3, "gain=%.3f" % r["g"])
        check("B8 自动补偿 → 前级实测 ≈10^(-6/20)", abs(r["pre"] - want_pre) < 0.02,
              "pre=%.4f want=%.4f" % (r["pre"], want_pre))
        check("B9 前级读数带补偿（前级 0.0 dB · 补偿 −6.0）",
              "0.0 dB" in r["txt"] and "补偿" in r["txt"] and "−6.0" in r["txt"], "%s" % r["txt"])
        check("B10 段增益输入框值 +6", abs(float(r["out"]) - 6) < 1e-6, "%s" % r["out"])

        # 精确输入：频率/Q 直接输数（不再用对数滑条）
        js(d, r"""
          const q = document.querySelector('#eqQ');
          q.value = 2;
          q.dispatchEvent(new Event('input', {bubbles: true}));
          const f = document.querySelector('#eqF');
          f.value = 873;
          f.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        time.sleep(0.25)
        r = js(d, "return { q: ENG.eq[0].Q.value, f: ENG.eq[0].frequency.value };")
        check("B11 Q 输入 2 → 引擎实测 Q=2", abs(r["q"] - 2.0) < 0.01, "Q=%.3f" % r["q"])
        check("B12 频率输入 873Hz → 引擎实测 873Hz", abs(r["f"] - 873) < 2, "f=%.1f" % r["f"])

        # 手动前级步进：+0.5 ×2 → 手动 1.0，总前级 = 10^((1-6)/20)
        js(d, "document.querySelector('#eqPreUp').click(); document.querySelector('#eqPreUp').click();")
        time.sleep(0.3)
        r = js(d, r"""
          return { pre: ENG.preamp.gain.value, val: Number(state.settings.eq_preamp),
                   txt: document.querySelector('#eqPreVal').textContent };
        """)
        want = 10 ** ((1.0 - 6.0) / 20)
        check("B13 前级 +0.5×2 → 手动 1.0dB，总前级实测 10^(-5/20)",
              abs(r["val"] - 1.0) < 1e-6 and abs(r["pre"] - want) < 0.02,
              "val=%.1f pre=%.4f want=%.4f" % (r["val"], r["pre"], want))

        # 关自动补偿 → 前级只剩手动 1dB
        js(d, r"""
          const cb = document.querySelector('#setEqAutoPre');
          cb.checked = false;
          cb.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(0.3)
        r = js(d, r"""
          return { pre: ENG.preamp.gain.value, txt: document.querySelector('#eqPreVal').textContent };
        """)
        check("B14 关自动补偿 → 前级实测 10^(1/20)，读数无补偿字样",
              abs(r["pre"] - 10 ** (1.0 / 20)) < 0.02 and "补偿" not in r["txt"],
              "pre=%.4f txt=%s" % (r["pre"], r["txt"]))
        js(d, r"""
          const cb = document.querySelector('#setEqAutoPre');
          cb.checked = true;
          cb.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(0.2)

        # 类型 chips + 删段
        ok, _ = click_real(d, '#eqSelBar button[data-t="lowshelf"]', "低架 chip")
        time.sleep(0.3)
        r = js(d, "return { t: ENG.eq[0].type, saved: state.settings.eq_bands[0].type };")
        check("B15 类型 chip 切低架 → 引擎与段表同步 lowshelf", ok and r["t"] == "lowshelf", "%s" % r)
        ok, _ = click_real(d, '#eqSelBar button[data-t="peaking"]', "峰 chip")
        time.sleep(0.2)
        ok2, _ = click_real(d, "#eqDelBand", "删段按钮")
        time.sleep(0.3)
        r = js(d, r"""
          return { rows: state.settings.eq_bands.length,
                   selEmpty: !!document.querySelector('#eqSelBar .eq-empty'),
                   g: ENG.eq[0].gain.value };
        """)
        check("B16 删段 → 段表归零 + 引擎落 identity + 空态回来",
              ok2 and r["rows"] == 0 and r["selEmpty"] and abs(r["g"]) < 1e-6, "%s" % r)

        # 10 段上限
        for _ in range(11):
            js(d, "document.querySelector('#eqAddBand').click();")
        time.sleep(0.3)
        r = js(d, r"""
          return { n: state.settings.eq_bands.length, toast: (t => t.length ? t[t.length-1].textContent : '')
                   (document.querySelectorAll('#toasts .toast')) };
        """)
        check("B17 加到 10 段封顶 + 提示「最多 10 段」",
              r["n"] == 10 and "最多 10 段" in r["toast"],
              "n=%s toast=%s" % (r["n"], r["toast"]))
        # 清空段表，给 C 组一个干净起点
        for _ in range(10):
            js(d, "document.querySelector('#eqDelBand') && document.querySelector('#eqDelBand').click()")
            time.sleep(0.08)

        # 防抖落盘：拖一次，立刻查库应是旧值，400ms 后应是新值
        ok, _ = click_real(d, "#eqAddBand", "加一段（防抖用例）")
        js(d, r"""
          const inp = document.querySelector('#eqDb');
          inp.value = 3;
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        r0 = store(d)
        time.sleep(0.6)
        r1 = store(d)
        bands1 = r1["settings"].get("eq_bands") or []
        check("B18 拖动 350ms 防抖后落盘（+3.0 在库）",
              len(bands1) == 1 and abs((bands1[0] or {}).get("db", -99) - 3.0) < 0.01,
              "库内=%s" % bands1)

        # 拖动（防抖挂起）→ 350ms 内做结构改动：旧定时器不得把改动前的段表写回去
        ok, _ = click_real(d, "#eqAddBand", "加一段（防抖交错用例）")
        time.sleep(0.2)
        js(d, r"""
          const inp = document.querySelector('#eqDb');
          inp.value = 8;
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        time.sleep(0.05)
        js(d, r"""
          // 点中第 1 段的控制点 → 切换选中到它（新段刚加，默认选中的是新段）
          const h = EQ_HANDLES[0];
          const c = document.getElementById('eqCurve');
          const r = c.getBoundingClientRect();
          const pe = new PointerEvent('pointerdown', { bubbles: true, pointerId: 1, isPrimary: true,
                                                        clientX: r.left + h.x, clientY: r.top + h.y, button: 0 });
          c.dispatchEvent(pe);
        """)
        time.sleep(0.1)
        js(d, r"""
          document.querySelector('#eqDelBand').click();   // 立即落盘的结构改动（删掉的是旧段）
        """)
        time.sleep(0.6)
        r2 = store(d)
        bands2 = r2["settings"].get("eq_bands") or []
        check("B19 防抖不回滚：拖动+删旧段交错后落盘 = 新段（+8.0）",
              len(bands2) == 1 and abs((bands2[0] or {}).get("db", -99) - 8.0) < 0.01,
              "库内=%s" % bands2)

        # 推子数字直填：一位小数（6.5）→ 段表/引擎跟随（当前段在 1kHz 标准位 = 推子 6 号）
        js(d, r"""
          const inp = document.querySelector('#eqBands input[data-gn="5"]');
          inp.value = '6.5';
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        time.sleep(0.2)
        r_after = js(d, "return { db: state.settings.eq_bands[0].db, g: ENG.eq[0].gain.value };")
        check("B20 推子数字直填 6.5 → 段表/引擎同步（一位小数）",
              abs(r_after["db"] - 6.5) < 1e-6 and abs(r_after["g"] - 6.5) < 0.3,
              "%s" % r_after)

        # 推子滚轮微调：上滚 +0.1dB → 引擎跟随
        js(d, r"""
          const band = document.querySelector('#eqBands .eq-band[data-gb="5"]');
          band.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true, cancelable: true }));
        """)
        time.sleep(0.15)
        r_wheel = js(d, "return { db: state.settings.eq_bands[0].db, g: ENG.eq[0].gain.value };")
        check("B21 推子滚轮上滚 → +0.1dB（微调）",
              abs(r_wheel["db"] - 6.6) < 1e-6 and abs(r_wheel["g"] - 6.6) < 0.3,
              "%s" % r_wheel)

        # ================================================== C 组：AutoEq 导入
        ok, why = click_real(d, "#eqImportBtn", "导入 AutoEq 按钮")
        check("C1 「导入 AutoEq」真实可达", ok, why)
        time.sleep(0.3)
        js(d, r"""
          document.querySelector('#eqHpModel').value = 'Test HP';
          const ta = document.querySelector('#eqImportText');
          ta.value = arguments[0];
        """, GOOD_APO)
        ok, _ = click_real(d, "#eqImportGo", "解析并应用")
        time.sleep(1.0)
        r = js(d, r"""
          return {
            mode: state.settings.eq_mode, hp: state.settings.eq_headphone,
            pre: state.settings.eq_preamp, n: (state.settings.eq_bands || []).length,
            b0: (state.settings.eq_bands || [])[0],
            b1: (state.settings.eq_bands || [])[1],
            preset: state.settings.eq_preset,
            libSel: document.querySelector('#setEqLib') ? document.querySelector('#setEqLib').innerHTML : '',
            toast: (t => t.length ? t[t.length-1].textContent : '')(document.querySelectorAll('#toasts .toast')),
          };
        """, GOOD_APO)
        check("C2 好文本导入 → 3 段 + 前级 −6.5 且整表重配引擎",
              ok and r["mode"] == "parametric" and r["n"] == 3 and abs(r["pre"] + 6.5) < 1e-6,
              "%s" % {k: r[k] for k in ("mode", "n", "pre")})
        check("C3 解析细节：PK→peaking 105Hz/6.5/Q0.71、kHz→1200Hz lowshelf",
              r["b0"] and r["b0"]["type"] == "peaking" and abs(r["b0"]["f"] - 105) < 0.1
              and abs(r["b0"]["db"] - 6.5) < 0.1 and abs(r["b0"]["q"] - 0.71) < 0.01
              and r["b1"] and r["b1"]["type"] == "lowshelf" and abs(r["b1"]["f"] - 1200) < 0.1,
              "%s / %s" % (r["b0"], r["b1"]))
        check("C4 型号落 eq_headphone + eq_preset=autoeq + 档案库有「Test HP」",
              r["hp"] == "Test HP" and r["preset"] == "autoeq" and "Test HP" in r["libSel"],
              "hp=%s preset=%s" % (r["hp"], r["preset"]))
        check("C5 toast 报告导入结果（3 段 · 前级 −6.5 dB）",
              "3 段" in r["toast"] and "−6.5" in r["toast"], "%s" % r["toast"])
        st = store(d)
        lib = st.get("eqpresets") or {}
        check("C6 eqpreset_set 已落库（bands/preamp/at 全带）",
              lib.get("Test HP") and len(lib["Test HP"]["bands"]) == 3
              and abs(lib["Test HP"]["preamp"] + 6.5) < 0.01 and lib["Test HP"].get("at", 0) > 0,
              "%s" % lib)
        r = js(d, "return { g: ENG.eq[0].gain.value, f: ENG.eq[0].frequency.value };")
        check("C7 导入即听感：引擎已按新段表重配（105Hz/6.5dB）",
              abs(r["f"] - 105) < 0.5 and abs(r["g"] - 6.5) < 0.3, "%s" % r)

        # 坏文本：报错 + 段表不被清掉。C2 成功后盒子会自动收起，先重新展开
        ok, _ = click_real(d, "#eqImportBtn", "导入 AutoEq（重新展开）")
        time.sleep(0.2)
        js(d, "document.querySelector('#eqImportText').value = arguments[0];", BAD_APO)
        ok2, _ = click_real(d, "#eqImportGo", "解析并应用（坏文本）")
        time.sleep(0.8)
        r = js(d, r"""
          return { n: (state.settings.eq_bands || []).length,
                   toast: (t => t.length ? t[t.length-1].textContent : '')(document.querySelectorAll('#toasts .toast')),
                   toastErr: (t => t.length ? t[t.length-1].classList.contains('err') : false)
                   (document.querySelectorAll('#toasts .toast')) };
        """, BAD_APO)
        check("C8 坏文本 → err toast 明确报错、段表原样保留",
              ok and ok2 and r["toastErr"] and r["n"] == 3 and "识别" in r["toast"],
              "n=%s toast=%s" % (r["n"], r["toast"]))

        # 档案一键切回 + 删档案
        js(d, r"""
          const sel = document.querySelector('#setEqLib');
          sel.value = 'Test HP';
          sel.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(0.5)
        r = js(d, r"""
          return { hp: state.settings.eq_headphone, pre: state.settings.eq_preamp,
                   n: (state.settings.eq_bands || []).length };
        """)
        check("C9 档案库选「Test HP」→ 段表/前级/型号整包切回",
              r["hp"] == "Test HP" and abs(r["pre"] + 6.5) < 0.01 and r["n"] == 3, "%s" % r)
        # 删档案走 confirm 二次确认（与删歌单同一惯例）；headless 里补一个放行桩
        js(d, "window.confirm = () => true;")
        ok, _ = click_real(d, "#eqLibDel", "删档案")
        time.sleep(0.5)
        st = store(d)
        check("C10 删档案（confirm 确认后）→ eqpreset_del 落盘、库空",
              ok and not ((st.get("eqpresets") or {}).get("Test HP")),
              "lib=%s" % (st.get("eqpresets"),))

        # ================================================== D 组：Hi-Res 徽标
        r = jsa(d, r"""
          (async () => {
            const out = {};
            state.playing = { platform: 'wyy', id: 'hr1', name: 'Hi-Res 测试', artist: '' };
            state.currentQuality = { format: 'flac', bitrate: 900 };
            // 有实测 96k/24 → 显示
            PROBE.set('wyy:hr1', { valid: true, fmt: 'flac', hires: { sample_rate: 96000, bit_depth: 24, channels: 2 } });
            paintSpec();
            out.hi = document.querySelector('#plSpec').textContent;
            out.npHi = document.querySelector('#npSpec').textContent;
            // CD 规格 44.1k/16 → 不显示（不是 Hi-Res）
            PROBE.set('wyy:hr1', { valid: true, fmt: 'flac', hires: { sample_rate: 44100, bit_depth: 16, channels: 2 } });
            paintSpec();
            out.cd = document.querySelector('#plSpec').textContent;
            // 无数据 → 不显示
            PROBE.delete('wyy:hr1');
            paintSpec();
            out.none = document.querySelector('#plSpec').textContent;
            state.playing = null; state.currentQuality = null;
            done(out);
          })();
        """)
        check("D1 实测 96kHz/24bit → 播放条与展开页都显示",
              "96kHz/24bit" in r["hi"] and "96kHz/24bit" in r["npHi"], "%s" % r["hi"])
        check("D2 CD 规格 44.1kHz/16bit → 不显示徽标",
              "16bit" not in r["cd"], "%s" % r["cd"])
        check("D3 无数据 → 不显示（不给假情报）",
              "bit" not in r["none"], "%s" % r["none"])

        # ================================================== E 组：收尾
        st = store(d)
        gains = st["settings"].get("eq_gains")
        check("E1 老键 eq_gains 全程未被写动（缺省图形推子 10×0 原样）",
              gains == [0] * 10, "%s" % gains)
        errs = [e for e in d.get_log("browser")
                if e.get("level") == "SEVERE" and "favicon" not in e.get("message", "")
                # C8 坏文本故意打的 400 会在网络层留一条资源日志，不是页面异常
                and not ("/api/eq/parse" in e.get("message", "")
                         and "status of 400" in e.get("message", ""))]
        check("E2 无未捕获的严重异常", not errs,
              "; ".join(e["message"][:140] for e in errs[:3]))

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
        shutil.rmtree(base_dir, ignore_errors=True)

    ok = sum(1 for _, p, _ in RESULTS if p)
    print("\n== 结果：%d pass / %d fail ==" % (ok, len(RESULTS) - ok))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
