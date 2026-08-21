"""回归：goto_with_login_retry 须区分"瞬态误跳登录页"与"会话真失效"。

背景（实测 2026-06-10）：机器高负载时（如另一个 Playwright 应用满转），
作家后台 SPA 的鉴权请求（/api/user/info/v2 等）超时，前端把仍然有效的
会话误判为未登录并跳转 /login。修复前 GUI 见到 /login 立刻弹"登录失效"，
列表渲染超时则报"未找到作品，请检查登录状态"——都把负载问题误报成登录问题。

不启动真实浏览器：用桩 page 模拟各种跳转/超时序列。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as U

LOGIN = "https://fanqienovel.com/main/writer/login"
TARGET = "https://fanqienovel.com/main/writer/book-manage"


class _FakePage:
    """goto 后按预设序列落到某个 URL；可指定第 N 次 goto 抛超时。

    landing_urls 元素可为 (url, late_url) 元组：goto 先落在 url，
    观察窗(wait_for_url)期间再异步跳到 late_url——模拟 SPA 鉴权跳转
    晚于 domcontentloaded 的真实时序。
    """

    def __init__(self, landing_urls, timeout_on=()):
        self._urls = list(landing_urls)
        self._timeout_on = set(timeout_on)
        self.url = "about:blank"
        self._late_url = None
        self.goto_calls = 0
        self.waited_ms = 0

    # 真实签名带 timeout（goto 必须有上限，否则平台埋点会让 load 拖满默认 30s）
    async def goto(self, url, wait_until="load", timeout=None):
        self.goto_calls += 1
        landing = self._urls[self.goto_calls - 1]
        if isinstance(landing, tuple):
            self.url, self._late_url = landing
        else:
            self.url, self._late_url = landing, None
        if self.goto_calls in self._timeout_on:
            raise U.PWTimeout("goto timeout")

    async def wait_for_url(self, pattern, timeout=30000):
        # 观察窗：有迟到跳转则发生之，否则如实超时
        if self._late_url:
            self.url = self._late_url
            self._late_url = None
            return
        if "/login" in self.url:
            return
        raise U.PWTimeout("no redirect")

    async def wait_for_timeout(self, ms):
        self.waited_ms += ms


def test_transient_redirect_recovers():
    """第一次被误跳登录页，重试后进入目标页 → True，不误报失效。"""
    page = _FakePage([LOGIN, TARGET])
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is True, "瞬态误跳应在重试后恢复，不应判为会话失效"
    assert page.goto_calls == 2
    assert page.waited_ms >= 1000, "重试前应有退避等待"
    print("  PASS 瞬态误跳登录页 → 重试恢复，不误报失效")


def test_real_expiry_detected():
    """两次都被重定向到登录页 → False（会话真失效）。"""
    page = _FakePage([LOGIN, LOGIN])
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is False, "连续两次跳登录页应判为会话失效"
    assert page.goto_calls == 2
    print("  PASS 连续两次跳登录页 → 判为会话真失效")


def test_first_try_success_no_retry():
    """一次到位 → True，且不做多余重试/等待。"""
    page = _FakePage([TARGET])
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is True
    assert page.goto_calls == 1, "成功后不应再重试"
    assert page.waited_ms == 0, "成功后不应有退避等待"
    print("  PASS 首次成功 → 无多余重试")


def test_goto_timeout_not_failure():
    """goto 超时但最终落在目标页 → True（超时不等于失败，以 URL 为准）。"""
    page = _FakePage([TARGET], timeout_on={1})
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is True, "goto 超时但已在目标页，不应判失败"
    assert page.goto_calls == 1
    print("  PASS goto 超时但落在目标页 → 不误判")


def test_timeout_then_login_then_recover():
    """第一次超时且跳登录页，重试成功 → True。"""
    page = _FakePage([LOGIN, TARGET], timeout_on={1})
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is True
    assert page.goto_calls == 2
    print("  PASS 超时+误跳后重试恢复")


def test_late_redirect_caught():
    """domcontentloaded 时还在目标页、随后才异步跳 /login —— 不能放行。

    实测发现的竞态：原代码在 goto 返回后立刻看 URL，会把"即将跳登录页"
    误判为成功。修复后观察窗内的迟到跳转应触发重试。
    """
    # 第一次: 落在目标页但随后跳登录; 第二次: 真正进入目标页
    page = _FakePage([(TARGET, LOGIN), TARGET])
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is True, "迟到跳转后的重试应恢复"
    assert page.goto_calls == 2, "迟到跳转必须被观察窗捕获并触发重试"
    print("  PASS 迟到的 /login 跳转被观察窗捕获 → 重试恢复")


def test_late_redirect_persistent_expiry():
    """两次都迟到跳登录页 → False（会话真失效，且跳转晚于加载完成）。"""
    page = _FakePage([(TARGET, LOGIN), (TARGET, LOGIN)])
    ok = asyncio.run(U.goto_with_login_retry(page, TARGET))
    assert ok is False
    print("  PASS 持续迟到跳转 → 判为会话真失效")


def test_landing_path_variants():
    """落地 URL 的各种变形都要认作「到了目标页」。

    goto 加了超时上限后可能在导航提交之前就超时，此时 page.url 还停在上一页，
    而「不在登录页」不等于「到了目标页」——所以要比 path。但这个比对容易收得太紧:
    **番茄会在 path 后面拼上 &书名**（2026-08-21 真机实测）。早先只允许后面跟 "/"，
    于是把正常导航判成失败，连带批末对账那道安全网一起挂掉，还报成「会话失效」
    ——而登录好好的、章节也确实发成功了。
    """
    A = U._at_target_path
    W = "https://fanqienovel.com/main/writer/chapter-manage/7613749318914149401"
    REAL = W + "&%E8%AF%B8%E5%A4%A9%E9%83%BD%E5%B8%82%E5%89%A7%E4%BB%8E%E5%87%A1%E4%BA%BA%E6%AD%8C%E5%BC%80%E5%A7%8B"
    cases = [
        (REAL, W, True, "真机: path 后拼了 &书名"),
        (W, W, True, "完全相同"),
        (W + "?tab=1", W, True, "带 query"),
        (W + "/sub", W, True, "子路径"),
        ("https://fanqienovel.com/main/writer/7613749318914149401/publish/",
         W, False, "还停在编辑器（就是要抓的那个 case）"),
        (W + "2", W, False, "另一本书(id 多一位)不能误匹配"),
        ("", W, False, "空 URL"),
    ]
    for cur, want, exp, name in cases:
        got = A(cur, want)
        assert got is exp, f"FAIL [{name}] {got} != {exp}"
        print(f"  [PASS] {name}")


def main():
    test_transient_redirect_recovers()
    test_real_expiry_detected()
    test_first_try_success_no_retry()
    test_goto_timeout_not_failure()
    test_timeout_then_login_then_recover()
    test_late_redirect_caught()
    test_late_redirect_persistent_expiry()
    test_landing_path_variants()
    print("\nALL PASSED (login-redirect retry)")


if __name__ == "__main__":
    main()
