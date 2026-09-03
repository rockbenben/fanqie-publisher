# -*- coding: utf-8 -*-
"""修改排期：排期没变的章不改；失败/跳过后残留对话框必须关掉再碰下一章。

回归锁定（2026-09-04 第3079章）：撞"每日上限"toast 走 DailyLimitReached 分支
直接 break，「修改定时」对话框留在页上——下一页点击被 arco-modal-wrapper 挡住
超时 30s，整批以"修改排期异常"收场；若不在页尾，下一章的日期会填进本章对话框。

运行: python tests/test_reschedule_skip_unchanged.py
"""
import asyncio
import inspect
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


print("== build_schedule_map: 排期未变的章不进 map ==")
rows = [
    {"title": "第1章", "date": "2027-09-20", "time": "07:00"},
    {"title": "第2章", "date": "2027-09-20", "time": "12:00"},
    {"title": "第3章", "date": None, "time": None},
    {"title": "第4章", "date": "2027-09-21", "time": "12:00"},
    {"title": "第4章", "date": "2027-09-22", "time": "07:00"},
]
sched = [("2027-09-20", "07:00"), ("2027-09-20", "12:01"), ("2027-09-21", "07:00"),
         ("2027-09-21", "12:00"), ("2027-09-22", "07:00")]
smap, dups, unchanged, order = fu.build_schedule_map(rows, sched)
check("完全一致的章被跳过", "第1章" not in smap and "第4章" not in smap and unchanged == 3)
check("只差一分钟的第2章仍要改", smap.get("第2章") == ("2027-09-20", "12:01"))
check("没有日期的章照改", smap.get("第3章") == ("2027-09-21", "07:00"))
check("map 里只剩要改的章", len(smap) == 2)
check("同名照报（哪怕两章都被跳过）", dups == ["第4章"])
check("全部未变 → 空 map", fu.build_schedule_map(rows[:1], sched[:1]) == ({}, [], 1, "desc"))
check("chapters 空 → 空 map", fu.build_schedule_map([], []) == ({}, [], 0, "desc"))

print("== 处理顺序：整体提前→旧→新(asc)，推后/未变→新→旧(desc) ==")
old = [{"title": f"第{i}章", "date": "2027-09-21", "time": "07:00"} for i in range(3)]
adv = [("2027-09-20", "07:00")] * 3
check("整体提前 → asc", fu.build_schedule_map(old, adv)[3] == "asc")
check("整体推后 → desc", fu.build_schedule_map(old, [("2027-09-22", "07:00")] * 3)[3] == "desc")
check("同日提前时刻也算提前", fu.build_schedule_map(old, [("2027-09-21", "06:59")] * 3)[3] == "asc")
check("有提前有推后 → 顾多数", fu.build_schedule_map(old, adv[:2] + [("2027-09-22", "07:00")])[3] == "asc")
check("行里没日期不计方向", fu.build_schedule_map([{"title": "x"}], adv[:1])[3] == "desc")

print("== _goto_last_page: 单页不点；有页码点最大页码；无页码一路下一页 ==")


class _FakePager:
    """假分页条：nums=有无页码项、active=最大页码已选中、next_pages=还能点几次下一页。"""

    def __init__(self, nums, active, next_pages):
        self.nums, self.active, self.next_left = nums, active, next_pages
        self.clicks, self.cell = [], "row-a"

    def locator(self, sel, has_text=None):
        page = self

        class _L:
            @property
            def last(self_):
                return self_

            async def count(self_):
                if has_text is not None:
                    return 1 if page.nums else 0
                return 1 if page.next_left > 0 else 0

            async def get_attribute(self_, name):
                return ("arco-pagination-item arco-pagination-item-active"
                        if page.active else "arco-pagination-item")

            async def click(self_):
                page.clicks.append("num" if has_text is not None else "next")
                if has_text is None:
                    page.next_left -= 1
                page.cell += "x"      # 表格首格变了
        return _L()

    async def evaluate(self, js):
        return self.cell

    async def wait_for_timeout(self, ms):
        pass


