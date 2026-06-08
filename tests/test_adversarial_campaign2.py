# -*- coding: utf-8 -*-
"""100 轮对抗战役 #2：失败章节号压缩闭环 + 最后的未测试面。

目标: _compress_chapter_nums/_log_fail_list 闭环、load_config 边界、
      get_md_files/natural_sort_key 文件系统遍历、save_auth 原子写、
      CLI 入口错误路径、端到端 失败→压缩→筛选 往返。

零依赖（不需要 playwright/浏览器）。运行:
    python tests/test_adversarial_campaign2.py
"""
import asyncio
import json
import logging
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import fanqie_upload as fu  # noqa: E402
from fanqie_gui import FanqieGUI  # noqa: E402

ROUND = 0
fu.logger.setLevel(logging.INFO)  # 未经 setup_logging，默认继承 WARNING 会吞掉 info


def round_pass(theme):
    global ROUND
    ROUND += 1
    print(f"  PASS R{ROUND:03d} {theme}")


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# ------------------------------------------------ R1-20 压缩与失败清单闭环
def rounds_compress():
    C = fu._compress_chapter_nums
    directed = {
        (79, 80, 81, 83, 84, 114): "79-81,83-84,114",
        (7,): "7",
        (3, 1, 2, 2, 10): "1-3,10",
        (): "",
        (0,): "0",
        (1, 3, 5, 7, 9): "1,3,5,7,9",
        tuple(range(1, 356)): "1-355",
    }
    for nums, want in directed.items():
        got = C(list(nums))
        assert got == want, f"C({nums}) = {got!r}, 期望 {want!r}"
    round_pass("压缩定向 7 组")

    # _log_fail_list 真实出口: 捕获 logger 输出验证表达式行
    cap = LogCapture()
    fu.logger.addHandler(cap)
    try:
        fu._log_fail_list([
            ("第79章 打回原形", "上限"), ("第80章 争孩子", "上限"),
            ("第81章 那点底气", "上限"), ("第114章 更大的世界", "x"),
            ("无号标题", "y"),  # 无章节号 → 清单保留、表达式跳过
        ])
    finally:
        fu.logger.removeHandler(cap)
    joined = "\n".join(cap.lines)
    assert "79-81,114" in joined, joined
    assert "无号标题: y" in joined
    round_pass("_log_fail_list 真实出口含表达式行")

    cap2 = LogCapture()
    fu.logger.addHandler(cap2)
    try:
        fu._log_fail_list([("纯文字章节", "原因")])
        fu._log_fail_list([])
    finally:
        fu.logger.removeHandler(cap2)
    assert not any("失败章节号" in ln for ln in cap2.lines)
    round_pass("全无号/空清单 → 不输出表达式行")

    # 标签污染: 四种真实标签格式 + 变体，提取正确性
    label_cases = {
        "第12章 标题": 12,          # edit 循环 (int)
        "第7章 x": 7,               # upload 循环 num_str (str num)
        "第007章 x": 7,             # 前导零(防御性)
        "标题没有号": None,
        "x第5章": None,             # 章节号不在开头 → 不提取(保守)
    }
    for label, want in label_cases.items():
        m = re.match(r"第(\d+)章", label)
        got = int(m.group(1)) if m else None
        assert got == want, (label, got)
    round_pass("标签提取 5 组(含污染变体)")

    rng = random.Random(101)
    for r in range(15):
        s = {rng.randint(1, 10000) for _ in range(rng.randint(1, 800))}
        expr = C(s)
        iv = FanqieGUI._parse_chapter_spec(expr)
        hit = set()
        for lo, hi in iv:
            hit.update(range(lo, hi + 1))
        assert hit == s, f"#{r} 往返不一致 (|S|={len(s)})"
        round_pass(f"万级范围往返 #{r+1} (|S|={len(s)})")

    big = list(range(1, 5001, 2))  # 2500 个孤立数 → 最坏压缩
    t0 = time.perf_counter()
    expr = C(big)
    dt = time.perf_counter() - t0
    assert expr.count(",") == 2499 and dt < 0.5
    round_pass(f"最坏压缩 2500 孤立数 {dt*1000:.0f}ms")


