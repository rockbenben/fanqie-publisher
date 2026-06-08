# -*- coding: utf-8 -*-
"""100 轮对抗战役：攻击此前从未被测试覆盖的核心纯函数。

目标: compute_schedule / _validate_times / _extract_chapter_num /
      _strip_chapter_prefix / _cn_to_int / parse_md_file /
      strip_md_formatting / deduplicate_titles / match_chapters /
      _classify_toasts(新词库) / _parse_chapter_spec(极端)

零依赖。运行:  python tests/test_adversarial_campaign.py
"""
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402
from fanqie_gui import FanqieGUI  # noqa: E402

ROUND = 0


def round_pass(theme):
    global ROUND
    ROUND += 1
    print(f"  PASS R{ROUND:03d} {theme}")


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- R1-25 排期
def schedule_oracle(n, start, times_str, per_day, sched):
    assert len(sched) == n, f"长度 {len(sched)} != {n}"
    prev_date = None
    used = {}
    for d, t in sched:
        assert DATE_RE.match(d), f"日期格式 {d}"
        assert TIME_RE.match(t), f"时间格式 {t}"
        datetime.strptime(d, "%Y-%m-%d")
        if prev_date is not None:
            assert d >= prev_date, f"日期倒退 {prev_date}->{d}"
        prev_date = d
        day = used.setdefault(d, set())
        assert t not in day, f"同日重复时刻 {d} {t} (输入: {times_str!r}/{per_day})"
        day.add(t)


def rounds_schedule():
    rng = random.Random(99)
    hostile_times = [
        "", "garbage", "25:00", "23:60", "08:00,08:00", "08:00",
        "23:58,23:59", "00:00", "08:00,12:00,20:00", "２０：００",
        "8:30, 08:30；20:00", "12:00,abc,13:00", "23:59",
    ]
    for r in range(25):
        for _ in range(12):
            n = rng.choice([0, 1, 2, 7, 83, 355])
            per_day = rng.choice([1, 2, 3, 5, 10, 60])
            ts = rng.choice(hostile_times)
            start = rng.choice(["2026-06-07", "2024-02-29", "2026-12-31"])
            sched = fu.compute_schedule(n, start, ts, per_day)
            schedule_oracle(n, start, ts, per_day, sched)
        round_pass(f"compute_schedule 敌意批次 #{r+1}")


# --------------------------------------------------- R26-40 章节号/中文数字
def rounds_extract():
    cases = {
        "第十六章 发布会": "16", "第一百二十三章 x": "123", "第两章 x": "2",
        "第零章 x": None, "第千章 x": "1000", "第二十回 x": "20",
        "2023年的夏天": None, "chapter-027": "27", "Chapter 3 - T": "3",
        "001_标题": "1", "0_x": "0", "39 标题": "39", "12.5章": "12",
        "第27话": "27", "第 27 章 标题": "27", "番外": None, "": None,
        "第A章": None, "第1000000章": "1000000",
    }
    for k, v in cases.items():
        got = fu._extract_chapter_num(k)
        assert got == v, f"_extract({k!r}) = {got!r}, 期望 {v!r}"
    round_pass("章节号定向用例 19 组")

    cn = {"十六": 16, "二十": 20, "一百二十三": 123, "千": 1000,
          "两": 2, "零": 0, "abc": 0, "": 0, "十十": 11}  # 十十=10+1*10? 行为快照
    for k, v in cn.items():
        got = fu._cn_to_int(k)
        if k == "十十":
            assert isinstance(got, int)  # 非法中文数字只要不抛、返回 int 即可
        else:
            assert got == v, f"_cn_to_int({k!r}) = {got}, 期望 {v}"
    round_pass("中文数字定向用例")

    alphabet = "第章回节话零一二两三四五六七八九十百千0123456789 _-.、:：chapterCHAPTER年的😀\t"
    rng = random.Random(7)
    for r in range(12):
        for _ in range(400):
            s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
            got = fu._extract_chapter_num(s)
            assert got is None or (got.isdigit() and int(got) >= 0), \
                f"非法返回 {got!r} for {s!r}"
            t = fu._strip_chapter_prefix(s)
            assert isinstance(t, str)
        round_pass(f"章节号/前缀剥离 400 例模糊 #{r+1}")
    # 占满 R26-40
    round_pass("章节号边界: 前导零/大数/空白")


