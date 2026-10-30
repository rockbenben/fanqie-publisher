# -*- coding: utf-8 -*-
"""GUI 里两条新逻辑的回归测试（零依赖：不需要 pytest / 真窗口）。

1) _schedule_param_warnings：排期输入框里「填了但不会被算进排期」的值，
   必须出现在预览面板里。以前这些值是静默丢的——用户要么点「开始上传」
   才被拦下，要么直接带着和自己填的不一样的排期发出去。

2) _fit_guidance_to_height：窗口被拖矮时自动收起引导栏，把高度让给
   章节预览（预览是不可逆发布前唯一的复核环节）。

运行:  python tests/test_gui_params.py
"""
import sys
import tkinter as tk
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fanqie_gui import (  # noqa: E402
    FanqieGUI, GUIDE_COLLAPSE_H, GUIDE_EXPAND_H,
)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------- 假变量/控件
class Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        if isinstance(self.v, Exception):
            raise self.v
        return self.v


class Widget:
    """只需要 winfo_manager()：返回当前管着它的几何管理器名。"""

    def __init__(self, packed=False):
        self.packed = packed

    def winfo_manager(self):
        return "pack" if self.packed else ""


# ------------------------------------------------------- _schedule_param_warnings
class WarnSelf:
    def __init__(self, date="", time="", days="", perday=1, days_packed=True):
        self.date_var = Var(date)
        self.time_var = Var(time)
        self.days_ahead_var = Var(days)
        self.perday_var = Var(perday)
        self.ent_days_ahead = Widget(days_packed)


def warns(**kw):
    return FanqieGUI._schedule_param_warnings(WarnSelf(**kw))


print("[W1 正常输入没有警告]")
check("W1 全空 → 无警告", warns() == [], str(warns()))
check("W1 正常日期+时间 → 无警告",
      warns(date="2026-10-29", time="08:00,12:00") == [],
      str(warns(date="2026-10-29", time="08:00,12:00")))
check("W1 分号也是合法分隔符",
      warns(time="08:00;12:00") == [], str(warns(time="08:00;12:00")))
check("W1 单位数小时 8:00 也认",
      warns(time="8:00") == [], str(warns(time="8:00")))

print("[W2 会被静默丢掉的时段必须报出来]")
w = warns(time="08:00、12:00")
check("W2 中文顿号 → 报出来", len(w) == 1, str(w))
check("W2 中文顿号 → 说清是分隔符的问题",
      w and "分隔符" in w[0], str(w))
w = warns(time="25:00")
check("W2 25:00 → 报出来", len(w) == 1, str(w))
check("W2 25:00 → 说清是值不合法（不是分隔符）",
      w and "分隔符" not in w[0] and "HH:MM" in w[0], str(w))
w = warns(time="08:00,25:00")
check("W2 混着填只报坏的那个",
      w and len(w) == 1 and "25:00" in w[0] and "08:00" not in w[0], str(w))
w = warns(time="八点")
check("W2 非时间格式 → 报出来", len(w) == 1, str(w))
w = warns(time="08:00、25:00")
check("W2 顿号 + 越界值 → 两个问题一起点出来（只说一件会让人改完再撞）",
      w and len(w) == 1 and "25:00" in w[0]
      # 断言要落在「分隔符」这几个字上：光看「、」是不行的——每条消息的
      # 尾巴「小时 00-23、分钟 00-59」里都有它，删掉分隔符那句照样能过。
      and "分隔符" in w[0] and "两个问题" in w[0], str(w))
w = warns(time="08:00、12:00")
check("W2 只有顿号的问题 → 说清是分隔符",
      w and len(w) == 1 and "分隔符" in w[0] and "不认" in w[0], str(w))

print("[W3 日期与「排到…天后」]")
w = warns(date="2026/10/29")
check("W3 日期格式错 → 报出来", len(w) == 1 and "YYYY-MM-DD" in w[0], str(w))
w = warns(days="一周")
check("W3 「排到」填非整数 → 报出来", len(w) == 1 and "整数" in w[0], str(w))
w = warns(days="一周", days_packed=False)
check("W3 「排到」那栏没显示 → 不提它（别让人去找不存在的东西）",
      w == [], str(w))
w = warns(days="3")
check("W3 「排到」填合法整数 → 不报", w == [], str(w))

