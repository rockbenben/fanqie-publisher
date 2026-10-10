# -*- coding: utf-8 -*-
"""章节重排工具 —— 把「未公开」的待发布章节按位置重新装内容，消掉中段缺口。

为什么需要它（2026-08-20 实测，推翻了"按原排期把缺章补回原位"那套做法）:
  番茄的目录顺序 = 章节 item 在卷内的位置（接口 chapter_list/v1 的 index），
  不是标题里的「第N章」，也不是定时发布时间。该接口每章根本没有章节号字段，
  编辑器里「第 __ 章」写的值只是拼进标题字符串。新建章一律追加到全书末尾，
  网页端没有任何插入/排序/拖拽入口（行操作只有"编辑"，编辑分卷只能建卷）；
  唯一的**移动位置**途径是手机 App 里「申请调整 → 审批通过 → 单章选中 →
  移动位置」，且**只对发布 3 天内的章有效**，超期位置就永远动不了。
  实证(当天快照): 之前补的第480/524/670/697章全部吊在读者目录最末尾，读者看到
  第699章之后接第480章；其中第697章还是按缺口槽位 8-19 12:02 准时发布的，位置
  照样在末尾。（697/698/699 后来由作者在 App 里手动移回原位，480/524/670 因超
  3 天窗口留在错位处——所以现在去读者目录看到的是这三章。）
  → 缺章补不回原位。但这条只约束**位置**：已发布章的**内容**在网页端照常
  能改（可编辑与否平台只看一个 cant_modify_reason 字段，与已发布无关），所以
  整段错位还能靠改写内容修回来——见下面 --include-published 那段。

做法:
  位置 i 的 item 装第 i 章。未公开（=10 待发布）的按 index 升序逐个用「修改内容」
  改写章节号+标题+正文，排期（timer_time）保持不变。改完队列覆盖到第（最大
  index）章，原来的中段缺口变成"队列末尾少排了 N 章"，之后按正常上传接着往后
  排即可。

  已公开（display_status=1）的章默认不碰 —— 加 --include-published 才纳入。
  **平台是允许改已发布章的**：可编辑与否平台只给一个 cant_modify_reason 字段
  （完结作品 / 审核中之类），跟 display_status 无关；主工具的修改模式也一直在
  改已发布章。默认跳过是本工具自己画的保守线，不是平台限制。

  为什么要开这个开关: 已公开段的错位是**位移**而不是缺章 —— 有几章被顶到书尾
  之后，中间整段就跟着偏移了 1/2/3 级（实测 2026-10-10: 位置 480-522 装
  第481-523章、523-667 装第525-669章、668-691 装第671-694章，而 692/693/694
  倒插着第480/524/670章）。这种错位靠「移动位置」治不了 —— 3 天窗口早过了；
  但它**可以靠改写内容治**：把位置 i 改回装第 i 章，整段立刻归位，且内容不会
  丢（读的是本地稿子，第480/524/670章 会回到位置 480/524/670）。

安全性:
  · 默认只改 display_status=10 且 cant_modify_reason 为空的章；
    --include-published 才把已公开的纳入，它自己也要过 cant_modify_reason 这关。
  · 跳过即将发布的章（默认 60 分钟内），避免和平台的发布动作抢。对已公开章，
    这个"缓冲"改看发布时间 —— 刚发布不到 60 分钟的章平台可能还在收尾。
  · 按 index 升序处理：中途停在哪都是"单调有洞"，不会产生乱序或重复。
  · **番茄拒绝同名章节**，而重排是置换不是覆盖：位置 i 想要的第 i 章此刻还装在
    另一个位置上，得等那个位置先改写过、章号腾出来才写得进去。排不进本趟的
    记成「章号被占」，自动再走一趟；会导致**永久重名**的（目标章号占在一个本轮
    不会改写的位置上）直接踢掉不写。详见 resolve_title_collisions。
  · 默认 dry-run，要真的写必须显式 --run。
  · 每次运行都重新抓平台真实状态计算，可安全重跑。

用法:
  python tools/remap/remap.py                    # 预览计划（只读）
  python tools/remap/remap.py --only 709 --run   # 灰度改一个 item
  python tools/remap/remap.py --limit 50 --run   # 改前 50 个
  python tools/remap/remap.py --run              # 全改
  python tools/remap/remap.py --audit            # 只做缺口体检，不改任何东西

  修已公开段的顺序错位（会改写已发布内容，务必先看 dry-run）:
  python tools/remap/remap.py --include-published --range 480-694          # 先看计划
  python tools/remap/remap.py --include-published --range 480-694 --run     # 真写入

  python tools/remap/remap.py --daily            # 无人值守日常跑（写日志+告警弹窗）
  python tools/remap/remap.py --self-check       # 纯离线自检

每天自动跑（--daily 自己管日志和告警，不需要再套 shell 脚本）:
  Windows  计划任务 → pythonw.exe "<仓库>\\tools\\remap\\remap.py" --daily
           （pythonw 没有控制台窗口，不会杵一个黑框）
  Linux/macOS  crontab -e →
           10 0 * * * cd <仓库> && python3 tools/remap/remap.py --daily
"""
import argparse
import calendar
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

EDIT_URL_TPL = (fu.BASE_URL
                + "/main/writer/{book_id}/publish/{item_id}/?enter_from=modifychapter")
# 位置体检的常量/纯函数/抓取都在 fanqie_upload，GUI「检查缺口」共用同一套。
SAFETY_MINUTES = 60
_VERIFY_WINDOW_S = 600  # 批末复核最多等多久（平台审核滞后，10 分钟）
_VERIFY_POLL_S = 45     # 复核轮询间隔
_DUP_MAX_ROUNDS = 3     # 「章号被占」最多再走几趟（排好序的话一趟就够）


