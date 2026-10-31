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
  位置 i 的 item 装第 i 章。未公开（=10 待发布）的用「修改内容」逐章改写章节号
  +标题+正文，排期（timer_time）保持不变；写入先后按章号依赖排（见下面安全性
  那条）。改完队列覆盖到第（最大 index）章，原来的中段缺口变成"队列末尾少排了
  N 章"，之后按正常上传接着往后排即可。

  已公开（display_status=1）的章默认不碰 —— 加 --include-published 才纳入。
  **平台是允许改已发布章的**：可编辑与否平台只给一个 cant_modify_reason 字段
  （完结作品 / 审核中之类），跟 display_status 无关；主工具的修改模式也一直在
  改已发布章。默认跳过是本工具自己画的保守线，不是平台限制。

  闭环（位置互相占着章号，如 480 装 481、481 装 482 … 最后又装回 480）用纯
  改写永远解不开：番茄拒绝同名/同段落章节，先写环上哪一章，它要的章号都还在
  环上另一个位置。2026-10-30 实测：用合成「调整中」占位破环会被平台按内容
  安全规范驳回（本章打卡字数清零）。所以工具**不生成任何合成占位正文**：
  默认自动用真实正文解开（本地未发布章节，一个闭环一份；加 --no-buffer 改为整环跳过）；
  也可用 --buffer-file（真实文本文件，不剧透）指定缓冲。无人值守（--daily）默认不自动解。
  换位完成后会写回正确章节。要过审的章（已公开/被驳回）
  提交后必须等平台生效再写依赖它的下一章，审核没跑完就暂停，等审完重跑继续。

  为什么要开这个开关: 已公开段的错位是**位移**而不是缺章 —— 有几章被顶到书尾
  之后，中间整段就跟着偏移了 1/2/3 级（实测 2026-10-10: 位置 480-522 装
  第481-523章、523-667 装第525-669章、668-691 装第671-694章，而 692/693/694
  倒插着第480/524/670章）。这种错位靠「移动位置」治不了 —— 3 天窗口早过了；
  但它**可以靠改写内容治**：把位置 i 改回装第 i 章，整段立刻归位，且内容不会
  丢（读的是本地稿子，第480/524/670章 会回到位置 480/524/670）。

安全性:
  · 默认只改 display_status=10、以及审核不通过（7）但平台没给「不可修改」
    理由的章；--include-published 才把已公开的纳入，它自己也要过
    cant_modify_reason 这关。
  · 跳过即将发布的章（默认 60 分钟内），避免和平台的发布动作抢。对已公开章，
    这个冷却改看发布时间 —— 刚发布不到 60 分钟的章平台可能还在收尾。
  · 写入顺序按章号依赖排：位置 i 要的第 i 章此刻还装在 p(i)，得等 p(i) 先
    改写过、章号腾出来才写得进去。中途停在哪都是"已写的一定在依赖前"，
    不会产生乱序或重复。闭环见上，默认只报告。
  · 会导致**永久重名**的（目标章号占在一个本轮不会改写的位置上）直接踢掉
    不写，并连锁到依赖它的位置。详见 resolve_write_plan。
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
  # 闭环默认自动用未发布章解开；加 --no-buffer 整环跳过；也可用 --buffer-file：
  python tools/remap/remap.py --include-published --range 480-694 --no-buffer --run
  python tools/remap/remap.py --include-published --range 480-694 --buffer-file buffer.md --run

  python tools/remap/remap.py --daily            # 无人值守日常跑（写日志+告警弹窗）
  python tools/remap/remap.py --self-check       # 纯离线自检

每天自动跑（--daily 自己管日志和告警，不需要再套 shell 脚本）:
  Windows  计划任务 → pythonw.exe "<仓库>\\tools\\remap\\remap.py" --daily
           （pythonw 没有控制台窗口，不会杵一个黑框）
  Linux/macOS  crontab -e →
           10 0 * * * cd <仓库> && python3 tools/remap/remap.py --daily
"""
import argparse
import asyncio
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
_APPLY_WINDOW_S = 600   # 单章提交后等平台审核/生效的窗口；超时就暂停，等重跑
_APPLY_POLL_S = 30
_HEARTBEAT_S = 30       # 单章操作超过这么久就报一次「还在动」
# 进度行的格式只有这一份定义：CLI 打印它，GUI 靠它把子进程的输出变成
# 进度条。两边各写一份必然对不上（改了一边忘了另一边，用户看到的是错的
# 进度——比没有进度更糟）。
STEP_PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")


def _ts(v):
    """平台给的时间戳有的是 int、有的是 "0" 这种字符串，统一下沉成 int。"""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def compute_remap(items, *, now_ts=None, safety_minutes=SAFETY_MINUTES,
                  include_published=False):
    """纯函数: 算出「哪些 item 要改成第几章」。

    items: [{index, pos, title, display_status, timer_time, create_time,
             item_id, cant_modify_reason, volume_id}]
      index/title/display_status/item_id 必填；pos/create_time/volume_id 可缺省
      （单卷书 pos 缺省等于 index）。
    返回 (plan, skipped):
      plan    = [{index, item_id, now_title, want_num, timer, published,
                  review_failed, needs_review, raw_index, volume_id}]，
                按 index 升序（index 是全书面位置，raw_index 是卷内原始下标）
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
            # 已公开章没有排期，冷却只能看发布时间：刚发出来不到 safety_minutes
            # 的章平台可能还在收尾（改完立刻要发下一轮会打架），让给下一轮。
            ct = _ts(it.get("create_time"))
            if ct and ct >= now_ts - safety_minutes * 60:
                skipped.append((i, f"发布不足 {safety_minutes} 分钟，平台可能仍在收尾"))
                continue
        elif (ds == fu.DISPLAY_REVIEW_FAILED
              and not (it.get("cant_modify_reason") or "").strip()):
            # 审核不通过且平台没给"不可修改"的理由 → 编辑入口是可用的（实测）。
            # 平台上那些被驳回的临时章就是 7；允许改写才能把这一章收尾写回正确内容。
            pass
        elif ds != fu.DISPLAY_PENDING:
            # 已知的那几个状态码给中文说法，别让用户对着数字猜。
            # 平台给了理由就照抄理由——它比我们的猜测更具体。
            _why = (it.get("cant_modify_reason")
                    or {fu.DISPLAY_REVIEWING: "审核中，等审完再改",
                        fu.DISPLAY_REVIEWING_PUBLISHED:
                            "已发布章的修改审核中，等审完再改",
                        fu.DISPLAY_REVIEW_FAILED:
                            "审核不通过（平台给了不可修改的理由）"}.get(
                        ds, f"状态 {ds} 非待发布"))
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
        # 已公开/曾被驳回的章都没有有效排期：timer_time 里留的是当初的定时。
        _needs_review = ds in (fu.DISPLAY_PUBLISHED, fu.DISPLAY_REVIEW_FAILED)
        tt = 0 if _needs_review else _ts(it.get("timer_time"))
        if tt and tt <= cutoff:
            skipped.append((i, f"{safety_minutes}分钟内就要发布，不动"))
            continue
        plan.append({"index": i, "item_id": it["item_id"], "now_title": it["title"],
                     "want_num": i, "timer": tt,
                     "published": ds == fu.DISPLAY_PUBLISHED,
                     "review_failed": ds == fu.DISPLAY_REVIEW_FAILED,
                     "needs_review": _needs_review,
                     # 回读要用原始 index 和所属卷（分页是卷内的）
                     "raw_index": it["index"], "volume_id": it.get("volume_id")})
    return plan, skipped


def build_chapter_holders(items):
    """章号 -> 装着它的所有位置（升序）。

    要用**全书**的 item 算：章号占在 --range 之外的位置上也是占着，只看范围内
    会漏判，然后把永久重名写进线上。同名多份也全部返回——只记首个会把另一份
    当成"已经腾出来"，写进去照样撞重名。
    """
    holders = {}
    for it in sorted(items, key=lambda x: x.get("pos", x["index"])):
        n = fu.chapter_title_num(it["title"])
        if n is not None:
            holders.setdefault(n, []).append(it.get("pos", it["index"]))
    return holders


