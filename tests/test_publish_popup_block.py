# -*- coding: utf-8 -*-
"""_navigate_to_publish_settings 弹窗遮挡仿真测试（零依赖：不需要 pytest / 浏览器）。

复现 2026-06 修改批次的 448 秒卡死：编辑器上出现「请在发布时间前30分钟提交
修改内容，否则无法完成修改」提示弹窗（按钮"我知道了"），遮罩挡住"下一步"，
每轮点击烧掉 Playwright 默认 30s 可操作性超时、异常被吞，14 轮 + 兜底 15s
≈ 448s 后才报"发布设置"超时，交由上层整章重试。

用虚拟时钟 + 假 page/locator 驱动真实状态机代码，验证：
1. 弹窗一开始就在 -> 点"我知道了"后快速到达发布设置（核心修复）
2. 弹窗在点"下一步"之后才弹 -> 同样被处理
3. 无弹窗正常流程不回归
4. 未知遮挡（没有"我知道了"按钮）-> 仍然失败，但快速失败而非 448s

运行:  python tests/test_publish_popup_block.py
"""
import asyncio
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402

HINT_TEXT = "请在发布时间前30分钟提交修改内容，否则无法完成修改"
# Playwright 未显式传 timeout 时的默认可操作性超时（线上烧掉 448s 的元凶）
PW_DEFAULT_CLICK_S = 30.0


class VirtualClock:
    def __init__(self):
        self.t = 0.0

    def advance(self, s):
        self.t += s


class FakeLocator:
    """按 (selector, has_text) 由 FakePage 动态求值的假 locator。"""

    def __init__(self, page, key):
        self.page = page
        self.key = key  # (selector, has_text)

    @property
    def first(self):
        return self

    async def count(self):
        self.page.clock.advance(0.03)
        return self.page._count(self.key)

    async def is_visible(self):
        self.page.clock.advance(0.03)
        return self.page._count(self.key) > 0

    async def click(self, timeout=None, **kw):
        await self.page._click(self.key, timeout)

    def locator(self, sub):
        # click_next_step 的 "visible=true" 兜底链
        return self


class FakePage:
    """模拟修改流程的页面状态机。

    stage: "editor" -> "settings"
    hint_visible: 提示弹窗（带"我知道了"按钮）是否正在遮挡页面
    unknown_overlay: 无任何已知按钮的未知遮挡层
    hint_after_next: 点"下一步"后才弹出提示弹窗（场景 2）
    """

    def __init__(self, clock, *, hint_visible=False, hint_after_next=False,
                 unknown_overlay=False):
        self.clock = clock
        self.stage = "editor"
        self.hint_visible = hint_visible
        self.hint_after_next = hint_after_next
        self.unknown_overlay = unknown_overlay
        self.hint_clicks = 0
        self.next_clicks = 0

    # -- 状态查询 -----------------------------------------------------------
    def _blocked(self):
        return self.hint_visible or self.unknown_overlay

    def _count(self, key):
        sel, has_text = key
        if sel == "text=发布设置":
            return 1 if self.stage == "settings" else 0
        if sel == "button.auto-editor-next":
            return 1 if self.stage == "editor" else 0
        if sel == "button" and has_text == "我知道了":
            return 1 if self.hint_visible else 0
        if sel == f"text={HINT_TEXT}" or (
                has_text is None and sel.startswith("text=请在发布时间前")):
            return 1 if self.hint_visible else 0
        # 其余（是否继续编辑/忽略全部/提交/取消/确认发布…）一律不存在
        return 0

    async def _click(self, key, timeout):
        sel, has_text = key
        if sel == "button" and has_text == "我知道了":
            self.clock.advance(0.05)
            self.hint_clicks += 1
            self.hint_visible = False
            return
        if sel == "button.auto-editor-next":
            if self._blocked():
                # 遮罩拦截点击：烧满超时后抛 PWTimeout
                burn = (timeout / 1000) if timeout else PW_DEFAULT_CLICK_S
                self.clock.advance(burn)
                raise fu.PWTimeout(
                    f"Locator.click: Timeout {timeout or 30000}ms exceeded.")
            self.clock.advance(0.05)
            self.next_clicks += 1
            if self.hint_after_next:
                # 平台对"下一步"的反应是弹出提示弹窗，而不是推进
                self.hint_after_next = False
                self.hint_visible = True
            else:
                self.stage = "settings"
            return
        self.clock.advance(0.05)

    # -- Page API -----------------------------------------------------------
    def locator(self, selector, has_text=None):
        return FakeLocator(self, (selector, has_text))

    async def evaluate(self, js, *args):
        self.clock.advance(0.03)
        # _visible_toast_texts 期望 dict；_apply_publish_options 忽略返回值
        return {"messages": [], "notifications": []}

    async def wait_for_timeout(self, ms):
        self.clock.advance(ms / 1000)

    async def wait_for_selector(self, selector, timeout=None):
        if selector == "text=发布设置" and self.stage == "settings":
            return
        self.clock.advance((timeout or 30000) / 1000)
        raise fu.PWTimeout(
            f"Page.wait_for_selector: Timeout {timeout}ms exceeded.\n"
            f"waiting for locator(\"{selector}\") to be visible")


