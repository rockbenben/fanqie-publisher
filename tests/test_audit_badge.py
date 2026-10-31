# -*- coding: utf-8 -*-
"""「检查缺口 ⚠」徽标的回归测试（零依赖：不建 Tk 窗口）。

守的是三件事：

1) 徽标反映**最近一次**检测的结果。
   以前只增不减：查有卡队列 → 挂 ⚠；用户改完那一章再查，没卡队列了 →
   ⚠ 还挂着，悬停提示还在说「后面 413 章卡在审核中」——那句已经是假的。

2) 徽标按作品隔离。
   卡队列是「这本书的这一章卡住了」，位置和章号只对那本书有意义。换一部
   作品还挂着上一本的 ⚠，用户会以为新书也卡住了。

3) 悬停提示说得出是**哪本书**查出来的。

运行:  python tests/test_audit_badge.py
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fanqie_gui import FanqieGUI, BTN_AUDIT  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class Combobox:
    def __init__(self):
        self.idx = -1

    def current(self, i=None):
        if i is None:
            return self.idx
        self.idx = i
        return i


class Btn:
    def __init__(self):
        self.kw = {}

    def configure(self, **kw):
        self.kw.update(kw)


class Stub:
    """只提供 _audit_badge / _note_review_block / _audit_tip 需要的那些东西。

    徽标那几个方法直接用真实现：测的就是「界面上的 ⚠ 什么时候在」，
    自己重写一遍就测不到真行为了。
    """

    _audit_badge = FanqieGUI._audit_badge
    _audit_tip = FanqieGUI._audit_tip
    _note_review_block = FanqieGUI._note_review_block
    _refresh_audit_badge = FanqieGUI._refresh_audit_badge
    _review_blocks = FanqieGUI._review_blocks
    _book_name = FanqieGUI._book_name
    _current_book_id = FanqieGUI._current_book_id

    def __init__(self, books=None, selected=-1):
        self._gui_state = {}
        self.saved = 0
        self.books = books or []
        self.cmb_book = Combobox()
        self.cmb_book.current(selected)
        self.btn_audit = Btn()

    def _save_gui_state(self):
        self.saved += 1


def badge_text(s):
    return s.btn_audit.kw.get("text", "")


BLK = {"index": 4783, "title": "第4783章 不是挨着的时候",
       "status": 7, "blocked": 413, "blocked_to": 5196}

BOOKS = [{"bookId": "111", "name": "诸天都市剧"},
         {"bookId": "222", "name": "另一本书"}]


print("[B1 查出卡队列 → 挂上徽标]")
# 徽标是「这本书的这一章卡住了」，所以要先选中那本书才该显示
s = Stub(BOOKS, selected=0)
FanqieGUI._note_review_block(s, BLK, "111")
check("B1 按钮带 ⚠", badge_text(s) == f"{BTN_AUDIT} ⚠", badge_text(s))
check("B1 徽标内容在", FanqieGUI._audit_badge(s) is not None)
check("B1 写盘了", s.saved == 1, str(s.saved))

print("[B1b 没选作品时不挂徽标]")
s_nb = Stub(BOOKS, selected=-1)          # 没选中任何作品
FanqieGUI._note_review_block(s_nb, BLK, "111")
check("B1b 没选作品 → 不显示 ⚠（点了也只会说「未选择作品」）",
      badge_text(s_nb) == BTN_AUDIT, badge_text(s_nb))
check("B1b 没选作品 → 徽标取不到", FanqieGUI._audit_badge(s_nb) is None)

print("[B2 再查没有卡队列 → 徽标要撤掉]")
FanqieGUI._note_review_block(s, None, "111")
check("B2 按钮回到「检查缺口」", badge_text(s) == BTN_AUDIT, badge_text(s))
check("B2 徽标没了", FanqieGUI._audit_badge(s) is None)
check("B2 状态文件里也清了", "review_block" not in s._gui_state,
      str(s._gui_state))
check("B2 撤徽标也写盘了", s.saved == 2, str(s.saved))

print("[B3 换书 → 不继承上一本的徽标]")
s = Stub(BOOKS, selected=0)
FanqieGUI._note_review_block(s, BLK, "111")
check("B3 本书时挂上", badge_text(s) == f"{BTN_AUDIT} ⚠", badge_text(s))
s.cmb_book.current(1)                      # 切到另一本书
FanqieGUI._refresh_audit_badge(s)
check("B3 换书后徽标撤掉", badge_text(s) == BTN_AUDIT, badge_text(s))
check("B3 换书后徽标取不到", FanqieGUI._audit_badge(s) is None)
s.cmb_book.current(0)                      # 切回来
FanqieGUI._refresh_audit_badge(s)
check("B3 切回来徽标还在", badge_text(s) == f"{BTN_AUDIT} ⚠", badge_text(s))

print("[B4 老数据（没有 book_id）照旧显示]")
s = Stub(BOOKS, selected=1)
s._gui_state["review_block"] = dict(BLK, at="2026-10-29T20:57:00")
FanqieGUI._refresh_audit_badge(s)
check("B4 判不出属于哪本 → 先当真话说", badge_text(s) == f"{BTN_AUDIT} ⚠",
      badge_text(s))

print("[B5 悬停提示说清是哪本书]")
s = Stub(BOOKS, selected=0)
FanqieGUI._note_review_block(s, BLK, "111")
tip = FanqieGUI._audit_tip(s)
check("B5 提示里有书名", "诸天都市剧" in tip, tip)
check("B5 提示里有位置和章数",
      "4783" in tip and "413" in tip, tip)
check("B5 提示说的是「最近一次检测」", "最近一次检测" in tip, tip)
s2 = Stub(BOOKS, selected=0)
tip2 = FanqieGUI._audit_tip(s2)
check("B5 没有卡队列时提示是常规说明", "未公开段可自动重排" in tip2, tip2)

print("[B6 体检结果 → 徽标的接线]")
# 前面 B1–B5 都是直接调 _note_review_block，手工把 book_id 喂进去。
# 真正决定「这条结论属于哪本书」的是接线本身：_render_audit 把
# rep["review_block"] 和**被体检的那本书**一起传下去。这段接线以前没人
# 验过——把 book_id 漏掉（存成空串），按书隔离就退化成「每本书都显示」，
# 而上面每一条断言照样全绿。


class RenderSelf(Stub):
    _render_audit = FanqieGUI._render_audit
    _compress_nums = staticmethod(FanqieGUI._compress_nums)
    _reader_view_around = FanqieGUI._reader_view_around

    def __init__(self, books=None, selected=-1):
        super().__init__(books, selected)
        self._last_audit = None
        self.preview = ""
        self.notices = []

    def _set_preview(self, text):
        self.preview = text

    def _notify(self, level, title, msg=""):
        self.notices.append((level, title))


def _items(n=6, ds=1, reason=""):
    out = []
    for i in range(1, n + 1):
        out.append({"index": i, "pos": i, "title": f"第{i}章 甲",
                    "display_status": ds, "create_time": 0, "timer_time": 0,
                    "item_id": f"i{i}", "cant_modify_reason": reason})
    return out


def _blocked_items():
    """第 5 章被拒、后面 15 章审核中 → 平台侧认得出「审核卡队列」。"""
    out = _items(4)
    out.append({"index": 5, "pos": 5, "title": "第5章 被拒", "display_status": 7,
                "create_time": 0, "timer_time": 0, "item_id": "i5",
                "cant_modify_reason": ""})
    for p in range(6, 21):
        out.append({"index": p, "pos": p, "title": f"第{p}章", "display_status": 4,
                    "create_time": 0, "timer_time": 0, "item_id": f"i{p}",
                    "cant_modify_reason": "审核中暂不支持修改"})
    return out


rs = RenderSelf(BOOKS, selected=1)      # 当前选中的是另一本书
FanqieGUI._render_audit(rs, _blocked_items(), None, "111")
check("B6 结论记到被体检的那本书上",
      (rs._gui_state.get("review_blocks") or {}).get("111", {}).get("book_id") == "111",
      str(rs._gui_state.get("review_blocks")))
check("B6 当前选中的是另一本 → 徽标不显示",
      FanqieGUI._audit_badge(rs) is None)
check("B6 体检报告里写了卡队列",
      "审核卡住了" in rs.preview, rs.preview[:200])

rs2 = RenderSelf(BOOKS, selected=0)
FanqieGUI._render_audit(rs2, _items(), None, "111")
check("B6 体检没查出卡队列 → 不留徽标",
      "111" not in (rs2._gui_state.get("review_blocks") or {}),
      str(rs2._gui_state.get("review_blocks")))
check("B6 体检没查出卡队列 → 当前这本也不显示",
      FanqieGUI._audit_badge(rs2) is None)

print("[B7 给另一本书体检，不该抹掉这一本的警告]")
# 结论**每本书一份**：两个作品都卡着的时候，给 A 体检不能把 B 的警告也抹掉
# ——那正是用户唯一能看见「这一本卡住了」的提示。
s7 = Stub(BOOKS, selected=0)
FanqieGUI._note_review_block(s7, BLK, "111")
check("B7 先存下 111 的结论",
      (s7._gui_state.get("review_blocks") or {}).get("111", {}).get("book_id") == "111",
      str(s7._gui_state.get("review_blocks")))
FanqieGUI._note_review_block(s7, None, "222")       # 体检另一本，没卡
check("B7 体检 222 之后，111 的结论还在",
      "111" in (s7._gui_state.get("review_blocks") or {}),
      str(s7._gui_state.get("review_blocks")))
FanqieGUI._note_review_block(s7, BLK, "222")       # 另一本也卡着
check("B7 222 也查出卡队列时，111 的结论没被抹掉",
      "111" in (s7._gui_state.get("review_blocks") or {}),
      str(s7._gui_state.get("review_blocks")))
FanqieGUI._note_review_block(s7, None, "111")       # 体检 111，仍然没卡
check("B7 体检 111 之后，它的结论被清掉",
      "111" not in (s7._gui_state.get("review_blocks") or {}),
      str(s7._gui_state.get("review_blocks")))
check("B7 222 的结论还在",
      "222" in (s7._gui_state.get("review_blocks") or {}),
      str(s7._gui_state.get("review_blocks")))

print("[B8 状态文件被写坏时不能崩]")
# 徽标是在建界面的时候算的：状态文件里 review_block 是个字符串（手改过、
# 或别的版本写歪了），下标 .get() 会 AttributeError —— 程序连窗口都开
# 不出来，界面上什么提示都没有。
s8 = Stub(BOOKS, selected=0)
s8._gui_state["review_block"] = "oops"
try:
    FanqieGUI._refresh_audit_badge(s8)
    check("B8 坏数据 → 不崩，徽标当没有", badge_text(s8) == BTN_AUDIT,
          badge_text(s8))
except Exception as e:
    check("B8 坏数据 → 不崩，徽标当没有", False, f"{type(e).__name__}: {e}")

print("[B9 老格式的那一条要能被清掉]")
# 加「每本书一份」之前写下的单条 review_block（没有 book_id）：它判不出属于
# 哪本书，就会在每一本书上显示一个假警告——而且没人清得掉它（只有查出卡
# 队列才会写盘）。下一次体检必须把它清掉。
s9 = Stub(BOOKS, selected=0)
s9._gui_state["review_block"] = dict(BLK, at="2026-10-29T20:57:00")
FanqieGUI._refresh_audit_badge(s9)
check("B9 老数据先照旧显示", badge_text(s9) == f"{BTN_AUDIT} ⚠", badge_text(s9))
FanqieGUI._note_review_block(s9, None, "111")
check("B9 体检一次之后，老数据被清掉",
      "review_block" not in s9._gui_state, str(s9._gui_state))
check("B9 清掉之后徽标不再显示", badge_text(s9) == BTN_AUDIT, badge_text(s9))

print("[B10 报告不许把位置正确的章报成错]")
# 2026-10-31 实测（用户反馈「位置很多明明是正确的，你为什么报错？」）：
# 位置 2 卡着第5290章，它后面每一章都比它小。按「章号 < 前面最大章号」判倒挂，
# 后面三章全进「顺序断裂」清单——可它们位置明明是对的（第3章在3、第4章在4…）。
# 报告照抄那份清单，用户看到的就是一堆假错。


def _overshoot_items():
    out = []
    for p in (1, 3, 4, 5):
        out.append({"index": p, "pos": p, "title": f"第{p}章 甲",
                    "display_status": 1, "create_time": 0, "timer_time": 0,
                    "item_id": f"i{p}", "cant_modify_reason": ""})
    out.append({"index": 2, "pos": 2, "title": "第5290章 甲",
                "display_status": 1, "create_time": 0, "timer_time": 0,
                "item_id": "i2", "cant_modify_reason": ""})
    return out


rs10 = RenderSelf(BOOKS, selected=0)
FanqieGUI._render_audit(rs10, _overshoot_items(), None, "111")
check("B10 C 段只数位置不对的那一章",
      "C 已公开段位置与章号不符：1 章" in rs10.preview, rs10.preview)
check("B10 B 段不再叫用户去移动位置正确的章",
      "B 已公开、位置与章号不符、发布 3 天内（App 里还能移动位置）：0 章"
      in rs10.preview, rs10.preview)
check("B10 报告里没有「顺序断裂的是 N 章」那种清单",
      "顺序断裂的是" not in rs10.preview, rs10.preview)
check("B10 顺序断点单独说明，并说清被隔断的章位置是对的",
      "顺序断在位置 3" in rs10.preview
      and "它们自己的位置大多是对的" in rs10.preview, rs10.preview)
check("B10 点名要改的是哪一章（位置 + 现在装的是什么）",
      "位置2 装第5290章" in rs10.preview, rs10.preview)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
