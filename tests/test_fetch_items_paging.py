# -*- coding: utf-8 -*-
"""_FETCH_ITEMS_JS 真实浏览器夹具测试（并发翻页改造后的判据保护）。

2026-08-27 把逐页串行改成「一波并发取 WAVE 页」提速 8 倍。速度是次要的，
这里守的是那条铁律: **宁可报错也不能给半份章节列表** —— 对账拿这份当真相，
少几百章就等于告诉用户"平台上没有"，照单补传就是几百章永远移不回原位的重复。

覆盖: 全量拼接/顺序、平台压页大小、分页失效、中间空洞不截断、单页出错整体
报错、total 对不上报错、大页被拒退回 100、签名 URL 缺翻页参数。

需要 Playwright 浏览器内核；内核缺失时打印 SKIP 并以 0 退出。
运行:  python tests/test_fetch_items_paging.py
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
URL = "https://x/api/chapter/chapter_list?volume_id=7&page_index=0&page_count=15"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# 假服务端: 按 cfg 复现平台的各种脾气（压页大小、无视 page_index、单页报错…）
INSTALL = r"""(cfg) => {
    window.__cfg = cfg; window.__calls = [];
    let inflight = 0; window.__peak = 0;
    window.fetch = async (u) => {
        inflight++; window.__peak = Math.max(window.__peak, inflight);
        const pi = +(/page_index=(\d+)/.exec(u) || [])[1];
        let pc = +(/page_count=(\d+)/.exec(u) || [])[1];
        window.__calls.push(pi + ':' + pc);
        await new Promise(r => setTimeout(r, 5));
        inflight--;
        const c = window.__cfg;
        if (c.rejectAbove && pc > c.rejectAbove)
            return {ok: true, json: async () => ({code: -100, message: '页太大'})};
        if (c.errPage !== undefined && pi === c.errPage)
            return {ok: true, json: async () => ({code: -1, message: 'boom'})};
        if (c.cap) pc = Math.min(pc, c.cap);
        const idx = c.ignoreIndex ? 0 : pi;
        const list = [];
        if (c.hole === undefined || idx !== c.hole) {
            for (let i = idx * pc; i < Math.min((idx + 1) * pc, c.total); i++)
                list.push({item_id: 'id' + i, index: i + 1,
                           title: '第' + (i + 1) + '章 x', display_status: 1,
                           timer_time: 0, create_time: 0});
        }
        // 真接口任何页都回 total_count（2026-08-28 实测），假服务端照做，
        // 否则测出来的"翻完了"判据比生产宽松。
        const data = {item_list: list, total_count: c.total};
        if (c.reportTotal !== undefined) data.total_count = c.reportTotal;
        return {ok: true, json: async () => ({code: 0, data: data})};
    };
}"""


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
        page.goto("about:blank")

        def run(cfg, url=URL):
            page.evaluate(INSTALL, cfg)
            res = page.evaluate(fu._FETCH_ITEMS_JS, url)
            return (res, page.evaluate("window.__calls"),
                    page.evaluate("window.__peak"))

        # ---- 1. 全量拼接 + 顺序 + 真的是并发 ----
        r, calls, peak = run({"total": 1326})
        items = r.get("items") or []
        check("全量: 1326 章一条不少", len(items) == 1326,
              f"got={len(items)} err={r.get('error')}")
        check("全量: 顺序仍是升序（Promise.all 保序）",
              [it["index"] for it in items] == list(range(1, 1327)),
              f"head={[it['index'] for it in items[:3]]}")
        check("全量: 一波并发（往返数 <= 4）", len(calls) <= 4, f"calls={calls}")
        check("全量: 确实并发而非串行", peak > 1, f"peak={peak}")

        # ---- 2. 平台把页大小压回 100: 不能只取到半份 ----
        r, calls, _ = run({"total": 1326, "cap": 100})
        check("压页大小: 仍取满 1326（不因首页'短'收工）",
              len(r.get("items") or []) == 1326,
              f"got={len(r.get('items') or [])} calls={len(calls)}")

        # ---- 3. 服务端无视 page_index: 停下来，别空转到 MAX_PAGES ----
        r, calls, _ = run({"total": 1326, "ignoreIndex": True})
        check("分页失效: 察觉后停止（不空转）", len(calls) == 16, f"calls={len(calls)}")
        check("分页失效: 只抓到 500/1326 -> 报错而不是悄悄返回半份",
              r.get("error") and "500/1326" in r["error"] and not r.get("items"),
              f"r={str(r)[:140]}")

        # ---- 4. 中间空洞不得截断后面的章 ----
        r, _, _ = run({"total": 1326, "cap": 100, "hole": 2})
        check("中间空页: 不在空洞处截断（继续翻到第 13 页，凑到 1226）",
              r.get("error") and "1226/1326" in r["error"], f"r={str(r)[:140]}")
        check("中间空页: 少 100 章 -> 报错，绝不当成'平台上就这些'",
              not r.get("items"), f"r={str(r)[:140]}")

        # ---- 5. 任一页出错 = 整体报错（绝不给半份） ----
        r, _, _ = run({"total": 1326, "cap": 100, "errPage": 5})
        check("单页出错: 整体报错且不返回 items",
              r.get("error") and not r.get("items"), f"r={str(r)[:120]}")

        # ---- 6. 平台给了 total 就必须对上 ----
        r, _, _ = run({"total": 1326, "reportTotal": 2000})
        check("total 对不上: 拒绝返回半份",
              r.get("error") and "只取到" in r["error"], f"r={str(r)[:120]}")

        # ---- 7. 大页被整体拒绝 → 退回 100 仍要成功 ----
        r, calls, _ = run({"total": 1326, "cap": 100, "rejectAbove": 100})
        check("大页被拒: 自动退回 100 并取满",
              len(r.get("items") or []) == 1326,
              f"got={len(r.get('items') or [])} err={r.get('error')}")

        # ---- 8. 签名 URL 里没有翻页参数 = 报错，不是静默取一页 ----
        r, _, _ = run({"total": 50}, url="https://x/api/chapter/chapter_list?a=1")
        check("缺翻页参数: 报错而非静默 no-op",
              r.get("error") and "page_index" in r["error"], f"r={str(r)[:120]}")
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
