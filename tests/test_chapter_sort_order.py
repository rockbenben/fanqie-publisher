# -*- coding: utf-8 -*-
"""章节文件排序顺序的回归测试（零依赖：不需要 pytest / 浏览器）。

守的是两件事：

1) 中文数字文件名（第一章…第十二章）必须按数值排。
   番茄的目录顺序 = 章节位置 = 发布顺序，排错就是读者看到乱序章节，
   而位置 3 天后就不能再移。README 的「MD 文件格式」明确支持这种命名，
   所以这是照文档用法就会踩的坑。

2) 阿拉伯数字 / 零填充前缀 / chapter-N / 纯数字这几种老命名的相对顺序
   **一个都不能变**——它们是绝大多数用户一直在用的写法。
   注意这里的「不变」指**元素序列**的先后：元素序列完全相同的两个名字
   （如 1.md 与 01.md）改前靠文件系统枚举顺序（本来就不确定），现在按
   原名分先后——这是变确定，不是变乱序。

运行:  python tests/test_chapter_sort_order.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fanqie_upload import (  # noqa: E402
    get_md_files, natural_sort_key, _cn_to_int, _CN_NUM_CHARS,
)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make(names):
    d = Path(tempfile.mkdtemp())
    for nm in names:
        (d / nm).write_text(f"# {nm}\n\n正文\n", encoding="utf-8")
    files = get_md_files(d)
    order = [f.name for f in files]
    shutil.rmtree(d, ignore_errors=True)
    return order


def cn(n):
    CN = "零一二三四五六七八九"
    if n < 10:
        return CN[n]
    if n < 20:
        return "十" + (CN[n % 10] if n % 10 else "")
    if n < 100:
        return CN[n // 10] + "十" + (CN[n % 10] if n % 10 else "")
    return str(n)


def main():
    print("[R1 中文数字按数值排]")
    names = [f"第{cn(i)}章 试炼场.md" for i in range(1, 13)]
    order = make(names)
    import re
    got = []
    for nm in order:
        m = re.search(rf"第([{_CN_NUM_CHARS}]+)章", nm)
        got.append(_cn_to_int(m.group(1)) if m else None)
    check("R1 第一章..第十二章 顺序递增", got == list(range(1, 13)), f"got={got}")
    check("R1 第一章排第一", order[0].startswith("第一章"), order[0])
    check("R1 第十二章排最后", order[-1].startswith("第十二章"), order[-1])

    print("[R2 中文数字 + 阿拉伯数字混写（写到 99 章后改用阿拉伯数字）]")
    mixed = [f"第{cn(i)}章.md" for i in range(1, 10)] + [f"第{i}章.md" for i in range(10, 13)]
    order = make(mixed)
    got = []
    for nm in order:
        m = re.search(rf"第([{_CN_NUM_CHARS}]+|\d+)章", nm)
        g = m.group(1)
        got.append(int(g) if g.isdigit() else _cn_to_int(g))
    check("R2 混写后章号仍然递增", got == list(range(1, 13)), f"got={got}")

    print("[R3 老命名的顺序一个都不能变]")
    check("R3 第1章..第12章", make([f"第{i}章 甲.md" for i in range(1, 13)]) ==
          [f"第{i}章 甲.md" for i in range(1, 13)])
    check("R3 零填充前缀", make([f"{i:03d}_新的旅程.md" for i in range(1, 13)]) ==
          [f"{i:03d}_新的旅程.md" for i in range(1, 13)])
    check("R3 chapter-N", make([f"chapter-{i}.md" for i in range(1, 13)]) ==
          [f"chapter-{i}.md" for i in range(1, 13)])
    check("R3 纯数字", make([f"{i}.md" for i in range(1, 13)]) ==
          [f"{i}.md" for i in range(1, 13)])

    # 混用命名家族：同一个目录里同时有「数字开头」和「文字开头」的文件。
    # 这条是本文件最要紧的一条——上面四条每种写法各占一个目录，压根没让
    # 两种写法相遇，所以「数字开头在前、文字开头在后」这个规则一次都没被
    # 验过。实测把键改回「文字开头在前」的那版，真实 get_md_files 的顺序
    # 整个翻过来，而当时全套测试照样全绿——这条断言就是为了钉住它。
    mixed_names = ["001_x.md", "2.md", "10.md", "chapter-1.md", "设定.md"]
    check("R3 混用命名家族：数字开头在前、文字开头在后",
          make(mixed_names) == ["001_x.md", "2.md", "10.md",
                                "chapter-1.md", "设定.md"],
          str(make(mixed_names)))
    check("R3 混用命名家族：章号也按数值排（2 在 10 前）",
          make(["chapter-10.md", "chapter-2.md", "chapter-1.md"]) ==
          ["chapter-1.md", "chapter-2.md", "chapter-10.md"],
          str(make(["chapter-10.md", "chapter-2.md", "chapter-1.md"])))

    # 元素序列完全相同的两个名字（数值一样、写法不同）：改前靠文件系统的
    # 枚举顺序（本来就不确定），现在按原名分先后。这条把「确定」这件事
    # 钉下来，别哪天又变回不确定的。
    check("R3 同值不同写法按原名分先后（确定，不再靠枚举顺序）",
          make(["1.md", "01.md"]) == ["01.md", "1.md"],
          str(make(["1.md", "01.md"])))

    # 名字以**中文数字开头**（"一.md"、"十二.md"）：改前整段是文本，现在
    # 的切法会把它切成「空文本 + 数字段」，空文本排在所有文字前面——于是
    # "一.md" 会跑到 "chapter-1.md" 前面，顺序整个翻过来。
    #
    # 这里必须拿**生产的键**去比：早先这组断言是拿「本地重写的改前键」跟
    # 「字符串大小」比——那两条比较对任何实现都成立，等于什么都没验（变异
    # 测试实测：把生产代码里那段合并删掉，整目录跑一遍照样全绿）。
    import re as _re

    def _old_key(name):
        """改前（HEAD）的键：只认阿拉伯数字，文本/数字交替。"""
        return [int(s) if s.isdigit() else s.lower()
                for s in _re.split(r"(\d+)", name)]

    for _a, _b in [("一.md", "chapter-1.md"), ("十.md", "a.md"),
                   ("十二.md", "001_x.md"), ("七零.md", "Z9.md")]:
        _now = sorted([_a, _b], key=lambda n: natural_sort_key(Path(n)))
        _old = sorted([_a, _b], key=_old_key)
        check(f"R3 中文数字开头的「{_a}」与「{_b}」先后和改前一致",
              _now == _old, f"现在={_now} 改前={_old}")
    check("R3 中文数字开头的仍按文件名本身排（不当成数值）",
          make(["十.md", "二.md"]) == ["二.md", "十.md"],
          str(make(["十.md", "二.md"])))

    # 「纯 ASCII 命名的顺序与改前逐项相同」这条承诺要有测试钉住：
    # 把改前的键在这里重写一遍，生成一批随机名字比两边的顺序。
    # （早先没有这条：把生产键里的 .lower() 去掉，整目录跑一遍照样全绿。）
    import random as _random
    _rng = _random.Random(20261029)
    _alpha = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-."
    _names = []
    for _ in range(300):
        _stem = "".join(_rng.choice(_alpha) for _ in range(_rng.randint(3, 18)))
        _names.append(_stem + ".md")
    _by_new = sorted(_names, key=lambda n: natural_sort_key(Path(n)))
    _by_old = sorted(_names, key=_old_key)
    check("R3 300 个随机纯 ASCII 文件名：与改前的键逐项同序",
          _by_new == _by_old,
          f"第一个不同处：{next((i for i, (x, y) in enumerate(zip(_by_new, _by_old)) if x != y), None)}")

    # 卷目录：目录名也要按数值排（V1 < V10 < V100），且目录内的文件排在目录之后
    d = Path(tempfile.mkdtemp())
    for vol in ("V1-凡人歌", "V10-玫瑰", "V100-北京"):
        (d / vol).mkdir()
        (d / vol / "chapter-0001.md").write_text("# x\n\n正文\n", encoding="utf-8")
    order = [f"{f.parent.name}/{f.name}" for f in get_md_files(d)]
    shutil.rmtree(d, ignore_errors=True)
    check("R3 卷目录 V1/V10/V100",
          order == ["V1-凡人歌/chapter-0001.md",
                    "V10-玫瑰/chapter-0001.md",
                    "V100-北京/chapter-0001.md"], str(order))

    print("[R4 数字段与文本段同位时不炸]")
    try:
        order = make(["第1章 甲.md", "第A章 乙.md", "chapter-1.md", "chapter-a.md"])
        check("R4 混合命名能排序", len(order) == 4, str(order))
    except Exception as e:
        check("R4 混合命名能排序", False, f"{type(e).__name__}: {e}")

    print("[R5 排序键不混用 int/str（避免 TypeError）]")
    # 比较时不会炸：两个形状不同的名字放一起比，元素序列的奇偶位是固定的
    # （偶数位文本、奇数位数字），所以永远是文本比文本、数字比数字。
    # 注意：改前的键也是交替的（re.split 出来的段），它同样不会混着比；
    # 它真正的问题是 int() 认不了 '²' 这类字符——那是 R6 管的。
    try:
        sorted([Path("a"), Path("a1"), Path("ab"), Path("第1章.md"),
                Path("第A章.md"), Path("第1章")],
               key=natural_sort_key)
        check("R5 形状不同的名字放一起比较也不炸", True)
    except Exception as e:
        check("R5 形状不同的名字放一起比较也不炸", False,
              f"{type(e).__name__}: {e}")

    print("[R6 怪字符文件名不崩]")
    # '①'、'²' 这类字符 isdigit() 为真但 int() 不认：分词器把它们放进文本段，
    # 再按 isdigit() 判成数字段就会在 int() 上抛 ValueError，整个扫描直接崩。
    for bad in ("十①十.md", "1²1.md", "第①章.md", "第Ⅲ章.md"):
        try:
            natural_sort_key(Path(bad))
            check(f"R6 {bad} 不崩", True)
        except Exception as e:
            check(f"R6 {bad} 不崩", False, f"{type(e).__name__}: {e}")
    try:
        order = make(["十①十.md", "1²1.md", "第①章.md"])
        check("R6 混着排也不崩", len(order) == 3, str(order))
    except Exception as e:
        check("R6 混着排也不崩", False, f"{type(e).__name__}: {e}")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