def _ts(v):
    """平台给的时间戳有的是 int、有的是 "0" 这种字符串，统一下沉成 int。"""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def compute_remap(items, *, now_ts=None, safety_minutes=SAFETY_MINUTES,
                  include_published=False):
    """纯函数: 算出「哪些 item 要改成第几章」。

    items: [{index,title,display_status,timer_time,item_id,cant_modify_reason}]
    返回 (plan, skipped):
      plan    = [{index, item_id, now_title, want_num, timer, published}]，
                按 index 升序
      skipped = [(index, 原因)]

    include_published: 已公开（display_status=1）的章也纳入改写。默认 False ——
    改已发布内容会重新过审、读者可能收到更新，得让人显式要。
    """
    now_ts = int(time.time()) if now_ts is None else now_ts
    cutoff = now_ts + safety_minutes * 60
    plan, skipped = [], []
    for it in sorted(items, key=lambda x: x.get("pos", x["index"])):
        # 用全书位置而不是原始 index：多卷作品的 index = 卷序号*10000 + 卷内位置
        # （见 fu.global_position），拿它直接和「第N章」比会把整卷判成错位。
        # 单卷书 pos 缺省等于 index，行为不变。
        i = it.get("pos", it["index"])
        ds = it["display_status"]
        if ds == fu.DISPLAY_PUBLISHED:
            if not include_published:
                continue                                # 已公开，默认静默跳过
            # 已公开章没有排期，"缓冲"只能看发布时间：刚发出来不到 safety_minutes
            # 的章平台可能还在收尾（改完立刻要发下一轮会打架），让给下一轮。
            ct = _ts(it.get("create_time"))
            if ct and ct >= now_ts - safety_minutes * 60:
                skipped.append((i, f"发布不足 {safety_minutes} 分钟，平台可能仍在收尾"))
                continue
        elif ds != fu.DISPLAY_PENDING:
            # 已知的那两个状态码给中文说法，别让用户对着数字猜
            _why = {4: "审核中，等审完再改",
                    7: "审核不通过 —— 它后面的章全卡在审核里，先改这一章"}.get(
                        ds, f"状态 {ds} 非待发布")
            skipped.append((i, _why))
            continue
        _n = fu.chapter_title_num(it["title"])
        if _n == i:
            continue                                    # 已经对了，不必报"跳过"
        if _n is None:
            # 楔子/番外/作者的话之类没有「第N章」的章节不参与主线编号，
            # 改写会把它的标题和正文替换成第i章的内容。
            skipped.append((i, f"标题无章节号（{it['title'][:16]}），不参与主线编号"))
            continue
        if it.get("cant_modify_reason"):
            skipped.append((i, it["cant_modify_reason"]))
            continue
        # 已公开章不该拿 timer_time 当"排期"：那是它当初的定时，现在早已无意义。
        tt = 0 if ds == fu.DISPLAY_PUBLISHED else _ts(it.get("timer_time"))
        if tt and tt <= cutoff:
            skipped.append((i, f"{safety_minutes}分钟内就要发布，不动"))
            continue
        plan.append({"index": i, "item_id": it["item_id"], "now_title": it["title"],
                     "want_num": i, "timer": tt, "published": ds == fu.DISPLAY_PUBLISHED,
                     # 回读要用原始 index 和所属卷（分页是卷内的）
                     "raw_index": it["index"], "volume_id": it.get("volume_id")})
    return plan, skipped


def build_chapter_holder(items):
    """章号 -> 现在装着它的那本书位置。同号取最靠前的那个。

    要用**全书**的 item 算，不能只喂本轮要改的那几个：章号占在 --range 之外的
    位置上也是占着，只看范围内会漏判，然后把永久重名写进线上。
    """
    holder = {}
    for it in sorted(items, key=lambda x: x.get("pos", x["index"])):
        n = fu.chapter_title_num(it["title"])
        if n is not None:
            holder.setdefault(n, it.get("pos", it["index"]))
    return holder


def resolve_title_collisions(entries, holder=None):
    """处理「番茄拒绝同名章节」：排个写得下去的序，并踢掉写了就永久重名的。

    平台给每个位置判章号唯一性，而重排是**置换**不是覆盖：位置 i 要写第 i 章，
    可第 i 章此刻还装在另一个位置上（p(i) = 现在装着第 i 章的位置）。只有那个
    位置先被改写过、章号腾出来，第 i 章才写得进去 —— 约束就是 p(i) 排在 i 前面。

    三种处理:
      · p(i) 排在 i 前面 —— 正常，占绝大多数，先写它就腾出来了。
      · p(i) 排在 i 后面 —— 本趟必然撞名。这不是错误，排到最后等下一趟就好；
        刻意放最后（而不是重试）是因为一趟能解开一大半，重试要来回几百次。
      · p(i) 不在本轮要改的位置里 —— 那个位置不会被改写，章号永远腾不出来，
        写进去就是**永久重名**，踢掉。但踢掉它自己又腾不出它的章号，占着它那个
        章号的位置也跟着写不了 —— 连锁，所以迭代到不动点为止。

    holder 来自 build_chapter_holder（全书）。返回 (ordered, dropped, deferred)，
    deferred 是排到最后的（会撞名、留给下一趟的）位置。
    """
    holder = build_chapter_holder(entries) if holder is None else holder
    live = {e["index"] for e in entries}
    changed = True
    while changed:
        changed = False
        for e in entries:
            if e["index"] not in live:
                continue
            p = holder.get(e["want_num"])
            if p is not None and p not in live:
                live.discard(e["index"])
                changed = True
    kept = [e for e in entries if e["index"] in live]
    dropped = [e for e in entries if e["index"] not in live]
    ordered = sorted(
        kept,
        key=lambda e: (0 if holder.get(e["want_num"], -1) < e["index"] else 1,
                       e["index"]))
    deferred = [e["index"] for e in ordered
                if holder.get(e["want_num"], -1) > e["index"]]
    return ordered, dropped, deferred




def check_applied(before, after, want_num):
    """对账: 平台回读结果是否就是我们要的。返回 None=通过，否则返回失败原因。

    标题号与排期都必须对上，任何一项不成立都说明"提交看起来成功但实际没生效"，
    这正是漏 151 章那类静默失败——宁可当失败中止，也不能记成功往下跑。
    """
    if after is None:
        return "回读不到该 item（平台状态未知）"
    if fu.chapter_title_num(after["title"]) != want_num:
        return f"平台标题仍是「{after['title'][:20]}」，没改成第{want_num}章"
    # 状态**不作为失败判据**: 平台审核是滞后的，改完可能先转"审核中"，
    # 也可能刚好碰上排期触发而转已发布。这两种都不代表修改没生效——真正的
    # 判据是标题号和排期。而 remap 是失败即停，为状态变化停掉整批代价太大
    # （每天额度有限、余量只有几天）。异常状态只提示，下一轮会重新对账。
    # 实测(2026-08-20): 改 82 个待发布章，状态全程保持 10，未见转审核中。
    if after["display_status"] != fu.DISPLAY_PENDING:
        print(f"    （注意：状态变成 {after['display_status']}，"
              f"多为审核滞后或排期已触发；标题与排期已核对无误，按成功计）",
              flush=True)

    # 已公开章没有排期可言：平台对它按 create_time 算，timer_time 里留的是当初
    # 的定时，拿它当判据会把「压根没动」误报成「排期被改」。
    if before.get("published"):
        return None
    if _ts(after.get("timer_time")) != _ts(before.get("timer")):
        return (f"排期被改动: {fmt(_ts(before.get('timer')))} → "
                f"{fmt(_ts(after.get('timer_time')))}")
    return None



