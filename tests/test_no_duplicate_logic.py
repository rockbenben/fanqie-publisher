# -*- coding: utf-8 -*-
"""重复实现回归测试：一个功能只能有一份实现，且只能待在一个地方。

这个文件把"重复检测"本身固化下来 —— 新写的复制粘贴、新加的别名，都会让它变红。

为什么值得一个专门的测试：本项目历史上的 bug 几乎全是"同一段逻辑存在两份，
修了一边漏了另一边"。别名尤其阴险 —— 一个函数挂两个名字，grep 规范名查不到
别名调用，连审计脚本都会误判成"没人用"。

运行: python tests/test_no_duplicate_logic.py
"""
import ast
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ([ROOT / "fanqie_upload.py", ROOT / "fanqie_gui.py"]
         + sorted(ROOT.glob("tools/*/*.py")))
FU = (ROOT / "fanqie_upload.py").read_text(encoding="utf-8")
GUI = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


print("== 共用实现只能有一份定义 ==")
SHARED = [
    # 三个单章原语
    "publish_one_chapter", "draft_one_chapter", "edit_one_chapter",
    # 确认 / 对账 / 记账
    "confirm_chapter_on_platform", "reconcile_batch_auto",
    "record_unprocessed", "record_rest_unprocessed", "log_fail_list",
    "compress_chapter_nums",
    # 筛选与解析
    "parse_chapter_spec", "filter_by_chapter_spec",
    "parse_datetime", "parse_time_spec",
    # 位置换算
    "global_position", "audit_chapter_positions",
    # 各入口的开场
    "tool_startup", "book_mismatch_abort",
    "require_login_cli", "load_local_chapters",
    "resolve_target", "resolve_headless", "local_chapter_index",
    # 提交动作
    "_submit_confirm_publish",
    # 批次执行器（发布/修改循环——当初漏 151 章的路径，绝不允许第二份）
    "run_creation_batch", "run_edit_batch",
]
defs = defaultdict(list)
for f in FILES:
    for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[n.name].append(f"{f.name}:{n.lineno}")
for name in SHARED:
    where = defs.get(name, [])
    check(f"{name} 只定义一次" + ("" if len(where) == 1 else f" -> {where}"),
          len(where) == 1)

print()
print("== GUI 的共用方法：一份定义，多处调用 ==")
for m, least in (("_read_schedule_params", 2), ("_require_login", 4),
                 ("_pack_volume_picker", 2), ("_begin_task", 3)):
    check(f"{m} 只定义一次", GUI.count(f"def {m}(") == 1)
    check(f"{m} 至少 {least} 处调用", GUI.count(f"self.{m}(") >= least)

print()
print("== 旧的重复写法不得复活 ==")
GONE = [
    (GUI, 'for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d")', "GUI 内联日期解析"),
    (GUI, "any(lo <= v <= hi for lo, hi in intervals)", "GUI 内联章节号命中"),
    (GUI, "def _as_int(", "GUI 自带的章节号转换"),
    (FU, 'acc + it["index"] % VOLUME_INDEX_STRIDE', "内联卷偏移公式"),
    (GUI, 'messagebox.showwarning("需要先登录"',
     "裸 messagebox 登录提示（定时模式会弹出没人点的模态框堵死整批）"),
]
for src, frag, label in GONE:
    check(f"{label} 已消除", frag not in src)
# 提交动作只能存在一次：它就在 _submit_confirm_publish 里，多一次就是又抄了一份。
# 这一步是"是否真的提交成功"的判定所在，曾按"按钮消失"算成功漏掉 151 章。
_SUBMIT = 'confirm_btn = page.locator("button", has_text=_CONFIRM_SUBMIT_RE)'
_n = FU.count(_SUBMIT)
check(f"提交动作全仓只有一份（实测 {_n} 处）", _n == 1 and _SUBMIT not in GUI)
# 页脚提交按钮不得按死文案定位：平台 2026-09 把它由「确认发布」改成「确认提交」
# （issue #3），死文案找不到 -> 每章抛错 -> 整批中止。认文案必须走
# _CONFIRM_SUBMIT_RE 这一个常量，两种文案都在里面。
for _dead in ('has_text="确认发布"', 'has_text="确认提交"'):
    check(f"提交按钮不按死文案定位（{_dead}）", _dead not in FU)

print()
print("== 不得出现「同一个东西两个名字」==")
# 别名不是重复定义，但同样有害：grep 规范名查不到别名调用点。
# 这个项目为此误判过好几次（audit / confirm_created / _log_fail_list ...）。
aliases = []
for f in FILES:
    for node in ast.parse(f.read_text(encoding="utf-8")).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        v = node.value
        pure = (isinstance(v, ast.Name)
                or (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)))
        if pure:
            aliases.append(f"{f.name}:{node.lineno} {ast.unparse(node)}")
check("模块级别名为 0" + ("" if not aliases else f" -> {aliases}"), not aliases)

print()
print("== 函数以下粒度的复制粘贴不得增加 ==")
# 连续 6 行（去空行去注释后）完全相同即算一处。批次循环已抽成执行器
# （run_creation_batch / run_edit_batch），剩下 2 组是可接受的惯用样板：
# 单章原语的重试骨架（抽出反而多一层跳转）与 GUI 两个 task 的开场样板
# （薄壳本来就该长得像）。卡在 2，再长出复制粘贴立即变红。
CAP = 2
W = 6
wins = defaultdict(set)
for f in FILES:
    rows = []
    for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        t = ln.strip()
        if t and not t.startswith("#"):
            rows.append((i, t))
    for k in range(len(rows) - W + 1):
        chunk = rows[k:k + W]
        body = tuple(t for _, t in chunk)
        if sum(len(t) for t in body) < 160 or len(set(body)) < 5:
            continue
        wins[body].add((f.name, chunk[0][0]))
# 同一处重复会被滑动窗口命中多次（错开 1 行就是另一个 body）。
# 早先用 行号//12 分桶去重，但桶边界是任意的——上方加几行就能把同一处
# 重复挤成两组，让这个守卫在无关改动上变红。改成按实际行号合并：
# 两组的位置集合能逐一配对到 W 行以内，就是同一处。
groups = [b for b, v in wins.items() if len(v) > 1]
clusters = []
for body in sorted(groups, key=lambda b: -len(b)):
    locs = sorted(wins[body])
    for c in clusters:
        if len(c) == len(locs) and all(
                a[0] == b[0] and abs(a[1] - b[1]) < W for a, b in zip(c, locs)):
            break
    else:
        clusters.append(locs)
n = len(clusters)
print(f"  当前重复块 {n} 组（上限 {CAP}）")
check(f"重复块不超过 {CAP} 组", n <= CAP)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
