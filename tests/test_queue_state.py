# -*- coding: utf-8 -*-
"""发布队列状态：队尾、末章、排期倒挂、撞时刻。

2026-10-31 用户报「队列排到第5191章，平台实际有5196章」。查下来是两层问题
叠在一起，这里把两条都锁住：

① **队尾漏算**：队尾过去只统计 display_status=10（待发布）。审核中(4)、
   审核不通过(7)的章**同样占着未来的发布坑位**，漏掉它们，新章就会被排进
   已被占用的时刻 —— 2026-10-10 实测：新章排在 2028-06-30 07:00~20:00，
   正好撞上第4783~4787章已占的坑位，且排在第5191章（2028-09-06）**前面**。
② **口径混用**：队尾（最新被占用的时刻）和末章（位置最后那章）是两个数。
   健康的书上重合；排期错乱的书上不重合。过去只报「最大时刻」那一个，
   于是标签写着第5191章、末章却是第5196章 —— 正是用户看到的那一幕。

真平台数据（2026-10-31 只读实测，book 7613749318914149401）：
    位置 5191  第5191章  timer=2028-09-06 07:00  待发布
    位置 5192~5196        timer=2028-06-30 …    待发布（早于 5191 → 倒挂）
    位置 4783~4787        timer=2028-06-30 …    待发布（与 5192~5196 撞时刻）

运行: python tests/test_queue_state.py
"""
import sys
from datetime import datetime
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


def ts(text):
    """本地时区的 '2028-06-30 07:00' -> unix 秒（平台按本机时区渲染）。"""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp())


def item(pos, title, timer=None, ds=fu.DISPLAY_PENDING, create=None):
    return {"index": pos, "pos": pos, "title": title,
            "display_status": ds,
            "timer_time": str(ts(timer)) if timer else "0",
            "create_time": str(ts(create)) if create else "0",
            "cant_modify_reason": ""}


