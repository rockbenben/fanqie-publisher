# -*- coding: utf-8 -*-
"""续排工具 —— 把本地还没发的章接在平台队列末尾（接的是时刻，不是整天）。

两种模式（自己选）:
  · 默认「维持深度」—— 只补到"今天 + N 天"，队列保持浅（--days-ahead N）
  · --all 「全部补齐」—— 本地还没发的全排上去，一次排完
  · --limit N 可给任一模式加个本次上限（分批用）

**只往后接，绝不补中段缺口**: 新建章一律追加到全书末尾，把中段缺章"发上去"
只会让它吊在书尾（本项目要消灭的正是这类错位）。中段缺口交给 tools/remap
（改写未公开章的内容）；本工具发现缺口会提醒，但不碰。

为什么要这个工具:
  番茄的目录顺序按章节在卷内的位置排、新建章只能追加到末尾——中段缺口在网页端
  永远补不回原位（只有手机 App 的 3 天移动窗口）。

  **注意: 防中段缺口已经不再是本工具的理由。** 这条论证写于 run_creation_batch
  合并之前——当时"一次排几百章"确实会因静默失败在中段留洞，压浅队列是把失败
  赶到队尾的唯一办法。现在发布类逐章确认落地、确认不到就中止整批，没排上的章
  永远是连续的队尾一段，与队列排多深无关。深队列不再更危险。

  残余风险很小但不是零: 逐章确认给出假阳性时，它后面的章仍会照排，形成一个
  真中段缺口。批末 reconcile_after_batch 会当场报出来，而一批跑完远在 3 天
  移动窗口之内，仍来得及救。浅队列只是把这种情况的波及范围再压小一点。

  于是浅深之别退化成一个**纯偏好**，没有对错，按自己的写作节奏挑:
  · **全排** —— 一次把存稿排完，之后不用再管。断更保护最强（出门/关机/生病，
    队列自己走几个月）。代价: 想改已排出去的剧情，得去平台一章章改。
  · **维持 N 天** —— 只排到「今天+N 天」，漏跑一天第二天自动补两天的量，也不
    断更。代价: 得让它定期跑（挂 cron 或自己记着）。好处是改稿只改本地文件。

  两个入口的默认值不同，是各自匹配用法，不是不一致:
  · 主 CLI --auto-continue / GUI「自动接续」 = 交互式一次性推存稿 → 默认**全排**
  · 本工具独立跑 = 挂 cron 重复执行 → 默认「维持 N 天」（重复作业只有这个默认
    说得通；要全排加 --all）

安全性（与 remap 同一套原则）:
  · 默认 dry-run，要真的写必须显式 --run。
  · 失败即停：任何一章没成功就中止整批，剩余如实记账。队尾停下无害，
    明天接着补；继续往下发才会把缺口卡在中间。
  · 每次都重新抓平台真实状态计算，可安全重跑（幂等，跑几次都只补到目标深度）。
  · 收尾用 reconcile_after_batch 跟平台对账，漏章当场报。

用法:
  python tools/keep_ahead/keep_ahead.py                 # 预览（只读）
  python tools/keep_ahead/keep_ahead.py --run           # 补齐队列
  python tools/keep_ahead/keep_ahead.py --days-ahead 5  # 改目标深度
  python tools/keep_ahead/keep_ahead.py --all --run     # 本地还没发的全排上去
  python tools/keep_ahead/keep_ahead.py --all --limit 200 --run   # 分批
  python tools/keep_ahead/keep_ahead.py --daily         # 无人值守（日志+告警）
  python tools/keep_ahead/keep_ahead.py --self-check    # 纯离线自检

每天自动跑（--daily 自己管日志和告警）:
  Windows      计划任务 → pythonw.exe "<仓库>\\tools\\keep_ahead\\keep_ahead.py" --daily
  Linux/macOS  crontab  → 20 0 * * * cd <仓库> && python3 tools/keep_ahead/keep_ahead.py --daily
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_DAYS_AHEAD = 3


# 起排槽位至少晚于"现在"这么多分钟。平台拒掉定时到过去的章，而提交本身要跑
# 十几秒/章；真正的最小提前量没有样本，宁可保守。
LEAD_MINUTES = 10


def queue_tail_dt(items):
    """平台队列排到哪一刻（datetime，精确到分）。没有待发布章返回 None。

    只看待发布章的 timer_time——已发布的是过去，不构成"队列深度"。
    """
    tails = []
    for it in items:
        if it.get("display_status") != fu.DISPLAY_PENDING:
            continue
        try:
            tt = int(it.get("timer_time") or 0)
        except (TypeError, ValueError):
            tt = 0
        if tt:
            tails.append(datetime.fromtimestamp(tt))
    return max(tails) if tails else None


def queue_tail_date(items, *, today=None):
    """平台队列排到哪一天了（返回 date）。没有待发布章就返回今天。"""
    dt = queue_tail_dt(items)
    return dt.date() if dt else (today or datetime.now().date())


def schedule_after(tail_dt, n, pub_time, per_day, *, now=None):
    """接着队尾往后排 n 章：先填满队尾那天剩下的槽位，再逐日往后。返回 [(date, time)]。

    以前只按日期接（队尾次日第 0 槽），队尾那天哪怕只排了 2/6 章也永远空着——
    2026-09-03 批次中止在 02-16 07:01，下次接续就从 02-17 起。现在:
      cut   = max(队尾时刻, 现在 + LEAD_MINUTES)
      start = cut 当天；丢掉当天模板里 ≤ cut 的槽位，其余照 compute_schedule 排
    按"≤ 队尾时刻的槽位数"切、不按"当天已有几章"数：平台上那天哪怕是用别的时间
    配置排的，新章也严格晚于队尾——章节顺序 = 发布顺序这条线不能破。
    队列排干/队尾在过去时从"现在 + 余量"起，只跳过已过去的槽——老逻辑起排
    "今天第 0 槽"，--daily 晚上跑时第一章就被平台拒、失败即停、每晚重现。
    """
    now = now or datetime.now()
    cut = now + timedelta(minutes=LEAD_MINUTES)
    if tail_dt and tail_dt > cut:
        cut = tail_dt
    if n <= 0:
        return []
    start = cut.strftime("%Y-%m-%d")
    cut_hm = cut.strftime("%H:%M")
    times = fu.validate_times(pub_time) or ["08:00"]
    effective = max(per_day, len(times))
    day = fu.compute_schedule(effective, start, pub_time, per_day)     # 当天模板
    k = sum(1 for _d, t in day if t <= cut_hm)
    sched = fu.compute_schedule(n + k, start, pub_time, per_day)[k:]
    # compute_schedule 的保序修复只会把临近午夜挤住的槽位前挪；万一挪到 ≤ cut，
    # 退回老行为——次日整天。宁可空半天，不能让新章早于队尾。
    if any(d == start and t <= cut_hm for d, t in sched):
        nxt = (cut + timedelta(days=1)).strftime("%Y-%m-%d")
        sched = fu.compute_schedule(n, nxt, pub_time, per_day)
    return sched


def plan_refill(items, num2path, *, days_ahead=DEFAULT_DAYS_AHEAD, per_day=9,
                all_remaining=False, limit=None, today=None):
    """算出「要补哪几章、从哪天开始排」。纯函数，便于自检。

    两种模式:
      · 维持深度（默认）—— 只补到"今天 + days_ahead 天"，队列保持浅。
      · all_remaining —— 本地还没发的全排上去，队列有多深排多深。

    两种模式的安全性其实一样: 真正防住缺口的是**失败即停**——失败发生在队尾、
    下轮从那里接着排，缺口根本形成不了。队列深度影响的是另外两件事:
      ① 排到几个月后的章，之后想改剧情就得去平台一章章改（且距发布不足
         30 分钟的修改可能不生效）；浅队列改稿只改本地文件。
      ② 平台规则: 有远期定时章在，后续「立即发布」的章不进审。

    返回 (nums, start_date, need_days, tail, gaps):
      nums       要排的章号（升序，只取平台最大章号之后的）
      start_date 队列末尾的次日；**只用来算 need_days**，不是真正的起排点
                 —— 那个由 schedule_after 接着队尾的时刻算（会先填满队尾那天
                 剩下的时间点）
      need_days  这批要占几天
      tail       当前队列排到哪天
      gaps       平台中段缺的章号（本工具不碰，交给 tools/remap；仅用于提醒）

    start_date 只用于维持深度模式算 need_days（按整天）；真正的排期由
    schedule_after 接着队尾的**时刻**算，会先填满队尾那天剩下的时间点。
    """
    today = today or datetime.now().date()
    tail = queue_tail_date(items, today=today)
    # 夹到今天: tail 落在过去是真实情况（队列已经排干、或有待发布章的
    # 排期早就过了平台却没发出去），但起排日不能跟着回到过去——平台会拒
    # 掉定时到过去的章，而 --daily 没有 GUI 那个「日期已过去」确认框：
    # 第一章被拒 → 失败即停 → 那晚一章都没排上，而且每晚都会重现。
    start = max(tail + timedelta(days=1), today)

    present = {fu.chapter_title_num(x.get("title")) for x in items}
    present.discard(None)
    # 只接在平台**最大章号之后**——中段缺口绝不能由本工具来补！
    # 新建章一律追加到全书末尾，把第729章这种中段缺章"发上去"只会让它吊在
    # 书尾，正是本项目要消除的那类错位。中段缺口归 tools/remap 处理（改写
    # 未公开章的内容），这里只负责往后续排。
    top = max(present) if present else 0
    todo = [n for n in sorted(num2path) if n > top]
    gaps = [n for n in range(1, top + 1) if n not in present and n in num2path]

    if all_remaining:
        nums = todo
    else:
        # 按夹过的 start 算，不然 tail 在过去时会把已经过去的那几天也算进来，
        # 一口气排出超过目标深度的量。
        need_days = (today + timedelta(days=days_ahead) - start).days + 1
        if need_days <= 0:
            return [], start, 0, tail, gaps
        nums = todo[:need_days * per_day]
    if limit:
        nums = nums[:limit]
    return nums, start, -(-len(nums) // per_day) if nums else 0, tail, gaps


# 新章确认在 fanqie_upload（定时发布/立即发布/续排 共用同一份）


async def run_publish(page, book_id, nums, schedule, num2path, sig_urls, *,
                      use_ai=False, max_retries=2, delay=3):
    """逐章定时发布；任何一章不成功即中止整批（剩余如实记账）。

    单章的"新建→填正文→定时发布→重试"走 fu.publish_one_chapter——那是
    CLI 上传、GUI 上传和本工具共用的同一份实现，不在这里再抄一遍循环。
    本函数只负责: 选题已定后的顺序控制、逐章确认、失败即停、记账。

    失败即停的理由: 新建章只能追加到全书末尾，跳过一章继续发会把缺口永久
    卡在中段；而停在队尾毫无代价——下一轮从平台真实状态重算，接着发即可。
    """
    new_url = fu.NEW_CHAPTER_URL_TPL.format(book_id=book_id)
    ok, fail = [], []

    def abort(i, reason):
        fail.append((nums[i], reason))
        fail.extend((n, "前方中止，未处理") for n in nums[i + 1:])
        return ok, fail

    for i, num in enumerate(nums):
        cnum, title, content = fu.parse_md_file(Path(num2path[num]))
        try:
            cnum_int = int(cnum) if cnum is not None else None
        except (TypeError, ValueError):
            cnum_int = None
        if cnum_int != num:
            print(f"    ✗ 本地文件章节号不符({cnum!r}) —— 中止整批", flush=True)
            return abort(i, f"本地文件章节号不符({cnum!r})")
        d, t = schedule[i]
        print(f"[{i + 1}/{len(nums)}] 第{num}章 {title[:20]} -> {d} {t}", flush=True)
        try:
            done, err = await fu.publish_one_chapter(
                page, new_url, str(num), title, content, schedule=(d, t),
                use_ai=use_ai, max_retries=max_retries, err_tag=f"ka_{num}")
        except fu.DailyLimitReached as ex:
            print(f"    撞字数上限（{ex}），中止整批", flush=True)
            return abort(i, f"字数上限:{ex}")
        if not done:
            print(f"    ✗ {err} —— 中止整批", flush=True)
            return abort(i, err or "未知失败")
        # 逐章确认新章真的落地了 —— 漏一章就是永久缺口，不能只信提交信号
        if not await fu.confirm_chapter_on_platform(page, sig_urls, num):
            print(f"    ✗ 平台上查不到 第{num}章（提交说成功了）—— 中止整批",
                  flush=True)
            return abort(i, f"提交后平台查不到 第{num}章")
        print("    ✓ 平台已确认", flush=True)
        ok.append(num)
        if i < len(nums) - 1 and delay > 0:
            await page.wait_for_timeout(int(delay * 1000))
    return ok, fail


def print_plan(items, nums, schedule, tail, need_days, days_ahead, per_day):
    pend = [x for x in items if x.get("display_status") == fu.DISPLAY_PENDING]
    print(f"平台 {len(items)} 章（待发布 {len(pend)}），队列排到 {tail}", flush=True)
    print("模式: 全部补齐（本地还没发的都排上去）× 每天 %d 章" % per_day
          if days_ahead is None else
          f"模式: 维持队列 {days_ahead} 天深度 × 每天 {per_day} 章", flush=True)
    if not nums:
        print("本地已全部发过，没有可排的章。" if days_ahead is None
              else "队列已达目标深度，无需补排。", flush=True)
        return
    print(f"本次补 {need_days} 天 / {len(nums)} 章："
          f"第{nums[0]}~{nums[-1]}章，从 {schedule[0][0]} {schedule[0][1]} 起接着排",
          flush=True)
    for n, (d, t) in list(zip(nums, schedule))[:5]:
        print(f"    第{n}章 -> {d} {t}", flush=True)
    if len(nums) > 5:
        print(f"    … 共 {len(nums)} 章，末章 第{nums[-1]}章 -> "
              f"{schedule[-1][0]} {schedule[-1][1]}", flush=True)


async def main_async(args):
    cfg = fu.load_config()
    book_id, num2path, headless = fu.tool_startup(args)
    if not book_id:
        # 返回 True(=需人工)而不是 False：run_unattended 靠返回值决定 exit 3 与
        # 弹窗。返回 False 会让 --daily 的计划任务每晚"成功"退出 0 却什么都没做，
        # 而队列会一天天耗到断更才被发现。跟 remap 保持一致。
        return True

    per_day = args.per_day or cfg.get("default_per_day", 9)
    # load_config 会用 DEFAULT_CONFIG 补齐该键，回退值只是防御；
    # 写成和 DEFAULT_CONFIG 一致的值，别让人以为本工具另有默认时间表
    pub_time = args.time or cfg.get("default_time", "08:00")

    async with async_playwright() as p:
        browser, ctx = await fu.create_context(p, headless=headless)
        page = await ctx.new_page()
        try:
            print("抓取平台章节状态…", flush=True)
            items, signed_url, volumes = await fu.fetch_chapter_items(page, book_id)
            if fu.volume_count(volumes) > 1:
                print(f"本作品 {fu.volume_count(volumes)} 卷，已跨卷合并 "
                      f"{len(items)} 章", flush=True)
            nums, start_date, need_days, tail, gaps = plan_refill(
                items, num2path, days_ahead=args.days_ahead, per_day=per_day,
                all_remaining=args.all, limit=args.limit)
            schedule = schedule_after(queue_tail_dt(items), len(nums),
                                      pub_time, per_day)
            print_plan(items, nums, schedule, tail, need_days,
                       None if args.all else args.days_ahead, per_day)
            if gaps:
                # 中段缺口本工具不碰（发上去只会吊在书尾），提醒去用 remap
                print(f"⚠ 平台中段还缺 {len(gaps)} 章（如 第"
                      + "、第".join(str(n) for n in gaps[:5])
                      + "章…）——这些**不能**靠发布补回原位，"
                        "请用 tools/remap/remap.py 处理", flush=True)
            if fu.book_mismatch_abort(items, num2path):
                return True

            if not nums or not args.run:
                if nums:
                    print(f"\n[dry-run] 本次将补排 {len(nums)} 章。"
                          f"确认无误后加 --run 才会真正写入。", flush=True)
                return False

            print(f"\n开始补排 {len(nums)} 章 —— 3 秒后开始…", flush=True)
            await page.wait_for_timeout(3000)
            sig_urls, _detach = fu.watch_chapter_list_url(page)
            sig_urls.append(signed_url)
            ok, fail = await run_publish(
                page, book_id, nums, schedule, num2path, sig_urls,
                use_ai=args.use_ai,
                max_retries=cfg.get("max_retries", 2),
                delay=cfg.get("delay_between_chapters", 3))
            print("=" * 50, flush=True)
            print(f"完成  成功 {len(ok)}  失败 {len(fail)}", flush=True)
            for n, why in fail[:30]:
                print(f"  第{n}章: {why}", flush=True)

            # 收尾对账：平台数据说了才算数（漏 151 章的教训）
            fail_list = []
            miss = await fu.reconcile_after_batch(page, book_id, ok, fail_list)
            abnormal = next((w for _n, w in fail
                             if "字数上限" not in w and w != "前方中止，未处理"), None)
            if miss:
                return True
            if abnormal:
                print(f"⚠ 需要人工处理: 批次异常中止: {abnormal}", flush=True)
                return True
            return False
        finally:
            await fu.close_browser_safely(browser)


def demo():
    """零依赖自检: 队列深度计算与补排选题。"""
    from datetime import date
    TODAY = date(2026, 8, 20)

    def pend(num, day):
        ts = int(datetime(2026, 8, day, 7, 0).timestamp())
        return {"title": f"第{num}章", "display_status": fu.DISPLAY_PENDING,
                "timer_time": ts, "index": num}

    def pub(num):
        return {"title": f"第{num}章", "display_status": fu.DISPLAY_PUBLISHED,
                "timer_time": 0, "index": num}

    local = {n: f"/x/chapter-{n}.md" for n in range(1, 100)}

    # 队列排到 8-22，目标 3 天(到 8-23) → 只需补 1 天
    items = [pub(1), pub(2), pend(3, 21), pend(4, 22)]
    nums, start, days, tail, _g = plan_refill(items, local, days_ahead=3, per_day=2,
                                          today=TODAY)
    assert tail == date(2026, 8, 22), tail
    assert days == 1 and start == date(2026, 8, 23), (days, start)
    assert nums == [5, 6], nums                      # 平台最大 4，接着排 5、6

    # 待发布章的排期已经过去（平台卡住、或缓存行的日期早过了）：
    # tail 落在过去是事实，但起排日不能跟着回到过去——平台会拒掉定时到
    # 过去的章，而 --daily 没有确认框：第一章被拒 → 失败即停 → 那晚白跑。
    items = [pub(1), pend(2, 15), pend(3, 16)]        # 排期都在 TODAY(8-20) 之前
    nums, start, days, tail, _g = plan_refill(items, local, days_ahead=3, per_day=2,
                                              today=TODAY)
    assert tail == date(2026, 8, 16), tail            # tail 照实报过去
    assert start == TODAY, start                      # 但起排日夹到今天
    assert days == 4 and nums == [4, 5, 6, 7, 8, 9, 10, 11], (days, nums)

    # 队列已够深 → 什么都不补（幂等：一天跑几次都一样）
    deep = [pub(1), pend(2, 25)]
    nums2, _s, days2, _t, _g2 = plan_refill(deep, local, days_ahead=3, per_day=2,
                                       today=TODAY)
    assert nums2 == [] and days2 == 0, (nums2, days2)

    # 漏跑一天：队列只到今天 → 自动补满 3 天的量，不断更
    thin = [pub(1), pend(2, 20)]
    nums3, start3, days3, _t, _g3 = plan_refill(thin, local, days_ahead=3, per_day=2,
                                           today=TODAY)
    assert days3 == 3 and len(nums3) == 6, (days3, nums3)
    assert start3 == date(2026, 8, 21), start3

    # 完全没有待发布章 → 从今天算深度，次日开排
    nums4, start4, days4, tail4, _g4 = plan_refill([pub(1)], local, days_ahead=2,
                                              per_day=3, today=TODAY)
    assert tail4 == TODAY and days4 == 2 and len(nums4) == 6
    assert nums4[0] == 2, nums4                      # 平台最大 1，接着往后
    assert start4 == date(2026, 8, 21), start4

    # 中段缺口绝不能由本工具补：发上去只会吊在书尾（这正是本项目要消的坑）。
    # 平台有 1、3、4，缺 2 —— 必须从 5 往后接，且把缺口报出来交给 remap。
    gap = [pub(1), pub(3), pend(4, 20)]
    nums5, _s, _d, _t, gaps5 = plan_refill(gap, local, days_ahead=1, per_day=2,
                                           today=TODAY)
    assert 2 not in nums5, nums5
    assert nums5[0] == 5, nums5
    assert gaps5 == [2], gaps5

    # --all: 本地还没发的全排上去，不受深度限制
    deep2 = [pub(1), pend(2, 25)]
    numsA, startA, daysA, _t, _gA = plan_refill(deep2, local, days_ahead=3, per_day=2,
                                           all_remaining=True, today=TODAY)
    assert len(numsA) == 97 and numsA[0] == 3, (len(numsA), numsA[:3])
    assert startA == date(2026, 8, 26), startA      # 接队列末尾(8-25)的次日
    assert daysA == 49, daysA                       # 97 章 / 每天 2 章，向上取整
    # 同样的输入在默认模式下什么都不补 —— 证明开关真的在起作用
    assert plan_refill(deep2, local, days_ahead=3, per_day=2, today=TODAY)[0] == []

    # --limit: 分批用，截断但起排日不变
    numsL, startL, daysL, _t, _gL = plan_refill(deep2, local, per_day=2, limit=5,
                                           all_remaining=True, today=TODAY)
    assert numsL == [3, 4, 5, 6, 7] and startL == startA, (numsL, startL)
    assert daysL == 3, daysL                        # 5 章 / 每天 2 章 → 3 天

    # ---- schedule_after: 接着队尾的时刻续排，不再整天跳 ----
    NOW = datetime(2026, 8, 20, 9, 0)
    T = "07:00,12:00,20:00"
    # 队尾 02-16 07:01（2026-09-03 中止现场），模板 3 时点 ×2 → 先填满当天
    s = schedule_after(datetime(2027, 2, 16, 7, 1), 5, T, 6, now=NOW)
    assert s == [("2027-02-16", "12:00"), ("2027-02-16", "12:01"),
                 ("2027-02-16", "20:00"), ("2027-02-16", "20:01"),
                 ("2027-02-17", "07:00")], s
    # 队尾那天已满 → 次日第 0 槽，与老行为一致
    s = schedule_after(datetime(2027, 2, 16, 20, 1), 2, T, 6, now=NOW)
    assert s[0] == ("2027-02-17", "07:00"), s
    # 平台上那天是按别的配置排的（队尾 15:30）：新章仍严格晚于队尾
    s = schedule_after(datetime(2027, 2, 16, 15, 30), 1, T, 6, now=NOW)
    assert s[0] == ("2027-02-16", "20:00"), s
    # 队列空 / 队尾在过去 → 从"现在+余量"起，只跳过已过去的槽
    s = schedule_after(None, 2, T, 6, now=datetime(2026, 8, 20, 15, 0))
    assert s[0] == ("2026-08-20", "20:00"), s
    s = schedule_after(datetime(2026, 8, 1, 7, 0), 1, T, 6,
                       now=datetime(2026, 8, 20, 21, 0))
    assert s[0] == ("2026-08-21", "07:00"), s        # 今天的槽都过了 → 明天
    s = schedule_after(None, 1, T, 6, now=datetime(2026, 8, 20, 6, 55))
    assert s[0] == ("2026-08-20", "12:00"), s        # 07:00 只差 5 分钟 < 余量，跳过
    assert schedule_after(datetime(2027, 1, 1, 7, 0), 0, T, 6, now=NOW) == []
    # 随机队尾 × 随机模板：新槽严格晚于队尾与 now+余量，且全程严格递增
    import random
    rnd = random.Random(7)
    lead = (NOW + timedelta(minutes=LEAD_MINUTES)).strftime("%Y-%m-%d %H:%M")
    for _ in range(300):
        tl = datetime(2026, 8, 20) + timedelta(minutes=rnd.randrange(0, 60 * 24 * 40))
        pd = rnd.randint(1, 9)
        tm = rnd.choice([T, "08:00", "23:50,23:55", "06:00,18:00", "00:00,23:59"])
        s = schedule_after(tl, rnd.randint(1, 12), tm, pd, now=NOW)
        cut = max(tl.strftime("%Y-%m-%d %H:%M"), lead)
        stamps = [f"{d} {t}" for d, t in s]
        assert all(x > cut for x in stamps), (tl, tm, pd, s)
        assert all(a < b for a, b in zip(stamps, stamps[1:])), (tl, tm, pd, s)
    # limit 在维持深度模式下同样生效
    assert len(plan_refill(thin, local, days_ahead=3, per_day=2, limit=2,
                           today=TODAY)[0]) == 2

    # 本地没文件的章号不会被排进去
    sparse = {5: "/x/5.md", 6: "/x/6.md"}
    nums6, _s, _d, _t, _g6 = plan_refill([pub(1)], sparse, days_ahead=1, per_day=9,
                                         today=TODAY)
    assert nums6 == [5, 6], nums6
    # --- 铁律: 发布路径必须逐章确认，且确认不到必须中止 ---
    # 与 remap（改内容）正相反: 那边 item 已存在，回读误判只是白丢一批；
    # 这边漏判一章 = 平台上根本没有这章，而新建只能追加到书尾，永久缺口。
    import inspect as _insp, re as _re
    _src = _insp.getsource(run_publish)
    assert "fu.confirm_chapter_on_platform" in _src, "发布路径必须逐章确认"
    assert "publish_one_chapter" in _src, "单章发布必须走共用原语，别再抄一遍循环"
    assert "publish_scheduled" not in _src, "发布循环不该在这里重复实现"
    # 确认不到必须走 abort：截取 fu.confirm_chapter_on_platform 之后的一小段源码来验
    _after = _src[_src.index("if not await fu.confirm_chapter_on_platform"):][:400]
    assert "return abort" in _after, "确认不到必须中止整批"
    _reasons = _re.findall(r"return abort\(i, ([^)]+)\)", _src)
    assert any("平台查不到" in r for r in _reasons), _reasons

    print("demo ok")


def main():
    ap = argparse.ArgumentParser(description="把平台队列补到 N 天深度")
    ap.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD,
                    help=f"只排到 N 天后（默认 {DEFAULT_DAYS_AHEAD} 天）")
    ap.add_argument("--all", action="store_true",
                    help="本地还没发的全排上去；不加则只排到 --days-ahead 天后")
    ap.add_argument("--limit", type=int, help="本次最多排多少章（分批用）")
    ap.add_argument("--per-day", type=int, help="每天几章（默认取 config）")
    ap.add_argument("--time", type=str, help="发布时间点，逗号分隔（默认取 config）")
    ap.add_argument("--run", action="store_true", help="真的写入（不加只预览）")
    ap.add_argument("--book-id", type=str)
    ap.add_argument("--content-dir", type=str)
    ap.add_argument("--use-ai", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--daily", action="store_true",
                    help="无人值守日常跑：等于 --run --headless，写日志、需人工时弹窗")
    ap.add_argument("--self-check", action="store_true", help="只跑自检，不联网")
    args = ap.parse_args()
    if args.self_check:
        demo()
        return
    fu.run_unattended(main_async, args, log_dir=LOG_DIR, name="续排",
                      hint="（队列每天少一天深度）")


if __name__ == "__main__":
    main()
