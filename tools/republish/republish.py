# -*- coding: utf-8 -*-
"""缺章补发工具 —— 把定时发布中段漏掉的章节，按原排期节奏补回平台。

背景: 定时发布曾因"确认发布按钮消失=成功"的假成功 bug 批量漏章（一本书
748 章漏 151 章，草稿箱同源堆积；根因与修复见 fanqie_upload.py 的
_await_submit_confirmation）。本工具用于补回这些缺口。

设计要点（防重复、可分月安全重跑）:
  1. 每次运行先抓平台真实章节列表，只补"平台上确实还没有"的章 —— 已补的
     自动跳过，重跑绝不产生重复章。
  2. 缺口的补发时间按番茄原排期的 9 槽位网格（07:00/:01/:02、12:00/:01/:02、
     20:00/:01/:02）在前后邻章之间填空算出，保证插到正确阅读位置、时间单调。
     槽位数与缺章数不符时退化为"邻章之间均匀插值"并告警。
  3. 撞每月/每日字数上限自动中止并如实记账；下月重跑会重新抓平台、从断点
     继续（跳过本月已补的）。

用法:
  python tools/republish/republish.py --dry-run          # 只看缺哪些+补发时间，不发布
  python tools/republish/republish.py --only 709         # 灰度单章
  python tools/republish/republish.py --range 709-1073   # 指定章号区间
  python tools/republish/republish.py --limit 300        # 本次最多补 300 章
  python tools/republish/republish.py --all              # 补全部当前缺口

book_id 默认取 .gui_state.json 的 last_book_id，content 目录默认取 config.json
的 chapters_dir，均可用 --book-id / --content-dir 覆盖。
"""
import argparse
import asyncio
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # .../fanqie
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

HERE = Path(__file__).resolve().parent
PLAN_SNAPSHOT = HERE / "republish_plan.json"

# 缺口排期核心（槽位网格填空 + 插值兜底）下沉到 fanqie_upload，CLI 与 GUI 共用。
compute_gap_schedule = fu.compute_gap_schedule

# 按行抓取平台章节: 章节号 / 标题 / 状态 / 日期 / 时间，逐页翻。
_EXTRACT_ROWS_JS = r"""async (opts) => {
    const MAX_TIME = (opts && opts.maxTime) || 240000;
    const t0 = Date.now();
    while (!document.querySelector('tr td')) {
        if (Date.now() - t0 > 15000) break;
        await new Promise(r => requestAnimationFrame(r));
    }
    const rows = [];
    const seen = new Set();
    const dateRe = /(\d{4}[-\/]\d{2}[-\/]\d{2})\s+(\d{2}:\d{2})/;
    const start = Date.now();
    for (let i = 0; i < 500 && Date.now() - start < MAX_TIME; i++) {
        let newCount = 0;
        for (const row of document.querySelectorAll('tr')) {
            const cells = row.querySelectorAll('td');
            if (cells.length < 2) continue;
            const title = cells[0].textContent.trim();
            if (!title) continue;
            let chapterNum = null;
            let m = title.match(/^第\s*(\d+)\s*[章回节话]/);
            if (m) chapterNum = parseInt(m[1], 10);
            else { m = title.match(/^(\d+)(?=$|[\s:：_\-.、章回节话])/);
                   if (m) chapterNum = parseInt(m[1], 10); }
            const key = chapterNum + '|' + title;
            if (seen.has(key)) continue;
            seen.add(key);
            let status = '';
            for (let ci = 1; ci < cells.length; ci++) {
                const ct = cells[ci].textContent.trim();
                if (/待发布|已发布|审核中|草稿|已拒绝/.test(ct)) { status = ct; break; }
            }
            const dm = row.textContent.match(dateRe);
            rows.push({ chapterNum, title, status,
                        date: dm ? dm[1].replace(/\//g,'-') : null,
                        time: dm ? dm[2] : null });
            newCount++;
        }
        let nextBtn = document.querySelector(
            'li.arco-pagination-item-next:not(.arco-pagination-item-disabled)');
        if (!nextBtn) break;
        const firstTitle = document.querySelector('tr td')?.textContent?.trim() || '';
        nextBtn.click();
        await new Promise(resolve => {
            const deadline = Date.now() + 8000;
            (function check() {
                const c = document.querySelector('tr td')?.textContent?.trim() || '';
                if ((c && c !== firstTitle) || Date.now() > deadline) { resolve(); return; }
                requestAnimationFrame(check);
            })();
        });
        if (newCount === 0 && i > 0) break;
    }
    return rows;
}"""


