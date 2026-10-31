# -*- coding: utf-8 -*-
"""接口版章节列表（api_items_to_rows / _extract_via_api）零依赖测试。

2026-08-27 把「获取目录」从逐页点下一页的 DOM 抓取换成 chapter_list 接口
（1326 章实测 48.1s -> 1.0s，同日与 DOM 逐条比对 1326 行 x 7 字段零差异）。
这里守两件事:

1. 行结构必须与 _EXTRACT_ALL_JS 完全同形同义——下游按 "待发布" in status 筛
   改排期、按 "审核中" in status 跳过不可编辑章、按 editUrl 导航去改内容。
   任何一项翻译错都不是"显示不对"，是把内容写进别的章。
2. 签名 URL 是从 resource timing 捞的，切卷后可能还是上一卷的。首行对不上
   就必须退回 DOM，宁可慢 50 倍也不能拿别的卷的章节号去匹配。

运行:  python tests/test_api_chapter_rows.py
"""
import asyncio
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
BOOK = "7613749318914149401"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def ts(text):
    """本地时区的 '2026-12-01 20:01' -> unix 秒（平台就是按本机时区渲染的）。"""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp())


def item(**kw):
    base = {"item_id": "111", "index": 1, "title": "第1章 开端",
            "display_status": fu.DISPLAY_PENDING, "create_time": "0",
            "timer_time": "0", "cant_modify_reason": ""}
    base.update(kw)
    return base


