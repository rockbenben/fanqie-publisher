# -*- coding: utf-8 -*-
"""_wait_publish_result 状态机仿真测试（零依赖：不需要 pytest / playwright / 浏览器）。

用虚拟时钟 + 假 page/locator 驱动 fanqie_upload 里的真实判定代码：
- 10 个定向场景锁住已修复过的回归（按钮消失=成功、上限/错误分类优先级、
  notification 角色分离、静默自愈、重点击预算守卫、超时语义）
- 100 轮随机模糊验证不变量（必然终止、点击次数受限、预算守卫、结果与脚本一致）

运行:  python tests/test_publish_result.py
"""
import asyncio
import random
import sys
from pathlib import Path

# Windows GBK 控制台无法打印 emoji/中文——测试输出（含 logger 走的 stderr）
# 统一 UTF-8 并容错
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402

TIMEOUT_MS = 15000
CDP = 0.03  # 每次 CDP 往返的模拟耗时（evaluate / is_visible 各计一次）


class VirtualClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def advance(self, s):
        self.t += s


class _TimeShim:
    """替身 time 模块：_wait_publish_result 只用到 monotonic。"""

    def __init__(self, clock):
        self.monotonic = clock.monotonic


class FakeBtn:
    def __init__(self, clock, visible_fn, on_click=None):
        self.clock = clock
        self.visible_fn = visible_fn  # (t) -> bool，可 raise
        self.on_click = on_click
        self.clicks = []  # 每次重点击的虚拟时刻

    async def is_visible(self):
        self.clock.advance(CDP)
        return self.visible_fn(self.clock.t)

    async def click(self, **kw):
        self.clock.advance(0.05)
        self.clicks.append(self.clock.t)
        if self.on_click:
            self.on_click(self.clock.t)

    async def evaluate(self, expr, timeout=None):
        self.clock.advance(CDP)
        return "<button class='arco-btn'>确认发布</button>"


class FakePage:
    def __init__(self, clock, toasts_fn):
        self.clock = clock
        self.toasts_fn = toasts_fn  # (t) -> {"messages": [...], "notifications": [...]}
        self.url = "https://fanqienovel.com/main/writer/test/publish"

    async def evaluate(self, js):
        self.clock.advance(CDP)
        return self.toasts_fn(self.clock.t)

    async def wait_for_timeout(self, ms):
        self.clock.advance(ms / 1000)

    def on(self, *a, **k):
        pass

    def remove_listener(self, *a, **k):
        pass


def windows(*items):
    """items: (start, end, text)。返回 toasts_fn 的一组窗口。"""
    return list(items)


def make_toasts_fn(messages=(), notifications=()):
    def fn(t):
        return {
            "messages": [x for (s, e, x) in messages if s <= t < e],
            "notifications": [x for (s, e, x) in notifications if s <= t < e],
        }
    return fn


def run_case(visible_fn, messages=(), notifications=(), on_click=None,
             timeout_ms=TIMEOUT_MS):
    """跑一轮 _wait_publish_result，返回 (outcome, clock, btn)。
    outcome: "success" | ("limit", msg) | ("error", msg) | ("timeout", msg)
    """
    clock = VirtualClock()
    real_time = fu.time
    fu.time = _TimeShim(clock)
    try:
        page = FakePage(clock, make_toasts_fn(messages, notifications))
        btn = FakeBtn(clock, visible_fn, on_click)

        async def go():
            await fu._wait_publish_result(page, btn, timeout=timeout_ms)

        try:
            asyncio.run(go())
            return "success", clock, btn
        except fu.DailyLimitReached as e:
            return ("limit", str(e)), clock, btn
        except RuntimeError as e:
            msg = str(e)
            kind = "timeout" if "按钮未消失" in msg else "error"
            return (kind, msg), clock, btn
    finally:
        fu.time = real_time


PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL [{name}] {detail}"
    PASS += 1
    print(f"  PASS {name}")