def _topo(nodes, deps, order_key):
    """Kahn 拓扑排序。返回 (能排的, 排不掉的=环上的)。"""
    indeg = {n: len(deps[n]) for n in nodes}
    dependents = {n: set() for n in nodes}
    for n in nodes:
        for d in deps[n]:
            dependents.setdefault(d, set()).add(n)
    ready = [n for n in nodes if indeg[n] == 0]
    order = []
    while ready:
        ready.sort(key=order_key)
        n = ready.pop(0)
        order.append(n)
        for m in dependents.get(n, ()):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    return order, set(nodes) - set(order)


def _one_cycle(left, deps):
    """从排不掉的节点里揪出一个环（只用于报告与选断点）。"""
    start = min(left)
    path, seen = [], {}
    cur = start
    while cur not in seen:
        seen[cur] = len(path)
        path.append(cur)
        nxt = [d for d in deps[cur] if d in left]
        if not nxt:
            return []
        cur = min(nxt)
    return path[seen[cur]:]


def resolve_write_plan(entries, holders, buffer_nums=None):
    """排写入顺序；闭环默认跳过，给了真实缓冲章号时用它解闭环。

    平台给每个位置判章号唯一性，而重排是**置换**不是覆盖：位置 i 要写第 i 章，
    可第 i 章此刻还装在另一个位置上（p(i) = 现在装着第 i 章的位置）。约束就是
    p(i) 必须先写过、章号腾出来。链条用拓扑排序；闭环（p 首尾相接）纯改写
    永远解不开。合成「调整中」占位会被平台按内容安全规范驳回（实测），所以
    缓冲必须是**真实正文**（本地未发布章节/用户给的场景），而且章号不能已经
    在平台目录里：先把它写到环上某位置，旧章号腾出来后按依赖换完整环，
    最后写回正确章。

    buffer_nums: 真实正文缓冲的章号列表，**一个闭环一个**（用同一个号解两个
    环会撞重名：前一个环还没把缓冲章写回去，后一个又写了一遍）。
    数量不够时，多出来的闭环照样整环跳过、报出来。

    返回 (steps, dropped, cycles):
      steps   = [{"entry": e, "placeholder": bool, "buffer_num": int|None}]，
                按执行顺序；placeholder=True 的是真实缓冲写入（位置 e，
                内容是 buffer_num 那章的正文）。
      dropped = 写了必然重名的 entry（含被跳过闭环连带卡住的，已排序）。
      cycles  = 被跳过的闭环（不含已用缓冲解开的那些），每项是位置列表。
    """
    by_i = {e["index"]: e for e in entries}
    # 目标章号的持有位置不在本轮改写范围 → 永远腾不出来，连锁到不动点。
    live = set(by_i)
    dropped = []
    changed = True
    while changed:
        changed = False
        for i in sorted(live):
            hs = holders.get(by_i[i]["want_num"]) or ()
            if any(h not in live for h in hs):
                live.discard(i)
                dropped.append(by_i[i])
                changed = True
    dropped.sort(key=lambda e: e["index"])
    if not live:
        return [], dropped, []

    deps = {i: {h for h in (holders.get(by_i[i]["want_num"]) or ())
                if h in live} for i in live}
    events = {}          # 负数事件号 -> 缓冲断点 {entry, buffer_num}
    cycles = []
    buffers = list(buffer_nums or [])

    def _key(n):
        if n < 0:
            return (1, events[n]["entry"]["index"])
        return (0, by_i[n]["index"])

    while deps:
        _order, left = _topo(set(deps), deps, _key)
        if not left:
            break
        cyc = _one_cycle(left, deps)
        if not cyc:
            raise RuntimeError(f"依赖图有环却找不到环: {sorted(left)[:10]}")
        if buffers:
            # 用真实正文解这个闭环：缓冲写入断点，断点的旧章号腾出来，
            # 依赖它的位置改等缓冲事件；断点自己的真实写入排在最后。
            bn = buffers.pop(0)
            b = min(cyc)
            ev = -(len(events) + 1)
            events[ev] = {"entry": by_i[b], "buffer_num": bn}
            for x in deps:
                if b in deps[x]:
                    deps[x].discard(b)
                    deps[x].add(ev)
            deps[b].add(ev)
            deps[ev] = set()
            continue
        cycles.append(sorted(cyc))
        # 闭环整环跳过；依赖环上章号的链也随之写不了，一起踢到不动点。
        drop = set(cyc)
        changed = True
        while changed:
            changed = False
            for i in list(deps):
                if i in drop:
                    continue
                hs = holders.get(by_i[i]["want_num"]) or ()
                if any(h in drop for h in hs):
                    drop.add(i)
                    changed = True
        for i in drop:
            deps.pop(i, None)
            live.discard(i)
            if i not in cyc:
                dropped.append(by_i[i])
        for i in deps:
            deps[i] -= drop
    order, left = _topo(set(deps), deps, _key)
    if left:
        raise RuntimeError(f"排序后仍有未处理位置: {sorted(left)[:10]}")
    dropped.sort(key=lambda e: e["index"])
    steps = []
    for n in order:
        if n < 0:
            steps.append({"entry": events[n]["entry"], "placeholder": True,
                          "buffer_num": events[n]["buffer_num"]})
        else:
            steps.append({"entry": by_i[n], "placeholder": False,
                          "buffer_num": None})
    return steps, dropped, cycles


def reviewing_in_scope(items, lo=0, hi=10 ** 9):
    """已发布章的修改正在审核中、且位置本身不符的位置。

    审核通过前旧标题/正文仍在线上，这一段无论怎么写都会撞重名——必须先停下，
    不能把它当"状态未知"跳过。
    """
    out = []
    for it in items:
        if it.get("display_status") != fu.DISPLAY_REVIEWING_PUBLISHED:
            continue
        pos = it.get("pos", it["index"])
        if not (lo <= pos <= hi):
            continue
        n = fu.chapter_title_num(it.get("title"))
        if n is None or n == pos:
            continue  # 位置本来就对，审的是正常内容，不挡重排
        out.append((pos, it.get("title") or "",
                    it.get("cant_modify_reason") or ""))
    return out




