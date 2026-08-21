# -*- coding: utf-8 -*-
"""不可逆模式（新建类）的策略契约：逐章确认 + 失败即停，CLI/GUI 同位点一致。

判据是**失败可不可逆**:
  · 定时发布/立即发布 —— 新建 item。漏一章 = 平台上根本没有这章，而新建只能
    追加到全书末尾，中段缺口再也补不回原位（2026-07-24 那次 748 章漏 151 章）。
    → 必须逐章确认；确认不到、或提交失败，都要中止整批。
  · 存草稿 —— 草稿丢了不影响正文顺序，保留原有的"连续3次"熔断即可。
  · 修改内容/修改排期 —— item 还在，重来一次即可，不该为确认牺牲吞吐。

运行: python tests/test_irreversible_modes.py
"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


CLI = inspect.getsource(fu.cmd_upload)
FU_SRC = (ROOT / "fanqie_upload.py").read_text(encoding="utf-8")
GUI = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")
# 批次循环已收进执行器（run_creation_batch / run_edit_batch），GUI 不再有循环段
EXEC = inspect.getsource(fu.run_creation_batch)
EDIT_EXEC = inspect.getsource(fu.run_edit_batch)
KA = (ROOT / "tools" / "keep_ahead" / "keep_ahead.py").read_text(encoding="utf-8")
REMAP = (ROOT / "tools" / "remap" / "remap.py").read_text(encoding="utf-8")

print("== 单章动作只许有一份实现（三原语）==")
# 每章要做的事都收在 fanqie_upload 的三个原语里，CLI/GUI/tools 只做编排。
# 曾经 keep_ahead 把整个发布循环抄了一遍（goto→填→发→重试），
# 这类重复会让"修一边漏一边"变成必然。
for _p in ("publish_one_chapter", "draft_one_chapter", "edit_one_chapter"):
    check(f"{_p} 在 fanqie_upload 里有且仅有一份定义",
          FU_SRC.count("async def " + _p + "(") == 1)
    check(f"{_p} 在 GUI 里没有第二份定义",
          "async def " + _p + "(" not in GUI)
# 编排层不许自己拼单章流程（那是原语的活）
for _name, _src in (("CLI", CLI), ("GUI", GUI), ("执行器", EXEC)):
    check(f"{_name} 不内联 publish_scheduled",
          "publish_scheduled(" not in _src)
    check(f"{_name} 不内联 save_draft", "save_draft(" not in _src)
check("keep_ahead 不内联发布循环",
      "publish_one_chapter" in KA and "await fu.publish_scheduled" not in KA)

print("== 通用件只许一份实现 ==")
# 这几样曾经在多处各写一份逐字相同的实现，是"改一边漏一边"的温床。
_TOOLS = {n: (ROOT / "tools" / n / (n + ".py")).read_text(encoding="utf-8")
          for n in ("remap", "keep_ahead", "clean_drafts")}
check("章号压缩只在 fanqie_upload 定义",
      FU_SRC.count("def compress_chapter_nums(") == 1
      and "def _compress_nums(nums)" not in GUI)
check("GUI 用共用的章号压缩", "compress_chapter_nums" in GUI)
for _n, _src in _TOOLS.items():
    check(f"{_n} 不自己取 last_book_id", "last_book_id" not in _src)
    check(f"{_n} 不自己拼本地章号索引",
          "get_md_files(" not in _src or "local_chapter_index" in _src)
check("三个取参数的共用件都在",
      all(f"def {f}(" in FU_SRC
          for f in ("resolve_target", "local_chapter_index", "resolve_headless")))

print("== 新建类必须逐章确认 + 失败即停（不变量只在执行器一份）==")
# 曾经这些不变量要求"CLI/GUI 同位点各有一份"，靠逐边 grep 保一致；
# 现在批次循环收进 run_creation_batch，一致性由构造保证——两个入口只是
# 调用它并注入回调（取消/进度），想漏都没处漏。
check("执行器: 挂签名监听", "watch_chapter_list_url" in EXEC)
check("执行器: 逐章确认", "confirm_chapter_on_platform" in EXEC)
check("执行器: 确认不到即中止", "平台上查不到 第" in EXEC)
check("执行器: 失败即停", "不可逆（缺章补不回原位）" in EXEC)
check("执行器: 存草稿保留连续3次熔断", "consec_fail >= 3" in EXEC)
check("CLI 上传走执行器", "run_creation_batch" in CLI)
check("CLI 修改走执行器", "run_edit_batch" in inspect.getsource(fu.cmd_edit))
check("GUI 上传走执行器", "run_creation_batch(" in GUI)
check("GUI 修改走执行器", "run_edit_batch(" in GUI)
check("GUI 不再自带批次循环（确认/监听只在执行器）",
      "confirm_chapter_on_platform" not in GUI
      and "watch_chapter_list_url" not in GUI)
check("修改执行器: 二次尝试兼有页面死亡短路与取消记账",
      "页面已失效，未二次尝试" in EDIT_EXEC
      and "用户取消，未二次尝试" in EDIT_EXEC)
# 三个入口现在查同一个名字（keep_ahead 曾用别名 confirm_created 包一层，
# 别名让 grep 查不到调用，审计连着误判过好几次）
check("keep_ahead 逐章确认",
      "confirm_chapter_on_platform" in KA and "平台上查不到" in KA)

print("== 草稿: 批末对账而非逐章确认 ==")
# 草稿丢了重传即可、不影响正文顺序 → 不该为一次确认失败中止整批；
# 但要补上"草稿ID读不到就无从判断"的推断盲区 → 用草稿箱真实内容对账。
check("有草稿对账函数", hasattr(fu, "reconcile_drafts_after_batch"))
check("走 draft_list 而非 chapter_list",
      "chapter/draft_list" in inspect.getsource(fu.fetch_draft_list))
_dsrc = inspect.getsource(fu.reconcile_drafts_after_batch)
check("草稿对账不中止批次", "abort" not in _dsrc and "break" not in _dsrc)
# 两边都走统一入口 reconcile_batch_auto，由它按模式挑对账方式——
# 曾经 CLI/GUI 各写一遍 if is_draft 的选择逻辑，加一种模式要改两处。
check("收尾对账在执行器里统一走 reconcile_batch_auto",
      "reconcile_batch_auto" in EXEC)
check("GUI 不再自己调对账（由执行器代管）", "reconcile_batch_auto" not in GUI)
check("统一入口内部按模式分流",
      "reconcile_drafts_after_batch" in inspect.getsource(fu.reconcile_batch_auto)
      and "reconcile_after_batch" in inspect.getsource(fu.reconcile_batch_auto))
check("草稿分页上限 30（接口 50 起报 code=-100）", fu._DRAFT_PAGE == 30)

print("== 可逆类不该逐章回读（回读误判会白丢整批）==")
_remap_loop = REMAP.split("async def run_remap(")[1].split("async def verify_batch(")[0]
check("remap 提交循环里没有逐章回读", "read_back" not in _remap_loop
      and "check_applied" not in _remap_loop)
check("remap 改为批末复核", "verify_batch" in REMAP)

print("== 确认函数本身的判据 ==")
check("按章号匹配而非模糊包含",
      "chapter_title_num(t) == num" in inspect.getsource(
          fu.confirm_chapter_on_platform))
check("拿不到签名 URL 时不阻断任务",
      "if not url_holder:" in inspect.getsource(fu.confirm_chapter_on_platform))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
