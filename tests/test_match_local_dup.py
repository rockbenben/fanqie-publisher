# -*- coding: utf-8 -*-
"""match_chapters 本地重复章节号测试（零依赖：不需要 pytest / 浏览器）。

回归锁定：本地两个文件同章节号（多卷子文件夹各自从 1 编号 / 残留旧副本）
修复前会都匹配到同一平台章节、修改模式先后写入两份内容，后写的（子文件夹
排在根目录之后）静默覆盖正确版本。修复后仅取首个，其余进 unmatched。

运行:  python tests/test_match_local_dup.py
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
    platform = [
        {"chapterNum": 1, "title": "第1章 旧标题", "editUrl": "/e/1"},
        {"chapterNum": 2, "title": "第2章 旧标题", "editUrl": "/e/2"},
    ]
    # 根目录的新稿在前，子文件夹的旧副本在后（get_md_files 的真实顺序）
    local = [
        ("1", "新标题", "新内容"),
        ("2", "第二章", "内容2"),
        ("1", "旧标题", "旧内容（残留副本）"),
    ]
    matched, unmatched = fu.match_chapters(local, platform)

    check("总量守恒", len(matched) + len(unmatched) == len(local))
    check("同号仅匹配一次", len(matched) == 2,
          f"matched={[(m[0], m[2]) for m in matched]}")
    by_num = {m[2]: m for m in matched}
    check("保留首个（根目录新稿）", by_num[1][4] == "新内容",
          f"got {by_num[1][4]!r}")
    check("重复副本进 unmatched", any(u[0] == 2 for u in unmatched),
          f"unmatched={unmatched}")

    # 无重复时行为不变
    local2 = [("1", "a", "x"), ("2", "b", "y")]
    m2, u2 = fu.match_chapters(local2, platform)
    check("无重复不受影响", len(m2) == 2 and not u2)

    # 与平台无关的重复号（平台无此章）不触发去重告警路径，照常 unmatched
    local3 = [("9", "a", "x"), ("9", "b", "y")]
    m3, u3 = fu.match_chapters(local3, platform)
    check("平台无此章的重复号全部 unmatched", not m3 and len(u3) == 2)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
