# -*- coding: utf-8 -*-
"""窗口与托盘（P6）逻辑验收：关闭行为 / 托盘降级 / 迷你条桥。

托盘的**观感**（任务栏图标、右键菜单、双击还原）必须在真机上肉眼看，
沙箱里的 GUI 进程活不过命令结束，测不了。这里只把能确定的部分钉住：

  W1 设置项齐全        —— tray / close_behavior 有默认值，模式只有 exit / tray
  W2 默认不惊喜        —— 没设过时点关闭就是退出，不是收托盘
  W3 收托盘生效        —— 选 tray 且有托盘图标时，关窗被拦下并隐藏
  W4 托盘不在就真退出  —— 图标没起来时绝不藏窗口（否则叫不回来）
  W5 退出时不再拦      —— 从托盘菜单退出，_TRAY.win 已摘，放行
  W6 托盘降级          —— pystray 装不上时 start_tray 返回 False，不抛
  W7 托盘图标可用      —— app.png / app.ico 能开出 RGBA 图
  W8 迷你条桥          —— 无窗口时安全返回 False；尺寸常量与 CSS 对得上

用法：python verify_win.py
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))
    return bool(ok)


class FakeWin:
    def __init__(self):
        self.hidden = False
        self.destroyed = False
        self.shown = False

    def hide(self):
        self.hidden = True

    def show(self):
        self.shown = True
        self.hidden = False

    def restore(self):
        pass

    def destroy(self):
        self.destroyed = True

    def resize(self, w, h):
        self.size = (w, h)

    def move(self, x, y):
        self.pos = (x, y)


def main():
    tmp = tempfile.mkdtemp(prefix="elysium_win_")
    os.environ["ELYSIUM_HOME"] = tmp
    sys.path.insert(0, REPO)
    import main as app

    # ---------------------------------------------------------- W1 设置项
    st = app.DEFAULT_STORE["settings"]
    check("W1a 默认建托盘图标", st.get("tray") is True, "tray=%s" % st.get("tray"))
    check("W1b 默认点关闭=退出（不惊喜）", st.get("close_behavior") == "exit",
          "close_behavior=%s" % st.get("close_behavior"))
    check("W1c 迷你条尺寸与 CSS 一致（430×138）", app.MINI_SIZE == (430, 138),
          "%s" % (app.MINI_SIZE,))

    # ---------------------------------------------------------- W2 默认退出
    win = FakeWin()
    app._TRAY.update(win=win, icon=None)
    app.STORE.data["settings"]["close_behavior"] = "exit"
    check("W2 默认设置下关窗放行（真退出）", app._on_closing(win) is True and not win.hidden)

    # ---------------------------------------------------------- W4 托盘不在
    app.STORE.data["settings"]["close_behavior"] = "tray"
    app._TRAY["icon"] = None
    ok = app._on_closing(win)
    check("W4 托盘没起来时也放行，不藏窗口", ok is True and not win.hidden,
          "icon=None hidden=%s" % win.hidden)

    # ---------------------------------------------------------- W3 收托盘
    app._TRAY["icon"] = object()          # 假装托盘图标在
    ok = app._on_closing(win)
    check("W3 选 tray 且托盘可用时拦下并隐藏", ok is False and win.hidden,
          "返回=%s hidden=%s" % (ok, win.hidden))

    # ---------------------------------------------------------- W5 退出不拦
    app._TRAY.update(win=win, icon=object())
    app._tray_quit()
    check("W5a 托盘退出会销毁窗口", win.destroyed is True)
    check("W5b 退出前摘掉窗口引用（保证不被拦）",
          app._TRAY["win"] is None and app._on_closing(win) is True,
          "win=%s" % app._TRAY["win"])

    # ---------------------------------------------------------- W6 降级
    saved = sys.modules.get("pystray", "MISSING")
    sys.modules["pystray"] = None         # import 时抛 ImportError
    app._TRAY.update(win=FakeWin(), icon=None)
    try:
        r = app.start_tray(app._TRAY["win"])
        check("W6 pystray 装不上时静默降级（返回 False 不抛）", r is False, "返回=%s" % r)
    except Exception as e:                # noqa: BLE001
        check("W6 pystray 装不上时静默降级（返回 False 不抛）", False, "抛了：%s" % e)
    finally:
        if saved == "MISSING":
            sys.modules.pop("pystray", None)
        else:
            sys.modules["pystray"] = saved

    # ---------------------------------------------------------- W7 图标
    im = app._tray_image()
    check("W7 托盘图标能从 app.png/app.ico 开出 RGBA 图",
          im is not None and im.mode == "RGBA" and im.size[0] > 0,
          "%s %s" % (getattr(im, "mode", None), getattr(im, "size", None)))

    # ---------------------------------------------------------- W8 迷你条桥
    app._TRAY["win"] = None
    b = app.Bridge()
    check("W8a 无窗口时 mini_mode 安全返回 False", b.mini_mode(True) is False)

    w2 = FakeWin()
    w2.width, w2.height = 1240, 820
    app._TRAY["win"] = w2
    ok = b.mini_mode(True)
    check("W8b 进入迷你条会缩到 430×138", ok is True and getattr(w2, "size", None) == (430, 138),
          "size=%s" % (getattr(w2, "size", None),))
    ok = b.mini_mode(False)
    check("W8c 还原回原来的窗口尺寸", ok is True and getattr(w2, "size", None) == (1240, 820),
          "size=%s" % (getattr(w2, "size", None),))

    ok = sum(1 for _, p, _ in RESULTS if p)
    print("\n== 结果：%d pass / %d fail ==" % (ok, len(RESULTS) - ok))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
