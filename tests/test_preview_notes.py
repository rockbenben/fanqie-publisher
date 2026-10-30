# -*- coding: utf-8 -*-
"""章节预览里的两条提醒（无章号文件 / 章号重复）的回归测试（零依赖）。

守的是：这两件事以前完全没有提示，而它们都会「照原样发到平台」——
  · 没有章号的文件（番外/设定/大纲）会被当成一章发布，位置按文件名排，
    可能排到全书最前面，而位置 3 天后就移不回来了。
  · 两个文件都是「第1章」时番茄不会拦，读者会看到两个第 1 章。

提醒只提示、不拦截（发布行为不变），所以断言只针对文字本身。

运行:  python tests/test_preview_notes.py
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fanqie_gui import FanqieGUI  # noqa: E402

PASS = FAIL = 0


class FakeWidget:
    """筛选器要用的那几件东西（见 fanqie_gui._filter_by_chapter_num）。"""

    def __init__(self):
        self.kw = {}
        self.mapped = False

    def configure(self, **kw):
        self.kw.update(kw)

    def winfo_manager(self):
        return "pack" if self.mapped else ""

    def pack(self, **kw):
        self.mapped = True

    def pack_forget(self):
        self.mapped = False


class FakeVar:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class FakeSelf:
    def __init__(self, enabled=True, text="", op="≤"):
        self.cmb_resched_filter_op = FakeWidget()
        self.lbl_resched_filter_info = FakeWidget()
        self._resched_info_row = FakeWidget()
        self.resched_filter_var = FakeVar(enabled)
        self.resched_filter_num_var = FakeVar(text)
        self.resched_filter_op_var = FakeVar(op)

    _parse_chapter_spec = staticmethod(FanqieGUI._parse_chapter_spec)
    _set_resched_filter_info = FanqieGUI._set_resched_filter_info


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def notes(items, mode="schedule"):
    return FanqieGUI._preview_notes(items, mode)


def main():
    print("[R1 正常情况不出提醒]")
    check("R1 全部有章号且不重复 → 无提醒",
          notes([("1", "甲"), ("2", "乙"), ("3", "丙")]) == [])

    print("[R2 无章号文件]")
    n = notes([("1", "甲"), (None, "番外 清明")])
    check("R2 有无章号文件时出提醒", len(n) == 1, str(n))
    check("R2 提醒说清数量与文件名",
          n and "1 个文件没有章号" in n[0] and "番外 清明" in n[0], str(n))
    check("R2 提醒给出可执行的出路",
          n and "按章节号筛选" in n[0], str(n))

    print("[R3 章号重复]")
    n = notes([("1", "甲"), ("1", "乙"), ("2", "丙")])
    check("R3 重复时出提醒", len(n) == 1, str(n))
    check("R3 提醒说清重复处数与章号",
          n and "章号重复 1 处" in n[0] and "第1章" in n[0], str(n))
    n = notes([("1", "甲"), ("1", "乙"), ("2", "丙"), ("2", "丁"), ("3", "戊")])
    check("R3 两处重复时计数正确",
          n and "章号重复 2 处" in n[0], str(n))

    print("[R4 两条同时出现时都在]")
    n = notes([(None, "番外"), ("1", "甲"), ("1", "乙")])
    check("R4 两条提醒都出现", len(n) == 2, str(n))
    check("R4 无章号那条在前", n and "没有章号" in n[0], str(n))

    print("[R5 被筛选掉的章不该再提醒（走真实筛选路径）]")
    # 原来这条只是把一个已经筛好的列表喂给 _preview_notes，等于把 R1 重说
    # 一遍：真正该验的是「筛选开着的时候，无章号的文件会不会被排除」——
    # 如果 kept_set 不再排除 num=None 的章，_preview_notes 这条断言照样过，
    # 但界面上会重复提醒一个已经被筛掉的文件。
    parsed = [("1", "甲", "正文"), (None, "番外 清明", "正文"), ("2", "乙", "正文")]
    kept, active = FanqieGUI._filter_by_chapter_num(
        FakeSelf(True, "1-2"), list(range(len(parsed))),
        key=lambda i: parsed[i][0])
    check("R5 筛选开着 → 无章号那章被排除",
          active and kept == [0, 2], f"kept={kept} active={active}")
    kept_items = [(parsed[i][0], parsed[i][1]) for i in kept]
    check("R5 筛掉之后不再提醒「没有章号」",
          FanqieGUI._preview_notes(kept_items) == [],
          str(FanqieGUI._preview_notes(kept_items)))
    # 筛选没开时它还在，就该提醒
    kept2, active2 = FanqieGUI._filter_by_chapter_num(
        FakeSelf(False, "1-2"), list(range(len(parsed))),
        key=lambda i: parsed[i][0])
    check("R5 筛选没开 → 无章号那章还在",
          not active2 and len(kept2) == 3, f"kept={kept2}")
    check("R5 筛选没开 → 照常提醒",
          len(FanqieGUI._preview_notes(
              [(parsed[i][0], parsed[i][1]) for i in kept2])) == 1)

    print("[R7 无章号的提醒要说出位置后果]")
    n = notes([(None, "番外 清明")])
    check("R7 说出「整体后移」这类位置后果",
          n and ("后移" in n[0] or "排到" in n[0]), str(n))
    n = notes([(None, "番外 清明")], mode="draft")
    check("R7 存草稿不说位置后果（草稿不进目录）",
          n and "后移" not in n[0] and "不影响章节位置" in n[0], str(n))

    print("[R6 存草稿模式的措辞不能写成「发布」]")
    n = notes([(None, "番外 清明")], mode="draft")
    check("R6 存草稿说「存成草稿」", n and "存成草稿" in n[0], str(n))
    check("R6 存草稿不说「按当前顺序发布」",
          n and "按当前顺序发布" not in n[0], str(n))
    n = notes([(None, "番外 清明")], mode="publish")
    check("R6 立即发布仍说「按当前顺序发布」",
          n and "按当前顺序发布" in n[0], str(n))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