# --------------------------------------------------------------------------
# 配置 / 本地文件
# --------------------------------------------------------------------------
def load_defaults():
    book_id, content_dir = None, None
    gs = ROOT / ".gui_state.json"
    if gs.exists():
        try:
            book_id = json.loads(gs.read_text(encoding="utf-8")).get("last_book_id")
        except Exception:
            pass
    cfg = ROOT / "config.json"
    if cfg.exists():
        try:
            content_dir = json.loads(cfg.read_text(encoding="utf-8")).get("chapters_dir")
        except Exception:
            pass
    return book_id, content_dir


def map_local_files(content_dir):
    num2path = {}
    for f in glob.glob(os.path.join(content_dir, "**", "chapter-*.md"), recursive=True):
        m = re.search(r"chapter-(\d+)\.md$", os.path.basename(f))
        if m:
            num2path[int(m.group(1))] = f
    return num2path


# --------------------------------------------------------------------------
# 缺口排期计算
# --------------------------------------------------------------------------
def build_plan(rows, num2path):
    """组装补发计划: [{num,date,time,path,has_file}]，按章号升序。"""
    assign, warnings = compute_gap_schedule(rows)
    plan = []
    for num in sorted(assign):
        d, t = assign[num]
        plan.append({"num": num, "date": d, "time": t,
                     "path": num2path.get(num),
                     "has_file": num in num2path})
    return plan, warnings


# --------------------------------------------------------------------------
# 浏览器: 抓平台 + 补发
# --------------------------------------------------------------------------
async def scrape_platform(page, book_id):
    url = fu.CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)
    if not await fu.goto_with_login_retry(page, url):
        raise RuntimeError("会话失效，请先在工具里重新 login")
    await page.wait_for_timeout(2000)
    return await page.evaluate(_EXTRACT_ROWS_JS, {"maxTime": 240000})


def select_entries(plan, args):
    present_ok = [e for e in plan if e["has_file"]]
    if args.only is not None:
        return [e for e in present_ok if e["num"] == args.only]
    if args.range:
        a, b = (int(x) for x in args.range.split("-"))
        present_ok = [e for e in present_ok if a <= e["num"] <= b]
    if args.limit:
        present_ok = present_ok[:args.limit]
    return present_ok


async def republish(page, entries, book_id):
    new_url = fu.NEW_CHAPTER_URL_TPL.format(book_id=book_id)
    ok_list, fail_list = [], []
    for idx, e in enumerate(entries):
        num, date_str, time_str, path = e["num"], e["date"], e["time"], e["path"]
        cnum, title, content = fu.parse_md_file(Path(path))
        try:
            cnum_int = int(cnum) if cnum is not None else None
        except (TypeError, ValueError):
            cnum_int = None
        if cnum_int != num:
            print(f"[跳过] 第{num}章 文件章节号不符({cnum!r})，不补以防错位", flush=True)
            fail_list.append((num, f"文件章节号不符({cnum!r})"))
            continue
        print(f"[{idx+1}/{len(entries)}] 第{num}章 {title} -> {date_str} {time_str}",
              flush=True)
        done = False
        for attempt in range(1, 4):
            try:
                # 每章都先导航到干净的新建章页（补发从平台已抓完后开始，
                # 当前停在 chapter-manage，首章也需导航）
                await page.goto(new_url)
                await fu.wait_for_editor_ready(page)
                await fu.fill_chapter(page, cnum, title, content)
                await fu.publish_scheduled(page, date_str, time_str, use_ai=False)
                print(f"    -> 定时发布成功 {date_str} {time_str}", flush=True)
                done = True
                break
            except fu.DailyLimitReached as ex:
                print(f"    达发布字数上限（{ex}），中止整批", flush=True)
                fail_list.append((num, f"字数上限:{ex}"))
                for r in entries[idx + 1:]:
                    fail_list.append((r["num"], "字数上限，未处理"))
                return ok_list, fail_list
            except Exception as ex:
                if attempt < 3:
                    print(f"    第{attempt}次失败: {ex}，重试", flush=True)
                    await page.wait_for_timeout(2000)
                else:
                    print(f"    失败: {ex}", flush=True)
                    fail_list.append((num, str(ex)[:120]))
        if done:
            ok_list.append(num)
    return ok_list, fail_list