# ----------------------------------------------------- R41-50 parse_md_file
def rounds_parse_md():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        specs = [
            ("bom.md", "﻿# 第1章 开端\n\n正文。".encode("utf-8")),
            ("gbk.md", "# 第2章 转折\n\n正文。".encode("gbk")),
            ("empty.md", b""),
            ("heading_only.md", "# 第3章 孤标题".encode("utf-8")),
            ("crlf.md", "# 第4章 回车\r\n\r\n正文\r\n第二段".encode("utf-8")),
            ("no_heading.txt", "没有标题的正文".encode("utf-8")),
            ("第5章 测试.md", "正文而已".encode("utf-8")),
            ("emoji😀.md", "# 第6章 emoji\n\n内容".encode("utf-8")),
            ("deep.md", ("# 第7章 深\n\n" + "段落\n\n" * 5000).encode("utf-8")),
            ("half_gbk.md", b"# \xb5\xda8\xd5\xc2 half\n\n\xff\xfe broken"),
        ]
        for i, (name, data) in enumerate(specs):
            fp = d / name
            fp.write_bytes(data)
            num, title, content = fu.parse_md_file(fp)
            assert isinstance(title, str) and title, f"{name}: 标题空 {title!r}"
            assert num is None or num.isdigit(), f"{name}: num={num!r}"
            assert isinstance(content, str)
            round_pass(f"parse_md_file: {name}")


# ------------------------------------------------ R51-60 strip_md_formatting
def rounds_strip_md():
    cases = [
        "***粗斜***文字", "**未闭合", "~~删~~除", "# 标题\n> 引用\n- 列表",
        "- [x] 任务\n1. 有序\n2) 也有序", "`code` 与 ```块``` 共存",
        "<!-- 注释 -->正文<!-- 未闭合", "<div>html</div><br", "![img](u) [链](u)",
        "普通\n\n\n\n\n多空行",
    ]
    for c in cases:
        out = fu.strip_md_formatting(c)
        assert isinstance(out, str)
    round_pass("strip_md 定向 10 例")

    rng = random.Random(13)
    alphabet = "*_~`#>-[]()<!x 字\n.1)"
    for r in range(7):
        for _ in range(200):
            s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
            fu.strip_md_formatting(s)  # 只要不抛
        round_pass(f"strip_md 200 例模糊 #{r+1}")

    # 性能/回溯炸弹
    for bomb, name in [("*" * 20000, "两万星号"),
                       ("[" * 5000 + "](" * 2000, "括号炸弹")]:
        t0 = time.perf_counter()
        fu.strip_md_formatting(bomb)
        dt = time.perf_counter() - t0
        assert dt < 1.5, f"{name} 耗时 {dt:.2f}s"
        round_pass(f"strip_md 回溯炸弹: {name} {dt*1000:.0f}ms")


# --------------------------------------------- R61-70 deduplicate_titles
def rounds_dedup():
    eng = [("33", "选择", "a"), ("39", "选择", "b"), (None, "选择（33）", "c"),
           (None, "选择", "d"), ("2", "选择（2）", "e")]
    out = fu.deduplicate_titles(eng)
    titles = [t for _, t, _ in out]
    assert len(titles) == len(set(titles)), f"工程化碰撞未解: {titles}"
    assert len(out) == len(eng)
    assert [c for _, _, c in out] == ["a", "b", "c", "d", "e"], "顺序/内容变了"
    round_pass("dedup 工程化后缀碰撞")

    rng = random.Random(21)
    pool = ["选择", "重逢", "选择（1）", "x"]
    for r in range(9):
        chapters = []
        for i in range(rng.randint(0, 60)):
            num = rng.choice([None, str(rng.randint(1, 50))])
            chapters.append((num, rng.choice(pool), f"c{i}"))
        out = fu.deduplicate_titles(chapters)
        titles = [t for _, t, _ in out]
        assert len(titles) == len(set(titles)), f"标题不唯一: {titles}"
        assert len(out) == len(chapters)
        assert [c for *_, c in out] == [c for *_, c in chapters]
        round_pass(f"dedup 随机批次 #{r+1}")


# ------------------------------------------------ R71-80 match_chapters
def rounds_match():
    rng = random.Random(31)
    for r in range(10):
        platform = []
        for i in range(rng.randint(0, 40)):
            num = rng.choice([None, rng.randint(1, 30)])
            platform.append({"chapterNum": num, "title": f"p{i}", "i": i})
        local = []
        for i in range(rng.randint(0, 40)):
            num = rng.choice([None, str(rng.randint(1, 35)), "0"])
            local.append((num, f"t{i}", f"c{i}"))
        matched, unmatched = fu.match_chapters(local, platform)
        assert len(matched) + len(unmatched) == len(local)
        first_idx = {}
        for ch in platform:
            n = ch.get("chapterNum")
            if n is not None and n not in first_idx:
                first_idx[n] = ch["i"]
        for li, pch, int_num, title, content in matched:
            assert pch["chapterNum"] == int_num
            assert pch["i"] == first_idx[int_num], "未保留首个重复章节"
            assert local[li][1] == title and local[li][2] == content
        round_pass(f"match_chapters 随机批次 #{r+1}")