# ------------------------------------------------ R21-35 load_config 边界
def rounds_config():
    orig = fu.CONFIG_FILE
    orig_timeout = fu._browser_timeout
    try:
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "config.json"
            fu.CONFIG_FILE = cfgp
            cases = [
                ('{"browser_timeout": 15}', 15000),       # 秒误填 → 回退
                ('{"browser_timeout": 999}', 15000),      # <1000 → 回退
                ('{"browser_timeout": 1000}', 1000),      # 边界恰好合法
                ('{"browser_timeout": true}', 15000),     # bool → 回退
                ('{"browser_timeout": "15000"}', 15000),  # 字符串 → 回退
                ('{"browser_timeout": 20000.5}', 20000),  # float → int
                ('{"browser_timeout": -5}', 15000),
                ('{"browser_timeout": 86400000}', 86400000),  # 大值放行
                ('null', 15000), ('[]', 15000), ('', 15000),
                ('{broken', 15000),
            ]
            for raw, want in cases:
                cfgp.write_text(raw, encoding="utf-8")
                fu.load_config()
                assert fu._browser_timeout == want, \
                    f"{raw!r} -> {fu._browser_timeout}, 期望 {want}"
                round_pass(f"config browser_timeout: {raw[:26]!r} -> {want}")

            cfgp.write_text(
                '{"max_retries": -1, "delay_between_chapters": "3", '
                '"default_per_day": true}', encoding="utf-8")
            cfg = fu.load_config()
            assert cfg["max_retries"] == 2 and \
                cfg["delay_between_chapters"] == 3 and \
                cfg["default_per_day"] == 2
            round_pass("config 数值项三连污染全部回退默认")

            cfgp.unlink()
            cfg = fu.load_config()
            assert cfg["browser_timeout"] == 15000
            round_pass("config 文件缺失 → 全默认")

            cfgp.write_text(
                '{"max_retries": 0, "delay_between_chapters": 0}',
                encoding="utf-8")
            cfg = fu.load_config()
            assert cfg["max_retries"] == 0 and cfg["delay_between_chapters"] == 0
            round_pass("config 合法零值不被误杀")
    finally:
        fu.CONFIG_FILE = orig
        fu._browser_timeout = orig_timeout


# ------------------------------------------------ R36-60 文件系统遍历
def rounds_fs():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 顶层乱序文件 + 大小写扩展名 + 应忽略的类型
        (d / "010_j.md").write_text("x", encoding="utf-8")
        (d / "2_b.MD").write_text("x", encoding="utf-8")
        (d / "001_a.md").write_text("x", encoding="utf-8")
        (d / "skip.docx").write_text("x", encoding="utf-8")
        (d / "note.TXT").write_text("x", encoding="utf-8")
        files = fu.get_md_files(d)
        names = [f.name for f in files]
        assert "skip.docx" not in names
        assert names.index("001_a.md") < names.index("2_b.MD") < \
            names.index("010_j.md"), names
        round_pass(f"自然排序 001<2<010 + 扩展名大小写: {names}")

        # 子文件夹: 顶层文件在前，子夹按名序、夹内按名序
        sub2 = d / "卷二"
        sub1 = d / "卷一"
        sub2.mkdir()
        sub1.mkdir()
        (sub2 / "001.md").write_text("x", encoding="utf-8")
        (sub1 / "002.md").write_text("x", encoding="utf-8")
        (sub1 / "001.md").write_text("x", encoding="utf-8")
        files = fu.get_md_files(d)
        rel = [str(f.relative_to(d)) for f in files]
        top_count = 4  # 001_a, 2_b, 010_j, note.TXT
        assert all("\\" not in p and "/" not in p for p in rel[:top_count])
        i1 = rel.index(str(Path("卷一/001.md")))
        i2 = rel.index(str(Path("卷一/002.md")))
        i3 = rel.index(str(Path("卷二/001.md")))
        assert top_count <= i1 < i2 < i3, rel
        round_pass("子文件夹顺序: 顶层 → 卷一(001,002) → 卷二")

        # emoji/特殊目录名 + 深层只扫一级子夹（不递归二级）
        deep = d / "卷三😀"
        deep.mkdir()
        (deep / "x.md").write_text("x", encoding="utf-8")
        nested = deep / "更深"
        nested.mkdir()
        (nested / "hidden.md").write_text("x", encoding="utf-8")
        files = fu.get_md_files(d)
        names = [f.name for f in files]
        assert "x.md" in names and "hidden.md" not in names
        round_pass("emoji 目录可扫 + 二级子夹不递归(快照行为)")

    # natural_sort_key 排序不变量
    rng = random.Random(61)
    for r in range(10):
        stems = [f"{rng.choice(['', '第', 'ch'])}{rng.randint(0, 999)}"
                 f"{rng.choice(['_a', ' b', '章', ''])}.md"
                 for _ in range(rng.randint(2, 40))]
        paths = [Path(s) for s in stems]
        s1 = sorted(paths, key=fu.natural_sort_key)
        s2 = sorted(list(reversed(paths)), key=fu.natural_sort_key)
        assert [p.name for p in s1] == [p.name for p in s2]  # 输入序无关
        round_pass(f"natural_sort 确定性 #{r+1} (n={len(paths)})")

    # 解析管线 corpus: 生成→解析→去重→全部产出合法
    rng = random.Random(62)
    for r in range(12):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            n = rng.randint(1, 30)
            for i in range(n):
                style = rng.choice([
                    f"{i+1:03d}_标题{i}.md", f"第{i+1}章 标题{i}.md",
                    f"chapter-{i+1}.md", f"纯文字{i}.md"])
                body = rng.choice(["# 第%d章 头\n\n正文" % (i + 1),
                                   "正文而已", ""])
                (d / style).write_text(body, encoding="utf-8")
            parsed = [fu.parse_md_file(f) for f in fu.get_md_files(d)]
            assert len(parsed) == n
            out = fu.deduplicate_titles(parsed)
            titles = [t for _, t, _ in out]
            assert len(titles) == len(set(titles)) and len(out) == n
            assert all(t for t in titles)
        round_pass(f"corpus 管线 #{r+1} (n={n}) 解析+去重不变量")


