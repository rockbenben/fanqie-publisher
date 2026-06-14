# -*- coding: utf-8 -*-
"""clear_editor 清空校验 + fill_chapter 拼接拦截测试（零依赖）。

回归锁定（2026-06-12 线上实证）：修改模式下"我知道了"提示弹窗在
进编辑器之后、清空之前晚到，会抢走键盘焦点——Ctrl+A/Delete 全部打到
弹窗上、旧正文残留；fill_chapter 用 DOM 事件粘贴不受影响，结果发布出
"旧+新拼接"的章节。修复前弹窗还会挡住"下一步"导致 448s 超时→整章重试
把错误掩盖掉；弹窗自动点掉后掩护消失，拼接稿直接发布成功。

修复后：clear_editor 校验字数归零（未归零→点掉弹窗重试→仍失败抛错），
fill_chapter 字数校验升级为"不超过预期 1.5 倍 + 100"。

运行:  python tests/test_clear_editor_verify.py
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

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeKeyboard:
    """键盘事件打到当前焦点：弹窗在场时编辑器收不到。"""

    def __init__(self, page):
        self.page = page
        self.selected = False

    async def press(self, key):
        self.page.clock += 0.05
        if self.page.modal_visible:
            return  # 焦点在弹窗上，编辑器无感
        if key.endswith("+a"):
            self.selected = True
        elif key == "Delete" and self.selected:
            self.page.wc = 0
            self.selected = False


class FakeLocator:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind  # "wc" | "ack" | "none"

    @property
    def first(self):
        return self

    async def count(self):
        if self.kind == "wc":
            return 1
        if self.kind == "ack":
            return 1 if self.page.modal_visible else 0
        return 0

    async def is_visible(self):
        return await self.count() > 0

    async def text_content(self):
        return f"正文字数 {self.page.wc}"

    async def click(self, **kw):
        if self.kind == "ack" and not self.page.modal_undismissable:
            self.page.modal_visible = False


class FakePage:
    """修改模式编辑器页：wc=正文字数；modal=抢焦点的提示弹窗。"""

    def __init__(self, wc=2480, modal_visible=False, modal_undismissable=False,
                 paste_appends=False):
        self.wc = wc
        self.modal_visible = modal_visible
        self.modal_undismissable = modal_undismissable
        self.paste_appends = paste_appends  # 模拟清空失效后粘贴=拼接
        self.clock = 0.0
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector, has_text=None):
        if selector == "text=正文字数":
            return FakeLocator(self, "wc")
        if selector == "button" and has_text == "我知道了":
            return FakeLocator(self, "ack")
        return FakeLocator(self, "none")

    async def evaluate(self, js, *args):
        self.clock += 0.03
        # fill_chapter 的粘贴 JS：拼接模式下旧内容残留
        if args and isinstance(args[0], list) and len(args[0]) == 3:
            content = args[0][2]
            self.wc = (self.wc + len(content)) if self.paste_appends \
                else len(content)
        return None

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000


def run(coro):
    try:
        asyncio.run(coro)
        return None
    except Exception as e:
        return e


def main():
    # ---- clear_editor ----
    # 1) 弹窗抢焦点（线上实证场景）：必须点掉弹窗并清空成功
    p = FakePage(wc=2480, modal_visible=True)
    err = run(fu.clear_editor(p))
    check("弹窗抢焦点: 清空成功", err is None and p.wc == 0,
          f"err={err!r} wc={p.wc}")
    check("弹窗抢焦点: 弹窗已被点掉", not p.modal_visible)

    # 2) 无弹窗正常路径
    p = FakePage(wc=2480)
    err = run(fu.clear_editor(p))
    check("正常: 清空成功", err is None and p.wc == 0, f"err={err!r} wc={p.wc}")
    check("正常: 不拖慢(虚拟<5s)", p.clock < 5, f"clock={p.clock:.1f}s")

    # 3) 弹窗点不掉（键盘始终被挡）：必须抛错交上层整章重试，
    #    绝不能静默返回让拼接稿发布
    p = FakePage(wc=2480, modal_visible=True, modal_undismissable=True)
    err = run(fu.clear_editor(p))
    check("弹窗点不掉: 抛错而非静默", isinstance(err, RuntimeError),
          f"err={err!r} wc={p.wc}")

    # ---- fill_chapter 拼接拦截 ----
    content = "正文" * 1250  # len=2500
    # 4) 清空失效 → 粘贴成拼接(2480+2500)：必须报错拦下
    p = FakePage(wc=2480, paste_appends=True)
    err = run(fu.fill_chapter(p, None, "标题", content))
    check("拼接稿被拦截", isinstance(err, RuntimeError), f"err={err!r}")

    # 5) 正常粘贴(字数=内容长度)：通过
    p = FakePage(wc=0)
    err = run(fu.fill_chapter(p, None, "标题", content))
    check("正常粘贴通过", err is None, f"err={err!r}")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
