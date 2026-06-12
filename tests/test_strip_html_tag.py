# -*- coding: utf-8 -*-
"""strip_md_formatting HTML 标签正则测试（零依赖：不需要 pytest / 浏览器）。

回归锁定：旧正则 <[^>]+> 的否定类匹配换行——正文里两个 ASCII 颜文字
（或散落的 < 与 >）之间的内容会被整段静默删除后发布。
修复后只移除真实标签形状（字母/斜杠开头、不跨行）。

运行:  python tests/test_strip_html_tag.py
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


def main():
    S = fu.strip_md_formatting

    # 1) 成对颜文字之间的正文不能被删
    out = S("(>_<)今天好累(>_<)")
    check("成对颜文字间正文保留", "今天好累" in out, f"out={out!r}")

    # 2) 跨行的 < ... > 不能把中间几段全删掉
    out = S("他低声说<\n中间这一大段正文。\n还有第二段。\n说完>结束")
    check("跨行尖括号间正文保留", "中间这一大段正文。" in out, f"out={out!r}")

    # 3) 数字比较符号不被当标签
    out = S("心率<60次，体温>38度")
    check("比较符号正文保留", "60次，体温" in out, f"out={out!r}")

    # 4) 书名误用尖括号保留
    out = S("他读过<剑来>这本书")
    check("CJK 尖括号内容保留", "<剑来>" in out, f"out={out!r}")

    # 5) 真实 HTML 标签照常移除（既有行为不回归）
    out = S("<div>正文</div><br>第二段</p>")
    check("真实标签移除", out == "正文第二段", f"out={out!r}")
    out = S('<img src="x.png" alt="图">独立行')
    check("带属性标签移除", out == "独立行", f"out={out!r}")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