def scenario_tests():
    print("[定向场景]")

    # S1 按钮 1s 后消失 → 成功，零重点击
    out, clk, btn = run_case(lambda t: t < 1.0)
    check("S1 按钮消失=成功", out == "success" and not btn.clicks,
          f"out={out} clicks={btn.clicks}")

    # S2 上限 toast(message) → DailyLimitReached，1.5s 内，零重点击
    out, clk, btn = run_case(
        lambda t: True, messages=windows((0.4, 3.4, "提交字数超出每日上限")))
    check("S2 上限秒级捕获", out[0] == "limit" and clk.t <= 1.5 and not btn.clicks,
          f"out={out} t={clk.t:.2f}")

    # S3 错误 toast → RuntimeError 立即失败
    out, clk, btn = run_case(
        lambda t: True, messages=windows((0.4, 3.4, "内容包含敏感词")))
    check("S3 错误秒级失败", out[0] == "error" and clk.t <= 1.5, f"out={out}")

    # S4 上限走 notification 渠道也能抓到
    out, clk, btn = run_case(
        lambda t: True, notifications=windows((0.4, 3.4, "已到达当日发布字数上限")))
    check("S4 上限via notification", out[0] == "limit", f"out={out}")

    # S5 常驻良性公告：不团灭、不压制自愈；预算守卫只允许 1 次重点击
    out, clk, btn = run_case(
        lambda t: True,
        notifications=windows((0.0, 999.0, "系统公告：新功能上线，敬请体验")))
    check("S5a 常驻公告不误判失败", out[0] == "timeout", f"out={out}")
    check("S5b 自愈不被公告压制且预算守卫生效",
          len(btn.clicks) == 1 and btn.clicks[0] <= 8.5,
          f"clicks={btn.clicks}")
    check("S5c 超时消息含词库提示与URL",
          "词库" in out[1] and "当前URL" in out[1], out[1][:120])

    # S6 点击被吞：重点击后 1s 按钮消失 → 成功且恰好 1 次重点击
    state = {"hide_at": None}

    def on_click(t):
        state["hide_at"] = t + 1.0

    def visible(t):
        return state["hide_at"] is None or t < state["hide_at"]

    out, clk, btn = run_case(visible, on_click=on_click)
    check("S6 静默自愈重点击成功", out == "success" and len(btn.clicks) == 1,
          f"out={out} clicks={btn.clicks}")

    # S7 同文本良性 toast 反复弹出（间隙 0.5s）→ 算有响应，不重点击
    rep = windows(*[(k * 2.0, k * 2.0 + 1.5, "已自动保存") for k in range(10)])
    out, clk, btn = run_case(lambda t: True, messages=rep)
    check("S7 重复toast算响应不自愈", out[0] == "timeout" and not btn.clicks,
          f"out={out} clicks={btn.clicks}")

    # S8 页面半死（is_visible 一直抛）→ 不当成功、按时超时、不死循环
    def dead(t):
        raise RuntimeError("Execution context was destroyed")

    out, clk, btn = run_case(dead)
    check("S8 半死页面按时超时", out[0] == "timeout" and 14.5 <= clk.t <= 17.0
          and not btn.clicks, f"out={out} t={clk.t:.2f}")

    # S9 同 tick 双 toast（错误在前）→ 仍判上限
    out, clk, btn = run_case(
        lambda t: True,
        messages=windows((0.4, 3.4, "操作过于频繁"),
                         (0.4, 3.4, "提交字数超出每日上限")))
    check("S9 双toast优先上限", out[0] == "limit", f"out={out}")

    # S10 按钮一开始就不在 → 立即成功
    out, clk, btn = run_case(lambda t: False)
    check("S10 立即成功", out == "success" and clk.t < 0.5, f"t={clk.t:.2f}")

    # S11 导航瞬态异常后按钮消失 → 成功（异常≠消失，但恢复后 False 即成功）
    def navigating(t):
        if 0.5 <= t < 1.2:
            raise RuntimeError("Execution context was destroyed")
        return t < 0.5  # 新页面上按钮不存在

    out, clk, btn = run_case(navigating)
    check("S11 导航瞬态异常后成功", out == "success", f"out={out}")