print("[W4 每天章数]")
w = warns(perday=0)
check("W4 每天 0 章 → 报出来", len(w) == 1 and "1-20" in w[0], str(w))
w = warns(perday=99)
check("W4 每天 99 章 → 也报出来（上限也要查）", len(w) == 1 and "1-20" in w[0], str(w))
w = warns(perday=20)
check("W4 每天 20 章 → 不报", w == [], str(w))
w = warns(perday=tk.TclError("boom"))
check("W4 每天章数读不出来 → 报出来而不是崩", len(w) == 1, str(w))


# ------------------------------------------------------ _fit_guidance_to_height
class Root:
    def __init__(self, h):
        self.h = h

    def winfo_height(self):
        return self.h


class Event:
    def __init__(self, widget):
        self.widget = widget


class DriftingRoot:
    """高度会在「决策之后、执行之前」变掉的那一档。

    第一次问是决策时看到的高度，之后问都是「真执行时」的高度——模拟一次
    快速拖拽（连着来好几个 Configure，第二个还没执行窗口就又变了）。
    """

    def __init__(self, first, then):
        self.first = first
        self.then = then
        self.calls = 0

    def winfo_height(self):
        self.calls += 1
        return self.first if self.calls == 1 else self.then


class FitSelf:
    # 推迟执行的那两个函数用真的：它们负责在真执行前再核对一次当前高度
    # （收栏不改变窗口高度，不会再有 Configure 来纠正它）。
    _collapse_guide_if_still_short = FanqieGUI._collapse_guide_if_still_short
    _expand_guide_if_still_tall = FanqieGUI._expand_guide_if_still_tall

    def __init__(self, h, packed=True, user_set=False, auto=False,
                 with_acct=True, root=None):
        self.root = root if root is not None else Root(h)
        self._guide_user_set = user_set
        self._guide_auto = auto
        self._guide_frame = Widget(packed)
        if with_acct:
            self._acct_frame = object()
        self.calls = []          # (delay, fn, args)
        self.collapsed = []

    def _after(self, delay, fn, *args):
        # 记一笔再真调用：after(0) 在下一轮事件循环才跑，但这里只关心
        # 「最终有没有收起」，所以直接执行，效果与真跑一遍一致。
        self.calls.append((delay, fn, args))
        fn(*args)

    def _set_guidance_collapsed(self, collapsed, user):
        self.collapsed.append((collapsed, user))


def fit(call=True, **kw):
    s = FitSelf(**kw)
    if call:
        FanqieGUI._fit_guidance_to_height(s)
    return s


print("[F1 窗口太矮 → 自动收起引导栏]")
s = fit(h=GUIDE_COLLAPSE_H - 1, packed=True)
check("F1 矮窗口 + 引导栏展开着 → 收起",
      s.collapsed == [(True, False)], str(s.collapsed))
check("F1 收起走 after(0) 推迟一轮（不布局过程中改几何）",
      s.calls and s.calls[0][0] == 0
      and s.calls[0][1] == s._collapse_guide_if_still_short,
      str(s.calls))

print("[F2 窗口够高 → 自动展开回来]")
s = fit(h=GUIDE_EXPAND_H + 1, packed=False, auto=True)
check("F2 高窗口 + 之前是自动收起的 → 展开",
      s.collapsed == [(False, False)], str(s.collapsed))

print("[F3 不该动的时候就不动]")
s = fit(h=GUIDE_COLLAPSE_H - 1, packed=True, user_set=True)
check("F3 用户自己表过态 → 不自动干预", s.collapsed == [], str(s.collapsed))
s = fit(h=GUIDE_EXPAND_H + 1, packed=False, auto=True, user_set=True)
check("F3 用户自己表过态 → 也不自动展开", s.collapsed == [], str(s.collapsed))
s = fit(h=GUIDE_EXPAND_H + 1, packed=False, auto=False)
check("F3 不是自动收起的（用户收的）→ 不擅自展开", s.collapsed == [], str(s.collapsed))
s = fit(h=GUIDE_COLLAPSE_H - 1, packed=False)
check("F3 已经是收起状态 → 不重复收起", s.collapsed == [], str(s.collapsed))
s = fit(h=0, packed=True)
check("F3 高度还没量出来（0）→ 不动", s.collapsed == [], str(s.collapsed))
s = fit(h=GUIDE_COLLAPSE_H - 1, packed=True, with_acct=False)
check("F3 界面还没建完 → 不动", s.collapsed == [], str(s.collapsed))

