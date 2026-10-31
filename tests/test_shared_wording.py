# -*- coding: utf-8 -*-
"""同一份措辞只能写一份：核心里那几处告警/摘要，GUI 与工具都得用同一份。

起因：2026-10-31 用户反馈体检报告「三段零值」那类问题后，顺手扫了全仓一遍
跨文件逐字重复的中文句子（28 处）。真正要紧的是**同一个警报写了好几份**：
「审核卡住了」「平台中段还缺」「平台未公开段排期有错」「修改完成! 成功/失败」
——这些是用户判断「出没出事、要不要动手」的唯一依据，漂一次就是两个入口
报不同的账。子命令的 -h 说明同样漂过（--daily 一边说「写日志」、一边说
「输出写 tools/remap/logs/」）。

这里钉住两条：
  1) 那些句子只在核心里出现一次，GUI 与工具都引用核心那份；
  2) 主 CLI 与独立工具对同一个选项的 -h 说明逐字一致（用户对照着看要一样）。

运行:  python tests/test_shared_wording.py
"""
import re
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fanqie_upload as fu  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def nospace(s):
    return re.sub(r"\s+", "", s)


# ---- ① 共享告警/摘要只在核心里定义 ----
print("== 共享告警/摘要只有一份 ==")
# (标签, 渲染出来的样子, 核心里应当出现的源码片段)
# 带数字的句子在源码里是 f-string 片段，所以第三项单独给「源码里长什么样」。
SHARED = [
    ("审核卡住了（头一行）", fu.review_block_headline(
        {"index": 1, "title": "t", "blocked": 2, "blocked_to": 3}),
     "⚠ 审核卡住了：位置 {blk['index']}"),
    ("被卡的是哪一章", fu.review_block_where(
        {"index": 1, "title": "t", "blocked": 2}),
     "被卡的那章：位置"),
    ("它不会自己过审", fu.review_block_never_passes(
        {"index": 1, "title": "t", "blocked": 2}),
     "它不会自己过审：平台按位置顺序审"),
    ("顺序断在位置", fu.order_break_line({"at": 3, "affected": 5}),
     "顺序断在位置 {brk['at']}"),
    ("平台中段还缺", fu.mid_gap_headline([7, 9]),
     "⚠ 平台中段还缺 {len(gaps)} 章"),
    ("平台未公开段排期有错", fu.schedule_order_warning(2, 1),
     "⚠ 平台未公开段排期有错：{inverted} 章"),
    ("体检结论", fu.AUDIT_CLEAN_LINE, "✓ 没查出问题：位置与章号全部对得上"),
    ("A 段无错位", fu.AUDIT_SEG_CLEAN["A"], "A 未公开段：位置与章号都对得上"),
    ("B 段无错位", fu.AUDIT_SEG_CLEAN["B"], "B 已公开段：没有还能在 3 天内"),
    ("C 段无错位", fu.AUDIT_SEG_CLEAN["C"], "C 已公开段：位置与章号都对得上"),
    ("排期没问题", fu.SCHEDULE_ORDER_CLEAN_LINE, "排期顺序没问题：未公开段里没有"),
    ("改写批末的账", fu.batch_summary_line(3, 1), "修改完成! 成功: {success}"),
]
CORE = read("fanqie_upload.py")
GUI = read("fanqie_gui.py")
TOOLS = {"tools/remap/remap.py": read("tools/remap/remap.py"),
         "tools/keep_ahead/keep_ahead.py": read("tools/keep_ahead/keep_ahead.py"),
         "tools/clean_drafts/clean_drafts.py": read("tools/clean_drafts/clean_drafts.py")}

for label, text, frag in SHARED:
    check(f"「{label}」在核心里", frag in CORE, frag[:40])
    # GUI 与工具里不该再出现同一句（要么引用核心，要么根本没有这个概念）
    for fname, fsrc in [("fanqie_gui.py", GUI)] + list(TOOLS.items()):
        check(f"「{label}」不在 {fname} 里另写一遍",
              nospace(text) not in nospace(fsrc), f"{fname}: {text[:40]}")

print("== GUI 与工具确实引用了核心那份 ==")
check("GUI 用了 review_block_headline", "review_block_headline(blk)" in GUI)
check("GUI 用了 review_block_never_passes",
      "review_block_never_passes(blk)" in GUI)
check("GUI 用了 batch_summary_line", "batch_summary_line(" in GUI)
check("GUI 用了 AUDIT_SEG_CLEAN", "AUDIT_SEG_CLEAN[" in GUI)
check("GUI 用了 AUDIT_CLEAN_LINE", "AUDIT_CLEAN_LINE" in GUI)
check("remap 用了 review_block_headline", "fu.review_block_headline(" in TOOLS["tools/remap/remap.py"])
check("remap 用了 AUDIT_SEG_CLEAN", "fu.AUDIT_SEG_CLEAN[" in TOOLS["tools/remap/remap.py"])
check("keep_ahead 用了 mid_gap_headline", "fu.mid_gap_headline(" in TOOLS["tools/keep_ahead/keep_ahead.py"])

# ---- ② 主 CLI 与独立工具的 -h 说明一致 ----
print("== 主 CLI 与工具的 -h 说明逐字一致 ==")


def help_of(args):
    out = subprocess.run([sys.executable, *args, "-h"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         cwd=str(ROOT)).stdout or ""
    return nospace(out)


CLI_REMAP = help_of([str(ROOT / "fanqie_upload.py"), "remap"])
TOOL_REMAP = help_of([str(ROOT / "tools" / "remap" / "remap.py")])
CLI_CD = help_of([str(ROOT / "fanqie_upload.py"), "clean-drafts"])
TOOL_CD = help_of([str(ROOT / "tools" / "clean_drafts" / "clean_drafts.py")])

for const in ("_H_RUN_WRITE", "_H_LIMIT_RW", "_H_NO_BUFFER", "_H_BUFFER_FILE",
              "_H_SAFETY_MINUTES", "_H_DAILY", "_H_BOOK_ID", "_H_CONTENT_DIR",
              "_H_HEADLESS", "_H_SHOW_BROWSER", "_H_ONLY", "_H_RANGE",
              "_H_INCLUDE_PUBLISHED", "_H_BUFFER_CHAPTER", "_H_USE_AI"):
    txt = getattr(fu, const)
    check(f"remap 的 {const} 两边一致",
          nospace(txt) in CLI_REMAP and nospace(txt) in TOOL_REMAP, txt[:30])
# --self-check 是工具自己才有的选项（主 CLI 不暴露它），所以只查工具那边
check("remap 的 --self-check 说明在工具里",
      nospace(fu._H_SELF_CHECK) in TOOL_REMAP, fu._H_SELF_CHECK)

for const in ("_H_RUN_DELETE", "_H_LIMIT_DEL", "_H_FORCE", "_H_BOOK_ID",
              "_H_CONTENT_DIR", "_H_HEADLESS"):
    txt = getattr(fu, const)
    check(f"clean-drafts 的 {const} 两边一致",
          nospace(txt) in CLI_CD and nospace(txt) in TOOL_CD, txt[:30])
check("clean-drafts 的 --self-check 说明在工具里",
      nospace(fu._H_SELF_CHECK) in TOOL_CD, fu._H_SELF_CHECK)

print()
if FAIL:
    print(f"FAILED {FAIL}: ")
    sys.exit(1)
print(f"全部通过（{PASS} 断言）")