def fuzz_tests(rounds=100):
    print(f"[随机模糊 {rounds} 轮]")
    benign = ["已自动保存", "草稿同步中", "番茄审核工作时间是7:00-24:00"]
    for i in range(rounds):
        rng = random.Random(20260606 + i)
        btn_hide_at = rng.uniform(0.2, 20.0) if rng.random() < 0.5 else None
        messages, notifications = [], []
        has_limit = has_error = False
        if rng.random() < 0.3:
            s = rng.uniform(0.2, 12.0)
            messages.append((s, s + rng.uniform(1.0, 3.0), "提交字数超出每日上限"))
            has_limit = True
        if rng.random() < 0.2:
            s = rng.uniform(0.2, 12.0)
            messages.append((s, s + rng.uniform(1.0, 3.0), "提交失败，请稍后再试"))
            has_error = True
        for _ in range(rng.randint(0, 3)):
            s = rng.uniform(0.0, 14.0)
            messages.append((s, s + rng.uniform(0.8, 2.5), rng.choice(benign)))
        if rng.random() < 0.3:
            notifications.append((0.0, 999.0, "系统公告：功能更新"))

        def visible(t, h=btn_hide_at):
            return h is None or t < h

        out, clk, btn = run_case(visible, messages, notifications)
        tag = out if isinstance(out, str) else out[0]
        # 不变量
        assert clk.t <= 18.0, f"fuzz#{i}: 未在预算内终止 t={clk.t:.2f}"
        assert len(btn.clicks) <= fu._RECLICK_MAX, f"fuzz#{i}: 重点击超限 {btn.clicks}"
        for ct in btn.clicks:
            assert ct <= 15.0 - fu._RECLICK_BUDGET_S + 0.6, \
                f"fuzz#{i}: 预算守卫失效 click@{ct:.2f}"
        if tag == "success":
            assert btn_hide_at is not None, f"fuzz#{i}: 按钮未消失却返回成功"
        elif tag == "limit":
            assert has_limit, f"fuzz#{i}: 无上限脚本却判上限"
        elif tag == "error":
            assert has_error, f"fuzz#{i}: 无错误脚本却判错误"
        elif tag == "timeout":
            assert btn_hide_at is None or btn_hide_at > 14.0, \
                f"fuzz#{i}: 按钮应消失({btn_hide_at})却超时"
    print(f"  PASS 全部 {rounds} 轮不变量")


def classify_unit_tests():
    print("[分类纯函数]")
    try:
        fu._classify_toasts(["操作过于频繁", "提交字数超出每日上限"])
        raise AssertionError("双toast未判上限")
    except fu.DailyLimitReached:
        pass
    try:
        fu._classify_toasts([], ["已到达当日发布字数上限"])
        raise AssertionError("notification上限未捕获")
    except fu.DailyLimitReached:
        pass
    fu._classify_toasts([], ["维护期间提交可能失败"])  # 常驻公告含敏感词不团灭
    try:
        fu._classify_toasts(["标题字数超出限制"])
        raise AssertionError("单章限制类错误未失败")
    except RuntimeError:
        pass
    # 2026-06-07 实测文案: 修改模式下本地标题与平台已有章节撞标题
    try:
        fu._classify_toasts(["本书中存在重复标题，请修改后再发布"])
        raise AssertionError("重复标题未失败")
    except fu.DailyLimitReached:
        raise AssertionError("重复标题误判为上限")
    except RuntimeError:
        pass
    fu._classify_toasts(["发布成功"], ["新功能上线"])
    print("  PASS 6 组分类断言")


if __name__ == "__main__":
    classify_unit_tests()
    scenario_tests()
    fuzz_tests(100)
    print(f"\nALL PASSED ({PASS} 场景断言 + 100 轮模糊 + 分类断言)")
