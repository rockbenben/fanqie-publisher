# -*- coding: utf-8 -*-
"""章节筛选对抗性测试（零依赖，不开 Tk 窗口）。

攻击真实代码: FanqieGUI._parse_chapter_spec + _filter_by_chapter_num
运行:  python tests/test_chapter_filter.py
"""
import random
import sys
import time
import unicodedata
from pathlib import Path

# Windows GBK 控制台无法打印 emoji 等字符——测试输出统一 UTF-8 并容错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fanqie_gui import FanqieGUI  # noqa: E402
import tkinter as tk  # noqa: E402  只用 TclError 类，不创建窗口

P = FanqieGUI._parse_chapter_spec
PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL [{name}] {detail}"
    PASS += 1
    print(f"  PASS {name}")


def nfkc(s):
    return unicodedata.normalize("NFKC", s).strip()


# ---------------------------------------------------------------- 假 GUI 载体
class FakeWidget:
    def __init__(self):
        self.kw = {}

    def configure(self, **kw):
        self.kw.update(kw)


class FakeVar:
    def __init__(self, v):
        self.v = v

    def get(self):
        if isinstance(self.v, Exception):
            raise self.v
        return self.v


class FakeSelf:
    def __init__(self, enabled=True, text="", op="≤"):
        self.cmb_resched_filter_op = FakeWidget()
        self.lbl_resched_filter_info = FakeWidget()
        self.resched_filter_var = FakeVar(enabled)
        self.resched_filter_num_var = FakeVar(text)
        self.resched_filter_op_var = FakeVar(op)

    _parse_chapter_spec = staticmethod(FanqieGUI._parse_chapter_spec)


def run_filter(text, items, key, enabled=True, op="≤"):
    fake = FakeSelf(enabled, text, op)
    kept, active = FanqieGUI._filter_by_chapter_num(fake, items, key)
    return kept, active, fake


