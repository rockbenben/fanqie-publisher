# -*- coding: utf-8 -*-
"""缺章补发的排期计算测试（零依赖：不需要 pytest / playwright / 浏览器）。

锁住 fanqie_upload.compute_gap_schedule 的槽位网格填空 + 插值兜底逻辑
（CLI 工具 tools/republish 与 GUI「补漏章」共用此核心）：
- 缺口按 9 槽位网格(07:00/:01/:02、12:00/:01/:02、20:00/:01/:02)在前后邻章间填空
- 槽位数与缺章数不符时退化为均匀插值并告警
- 全局补发时间严格单调（不打乱阅读顺序）

运行:  python tests/test_republish_schedule.py
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fanqie_upload as R  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL [{name}] {detail}"
    PASS += 1
    print(f"  PASS {name}")


def row(num, d, t, status="待发布"):
    return {"chapterNum": num, "title": f"第{num}章 x", "status": status,
            "date": d, "time": t}


def grid_tests():
    print("[槽位网格填空]")
    # 一天 9 章，缺中间第 5 章(槽位 12:01)
    day = "2026-08-20"
    rows = [row(n, day, R.GAP_SLOTS[i]) for i, n in enumerate(range(1, 10)) if n != 5]
    assign, warns = R.compute_gap_schedule(rows)
    check("单缺补回正确槽位",
          assign.get(5) == (day, "12:01"), f"got={assign.get(5)}")
    check("无告警", not warns, str(warns))

    # 跨日缺口：Day1 只到 20:01(缺 20:02)，Day2 从 07:00 起
    rows = [row(1, "2026-08-20", "20:00"), row(2, "2026-08-20", "20:01"),
            row(4, "2026-08-21", "07:00"), row(5, "2026-08-21", "07:01")]
    assign, warns = R.compute_gap_schedule(rows)
    check("跨日缺口补到前一天末槽",
          assign.get(3) == ("2026-08-20", "20:02"), f"got={assign.get(3)}")

    # 连续多章缺口填满整块空槽
    rows = [row(1, "2026-08-20", "12:00"), row(14, "2026-08-21", "20:01")]
    # 中间 2..13 共 12 章，槽位: 20-日 12:01,12:02,20:00,20:01,20:02(5) +
    # 21-日 07:00..20:00(7) = 12
    assign, warns = R.compute_gap_schedule(rows)
    check("多章块槽位数匹配无告警",
          len([k for k in assign if 2 <= k <= 13]) == 12 and not warns,
          f"n={len([k for k in assign if 2<=k<=13])} warns={warns}")
    seq = [assign[c] for c in sorted(assign)]
    check("块内时间严格单调",
          all(seq[i] < seq[i + 1] for i in range(len(seq) - 1)), str(seq))


def interp_tests():
    print("[插值兜底]")
    # 邻章间隙小于缺章数所需槽位 → 槽位数≠缺章数 → 插值 + 告警
    # Day 同一天 12:00 与 12:02 之间要塞 5 章(网格只有 12:01 一个槽)
    rows = [row(1, "2026-08-20", "12:00"), row(7, "2026-08-20", "12:02")]
    assign, warns = R.compute_gap_schedule(rows)
    got = [assign[c] for c in range(2, 7)]
    check("插值补足数量", len(got) == 5, f"got={got}")
    check("插值触发告警", any("插值" in w for w in warns), str(warns))
    # 分钟粒度下窗口塞不下这么多章 → 允许同分钟重复（番茄按章号排序），
    # 但必须非递减、且不越出邻章 [P, S] 区间（不侵占别的章的时间）
    P, S = ("2026-08-20", "12:00"), ("2026-08-20", "12:02")
    check("插值非递减且不越界",
          all(P <= g <= S for g in got)
          and all(got[i] <= got[i + 1] for i in range(len(got) - 1)), str(got))


def edge_tests():
    print("[边界]")
    check("空输入不崩", R.compute_gap_schedule([]) == ({}, R.compute_gap_schedule([])[1]))
    a, w = R.compute_gap_schedule([])
    check("空输入返回空分配+告警", a == {} and len(w) >= 1, f"{a} {w}")
    # 无缺口
    rows = [row(1, "2026-08-20", "07:00"), row(2, "2026-08-20", "07:01")]
    a, w = R.compute_gap_schedule(rows)
    check("无缺口→空分配", a == {}, str(a))
    # 邻章缺时间 → 该段跳过并告警（不静默丢、不崩溃）
    rows = [row(1, "2026-08-20", None), row(3, "2026-08-20", "07:02")]
    a, w = R.compute_gap_schedule(rows)
    check("邻章缺时间→告警跳过", 2 not in a and any("缺日期或时间" in x for x in w),
          f"a={a} w={w}")


def overdue_tests():
    print("[过期缺口检测]")
    # 三天各一章缺，now 落在中间那天之后
    rows = [row(1, "2026-08-20", "20:00"), row(3, "2026-08-21", "20:00"),
            row(5, "2026-08-22", "20:00")]
    assign, _ = R.compute_gap_schedule(rows)  # 缺 2(20-21间),4(21-22间)
    # now = 08-21 12:00 → 章2(排到 08-20/21)过期，章4(08-21/22之后)未过期
    od = R.overdue_gap_nums(assign, ("2026-08-21", "12:00"))
    check("过期检测命中已过期章", 2 in od and 4 not in od, f"assign={assign} od={od}")
    # now 在所有之前 → 无过期
    check("全未来→无过期",
          R.overdue_gap_nums(assign, ("2026-01-01", "00:00")) == [], "")
    # now 在所有之后 → 全过期，升序
    allod = R.overdue_gap_nums(assign, ("2027-01-01", "00:00"))
    check("全过去→全过期且升序", allod == sorted(assign), f"{allod}")


if __name__ == "__main__":
    grid_tests()
    interp_tests()
    edge_tests()
    overdue_tests()
    print(f"\nALL PASSED ({PASS} 断言)")
