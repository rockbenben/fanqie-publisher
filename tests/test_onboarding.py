# -*- coding: utf-8 -*-
"""新手引导步骤判定测试（零依赖：不创建 Tk 窗口）。

攻击真实代码: FanqieGUI._compute_onboarding_step
运行:  python tests/test_onboarding.py
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fanqie_gui import FanqieGUI  # noqa: E402

STEP = FanqieGUI._compute_onboarding_step
PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL [{name}] {detail}"
    PASS += 1
    print(f"  PASS {name}")


# ---- 四个有序状态：每多满足一个前置条件，步号 +1 ----
check("未登录→①", STEP(False, False, False) == 1)
check("未登录(即便后续条件为真)→①", STEP(False, True, True) == 1,
      "登录是第一道门槛，后面的条件不该把步号往前推")
check("已登录·未选作品→②", STEP(True, False, False) == 2)
check("已登录·未选作品(即便有章节)→②", STEP(True, False, True) == 2,
      "选作品的优先级高于有无章节")
check("已登录·选了作品·无章节→③", STEP(True, True, False) == 3)
check("全部就绪→④", STEP(True, True, True) == 4)

# ---- 单调性：从全 False 逐个置真，步号单调不减且覆盖 1..4 ----
seq = [STEP(False, False, False),
       STEP(True, False, False),
       STEP(True, True, False),
       STEP(True, True, True)]
check("单调递增 1→2→3→4", seq == [1, 2, 3, 4], str(seq))

# ---- 返回值始终是 1..4 的合法步号（穷举 8 种布尔组合） ----
all_steps = [STEP(a, b, c)
             for a in (False, True)
             for b in (False, True)
             for c in (False, True)]
check("步号恒在 1..4", all(1 <= s <= 4 for s in all_steps), str(all_steps))

print(f"\nALL PASSED ({PASS} 断言)")