def print_plan(plan, warnings, present_count):
    def compress(a):
        out, i = [], 0
        while i < len(a):
            j = i
            while j + 1 < len(a) and a[j + 1] == a[j] + 1:
                j += 1
            out.append(str(a[i]) if i == j else f"{a[i]}-{a[j]}")
            i = j + 1
        return ",".join(out)
    nums = [e["num"] for e in plan]
    nofile = [e["num"] for e in plan if not e["has_file"]]
    print(f"平台在架章节: {present_count}", flush=True)
    print(f"当前缺口(需补): {len(plan)} 章", flush=True)
    print(f"章号: {compress(nums)}", flush=True)
    if nofile:
        print(f"⚠ 本地缺文件、无法补: {compress(nofile)}", flush=True)
    # 过期缺口：原定排期已过，番茄可能拒绝定时到过去、且已是读者可见断档
    now = datetime.now()
    assign = {e["num"]: (e["date"], e["time"]) for e in plan}
    overdue = fu.overdue_gap_nums(
        assign, (now.strftime("%Y-%m-%d"), now.strftime("%H:%M")))
    if overdue:
        print(f"⚠ 排期已过期 {len(overdue)} 章: {compress(overdue)}"
              f"（番茄可能拒绝定时到过去，建议改用立即发布尽快补）", flush=True)
    for w in warnings:
        print(f"⚠ {w}", flush=True)
    print("补发时间样例:", flush=True)
    for e in plan[:5]:
        print(f"    第{e['num']}章 -> {e['date']} {e['time']}", flush=True)
    if len(plan) > 5:
        print(f"    ... 共 {len(plan)} 章", flush=True)


async def main_async(args):
    fu.load_config()
    book_id = args.book_id or load_defaults()[0]
    content_dir = args.content_dir or load_defaults()[1]
    if not book_id or not content_dir:
        print("缺 book_id 或 content_dir（.gui_state.json/config.json 未找到，"
              "请用 --book-id/--content-dir 指定）", flush=True)
        return
    num2path = map_local_files(content_dir)
    print(f"本地章节文件: {len(num2path)} 个  |  book_id: {book_id}", flush=True)

    async with async_playwright() as p:
        browser, context = await fu.create_context(p, headless=args.headless)
        page = await context.new_page()
        try:
            print("抓取平台章节列表…", flush=True)
            rows = await scrape_platform(page, book_id)
            present = sum(1 for r in rows if r.get("chapterNum") is not None)
            plan, warnings = build_plan(rows, num2path)
            # 快照只留 章号/日期/时间/是否有本地文件——不写机器绝对路径，
            # 便于纳入版本管理做记录；补发用内存里的完整 plan（含 path）。
            snap = [{k: e[k] for k in ("num", "date", "time", "has_file")} for e in plan]
            PLAN_SNAPSHOT.write_text(
                json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
            print_plan(plan, warnings, present)

            if args.dry_run:
                print(f"[dry-run] 已写快照 {PLAN_SNAPSHOT}，未发布任何章节。", flush=True)
                return

            entries = select_entries(plan, args)
            if not entries:
                print("没有匹配的可补章节（可能已补完或筛选为空）。", flush=True)
                return
            print(f"本次将补 {len(entries)} 章 —— 3 秒后开始…", flush=True)
            await page.wait_for_timeout(3000)
            ok, fail = await republish(page, entries, book_id)
            print("=" * 50, flush=True)
            print(f"补发完成  成功 {len(ok)}  失败 {len(fail)}", flush=True)
            if ok:
                print("成功章号:", ",".join(map(str, ok)), flush=True)
            if fail:
                print("失败明细:", flush=True)
                for n, r in fail:
                    print(f"  第{n}章: {r}", flush=True)
        finally:
            await fu.close_browser_safely(browser)


def main():
    ap = argparse.ArgumentParser(description="缺章补发工具")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--only", type=int, help="只补指定章号（灰度）")
    g.add_argument("--range", type=str, help="补指定章号区间，如 709-1073")
    g.add_argument("--all", action="store_true", help="补全部当前缺口")
    ap.add_argument("--limit", type=int, help="本次最多补多少章（分月控量）")
    ap.add_argument("--dry-run", action="store_true", help="只看缺口+排期，不发布")
    ap.add_argument("--book-id", type=str, help="覆盖 book_id")
    ap.add_argument("--content-dir", type=str, help="覆盖本地章节目录")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    args = ap.parse_args()
    if not (args.only or args.range or args.all or args.dry_run):
        ap.error("请指定 --dry-run / --only / --range / --all 之一")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
