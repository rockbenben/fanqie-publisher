# -*- coding: utf-8 -*-
"""每日字数上限"中止整批 + 记录剩余"行为测试（零依赖：不需要 playwright/浏览器）。

背景（2026-06-27 用户指示）：以前撞到平台"当日发布字数上限"toast 时，上层只
跳过本章、继续发后续章节——但实践中后续章节多半重复撞限或触发别的拦截，徒增
额外错误。改为：捕获到上限即中止整批，把本章与所有剩余未处理章节如实记入失败
清单（_log_fail_list 会压成章节号区间，可直接粘贴明天补传），不静默丢弃。

本测试锁住承担"记录剩余"的纯函数 _record_unprocessed，以及它与
_compress_chapter_nums 的补传往返。

运行:  python tests/test_daily_limit_abort.py
"""
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    PASS += 1
    print(f"  PASS {name}")


# 1) 基本：剩余章节按 (号, 标题) 追加，原因统一为"未处理"，返回条数
fl = []
n = fu._record_unprocessed(fl, [(80, "第八十章标题"), (81, "甲"), (82, "乙")])
check("R1 返回条数=3", n == 3, n)
check("R1 全部记入清单", len(fl) == 3, fl)
check("R1 标签带第N章", fl[0][0] == "第80章 第八十章标题", fl[0])
check("R1 原因统一未处理", all(r == "每日字数上限，未处理" for _, r in fl),
      [r for _, r in fl])

# 2) 章节号缺失（None/空）时不硬塞"第章"，标签退化为纯标题
fl = []
fu._record_unprocessed(fl, [(None, "无号章"), ("", "也无号")])
check("R2 None 不产出第章", fl[0][0] == "无号章", fl[0])
check("R2 空串不产出第章", fl[1][0] == "也无号", fl[1])

# 3) 空剩余：不追加、返回 0（末章撞限时 i+1 越界为空）
fl = [("第1章 已发", "之前的真失败")]
n = fu._record_unprocessed(fl, [])
check("R3 空剩余返回0", n == 0, n)
check("R3 不动既有清单", len(fl) == 1, fl)

# 4) 生成器入参（调用点都传生成器表达式）也能正常消费
fl = []
matched = [(0, {}, 90, "丙", "正文"), (1, {}, 91, "丁", "正文")]
n = fu._record_unprocessed(fl, ((m[2], m[3]) for m in matched))
check("R4 生成器消费", n == 2 and fl[1][0] == "第91章 丁", fl)

# 4b) 自定义 reason（连续失败熔断中止复用同一函数，原因文案不同）
fl = []
fu._record_unprocessed(fl, [(5, "戊")], reason="流程异常中止，未处理")
check("R4b 自定义原因", fl[0] == ("第5章 戊", "流程异常中止，未处理"), fl)

# 4c) 熔断中止计数对账：成功+失败必须等于总数（修复前 rest 不计入 → 漏账）
#     模拟 50 章，前 3 章连续失败触发熔断，剩 47 章未处理。
success, failed = 0, 0
fl = []
files_total = 50
broke_at = 2  # i=0,1,2 三章失败后熔断（consec_fail 第 3 次）
for _ in range(broke_at + 1):
    failed += 1  # 每章本身计失败
failed += fu._record_unprocessed(
    fl, ((c, f"第{c}章") for c in range(broke_at + 1, files_total)),
    reason="流程异常中止，未处理")
check("R4c 熔断后成功+失败=总数", success + failed == files_total,
      f"{success}+{failed}!={files_total}")
check("R4c 剩余全部记入清单", len(fl) == files_total - (broke_at + 1), len(fl))

# 5) 补传往返：撞限章 + 剩余未处理 → _log_fail_list 的章节号压缩可直接粘贴
#    模拟第 79 章撞限，80-85 未处理；期望补传号 "79-85"
fl = [("第79章 撞限章", "当日发布字数已达上限: 已到达当日发布字数上限")]
fu._record_unprocessed(fl, [(c, f"第{c}章") for c in range(80, 86)])
nums = []
for label, _ in fl:
    m = re.match(r"第(\d+)章", label)
    if m:
        nums.append(int(m.group(1)))
check("R5 补传号连续覆盖撞限章+剩余",
      fu._compress_chapter_nums(nums) == "79-85",
      fu._compress_chapter_nums(nums))

print(f"\nALL PASSED ({PASS} 断言)")
