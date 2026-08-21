# -*- coding: utf-8 -*-
"""CLI 命令的调用契约：参数真的能绑上，不是只有 `-h` 能过。零依赖。

起因: `reschedule` 子命令曾经把 5 个位置参数传给只接受 3 个位置参数的
reschedule_on_manage_page，还传了一个根本不存在的 all_volumes=，并且从头到尾
没抓平台章节、没建 schedule_map —— 这个命令**从来没能跑起来**，而
test_entrypoint_parity 只断言 `reschedule -h` 退出 0，所以一路绿灯。

这里用 inspect.signature().bind() 静态验证「调用点的实参能绑上被调函数的
签名」，不需要浏览器和账号。

运行: python tests/test_cli_call_contracts.py
"""
import ast
import inspect
import textwrap
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f"  <- {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILED.append(label)


def calls_in(func, callee_name):
    """在 func 源码里找出对 callee_name 的调用，返回 (位置实参数, 关键字名集合)。"""
    # textwrap.dedent 而不是手工切 4 空格 + cleandoc：cleandoc 会把 docstring
    # 的缩进也一起吃掉，函数体看起来就成了空的（IndentationError）
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        if fn != callee_name:
            continue
        n_pos = len(n.args)
        kw = {k.arg for k in n.keywords if k.arg}
        out.append((n_pos, kw))
    return out


def bindable(callee, n_pos, kwargs):
    """位置实参 + 关键字实参能否绑上 callee 的签名（第一个 page 之类照给占位）。"""
    sig = inspect.signature(callee)
    try:
        sig.bind(*([object()] * n_pos), **{k: object() for k in kwargs})
        return True, ""
    except TypeError as e:
        return False, str(e)


print("== 调用点的实参必须能绑上被调函数签名 ==")
CONTRACTS = [
    (fu.cmd_reschedule_cli, "reschedule_on_manage_page", fu.reschedule_on_manage_page),
    (fu.cmd_upload, "run_creation_batch", fu.run_creation_batch),
    (fu.cmd_edit, "run_edit_batch", fu.run_edit_batch),
    (fu.cmd_upload, "load_local_chapters", fu.load_local_chapters),
    (fu.cmd_edit, "load_local_chapters", fu.load_local_chapters),
]
for caller, name, callee in CONTRACTS:
    found = calls_in(caller, name)
    check(f"{caller.__name__} 里找得到 {name}( 的调用", bool(found))
    for n_pos, kw in found:
        ok, why = bindable(callee, n_pos, kw)
        check(f"{caller.__name__} -> {name}: {n_pos} 位置 + {sorted(kw)} 可绑定",
              ok, why)

print()
print("== reschedule 必须真的做完整件事，不能只开个浏览器 ==")
RS = inspect.getsource(fu.cmd_reschedule_cli)
check("抓平台章节", "extract_chapters_from_page" in RS)
check("只挑「待发布」", "待发布" in RS)
check("算排期", "compute_schedule(" in RS)
check("建 schedule_map 再传进去", "schedule_map" in RS)
check("同名章节会中止（排期按标题匹配，同名必错配）", "同名章节" in RS)
check("多卷走 volume_texts 而非不存在的 all_volumes",
      "volume_texts=" in RS and "all_volumes=" not in RS)
check("收尾保存会话", "save_auth" in RS)

print()
print("== README 里给的 reschedule 示例，参数都真实存在 ==")
import subprocess  # noqa: E402
_help = subprocess.run([sys.executable, str(ROOT / "fanqie_upload.py"),
                        "reschedule", "-h"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT)).stdout or ""
for flag in ("--book-id", "--schedule", "--time", "--per-day", "--all-volumes"):
    check(f"reschedule 有 {flag}", flag in _help)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
