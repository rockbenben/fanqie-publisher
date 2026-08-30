# -*- coding: utf-8 -*-
"""失败清单的同因折叠（零依赖：不需要 playwright/浏览器）。

背景（2026-08-30 实测）：第1385章挂掉后整批中止，剩余 3710 章逐条记成
"前方中止，未处理"，清单于是刷出 3711 行，唯一有信息量的那条真实原因被冲到
屏幕外。折叠只动显示：记账条数、以及末尾那行可直接粘贴补传的章节号压缩
表达式，必须一个字都不变（那才是用户真正要拿走的东西）。

运行:  python tests/test_fail_report_collapse.py
"""
import logging
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


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def capture(fail_list):
    cap = Cap()
    fu.logger.setLevel(logging.INFO)   # 清单走 INFO，默认等级会把它整条吞掉
    fu.logger.addHandler(cap)
    try:
        fu.log_fail_list(fail_list)
    finally:
        fu.logger.removeHandler(cap)
    return cap.lines


def items_of(lines):
    return [ln for ln in lines if ln.lstrip().startswith("- ")]


# 1) 真实事故形状：1 条真失败 + 3710 条"前方中止" → 2 行
fl = [("第1385章 顶尖律师团", "Timeout 15000ms exceeded.")]
fl += [(f"第{n}章 标题{n}", "前方中止，未处理") for n in range(1386, 5096)]
lines = capture(fl)
items = items_of(lines)
check("3711 条折成 2 行", len(items) == 2, f"{len(items)} 行")
check("真实失败原因还在第一行",
      "Timeout 15000ms exceeded." in items[0], items[0])
check("折叠行给出首尾与条数",
      "第1386章" in items[1] and "第5095章" in items[1] and "3710" in items[1],
      items[1])
expr = next(ln for ln in lines if "失败章节号" in ln)
check("可粘贴的补传章节号不受折叠影响", "1385-5095" in expr, expr)

# 2) 短游程照旧逐条打，别把 3 章也糊成一行
short = [(f"第{n}章 t", "上限") for n in (79, 80, 81)] + [("第114章 t", "x")]
check("≤3 章的同因游程保持逐条", len(items_of(capture(short))) == 4)

# 3) 折叠只看"连续"：同一原因被别的原因打断后各自成段
mixed = ([(f"第{n}章 t", "A") for n in range(1, 6)]
         + [("第9章 t", "B")]
         + [(f"第{n}章 t", "A") for n in range(10, 16)])
items3 = items_of(capture(mixed))
check("非连续同因不合并", len(items3) == 3, str(items3))

# 4) 空清单不产出任何行
check("空清单静默", capture([]) == [])

print(f"\n全部通过 ({PASS} 项)")
