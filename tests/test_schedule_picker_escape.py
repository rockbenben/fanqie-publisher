# -*- coding: utf-8 -*-
"""定时发布填日期/时间时不得把「发布设置」弹窗自己关掉（零依赖仿真）。

复现 2026-09-03 那批：填完时间后无条件按 Escape，面板早已被 Enter 关上，
Escape 就穿透到 Arco Modal（escToExit 默认开）把「发布设置」关掉。~0.3s 后
弹窗卸载，正好撞上「确认发布」点击，日志只留下
    Locator.click: Timeout 15000ms exceeded ... element was detached from the DOM
419 次定时发布里 21 次中招，第1783章 三次重试全撞上 -> 整批中止、剩 3483 章没发。

运行:  python tests/test_schedule_picker_escape.py
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


def _match(has_text, text):
    """按 Playwright 语义判断 has_text 是否命中 text。

    str 是**子串**匹配，regex 按 search 匹配——不能写成等值比较：源码用
    _CONFIRM_SUBMIT_RE（regex）定位页脚提交按钮，等值比较会让这个假 page
    永远返回 0，测试就变成"因为找不到按钮所以没点"，反而掩盖真问题。
    """
    if has_text is None:
        return False
    if hasattr(has_text, "search"):
        return bool(has_text.search(text))
    return has_text in text


class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    async def count(self):
        return self.page._count(self.key)

    async def is_visible(self):
        return self.page._count(self.key) > 0

    async def click(self, timeout=None, **kw):
        await self.page._click(self.key, timeout)

    async def input_value(self):
        sel = self.key[0]
        if "请选择日期" in sel:
            return self.page.date
        if "请选择时间" in sel:
            return self.page.time
        return ""

    def locator(self, sub):
        return self


class FakePage:
    """「发布设置」弹窗 + 日期/时间面板的状态机。

    关键真实行为：Enter 会关掉面板；面板关着时按 Escape 关的是弹窗本身。
    submit_label 是页脚提交按钮的文案：2026-09 番茄由「确认发布」改成
    「确认提交」（issue #3），源码按 _CONFIRM_SUBMIT_RE 两种都认，所以这个
    假 page 也必须按 Playwright 语义（str 子串 / re search）匹配，不能写死等值。
    """

    def __init__(self):
        self.modal = True          # 「发布设置」弹窗是否还开着
        self.panel = None          # 当前打开的下拉面板: None/"date"/"time"
        self.date = self.time = ""
        self.focus = None
        self.published = False
        self.escapes = 0
        self.steal_focus = False   # 弹窗/公告抢焦点：键盘打不进输入框
        self.submit_label = "确认发布"

    def _is_submit(self, has_text):
        return has_text is not None and _match(has_text, self.submit_label)

    def _count(self, key):
        sel, has_text = key
        if not self.modal:
            return 0
        if sel == "text=发布设置":
            return 1
        if sel == "input[placeholder='请选择日期']":
            return 1
        if sel == "input[placeholder='请选择时间']":
            return 1
        if sel == "button" and self._is_submit(has_text):
            return 1
        return 0

    async def _click(self, key, timeout):
        sel, has_text = key
        if not self.modal:
            raise fu.PWTimeout(
                f"Locator.click: Timeout {timeout or 30000}ms exceeded.\n"
                "  - element was detached from the DOM, retrying")
        if sel == "input[placeholder='请选择日期']":
            self.focus, self.panel = "date", "date"
        elif sel == "input[placeholder='请选择时间']":
            self.focus, self.panel = "time", "time"
        if self.steal_focus:
            self.focus = None
        elif sel == "button" and self._is_submit(has_text):
            if self.panel:             # 面板没关会盖住页脚按钮
                raise fu.PWTimeout(
                    f"Locator.click: Timeout {timeout or 30000}ms exceeded.\n"
                    "  - <div class=arco-picker-dropdown> intercepts pointer events")
            self.published = True
            self.modal = False

    def locator(self, selector, has_text=None):
        return FakeLocator(self, (selector, has_text))

    async def evaluate(self, js, *args):
        if "定时发布" in js:
            return "clicked"
        return {"messages": [], "notifications": []}

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_selector(self, selector, timeout=None):
        if self._count((selector, None)):
            return
        raise fu.PWTimeout(f"Page.wait_for_selector: Timeout {timeout}ms exceeded.")

    @property
    def keyboard(self):
        return FakeKeyboard(self)

    @property
    def url(self):
        return "https://fanqienovel.com/main/writer/publish"


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        p = self.page
        if key == "Enter":
            p.panel = None                       # Enter 确认并关闭面板
        elif key == "Escape":
            p.escapes += 1
            if p.panel:
                p.panel = None
            else:
                p.modal = False                  # 穿透到 Modal —— 线上的坑
        elif key.endswith("+a"):
            pass

    async def type(self, text, delay=0):
        if self.page.focus == "date":
            self.page.date = text
        elif self.page.focus == "time":
            self.page.time = text


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def _run(page):
    async def _noop_wait(pg, btn, **kw):
        return
    orig = fu._wait_publish_result
    fu._wait_publish_result = _noop_wait
    try:
        await fu.publish_scheduled(page, "2027-02-16", "12:00")
    finally:
        fu._wait_publish_result = orig


def test_modal_survives_and_publishes():
    page = FakePage()
    err = None
    try:
        asyncio.run(_run(page))
    except Exception as e:
        err = e
    check("填完日期时间后弹窗还在", page.modal or page.published, f"err={err!r}")
    check("确认发布真的点下去了", page.published, f"err={err!r}")
    check("日期/时间填对了", (page.date, page.time) == ("2027-02-16", "12:00"),
          f"{page.date!r} {page.time!r}")
    check("全程没按过 Escape", page.escapes == 0, f"escapes={page.escapes}")


def test_stolen_focus_never_publishes():
    """真机实测：焦点被抢时 Enter 后输入框为空。必须在点确认前抛错，不能错发。"""
    page = FakePage()
    page.steal_focus = True
    err = None
    try:
        asyncio.run(_run(page))
    except Exception as e:
        err = e
    check("输入框为空 -> 抛错而非继续", isinstance(err, RuntimeError)
          and "没填上" in str(err), f"err={err!r}")
    check("输入框为空 -> 没点确认发布", not page.published)


def test_vanished_modal_reports_real_reason():
    """弹窗真的没了时，错误必须说人话，而不是甩一个 15s Timeout。"""
    page = FakePage()

    async def kill_then_run():
        # 弹窗在 count()>0 之后、click 之前被卸载（线上就是这个时序）
        orig_count = page._count
        def count_then_kill(key):
            n = orig_count(key)
            if key[0] == "button" and page._is_submit(key[1]):
                page.modal = False
            return n
        page._count = count_then_kill
        await fu._submit_confirm_publish(page)

    err = None
    try:
        asyncio.run(kill_then_run())
    except Exception as e:
        err = e
    check("弹窗消失 -> 报真因而非裸超时",
          "对话框在点击提交按钮前消失" in str(err), f"err={err!r}")


def test_new_confirm_submit_label():
    """2026-09 番茄改版：页脚按钮由「确认发布」改成「确认提交」（issue #3）。

    按死文案定位时 count()==0 -> 每章抛"未找到确认发布按钮" -> 整批中止
    （三条提交路径共用 _submit_confirm_publish）。定位改用 _CONFIRM_SUBMIT_RE
    后，新旧两种文案都必须能提交；同时也确认旧文案没被顺手丢掉——平台灰度
    或回滚任一版本都得能跑。
    """
    for label in ("确认提交", "确认发布"):
        page = FakePage()
        page.submit_label = label
        err = None
        try:
            asyncio.run(_run(page))
        except Exception as e:
            err = e
        # 只留这一条断言：失败路径下 published 必为 False，不存在"没跑也算过"。
        check(f"「{label}」文案下真的提交了", page.published, f"err={err!r}")


if __name__ == "__main__":
    print("定时发布 日期/时间面板 与 发布设置弹窗:")
    test_modal_survives_and_publishes()
    test_stolen_focus_never_publishes()
    test_vanished_modal_reports_real_reason()
    test_new_confirm_submit_label()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