def check_applied(before, after, want_num, notes=None):
    """对账: 平台回读结果是否就是我们要的。返回 None=通过，否则返回失败原因。

    标题号与排期都必须对上，任何一项不成立都说明"提交看起来成功但实际没生效"，
    这正是漏 151 章那类静默失败——宁可当失败中止，也不能记成功往下跑。

    notes: 可选。给了就把"状态跟预期不一样"的提示塞进这个列表（由调用方
    汇总成一行打印），不给就直接打印。直接打印的话，一批 23 章改已发布段
    就会刷出 23 行一模一样的提示（2026-10-31 实测），日志被噪音埋掉。
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
    #
    # 比的是「这一章本来就该有的状态」，不是一律跟"待发布"比：已发布章改完
    # 仍然是已发布（status=1），拿"不是待发布"当异常，它每次都会被报成状态
    # 变了——一批 9 章已发布段就是 9 条假提示（2026-10-31 实测）。
    _want_status = (fu.DISPLAY_PUBLISHED if before.get("published")
                    else fu.DISPLAY_PENDING)
    if after["display_status"] != _want_status:
        _msg = (f"    （注意：状态是 {after['display_status']}"
                f"（应为 {_want_status}），多为审核滞后或排期已触发；"
                f"标题与排期已核对无误，按成功计）")
        if notes is None:
            print(_msg, flush=True)
        else:
            notes.append(_msg)

    # 已公开/曾被驳回的章都没有有效排期：它们的 timer_time 里留的是当初的
    # 定时（已公开的按 create_time 算发布时刻），拿它当判据会把「压根没动」
    # 误报成「排期被改」。
    if before.get("needs_review") or before.get("published"):
        return None
    if _ts(after.get("timer_time")) != _ts(before.get("timer")):
        return (f"排期被改动: {fmt(_ts(before.get('timer')))} → "
                f"{fmt(_ts(after.get('timer_time')))}")
    return None



def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def fmt_dur(seconds):
    """把秒数说成人话：48 秒 / 3 分 12 秒 / 1 小时 20 分。"""
    s = int(max(0, round(seconds)))
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒" if sec else f"{m} 分"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分" if m else f"{h} 小时"


def step_line(n, total, body, eta=""):
    """进度行的唯一格式（认它的地方见 STEP_PROGRESS_RE）。

    eta 传空串时**不留尾随空格**——进度行会刷几百行，尾巴上一个空格看着
    像少了半个字段。
    """
    return f"[{n}/{total}] {body}" + (f" {eta}" if eta else "")


def eta_hint(elapsed_s, done, left):
    """按已完成的步数平均估剩余时间。

    少于 2 步不给预估：两个样本的平均是骗人，宁可不说。
    """
    if done < 2 or left <= 0:
        return ""
    per = elapsed_s / done
    return f"（已用 {fmt_dur(elapsed_s)}，预计还需 {fmt_dur(per * left)}）"


async def with_heartbeat(coro, label, interval_s=_HEARTBEAT_S):
    """跑一个可能很慢的操作，途中定期报一次「还在动」。

    为什么需要（2026-10-31 用户反馈：重排中一直不知道是否再动）：
    改写一章要开编辑器、清空旧稿、灌正文、等平台保存，这一段可能一分多钟
    一个字都不吐。屏幕上一动不动，用户只能猜程序是死了还是真在动。
    间隔到了就报一次当前动作和已用时间——让"还在动"变成看得见的事实。

    心跳本身不影响语义：超时只是再等一轮，真正的结果照旧从 coro 里取。
    """
    task = asyncio.ensure_future(coro)
    t0 = time.monotonic()
    while True:
        done, _pending = await asyncio.wait({task},
                                           timeout=max(interval_s, 0.01))
        if done:
            return task.result()
        print(f"    ⏳ {label} 还在进行"
              f"（已用 {fmt_dur(time.monotonic() - t0)}）…", flush=True)


async def wait_applied(page, book_id, pos, want_num, *,
                       window_s=_APPLY_WINDOW_S, poll_s=_APPLY_POLL_S):
    """等平台把位置 pos 的标题真正改成第 want_num 章（旧内容让位）。

    已发布章的修改会先进审核，审核出结果前旧标题/正文仍在线上——这时写下一章
    同号内容，平台查重看到的还是旧内容，照样报"重复"。提交成功 ≠ 章号腾出来了，
    依赖它的下一笔必须等这里返回 applied。
    返回 (state, detail): applied / pending / rejected。
    """
    deadline = time.monotonic() + window_s
    detail = "平台未反映"
    while True:
        items = (await fu.fetch_chapter_items(page, book_id))[0]
        it = next((x for x in items if x.get("pos", x["index"]) == pos), None)
        if it is not None:
            got = fu.chapter_title_num(it.get("title"))
            st = it.get("display_status")
            detail = f"标题第{got}章/状态{st}"
            if st == fu.DISPLAY_REVIEW_FAILED:
                return "rejected", detail
            reviewing = st in (fu.DISPLAY_REVIEWING,
                               fu.DISPLAY_REVIEWING_PUBLISHED)
            if got == want_num and not reviewing:
                return "applied", detail
        if time.monotonic() >= deadline:
            return "pending", detail
        print(f"    等平台审核/生效: 位置 {pos} → 第{want_num}章"
              f"（{detail}），{poll_s}s 后重查…", flush=True)
        await page.wait_for_timeout(poll_s * 1000)


def resolve_buffers(args, num2path, holders, platform_max=0, n_cycles=0,
                    allow_auto=True):
    """解析闭环用的真实正文缓冲，返回 (buffers, err)。

    buffers = [{"num": 章号, "path": 路径}, ...]，**一个闭环一个**。
    为什么不能一个缓冲解多个闭环：缓冲章号在环转完写回正确章节之前一直占着
    那个位置，第二个闭环再写同一个号就是重名（平台直接拒）。

    三种来源：
      --buffer-file PATH   指定真实文本文件（不剧透）；只解一个闭环。
      --buffer-chapter N   指定本地某章的真实正文；只解一个闭环。
      --buffer-chapter auto / 什么都没给  自动挑：平台末尾之后的未发布章，
                           按章号从小到大取 n_cycles 个。

    两条硬约束：章号不能已在平台目录里（写进去就是重名）；正文按平台的
    非空白字符口径至少 1000 字。
    --buffer-chapter 还要求**平台末尾之后**：平台中段的标题缺号可能只是位置
    错位/缺号，正文仍挂在别的标题下，拿它当缓冲会撞重名。--buffer-file 是
    用户自己挑的一段真实场景，不受"末尾之后"这条限（内容有没有已经在平台
    上由用户自己保证）。

    allow_auto=False（--no-buffer / 无人值守 --daily）时不给参数就一个也不给：
    闭环整环跳过并报出来。无人值守不自动解，是因为缓冲会让某一章的正文在
    读者端短暂露出来——没人盯着的时候不该替作者做这个决定。
    """
    file_arg = getattr(args, "buffer_file", None)
    chap_arg = getattr(args, "buffer_chapter", None)
    want = []
    if file_arg:
        p = Path(file_arg)
        if not p.is_file():
            return [], f"缓冲文件不存在: {p}"
        want.append({"num": None, "path": str(p)})
    elif chap_arg and str(chap_arg).strip().lower() != "auto":
        try:
            num = int(chap_arg)
        except (TypeError, ValueError):
            return [], "--buffer-chapter 要填章号或 auto"
        if num not in num2path:
            return [], f"本地缺第{num}章文件"
        if num <= platform_max:
            return [], (f"第{num}章不高于平台末尾（{platform_max}）："
                        f"中段缺号很可能正文仍在平台上（换个标题挂着），"
                        f"当缓冲会撞重名。请用高于末尾的未发布章节，"
                        f"或 --buffer-file 指定一段真实场景")
        want.append({"num": num, "path": num2path[num]})
    elif n_cycles > 0:
        if not allow_auto:
            return [], None
        cand = sorted(n for n in num2path
                      if n > platform_max and n not in holders)
        if not cand:
            return [], None          # 没有可用的：闭环跳过，报告里说清楚
        for num in cand[:n_cycles]:
            want.append({"num": num, "path": num2path[num]})
    else:
        return [], None

    out = []
    for b in want:
        cnum, _title, content = fu.parse_md_file(Path(b["path"]))
        try:
            n = int(cnum)
        except (TypeError, ValueError):
            return [], "缓冲章节没有章号（需要 # 第N章 标题）"
        if n in holders:
            return [], (f"第{n}章在平台目录里已存在，不能当缓冲"
                        f"（会撞重名）；换一章或改章号")
        if b["num"] is not None and n != b["num"]:
            return [], (f"缓冲文件里的章号是第{n}章，"
                        f"与指定的第{b['num']}章对不上")
        body = fu._prepare_body(content or "")
        nchar = len(re.sub(r"\s", "", body))
        if nchar < 1000:
            return [], f"缓冲正文只有 {nchar} 字（平台要求 ≥1000）"
        out.append({"num": n, "path": b["path"]})
    return out, None


def _apply_range(plan, skipped, args):
    """按 --only / --range 收窄 plan 与 skipped，返回 (plan, skipped, 错误信息)。

    skipped 必须一起筛：它来自全书的 compute_remap，不筛就会把范围外的
    「跳过 index 4783: 审核中，等审完再改」混进本次报告里——用户限定了
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
    n_rej_rw = sum(1 for e in plan if e.get("review_failed"))
    print(f"平台 item 总数 {len(items)}  已公开 {len(pub)}"
          f"{f'（含 {n_pub_rw} 个要改写）' if n_pub_rw else '（动不了）'}  "
          f"未公开 {len(pend)}（可改）", flush=True)
    if pend and plan:
        ps = [x.get("pos", x["index"]) for x in pend]
        print(f"可改范围: 位置 {min(ps)}~{max(ps)}", flush=True)
    if plan:
        print(f"需改写 {len(plan)} 个"
              + (f"，跳过 {len(skipped)} 个" if skipped else ""), flush=True)
    else:
        # 空计划不报「需改写 0 个」——那像一张填了零的表，用户得自己判断
        # 「0 是不是好消息」（2026-10-31 用户反馈）。跳过的章照旧要说。
        print(f"没有需要改写的章{f"，跳过 {len(skipped)} 个" if skipped else ""}",
              flush=True)
    if n_pub_rw:
        print(f"⚠ 其中 {n_pub_rw} 个是已发布章：内容会被改写、重新过审，"
              f"读者可能收到更新。--run 之前请把这份计划看仔细。", flush=True)
    if n_rej_rw:
        print(f"⚠ 另有 {n_rej_rw} 个是审核不通过、平台仍允许修改的章"
              f"（多为之前被驳回的临时章、卡队列留下的）→ 本次会一并写回。",
              flush=True)
    for i, why in skipped[:10]:
        print(f"  跳过 index {i}: {why}", flush=True)
    if len(skipped) > 10:
        # 只列 10 行却不给总数，用户会以为就这 10 章（实测一次跳过 413 章）。
        print(f"  … 共 {len(skipped)} 个跳过", flush=True)
    missing = [e["want_num"] for e in plan if e["want_num"] not in num2path]
    if missing:
        print(f"⚠ 本地缺文件、无法改: {missing[:20]}"
              f"{' …' if len(missing) > 20 else ''}", flush=True)
    def _sched(e):
        """已发布/曾被驳回的章不排期，挂一列「定时 —」只是噪音。"""
        return "" if e.get("needs_review") else f"   定时 {fmt(e['timer'])}"

    for e in plan[:5]:
        print(f"  index {e['index']:>5} 现「{e['now_title'][:20]}」"
              f" → 第{e['want_num']}章{_sched(e)}", flush=True)
    if len(plan) > 5:
        print(f"  … 共 {len(plan)} 个，末个 index {plan[-1]['index']}"
              f"{_sched(plan[-1])}", flush=True)


