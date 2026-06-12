# -*- coding: utf-8 -*-
"""_EXTRACT_ALL_JS 真实浏览器夹具测试。

与其他套件不同：本测试需要 Playwright 浏览器内核（chromium）。
内核缺失/启动失败时打印 SKIP 并以 0 退出，不阻塞零依赖套件约定。

覆盖：
1. 单页提取：章节号（含数字开头特殊章节的前视守卫——2024新春番外
   不得被误编号 2024）、状态、编辑链接、最新发布时间
2. 翻页：跨页重复行去重、页计数、next 按钮 disabled 终止

运行:  python tests/test_js_blocks_browser.py
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


PAGE1 = """
<html><body>
<table><tbody>
  <tr><th>标题</th><th>状态</th><th>时间</th></tr>
  <tr>
    <td>第1章 开端 <a href="/main/writer/123/publish/?chapter_id=11">编辑</a></td>
    <td>已发布</td><td>2026-06-01 08:00</td>
  </tr>
  <tr>
    <td>2024新春番外</td><td>待发布</td><td>2026-06-15 09:00</td>
  </tr>
  <tr>
    <td>300:遇见</td><td>待发布</td><td>2026-06-14 10:00</td>
  </tr>
</tbody></table>
</body></html>
"""

PAGED = """
<html><body>
<table id="t"><tbody id="tb">
  <tr><td>第1章 一</td><td>已发布</td><td>2026-06-01 08:00</td></tr>
  <tr><td>第2章 二</td><td>待发布</td><td>2026-06-02 08:00</td></tr>
</tbody></table>
<ul>
  <li class="arco-pagination-item">1</li>
  <li class="arco-pagination-item">2</li>
  <li class="arco-pagination-item-next" onclick="goPage2()">&gt;</li>
</ul>
<script>
function goPage2() {
  document.getElementById('tb').innerHTML =
    '<tr><td>第2章 二</td><td>待发布</td><td>2026-06-02 08:00</td></tr>' +
    '<tr><td>第3章 三</td><td>待发布</td><td>2026-06-03 08:00</td></tr>';
  var n = document.querySelector('.arco-pagination-item-next');
  n.classList.add('arco-pagination-item-disabled');
}
</script>
</body></html>
"""


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
        opts = {"waitTimeout": 3000, "maxTime": 15000}

        # ---- 1. 单页提取 ----
        page.set_content(PAGE1)
        r = page.evaluate(fu._EXTRACT_ALL_JS, opts)
        chs = r["chapters"]
        by_title = {c["title"]: c for c in chs}
        check("单页: 提取 3 行", len(chs) == 3, f"{[c['title'] for c in chs]}")
        check("单页: 第1章 编号 1",
              by_title.get("第1章 开端 编辑", {}).get("chapterNum") == 1
              or any(c["chapterNum"] == 1 for c in chs))
        bonus = next((c for c in chs if "番外" in c["title"]), None)
        check("单页: 番外不被误编号(前视守卫)",
              bonus is not None and bonus["chapterNum"] is None,
              f"chapterNum={bonus and bonus['chapterNum']}")
        c300 = next((c for c in chs if "遇见" in c["title"]), None)
        check("单页: '300:遇见' 编号 300",
              c300 is not None and c300["chapterNum"] == 300,
              f"chapterNum={c300 and c300['chapterNum']}")
        check("单页: 状态提取", bonus["status"] == "待发布",
              f"status={bonus['status']!r}")
        c1 = next((c for c in chs if c["chapterNum"] == 1), None)
        check("单页: 编辑链接提取",
              c1 is not None and c1["editUrl"]
              and "publish" in c1["editUrl"], f"editUrl={c1 and c1['editUrl']}")
        lp = r["lastPublish"]
        check("单页: 最新发布时间取最大",
              lp and lp["date"] == "2026-06-15" and lp["time"] == "09:00",
              f"lastPublish={lp}")

        # ---- 2. 翻页 + 跨页去重 ----
        page.set_content(PAGED)
        r = page.evaluate(fu._EXTRACT_ALL_JS, opts)
        chs = r["chapters"]
        nums = sorted(c["chapterNum"] for c in chs)
        check("翻页: 跨页去重后 3 章", nums == [1, 2, 3],
              f"nums={nums}, titles={[c['title'] for c in chs]}")
        check("翻页: 页计数 2", r["pageCount"] == 2,
              f"pageCount={r['pageCount']}")
        check("翻页: totalPages 解析 2", r["totalPages"] == 2,
              f"totalPages={r['totalPages']}")
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
