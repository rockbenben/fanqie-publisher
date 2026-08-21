# -*- coding: utf-8 -*-
"""入口对等契约：每个能力都要 CLI 和 GUI 各有一个入口。

背景: tools/ 下那几个能力最初是救火脚本（remap、keep_ahead、clean_drafts），
一度只有命令行能用；而"修改排期"反过来只有 GUI 有。这种不对称会让人以为
某个功能不存在，也让自动化没法覆盖全部操作。

运行: python tests/test_entrypoint_parity.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


help_txt = subprocess.run(
    [sys.executable, str(ROOT / "fanqie_upload.py"), "-h"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=str(ROOT)).stdout or ""
up_help = subprocess.run(
    [sys.executable, str(ROOT / "fanqie_upload.py"), "upload", "-h"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=str(ROOT)).stdout or ""
GUI = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")

# 能力 -> (CLI 里该出现什么, GUI 里该出现什么)
CAPS = {
    "定时发布": ("--schedule", '"schedule"'),
    "立即发布": ("--publish", '"publish"'),
    "存草稿": ("upload", '"draft"'),
    "修改内容": ("--edit", '"edit"'),
    "修改排期": ("reschedule", '"reschedule"'),
    "章节重排": ("remap", "_on_tool_remap"),
    "缺口体检": ("audit", "_on_audit_gaps"),
    "清空草稿箱": ("clean-drafts", "_on_tool_clean_drafts"),
    "自动接续队列": ("--auto-continue", "autocont_var"),
}

print("== 每个能力都要 CLI + GUI 双入口 ==")
for cap, (cli_token, gui_token) in CAPS.items():
    in_cli = cli_token in help_txt or cli_token in up_help
    check(f"{cap}: CLI 有入口", in_cli)
    check(f"{cap}: GUI 有入口", gui_token in GUI)

import subprocess as _sp


def _opts(*cmd):
    out = _sp.run([sys.executable] + list(cmd) + ["-h"], capture_output=True,
                  text=True, encoding="utf-8", errors="replace",
                  cwd=str(ROOT)).stdout or ""
    return {ln.strip().split()[0] for ln in out.splitlines()
            if ln.strip().startswith("--")}


_main = str(ROOT / "fanqie_upload.py")
print("== 用户可调的参数，CLI 和 GUI 都要有 ==")
# 只查"入口存在"不够——参数缺一个同样是功能缺失。曾经漏过三处:
# CLI 没有按章节号筛选（却在日志里教用户去用它补传）、CLI 没有按修改日期筛选、
# GUI 没有队列深度。
_up = _opts(_main, "upload")
_rs = _opts(_main, "reschedule")
OPT_PAIRS = [
    ("每天章数", "--per-day" in _up, "perday_var" in GUI),
    ("发布时间点", "--time" in _up, "time_var" in GUI),
    ("起始日期", "--schedule" in _up, "date_var" in GUI),
    ("AI 申报", "--use-ai" in _up, "use_ai_var" in GUI),
    ("无头运行", "--headless" in _up, "headless_var" in GUI),
    ("自动接续", "--auto-continue" in _up, "autocont_var" in GUI),
    ("维持N天深度", "--days-ahead" in _up, "days_ahead_var" in GUI),
    ("重名自动去重", "--unique-titles" in _up, "unique_var" in GUI),
    ("章节间延时", "--delay" in _up, "delay_between_chapters" in GUI),
    ("合并所有卷", "--all-volumes" in _rs, "all_volumes_var" in GUI),
    ("按章节号筛选", "--chapters" in _up, "resched_filter_var" in GUI),
    ("按修改日期筛选", "--modified-after" in _up, "按修改日期筛选" in GUI),
]
for _n, _c, _g in OPT_PAIRS:
    check(f"{_n}: CLI 与 GUI 都有", _c and _g)

print("== 工具自身的参数，主 CLI 都要暴露 ==")
# 曾经漏过三处: upload 没有 --days-ahead（keep_ahead 的维持深度没入口）、
# audit 没有 --daily（当不了烟雾报警器）、clean-drafts 没有 --show-browser。
_ka_own = _opts(str(ROOT / "tools/keep_ahead/keep_ahead.py"))
_up = _opts(_main, "upload")
check("upload 暴露了 --auto-continue", "--auto-continue" in _up)
check("upload 暴露了 --days-ahead（维持深度）", "--days-ahead" in _up)
check("keep_ahead 的深度参数没被漏掉",
      "--days-ahead" not in _ka_own or "--days-ahead" in _up)

_ad = _opts(_main, "audit")
check("audit 支持 --daily（无人值守体检）", "--daily" in _ad)
check("audit 是只读的（没有 --run）", "--run" not in _ad)

_cd_own = _opts(str(ROOT / "tools/clean_drafts/clean_drafts.py"))
_cd = _opts(_main, "clean-drafts")
for _o in ("--run", "--limit", "--force"):
    check(f"clean-drafts 暴露了 {_o}", _o in _cd)
check("clean-drafts 有 --show-browser（与其它子命令一致）", "--show-browser" in _cd)

_rm_own = _opts(str(ROOT / "tools/remap/remap.py"))
_rm = _opts(_main, "remap")
_missing = {o for o in _rm_own if o not in _rm} - {"--audit", "--self-check"}
check(f"remap 的参数主 CLI 没漏（差集 {_missing or '空'}）", not _missing)

print("== README 教的命令必须真能跑 ==")
# 文档教内部路径（tools/xxx.py）而代码已提供子命令，用户会照着敲然后困惑。
import re as _re
_readme = (ROOT / "README.md").read_bytes().decode("utf-8")
_cmds = set(_re.findall(r"python3? fanqie_upload\.py ([a-z-]+)", _readme))
_subs = set(_re.findall(r"^    ([a-z-]+)\s{2,}", help_txt, _re.M))
for _c in sorted(_cmds):
    check(f"README 里的 `{_c}` 是真子命令", _c in _subs or _c in ("-h",))
# 用户文档不该再教内部路径（--self-check 是开发用，允许）
_toolrefs = [ln.strip() for ln in _readme.splitlines()
             if "python tools/" in ln and "--self-check" not in ln]
check(f"README 不再教内部路径（残留 {len(_toolrefs)} 处）", not _toolrefs)

print("== 工具模块要满足共用外壳的约定 ==")
# run_unattended 会取 mod.LOG_DIR 和 mod.main_async；clean_drafts 曾漏了
# LOG_DIR，主 CLI 一调就 AttributeError 崩掉（--self-check 和直跑都测不出来，
# 只有走主 CLI 那条路才暴露）。
import importlib.util as _ilu

for _name in ("remap", "keep_ahead", "clean_drafts"):
    _path = ROOT / "tools" / _name / (_name + ".py")
    _spec = _ilu.spec_from_file_location("_probe_" + _name, _path)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    check(f"{_name} 有 LOG_DIR", hasattr(_mod, "LOG_DIR"))
    check(f"{_name} 有 main_async", callable(getattr(_mod, "main_async", None)))
    check(f"{_name} 有 --self-check", "--self-check" in _opts(str(_path)))

print("== 子命令都能解析 ==")
for cmd in ("login", "books", "upload", "remap", "audit",
            "clean-drafts", "reschedule"):
    r = subprocess.run(
        [sys.executable, str(ROOT / "fanqie_upload.py"), cmd, "-h"],
        capture_output=True, text=True, cwd=str(ROOT))
    check(f"{cmd} -h 可用", r.returncode == 0)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