def main():
    print("== ① 队尾要算所有未公开的章 ==")
    # 只统计待发布 → 队尾是 2028-09-06；审核中的章排在更后面却不算数 →
    # 新章被排进 2028-09-06 之后、而那章占着 2028-10-01（实测事故的形状）
    items = [
        item(1, "第1章 甲", "2028-09-06 07:00"),
        item(2, "第2章 乙", "2028-10-01 07:00", ds=fu.DISPLAY_REVIEWING),
    ]
    check("审核中的章也占坑位（队尾取最晚时刻）",
          fu.queue_tail_dt(items) == datetime.fromtimestamp(ts("2028-10-01 07:00")),
          str(fu.queue_tail_dt(items)))
    items2 = [
        item(1, "第1章 甲", "2028-09-06 07:00"),
        item(2, "第2章 乙", "2028-11-01 07:00", ds=fu.DISPLAY_REVIEW_FAILED),
    ]
    check("审核不通过的章也占坑位",
          fu.queue_tail_dt(items2) == datetime.fromtimestamp(ts("2028-11-01 07:00")),
          str(fu.queue_tail_dt(items2)))
    items3 = [
        item(1, "第1章 甲", "2028-09-06 07:00"),
        item(2, "第2章 乙", "2026-01-01 07:00", ds=fu.DISPLAY_PUBLISHED),
    ]
    check("已发布的章不占坑位",
          fu.queue_tail_dt(items3) == datetime.fromtimestamp(ts("2028-09-06 07:00")),
          str(fu.queue_tail_dt(items3)))
    check("没有未公开章 -> 队尾为 None", fu.queue_tail_dt([]) is None)
    check("认不出的状态码按「还占着坑位」处理（保守）",
          fu.is_unpublished(None) is True)
    check("已发布章的修改审核中（5）不占坑位",
          fu.is_unpublished(fu.DISPLAY_REVIEWING_PUBLISHED) is False)

    print()
    print("== ② 队尾时刻 与 末章 是两个数 ==")
    # 真实形状：末章第5196章排在 2028-06-30，而第5191章排在 2028-09-06
    items = [
        item(5191, "第5191章 钻头进去两厘米就空了", "2028-09-06 07:00"),
        item(5192, "第5192章 这一节不记在本子上", "2028-06-30 07:00"),
        item(5193, "第5193章 环节没有手", "2028-06-30 07:01"),
    ]
    st = fu.queue_state(items)
    check("队尾 = 最晚被占用的时刻",
          st["tail_dt"] == datetime.fromtimestamp(ts("2028-09-06 07:00")),
          str(st["tail_dt"]))
    check("末章 = 位置最后那章（第5193章）",
          st["last"] and st["last"]["pos"] == 5193, str(st["last"]))
    sm = fu.queue_summary(st)
    check("摘要里队尾时刻与末章都在",
          (sm["date"], sm["time"]) == ("2028-09-06", "07:00")
          and sm["chapter"].startswith("第5193章"), str(sm))
    check("摘要里末章自己的时刻也在",
          (sm["last_date"], sm["last_time"]) == ("2028-06-30", "07:01"), str(sm))
    check("末章排在更早 → 末章自己被标成倒挂", sm["last_inverted"] is True, str(sm))
    check("队尾那一章也记着名字（标签要能指名道姓）",
          sm["tail_chapter"].startswith("第5191章"), str(sm))

    print()
    print("== ③ 排期倒挂 / 撞时刻 能被工具自己发现 ==")
    # 实测平台形状：5192~5196 早于 5191（倒挂 5 处）；
    # 4783~4787 与 5192~5196 占同一批时刻（撞 5 处）
    items = []
    for i, (pos, t) in enumerate([
            (4783, "2028-06-30 07:00"), (4784, "2028-06-30 07:01"),
            (4785, "2028-06-30 12:00"), (4786, "2028-06-30 12:01"),
            (4787, "2028-06-30 20:00"),
            (5191, "2028-09-06 07:00"),
            (5192, "2028-06-30 07:00"), (5193, "2028-06-30 07:01"),
            (5194, "2028-06-30 12:00"), (5195, "2028-06-30 12:01"),
            (5196, "2028-06-30 20:00")]):
        items.append(item(pos, f"第{pos}章", t))
    st = fu.queue_state(items)
    check("倒挂 5 处（位置在后面却排在更早）",
          len(st["inverted"]) == 5, str([r["pos"] for r in st["inverted"]]))
    check("撞时刻 5 处",
          len(st["clash"]) == 5, str(st["clash"]))
    check("倒挂的章指得出是前面哪一章压着它",
          st["inverted"][0]["after_pos"] == 5191, str(st["inverted"][0]))
    rep = fu.audit_chapter_positions(items)
    check("体检报告里带出排期倒挂", len(rep["sched_inverted"]) == 5,
          str(rep["sched_inverted"]))
    check("体检报告里带出撞时刻", len(rep["sched_clash"]) == 5, str(rep["sched_clash"]))
    lines = fu.schedule_order_lines(rep)
    check("报告说得清怎么修", any("修改排期" in ln for ln in lines), str(lines))
    check("报告点名了倒挂的章", any("位置5192" in ln for ln in lines), str(lines))
    rep_ok = fu.audit_chapter_positions([item(1, "第1章", "2028-01-01 07:00")])
    check("正常书没有这一段", fu.schedule_order_lines(rep_ok) == [])

    print()
    print("== ④ 行 -> items 的转换只有一份，且不丢真状态 ==")
    # 接口那条路：行里带 display_status/timer_time/pos，直接用
    rows = [{
        "title": "第5196章 末章", "status": "待发布",
        "display_status": fu.DISPLAY_PENDING,
        "timer_time": ts("2028-06-30 20:00"),
        "pos": 5196, "date": "2028-06-30", "time": "20:00"}]
    got = fu.rows_to_items(rows)
    check("接口行的 timer_time 直接用",
          got[0]["timer_time"] == ts("2028-06-30 20:00"), str(got))
    check("接口行的状态码直接用",
          got[0]["display_status"] == fu.DISPLAY_PENDING, str(got))
    check("接口行的位置直接用", got[0]["pos"] == 5196, str(got))
    # DOM 兜底那条路：只有中文状态 + 日期时间，反推得出坑位
    rows = [
        {"title": "第1章 甲", "status": "待发布",
         "date": "2028-06-30", "time": "20:00"},
        {"title": "第2章 乙", "status": "审核中",
         "date": "2028-10-01", "time": "07:00"},
        {"title": "第3章 丙", "status": "已发布",
         "date": "2026-01-01", "time": "07:00"},
    ]
    got = fu.rows_to_items(rows)
    check("DOM 行：待发布从日期反推坑位",
          got[0]["timer_time"] == ts("2028-06-30 20:00"), str(got))
    check("DOM 行：审核中也占坑位（老代码只认待发布）",
          got[1]["timer_time"] == ts("2028-10-01 07:00")
          and got[1]["display_status"] == fu.DISPLAY_REVIEWING, str(got))
    check("DOM 行：已发布不占坑位",
          got[2]["timer_time"] == 0
          and got[2]["display_status"] == fu.DISPLAY_PUBLISHED, str(got))
    check("DOM 行：位置未知时如实留空（不拿列表顺序冒充位置）",
          got[0]["pos"] is None, str(got))
    check("位置未知 -> 队尾照常算（位置与时刻无关）",
          fu.queue_tail_dt(got) == datetime.fromtimestamp(ts("2028-10-01 07:00")),
          str(fu.queue_tail_dt(got)))

    print()
    print("== ⑤ 接口行不再把审核中的章降级成创建时间 ==")
    api = [{
        "item_id": "A", "index": 2, "title": "第2章 在审",
        "display_status": fu.DISPLAY_REVIEWING,
        "timer_time": str(ts("2028-10-01 07:00")),
        "create_time": str(ts("2026-10-08 20:14")),
        "cant_modify_reason": "审核中暂不支持修改"},
        {"item_id": "B", "index": 1, "title": "第1章 已发",
         "display_status": fu.DISPLAY_PUBLISHED,
         "timer_time": "0",
         "create_time": str(ts("2026-03-05 20:39")),
         "cant_modify_reason": ""},
    ]
    rows = fu.api_items_to_rows(api, "7613749318914149401")
    check("审核中的章保留排期时刻（不再降级成创建时间）",
          (rows[0]["date"], rows[0]["time"]) == ("2028-10-01", "07:00"),
          f"{rows[0]['date']} {rows[0]['time']}")
    check("已发布的章仍用创建时间（那是它公开的时刻）",
          (rows[1]["date"], rows[1]["time"]) == ("2026-03-05", "20:39"),
          f"{rows[1]['date']} {rows[1]['time']}")
    check("行里带 display_status", rows[0]["display_status"] == fu.DISPLAY_REVIEWING)
    check("行里带 timer_time", rows[0]["timer_time"] == ts("2028-10-01 07:00"))
    check("行里带位置（接口的 index）", rows[0]["pos"] == 2, str(rows[0]))

    print()
    print("== ⑥ 复现 2026-10-10 那次续排出事 ==")
    # 当时：第4577~4782章待发布（排到 2028-06-29 20:01），第4783~5191章
    # 还在审核（同样占着后面的坑位，最晚到 2028-09-06 07:00）。
    # 老口径只数「待发布」-> 队尾 2028-06-29 20:01 -> 新章被排到
    # 2028-06-30 07:00~20:00，正好撞上已占的坑位。
    old = []
    new = []
    for i in range(615):
        pos = 4577 + i
        # 每章往后 15 分钟一个坑位（够用来还原相对次序）
        mins = 4577 * 15 + i * 15
        when = datetime.fromtimestamp(ts("2028-05-26 20:00")).timestamp() + mins * 60
        when_txt = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
        ds = fu.DISPLAY_PENDING if pos <= 4782 else fu.DISPLAY_REVIEWING
        new.append(item(pos, f"第{pos}章", when_txt, ds=ds))
        if ds == fu.DISPLAY_PENDING:
            old.append(item(pos, f"第{pos}章", when_txt, ds=ds))
    tail_new = fu.queue_tail_dt(new)
    tail_old = fu.queue_tail_dt(old)
    check("新口径：队尾在最后那一章（2028-09-06 07:00 之后）",
          tail_new > tail_old, f"{tail_new} vs {tail_old}")
    check("新口径的队尾严格晚于老口径（不再排进已占的坑位）",
          tail_new == fu.queue_tail_dt(new) and tail_new != tail_old,
          f"{tail_new} vs {tail_old}")
    # 用老口径排期：正好落在已占的 2028-06-30 那些时刻上
    check("老口径会把新章排到已占坑位之前（这就是那次事故的形状）",
          tail_old < tail_new, f"{tail_old} < {tail_new}")

    print()
    print("== ⑦ 标签文案：末章和队尾都要说清楚 ==")
    sm_ok = {"date": "2028-09-06", "time": "07:00",
             "chapter": "第5196章 我不能保证明天还在",
             "last_date": "2028-09-06", "last_time": "07:00",
             "inverted": 0, "clash": 0, "last_inverted": False}
    t_ok = fu.queue_label_text(sm_ok)
    check("正常书：队尾时刻 + 末章",
          t_ok == "队列排到：2028-09-06 07:00（第5196章 我不能保证明天还在）", t_ok)
    sm_bad = dict(sm_ok, inverted=5, clash=5, last_inverted=True,
                  last_date="2028-06-30", last_time="20:00")
    t_bad = fu.queue_label_text(sm_bad)
    check("排期错乱：仍然报末章（第5196章）",
          "第5196章" in t_bad.split("\n")[0], t_bad)
    check("排期错乱：第二行说清末章排在几点",
          "2028-06-30 20:00" in t_bad and "比队尾更早" in t_bad, t_bad)
    check("排期错乱：给出倒挂/撞时刻的条数",
          "5 章排在比它前面那章更早" in t_bad
          and "5 个时刻被两章以上占用" in t_bad, t_bad)
    check("排期错乱：说清怎么修", "修改排期" in t_bad, t_bad)
    check("读不到平台数据时不编日期",
          fu.queue_label_text({"date": None, "time": None}) == "暂无发布记录")

    print()
    print("== ⑨ 排期顺序护栏：发布顺序必须 = 位置顺序 ==")
    # 真实形状：位置 4783~4787 与 5192~5196 占用同一批时刻，
    # 5192~5196 又排在位置 5191（2028-09-06）之前。
    items = []
    for pos, t in ((4783, "2028-06-30 07:00"), (4784, "2028-06-30 07:01"),
                   (4785, "2028-06-30 12:00"), (4786, "2028-06-30 12:01"),
                   (4787, "2028-06-30 20:00"),
                   (5191, "2028-09-06 07:00"),
                   (5192, "2028-06-30 07:00"), (5193, "2028-06-30 07:01"),
                   (5194, "2028-06-30 12:00"), (5195, "2028-06-30 12:01"),
                   (5196, "2028-06-30 20:00")):
        items.append(item(pos, f"第{pos}章", t))
    # ① 新章排到已占的坑位上（2026-10-10 那次事故的形状）→ 必须报出来
    bad = [("2028-06-30", "07:00"), ("2028-06-30", "07:01"),
           ("2028-06-30", "12:00")]
    conf = fu.schedule_order_conflicts(items, new_slots=bad)
    check("新章排在队尾之前 → 报冲突", len(conf) > 0, str(conf))
    check("冲突里点得出「比它前面的章更早」",
          any("更早" in c for c in conf), str(conf))
    check("冲突里点得出「撞了哪一章」",
          any("撞了" in c for c in conf), str(conf))
    # ② 新章排在队尾之后 → 干净
    ok = [("2028-09-06", "12:00"), ("2028-09-06", "20:00")]
    check("新章排在队尾之后 → 无冲突",
          fu.schedule_order_conflicts(items, new_slots=ok) == [])
    # ③ 改已有章排到更早 → 也要报（改动前已有的错位不该反复冒出来）
    changes = {5196: fu.slot_timestamp("2028-06-30", "07:00")}
    conf2 = fu.schedule_order_conflicts(items, changes=changes)
    check("把章改到更早 → 报冲突", len(conf2) > 0, str(conf2))
    check("只报与本次改动有关的（不把旧错位算进来）",
          all("5196" in c for c in conf2), str(conf2))
    # ④ 已发布章的旧错位不该在改别章时冒出来
    changes2 = {4783: fu.slot_timestamp("2028-06-30", "07:00")}
    conf3 = fu.schedule_order_conflicts(items, changes=changes2)
    check("改别章时不报旧错位（只报 4783 自己的）",
          all("4783" in c for c in conf3), str(conf3))

    print()
    print("== ⑩ 一键修复排期 ==")
    plan, lines = fu.schedule_repair_plan(
        items, pub_time="07:00,12:00,20:00", per_day=6)
    check("方案非空", bool(plan), str(lines))
    check("只挪错的那 5 章（5192~5196），不牵连前面的",
          [p for p, _t, _d, _tt in plan] == [5192, 5193, 5194, 5195, 5196],
          str([p for p, _t, _d, _tt in plan]))
    check("挪到队尾之后（晚于 2028-09-06 07:00）",
          all(f"{d} {t}" > "2028-09-06 07:00" for _p, _ti, d, t in plan),
          str(plan))
    check("修复后不再有冲突",
          fu.schedule_order_conflicts(
              items,
              changes={p: fu.slot_timestamp(d, t) for p, _ti, d, t in plan}) == [])
    check("方案里没有跟已有章撞时刻",
          not (set(fu.slot_timestamp(d, t) for _p, _ti, d, t in plan)
               & {fu.chapter_publish_ts(it) for it in items}),
          str(plan))
    check("说明里说清挪了几章、挪到哪",
          any("队尾" in ln for ln in lines) and any("5" in ln for ln in lines),
          str(lines))
    # 没有错位就没有方案
    clean = [item(i, f"第{i}章", f"2028-06-{i:02d} 07:00") for i in range(1, 5)]
    check("排期没问题时没有方案",
          fu.schedule_repair_plan(clean, pub_time="07:00", per_day=3) == ([], []))

    print()
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
