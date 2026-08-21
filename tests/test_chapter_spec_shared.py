# -*- coding: utf-8 -*-
"""章节号筛选：CLI 与 GUI 必须同一套解析。

断层曾经是: CLI 跑批失败会在日志里教用户「可直接粘贴到「按章节号筛选」补传」，
但 CLI 自己没有这个参数，只能去开 GUI。现在 --chapters 与 GUI 共用同一个解析器。

运行: python tests/test_chapter_spec_shared.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fanqie_upload import (  # noqa: E402
    filter_by_chapter_spec, parse_chapter_spec, compress_chapter_nums,
)

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


ITEMS = [(n, f"第{n}章") for n in range(1, 21)]
K = lambda x: x[0]          # noqa: E731


def sel(spec):
    return [x[0] for x in filter_by_chapter_spec(ITEMS, spec, key=K)[0]]


print("== 各种写法 ==")
for spec, want in (("5-10", [5, 6, 7, 8, 9, 10]),
                   ("1,3,5-7", [1, 3, 5, 6, 7]),
                   ("≥18", [18, 19, 20]), (">=18", [18, 19, 20]),
                   ("≤3", [1, 2, 3]), ("<=3", [1, 2, 3]),
                   ("7", [7]),
                   ("3、7~9", [3, 7, 8, 9]),      # 顿号 + 波浪号
                   ("１，２", [1, 2])):            # 全角数字与逗号
    check(f"{spec} -> {want}", sel(spec) == want)

print("== 非法输入必须报错，不能静默放行 ==")
for bad in ("abc", "5-", "-", "1,,x"):
    try:
        filter_by_chapter_spec(ITEMS, bad, key=K)
        check(f"{bad} 报错", False)
    except ValueError:
        check(f"{bad} 报错", True)
check("空值不生效", filter_by_chapter_spec(ITEMS, "", key=K) == (ITEMS, False))

print("== 补传闭环：压缩出来的清单能原样喂回去 ==")
failed_nums = [3, 4, 5, 9, 12, 13, 14]
spec = compress_chapter_nums(failed_nums)
check(f"压缩成 {spec}", spec == "3-5,9,12-14")
check("喂回筛选能选中原来那批", sel(spec) == failed_nums)

print("== CLI 与 GUI 用同一个解析器 ==")
GUI = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")
check("GUI 不再自带解析实现",
      "def _parse_chapter_spec(raw):" not in GUI)
check("GUI 转发到共用实现", "_parse_chapter_spec = staticmethod(parse_chapter_spec)" in GUI)
_up = subprocess.run([sys.executable, str(ROOT / "fanqie_upload.py"), "upload", "-h"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace", cwd=str(ROOT)).stdout or ""
check("CLI 有 --chapters", "--chapters" in _up)
check("解析器本身可直接调用", parse_chapter_spec("1-3") == [(1, 3)])

print()
print("== 命中判定只能有一份（GUI 不得再自带一套） ==")
# 曾经的断层: GUI 自己内联了区间命中 + ≤/≥ 阈值，CLI 用 filter_by_chapter_spec。
# 两份实现一旦漂移，同一个表达式在两个入口筛出不同的章集 —— 而这正是补传路径，
# 筛少一章就是漏一章。现在 GUI 只负责 UI 状态，判定全部转发。
check("GUI 调用共用筛选", "filter_by_chapter_spec(items, spec, key=key)" in GUI)
check("GUI 不再内联区间命中", "any(lo <= v <= hi for lo, hi in intervals)" not in GUI)
check("GUI 不再自带 _as_int", "def _as_int(" not in GUI)

print()
print("== 日期筛选：解析与边界两侧必须一致 ==")
from fanqie_upload import parse_time_spec  # noqa: E402
_cut = parse_time_spec("2026-08-20")
check("只给日期按当天 00:00 算", parse_time_spec("2026-08-20 00:00") == _cut)
check("非法日期返回 None", parse_time_spec("20260820") is None)
check("GUI 调用共用日期解析", "parse_time_spec(raw)" in GUI)
check("GUI 不再内联 strptime 循环",
      'for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d")' not in GUI)

# 边界: mtime 正好等于 cutoff 的文件，两侧必须落在同一边。
# 曾经 CLI 用 > (排除)、GUI「晚于」用 >= (包含)，同一串日期差一个文件。
_FU = (ROOT / "fanqie_upload.py").read_text(encoding="utf-8")
check("CLI 边界含等于（与 GUI「晚于」一致）",
      "(f.stat().st_mtime >= _ts) == _newer" in _FU)
check("GUI 边界含等于", "(mt.timestamp() >= cutoff_ts) == (op != " in GUI)

print()
print("== 自动接续必须读真实缓存字段（不是不存在的那两个）==")
# _EXTRACT_ALL_JS 产出的行是 {title, chapterNum, editUrl, status, date, time,
# rowIndex}；缓存里**没有** display_status / timer_time。GUI 曾直接
# .get("display_status", 1) / .get("timer_time", 0)，等于把每章都当「已发布、
# 无定时」，队列末尾恒等于今天、起始日期恒为明天 —— 队列已排到 9 月底时勾自动
# 接续，新章会全堆到明天起的已占用日期上，正是这个功能要防的事。
check("GUI 从中文状态判待发布", '"待发布" in status' in GUI)
check("GUI 从 date/time 换算 timer_time",
      'c.get("date")' in GUI and "timestamp()" in GUI)
check("GUI 不再默认 display_status",
      'c.get("display_status", 1)' not in GUI)
check("GUI 不再默认 timer_time", 'c.get("timer_time", 0)' not in GUI)
# 自动接续的筛选结果不许写回 self.*（用户点「否」会永久裁剪预览）
check("自动接续只用局部子集", "autocont_subset" in GUI)
check("不再写回 self.parsed_chapters",
      "self.parsed_chapters, self.files = parsed_new" not in GUI)

print()
print("== 多卷位置公式只能有一份 ==")
# demo 里那两条多卷断言测的是 global_position，但生产路径曾内联同一个公式，
# 等于断言盖不住真正跑的代码。卷偏移算错 = 全书错位，代价极大。
check("fetch_chapter_items 走 global_position",
      'it["pos"] = global_position(it["index"], {ordinal: acc})' in _FU)
check("生产路径不再内联公式",
      'acc + it["index"] % VOLUME_INDEX_STRIDE' not in _FU)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