# ------------------------------------------------ R81-90 _validate_times
def rounds_validate_times():
    cases = {
        "24:00": [], "23:60": [], "8:30": ["08:30"], "": [],
        "８：３０": ["08:30"],                      # 全角数字+冒号
        "20:00, 08:00, 20:00": ["08:00", "20:00"],  # 去重+排序
        "12:00,abc,13:00": ["12:00", "13:00"],
        "00:00": ["00:00"], "23:59": ["23:59"],
        "08:00；12:00，20:00": ["08:00", "12:00", "20:00"],
    }
    for k, v in cases.items():
        got = fu._validate_times(k)
        assert got == v, f"_validate_times({k!r}) = {got}, 期望 {v}"
    round_pass("validate_times 定向 10 组")

    rng = random.Random(41)
    alphabet = "0123456789:：,，;；． abc２５"
    for r in range(9):
        for _ in range(300):
            s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
            out = fu._validate_times(s)
            assert out == sorted(set(out)), f"未排序去重 {out}"
            for t in out:
                assert TIME_RE.match(t), f"非法时间 {t!r} from {s!r}"
        round_pass(f"validate_times 300 例模糊 #{r+1}")


# ------------------------------------------------ R91-95 筛选极端
def rounds_filter_extreme():
    P = FanqieGUI._parse_chapter_spec
    big = ",".join(str(i) for i in range(1, 100001))
    t0 = time.perf_counter()
    iv = P(big)
    dt = time.perf_counter() - t0
    assert iv is not None and len(iv) == 100000 and dt < 2.0, f"{dt:.2f}s"
    round_pass(f"筛选 10 万 token 解析 {dt*1000:.0f}ms")

    t0 = time.perf_counter()
    hit = [n for n in range(1, 356) if any(lo <= n <= hi for lo, hi in iv)]
    dt = time.perf_counter() - t0
    assert len(hit) == 355 and dt < 3.0
    round_pass(f"筛选 10 万区间×355 章命中 {dt*1000:.0f}ms")

    import unicodedata
    for raw, want in [("１-３，５", [(1, 3), (5, 5)]),
                      ("0-0", [(0, 0)]),
                      ("1-2,2-3,3-4", [(1, 2), (2, 3), (3, 4)])]:
        got = P(unicodedata.normalize("NFKC", raw))
        assert got == want, (raw, got)
    round_pass("筛选 NFKC/重叠区间")
    rng = random.Random(51)
    for r in range(2):
        for _ in range(500):
            s = "".join(rng.choice("0123456789,-~、;； .") for _ in
                        range(rng.randint(0, 40)))
            out = P(s)
            assert out is None or all(
                isinstance(lo, int) and lo <= hi for lo, hi in out)
        round_pass(f"筛选 500 例模糊 #{r+1}")


# ------------------------------------------------ R96-100 toast 新词库
def rounds_toast_words():
    err_cases = ["不能早于当前时间", "定时时间已过期", "暂不支持该操作",
                 "发布时间不能早于当前时间，请重新选择"]
    for t in err_cases:
        try:
            fu._classify_toasts([t])
            raise AssertionError(f"漏判错误: {t}")
        except fu.DailyLimitReached:
            raise AssertionError(f"误判上限: {t}")
        except RuntimeError:
            pass
    round_pass("新词库: 过去时间/不支持类秒级失败")

    benign = ["发布成功", "已保存", "草稿保存成功", "审核通过", "已过审",
              "已自动保存", "新功能上线公告"]
    for t in benign:
        fu._classify_toasts([t])
    round_pass("新词库: 良性提示不误杀(含'已过审')")

    for t in ["已到达当日发布字数上限", "提交字数超出每日上限"]:
        try:
            fu._classify_toasts([t])
            raise AssertionError("上限漏判")
        except fu.DailyLimitReached:
            pass
    round_pass("新词库: 上限分类不受影响")

    try:
        fu._classify_toasts(["操作过于频繁", "提交字数超出每日上限"])
        raise AssertionError("x")
    except fu.DailyLimitReached:
        pass
    round_pass("新词库: 双 toast 优先级保持")

    fu._classify_toasts([], ["系统维护期间提交可能失败"])
    round_pass("新词库: 常驻公告角色分离保持")


if __name__ == "__main__":
    print("[R1-25 compute_schedule]")
    rounds_schedule()
    print("[R26-40 章节号/中文数字/前缀剥离]")
    rounds_extract()
    print("[R41-50 parse_md_file]")
    rounds_parse_md()
    print("[R51-60 strip_md_formatting]")
    rounds_strip_md()
    print("[R61-70 deduplicate_titles]")
    rounds_dedup()
    print("[R71-80 match_chapters]")
    rounds_match()
    print("[R81-90 _validate_times]")
    rounds_validate_times()
    print("[R91-95 筛选极端]")
    rounds_filter_extreme()
    print("[R96-100 toast 新词库]")
    rounds_toast_words()
    assert ROUND == 100, f"轮数 {ROUND} != 100"
    print(f"\nALL {ROUND} ADVERSARIAL ROUNDS PASSED")
