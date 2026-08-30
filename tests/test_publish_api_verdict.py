# -*- coding: utf-8 -*-
"""publish_article 接口响应判定测试（零依赖：不需要 pytest / playwright / 浏览器）。

背景（2026-06-26 真机抓包）：编辑/发布最终调 /api/author/publish_article/v0/，
业务结果藏在 HTTP 200 的 JSON body 的 `code` 字段里：
  成功: {"code":0,"data":{"item_id":"...","tips":""},"message":"success"}
  失败: {"code":-3026,"data":null,"message":"文章内容有大段落重复，请修改后提交"}
此前 _wait_publish_result 仅靠"确认发布按钮消失"判成功，无法区分 code!=0 的失败，
会把被拒章节误报成"成功"。本测试锁住"读 code 判定"这条新逻辑。

运行:  python tests/test_publish_api_verdict.py
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

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL [{name}] {detail}"
    PASS += 1
    print(f"  PASS {name}")


# 真机抓到的两条原始 body（保持原样，作为回归基准）
REAL_SUCCESS = ('{"code":0,"data":{"item_id":"7655447097667240472","tips":""},'
                '"log_id":"202606260542161C3C0ADA0B8C20684E51","message":"success"}\n')
REAL_FAIL_DUP = ('{"code":-3026,"data":null,'
                 '"log_id":"2026062605323013330AE51D93F7665C69",'
                 '"message":"文章内容有大段落重复，请修改后提交"}\n')


def interpret_unit_tests():
    print("[interpret 纯函数]")
    f = fu._interpret_publish_response

    # 真机成功
    v, m = f(REAL_SUCCESS)
    check("成功 code=0", v == "success", f"got={(v, m)}")

    # 真机失败（大段落重复）
    v, m = f(REAL_FAIL_DUP)
    check("失败 code=-3026", v == "fail" and "大段落重复" in m, f"got={(v, m)}")

    # code 为字符串 "0" 也算成功（防平台序列化差异）
    v, m = f('{"code":"0","message":"success"}')
    check("字符串0成功", v == "success", f"got={(v, m)}")

    # 每日上限类失败 → daily_limit（即便 code!=0）
    v, m = f('{"code":-5001,"data":null,"message":"已到达当日发布字数上限"}')
    check("上限归类daily_limit", v == "daily_limit", f"got={(v, m)}")
    v, m = f('{"code":-5001,"message":"提交字数超出每日上限"}')
    check("上限文案2归类daily_limit", v == "daily_limit", f"got={(v, m)}")

    # 每月上限（2026-07-25 真机实测接口原文）→ 同样归 daily_limit（中止整批、
    # 不重试）。此前漏在词库外被当普通 fail 重试，白占宝贵额度。
    v, m = f('{"code":-5001,"message":"提交字数超出每月上限"}')
    check("每月上限归类daily_limit", v == "daily_limit", f"got={(v, m)}")
    v, m = f('{"code":-5001,"message":"已达本月发布字数上限"}')
    check("本月上限归类daily_limit", v == "daily_limit", f"got={(v, m)}")

    # 非上限失败带文案
    v, m = f('{"code":-9,"message":"内容包含敏感词"}')
    check("普通失败带文案", v == "fail" and "敏感" in m, f"got={(v, m)}")

    # 失败但无 message → 仍 fail，message 兜底带上 code
    v, m = f('{"code":-9,"data":null}')
    check("失败无message兜底", v == "fail" and "-9" in m, f"got={(v, m)}")

    # 无法解析 / 非 publish 响应 → None（不参与判定，回退到原有按钮逻辑）
    for bad in ("", "not json", "null", "[]", "{}", '{"data":1}',
                '{"message":"ok"}'):
        v, m = f(bad)
        check(f"无code返回None ({bad[:12]!r})", v is None, f"got={(v, m)}")


# --------------------------------------------------------------------------
# 集成仿真：扩展 test_publish_result 的假 page，能在指定虚拟时刻触发
# publish_article 响应，驱动 _wait_publish_result 的真实判定。
# --------------------------------------------------------------------------
CDP = 0.03
TIMEOUT_MS = 15000


class VirtualClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


class _TimeShim:
    def __init__(self, clock):
        self.monotonic = clock.monotonic


class FakeResp:
    def __init__(self, url, status, body):
        self.url = url
        self.status = status
        self._body = body

    async def text(self):
        return self._body


class FakeBtn:
    def __init__(self, clock, visible_fn):
        self.clock = clock
        self.visible_fn = visible_fn
        self.clicks = []

    async def is_visible(self):
        self.clock.t += CDP
        return self.visible_fn(self.clock.t)

    async def click(self, **kw):
        self.clock.t += 0.05
        self.clicks.append(self.clock.t)

    async def evaluate(self, expr, timeout=None):
        self.clock.t += CDP
        return "<button class='arco-btn'>确认发布</button>"


class FakePage:
    """假 page：每次 evaluate(toast 轮询) 时，触发到点的 publish_article 响应。

    nav_at: 虚拟时刻——页面导航离开编辑器、URL 变为 chapter-manage
    （真机实测：提交成功后 SPA 跳回章节管理页）。None = 一直停在编辑器。
    """

    def __init__(self, clock, responses, nav_at=None, nav_url=None):
        self.clock = clock
        self._nav_at = nav_at
        self._nav_url = nav_url or "https://fanqienovel.com/main/writer/chapter-manage/123"
        self._handler = None
        self._responses = list(responses)  # [(t, FakeResp), ...]
        self._fired = set()

    @property
    def url(self):
        if self._nav_at is not None and self.clock.t >= self._nav_at:
            return self._nav_url
        return "https://fanqienovel.com/main/writer/test/publish"

    def _fire_due(self):
        for i, (t, resp) in enumerate(self._responses):
            if i not in self._fired and self.clock.t >= t:
                self._fired.add(i)
                if self._handler:
                    self._handler(resp)

    async def evaluate(self, js):
        self.clock.t += CDP
        self._fire_due()
        return {"messages": [], "notifications": []}

    async def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000
        # 响应任意时刻都可能到达（含按钮消失后的确认宽限窗，
        # 其间只调 wait_for_timeout 不调 evaluate）
        self._fire_due()
        # 让 _on_response 里 create_task 的 body 补抓任务有机会运行
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def on(self, event, handler):
        if event == "response":
            self._handler = handler

    def remove_listener(self, *a, **k):
        pass


def run_sim(visible_fn, responses, timeout_ms=TIMEOUT_MS, nav_at=None, nav_url=None):
    clock = VirtualClock()
    real_time = fu.time
    fu.time = _TimeShim(clock)
    try:
        page = FakePage(clock, responses, nav_at=nav_at, nav_url=nav_url)
        btn = FakeBtn(clock, visible_fn)

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


LOGIN_URL = "https://fanqienovel.com/login"


def integration_tests():
    print("[集成仿真]")
    URL = "https://fanqienovel.com/api/author/publish_article/v0/"

    # I1 接口先回 code!=0（按钮还在）→ 立即判失败，不等到超时
    out, clk, btn = run_sim(
        lambda t: True,
        [(0.5, FakeResp(URL, 200, REAL_FAIL_DUP))])
    check("I1 接口失败秒级捕获",
          out[0] == "error" and "大段落重复" in out[1] and clk.t <= 2.0,
          f"out={out} t={clk.t:.2f}")

    # I2 接口回 code!=0 上限 → DailyLimitReached
    out, clk, btn = run_sim(
        lambda t: True,
        [(0.5, FakeResp(URL, 200, '{"code":-5001,"message":"已到达当日发布字数上限"}'))])
    check("I2 接口上限→limit", out[0] == "limit" and clk.t <= 2.0, f"out={out}")

    # I3 接口回 code=0 即便按钮还没来得及消失也判成功
    out, clk, btn = run_sim(
        lambda t: True,  # 按钮永不消失
        [(0.5, FakeResp(URL, 200, REAL_SUCCESS))])
    check("I3 接口成功即判成功", out == "success" and clk.t <= 2.0,
          f"out={out} t={clk.t:.2f}")

    # I4 接口失败必须压过"按钮消失"假成功：按钮 1s 消失，但接口 0.5s 已回失败
    out, clk, btn = run_sim(
        lambda t: t < 1.0,
        [(0.5, FakeResp(URL, 200, REAL_FAIL_DUP))])
    check("I4 接口失败压过按钮消失", out[0] == "error", f"out={out}")

    # ---- 2026-07-24 契约收紧：按钮消失 ≠ 成功 ----
    # 定时发布 748 章漏 151 章的根因：对话框被异常关闭时按钮同样消失，
    # 章节没提交却被记"成功"（正文只留在自动草稿）。新契约：按钮消失后
    # 还须「接口 code=0」或「页面导航离开编辑器」二者其一确认。

    # I5 按钮消失、无接口、未导航（假成功场景）→ 必须判失败触发重试
    out, clk, btn = run_sim(lambda t: t < 1.0, [])
    check("I5 消失但未确认→失败",
          out[0] == "error" and "未获确认" in out[1], f"out={out}")

    # I6 按钮消失、无接口、随后导航离开编辑器 → 成功（真实成功的形态）
    out, clk, btn = run_sim(lambda t: t < 1.0, [], nav_at=1.5)
    check("I6 消失+导航→成功", out == "success", f"out={out}")

    # I7 按钮先消失，接口 code=0 在宽限窗内迟到 → 成功（在途响应不丢）
    out, clk, btn = run_sim(
        lambda t: t < 1.0,
        [(2.0, FakeResp(URL, 200, REAL_SUCCESS))])
    check("I7 迟到成功响应被采信", out == "success", f"out={out}")

    # I8 按钮先消失，接口迟到 code!=0 → 失败（假成功被接口揭穿）
    out, clk, btn = run_sim(
        lambda t: t < 1.0,
        [(2.0, FakeResp(URL, 200, REAL_FAIL_DUP))])
    check("I8 迟到失败响应压过消失",
          out[0] == "error" and "大段落重复" in out[1], f"out={out}")

    # I9 按钮先消失，接口迟到上限 → DailyLimitReached（上层中止整批）
    out, clk, btn = run_sim(
        lambda t: t < 1.0,
        [(2.0, FakeResp(URL, 200, '{"code":-5001,"message":"提交字数超出每日上限"}'))])
    check("I9 迟到上限响应→limit", out[0] == "limit", f"out={out}")

    # I10 改期流程形态：对话框开在 chapter-manage 页（非编辑器）→
    # 按钮消失即导航条件天然满足，行为与旧契约一致
    out, clk, btn = run_sim(lambda t: t < 1.0, [], nav_at=0.0)
    check("I10 chapter-manage页消失即成功", out == "success", f"out={out}")

    # I11 会话掉线：按钮消失后 URL 跳 /login（离开了编辑器但≠chapter-manage）
    # 绝不能当成功，否则又是静默漏章 → 判"未获确认"失败触发重试
    out, clk, btn = run_sim(lambda t: t < 1.0, [], nav_url=LOGIN_URL, nav_at=1.5)
    check("I11 掉登录页≠成功→失败",
          out[0] == "error" and "未获确认" in out[1], f"out={out}")


def success_url_unit_tests():
    print("[_is_publish_success_url 纯函数]")
    f = fu._is_publish_success_url
    check("chapter-manage是成功页",
          f("https://fanqienovel.com/main/writer/chapter-manage/123&%E4%B9%A6"))
    check("编辑器页不是成功页",
          not f("https://fanqienovel.com/main/writer/123/publish/?enter_from=x"))
    check("登录页不是成功页", not f("https://fanqienovel.com/login"))
    check("空串不是成功页", not f(""))
    check("None不是成功页", not f(None))


def modify_timer_verdict_tests():
    # --- 改期接口纳入权威判据（2026-08-20 第1396章 误判失败的回归） ---
    # 改期走 modify_timer，之前不在权威判据里，只剩"按钮消失"启发式：接口已 200、
    # toast 已"修改成功"，却因按钮没消失被判失败并白重试两次。
    import inspect as _inspect
    _src = _inspect.getsource(fu._wait_publish_result)
    _ok = ('"publish_article" in url or "modify_timer" in url' in _src
           or '"modify_timer" in url or "publish_article" in url' in _src)
    check("改期接口纳入权威判据", _ok)
    # 改期成功/失败响应用同一个解析器
    check("改期 code=0 判成功",
          fu._interpret_publish_response('{"code":0,"message":"success"}')[0] == "success")
    check("改期 code!=0 判失败",
          fu._interpret_publish_response(
              '{"code":-1,"message":"服务器开小差了，请稍后再试"}')[0] == "fail")


if __name__ == "__main__":
    interpret_unit_tests()
    success_url_unit_tests()
    integration_tests()
    modify_timer_verdict_tests()
    print(f"\nALL PASSED ({PASS} 断言)")
