# -*- coding: utf-8 -*-
"""修改排期：失败章要进清单（章名+报错+原定时间），重试间隔要拉开。

起因 2026-09-04：1698 章改期只报"成功 1697 失败 1"，哪一章、为什么、原定几点
全得自己去 grep 日志。第2782章连吃三个服务端 toast（网络不好 / 服务器开小差
×2）——三次重试全挤在 12 秒内，等于一起撞同一个故障窗。

运行: python tests/test_reschedule_fail_report.py
"""
import asyncio
import logging
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


class _Cap(logging.Handler):
    """抓 logger 输出，验清单真的打出来了（不是只存在内存里）。"""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        self._lv = fu.logger.level
        fu.logger.setLevel(logging.DEBUG)
        fu.logger.addHandler(self)
        return self

    def __exit__(self, *a):
        fu.logger.removeHandler(self)
        fu.logger.setLevel(self._lv)

    @property
    def text(self):
        return "\n".join(self.lines)


print("== retry_wait_ms: 间隔拉开，越退越久 ==")
waits = [fu.retry_wait_ms(a) for a in (1, 2, 3)]
check("1s / 4s / 10s", waits == [1000, 4000, 10000])
check("超出表长取最后一项", fu.retry_wait_ms(9) == 10000)
check("单调不减", all(b >= a for a, b in zip(waits, waits[1:])))

SRC = Path(__file__).resolve().parent.parent.joinpath(
    "fanqie_upload.py").read_text(encoding="utf-8")
check("退避只有一处实现（四条重试路径共用同一个收尾）",
      SRC.count("await page.wait_for_timeout(retry_wait_ms(attempt))") == 1
      and SRC.count("await after_attempt_failed(") == 4)


# --------------------------------------------------------------------------
# 假页面：只实现 _reschedule_current_volume 真正用到的那几个动作
# --------------------------------------------------------------------------
class _Loc:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    @property
    def first(self):
        return self

    async def count(self):          # 只有翻页按钮会问 count → 单页表
        return 0

    async def wait_for(self, timeout=None, state=None):
        if state == "hidden":
            if self.page.dialog_open:
                raise TimeoutError("对话框没关掉")
            return
        if not self.page.dialog_open:
            raise TimeoutError("对话框没出现")

    async def is_visible(self):
        return self.page.dialog_open

    async def click(self, **kw):
        self.page.focus = self.kind

    async def input_value(self):
        return self.page.values.get(self.kind, "")


class _KB:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        if key.endswith("+a"):
            self.page.values[self.page.focus] = ""
        elif key == "Escape" and self.page.escape_works:
            self.page.dialog_open = False

    async def type(self, text, delay=0):
        self.page.values[self.page.focus] = \
            self.page.values.get(self.page.focus, "") + text


class _Page:
    """titles: 表格里的章节标题；escape_works=False 模拟对话框关不掉。"""

    def __init__(self, titles, escape_works=True):
        self.titles = titles
        self.escape_works = escape_works
        self.dialog_open = False
        self.focus = None
        self.values = {}
        self.waits = []             # wait_for_timeout 记录，用来看退避
        self.keyboard = _KB(self)
        self.url = "about:blank"

    async def goto(self, url):
        self.url = url

    async def wait_for_selector(self, sel, timeout=None):
        return True

    async def evaluate(self, js, arg=None):
        if js is fu._DETECT_CLOCK_ICON_JS:
            return "i.clock"
        if js is fu._CLICK_CLOCK_ICON_JS:
            self.values["current"] = arg    # 记下正在改哪一章
            self.dialog_open = True
            return True
        return list(self.titles)            # 扫描本页标题

    def locator(self, sel, has_text=None):
        if has_text == "确认修改":
            return _Loc(self, "confirm")
        if "日期" in sel:
            return _Loc(self, "date")
        if "时间" in sel:
            return _Loc(self, "time")
        return _Loc(self, "pager")

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)


TOAST = "发布失败，页面提示: 服务器开小差了，请稍后再试"
_REAL_WAIT = fu._wait_publish_result
_REAL_SETTLE = fu.settle_page


