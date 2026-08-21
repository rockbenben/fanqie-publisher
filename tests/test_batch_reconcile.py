# -*- coding: utf-8 -*-
"""批次收尾对账 + 章节位置体检的纯函数契约（零依赖）。

背景: 2026-07-24 748 章定时发布，日志记"成功 736"，平台上却少 151 章，
三周后人工枚举才发现——那时早已超过手机 App 的 3 天移动窗口，全部永久错位。
对账就是把"日志说的"和"平台真有的"当场怼一遍。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fanqie_upload import (  # noqa: E402
    find_missing_after_batch, audit_chapter_positions, chapter_title_num,
    volume_count, global_position, VOLUME_INDEX_STRIDE,
    verify_content_matches_book,
    DISPLAY_PUBLISHED, DISPLAY_PENDING,
)

FAILED = []


def check(label, cond):
    if cond:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}")
        FAILED.append(label)


def item(index, title, ds=DISPLAY_PUBLISHED, ct=0, tt=0):
    return {"index": index, "title": title, "display_status": ds,
            "create_time": ct, "timer_time": tt, "item_id": f"i{index}",
            "cant_modify_reason": ""}


print("== 章号解析 ==")
check("第12章", chapter_title_num("第12章 标题") == 12)
check("全角空格/回节话", chapter_title_num("第 7 回 x") == 7)
check("番外无号", chapter_title_num("番外：后日谈") is None)
check("空标题不炸", chapter_title_num("") is None and chapter_title_num(None) is None)

print("== 批次对账 ==")
items = [item(1, "第1章 甲"), item(2, "第2章 乙"), item(3, "第4章 丁")]
check("全在→无缺", find_missing_after_batch(items, [1, 2]) == [])
check("第3章记成功但平台没有→报出来",
      find_missing_after_batch(items, [1, 2, 3, 4]) == [3])
check("多章缺失按升序", find_missing_after_batch(items, [9, 3, 7]) == [3, 7, 9])
check("空集合不报", find_missing_after_batch(items, []) == [])
check("claimed 含 None 不炸", find_missing_after_batch(items, [None, 1]) == [])
check("平台标题无章号不当作存在",
      find_missing_after_batch([item(1, "番外")], [1]) == [1])
# 关键: 待发布章也算"在平台上"——它已经建出来了，只是还没公开
check("待发布章算存在",
      find_missing_after_batch([item(5, "第5章 戊", ds=DISPLAY_PENDING)], [5]) == [])

print("== 位置体检: 均匀后移不算错位，倒挂才算 ==")
NOW, H = 1_000_000, 3600
shifted = [item(1, "第1章", ct=NOW - 99 * H), item(2, "第3章", ct=NOW - 98 * H),
           item(3, "第4章", ct=NOW - 97 * H)]
rep = audit_chapter_positions(shifted, now_ts=NOW)
check("缺号但顺序单调→不报", rep["in_window"] == [] and rep["expired"] == [])

stranded = shifted + [
    item(4, "第2章", ct=NOW - 1 * H),      # 1 小时前发的补章，吊在末尾
    item(5, "第3章", ct=NOW - 100 * H),    # 100 小时前，超窗口
]
rep = audit_chapter_positions(stranded, now_ts=NOW, window_h=72)
check("窗口内倒挂→B段", [r["num"] for r in rep["in_window"]] == [2])
check("超窗口倒挂→C段", [r["num"] for r in rep["expired"]] == [3])
check("B段剩余小时数正确", 70 < rep["in_window"][0]["left_h"] < 72)

print("== 位置体检: 未公开段 ==")
pend = [item(1, "第1章"), item(2, "第9章", ds=DISPLAY_PENDING, tt=NOW + 50 * H),
        item(3, "第3章", ds=DISPLAY_PENDING, tt=NOW + 60 * H)]
rep = audit_chapter_positions(pend, now_ts=NOW)
check("位置≠章号→A段", rep["pending_bad"] == [2])
check("位置==章号→不进A段", 3 not in rep["pending_bad"])
check("已公开的不进A段", 1 not in rep["pending_bad"])

print("== 章号解析与平台标题对称（裸数字标题也认） ==")
check("裸数字+空格", chapter_title_num("1 开端") == 1)
check("裸数字+冒号", chapter_title_num("27：黛玉葬花") == 27)
check("裸数字结尾", chapter_title_num("39") == 39)
check("年份不误判", chapter_title_num("2023年的夏天") is None)
check("裸数字标题不再误报漏章",
      find_missing_after_batch([item(1, "1 开端")], [1]) == [])

print("== 内容归属校验（防定时任务写错书）==")
import tempfile, os
_tmp = tempfile.mkdtemp()
def _mk(n, title):
    fp = os.path.join(_tmp, f"chapter-{n}.md")
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write("# 第%d章 %s\n\n正文\n" % (n, title))
    return fp
right = {1: _mk(1, "开端"), 2: _mk(2, "转折"), 3: _mk(3, "终局")}
plat_ok = [item(1, "第1章 开端"), item(2, "第2章 转折"), item(3, "第3章 终局")]
plat_other = [item(1, "第1章 另一本书"), item(2, "第2章 完全不同"),
              item(3, "第3章 毫不相干")]
ok, why = verify_content_matches_book(plat_ok, right)
check("同一本书放行", ok is True and "8" not in why)
ok2, why2 = verify_content_matches_book(plat_other, right)
check("拿错目录拦下", ok2 is False and "疑似不属于这本书" in why2)
# 只有待发布章时无从比对 —— 放行而不是误拦（新书首次发布就是这种）
ok3, why3 = verify_content_matches_book(
    [item(1, "第1章 开端", ds=DISPLAY_PENDING)], right)
check("无已发布样本时放行", ok3 is True and "跳过校验" in why3)
# 平台已发布章本地没有 -> 无样本，同样放行
ok4, _w = verify_content_matches_book([item(9, "第9章 未知")], right)
check("本地缺该章时不误拦", ok4 is True)

print("== 多卷: index = 卷序号*10000 + 卷内位置 ==")
# 真多卷作品实测: 卷1 index 1~100、卷2 10001~10150、卷3 20001~20115，
# 对应第1~100 / 第151~250 / 第266~365 章（365 章逐条比对零偏差）
OFF = {0: 0, 1: 100, 2: 250}
check("卷1 首章", global_position(1, OFF) == 1)
check("卷1 末章", global_position(100, OFF) == 100)
check("卷2 首章", global_position(10001, OFF) == 101)
check("卷2 index 10051 -> 第151位", global_position(10051, OFF) == 151)
check("卷3 index 20016 -> 第266位", global_position(20016, OFF) == 266)
check("单卷等同 index", global_position(50, {0: 0}) == 50)
check("未知卷序号不炸", global_position(90001, OFF) == 1)
check("stride 常量", VOLUME_INDEX_STRIDE == 10000)

print("== 多卷: 体检按全书位置判定 ==")
mv = [
    # 卷1: 位置 1、2 装第1、2章 —— 正常
    item(1, "第1章", ds=DISPLAY_PENDING, tt=NOW + 50 * H),
    item(2, "第2章", ds=DISPLAY_PENDING, tt=NOW + 51 * H),
    # 卷2 首章: 原始 index 10001，全书位置 3，标题却是第4章 —— 该报
    dict(item(10001, "第4章", ds=DISPLAY_PENDING, tt=NOW + 52 * H), pos=3),
    # 卷2 次章: 原始 index 10002，全书位置 4，标题第4章 —— 正常
    dict(item(10002, "第4章", ds=DISPLAY_PENDING, tt=NOW + 53 * H), pos=4),
]
rep = audit_chapter_positions(mv, now_ts=NOW)
check("只报全书位置 3 那一条", rep["pending_bad"] == [3])
check("不会把整卷误判为错位", 10001 not in rep["pending_bad"]
      and 10002 not in rep["pending_bad"])

print("== 多卷: 漏章对账跨卷合并 ==")
# 卷2 的章必须算"平台上有"，否则整卷被判成漏章（这正是多卷最危险的假漏报）
across = [item(1, "第1章"), dict(item(10001, "第2章"), pos=2),
          dict(item(20001, "第3章"), pos=3)]
check("跨卷都算存在", find_missing_after_batch(across, [1, 2, 3]) == [])
check("真缺的仍报出", find_missing_after_batch(across, [1, 2, 3, 4]) == [4])

print("== 分卷计数 ==")
check("无分卷信息按 1 卷", volume_count({}) == 1)
check("空列表按 1 卷", volume_count({"volumes": []}) == 1)
check("两卷", volume_count({"volumes": ["第一卷", "第二卷"]}) == 2)
check("字段异常不炸", volume_count({"volumes": None}) == 1)


print("== 新章确认（防永久缺口的最后一道闸）==")
import asyncio as _aio
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "keep_ahead"))
import keep_ahead as _K


class _FakePage:
    """按预设序列返回列表标题，模拟"新章稍后才出现"。

    序列元素可以是: 标题列表（成功）/ Exception（evaluate 本身抛）/
    {"error": ...}（接口通了但返回 429、code!=0 —— 探测器必须把它当
    「这次问不出来」而不是「平台上没有」）。
    """

    def __init__(self, seq):
        self.seq, self.n = seq, 0

    async def evaluate(self, js, arg):
        v = self.seq[min(self.n, len(self.seq) - 1)]
        self.n += 1
        if isinstance(v, Exception):
            raise v
        if isinstance(v, dict):
            return v          # 直接透传 {"error": ...}
        return {"titles": v}  # 探测 JS 的成功返回形状

    async def wait_for_timeout(self, ms):
        pass


def _confirm(seq, num=1470, **kw):
    p = _FakePage(seq)
    return _aio.run(_K.fu.confirm_chapter_on_platform(p, "u?page_index=0&page_count=15",
                                       num, poll_s=0, **kw)), p


# 接口报错（429 / code=-100）不等于「平台上没有」：判成没有会中止整批并
# 记入补传清单，用户照单补传就是书尾多一章永远移不回去的重复章。
_ok, _p = _confirm([{"error": "http 429"}, ["第1470章 甲"]])
check("探测报错后继续轮询，问通即确认", _ok is True)
_ok, _p = _confirm([{"error": "code -100 服务器开小差了"}], window_s=0)
check("整窗都问不通 -> 放行交批末对账（不判失败）", _ok is True)
_ok, _p = _confirm([["第999章 别的"]], window_s=0)
check("问得通但确实没有 -> 判失败", _ok is False)

_ok, _p = _confirm([["第1470章 甲", "第1469章 乙"]])
check("立刻出现→确认，且不多轮询", _ok is True and _p.n == 1)
_ok, _p = _confirm([["第1469章 乙"], ["第1469章 乙"], ["第1470章 甲"]])
check("滞后几轮才出现→仍确认", _ok is True and _p.n == 3)
check("一直不出现→报False（否则就是永久缺口）",
      _confirm([["第1469章 乙"]], window_s=0)[0] is False)
# 这条原本断言「evaluate 抛异常 -> 判 False」。那个判据是错的: 一次网络抖动
# 就会中止整批 + 把已发成功的章记进补传清单，用户照单补传 = 书尾多一章重复，
# 而重复章按番茄的追加序永远移不回去。异常属于「这次问不出来」，应放行，
# 由批末对账拿完整章节列表核实（代价只是晚一点发现）。
check("evaluate 抛异常 -> 放行交批末对账，不判失败",
      _confirm([RuntimeError("boom")], window_s=0)[0] is True)
check("异常之后问通了 -> 正常确认",
      _confirm([RuntimeError("boom"), ["第1470章 甲"]])[0] is True)
check("隔壁章出现不能算数",
      _confirm([["第1471章 丙", "第1469章 乙"]], window_s=0)[0] is False)
check("裸数字标题也认", _confirm([["1470 甲"]])[0] is True)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