def attacks():
    print("[R1 解析器恶意输入]")
    cases_none = ["abc", "1,abc", "5-", "-5", "--", "5--10", "1,2,-",
                  "5–10",  # en-dash 不是合法分隔
                  "1.5", "1,2.5", "😀", "1,😀", "\n", "1-2-3", "1,,-",
                  "999999999999999999999999-x"]
    for c in cases_none:
        r = P(nfkc(c))
        check(f"R1 非法→None/空: {c!r:>12.12}", not r, f"got {r}")
    check("R1 大数不溢出", P("1-999999999999999999999") ==
          [(1, 999999999999999999999)])
    check("R1 0 与 0-0", P("0,0-0") == [(0, 0), (0, 0)])
    check("R1 重复/重叠区间保持原样", P("3,3,1-10,5-8") ==
          [(3, 3), (3, 3), (1, 10), (5, 8)])

    print("[R2 解析器 2000 轮随机模糊：永不抛异常]")
    alphabet = "0123456789,-~、;； ，．.５－１０×abc😀\t\n"
    rng = random.Random(42)
    for i in range(2000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        try:
            r = P(nfkc(s))
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"R2 fuzz#{i} 抛异常: {s!r} -> {e}")
        if r:
            for lo, hi in r:
                assert isinstance(lo, int) and isinstance(hi, int) and lo <= hi, \
                    f"R2 fuzz#{i} 非法区间 {r} from {s!r}"
    check("R2 2000 轮模糊", True)

    print("[R3 Unicode 链路]")
    check("R3 全角混合", P(nfkc("５－１０，３；７")) == [(5, 10), (3, 3), (7, 7)])
    check("R3 全角波浪线", P(nfkc("５～８")) == [(5, 8)])
    check("R3 上标² 经NFKC", P(nfkc("²")) == [(2, 2)])  # NFKC: ²→2
    check("R3 阿拉伯-印度数字", P("٥-١٠") == [(5, 10)])  # \d/int 均支持 Unicode 数字
    check("R3 罗马数字Ⅻ拒绝", P(nfkc("Ⅻ")) is None or P(nfkc("Ⅻ")) == [(12, 12)])
    # NFKC 把 Ⅻ 转成 "XII"（字母）→ None；两种实现都安全，只要不抛错

    print("[R4 筛选分支与 UI 状态转换（假 self 驱动真实代码）]")
    items = list(range(1, 356))
    k = lambda x: x  # noqa: E731

    kept, active, f = run_filter("30", items, k, op="≤")
    check("R4 阈值≤30", active and kept == list(range(1, 31))
          and f.cmb_resched_filter_op.kw.get("state") == "readonly")
    kept, active, f = run_filter("350", items, k, op="≥")
    check("R4 阈值≥350", active and kept == [350, 351, 352, 353, 354, 355])
    kept, active, f = run_filter("1,3,5-10", items, k)
    check("R4 组合命中+禁用运算符",
          active and kept == [1, 3, 5, 6, 7, 8, 9, 10]
          and f.cmb_resched_filter_op.kw.get("state") == "disabled")
    kept, active, f = run_filter("abc", items, k)
    check("R4 非法→全量+红字+不激活",
          not active and kept == items
          and f.lbl_resched_filter_info.kw.get("foreground") == "red")
    kept, active, f = run_filter(",", items, k)
    check("R4 单独逗号→全量+红字", not active and kept == items
          and f.lbl_resched_filter_info.kw.get("foreground") == "red")
    kept, active, f = run_filter("1,3", items, k, enabled=False)
    check("R4 未勾选→直通+恢复readonly", not active and kept == items
          and f.cmb_resched_filter_op.kw.get("state") == "readonly")
    kept, active, f = run_filter("", items, k)
    check("R4 勾选留空→直通", not active and kept == items)
    fake = FakeSelf(True, tk.TclError("var destroyed"))
    kept, active = FanqieGUI._filter_by_chapter_num(fake, items, k)
    check("R4 TclError→直通不崩", not active and kept == items)

    print("[R5 语义陷阱: 尾随逗号改变语义但有 UI 信号]")
    kept, active, f = run_filter("30,", items, k, op="≤")
    check("R5 '30,'=精确命中30 且运算符禁用",
          active and kept == [30]
          and f.cmb_resched_filter_op.kw.get("state") == "disabled")

    print("[R6 性能: 万级 token × 355 章]")
    big = ",".join(str(i) for i in range(1, 10001))
    t0 = time.perf_counter()
    kept, active, f = run_filter(big, items, k)
    dt = time.perf_counter() - t0
    check("R6 万token正确且<200ms", active and len(kept) == 355 and dt < 0.2,
          f"dt={dt*1000:.1f}ms kept={len(kept)}")

    print("[R7 空结果与 None/str key]")
    # 注意: 纯数字 "9999" 是阈值模式(≤9999=全保留)，空结果要用集合/≥写法
    kept, active, f = run_filter("9999", items, k)
    check("R7 纯数字走阈值模式(≤9999=全量)", active and kept == items)
    kept, active, f = run_filter("9999-9999", items, k)
    check("R7 集合空结果合法", active and kept == [])
    kept, active, f = run_filter("9999", items, k, op="≥")
    check("R7 阈值空结果合法", active and kept == [])
    mixed = [("1", "a"), (None, "b"), ("12", "c"), (7, "d")]
    kept, active, f = run_filter("1,7,12", mixed, lambda x: x[0])
    check("R7 str/int/None key 混合", active and kept ==
          [("1", "a"), ("12", "c"), (7, "d")])
    kept, active, f = run_filter("5", mixed, lambda x: x[0], op="≤")
    check("R7 阈值模式 str key", active and kept == [("1", "a")])

    print("[R8 组合边界]")
    kept, active, f = run_filter("355-9999", items, k)
    check("R8 上越界区间", active and kept == [355])
    kept, active, f = run_filter("0-1", items, k)
    check("R8 下越界区间", active and kept == [1])
    kept, active, f = run_filter("10-5,300", items, k)
    check("R8 倒序范围+单号", active and kept == [5, 6, 7, 8, 9, 10, 300])


def compress_roundtrip():
    """失败章节号压缩表达式：正确性 + 与筛选解析器的往返一致性。"""
    print("[R9 失败章节号压缩与往返]")
    import fanqie_upload as fu

    C = fu._compress_chapter_nums
    check("R9 连续段压缩", C([79, 80, 81, 83, 84, 114]) == "79-81,83-84,114")
    check("R9 单元素", C([7]) == "7")
    check("R9 乱序去重", C([3, 1, 2, 2, 10]) == "1-3,10")
    check("R9 空集", C([]) == "")

    # 往返性质: parse(compress(S)) 命中的集合 == S
    rng = random.Random(77)
    for i in range(300):
        s = {rng.randint(1, 400) for _ in range(rng.randint(1, 60))}
        expr = C(s)
        intervals = FanqieGUI._parse_chapter_spec(expr)
        assert intervals, f"#{i} 压缩结果不可解析: {expr!r}"
        hit = {n for n in range(1, 401)
               if any(lo <= n <= hi for lo, hi in intervals)}
        assert hit == s, f"#{i} 往返不一致: {expr!r}"
    check("R9 300 轮随机往返一致", True)


if __name__ == "__main__":
    attacks()
    compress_roundtrip()
    print(f"\nALL ADVERSARIAL CHECKS PASSED ({PASS} 断言 + 2300 轮模糊)")