print("[F4 只认主窗口自己的 Configure]")
s = fit(call=False, h=GUIDE_COLLAPSE_H - 1, packed=True)
FanqieGUI._fit_guidance_to_height(s, Event(object()))   # 子控件的重排
check("F4 子控件的 Configure → 忽略", s.collapsed == [], str(s.collapsed))
s2 = fit(call=False, h=GUIDE_COLLAPSE_H - 1, packed=True)
FanqieGUI._fit_guidance_to_height(s2, Event(s2.root))
check("F4 主窗口自己的 Configure → 处理",
      s2.collapsed == [(True, False)], str(s2.collapsed))


print("[F5 真跑 _set_guidance_collapsed（自动收起那侧要写 _guide_auto）]")
# F1–F4 把 _set_guidance_collapsed 换成了假的，只验「有没有被调用」。
# 真的那个还负责写 _guide_auto——把那一行删掉，全套测试照样全绿（变异
# 测试实测过），但产品里窗口拖矮之后就再也不会自动展开了。这条直接跑
# 真的那个，把标志的写侧也钉住。


class Frame:
    """能 pack / pack_forget 的假 Frame。"""

    def __init__(self):
        self.packed = False

    def pack(self, **kw):
        self.packed = True

    def pack_forget(self):
        self.packed = False


class RealSelf:
    _set_guidance_collapsed = FanqieGUI._set_guidance_collapsed

    def __init__(self):
        self._gui_state = {}
        self._guide_user_set = False
        self._guide_auto = False
        self.saved = 0
        self._guide_frame = Frame()
        self._guide_stub = Frame()
        self._acct_frame = object()

    def _save_gui_state(self):
        self.saved += 1

    def _update_guidance(self):
        pass


rs = RealSelf()
rs._set_guidance_collapsed(True, False)          # 自动收起
check("F5 自动收起 → _guide_auto 变 True", rs._guide_auto is True)
check("F5 自动收起 → 不写盘（不覆盖用户存的选择）", rs.saved == 0, str(rs.saved))
check("F5 自动收起 → 引导栏真的收起来了", rs._guide_frame.packed is False)
rs._set_guidance_collapsed(False, False)         # 窗口变高，自动展开
check("F5 自动展开 → _guide_auto 变回 False", rs._guide_auto is False)
check("F5 自动展开 → 引导栏回来了", rs._guide_frame.packed is True)
rs2 = RealSelf()
rs2._set_guidance_collapsed(True, True)          # 用户自己点的
check("F5 用户自己收起 → _guide_user_set 变 True", rs2._guide_user_set is True)
check("F5 用户自己收起 → _guide_auto 变 False（不再自动干预）",
      rs2._guide_auto is False)
check("F5 用户自己收起 → 写盘记住选择", rs2.saved == 1, str(rs2.saved))
check("F5 用户自己收起 → 状态文件里有这个选择",
      rs2._gui_state.get("guidance_collapsed") is True, str(rs2._gui_state))


print("[F6 推迟执行时要重新核对（决策之后窗口可能已经变了）]")
# 决策是按当时的窗口高度做的，可真执行时窗口可能又变高了（一次快速拖拽
# 会连着来好几个 Configure）。照旧执行就会把引导栏收在一个高窗口上——
# 而收栏本身不改变窗口高度，不会再有 Configure 来纠正它，「下一步」那句
# 话就这么没了。
s = FitSelf(0, packed=True,
            root=DriftingRoot(GUIDE_COLLAPSE_H - 1, GUIDE_EXPAND_H + 1))
FanqieGUI._fit_guidance_to_height(s)
check("F6 窗口在推迟期间变高了 → 不收",
      s.collapsed == [], str(s.collapsed))
s = FitSelf(0, packed=False, auto=True,
            root=DriftingRoot(GUIDE_EXPAND_H + 1, GUIDE_COLLAPSE_H - 1))
FanqieGUI._fit_guidance_to_height(s)
check("F6 窗口在推迟期间变矮了 → 不展开",
      s.collapsed == [], str(s.collapsed))
s = FitSelf(0, packed=True,
            root=DriftingRoot(GUIDE_COLLAPSE_H - 1, GUIDE_COLLAPSE_H - 1))
FanqieGUI._fit_guidance_to_height(s)
check("F6 高度没变 → 照常收", s.collapsed == [(True, False)], str(s.collapsed))
s = FitSelf(0, packed=True, user_set=False,
            root=DriftingRoot(GUIDE_COLLAPSE_H - 1, GUIDE_EXPAND_H + 1))
s._guide_user_set = True
FanqieGUI._fit_guidance_to_height(s)
check("F6 用户刚自己点过 → 不替他决定", s.collapsed == [], str(s.collapsed))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
