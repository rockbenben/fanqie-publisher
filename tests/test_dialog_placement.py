# -*- coding: utf-8 -*-
"""弹窗摆位：整个外框必须在那块屏幕的**工作区**里（零依赖）。

守的是一件能把程序卡死的事：欢迎引导是 grab_set 的模态窗，它唯一的出口
是底下那颗「开始使用」按钮。按钮要是被压在任务栏下面（或整窗跑到屏幕
外），用户点不到、主窗口又被 grab 住——程序就死了，连报错都出不来。

这里量的都是真东西：客户区高度、外框（标题栏+边框）多出来的那一圈、
以及 Windows 上那块屏幕的工作区（去掉任务栏）。

没有显示环境（headless CI）时跳过，不算失败。

运行:  python tests/test_dialog_placement.py
"""
import sys
import tkinter as tk
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fanqie_gui import FanqieGUI, _display_work_area  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class Stub:
    """_place_over_parent 只需要 self.root。"""

    def __init__(self, root):
        self.root = root


def _frame_chrome(win):
    """外框比客户区多出来的那一圈：左右边框 + 标题栏。"""
    parts = win.wm_geometry().split("+")
    try:
        fx, fy = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        fx = fy = 0
    return (max(0, win.winfo_rootx() - fx), max(0, win.winfo_rooty() - fy))


def main():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"  SKIP  没有显示环境（{e}），跳过弹窗摆位测试")
        return
    root.geometry("1000x700+100+100")
    root.update()

    # 先看一眼这台机器的工作区：问不到就**失败**（不是跳过）。
    # 问不到工作区只有两种可能：这个助手坏了，或者这台机器没有任务栏那回事
    # ——后者在 Windows 上不存在。而「跳过」恰恰会在助手坏掉时把这条守卫
    # 自己关掉（变异测试实测：把助手改成永远返回 None，整目录跑一遍全绿）。
    wa = _display_work_area(root.winfo_rootx(), root.winfo_rooty())
    if not wa:
        check("这台机器问得到显示器工作区", False,
              "_display_work_area 返回 None——助手坏了，不是环境问题")
        root.destroy()
        print(f"\n{PASS} passed, {FAIL} failed")
        sys.exit(1)
    left, top, right, bottom = wa
    print(f"     工作区: {left},{top} - {right},{bottom}")

    # 主窗口摆到工作区下缘——最容易被弹窗甩出去的位置
    root.geometry(f"+{left + 10}+{max(top, bottom - 200)}")
    root.update()

    win = tk.Toplevel(root)
    win.title("欢迎")
    win.geometry("722x353")        # 真欢迎窗的尺寸（实测）
    frm = tk.Frame(win)
    frm.pack(fill="both", expand=True)
    tk.Label(frm, text="说明").pack()
    btn = tk.Button(frm, text="开始使用")
    btn.pack(side="bottom", pady=8)
    win.update()

    FanqieGUI._place_over_parent(Stub(root), win)
    win.update()

    bw, bh = _frame_chrome(win)
    frame_top = win.winfo_rooty() - bh
    frame_bottom = win.winfo_rooty() + win.winfo_height()
    print(f"     弹窗外框 y: {frame_top} .. {frame_bottom}"
          f"（客户区 {win.winfo_width()}x{win.winfo_height()}，"
          f"标题栏 {bh}px，左右边框 {bw}px）")
    check("弹窗外框整个在工作区里（上边不越界）",
          frame_top >= top, f"frame_top={frame_top} top={top}")
    check("弹窗外框整个在工作区里（下边不越界）",
          frame_bottom <= bottom, f"frame_bottom={frame_bottom} bottom={bottom}")
    check("弹窗横向整个在工作区里",
          win.winfo_rootx() >= left
          and win.winfo_rootx() + win.winfo_width() <= right,
          f"x={win.winfo_rootx()} .. {win.winfo_rootx() + win.winfo_width()}"
          f" / {left}..{right}")

    # 最要紧的一条：唯一那颗按钮不能落在工作区外面
    by = btn.winfo_rooty() + btn.winfo_height()
    bx = btn.winfo_rootx() + btn.winfo_width()
    check("「开始使用」按钮整个在工作区里（点得到）",
          by <= bottom and btn.winfo_rooty() >= top
          and bx <= right and btn.winfo_rootx() >= left,
          f"按钮 y={btn.winfo_rooty()}..{by} x={btn.winfo_rootx()}..{bx}")

    # 窗口再小一点、更挤的位置，也一样要摆得住
    root.geometry(f"+{left + 10}+{max(top, bottom - 120)}")
    root.update()
    win2 = tk.Toplevel(root)
    win2.title("小窗")
    win2.geometry("722x353")
    tk.Label(win2, text="x").pack()
    win2.update()
    FanqieGUI._place_over_parent(Stub(root), win2)
    win2.update()
    bw2, bh2 = _frame_chrome(win2)
    check("主窗口贴着下缘时，弹窗也没被推出工作区",
          (win2.winfo_rooty() - bh2) >= top
          and (win2.winfo_rooty() + win2.winfo_height()) <= bottom,
          f"y={win2.winfo_rooty() - bh2}..{win2.winfo_rooty() + win2.winfo_height()}"
          f" / {top}..{bottom}")

    root.destroy()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