LOG_DIR = Path(__file__).resolve().parent / "logs"   # --daily 的日志落这里




def needs_attention(rep, plan, *, now_ts=None):
    """是否需要惊动人。返回 (bool, 一句话原因)。

    两种情况人必须知道:
      · B 段非空——有已公开的错位章还在 3 天移动窗口内，过了就只能改写内容
        修整段（代价高得多），窗口内则能在 App 里直接移位；
      · 余量跌破移动窗口——再不补跑，错位章发出去时窗口都来不及用。
    """
    now_ts = time.time() if now_ts is None else now_ts
    if rep["in_window"]:
        soonest = min(r["left_h"] for r in rep["in_window"])
        return True, (f"{len(rep['in_window'])} 章已公开且顺序错位，"
                      f"最紧的还剩 {soonest:.0f} 小时可在 App 申请移动")
    # timer=0 表示该章还没设定时（不会自己发出去），没有"余量"可言，别拿 0 去减
    first = next((e for e in plan if int(e.get("timer") or 0) > 0), None)
    if first:
        left_h = (int(first["timer"]) - now_ts) / 3600
        if left_h < fu.MOVE_WINDOW_H:
            return True, (f"余量只剩 {left_h:.0f} 小时：位置 {first['index']} "
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
    print("\n" + "=" * 60, flush=True)
    print(fu.review_block_headline(blk), flush=True)
    print(fu.review_block_where(blk), flush=True)
    # 「修改内容」是图形界面里的操作模式，命令行没有这个模式——纯终端用户
    # 照着这句话找不到入口。所以这里必须留一条不依赖界面的路：去平台改。
    print("   怎么办：去番茄后台把这一章改掉重新提交；或在图形界面里用"
          "「修改内容」模式重发它。改完重新送审，后面这批就跟着过。", flush=True)
    print(fu.review_block_never_passes(blk), flush=True)
    print("=" * 60 + "\n", flush=True)


def print_audit(rep, plan, include_published=False):
    """体检报告 + 余量告警（余量=第一个还没改对的 item 距离被发出去还有多久）。"""
    _print_review_block(rep.get("review_block"))
    # 审核状态（审核中 / 被驳回）单列一段：跟 GUI 的体检报告同一份措辞。
    _rev_lines = fu.review_status_lines(rep)
    if _rev_lines:
        print("\n—— 审核状态 ——", flush=True)
        for _ln in _rev_lines:
            print(_ln, flush=True)
    print("\n—— 缺口体检 ——", flush=True)
    # 未公开段排期错位（倒挂 / 撞时刻）：位置体检看不出这个，而它正是续排
    # 出事的直接后果（2026-10-31 实测：位置全对，时刻全乱）。措辞与 GUI
    # 同一份（fanqie_upload.schedule_order_lines），两边不该各说各话。
    _sched_lines = fu.schedule_order_lines(rep)
    if _sched_lines:
        for _ln in _sched_lines:
            print(_ln, flush=True)
        print("", flush=True)
    _pend, _mov, _mis = fu.audit_position_counts(rep)
    if not (_pend or _mov or _mis):
        # 全清：不列三段零值，只给结论（与 GUI 同一份措辞）。
        # 原来这里恒定输出「A 未公开段位置不符: 0 个」，像一张填了零的表
        # ——用户得自己判断「0 是不是好消息」（2026-10-31 用户反馈）。
        print(fu.AUDIT_CLEAN_LINE, flush=True)
        return
    if _pend:
        print(f"A 未公开段位置不符: {_pend} 个 → 本工具 --run 自动改", flush=True)
    else:
        print(fu.AUDIT_SEG_CLEAN["A"], flush=True)
    first = next((e for e in plan if int(e.get("timer") or 0) > 0), None)
    if first:
        left_h = (int(first["timer"]) - time.time()) / 3600
        flag = "⚠ " if left_h < fu.MOVE_WINDOW_H else ""
        print(f"  {flag}余量: 位置 {first['index']} 还剩 {left_h / 24:.1f} 天"
              f"（{left_h:.0f} 小时）就被发布前沿追上"
              f"—— 它将于 {fmt(first['timer'])} 发出", flush=True)
        if left_h < fu.MOVE_WINDOW_H:
            print("  ⚠ 余量已跌破移动窗口：今天必须补跑，否则错位章发出后"
                  "只能去 App 申请+逐章移动", flush=True)
    if _mov:
        print(f"B 已公开、位置与章号不符、还能在 App 里申请移动: {_mov} 章",
              flush=True)
        for r in rep["in_window"]:
            print(f"  第{r['num']}章（现在位置 {r['index']}，应该在位置 "
                  f"{r['num']}）发布于 {fmt(r['pub_at'])}"
                  f"，窗口还剩 {r['left_h']:.1f} 小时", flush=True)
    else:
        print(fu.AUDIT_SEG_CLEAN["B"], flush=True)
    # C 段报的是「位置与章号对不上」的那一批（= --include-published 的改写
    # 范围），不是「排在更大章号后面」的倒挂集合：后者只要一章冲到了前面，
    # 它后面每一章都算进去（实测 640 章），可其中绝大多数位置明明是对的，
    # 报出来等于报假错（2026-10-31 实测）。
    brk = rep.get("order_break") or {}
    if _mis:
        # 只报一行：位置动不了（App 窗口已关），但内容能改写。改写计划和上面逐条
        # 列出的目标已经说清范围了，这里再展开一遍只会和 GUI 的体检报告打架。
        if include_published:
            lo = min((e["index"] for e in plan), default=0)
            hi = max((e["index"] for e in plan), default=0)
            print(f"C 已公开段位置与章号不符：{len(plan)} 章（位置 {lo}-{hi}）"
                  f"，位置不可动、内容可改写", flush=True)
        else:
            print(f"C 已公开段位置与章号不符：{len(_mis)} 章 —— "
                  + "、".join(f"第{r['num']}章(位置{r['index']})"
                              for r in _mis[:10])
                  + (f"…共{len(_mis)}章" if len(_mis) > 10 else "")
                  + "（本工具默认不改已发布段；加 --include-published 才改写）",
                  flush=True)
    else:
        print(fu.AUDIT_SEG_CLEAN["C"], flush=True)
    if brk.get("at"):
        print(f"  {fu.order_break_line(brk)}", flush=True)


async def run_remap(page, book_id, steps, num2path, *,
                    use_ai=False, max_retries=2):
    """按 steps 的顺序写入；steps 已由 resolve_write_plan 排好依赖。

    steps 里可能有 **真实缓冲** 步（placeholder=True）：把该步自带的
    buffer_path 指向的真实章节正文写到闭环断点，等它过审/生效后再换位。
    它不用平台会驳回的合成提示文本。提交成功 ≠ 平台已生效：已发布章的修改
    会先进审核（status 5），审核通过前旧标题/正文仍在线上——这类章
    （needs_review）写完必须等 wait_applied 确认真的换过来了，再写依赖它的
    下一章；待发布章没有审核这一步，改写即时生效、不用等。审核没在窗口内
    完成就**暂停**（不是失败）。

    返回 (new_ok, fail, paused)。paused = (位置, "write"/"buffer", 详情)。
    """
    new_ok, fail = [], []
    paused = None
    t0 = time.monotonic()   # 整批起点，用来报「已用 / 预计还需」
    n_done = 0              # 已完成的步数（含缓冲步），算平均耗时的样本
    total = len(steps)

    def abort(n, reason):
        fail.append((steps[n - 1]["entry"]["index"], reason))
        for s in steps[n:]:
            fail.append((s["entry"]["index"], "前方中止，未处理"))
        return new_ok, fail, paused

    def pause_tail(n):
        for s in steps[n:]:
            fail.append((s["entry"]["index"], "前方暂停，未处理"))
        return new_ok, fail, paused

    for n, s in enumerate(steps, 1):
        e = s["entry"]
        want = e["want_num"]
        url = EDIT_URL_TPL.format(book_id=book_id, item_id=e["item_id"])
        if s["placeholder"]:
            # 每个闭环用自己那份缓冲（章号不能重复，否则撞重名）。
            bpath = s.get("buffer_path")
            if not bpath:
                print("    ✗ 闭环缓冲缺少真实文本文件 —— 中止整批", flush=True)
                return abort(n, "缺少缓冲文件")
            bnum, btitle, bcontent = fu.parse_md_file(Path(bpath))
            try:
                bnum_int = int(bnum) if bnum is not None else None
            except (TypeError, ValueError):
                bnum_int = None
            if bnum_int is None:
                print("    ✗ 缓冲章节号无效 —— 中止整批", flush=True)
                return abort(n, "缓冲章节号无效")
            if s.get("buffer_num") is not None and bnum_int != s["buffer_num"]:
                print(f"    ✗ 缓冲章号对不上（要第{s['buffer_num']}章，"
                      f"文件里是第{bnum_int}章）—— 中止整批", flush=True)
                return abort(n, "缓冲章号对不上")
            print(step_line(
                n, total,
                f"index {e['index']}: 借用真实章节"
                f"第{bnum_int}章作缓冲（{btitle[:18]}，腾出第{want}章）",
                eta_hint(time.monotonic() - t0, n_done, total - n + 1)),
                flush=True)
            try:
                done, err = await with_heartbeat(
                    fu.edit_one_chapter(
                        page, url, bnum_int, btitle, bcontent,
                        use_ai=False, max_retries=max_retries,
                        set_num=str(bnum_int)),
                    f"位置 {e['index']} 写缓冲正文（第{bnum_int}章）")
            except fu.DailyLimitReached as ex:
                print(f"    撞字数上限（{ex}），中止整批", flush=True)
                return abort(n, f"字数上限:{ex}")
            if not done:
                print(f"    ✗ 缓冲写入失败: {err} —— 中止整批", flush=True)
                return abort(n, err or "缓冲写入失败")
            state, detail = await wait_applied(
                page, book_id, e["index"], bnum_int)
            if state == "rejected":
                print(f"    ✗ 缓冲审核不通过: {detail} —— 中止整批",
                      flush=True)
                return abort(n, f"缓冲审核不通过:{detail}")
            if state == "pending":
                paused = (e["index"], "buffer", detail)
                print(f"    ⏸ 缓冲已提交，平台还在审核（{detail}）"
                      f"—— 本次暂停", flush=True)
                return pause_tail(n)
            n_done += 1
            continue

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
        print(step_line(
            n, total,
            f"index {e['index']}: 「{e['now_title'][:18]}」"
            f" → 第{want}章 {title[:18]}",
            eta_hint(time.monotonic() - t0, n_done, total - n + 1)),
            flush=True)
        try:
            done, err = await with_heartbeat(
                fu.edit_one_chapter(
                    page, url, want, title, content,
                    use_ai=use_ai, max_retries=max_retries, set_num=str(want)),
                f"位置 {e['index']} 改写为第{want}章")
        except fu.DailyLimitReached as ex:
            print(f"    撞字数上限（{ex}），中止整批", flush=True)
            return abort(n, f"字数上限:{ex}")
        if not done:
            print(f"    ✗ 改写失败: {err} —— 中止整批", flush=True)
            return abort(n, err or "未知失败")
        new_ok.append(e["index"])

        if e.get("needs_review"):
            state, detail = await wait_applied(
                page, book_id, e["index"], want)
            if state == "rejected":
                print(f"    ✗ 改写审核不通过: {detail} —— 中止整批",
                      flush=True)
                new_ok.pop()
                return abort(n, f"改写审核不通过:{detail}")
            if state == "pending":
                paused = (e["index"], "write", detail)
                print(f"    ⏸ 已提交，平台还在审核（{detail}）—— 本次暂停",
                      flush=True)
                return pause_tail(n)
        n_done += 1
    return new_ok, fail, paused


async def verify_batch(page, book_id, entries, done_positions, *,
                       window_s=_VERIFY_WINDOW_S, poll_s=_VERIFY_POLL_S):
    """批末复核: 重抓平台真实状态核实本批目标，没反映的按窗口轮询等审核。

    返回 (still_pending, fresh_items, notes)。still_pending 不算失败——下一轮会从
    平台真实状态重算，真没生效的会被再改一次。notes 是"状态跟预期不一样"的
    提示，调用方汇总成一行打印（逐章打印的话，一批 23 章就是 23 行噪音）。
    """
    byidx = {e["index"]: e for e in entries}
    pending = list(done_positions)
    deadline = time.monotonic() + window_s
    fresh = []
    notes = []
    while True:
        fresh = (await fu.fetch_chapter_items(page, book_id))[0]
        bypos = {x.get("pos", x["index"]): x for x in fresh}
        still = []
        for i in pending:
            after = bypos.get(i)
            if after is None or check_applied(byidx.get(i, {}), after, i, notes):
                still.append(i)
        pending = still
        if not pending or time.monotonic() >= deadline:
            return pending, fresh, notes
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

    # 不用 async with：部分机器上 Playwright 的 driver stop 会无限等，
    # 把已经打印好的结果扣住（GUI/CLI 都在等这个进程退出）。手动 start，
    # 收尾时给 stop 一个硬上限；任何 return 都会走到下面的 finally。
    p = await async_playwright().start()
    browser = None
    try:
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
            # 「跳过 index 4783: 审核中，等审完再改」都是全书口径 —— 用户限定了
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

            # 已发布章的修改会先进审核（status 5）：审核通过前旧内容仍在线上，
            # 这一段无论怎么写都会撞重名。明确停在这里，别把它当"状态未知"绕过。
            _lo, _hi = 0, 10 ** 9
            if args.only is not None:
                _lo = _hi = args.only
            elif args.range:
                _m = re.fullmatch(r"(\d+)-(\d+)", args.range.replace(" ", ""))
                if _m:
                    _lo, _hi = int(_m.group(1)), int(_m.group(2))
            busy = reviewing_in_scope(items, _lo, _hi)
            if busy:
                print("\n⚠ 有已发布章的修改正在审核中（审核通过前旧内容仍在线上）:",
                      flush=True)
                for _pos, _title, _why in busy[:10]:
                    print(f"  位置 {_pos}「{_title[:24]}」"
                          f"{('：' + _why) if _why else ''}", flush=True)
                print("  等审核结束后重跑本工具；本次不会写入。", flush=True)
                return True

            # 范围已经在 _apply_range 里作用于 plan，这里直接用。
            entries = plan
            holders = build_chapter_holders(items)
            _by_pos = {it.get("pos", it["index"]): it for it in items}
            platform_max = max((it.get("pos", it["index"]) for it in items),
                               default=0)
            # 先不带缓冲排一次，看看有几个闭环 —— 才知道要配几个缓冲。
            # 闭环是死锁：位置互相占着章号（位置 670 装第673章、位置 673 装
            # 第676章……最后一个装第670章），写谁都要撞重名。要解开必须先往
            # 环上某个位置写一份**章号还没被平台占用**的真实正文，腾出章号，
            # 环转完再把正确章节写回原位。
            _pre, _pre_drop, cycles_pre = resolve_write_plan(entries, holders)
            # 无人值守（--daily）不自动解闭环：缓冲会让某一章的正文在读者端
            # 短暂露出来，没人盯着的时候不该替作者做这个决定。
            allow_auto = not (getattr(args, "no_buffer", False)
                              or getattr(args, "daily", False))
            buffers, buffer_err = resolve_buffers(
                args, num2path, holders, platform_max,
                n_cycles=len(cycles_pre), allow_auto=allow_auto)
            if buffer_err:
                print(f"\n⚠ 缓冲不可用: {buffer_err}", flush=True)
                print("  本次不会写入。", flush=True)
                return True
            buffer_by_num = {b["num"]: b["path"] for b in buffers}
            steps, dropped, cycles = resolve_write_plan(
                entries, holders,
                buffer_nums=[b["num"] for b in buffers])
            for _s in steps:
                if _s["placeholder"]:
                    _s["buffer_path"] = buffer_by_num.get(_s["buffer_num"])
            if dropped:
                print(f"\n踢掉 {len(dropped)} 个会造成永久重名的位置"
                      f"（它们要的章号还占在本轮改不到的地方）:", flush=True)
                for e in dropped[:10]:
                    hs = holders.get(e["want_num"]) or []
                    print(f"  位置 {e['index']}（现装「{e['now_title'][:18]}」）："
                          f"第{e['want_num']}章 还占在位置 "
                          f"{'、'.join(map(str, hs))}，把它一起纳入 --range "
                          f"才能换", flush=True)
                if len(dropped) > 10:
                    print(f"  … 共 {len(dropped)} 个", flush=True)
            n_cycle = sum(len(c) for c in cycles)
            ph_steps = [s for s in steps if s["placeholder"]]
            n_buffer = len(ph_steps)
            if ph_steps:
                print(f"\n用真实正文缓冲解 {n_buffer} 个闭环:", flush=True)
                for _s in ph_steps:
                    _bn = _s["buffer_num"]
                    _bt = ""
                    try:
                        _bn2, _bt, _ = fu.parse_md_file(Path(_s["buffer_path"]))
                    except Exception:
                        pass
                    print(f"  位置 {_s['entry']['index']} ← 本地第{_bn}章"
                          f"「{_bt[:24]}」", flush=True)
                print("  做法：先把这份正文写到那个位置腾出章号，环转完再把"
                      "正确章节写回原位；缓冲章仍留在本地。", flush=True)
                print("  ⚠ 换位期间（等平台过审，可能几分钟到几小时）"
                      "那个位置的读者会短暂看到这份正文。", flush=True)
            if cycles:
                print(f"\n跳过 {len(cycles)} 个闭环（共 {n_cycle} 章）:",
                      flush=True)
                for c in cycles:
                    _now = "、".join(
                        f"位置{p}装第"
                        f"{fu.chapter_title_num(_by_pos.get(p, {}).get('title') or '') or '?'}章"
                        for p in c[:4])
                    print(f"  {len(c)} 章：位置 {min(c)}-{max(c)} 之间：{_now}"
                          f"{' …' if len(c) > 4 else ''}", flush=True)
                print("  这些位置互相占着章号，读者目录里是一圈（写到第670章"
                      "之后又接回第670章），纯改写是死锁：先写谁都会撞重名。",
                      flush=True)
                if allow_auto and not buffers:
                    print("  本地没有高于平台末尾的未发布章节可作缓冲，这批"
                          "只能先放着；把后面几章补进本地、或用 --buffer-file "
                          "指定一段真实场景（不剧透），再跑一次。", flush=True)
                elif not allow_auto:
                    print("  已按 --no-buffer / 无人值守跳过。去掉这个开关"
                          "（或用 --buffer-chapter auto）就会自动解。",
                          flush=True)
                else:
                    print("  还剩的闭环可以用 --buffer-file 指定一段真实场景"
                          "再解一个。", flush=True)
            # --limit 只能截在"缓冲已经换回"的位置：截在换回之前，那一章会永远
            # 停在借来的真实正文上（读者看到的是别人的稿子），这种截断直接拒绝。
            if args.limit and len(steps) > args.limit:
                _prefix = steps[:args.limit]
                _left = ({s["entry"]["index"] for s in _prefix
                          if s["placeholder"]}
                         - {s["entry"]["index"] for s in _prefix
                            if not s["placeholder"]})
                if _left:
                    print("⚠ --limit 会截在缓冲换回之前，闭环会留下真实缓冲。"
                          "请去掉 --limit 或调大它；本次不会写入。", flush=True)
                    return True
                steps = _prefix
            n_real = sum(1 for s in steps if not s["placeholder"])

            if not args.run:
                print(f"\n[dry-run] 本次将改写 {n_real} 个 item"
                      + (f"（另用 {n_buffer} 个真实缓冲" if n_buffer else "")
                      + (f"；{n_cycle} 章闭环没动（见上）" if n_cycle else "")
                      + ("）" if (n_buffer or n_cycle) else "")
                      + "。确认无误后加 --run 才会真正写入。", flush=True)
                return attn
            if not steps:
                if cycles:
                    print(f"只剩 {n_cycle} 章闭环，本次没动它们"
                          f"（{('本地没有可用的缓冲章节' if allow_auto and not buffers else '按 --no-buffer / 无人值守跳过')}）。"
                          f"补几章到本地、或用 --buffer-file 指定真实场景再跑。",
                          flush=True)
                    return True
                # 比如剩下的都在"临近发布"的冷却里被跳过（--safety-minutes，
                # 默认 60 分钟）——告警仍要带出去
                print("没有需要改写的 item。", flush=True)
                return attn
            print(f"\n开始改写 {n_real} 个 item"
                  + (f"（含 {n_buffer} 个真实缓冲）" if n_buffer else "")
                  + " —— 3 秒后开始…", flush=True)
            await page.wait_for_timeout(3000)
            t_run = time.monotonic()
            ok, fail, paused = await run_remap(
                page, book_id, steps, num2path, use_ai=args.use_ai)
            _write_s = time.monotonic() - t_run
            _tail = ("前方中止，未处理", "前方暂停，未处理")
            real_fail = [(i, w) for i, w in fail if w not in _tail]
            unprocessed = len(fail) - len(real_fail)
            # 「没动」= 本来要改、但这次一个字节都没写的位置（闭环跳过 + 被踢掉
            # 的重名位置）。它不体现在 fail 里（那些位置压根没排进写入步骤），
            # 所以汇总里必须单列：只写「成功 23 失败 0」会让人以为全改完了，
            # 而读者目录里那 9 章其实还是错的（2026-10-31 用户反馈）。
            n_untouched = n_cycle + len(dropped)
            print("=" * 50, flush=True)
            print(f"完成  成功 {len(ok)}  失败 {len(real_fail)}"
                  + (f"  未处理 {unprocessed}" if unprocessed else "")
                  + (f"  没动 {n_untouched}" if n_untouched else "")
                  + f"  （写入用时 {fmt_dur(_write_s)}）",
                  flush=True)
            _ok_set = set(ok)
            left_buf = [s["entry"]["index"] for s in ph_steps
                        if s["entry"]["index"] not in _ok_set]
            if n_buffer:
                print(f"  · 真实缓冲 {n_buffer} 个"
                      + (f"，仍有 {len(left_buf)} 个未换回" if left_buf
                         else "，均已换回"), flush=True)
            if n_cycle:
                # 说清楚"没动"是什么：哪几个位置、现在各装什么、读者会看到什么。
                print(f"  · 没动的 {n_cycle} 章是闭环：这些位置互相占着章号，"
                      f"纯改写解不开（写谁都会撞重名）", flush=True)
                for c in cycles:
                    _now = "、".join(
                        f"位置{p}装第"
                        f"{fu.chapter_title_num(_by_pos.get(p, {}).get('title') or '') or '?'}章"
                        for p in c[:4])
                    print(f"    {len(c)} 章：位置 {min(c)}-{max(c)} 之间：{_now}"
                          f"{' …' if len(c) > 4 else ''}", flush=True)
                print("    读者目录里这一段还是转圈的（…第669章 → 第673章 → "
                      "… → 第670章 → 第671章…）", flush=True)
                print("    下次跑会自动用真实正文缓冲解开（除非加 --no-buffer）",
                      flush=True)
            if dropped:
                print(f"  · 另有 {len(dropped)} 个位置被踢掉：它们要的章号"
                      f"还占在本轮改不到的地方，写进去就是永久重名", flush=True)
            if paused:
                _kind = "真实缓冲" if paused[1] == "buffer" else "改写"
                print(f"⏸ 位置 {paused[0]} 的{_kind}已提交，平台审核中"
                      f"（{paused[2]}）—— 本次暂停。", flush=True)
                print("   等审核结束后重跑本工具，会从平台真实状态继续。",
                      flush=True)
            for i, why in real_fail[:30]:
                print(f"  index {i}: {why}", flush=True)
            if unprocessed:
                print(f"  … 其余 {unprocessed} 个位置未处理"
                      f"（前方中止/暂停）", flush=True)
            if left_buf:
                # 形状要和 GUI 的解析对上（它按「位置 N 临时借用了真实章节」取号），
                # 这里写成列表 repr 的话，GUI 弹窗就只剩半句没有位置。
                print(f"⚠ 位置 {'、'.join(map(str, left_buf))} 临时借用了真实章节"
                      "（尚未换回）。重跑本工具会写回正确内容。", flush=True)
            if paused:
                # 已提交的章在审核里，批末复核等不出结果；直接按暂停收尾
                print(f"⚠ 需要人工处理: 位置 {paused[0]} 审核中，"
                      f"等审完重跑本工具继续", flush=True)
                return True
            # 批末复核：重抓平台真实状态核实本批目标；没反映的按窗口轮询等审核。
            # 复核完再用这份新状态算一遍还剩多少位置与章号不符。
            not_yet, fresh, _notes = await verify_batch(page, book_id, entries, ok)
            print(f"批末复核: 本批提交 {len(ok)} 章，平台已反映 "
                  f"{len(ok) - len(not_yet)} 章", flush=True)
            if _notes:
                # 同一句话刷了 N 遍没意义，合并成一行说清"有几章这样"。
                print(f"  · {len(_notes)} 章的平台状态跟预期不一样"
                      f"（多为审核滞后或排期已触发）；标题与排期已核对无误，"
                      f"按成功计", flush=True)
            if not_yet:
                print(f"  · 等满 {_VERIFY_WINDOW_S // 60} 分钟仍未反映 "
                      f"{len(not_yet)} 章: {not_yet[:10]}"
                      f"{' …' if len(not_yet) > 10 else ''}", flush=True)
                print("    不算失败——下一轮会按平台真实状态重算，"
                      "真没生效的会被再改一次", flush=True)
            left, _ = compute_remap(fresh, safety_minutes=args.safety_minutes)
            print(f"复核: 未公开段还有 {len(left)} 个 item 位置与章号不符", flush=True)
            if args.include_published:
                # 刚才改的是已公开段，那一段也得有回执——只报"未公开段 0 个"
                # 的话，用户根本不知道刚修的那 9 章到底对上了没有。
                _rep2 = fu.audit_chapter_positions(fresh)
                _still = sum(1 for r in (_rep2.get("misplaced_pub") or [])
                             if r.get("published"))
                if _still:
                    print(f"复核: 已公开段还有 {_still} 章位置与章号不符"
                          f"（本次没动的那几章）", flush=True)
                else:
                    print("复核: 已公开段的顺序已全部对上", flush=True)
            if ok:
                # 读者目录现在是什么样 —— 这是作者最关心的一句话，
                # 光看"成功 9"不知道顺序到底对没对。
                _lo_f, _hi_f = min(ok), max(ok)
                _view = fu.reader_view_around(fresh, _lo_f, span=2)
                print(f"读者目录现在看到（位置 {_lo_f}~{_hi_f} 那一段）：… "
                      + " → ".join(_view) + " …", flush=True)
            attn, why = needs_attention(fu.audit_chapter_positions(fresh), left)
            # 异常中止（≠字数上限）也要惊动人：撞上限是每天的常态，
            # 但改写失败/对账不过若不报，会每天静默卡在同一处
            abnormal = next((w for _i, w in real_fail
                             if "字数上限" not in w), None)
            # 撞每月上限 ≠ 撞每日上限: 后者明天照跑，前者意味着本月剩下的日子
            # 一章都改不动，而发布前沿照走。必须当场算余量够不够撑到下月。
            hit_monthly = any(fu.is_monthly_limit(w) for _i, w in real_fail)
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
            if left_buf and not attn:
                attn, why = True, (
                    f"位置 {'、'.join(map(str, left_buf))} 临时借用了真实章节，"
                    f"重跑本工具写回正确内容")
            if cycles and not attn:
                attn, why = True, (
                    f"{n_cycle} 章闭环已跳过：可用 --buffer-chapter auto / "
                    f"--buffer-file 用真实正文解，或人工占位后再跑")
            if attn:
                print(f"⚠ 需要人工处理: {why}", flush=True)
            return attn
        finally:
            await fu.close_browser_safely(
                browser, timeout_s=10 if not args.run else 30)
    finally:
        try:
            await asyncio.wait_for(p.stop(), timeout=10)
        except (asyncio.TimeoutError, TimeoutError):
            print("Playwright 收尾超过 10 秒，放弃等待（不影响结果）",
                  flush=True)
        except Exception as e:
            print(f"Playwright 收尾失败（不影响结果）: {e}", flush=True)


def demo():
    """零依赖自检: 已公开不碰 / 临近发布不碰 / 依赖顺序 / 闭环跳过 / 永久重名。"""
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
    assert a and "40 小时" in why, why                       # 余量跌破窗口必须报
    a, why = needs_attention(none_hit, [{"index": 9, "timer": now + 100 * H}],
                             now_ts=now)
    assert not a and why == "", (a, why)                    # 余量充裕不吵
    assert not needs_attention({"in_window": [], "expired": [], "pending_bad": []},
                               [], now_ts=now)[0]

    rep = fu.audit_chapter_positions(aud, now_ts=now, window_h=72)
    assert rep["pending_bad"] == [6], rep
    assert [r["num"] for r in rep["in_window"]] == [2], rep
    assert [r["num"] for r in rep["expired"]] == [3], rep
    assert 70 < rep["in_window"][0]["left_h"] < 72, rep      # 剩 71 小时
    # --- 铁律: 回读不到 / 平台未反映绝不能中止批次 ---
    # 平台生效是滞后的（改动要过审才反映到列表，标题号同理），把"读不到"
    # 当失败会白白丢掉整批。2026-08-20 实测就因此丢了 547 章的机会。
    # 现在提交循环里只有平台明确驳回（wait_applied 返回 rejected）才中止，
    # "未反映"（pending）一律暂停，等审完重跑。
    import inspect as _insp, re as _re
    _src = _insp.getsource(run_remap)
    assert "read_back" not in _src, "run_remap 不该再逐章回读"
    assert "check_applied" not in _src, "回读判据不该出现在提交循环里"
    assert "verify_batch" in _insp.getsource(main_async), "批末必须复核"
    _reasons = _re.findall(r"return abort\(n, ([^)]+)\)", _src)
    assert _reasons, "找不到 abort 调用"
    for _r in _reasons:
        assert ("字数上限" in _r or "本地" in _r or "err" in _r
                or "未知失败" in _r or "审核" in _r or "缓冲" in _r), (
            f"回读类原因不该中止批次: {_r}")

    # --- 已公开段: --include-published 才纳入，冷却改看发布时间 ---
    pub_shift = [
        # 已公开且装第1章 → 不动
        {"index": 1, "title": "第1章 甲", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 10 * H, "timer_time": 0, "item_id": "a",
         "cant_modify_reason": ""},
        # 已公开但整段位移（装第3章却占着位置2）→ 默认不碰，开关打开才进计划
        {"index": 2, "title": "第3章 乙", "display_status": fu.DISPLAY_PUBLISHED,
         "create_time": now - 10 * H, "timer_time": 0, "item_id": "b",
         "cant_modify_reason": ""},
        # 已公开、发布不到冷却时间 → 让给下一轮
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
    # 已公开/曾被驳回的章没有排期可比：check_applied 不能拿 timer_time 判它
    assert check_applied({"published": True, "timer": 0},
                         {"title": "第2章 乙",
                          "display_status": fu.DISPLAY_PUBLISHED,
                          "timer_time": "1699999999"}, 2) is None
    assert "没改成第2章" in check_applied(
        {"published": True, "timer": 0},
        {"title": "第3章 乙", "display_status": fu.DISPLAY_PUBLISHED}, 2)

    # --- 写入顺序: 依赖先行；闭环跳过，不生成占位正文 ---
    # 环形互换: 位置1 装第2章、位置2 装第1章。纯改写无解——先写谁，它要的
    # 章号都还在对方手里。工具不再写合成占位（平台会按内容安全驳回），
    # 整环跳过并报出来。
    ring_items = [{"index": 1, "title": "第2章 甲"}, {"index": 2, "title": "第1章 乙"}]
    _h = build_chapter_holders(ring_items)
    _ring = [{"index": 1, "now_title": "第2章 甲", "want_num": 1},
             {"index": 2, "now_title": "第1章 乙", "want_num": 2}]
    _st, _drop, _cyc = resolve_write_plan(_ring, _h)
    assert _st == [], _st
    assert _drop == [] and [sorted(c) for c in _cyc] == [[1, 2]], (_drop, _cyc)

    # 目标章号占在本轮改不到的位置 → 写进去就是永久重名，踢掉。
    # 位置1 想要的第1章还占在位置0，而位置0 不在 entries 里。位置1 自己装的是
    # 第9章，本轮没人要第9章 —— 所以它被踢不会连累别人（连锁见下一段用例）。
    orphan_items = [{"index": 0, "title": "第1章 甲"},
                    {"index": 1, "title": "第9章 乙"},
                    {"index": 5, "title": "第7章 丙"}]
    _ho = build_chapter_holders(orphan_items)
    _orphan = [{"index": 1, "now_title": "第9章 甲", "want_num": 1},
               {"index": 5, "now_title": "第7章 乙", "want_num": 5}]
    _st2, _d2, _c2 = resolve_write_plan(_orphan, _ho)
    assert [s["entry"]["index"] for s in _st2] == [5], _st2
    assert [e["index"] for e in _d2] == [1] and _c2 == [], (_d2, _c2)

    # 连锁踢：位置1 被踢后它装着的第2章腾不出来，占着第2章的位置2 也跟着写不了。
    # 只踢一次是不够的，必须迭代到不动点。
    cas_items = [{"index": 0, "title": "第1章 甲"}, {"index": 1, "title": "第2章 乙"}]
    _hc = build_chapter_holders(cas_items)
    _cas = [{"index": 1, "now_title": "第2章 甲", "want_num": 1},
            {"index": 2, "now_title": "第9章 乙", "want_num": 2}]
    _st3, _d3, _c3 = resolve_write_plan(_cas, _hc)
    assert [s["entry"]["index"] for s in _st3] == [] and _c3 == [], _st3
    assert [e["index"] for e in _d3] == [1, 2], _d3

    # 无环的置换: 位置 10/20/30 要的章号被顶到 40/41/42。
    real_items = [{"index": 10, "title": "第11章 甲"},
                  {"index": 20, "title": "第21章 乙"},
                  {"index": 30, "title": "第31章 丙"},
                  {"index": 40, "title": "第10章 丁"},
                  {"index": 41, "title": "第20章 戊"},
                  {"index": 42, "title": "第30章 己"}]
    _hr = build_chapter_holders(real_items)
    _all = [{"index": i, "now_title": t, "want_num": i} for i, t in
            ((it["index"], it["title"]) for it in real_items)]
    _st5, _d5, _c5 = resolve_write_plan(_all, _hr)
    assert _d5 == [] and _c5 == [], (_d5, _c5)
    _pos5 = {s["entry"]["index"]: k for k, s in enumerate(_st5)}
    assert sorted(_pos5) == [10, 20, 30, 40, 41, 42], _st5
    assert (_pos5[40] < _pos5[10] and _pos5[41] < _pos5[20]
            and _pos5[42] < _pos5[30]), _st5
    # 只纳入 10/20/30（40-42 在范围外）→ 三个全被踢，写进去必永久重名
    _st4, _d4, _c4 = resolve_write_plan(_all[:3], _hr)
    assert _st4 == [] and [e["index"] for e in _d4] == [10, 20, 30], (_st4, _d4)

    # 线上实测结构: 215 个位置是一个 206 环 + 一个 9 环。旧实现把 480 排到最后，
    # 首笔去写 481（它要的第481章还在 480）→ 平台报重复标题和段落。
    live_items = []
    for pos in range(480, 695):
        n = (480 if pos == 692 else 524 if pos == 693 else 670 if pos == 694
             else pos + 1 if pos <= 522 else pos + 2 if pos <= 667 else pos + 3)
        live_items.append({"index": pos, "pos": pos, "title": f"第{n}章 x"})
    _hl = build_chapter_holders(live_items)
    _entries = [{"index": p, "want_num": p, "now_title": "x"}
                for p in range(480, 695)]
    _st6, _d6, _c6 = resolve_write_plan(_entries, _hl)
    assert _d6 == [], _d6
    assert sorted(len(c) for c in _c6) == [9, 206], _c6
    assert _st6 == [], "闭环不该生成任何写入步骤"
    assert all(not s["placeholder"] for s in _st6)

    # 当前线上：480 挂着一章被驳回的临时章（标题 6676，第481章因此已腾出来）
    # → 206 环断成链，只剩 9 环；工具应修 206 链（480 在队尾写回），跳过 9 环。
    broken_items = []
    for pos in range(480, 695):
        n = (6676 if pos == 480 else
             480 if pos == 692 else 524 if pos == 693 else 670 if pos == 694
             else pos + 1 if pos <= 522 else pos + 2 if pos <= 667 else pos + 3)
        broken_items.append({"index": pos, "pos": pos, "title": f"第{n}章 x"})
    _hb = build_chapter_holders(broken_items)
    _st7, _d7, _c7 = resolve_write_plan(_entries, _hb)
    assert _d7 == [], _d7
    assert sorted(len(c) for c in _c7) == [9], _c7
    _idx7 = [s["entry"]["index"] for s in _st7]
    assert len(_idx7) == 206 and _idx7[0] == 481, _idx7[:3]
    assert 480 in _idx7 and not any(i in _idx7 for i in _c7[0]), _idx7[-3:]
    _done = set()
    for s in _st7:
        e = s["entry"]
        assert all(h in _done for h in _hb.get(e["want_num"], [])), (
            e["index"], _hb[e["want_num"]])
        _done.add(e["index"])
    assert len(_done) == 206, len(_done)

    # 重试分类: 内容/限流拒绝不该白重试；字段填充类仍留给自愈
    assert not fu.is_retryable_publish_error("本书中存在重复标题和段落，请修改后再发布")
    assert not fu.is_retryable_publish_error("提交频繁，请稍后再试")
    assert fu.is_retryable_publish_error("正文至少输入1000字")
    assert fu.is_retryable_publish_error("net::ERR_TIMED_OUT")
    assert fu.is_retryable_publish_error("服务器开小差了")

    print("demo ok")


def main():
    ap = argparse.ArgumentParser(
        description="章节重排（默认只动未公开的章；--include-published 连已发布段一起改）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--only", type=int, help=fu._H_ONLY)
    g.add_argument("--range", type=str, help=fu._H_RANGE)
    ap.add_argument("--limit", type=int, help=fu._H_LIMIT_RW)
    ap.add_argument("--include-published", action="store_true",
                    help=fu._H_INCLUDE_PUBLISHED)
    bg = ap.add_mutually_exclusive_group()
    bg.add_argument("--buffer-chapter", metavar="N|auto",
                    help=fu._H_BUFFER_CHAPTER)
    bg.add_argument("--buffer-file", metavar="PATH", help=fu._H_BUFFER_FILE)
    ap.add_argument("--no-buffer", action="store_true", help=fu._H_NO_BUFFER)
    ap.add_argument("--run", action="store_true", help=fu._H_RUN_WRITE)
    ap.add_argument("--safety-minutes", type=int, default=SAFETY_MINUTES,
                    help=fu._H_SAFETY_MINUTES)
    fu.add_common_tool_args(ap)
    ap.add_argument("--use-ai", action="store_true", help=fu._H_USE_AI)
    ap.add_argument("--show-browser", action="store_true", help=fu._H_SHOW_BROWSER)
    ap.add_argument("--daily", action="store_true", help=fu._H_DAILY)
    ap.add_argument("--audit", action="store_true",
                    help="只做缺口体检（A/B/C 三段 + 余量告警），不改任何东西")
    ap.add_argument("--self-check", action="store_true", help=fu._H_SELF_CHECK)
    args = ap.parse_args()
    if args.self_check:
        demo()
        return
    fu.run_unattended(main_async, args, log_dir=LOG_DIR, name="重排",
                      hint="（余量每天少 6 章）")


if __name__ == "__main__":
    main()
