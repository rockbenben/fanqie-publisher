# -*- coding: utf-8 -*-
"""修改排期模式：起始日期要跟着「最后一章已发布」走。

回归锁定：date_var 只在 _apply_last_publish 里被设置，而修改排期从不经过它——
框里一直是启动时的「明天」或定时发布模式留下的旧值。修改排期挪的正是待发布章，
起点不能接在队尾（那是待发布章自己），要接在最后一章已发布之后。

运行: python tests/test_reschedule_start_date.py
"""
import inspect
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_gui as fg  # noqa: E402  (导入不创建 Tk 窗口)

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


ROWS = [
    {"title": "第1章", "status": "已发布", "date": "2026-08-30", "time": "08:00"},
    {"title": "第2章", "status": "已发布", "date": "2026-09-01", "time": "20:00"},
    {"title": "第3章", "status": "已发布", "date": "2026-09-01", "time": "08:00"},
    {"title": "第4章", "status": "待发布", "date": "2026-09-20", "time": "08:00"},
    {"title": "草稿", "status": "未知状态7", "date": "2026-09-30", "time": "08:00"},
    {"title": "无日期", "status": "已发布", "date": None, "time": None},
]
lp = fg.FanqieGUI._last_published(ROWS)

print("== 最后一章已发布 ==")
check("取已发布里最晚的一章", lp == {"date": "2026-09-01", "time": "20:00", "chapter": "第2章"})
check("待发布（队尾）不算", lp["date"] != "2026-09-20")
check("全是待发布 → None", fg.FanqieGUI._last_published(ROWS[3:5]) is None)
check("空表 → None", fg.FanqieGUI._last_published([]) is None)

print("== 修改排期经 _on_platform_chapters_fetched 设起始日期 ==")
src = inspect.getsource(fg.FanqieGUI._on_platform_chapters_fetched)
check("reschedule 用 _last_published", 'if mode == "reschedule":' in src and "self._last_published(chapters)" in src)
check("只有 edit 才停在「已索引」", 'if mode == "edit" or not lp:' in src)
check("标签写「最后发布」", '"最后发布" if mode == "reschedule"' in src)

print("== 合并所有卷时队尾取最晚的一卷 ==")
src = inspect.getsource(fg.FanqieGUI._fetch_platform_chapters_for_edit)
check("不再只留第一卷的 last_pub", "if lp and not last_pub:" not in src)
check("按 date time 取最大", "if lp and (not last_pub or" in src)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