def _fake_submit(bad_title):
    """让 bad_title 每次都吃服务端 toast，其余章正常提交。"""
    async def fake_result(pg, btn, timeout=None):
        if pg.values.get("current") == bad_title:
            raise RuntimeError(TOAST)
        pg.dialog_open = False
    return fake_result


async def _noop_settle(page, **kw):
    return None


TITLES = ["第2781章 被子在洗衣机里", "第2782章 他叫我去后头吃饭",
          "第2783章 我女儿的事我不知道？"]
SMAP = {TITLES[0]: ("2027-08-01", "12:00"),
        TITLES[1]: ("2027-08-01", "12:01"),
        TITLES[2]: ("2027-08-01", "20:00")}


async def _run_volume(escape_works=True):
    page = _Page(TITLES, escape_works)
    fu._wait_publish_result = _fake_submit(TITLES[1])
    fail_list, remaining = [], dict(SMAP)
    try:
        s, f, aborted = await fu._reschedule_current_volume(
            page, remaining, len(SMAP), delay=0, fail_list=fail_list)
    finally:
        fu._wait_publish_result = _REAL_WAIT
    return page, fail_list, remaining, s, f, aborted


async def _run_outer():
    page = _Page(TITLES)
    fu._wait_publish_result = _fake_submit(TITLES[1])
    fu.settle_page = _noop_settle
    try:
        return await fu.reschedule_on_manage_page(page, "bid", SMAP, delay=0)
    finally:
        fu._wait_publish_result = _REAL_WAIT
        fu.settle_page = _REAL_SETTLE


print("== 失败章进清单：章名 + 报错 + 原定时间 ==")
page, fl, rem, s, f, aborted = asyncio.run(_run_volume())
check("成功 2 失败 1", (s, f) == (2, 1))
check("清单只有一条", len(fl) == 1)
entry = fl[0] if fl else ("", "")
check("条目里是失败那一章", entry[0] == TITLES[1])
check("带上平台报的原话", TOAST in entry[1])
check("带上原定时间（手改时照它填）", "原定 2027-08-01 12:01" in entry[1])
check("成功的章不进清单", all(e[0] != TITLES[0] for e in fl))
check("处理过的章都从 remaining 摘掉（失败的也算处理过）", rem == {})
check("没中止，后面的章照改", aborted is False)

print("== 重试间隔真的按退避表拉开 ==")
# 失败章 3 次尝试 → 2 次重试等待；成功的章不产生退避等待
check("重试等待 = 1s、4s",
      [w for w in page.waits if w in (1000, 4000, 10000)] == [1000, 4000])

print("== 对话框关不掉：中止且不重复记账 ==")
page2, fl2, rem2, s2, f2, aborted2 = asyncio.run(_run_volume(escape_works=False))
check("aborted=True", aborted2 is True)
check("本章留在 remaining 等调用方记未处理", TITLES[1] in rem2)
check("不在失败清单里重复记一遍", all(e[0] != TITLES[1] for e in fl2))
check("failed 没提前加", f2 == 0)

print("== 调用方：成功+失败 = 总数，清单一次汇报 ==")
with _Cap() as cap:
    ok, bad = asyncio.run(_run_outer())
check("成功+失败 = 总数", (ok, bad) == (2, 1) and ok + bad == len(SMAP))
check("完成横幅只有一处（CLI/GUI 都不再各抄一份）",
      SRC.count("修改排期完成!") == 1
      and "修改排期完成" not in Path(__file__).resolve().parent.parent.joinpath(
          "fanqie_gui.py").read_text(encoding="utf-8"))
check("横幅在前、失败清单在后（最该看的落在最后）",
      cap.text.index("修改排期完成") < cap.text.index("失败章节及原因"))
check("清单打进日志了", "失败章节及原因" in cap.text and TITLES[1] in cap.text)
check("日志里带原定时间", "原定 2027-08-01 12:01" in cap.text)
check("补救文案换成改期专用的（别把人引去「按章节号筛选」重发一遍）",
      "去平台按上面的时间逐章手改" in cap.text and "--chapters" not in cap.text)

print("== log_fail_list: 上传/修改的默认文案不变 ==")
with _Cap() as cap2:
    fu.log_fail_list([("第9章 x", "错")])
check("仍是补传文案", "--chapters" in cap2.text)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