def main():
    # ---- 1. 两种状态各取各的时间戳 ----
    rows = fu.api_items_to_rows([
        item(item_id="A", title="第9章 待", display_status=fu.DISPLAY_PENDING,
             timer_time=str(ts("2026-12-01 20:01")),
             create_time=str(ts("2026-08-27 20:59"))),
        item(item_id="B", title="第1章 已", display_status=fu.DISPLAY_PUBLISHED,
             timer_time="0", create_time=str(ts("2026-03-05 20:39"))),
    ], BOOK)
    check("待发布取 timer_time（改排期靠它）",
          (rows[0]["date"], rows[0]["time"]) == ("2026-12-01", "20:01"),
          f"{rows[0]['date']} {rows[0]['time']}")
    check("已发布取 create_time（平台发布时改写该字段）",
          (rows[1]["date"], rows[1]["time"]) == ("2026-03-05", "20:39"),
          f"{rows[1]['date']} {rows[1]['time']}")
    check("状态文案与 DOM 那一列一致",
          rows[0]["status"] == "待发布" and rows[1]["status"] == "已发布",
          f"{rows[0]['status']!r} {rows[1]['status']!r}")
    check("下游筛选照常: '待发布' in status 只命中待发布章",
          ["待发布" in r["status"] for r in rows] == [True, False])
    check("editUrl 形状与页面「编辑」按钮一致",
          rows[0]["editUrl"] ==
          f"/main/writer/{BOOK}/publish/A/?enter_from=modifychapter",
          rows[0]["editUrl"])
    check("rowIndex 保持返回顺序", [r["rowIndex"] for r in rows] == [0, 1])
    # 2026-10-31：行里带平台真状态（算队尾要靠它们），位置取接口的 index
    check("行里带 display_status", rows[0]["display_status"] == fu.DISPLAY_PENDING)
    check("行里带 timer_time", rows[0]["timer_time"] == ts("2026-12-01 20:01"))
    check("行里带位置（接口的 index）", rows[0]["pos"] == 1, str(rows[0].get("pos")))
    # 审核中的章同样占着未来的坑位，排期时刻不能被降级成创建时间
    # （2026-10-10 实测：降级会让队尾算短，新章排进已占用的时刻）
    _rev = fu.api_items_to_rows([
        item(item_id="C", title="第3章 在审", display_status=fu.DISPLAY_REVIEWING,
             timer_time=str(ts("2026-12-05 08:00")),
             create_time=str(ts("2026-08-27 20:59")),
             cant_modify_reason="审核中暂不支持修改"),
    ], BOOK)
    check("审核中的章保留排期时刻",
          (_rev[0]["date"], _rev[0]["time"]) == ("2026-12-05", "08:00"),
          f"{_rev[0]['date']} {_rev[0]['time']}")

    # ---- 2. 平台说不让改的章，必须和 DOM 一样拿不到编辑链接 ----
    # 2026-08-28 在完结作品上实测: 平台根本不渲染「编辑」按钮，DOM 的 editUrl
    # 是 None，修改模式据此安全跳过。接口侧凭 item_id 硬拼一个链接，就会变成
    # "导航过去改一章平台明说不让改的章"。
    rows = fu.api_items_to_rows([
        item(title="第1章 完结", display_status=fu.DISPLAY_PUBLISHED,
             cant_modify_reason="完结作品不可修改"),
        item(title="第2章 未知", display_status=99, cant_modify_reason="审核中"),
        item(title="第3章 未知无理由", display_status=99, cant_modify_reason=""),
        item(title="第4章 正常", display_status=fu.DISPLAY_PENDING),
    ], BOOK)
    check("cant_modify_reason 非空 -> editUrl 为 None（与 DOM 一致）",
          rows[0]["editUrl"] is None, rows[0]["editUrl"])
    check("状态文案保持 DOM 原样，不把理由拼进去",
          rows[0]["status"] == "已发布", rows[0]["status"])
    check("未知状态 -> 用平台自己的说法当状态（'审核中' in status 才有机会命中）",
          rows[1]["status"] == "审核中", rows[1]["status"])
    # 「不可乱改」的保障是"平台明说了不能改"（reason 非空）。实测审核不通过
    # （status=7, reason 为空）的编辑器能进，而那一章恰恰是解审核卡队列唯一
    # 能动的地方 —— 所以 status=7 必须可改。
    # 现在 7 已经是个**认得的码**（_DISPLAY_STATUS_TEXT 里有它），走「认得的
    # 码」那条分支照样可改，不必再把没见过的码一起放开：那个码平台会拿它
    # 做什么我们不知道，凭「reason 为空」就合成编辑链接，等于把写权限开到
    # 一个没人验证过的状态上（改内容要重过审，读者会收到更新）。
    check("未知状态 + 平台明说不可改 -> 不给编辑链接（不可乱改的真正保障）",
          rows[1]["editUrl"] is None, rows[1]["editUrl"])
    check("未知状态 + 平台没给理由 -> 也不给编辑链接（没验证过的码不去动）",
          rows[2]["editUrl"] is None, rows[2]["editUrl"])
    check("未知状态且平台没给理由 -> 状态文本仍可辨认，不伪装成已发布",
          rows[2]["status"] == "未知状态99", rows[2]["status"])
    check("未知状态不冒充待发布（否则会被拉进改排期名单）",
          not any("待发布" in r["status"] for r in rows[:3]),
          [r["status"] for r in rows[:3]])
    check("正常待发布章照常拿到编辑链接",
          rows[3]["editUrl"] and rows[3]["editUrl"].endswith(
              "/?enter_from=modifychapter"), rows[3]["editUrl"])

    # ---- 3. 缺字段不崩、不给假数据 ----
    rows = fu.api_items_to_rows([
        item(title="楔子", timer_time=None, create_time=None),
        item(item_id=None, title="第5章 无id"),
        item(title="2024新春番外", timer_time="not-a-number"),
    ], BOOK)
    check("无时间戳 -> date/time 为 None（不是 1970）",
          rows[0]["date"] is None and rows[0]["time"] is None,
          f"{rows[0]['date']} {rows[0]['time']}")
    check("无 item_id -> editUrl 为 None（调用方据此跳过）",
          rows[1]["editUrl"] is None, rows[1]["editUrl"])
    check("时间戳是脏字符串也不崩", rows[2]["date"] is None, rows[2]["date"])
    check("没有「第N章」的标题 chapterNum 为 None（楔子/番外不参与编号）",
          rows[0]["chapterNum"] is None, rows[0]["chapterNum"])
    check("数字开头番外不被误编号 2024（与 DOM 侧前视守卫一致）",
          rows[2]["chapterNum"] is None, rows[2]["chapterNum"])

    # ---- 4. 章节号解析与平台标题一致 ----
    rows = fu.api_items_to_rows(
        [item(title=t) for t in ("第12章 甲", "300:遇见", "1 开端")], BOOK)
    check("章节号解析: 第N章 / 裸数字+分隔 / 裸数字+空格",
          [r["chapterNum"] for r in rows] == [12, 300, 1],
          [r["chapterNum"] for r in rows])

    # ---- 5. _extract_via_api 的回退闸门 ----
    class FakePage:
        def __init__(self, signed, fetch_res, first_cell):
            self.signed, self.fetch_res, self.first_cell = signed, fetch_res, first_cell

        async def evaluate(self, js, arg=None):
            if js is fu._SIGNED_CHAPTER_LIST_JS:
                return self.signed
            if js is fu._FETCH_ITEMS_JS:
                return self.fetch_res
            return self.first_cell

    ITEMS = {"items": [item(title="第1326章 货到那刻")]}

    def run(page):
        return asyncio.new_event_loop().run_until_complete(
            fu._extract_via_api(page, BOOK))

    check("首行对得上 -> 用接口结果",
          len(run(FakePage("u?page_index=0", ITEMS, "第1326章 货到那刻")) or []) == 1)
    check("首行是别的卷 -> 退回 DOM（返回 None）",
          run(FakePage("u?page_index=0", ITEMS, "第7章 另一卷")) is None)
    check("页面首行带「编辑」等尾巴仍算对得上",
          run(FakePage("u?page_index=0", ITEMS, "第1326章 货到那刻 编辑")) is not None)
    check("没捞到签名 URL -> 退回 DOM",
          run(FakePage(None, ITEMS, "第1326章 货到那刻")) is None)
    check("接口报错 -> 退回 DOM，绝不返回半份",
          run(FakePage("u?page_index=0", {"error": "第3页 HTTP 500"}, "x")) is None)
    check("空书: 没有行就不做首行校验，正常返回空",
          run(FakePage("u?page_index=0", {"items": []}, "")) == [])

    class BoomPage(FakePage):
        async def evaluate(self, js, arg=None):
            raise RuntimeError("页面已销毁")
    check("evaluate 抛异常 -> 退回 DOM 而不是炸掉整个抓取",
          run(BoomPage(None, None, None)) is None)

    print("== 可编辑性：认得的码看 cant_modify_reason，没认得的码一律不动 ==")
    _ed = fu.api_items_to_rows([
        item(item_id="A", title="第7章 被拒", display_status=7),      # reason 空
        item(item_id="B", title="第4章 在审", display_status=4,
             cant_modify_reason="审核中暂不支持修改"),
        item(item_id="C", title="第9章 完结", display_status=98,
             cant_modify_reason="完结作品不可修改"),
        item(item_id="D", title="第5章 未知", display_status=42),     # reason 空
    ], BOOK)
    _by = {r["chapterNum"]: r for r in _ed}
    check("审核不通过(reason空) -> 有编辑链接", bool(_by[7]["editUrl"]), _by[7])
    check("审核不通过 -> 显示「审核不通过」",
          _by[7]["status"] == "审核不通过", _by[7]["status"])
    check("审核不通过 -> 不含「审核中」（不会被修改模式误跳过）",
          "审核中" not in _by[7]["status"], _by[7]["status"])
    check("审核中(reason非空) -> 无编辑链接", not _by[4]["editUrl"], _by[4])
    check("审核中 -> 显示「审核中」",
          _by[4]["status"] == "审核中", _by[4]["status"])
    check("审核中 -> 含「审核中」（修改模式会正确跳过）",
          "审核中" in _by[4]["status"], _by[4]["status"])
    # 审核中 + 平台没给理由：仍然**不能**改。改前 4 是个没人认得的码，
    # 一律不可编辑；现在它是认得的码了，不能跟着「reason 为空 → 可改」
    # 一起放开——那一章正在审，工具不该去动它（改内容要重过审，读者会
    # 收到更新）。放开那条路的只有审核不通过（7）。
    _ed4 = fu.api_items_to_rows([
        item(item_id="E", title="第4章 在审", display_status=4),
    ], BOOK)
    check("审核中(reason为空) -> 仍无编辑链接（正在审的不碰）",
          not _ed4[0]["editUrl"], _ed4[0])
    # 布尔值也是 int 的子类：display_status=true 会被当成 1（已发布），
    # 于是拿到编辑入口。平台的数值字段发来布尔值时，一律当「认不出的码」。
    for _bad in (True, False):
        _edb = fu.api_items_to_rows([
            item(item_id="F", title="第6章 布尔", display_status=_bad),
        ], BOOK)
        check(f"display_status={_bad!r}（布尔） -> 无编辑链接",
              not _edb[0]["editUrl"], _edb[0])
    check("平台明说不可改的未知状态 -> 无编辑链接", not _by[9]["editUrl"], _by[9])
    check("未知状态 + reason空 -> 不给编辑链接（没验证过的状态码不去动）",
          not _by[5]["editUrl"], _by[5])

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