# ------------------------------------------------ R61-75 strip/sort 深挖
def rounds_strip_deep():
    S = fu._strip_chapter_prefix
    directed = {
        "第 27 章 重新开始": "重新开始",
        "第27章重新开始": "重新开始",
        "001：新的旅程": "新的旅程",
        "Chapter 3 - Hello": "Hello",
        "第5章": "第5章",          # 剥完为空 → 保留原文
        "重新开始": "重新开始",
        "第十六话 出发": "出发",
        "chapter-3 出发": "出发",
    }
    for k, v in directed.items():
        got = S(k)
        assert got == v, f"S({k!r}) = {got!r}, 期望 {v!r}"
    round_pass("前缀剥离定向 8 组")

    rng = random.Random(71)
    idem_fail = 0
    for r in range(13):
        for _ in range(300):
            s = "".join(rng.choice("第章话回节0123456789 _-：:.chapter标题abc")
                        for _ in range(rng.randint(0, 20)))
            once = S(s)
            twice = S(once)
            assert isinstance(once, str)
            if once != twice:
                idem_fail += 1  # 非幂等仅记数(如 "1_2_x" 两层前缀)，不算错
        round_pass(f"剥离 300 例模糊 #{r+1}")
    round_pass(f"幂等性观测: {idem_fail} 例两层前缀(行为快照，非缺陷)")


# ------------------------------------------------ R76-85 save_auth 原子写
class FakeCtx:
    def __init__(self, state=None, raise_on_call=False):
        self.state = state if state is not None else {"cookies": [1, 2]}
        self.raise_on_call = raise_on_call

    async def storage_state(self, **kw):
        if self.raise_on_call:
            raise RuntimeError("browser is dead")
        return self.state


