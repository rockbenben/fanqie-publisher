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
    """假 page：每次 evaluate(toast 轮询) 时，触发到点的 publish_article 响应。"""

    def __init__(self, clock, responses):
        self.clock = clock
        self.url = "https://fanqienovel.com/main/writer/test/publish"
        self._handler = None
        self._responses = list(responses)  # [(t, FakeResp), ...]
        self._fired = set()

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
        # 让 _on_response 里 create_task 的 body 补抓任务有机会运行
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def on(self, event, handler):
        if event == "response":
            self._handler = handler

    def remove_listener(self, *a, **k):
        pass


def run_sim(visible_fn, responses, timeout_ms=TIMEOUT_MS):
    clock = VirtualClock()
    real_time = fu.time
    fu.time = _TimeShim(clock)
    try:
        page = FakePage(clock, responses)
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

    # I5 无接口响应（旧路径）→ 仍按按钮消失判成功，保持向后兼容
    out, clk, btn = run_sim(lambda t: t < 1.0, [])
    check("I5 无接口回退按钮逻辑", out == "success", f"out={out}")


if __name__ == "__main__":
    interpret_unit_tests()
    integration_tests()
    print(f"\nALL PASSED ({PASS} 断言)")
