# -*- coding: utf-8 -*-
"""草稿箱清理工具 —— 批量删除草稿箱里的草稿。

为什么会堆一堆草稿:
  番茄在编辑器里填正文时会自动存草稿。定时发布曾有"确认发布按钮消失=成功"的
  假成功 bug，每次假成功都留下一份孤儿草稿（748 章那次漏 151 章，草稿箱同源
  堆积）。这些草稿的内容本地都有源文件，删掉不丢东西。

安全性:
  · 默认 dry-run，要真删必须显式 --run。
  · 删之前强制做一次**安全检查**: 每条草稿的章号必须在本地目录里有对应文件，
    否则说明这份内容只存在于草稿里，删了就没了 —— 有任何一条对不上就拒绝执行
    （除非 --force 明确覆盖）。
  · 只删草稿表（表头含"修改时间"那张），绝不碰上面的章节表。
  · 每删一条都等首行换人（分页表行数恒定，只能看身份），换不动就停
    —— 避免"点了没反应"还一直空点。

用法:
  python tools/clean_drafts/clean_drafts.py                # 预览+安全检查
  python tools/clean_drafts/clean_drafts.py --limit 1 --run  # 灰度删 1 条
  python tools/clean_drafts/clean_drafts.py --run          # 全删
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = Path(__file__).resolve().parent / "logs"   # --daily 的日志落这里
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

# 草稿表 = 表头含"修改时间"的那张；每行操作列有 .icon-delete.tomato-delete
_DELETE_FIRST_JS = r"""() => {
    const tables = [...document.querySelectorAll('table')];
    const di = tables.findIndex(tb => [...tb.querySelectorAll('th')]
        .some(e => e.textContent.trim() === '修改时间'));
    if (di < 0) return {ok: false, why: '找不到草稿表'};
    const tr = tables[di].querySelector('tbody tr');
    if (!tr) return {ok: false, why: '草稿表没有行'};
    const name = (tr.querySelector('td') || {}).innerText || '';
    for (const t of ['mouseover', 'mouseenter', 'mousemove'])
        tr.dispatchEvent(new MouseEvent(t, {bubbles: true}));
    const btn = tr.querySelector('.icon-delete, .tomato-delete');
    if (!btn) return {ok: false, why: '该行没有删除按钮'};
    // 用首行的 item_id 作身份：分页表删掉一行会自动补下一行进来，
    // 行数恒定不变，只能靠"首行换人了"来判断删除是否生效。
    const link = tr.querySelector('a[href*="/publish/"]');
    const id = link ? (link.getAttribute('href').match(/publish\/(\d+)/) || [])[1]
                    : null;
    btn.click();
    return {ok: true, name: name.trim().slice(0, 30), id: id};
}"""


# 等首行换成别的草稿（分页表会自动补行，行数不变，只能看身份变没变）
_WAIT_ROW_GONE_JS = r"""async (beforeId) => {
    const firstId = () => {
        const tables = [...document.querySelectorAll('table')];
        const di = tables.findIndex(tb => [...tb.querySelectorAll('th')]
            .some(e => e.textContent.trim() === '修改时间'));
        if (di < 0) return 'no-table';
        const tr = tables[di].querySelector('tbody tr');
        if (!tr) return 'empty';
        const a = tr.querySelector('a[href*="/publish/"]');
        return a ? (a.getAttribute('href').match(/publish\/(\d+)/) || [])[1] : null;
    };
    const t0 = Date.now();
    while (Date.now() - t0 < 8000) {
        const cur = firstId();
        if (cur === 'empty') return true;          // 删空了也算成功
        if (cur && cur !== beforeId) return true;  // 首行换人 = 上一条已删掉
        await new Promise(r => setTimeout(r, 200));
    }
    return false;
}"""

# 确认弹窗：点其中的确认按钮（不同文案都兜住，但绝不点"取消"）
_CONFIRM_JS = r"""() => {
    const dlgs = [...document.querySelectorAll(
        '.arco-modal, .byte-modal, [role="dialog"]')]
        .filter(d => d.getClientRects().length);
    for (const d of dlgs) {
        for (const b of d.querySelectorAll('button')) {
            const t = (b.textContent || '').trim();
            if (t && /^(确定|确认|删除|确认删除|是)$/.test(t)) {
                b.click();
                return {clicked: t};
            }
        }
    }
    return {clicked: null,
            texts: dlgs.map(d => d.innerText.replace(/\s+/g, ' ').slice(0, 120))};
}"""


def local_chapter_nums(content_dir):
    """本地有哪些章号（安全检查用）。索引本身走共用实现。"""
    return set(fu.local_chapter_index(content_dir))


def safety_check(drafts, local_nums):
    """删之前确认没有"只存在于草稿里"的内容。返回 (ok, 说明, 风险条目)。"""
    risky = []
    for d in drafts:
        n = fu.chapter_title_num(d.get("title"))
        if n is None:
            # 没有章号的草稿：0 字的是空壳，可删；有字的说明是别处没有的稿子
            if d.get("word_number"):
                risky.append(f"「{d.get('title', '')[:20]}」{d.get('word_number')}字")
        elif n not in local_nums:
            risky.append(f"第{n}章（本地无此文件）")
    if risky:
        return False, f"{len(risky)} 条草稿的内容本地没有", risky
    return True, "所有草稿的内容本地都有源文件，删除不丢东西", []


async def open_draft_box(page, book_id):
    if not await fu.goto_with_login_retry(
            page, fu.CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)):
        raise RuntimeError("会话失效，请先重新登录")
    await page.wait_for_timeout(2500)
    await page.evaluate("""() => {
        for (const el of document.querySelectorAll('*'))
            if (el.children.length === 0 &&
                (el.textContent || '').trim() === '草稿箱') { el.click(); return; }
    }""")
    await page.wait_for_timeout(3000)


async def delete_drafts(page, book_id, want):
    """留在草稿箱页面连续删除；用 DOM 行数验证每次删除真的生效。

    早先每删一条都重新导航整页 + 重抓全量草稿列表来"确认总数减少"，
    单条开销 15~20 秒（删除本身只要 1 秒）。删除后表格会就地重渲染，
    直接看行数变化就够了——整页导航和全量列表只在翻页/收尾时才需要。
    返回 (删掉数, 停止原因)。
    """
    done = 0
    while done < want:
        r = await page.evaluate(_DELETE_FIRST_JS)
        if not r.get("ok"):
            # 本页删空了：翻回草稿箱拿下一页；仍没有就是真删完了
            await open_draft_box(page, book_id)
            r2 = await page.evaluate(_DELETE_FIRST_JS)
            if not r2.get("ok"):
                return done, r2.get("why", "没有可删的行")
            r = r2
        await page.wait_for_timeout(400)
        await page.evaluate(_CONFIRM_JS)
        # 等这一行真的消失（行数减少），而不是盲等固定时间
        gone = await page.evaluate(_WAIT_ROW_GONE_JS, r.get("id"))
        if not gone:
            return done, (f"删「{r.get('name')}」后首行没变，停止以免空点")
        done += 1
        if done % 20 == 0 or done == want:
            print(f"  已删 {done}/{want}  最近一条「{r.get('name')}」", flush=True)
    return done, ""


async def main_async(args):
    book_id, num2path, headless = fu.tool_startup(args)
    if not book_id:
        return False
    local_nums = set(num2path)
    async with async_playwright() as p:
        browser, ctx = await fu.create_context(p, headless=headless)
        page = await ctx.new_page()
        try:
            drafts, total = await fu.fetch_draft_list(page, book_id)
            print(f"草稿箱 {total} 条", flush=True)
            ok, why, risky = safety_check(drafts, local_nums)
            print(("✓ " if ok else "⚠ ") + why, flush=True)
            for r in risky[:10]:
                print(f"    {r}", flush=True)
            if not ok and not args.force:
                print("已中止，未删除任何东西。确认要删就加 --force。", flush=True)
                return True
            want = min(args.limit or total, total)
            if not args.run:
                print(f"\n[dry-run] 本次将删除 {want} 条。加 --run 才真删。",
                      flush=True)
                return
            print(f"\n开始删除 {want} 条 —— 3 秒后开始…", flush=True)
            await page.wait_for_timeout(3000)
            await open_draft_box(page, book_id)
            done, stop = await delete_drafts(page, book_id, want)
            print("=" * 50, flush=True)
            print(f"已删除 {done} 条" + (f"；提前停止: {stop}" if stop else ""),
                  flush=True)
            _d, total_after = await fu.fetch_draft_list(page, book_id)
            print(f"复核: 草稿箱现有 {total_after} 条（删前 {total}）", flush=True)
        finally:
            await fu.close_browser_safely(browser)


def demo():
    """零依赖自检: 安全检查的判据。"""
    local = {1, 2, 3}
    d = lambda t, w=100: {"title": t, "word_number": w}
    ok, _why, _r = safety_check([d("第1章 甲"), d("第2章 乙")], local)
    assert ok, "本地都有就该放行"
    ok, _why, risky = safety_check([d("第9章 丙")], local)
    assert not ok and "第9章" in risky[0], risky      # 本地没有 → 拦
    ok, _why, _r = safety_check([d("未命名草稿", 0)], local)
    assert ok, "0 字空草稿可删"
    ok, _why, risky = safety_check([d("未命名草稿", 500)], local)
    assert not ok, "有字但无章号的草稿是别处没有的内容，必须拦"
    print("demo ok")


def main():
    ap = argparse.ArgumentParser(description="清空草稿箱")
    ap.add_argument("--run", action="store_true", help="真的删除（不加只预览）")
    ap.add_argument("--limit", type=int, help="本次最多删多少条")
    ap.add_argument("--force", action="store_true",
                    help="安全检查不通过也照删（会丢内容，慎用）")
    ap.add_argument("--book-id", type=str)
    ap.add_argument("--content-dir", type=str)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        demo()
        return
    fu.setup_logging()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