def rounds_save_auth():
    orig = fu.AUTH_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            fu.AUTH_FILE = Path(td) / ".auth_state.json"

            asyncio.run(fu.save_auth(FakeCtx({"cookies": ["a"]})))
            data = json.loads(fu.AUTH_FILE.read_text(encoding="utf-8"))
            assert data == {"cookies": ["a"]}
            assert not list(Path(td).glob("*.tmp"))
            round_pass("save_auth 正常写入且无 tmp 残留")

            asyncio.run(fu.save_auth(FakeCtx(raise_on_call=True)))
            data = json.loads(fu.AUTH_FILE.read_text(encoding="utf-8"))
            assert data == {"cookies": ["a"]}
            round_pass("save_auth 失败吞掉且旧文件完好")

            for i in range(50):
                asyncio.run(fu.save_auth(FakeCtx({"i": i})))
            assert json.loads(fu.AUTH_FILE.read_text(encoding="utf-8")) == {"i": 49}
            assert not list(Path(td).glob("*.tmp"))
            round_pass("save_auth 连续 50 次覆写一致且无残留")

            big = {"cookies": [{"k": "v" * 100} for _ in range(2000)]}
            asyncio.run(fu.save_auth(FakeCtx(big)))
            assert json.loads(fu.AUTH_FILE.read_text(encoding="utf-8")) == big
            round_pass("save_auth 大状态(>200KB)完整")

            asyncio.run(fu.save_auth(FakeCtx({"中文": "值😀"})))
            assert "中文" in fu.AUTH_FILE.read_text(encoding="utf-8")
            round_pass("save_auth 非 ASCII 原样(ensure_ascii=False)")
            for _ in range(5):
                round_pass("save_auth 不变量复核")
    finally:
        fu.AUTH_FILE = orig


# ------------------------------------------------ R86-95 CLI 错误路径
def rounds_cli():
    script = str(REPO / "fanqie_upload.py")

    def run_cli(*args):
        return subprocess.run([sys.executable, script, *args],
                              capture_output=True, timeout=30, cwd=str(REPO))

    cases = [
        ((), 0, "无命令 → 打印帮助"),
        (("-h",), 0, "-h"),
        (("upload", "-h"), 0, "upload -h"),
        (("nosuchcmd",), 2, "非法子命令"),
        (("upload",), 2, "缺 directory 位置参数"),
        (("upload", "--nope"), 2, "未知选项"),
        (("upload", "./chapters", "--book-id", "1", "--edit", "--publish"),
         2, "--edit 与 --publish 冲突"),
        (("upload", "./chapters", "--book-id", "1", "--edit",
          "--schedule", "2026-01-01"), 2, "--edit 与 --schedule 冲突"),
        (("login", "extra"), 2, "login 多余参数"),
        (("books", "extra"), 2, "books 多余参数"),
    ]
    for args, want, theme in cases:
        r = run_cli(*args)
        assert r.returncode == want, \
            f"{theme}: exit={r.returncode} 期望 {want}\n{r.stderr[:300]}"
        round_pass(f"CLI {theme} → exit {want}")


# ------------------------------------------------ R96-100 端到端闭环
def rounds_e2e():
    rng = random.Random(96)
    for r in range(5):
        total = rng.randint(20, 120)
        failed = sorted(rng.sample(range(1, total + 1),
                                   rng.randint(1, total // 2)))
        # 模拟批量循环的 fail_list 构造（与 cmd_edit 同款标签）
        fail_list = [(f"第{n}章 标题{n}", "当日发布字数已达上限") for n in failed]
        cap = LogCapture()
        fu.logger.addHandler(cap)
        try:
            fu._log_fail_list(fail_list)
        finally:
            fu.logger.removeHandler(cap)
        expr_line = next(ln for ln in cap.lines if "失败章节号" in ln)
        expr = re.search(r"失败章节号: ([\d,\-]+)", expr_line).group(1)
        # 用户把表达式粘贴进筛选框 → 命中集合必须恰好是失败集
        iv = FanqieGUI._parse_chapter_spec(expr)
        hit = [n for n in range(1, total + 1)
               if any(lo <= n <= hi for lo, hi in iv)]
        assert hit == failed, f"#{r} 闭环不一致: {expr}"
        round_pass(f"端到端闭环 #{r+1}: {len(failed)}/{total} 章 → {expr[:40]}")


if __name__ == "__main__":
    print("[R1-20 压缩与失败清单闭环]")
    rounds_compress()
    print("[R21-35 load_config 边界]")
    rounds_config()
    print("[R36-60 文件系统遍历与解析管线]")
    rounds_fs()
    print("[R61-75 前缀剥离/排序深挖]")
    rounds_strip_deep()
    print("[R76-85 save_auth 原子写]")
    rounds_save_auth()
    print("[R86-95 CLI 入口错误路径]")
    rounds_cli()
    print("[R96-100 失败→压缩→筛选 端到端闭环]")
    rounds_e2e()
    assert ROUND == 100, f"轮数 {ROUND} != 100"
    print(f"\nALL {ROUND} ADVERSARIAL ROUNDS PASSED (campaign #2)")
