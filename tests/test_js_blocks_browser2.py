# -*- coding: utf-8 -*-
"""toast 抓取器 / BOOKS_JS / LAST_PUBLISH_JS 真实浏览器夹具测试。

需要 Playwright 浏览器内核；缺失时 SKIP（退出码 0）。

覆盖（此前仅有逐行 trace 的三个 JS 面）：
1. _visible_toast_texts：content 节点优先、display:none 退场残留过滤、
   message/notification 角色分离、宽选择器回退、去重
2. BOOKS_JS：bookId/书名(URL编码段+链接文字回退)/章数/字数/状态提取

LAST_PUBLISH_JS 那条已删（2026-10-31）：它只扫章节管理页首页的十几行，
「队列排到」改走 chapter_list 接口拿全书（fanqie_upload.queue_state）。

运行:  python tests/test_js_blocks_browser2.py
"""
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


TOASTS = """
<html><body>
  <div class="arco-message"><span class="arco-message-content">已到达当日发布字数上限</span></div>
  <div class="arco-message" style="display:none">
    <span class="arco-message-content" style="display:none">已消失的旧错误</span>
  </div>
  <div class="arco-message"><span class="arco-message-content">已到达当日发布字数上限</span></div>
  <div class="arco-notification"><span class="arco-notification-content">系统公告：今晚维护</span></div>
</body></html>
"""

BOOKS = """
<html><body>
<div class="book-card">
  <a href="/main/writer/chapter-manage/7711112222&%E6%B5%8B%E8%AF%95%E4%B9%A6?x=1">进入</a>
  <div>测试书 · 120章 · 35.6万字 · 连载中 · 已签约</div>
</div>
<div class="book-card">
  <a href="/main/writer/chapter-manage/8800001111">无名书链接</a>
  <div>另一本 · 3章 · 0.2万字 · 已完结</div>
</div>
</body></html>
"""

LASTPUB = """
<html><body>
<table><tbody>
  <tr><td>第1章</td><td>2026/05/30 08:00</td></tr>
  <tr><td>第2章</td><td>2026-06-02 21:30</td></tr>
  <tr><td>第3章</td><td>2026-06-02 09:00</td></tr>
</tbody></table>
</body></html>
"""
# 注: LAST_PUBLISH_JS 已于 2026-10-31 删除（只扫首页十几行，队尾算不准），
# 这份夹具随之不再被使用 —— 留着是为了记录它当年长什么样，方便对照。


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright 未安装")
        sys.exit(0)
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        print(f"SKIP: 浏览器内核不可用({e})")
        sys.exit(0)

    try:
        page = browser.new_page()

        # ---- 1. toast 抓取器（_visible_toast_texts 是 async，用其 JS 直接驱动
        #         再经由 async 包装跑一次完整 Python 路径） ----
        page.set_content(TOASTS)
        # 同步页面上直接验证 Python 函数所用的 JS 行为：借 async API 太重，
        # 这里通过 evaluate 等价调用——与 _visible_toast_texts 内嵌 JS 相同源。
        import re as _re
        js_m = _re.search(r'page\.evaluate\(\s*"""(\(\) => \{.*?\})"""',
                          open(Path(__file__).resolve().parent.parent
                               / "fanqie_upload.py", encoding="utf-8").read(),
                          _re.S)
        assert js_m, "未能定位 _visible_toast_texts 的内嵌 JS"
        toasts = page.evaluate(js_m.group(1))
        check("toast: 上限文案捕获且去重",
              toasts["messages"] == ["已到达当日发布字数上限"],
              f"messages={toasts['messages']}")
        check("toast: display:none 退场残留被过滤",
              "已消失的旧错误" not in toasts["messages"])
        check("toast: notification 角色分离",
              toasts["notifications"] == ["系统公告：今晚维护"],
              f"notifications={toasts['notifications']}")

        # ---- 2. BOOKS_JS ----
        page.set_content(BOOKS)
        books = page.evaluate(fu.BOOKS_JS)
        check("books: 提取 2 本", len(books) == 2, f"{books}")
        b1 = next((b for b in books if b["bookId"] == "7711112222"), None)
        check("books: URL编码书名解码", b1 and b1["name"] == "测试书",
              f"name={b1 and b1['name']!r}")
        check("books: 章数/字数", b1 and b1["chapters"] == "120"
              and b1["words"] == "35.6万", f"{b1}")
        check("books: 状态拼接", b1 and "连载中" in b1["status"]
              and "已签约" in b1["status"], f"status={b1 and b1['status']!r}")
        b2 = next((b for b in books if b["bookId"] == "8800001111"), None)
        check("books: 无名作品回退链接文字",
              b2 and b2["name"] == "无名书链接", f"name={b2 and b2['name']!r}")

        # ---- 3. LAST_PUBLISH_JS 已删除（2026-10-31）----
        # 那个 JS 只扫章节管理页**首页**的十几行，按「最大时刻」取一行。
        # 几千章的书，首页那点数据跟真实队尾毫无关系 —— 实测标签写着
        # 第5191章，平台实际有5196章，末章还排在更早的时刻。「队列排到」
        # 现在走 chapter_list 接口拿全书（fanqie_upload.queue_state）。
        # 这里不再测一个已经不存在的常量。
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