def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def _apply_range(plan, skipped, args):
    """按 --only / --range 收窄 plan 与 skipped，返回 (plan, skipped, 错误信息)。

    skipped 必须一起筛：它来自全书的 compute_remap，不筛就会把范围外的
    「跳过 index 4783: 状态 4 非待发布」混进本次报告里——用户限定了
    480-694，看到的却是 4783~4792 的理由，纯噪音还会让人以为漏了什么。
    """
    if args.only is not None:
        return ([e for e in plan if e["index"] == args.only],
                [s for s in skipped if s[0] == args.only], None)
    if args.range:
        raw = args.range.replace(" ", "")
        m = re.fullmatch(r"(\d+)-(\d+)", raw)
        if not m:
            return plan, skipped, args.range
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            return plan, skipped, f"{lo}-{hi}（起点比终点大）"
        return ([e for e in plan if lo <= e["index"] <= hi],
                [s for s in skipped if lo <= s[0] <= hi], None)
    return plan, skipped, None


def print_plan(items, plan, skipped, num2path):
    pub = [x for x in items if x["display_status"] == fu.DISPLAY_PUBLISHED]
    pend = [x for x in items if x["display_status"] == fu.DISPLAY_PENDING]
    n_pub_rw = sum(1 for e in plan if e.get("published"))
    print(f"平台 item 总数 {len(items)}  已公开 {len(pub)}"
          f"{f'（含 {n_pub_rw} 个要改写）' if n_pub_rw else '（动不了）'}  "
          f"未公开 {len(pend)}（可改）", flush=True)
    if pend:
        ps = [x.get("pos", x["index"]) for x in pend]
        print(f"可改范围: 位置 {min(ps)}~{max(ps)}", flush=True)
    print(f"需改写 {len(plan)} 个"
          + (f"，跳过 {len(skipped)} 个" if skipped else ""), flush=True)
    if n_pub_rw:
        print(f"⚠ 其中 {n_pub_rw} 个是已发布章：内容会被改写、重新过审，"
              f"读者可能收到更新。--run 之前请把这份计划看仔细。", flush=True)
    for i, why in skipped[:10]:
        print(f"  跳过 index {i}: {why}", flush=True)
    missing = [e["want_num"] for e in plan if e["want_num"] not in num2path]
    if missing:
        print(f"⚠ 本地缺文件、无法改: {missing[:20]}"
              f"{' …' if len(missing) > 20 else ''}", flush=True)
    def _sched(e):
        """已发布章不排期，挂一列「定时 —」只是噪音（而且会让人以为漏改了什么）。"""
        return "" if e.get("published") else f"   定时 {fmt(e['timer'])}"

    for e in plan[:5]:
        print(f"  index {e['index']:>5} 现「{e['now_title'][:20]}」"
              f" → 第{e['want_num']}章{_sched(e)}", flush=True)
    if len(plan) > 5:
        print(f"  … 共 {len(plan)} 个，末个 index {plan[-1]['index']}"
              f"{_sched(plan[-1])}", flush=True)


LOG_DIR = Path(__file__).resolve().parent / "logs"




def needs_attention(rep, plan, *, now_ts=None):
    """是否需要惊动人。返回 (bool, 一句话原因)。

    两种情况人必须知道:
      · B 段非空——有已公开的错位章还在 3 天移动窗口内，过了就只能改写内容
        修整段（代价高得多），窗口内则能在 App 里直接移位；
      · 缓冲跌破移动窗口——再不补跑，错位章发出去时窗口都来不及用。
    """
    now_ts = time.time() if now_ts is None else now_ts
    if rep["in_window"]:
        soonest = min(r["left_h"] for r in rep["in_window"])
        return True, (f"{len(rep['in_window'])} 章已公开且顺序错位，"
                      f"最紧的还剩 {soonest:.0f} 小时可在 App 申请移动")
    # timer=0 表示该章还没设定时（不会自己发出去），没有"缓冲"可言，别拿 0 去减
    first = next((e for e in plan if int(e.get("timer") or 0) > 0), None)
    if first:
        left_h = (int(first["timer"]) - now_ts) / 3600
        if left_h < fu.MOVE_WINDOW_H:
            return True, (f"缓冲只剩 {left_h:.0f} 小时：位置 {first['index']} "
                          f"还没改对就要发出去了")
    return False, ""


def margin_days(plan, *, now_ts=None):
    """余量: 发布前沿追上"第一个还没改对的位置"还有多少天。

    这是本工具真正要盯的指标——不是"还剩多少章要改"，而是"还有多少天可改"。
    前沿速度按待发布章的实际排期算（每天几章由用户自己定），所以直接看
    第一个未改对的 item 什么时候发出去即可: 那一刻它就带着错内容固定在读者目录里，
    只能等 --include-published 整段改写才能纠正。
    返回 (天数, 位置) ；无待改项返回 (None, None)。
    """
    first = next((e for e in plan if int(e.get("timer") or 0) > 0), None)
    if not first:
        return None, None
    now_ts = time.time() if now_ts is None else now_ts
    return (int(first["timer"]) - now_ts) / 86400, first["index"]


def monthly_limit_outlook(days_left_in_month, margin_d):
    """撞每月上限后：余量够不够撑到下月额度重置。返回 (是否告急, 一句话)。

    撞月限意味着接下来十几二十天 remap 一章都改不动，而前沿照走不误。
    余量必须覆盖这段空窗，否则会有章带着错内容发出去。
    """
    if margin_d is None:
        return False, ""
    if margin_d < days_left_in_month:
        return True, (f"撞每月上限，本月剩 {days_left_in_month} 天改不动，"
                      f"但余量只有 {margin_d:.1f} 天——不够撑到下月，"
                      f"必须再降低每天发布章数")
    return False, (f"撞每月上限（正常）。本月剩 {days_left_in_month} 天，"
                   f"余量 {margin_d:.1f} 天，够撑到下月重置")


