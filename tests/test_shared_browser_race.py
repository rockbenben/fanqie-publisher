"""回归：_SharedBrowser.ensure() 并发不得重复启动浏览器（资源泄漏）。

不启动真实浏览器：把 fanqie_gui 模块命名空间里的 async_playwright /
create_context / close_browser_safely 替换成会在 await 点让出的桩，使两个
并发 ensure() 像在真实 worker 事件循环上那样交错。

修复前：两个并发 ensure() 会各 start() 一套 playwright+chromium，只有最后
赋值的被记录，另一套子进程成为孤儿（连 _on_close 都关不到）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_gui as G

counts = {"pw": 0, "browser": 0}


class _FakePW:
    async def start(self):
        counts["pw"] += 1
        await asyncio.sleep(0)        # 让出 -> 另一个 ensure 得以并发推进
        return self

    async def stop(self):
        return None


class _FakeBrowser:
    def is_connected(self):
        return True


async def _fake_create_context(pw, headless=True):
    counts["browser"] += 1
    await asyncio.sleep(0)            # 第二个 await 让出点
    return _FakeBrowser(), object()


async def _fake_close(browser, timeout_s=30):
    return None


async def _scenario():
    G.async_playwright = lambda: _FakePW()
    G.create_context = _fake_create_context
    G.close_browser_safely = _fake_close

    sb = G._SharedBrowser()
    # 两个首次 ensure 并发（实测可达：登录后刷新作品列表 + 切到修改模式拉章节）
    await asyncio.gather(sb.ensure(), sb.ensure())

    leaked = counts["browser"] - 1   # 只应记录 1 套，其余为泄漏
    assert leaked == 0, (
        f"RACE: ensure() 创建了 {counts['browser']} 套浏览器但只跟踪 1 套，"
        f"泄漏 {leaked} 套")
    assert counts["pw"] == 1, f"playwright.start() 调用 {counts['pw']} 次（应为 1）"
    assert sb._browser is not None and sb._browser.is_connected()


def main():
    asyncio.run(_scenario())
    print("  PASS 并发 ensure() 只启动一套浏览器，无泄漏")
    print("\nALL PASSED (shared-browser race)")


if __name__ == "__main__":
    main()
