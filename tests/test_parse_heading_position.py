# -*- coding: utf-8 -*-
"""parse_md_file 标题位置语义测试（零依赖：不需要 pytest / 浏览器）。

回归锁定：标题只认文件首行的 "# "。修复前会扫描全文找第一个 "# " 行，
中部出现的 "# 场景X" 之类会被当成标题，且它之前的全部正文被静默丢弃——
发布出去的章节缺前半截。

运行:  python tests/test_parse_heading_position.py
"""
import sys
import tempfile
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
    d = Path(tempfile.mkdtemp())

    # 1) 中部 "# " 行：之前的正文必须保留，标题取自文件名而非中部行
    fp = d / "010_测试章.md"
    fp.write_text(
        "前半段第一行。\n前半段第二行。\n\n# 场景二\n\n后半段。",
        encoding="utf-8")
    num, title, content = fu.parse_md_file(fp)
    check("中部#行: 前半正文保留", "前半段第一行。" in content,
          f"content={content[:40]!r}")
    check("中部#行: 后半正文保留", "后半段。" in content)
    check("中部#行: 标题取自文件名", title == "测试章", f"title={title!r}")
    check("中部#行: 章节号来自文件名", num == "10", f"num={num!r}")

    # 2) 首行标题：行为不变
    fp2 = d / "a.md"
    fp2.write_text("# 第27章 重新开始\n\n正文。", encoding="utf-8")
    num2, title2, content2 = fu.parse_md_file(fp2)
    check("首行标题: 标题提取", title2 == "重新开始", f"title={title2!r}")
    check("首行标题: 章节号提取", num2 == "27", f"num={num2!r}")
    check("首行标题: 正文不含标题行", content2 == "正文。",
          f"content={content2!r}")

    # 3) 文件开头有空行+标题（read 时 strip，首个非空行即首行）
    fp3 = d / "b.md"
    fp3.write_text("\n\n# 第3章 空行后\n正文3", encoding="utf-8")
    _, title3, content3 = fu.parse_md_file(fp3)
    check("前导空行后的标题仍识别", title3 == "空行后", f"title={title3!r}")
    check("前导空行: 正文正确", content3 == "正文3")

    # 4) 无标题文件：全文为正文（行为不变）
    fp4 = d / "c.md"
    fp4.write_text("纯正文\n第二行", encoding="utf-8")
    _, _, content4 = fu.parse_md_file(fp4)
    check("无标题: 全文保留", content4 == "纯正文\n第二行")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