pg = _FakePager(nums=False, active=False, next_pages=0)
asyncio.run(fu._goto_last_page(pg))
check("单页表：一次都不点", pg.clicks == [])
pg = _FakePager(nums=True, active=True, next_pages=0)
asyncio.run(fu._goto_last_page(pg))
check("已在最后一页：不点", pg.clicks == [])
pg = _FakePager(nums=True, active=False, next_pages=0)
asyncio.run(fu._goto_last_page(pg))
check("有页码：点一次最大页码就到", pg.clicks == ["num"])
pg = _FakePager(nums=False, active=False, next_pages=3)
asyncio.run(fu._goto_last_page(pg))
check("无页码：一路下一页到 disabled", pg.clicks == ["next"] * 3)

print("== _dismiss_resched_dialog: 只在对话框开着时按 Escape，并确认关了 ==")


class _Btn:
    def __init__(self, page, closes_after):
        self.page, self.closes_after = page, closes_after

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self.page.escapes < self.closes_after

    async def wait_for(self, state, timeout):
        assert state == "hidden"
        if self.page.escapes < self.closes_after:
            raise TimeoutError("still open")


class _Page:
    def __init__(self, closes_after):
        self.escapes = 0
        self._btn = _Btn(self, closes_after)
        page = self

        class _KB:
            async def press(self_, key):
                assert key == "Escape"
                page.escapes += 1
        self.keyboard = _KB()

    def locator(self, sel, has_text=None):
        assert has_text == "确认修改"
        return self._btn


run = asyncio.run
p = _Page(closes_after=0)
check("没有对话框 → 不按 Escape 直接 True",
      run(fu._dismiss_resched_dialog(p)) is True and p.escapes == 0)
p = _Page(closes_after=1)
check("按一次就关 → True", run(fu._dismiss_resched_dialog(p)) is True and p.escapes == 1)
p = _Page(closes_after=99)
check("怎么按都不关 → 有限次后 False",
      run(fu._dismiss_resched_dialog(p)) is False and p.escapes == 3)

print("== _reschedule_current_volume: 失败/跳过后先关对话框再碰下一章 ==")
src = inspect.getsource(fu._reschedule_current_volume)
guard = "if not ok and not await _dismiss_resched_dialog(page):"
check("裸按 Escape 已删", 'press("Escape")' not in src)
check("非成功路径（含 DailyLimitReached 的 break）统一走 _dismiss_resched_dialog", guard in src)
check("关不掉就停止扫描（aborted=True），本章留在 remaining 计入未处理",
      guard in src and src.index(guard) < src.index("            del remaining[title]")
      and "return success, failed, True" in src)
outer = inspect.getsource(fu.reschedule_on_manage_page)
check("调用方：aborted 就不再换卷（换卷是 JS 点击，弹窗挡不住它）", "if aborted:" in outer)
check("调用方：扫描异常也走未处理清单而不是裸抛", "except Exception as e:" in outer and "扫描中断" in outer)

print("== 旧→新顺序：先跳最后一页、点「上一页」、页内行倒序 ==")
check("asc 先跳最后一页", "_goto_last_page(page)" in src)
check("asc 用「上一页」", "arco-pagination-item-prev" in src)
check("asc 页内行倒序", "reversed(matched_on_page)" in src)
check("调用方把 order 传给每卷", "order=order" in outer)
check("多卷时卷序也跟方向：desc 先动最新的卷", "list(reversed(volume_texts))" in outer)
gui_src2 = Path(__file__).resolve().parent.parent.joinpath("fanqie_gui.py").read_text(encoding="utf-8")
check("CLI/GUI 都把 order 传给 reschedule_on_manage_page",
      "order=order" in inspect.getsource(fu.cmd_reschedule_cli) and "order=order," in gui_src2)

print("== CLI / GUI 都经 build_schedule_map 建 map ==")
check("CLI", "build_schedule_map(pending, schedule)" in inspect.getsource(fu.cmd_reschedule_cli))
gui_src = Path(__file__).resolve().parent.parent.joinpath("fanqie_gui.py").read_text(encoding="utf-8")
check("GUI", "build_schedule_map(" in gui_src and "count = len(schedule_map)" in gui_src)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
