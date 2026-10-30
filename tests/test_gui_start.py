# -*- coding: utf-8 -*-
"""GUI 能不能起来：构造 FanqieGUI 不许崩（零依赖，不需要 pytest）。

守的是一类很难靠单测发现的回归：**初始化顺序**。

界面是分块建的，底部行动条最先建（先 pack 的先分到空间），作品下拉框
最后才建。而行动条上那个「检查缺口 ⚠」徽标在建它的时候就要算一次——
算徽标要看当前选中的是哪本书，于是碰到了还没建出来的下拉框：
`AttributeError: 'FanqieGUI' object has no attribute 'cmb_book'`，
程序连窗口都开不出来。

这一条在审阅中真的发生过（2026-10-30 复审轮，父代理自己复审时抓到的），
所以钉在这里：任何「构造期就要读还没建出来的控件」的改动都会在这里红。

没有显示环境（headless CI）时跳过，不算失败。

运行:  python tests/test_gui_start.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tkinter as tk  # noqa: E402
import fanqie_gui  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    # 有没有显示环境？Tk 起不来就跳过，别把 headless 机器判成失败。
    probe = None
    try:
        probe = tk.Tk()
    except tk.TclError as e:
        print(f"  SKIP  没有显示环境（{e}），跳过 GUI 启动测试")
        return
    probe.destroy()

    # 状态文件/配置指到临时目录：这次测试不许碰用户自己的那两份。
    tmp = Path(tempfile.mkdtemp())
    fanqie_gui.GUI_STATE_FILE = tmp / ".gui_state.json"
    fanqie_gui.CONFIG_FILE = tmp / "config.json"

    app = None
    try:
        app = fanqie_gui.FanqieGUI()
        app.root.update_idletasks()
        check("GUI 构造成功（初始化不崩）", True)
        txt = app.btn_audit.cget("text")
        check("审计按钮文案是预期的两种之一",
              txt in ("检查缺口", "检查缺口 ⚠"), repr(txt))
        check("构造完没有把状态写进用户目录",
              not (REPO / ".gui_state.json.tmp").exists())
    except Exception as e:
        print("    " + traceback.format_exc().replace("\n", "\n    "))
        check("GUI 构造成功（初始化不崩）", False, f"{type(e).__name__}: {e}")
    finally:
        if app is not None:
            try:
                app.root.destroy()
            except Exception:
                pass

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