def _print_review_block(blk):
    """审核被拒卡住后续时，先于所有其它结论报出来。

    它是唯一一条「工具改不了、但不处理就一直卡着」的故障，所以放最前面，
    而且给到能立刻动手的位置和章号——只说"有章审核不通过"等于没说。
    """
    if not blk:
        return
    to = f"（一直排到位置 {blk['blocked_to']}）" if blk.get("blocked_to") else ""
    print("\n" + "=" * 60, flush=True)
    print(f"⚠ 审核卡住了：位置 {blk['index']} 这一章没过审核，"
          f"后面 {blk['blocked']} 章全卡在审核中{to}", flush=True)
    print(f"   被卡住的那章：位置 {blk['index']}「{blk['title'][:24]}」", flush=True)
    print("   怎么办：去番茄后台把这一章改掉重新提交；或用本工具的「修改内容」"
          "模式重发它。", flush=True)
    print(f"   ⚠ 它不会自己过审：平台按位置顺序审，这一章不解决，后面这 "
          f"{blk['blocked']} 章就一直卡着不发布。", flush=True)
    print("=" * 60 + "\n", flush=True)


def print_audit(rep, plan, include_published=False):
    """体检报告 + 缓冲告警（缓冲=第一个还没改对的 item 距离被发出去还有多久）。"""
    _print_review_block(rep.get("review_block"))
    print("\n—— 缺口体检 ——", flush=True)
    print(f"A 未公开段位置不符: {len(rep['pending_bad'])} 个 → 本工具 --run 自动改",
          flush=True)
    first = next((e for e in plan if int(e.get("timer") or 0) > 0), None)
    if first:
        left_h = (int(first["timer"]) - time.time()) / 3600
        flag = "⚠ " if left_h < fu.MOVE_WINDOW_H else ""
        print(f"  {flag}缓冲: index {first['index']} 将于 {fmt(first['timer'])} 发出"
              f"，还剩 {left_h:.1f} 小时（{left_h / 24:.1f} 天）", flush=True)
        print(f"  余量: {left_h / 24:.1f} 天（前沿追上第一个未改对位置的时间）",
              flush=True)
        if left_h < fu.MOVE_WINDOW_H:
            print("  ⚠ 缓冲已跌破移动窗口：今天必须补跑，否则错位章发出后"
                  "只能去 App 申请+逐章移动", flush=True)
    if rep["in_window"]:
        print(f"B 已公开、还能在 App 里申请移动: {len(rep['in_window'])} 章", flush=True)
        for r in rep["in_window"]:
            print(f"  第{r['num']}章（现在位置 {r['index']}）发布于 {fmt(r['pub_at'])}"
                  f"，窗口还剩 {r['left_h']:.1f} 小时", flush=True)
    if rep["expired"]:
        # 只报一行：位置动不了（App 窗口已关），但内容能改写。改写计划和上面逐条
        # 列出的目标已经说清范围了，这里再展开一遍只会和 GUI 的体检报告打架。
        if include_published:
            lo = min((e["index"] for e in plan), default=0)
            hi = max((e["index"] for e in plan), default=0)
            print(f"C 已公开段顺序错位：{len(plan)} 章（位置 {lo}-{hi}）"
                  f"，位置不可动、内容可改写", flush=True)
        else:
            print(f"C 已公开、超窗口无解: {len(rep['expired'])} 章 —— "
                  + "、".join(f"第{r['num']}章(位置{r['index']})"
                              for r in rep["expired"]), flush=True)
    if not (rep["in_window"] or rep["expired"]):
        print("B/C 已公开段无顺序倒挂", flush=True)


async def run_remap(page, book_id, entries, num2path, *,
                    use_ai=False, max_retries=2, tolerate_dup=False):
    """逐章改写。**只认提交时的接口反馈**，不做逐章回读。

    返回 (new_ok, fail, dup)。dup 是「章号被占、没写进去」的位置 —— 那是没写成，
    但也不是失败：占着它的位置改写完，名字就释放了（主工具的修改模式也这么
    处理）。tolerate_dup 打开时不因它中止整批，交给调用方再走一趟。

    为什么不逐章回读: 平台的生效是滞后的——改动要过审才反映到列表接口，
    标题号同理。提交本身已有权威信号（publish_article 的 code），有问题
    平台当场就报；而"列表还没反映"根本不是失败。2026-08-20 实测: 位置770
    提交成功、正文逐字吻合，列表 60 秒内仍显示旧标题，却被判死中止整批，
    当天剩余 547 章全没跑。逐章回读还要给每章多花一次请求。
    → 回读统一挪到批末（verify_batch），没反映的按窗口轮询等审核。

    失败即停仍然保留，但只针对**真失败**: 提交被拒、撞字数上限、本地文件
    对不上。队尾停下无害，下一轮从平台真实状态重算、接着修。
    """
    new_ok, fail, dup = [], [], []

    def abort(n, reason):
        fail.append((entries[n - 1]["index"], reason))
        fail.extend((x["index"], "前方中止，未处理") for x in entries[n:])
        return new_ok, fail, dup

    for n, e in enumerate(entries, 1):
        want = e["want_num"]
        path = num2path.get(want)
        if not path:
            print(f"    ✗ 本地缺 第{want}章 文件 —— 中止整批", flush=True)
            return abort(n, f"本地缺 第{want}章 文件")
        cnum, title, content = fu.parse_md_file(Path(path))
        try:
            cnum_int = int(cnum) if cnum is not None else None
        except (TypeError, ValueError):
            cnum_int = None
        if cnum_int != want:
            print(f"    ✗ 本地文件章节号不符({cnum!r}) —— 中止整批", flush=True)
            return abort(n, f"本地文件章节号不符({cnum!r})")
        print(f"[{n}/{len(entries)}] index {e['index']}: 「{e['now_title'][:18]}」"
              f" → 第{want}章 {title[:18]}", flush=True)
        url = EDIT_URL_TPL.format(book_id=book_id, item_id=e["item_id"])
        try:
            done, err = await fu.edit_one_chapter(
                page, url, want, title, content,
                use_ai=use_ai, max_retries=max_retries, set_num=str(want))
        except fu.DailyLimitReached as ex:
            print(f"    撞字数上限（{ex}），中止整批", flush=True)
            return abort(n, f"字数上限:{ex}")
        if not done:
            # 「重复」= 章号还被另一个位置占着，不是真失败。不这么放，整个置换
            # 会被第一个撞名的位置卡死：那正是 resolve_title_collisions 里排到
            # 最后那批（它们占着的位置还没轮到改写），等下一趟就好。
            if tolerate_dup and err and "重复" in err:
                dup.append(e["index"])
                print(f"    ~ 章号「{want}」仍被占用（{err[:28]}），留到下一趟",
                      flush=True)
                continue
            print(f"    ✗ 改写失败: {err} —— 中止整批", flush=True)
            return abort(n, err or "未知失败")
        new_ok.append(e["index"])
    return new_ok, fail, dup