def run_nav(page):
    """跑一轮真实状态机，返回 (异常或 None)。"""
    async def go():
        await fu._navigate_to_publish_settings(
            page, use_ai=False, draft_action="继续编辑")
    try:
        asyncio.run(go())
        return None
    except Exception as e:
        return e


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_hint_blocks_from_start():
    """场景 1（线上 298/299 章）：进状态机时提示弹窗已在 -> 必须点掉并快速完成。"""
    clock = VirtualClock()
    page = FakePage(clock, hint_visible=True)
    err = run_nav(page)
    check("弹窗预先存在: 到达发布设置", err is None, f"err={err!r}")
    check("弹窗预先存在: 点了我知道了", page.hint_clicks == 1,
          f"clicks={page.hint_clicks}")
    check("弹窗预先存在: 60s 内完成(线上为448s)", clock.t < 60,
          f"elapsed={clock.t:.1f}s")


def test_hint_after_next_click():
    """场景 2：点"下一步"之后平台才弹提示 -> 同样被状态机消化。"""
    clock = VirtualClock()
    page = FakePage(clock, hint_after_next=True)
    err = run_nav(page)
    check("弹窗后弹: 到达发布设置", err is None, f"err={err!r}")
    check("弹窗后弹: 点了我知道了", page.hint_clicks == 1,
          f"clicks={page.hint_clicks}")
    check("弹窗后弹: 60s 内完成", clock.t < 60, f"elapsed={clock.t:.1f}s")


def test_normal_flow_no_popup():
    """场景 3：无弹窗正常流程，不回归。"""
    clock = VirtualClock()
    page = FakePage(clock)
    err = run_nav(page)
    check("正常流程: 到达发布设置", err is None, f"err={err!r}")
    check("正常流程: 下一步只点一次", page.next_clicks == 1,
          f"clicks={page.next_clicks}")
    check("正常流程: 未点我知道了", page.hint_clicks == 0,
          f"clicks={page.hint_clicks}")


def test_unknown_overlay_fails_fast():
    """场景 4：未知遮挡层（没有我知道了按钮）-> 仍超时失败交上层重试，
    但应快速失败（<180s），不是线上那种 448s/章 的烧法。"""
    clock = VirtualClock()
    page = FakePage(clock, unknown_overlay=True)
    err = run_nav(page)
    check("未知遮挡: 仍以超时失败", isinstance(err, fu.PWTimeout),
          f"err={err!r}")
    check("未知遮挡: 快速失败(<180s, 线上448s)", clock.t < 180,
          f"elapsed={clock.t:.1f}s")


if __name__ == "__main__":
    print("== _navigate_to_publish_settings 弹窗遮挡仿真 ==")
    test_hint_blocks_from_start()
    test_hint_after_next_click()
    test_normal_flow_no_popup()
    test_unknown_overlay_fails_fast()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
