# -*- coding: utf-8 -*-
"""源码必须在 CI 矩阵里最老的 Python（3.10）上也能解析。

起因：2026-10-31 的 CI 红在 tests/test_entrypoint_parity.py——
tools/remap/remap.py 里写了一行 f-string 嵌套同款双引号：

    print(f"没有需要改写的章{f"，跳过 {len(skipped)} 个" if skipped else ""}")

这种写法是 PEP 701 的东西，**Python 3.12+ 才允许**（3.12 之前 f-string 里的
表达式不能再用同款引号）。3.10/3.11 拿到这行直接 SyntaxError，整个文件
import 不了——remap 子命令在 3.10 上是彻底跑不起来的。

麻烦的地方在于：本地开发用的是 3.14/3.15，全绿；只有 CI 的 3.10 矩阵会红。
「本地过了」在这里等于没测。所以这里在**当前解释器**上把 PEP 701 专用语法
揪出来——在 3.12+ 上它能解析，但源码里有 3.10 拒绝的写法；在 3.10/3.11 上
它连解析都过不了（这一支由 ast.parse 的 SyntaxError 兜住）。

运行:  python tests/test_py310_compat.py
"""
import ast
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些目录不是本仓的源码（虚拟环境、缓存、第三方包），扫了只会误报。
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
             "site-packages", "node_modules", ".mypy_cache", ".ruff_cache"}

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def read_src(path):
    """按 Python 自己认定的编码读源码（处理 BOM 与 # -*- coding -*- 声明）。"""
    with tokenize.open(str(path)) as f:
        return f.read()


def pep701_violations(src):
    """找出只有 3.12+ 才能解析的写法，返回 [(行号, 说明), ...]。

    两种情况都算违规：
      1) 本解释器就解析不了 → 在 3.10/3.11 上跑这个测试会走到这里；
      2) 能解析，但 f-string 的 {} 里出现了和外层同款的引号 → PEP 701 专用。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(e.lineno or 0,
                 f"本解释器（{sys.version_info.major}.{sys.version_info.minor}）"
                 f"解析不了这个文件：{e.msg}")]

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        seg = ast.get_source_segment(src, node)
        if not seg:
            continue
        # 外层引号：跳过头部前缀（f / r / b）后遇到的第一个引号
        i = 0
        while i < len(seg) and seg[i] not in "\"'":
            i += 1
        if i >= len(seg):
            continue
        quote = seg[i]
        # 花括号深度 > 0 的区域里出现同款引号 → 3.12 之前不合法
        depth = 0
        for j in range(i + 1, len(seg)):
            c = seg[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == quote and depth > 0:
                out.append((node.lineno,
                            f"f-string 的 {{}} 里用了和外层同款的 {quote}："
                            f"{seg.strip()[:90]}"))
                break
    return out


def py_files():
    """只扫**本仓的**源码。

    扫整个工作区是不行的：仓库根下有 .probe/、.workbuddy-ai/、.claude/ 这些
    被 gitignore 掉的工具目录（本地开发工具留下的），它们不是本仓代码，
    里面出现什么版本的语法都不该让本仓的 CI 变红。所以按 git 的口径来：
    已跟踪的文件 + 还没提交的新文件（-o），排除被忽略的（--exclude-standard）。
    git 不可用时退回「根目录 + tools/ + tests/」这个保守范围。
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "--", "*.py"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            for rel in out.stdout.splitlines():
                rel = rel.strip()
                if rel:
                    yield ROOT / rel
            return
    except (OSError, subprocess.SubprocessError):
        pass
    for p in sorted(ROOT.rglob("*.py")):
        parts = set(p.relative_to(ROOT).parts)
        if SKIP_DIRS & parts:
            continue
        if parts & {".probe", ".workbuddy-ai", ".claude", ".omo", ".gstack",
                    ".codegraph", ".qoder-credits"}:
            continue
        yield p


print("== 每个源码文件都能在 3.10 上解析 ==")
n_files = 0
for p in py_files():
    n_files += 1
    rel = p.relative_to(ROOT)
    try:
        src = read_src(p)
    except (SyntaxError, UnicodeDecodeError) as e:
        check(f"{rel} 读得进来", False, str(e))
        continue
    bad = pep701_violations(src)
    check(f"{rel} 没有 3.12+ 专用语法",
          not bad,
          "；".join(f"第 {ln} 行 {msg}" for ln, msg in bad))

print()
print(f"扫了 {n_files} 个源码文件")
if FAIL:
    print(f"FAILED {FAIL}：")
    sys.exit(1)
print(f"全部通过（{PASS} 断言）")