async def verify_batch(page, book_id, entries, done_positions, *,
                       window_s=_VERIFY_WINDOW_S, poll_s=_VERIFY_POLL_S):
    """批末复核: 重抓平台真实状态核实本批目标，没反映的按窗口轮询等审核。

    返回 (still_pending, fresh_items)。still_pending 不算失败——下一轮会从
    平台真实状态重算，真没生效的会被再改一次。
    """
    byidx = {e["index"]: e for e in entries}
    pending = list(done_positions)
    deadline = time.monotonic() + window_s
    fresh = []
    while True:
        fresh = (await fu.fetch_chapter_items(page, book_id))[0]
        bypos = {x.get("pos", x["index"]): x for x in fresh}
        still = []
        for i in pending:
            after = bypos.get(i)
            if after is None or check_applied(byidx.get(i, {}), after, i):
                still.append(i)
        pending = still
        if not pending or time.monotonic() >= deadline:
            return pending, fresh
        print(f"  复核: 还有 {len(pending)} 章平台未反映（审核滞后），"
              f"{poll_s}s 后重查…", flush=True)
        await page.wait_for_timeout(poll_s * 1000)


async def main_async(args):
    book_id, num2path, headless = fu.tool_startup(args)
    if not book_id:
        # 必须返回 True(=需人工)：run_unattended 靠返回值决定 exit 3 与弹窗，
        # 返回 None 会让 --daily 的计划任务每晚"成功"退出 0 却什么都没做，
        # 而错位积压会一路冲过 72 小时的 App 可移动窗口。
        return True

    async with async_playwright() as p:
        browser, ctx = await fu.create_context(p, headless=headless)
        page = await ctx.new_page()
        try:
            print("抓取平台章节状态…", flush=True)
            items, signed, volumes = await fu.fetch_chapter_items(page, book_id)
            nvol = fu.volume_count(volumes)
            if nvol > 1:
                # 位置已按全书连续换算（卷序号*10000+卷内位置 → 累加偏移），
                # 所以"位置 i 装第 i 章"跨卷同样成立；改写按 item_id 走，不受卷影响。
                print(f"本作品 {nvol} 卷，已跨卷合并 {len(items)} 章，"
                      f"位置按全书连续计：", flush=True)
                for v in volumes:
                    print(f"    卷{v.get('index')} {v.get('volume_name')}"
                          f"  {v.get('item_count')} 章", flush=True)
            plan, skipped = compute_remap(
                items, safety_minutes=args.safety_minutes,
                include_published=args.include_published)
            # 范围筛选必须提到打印之前。compute_remap 是在**全书**上跑的，
            # --range 480-694 若留到后面才筛，打印出来的「需改写 N 个」和
            # 「跳过 index 4783: 状态 4 非待发布」都是全书口径 —— 用户限定了
            # 480-694，却看到 4783~4792 的跳过理由，跟本次操作毫无关系。
            plan, skipped, bad_range = _apply_range(plan, skipped, args)
            if bad_range:
                print(f"位置区间格式不对: {bad_range}", flush=True)
                print("  应为「起始-结束」，例如 480-694", flush=True)
                return True
            print_plan(items, plan, skipped, num2path)
            rep = fu.audit_chapter_positions(items)
            print_audit(rep, plan, args.include_published)
            attn, why = needs_attention(rep, plan)
            if args.audit:
                return attn
            if attn:
                print(f"⚠ 需要人工处理: {why}", flush=True)

            # 这里是真写入（改写正文），拿错目录后果尤其重
            if fu.book_mismatch_abort(items, num2path):
                return True

            # 范围已经在 _apply_range 里作用于 plan，这里直接用。
            entries = plan
            # 同名冲突在这里解决，且必须在 --limit 之前：截断出来的子集会把
            # 「本来会腾出章号」的位置切掉，剩下的一律被判永久重名、整批不写。
            holder = build_chapter_holder(items)
            entries, dropped, deferred = resolve_title_collisions(entries, holder)
            if dropped:
                print(f"\n踢掉 {len(dropped)} 个会造成永久重名的位置"
                      f"（它们要的章号还占在本轮改不到的地方）:", flush=True)
                for e in dropped[:10]:
                    print(f"  位置 {e['index']}（现装「{e['now_title'][:18]}」）："
                          f"第{e['want_num']}章 还占在位置 "
                          f"{holder.get(e['want_num'])}，把它一起纳入 --range "
                          f"才能换", flush=True)
            if deferred:
                print(f"另有 {len(deferred)} 个位置本趟会撞名（章号还没腾出来），"
                      f"已排到最后、跑完自动再走一趟: {deferred[:10]}"
                      f"{' …' if len(deferred) > 10 else ''}", flush=True)
            if args.limit:
                entries = entries[:args.limit]

            if not args.run:
                print(f"\n[dry-run] 本次将改写 {len(entries)} 个 item。"
                      f"确认无误后加 --run 才会真正写入。", flush=True)
                return attn
            if not entries:
                # 比如剩余的都在"60分钟内要发布"里被跳过——告警仍要带出去
                print("没有需要改写的 item。", flush=True)
                return attn
            print(f"\n开始改写 {len(entries)} 个 item —— 3 秒后开始…", flush=True)
            await page.wait_for_timeout(3000)
            ok, fail, dup = await run_remap(page, book_id, entries, num2path,
                                            use_ai=args.use_ai,
                                            tolerate_dup=True)
            # 撞名的第二趟：占着章号的位置这趟已经改写过了，名字腾出来了。
            # 只在没出真失败时继续 —— 真失败说明流程本身有问题，换一轮重跑整个
            # 工具更合适（它每次都从平台真实状态重算）。
            rnd, prev = 0, len(dup)
            while dup and not fail and rnd < _DUP_MAX_ROUNDS:
                rnd += 1
                print(f"\n第 {rnd + 1} 趟：重写 {len(dup)} 个章号被占的 item…",
                      flush=True)
                again = [e for e in entries if e["index"] in dup]
                ok2, fail2, dup2 = await run_remap(page, book_id, again, num2path,
                                                    use_ai=args.use_ai,
                                                    tolerate_dup=True)
                ok += ok2
                fail += fail2
                dup = dup2
                if len(dup) >= prev:
                    break                    # 没有进展，再转也是白转
                prev = len(dup)
            if dup:
                print(f"  · {len(dup)} 章仍未写入（章号一直被占）: "
                      f"{dup[:10]}{' …' if len(dup) > 10 else ''}", flush=True)
                print("    不算失败——再跑一轮本工具，状态会重算", flush=True)
            print("=" * 50, flush=True)
            print(f"完成  成功 {len(ok)}  失败 {len(fail)}  "
                  f"章号被占未写入 {len(dup)}", flush=True)
            for i, why in fail[:30]:
                print(f"  index {i}: {why}", flush=True)
            # 复核: 重新抓一遍平台真实状态，报还剩多少位置与章号不符
            # 批末复核：重抓平台真实状态核实本批目标；没反映的按窗口轮询等审核
            not_yet, fresh = await verify_batch(page, book_id, entries, ok)
            print(f"批末复核: 本批提交 {len(ok)} 章，平台已反映 "
                  f"{len(ok) - len(not_yet)} 章", flush=True)
            if not_yet:
                print(f"  · 等满 {_VERIFY_WINDOW_S // 60} 分钟仍未反映 "
                      f"{len(not_yet)} 章: {not_yet[:10]}"
                      f"{' …' if len(not_yet) > 10 else ''}", flush=True)
                print("    不算失败——下一轮会按平台真实状态重算，"
                      "真没生效的会被再改一次", flush=True)
            left, _ = compute_remap(fresh, safety_minutes=args.safety_minutes)
            print(f"复核: 未公开段还有 {len(left)} 个 item 位置与章号不符", flush=True)
            attn, why = needs_attention(fu.audit_chapter_positions(fresh), left)
            # 异常中止（≠字数上限）也要惊动人：撞上限是每天的常态，
            # 但改写失败/对账不过若不报，会每天静默卡在同一处
            abnormal = next((w for _i, w in fail
                             if "字数上限" not in w and w != "前方中止，未处理"),
                            None)
            # 撞每月上限 ≠ 撞每日上限: 后者明天照跑，前者意味着本月剩下的日子
            # 一章都改不动，而发布前沿照走。必须当场算余量够不够撑到下月。
            hit_monthly = any(fu.is_monthly_limit(w) for _i, w in fail)
            if hit_monthly:
                md, _pos = margin_days(left)
                today = datetime.now().date()
                days_left = calendar.monthrange(today.year, today.month)[1] - today.day
                bad, msg = monthly_limit_outlook(days_left, md)
                print(f"  {msg}", flush=True)
                if bad and not attn:
                    attn, why = True, msg
            if abnormal and not attn:
                attn, why = True, f"批次异常中止: {abnormal}"
            if attn:
                print(f"⚠ 需要人工处理: {why}", flush=True)
            return attn
        finally:
            await fu.close_browser_safely(browser)


def demo():
    """零依赖自检: 已公开不碰 / 临近发布不碰 / 已对齐不动 / 升序 / 位置i←第i章。"""
    now, hour = 1_000_000, 3600
    items = [
        # 已公开的即使错位也不碰
        {"index": 1, "title": "第1章 甲", "display_status": fu.DISPLAY_PUBLISHED,
         "timer_time": 0, "item_id": "a", "cant_modify_reason": ""},
        {"index": 2, "title": "第9章 乙", "display_status": fu.DISPLAY_PUBLISHED,
         "timer_time": 0, "item_id": "b", "cant_modify_reason": ""},
        # 已经对了 → 不动
        {"index": 3, "title": "第3章 丙", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 10 * hour, "item_id": "c", "cant_modify_reason": ""},
        # 半小时后就发 → 跳过
        {"index": 4, "title": "第5章 丁", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + hour // 2, "item_id": "d", "cant_modify_reason": ""},
        # 乱序输入，要求输出按 index 升序
        {"index": 6, "title": "第8章 己", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 20 * hour, "item_id": "f", "cant_modify_reason": ""},
        {"index": 5, "title": "第7章 戊", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 15 * hour, "item_id": "e", "cant_modify_reason": ""},
        # 平台说改不了
        {"index": 7, "title": "第9章 庚", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 30 * hour, "item_id": "g", "cant_modify_reason": "审核中"},
    ]
    plan, skipped = compute_remap(items, now_ts=now, safety_minutes=60)
    assert [e["index"] for e in plan] == [5, 6], plan
    assert [e["want_num"] for e in plan] == [5, 6], plan
    assert [e["item_id"] for e in plan] == ["e", "f"], plan
    assert dict(skipped)[4].startswith("60分钟内"), skipped
    assert dict(skipped)[7] == "审核中", skipped
    assert fu.chapter_title_num("第 12 回 x") == 12 and fu.chapter_title_num("番外") is None
    # 全部已公开 → 什么都不做
    assert compute_remap(items[:2], now_ts=now) == ([], [])

    # --- 对账: 平台数据说了才算数 ---
    before = {"index": 709, "want_num": 709, "timer": 1_700_000_000}
    good = {"index": 709, "title": "第709章 甲", "display_status": fu.DISPLAY_PENDING,
            "timer_time": "1700000000"}
    assert check_applied(before, good, 709) is None
    assert "回读不到" in check_applied(before, None, 709)                    # 读不到=不算成功
    assert "没改成第709章" in check_applied(
        before, dict(good, title="第710章 甲"), 709)                        # 标题没变=没生效
    # 状态变化不判失败（审核滞后/排期触发都会变状态，但修改其实已生效）
    assert check_applied(before, dict(good, display_status=fu.DISPLAY_PUBLISHED), 709) is None
    assert "排期被改动" in check_applied(
        before, dict(good, timer_time="1700003600"), 709)                   # 排期被吃掉
    # 倒序分页定位: 页 0 装 index total..total-99

    # --- 体检分段: 倒挂才算错位，均匀后移(缺号)不算 ---
    H = 3600
    aud = [
        # 位置1..3 装第1,3,4章: 缺了第2章，但顺序单调 → 不算倒挂
        {"index": 1, "title": "第1章", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 100 * H, "timer_time": 0},
        {"index": 2, "title": "第3章", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 99 * H, "timer_time": 0},
        {"index": 3, "title": "第4章", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 98 * H, "timer_time": 0},
        # 补发吊在末尾: 第2章排在第4章之后 → 倒挂。发布于 1 小时前 → 还在窗口内
        {"index": 4, "title": "第2章", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 1 * H, "timer_time": 0},
        # 同样倒挂但发布于 100 小时前 → 超窗口
        {"index": 5, "title": "第3章", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 100 * H, "timer_time": 0},
        # 未公开且位置不符 → A 段
        {"index": 6, "title": "第9章", "display_status": fu.DISPLAY_PENDING,
         "create_time": now, "timer_time": now + 50 * H},
    ]
    # --- 多卷: index = 卷序号*10000 + 卷内位置，位置要按全书连续算 ---
    #     (真多卷作品实测: 卷1 index 1~100、卷2 10001~10150、卷3 20001~20115)
    multi = [
        {"index": 100, "pos": 100, "title": "第100章", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 50 * H, "item_id": "a", "cant_modify_reason": "",
         "volume_id": "v1"},
        # 卷2 第 1 个位置 = 全书第 101 位；标题却是第102章 → 该改
        {"index": 10001, "pos": 101, "title": "第102章", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 51 * H, "item_id": "b", "cant_modify_reason": "",
         "volume_id": "v2"},
        # 卷2 第 2 个位置 = 全书第 102 位；标题正好 → 不该动
        {"index": 10002, "pos": 102, "title": "第102章", "display_status": fu.DISPLAY_PENDING,
         "timer_time": now + 52 * H, "item_id": "c", "cant_modify_reason": "",
         "volume_id": "v2"},
    ]
    mplan, _ = compute_remap(multi, now_ts=now)
    assert [e["index"] for e in mplan] == [101], mplan       # 只有全书第101位要改
    assert mplan[0]["want_num"] == 101, mplan                # 装第101章
    assert mplan[0]["raw_index"] == 10001, mplan             # 回读要用原始 index
    assert mplan[0]["volume_id"] == "v2", mplan              # 且要按卷翻页
    # 若按原始 index 比（旧逻辑），10001 会被当成"位置 10001"，整卷判错位
    assert all(e["index"] < 1000 for e in mplan), "位置没做卷偏移换算"
    assert fu.global_position(20016, {0: 0, 1: 100, 2: 250}) == 266
    assert fu.global_position(50, {0: 0}) == 50               # 单卷等同 index

    # --- 每月上限 vs 每日上限: 处置完全不同 ---
    assert fu.is_monthly_limit("提交字数超出每月上限")
    assert not fu.is_monthly_limit("提交字数超出每日上限")
    # 余量 2 天、本月还剩 10 天改不动 → 撑不到下月，必须告警
    bad, msg = monthly_limit_outlook(10, 2.0)
    assert bad and "不够撑到下月" in msg, msg
    # 余量 20 天、本月还剩 10 天 → 正常，不吵
    ok_, msg2 = monthly_limit_outlook(10, 20.0)
    assert not ok_ and "够撑到下月" in msg2, msg2
    # 没有待改项就无所谓
    assert monthly_limit_outlook(10, None) == (False, "")

    # 余量天数取"第一个未改对位置"的发布时刻
    md, pos = margin_days([{"index": 9, "timer": now + 48 * H}], now_ts=now)
    assert abs(md - 2.0) < 0.01 and pos == 9, (md, pos)
    assert margin_days([{"index": 9, "timer": 0}], now_ts=now) == (None, None)

    # --- 该不该惊动人 ---
    b_hit = {"in_window": [{"num": 5, "index": 9, "left_h": 12.0}],
             "expired": [], "pending_bad": []}
    none_hit = {"in_window": [], "expired": [], "pending_bad": [9]}
    a, why = needs_attention(b_hit, [], now_ts=now)
    assert a and "12 小时" in why, why                       # B 段非空必须报
    a, why = needs_attention(none_hit, [{"index": 9, "timer": now + 40 * H}],
                             now_ts=now)
    assert a and "40 小时" in why, why                       # 缓冲跌破窗口必须报
    a, why = needs_attention(none_hit, [{"index": 9, "timer": now + 100 * H}],
                             now_ts=now)
    assert not a and why == "", (a, why)                    # 缓冲充裕不吵
    assert not needs_attention({"in_window": [], "expired": [], "pending_bad": []},
                               [], now_ts=now)[0]

    rep = fu.audit_chapter_positions(aud, now_ts=now, window_h=72)
    assert rep["pending_bad"] == [6], rep
    assert [r["num"] for r in rep["in_window"]] == [2], rep
    assert [r["num"] for r in rep["expired"]] == [3], rep
    assert 70 < rep["in_window"][0]["left_h"] < 72, rep      # 剩 71 小时
    # --- 铁律: 回读失败绝不能中止批次 ---
    # 平台生效是滞后的（改动要过审才反映到列表，标题号同理），把"读不到"
    # 当失败会白白丢掉整批。2026-08-20 实测就因此丢了 547 章的机会。
    import inspect as _insp, re as _re
    _src = _insp.getsource(run_remap)
    assert "read_back" not in _src, "run_remap 不该再逐章回读"
    assert "check_applied" not in _src, "回读判据不该出现在提交循环里"
    assert "verify_batch" in _insp.getsource(main_async), "批末必须复核"
    _reasons = _re.findall(r"return abort\(n, ([^)]+)\)", _src)
    assert _reasons, "找不到 abort 调用"
    for _r in _reasons:
        assert ("字数上限" in _r or "本地" in _r or "err" in _r
                or "未知失败" in _r), f"回读类原因不该中止批次: {_r}"

    # --- 已公开段: --include-published 才纳入，缓冲改看发布时间 ---
    pub_shift = [
        # 已公开且装第1章 → 不动
        {"index": 1, "title": "第1章 甲", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 10 * H, "timer_time": 0, "item_id": "a",
         "cant_modify_reason": ""},
        # 已公开但整段位移（装第3章却占着位置2）→ 默认不碰，开关打开才进计划
        {"index": 2, "title": "第3章 乙", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 10 * H, "timer_time": 0, "item_id": "b",
         "cant_modify_reason": ""},
        # 已公开、发布不到缓冲 → 让给下一轮
        {"index": 3, "title": "第5章 丙", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - H // 2, "timer_time": 0, "item_id": "c",
         "cant_modify_reason": ""},
        # 已公开、平台明说改不了 → 一样跳过
        {"index": 4, "title": "第6章 丁", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 10 * H, "timer_time": 0, "item_id": "d",
         "cant_modify_reason": "完结作品不可修改"},
    ]
    assert compute_remap(pub_shift, now_ts=now) == ([], []), "默认不该碰已发布章"
    p_pub, s_pub = compute_remap(pub_shift, now_ts=now, include_published=True)
    assert [e["index"] for e in p_pub] == [2], p_pub
    assert p_pub[0]["published"] is True and p_pub[0]["timer"] == 0, p_pub
    _sk = dict(s_pub)
    assert "发布不足" in _sk[3], s_pub
    assert _sk[4] == "完结作品不可修改", s_pub
    # 已公开章没有排期可比：check_applied 不能拿 timer_time 判它
    assert check_applied({"published": True, "timer": 0},
                         {"title": "第2章 乙",
                          "display_status": fu.DISPLAY_PUBLISHED,
                          "timer_time": "1699999999"}, 2) is None
    assert "没改成第2章" in check_applied(
        {"published": True, "timer": 0},
        {"title": "第3章 乙", "display_status": fu.DISPLAY_PUBLISHED}, 2)

    # --- 同名冲突: 环形互换 ---
    # 环形互换: 位置1 装第2章、位置2 装第1章。要点——约束是"占着章号的位置
    # 必须先改写"，即 p(i) 排在 i 前面。p(1)=2 > 1 → 位置1 排最后；p(2)=1 < 2
    # → 位置2 照常排前面。本趟位置2 会撞名（它的第2章还占在没改的位置1），
    # 位置1 写完后第2章才腾得出来，第二趟补上 —— 一个都不该被踢。
    ring_items = [{"index": 1, "title": "第2章 甲"}, {"index": 2, "title": "第1章 乙"}]
    _h = build_chapter_holder(ring_items)
    _ring = [{"index": 1, "now_title": "第2章 甲", "want_num": 1},
             {"index": 2, "now_title": "第1章 乙", "want_num": 2}]
    _ord, _drop, _def = resolve_title_collisions(_ring, _h)
    assert [e["index"] for e in _ord] == [2, 1], _ord
    assert _drop == [], _drop
    assert _def == [1], _def

    # 目标章号占在本轮改不到的位置 → 写进去就是永久重名，踢掉。
    # 位置1 想要的第1章还占在位置0，而位置0 不在 entries 里。位置1 自己装的是
    # 第9章，本轮没人要第9章 —— 所以它被踢不会连累别人（连锁见下一段用例）。
    orphan_items = [{"index": 0, "title": "第1章 甲"},
                    {"index": 1, "title": "第9章 乙"},
                    {"index": 5, "title": "第7章 丙"}]
    _ho = build_chapter_holder(orphan_items)
    _orphan = [{"index": 1, "now_title": "第9章 甲", "want_num": 1},
               {"index": 5, "now_title": "第7章 乙", "want_num": 5}]
    _o2, _d2, _f2 = resolve_title_collisions(_orphan, _ho)
    assert [e["index"] for e in _o2] == [5], _o2
    assert [e["index"] for e in _d2] == [1], _d2

    # 连锁踢：位置1 被踢后它装着的第2章腾不出来，占着第2章的位置2 也跟着写不了。
    # 只踢一次是不够的，必须迭代到不动点。
    cas_items = [{"index": 0, "title": "第1章 甲"}, {"index": 1, "title": "第2章 乙"}]
    _hc = build_chapter_holder(cas_items)
    _cas = [{"index": 1, "now_title": "第2章 甲", "want_num": 1},
            {"index": 2, "now_title": "第9章 乙", "want_num": 2}]
    _o3, _d3, _f3 = resolve_title_collisions(_cas, _hc)
    assert [e["index"] for e in _o3] == [], _o3
    assert [e["index"] for e in _d3] == [1, 2], _d3

    # 实测结构(2026-10-10): 位置 10/20/30 想要的章号分别被顶到 40/41/42。
    real_items = [{"index": 10, "title": "第11章 甲"},
                  {"index": 20, "title": "第21章 乙"},
                  {"index": 30, "title": "第31章 丙"},
                  {"index": 40, "title": "第10章 丁"},
                  {"index": 41, "title": "第20章 戊"},
                  {"index": 42, "title": "第30章 己"}]
    _hr = build_chapter_holder(real_items)
    _all = [{"index": i, "now_title": t, "want_num": i} for i, t in
            ((it["index"], it["title"]) for it in real_items)]
    # 全纳入 → 一个都不踢，且 10/20/30 排到最后（它们的章号还在 40/41/42）
    _o5, _d5, _f5 = resolve_title_collisions(_all, _hr)
    assert _d5 == [], _d5
    assert _f5 == [10, 20, 30], _f5
    # 顺序也对：40/41/42 的章号已经缺位（holder 里没有），照常先写；
    # 10/20/30 的章号还压在上面那三个位置，垫到最后。
    assert [e["index"] for e in _o5] == [40, 41, 42, 10, 20, 30], _o5
    # 只纳入 10/20/30（40-42 在范围外）→ 三个全被踢，写进去必永久重名
    _o4, _d4, _f4 = resolve_title_collisions(_all[:3], _hr)
    assert [e["index"] for e in _o4] == [], _o4
    assert [e["index"] for e in _d4] == [10, 20, 30], _d4

    print("demo ok")


def main():
    ap = argparse.ArgumentParser(description="章节重排（只动未公开的章）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--only", type=int, help="只改指定位置（全书位置，多卷已折算；灰度用）")
    g.add_argument("--range", type=str, help="只改位置区间（全书位置），如 709-800")
    ap.add_argument("--limit", type=int, help="本次最多改多少个")
    ap.add_argument("--include-published", action="store_true",
                    help="把已公开的章也纳入改写（用于修已发布段的顺序错位）。"
                         "平台允许改已发布章，但内容会重新过审、读者可能收到"
                         "更新 —— 务必先看 dry-run")
    ap.add_argument("--run", action="store_true", help="真的写入（不加只预览）")
    ap.add_argument("--safety-minutes", type=int, default=SAFETY_MINUTES,
                    help="跳过多少分钟内就要发布的章（默认 60）")
    ap.add_argument("--book-id", type=str, help="覆盖 book_id")
    ap.add_argument("--content-dir", type=str, help="覆盖本地章节目录")
    ap.add_argument("--use-ai", action="store_true")
    ap.add_argument("--headless", action="store_true",
                    help="无头运行（覆盖 config.json 的 headless）")
    ap.add_argument("--show-browser", action="store_true",
                    help="显示浏览器窗口（覆盖 config.json 的 headless）")
    ap.add_argument("--daily", action="store_true",
                    help="无人值守日常跑：等于 --run --headless，"
                         "输出写 tools/remap/logs/，需要人工处理时弹窗")
    ap.add_argument("--audit", action="store_true",
                    help="只做缺口体检（A/B/C 三段 + 缓冲告警），不改任何东西")
    ap.add_argument("--self-check", action="store_true", help="只跑自检，不联网")
    args = ap.parse_args()
    if args.self_check:
        demo()
        return
    fu.run_unattended(main_async, args, log_dir=LOG_DIR, name="重排",
                      hint="（缓冲每天少 6 章）")


if __name__ == "__main__":
    main()
