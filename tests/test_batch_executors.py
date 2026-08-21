# -*- coding: utf-8 -*-
"""批次执行器（run_creation_batch / run_edit_batch）行为测试。零依赖。

这两个循环就是当初 748 章漏 151 章的路径。合并前它们藏在 cmd_upload /
cmd_edit / 两个 GUI task 里，被 input() 和 playwright 挡着**从来没有被
直接测过**——策略只能靠 grep 源码标记间接保证。抽成独立函数后，第一次
可以拿假原语把每条中止路径真正跑一遍。

不变量（每个用例都断言）: success + failed + skipped == total，
且 fail_list 覆盖所有未成功章节——这正是历史上"汇总对不上、补传清单
缺章"那族 bug 的判据。

运行: python tests/test_batch_executors.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f"  <- {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILED.append(label)


class FakePage:
    def __init__(self):
        self.closed = False

    async def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return self.closed


class Stub:
    """把 fanqie_upload 的原语打成脚本化假件，记录调用。"""

    def __init__(self, publish_script=None, draft_script=None,
                 edit_script=None, confirm_ok=True, miss=None):
        self.publish_script = publish_script or []   # 每章: ("ok",)/("fail",msg)/("limit",msg)
        self.draft_script = draft_script or []       # 每章: (ok, draft_id, err) 或 ("limit",msg)
        self.edit_script = edit_script or []
        self.confirm_ok = confirm_ok
        self.miss = miss if miss is not None else []
        self.publish_calls = []
        self.edit_calls = []
        self.confirm_calls = []
        self.reconciled = None

    def install(self):
        self._saved = {n: getattr(fu, n) for n in (
            "publish_one_chapter", "draft_one_chapter", "edit_one_chapter",
            "confirm_chapter_on_platform", "reconcile_batch_auto",
            "watch_chapter_list_url")}

        async def publish(page, url, num, title, content, **kw):
            act = self.publish_script[len(self.publish_calls)]
            self.publish_calls.append(num)
            if act[0] == "limit":
                raise fu.DailyLimitReached(act[1])
            return (True, "") if act[0] == "ok" else (False, act[1])

        async def draft(page, url, num, title, content, **kw):
            act = self.draft_script[len(self.publish_calls)]
            self.publish_calls.append(num)
            if act[0] == "limit":
                raise fu.DailyLimitReached(act[1])
            return act

        async def edit(page, edit_url, num, title, content, **kw):
            act = self.edit_script[len(self.edit_calls)]
            self.edit_calls.append(num)
            if act[0] == "limit":
                raise fu.DailyLimitReached(act[1])
            if act[0] == "boom":
                raise RuntimeError(act[1])
            return (True, "") if act[0] == "ok" else (False, act[1])

        async def confirm(page, holder, num, **kw):
            self.confirm_calls.append(num)
            return self.confirm_ok

        async def reconcile(page, book_id, claimed, fail_list, *, is_draft):
            # 忠实于真实实现: missing = [n for n in claimed_nums if n not in present]
            # —— 只会报「还在对账名单里」的章。桩若无条件返回 miss，就测不出
            # 「已在循环里记过失败的章有没有被摘出名单」。
            self.reconciled = list(claimed)
            hit = [m for m in self.miss if m in claimed]
            for m in hit:
                fail_list.append((f"第{m}章 ?", "对账缺失"))
            return hit

        fu.publish_one_chapter = publish
        fu.draft_one_chapter = draft
        fu.edit_one_chapter = edit
        fu.confirm_chapter_on_platform = confirm
        fu.reconcile_batch_auto = reconcile
        fu.watch_chapter_list_url = lambda page: ({"url": "u"}, lambda: None)
        return self

    def restore(self):
        for n, v in self._saved.items():
            setattr(fu, n, v)


PARSED = [(1, "甲", "正文一"), (2, "乙", "正文二"), (3, "丙", "正文三")]


def run_creation(stub, *, is_draft=False, cancel=None, progress=None, parsed=PARSED):
    stub.install()
    try:
        return asyncio.run(fu.run_creation_batch(
            FakePage(), parsed, "u", book_id="B", is_draft=is_draft,
            delay=0, cancel_check=cancel, progress_cb=progress))
    finally:
        stub.restore()


def creation_invariant(label, s_, f_, fl, total):
    check(f"{label}: 成功+失败=总数", s_ + f_ == total, f"{s_}+{f_}!={total}")
    check(f"{label}: 清单覆盖全部未成功章", len(fl) == f_, f"清单{len(fl)}≠失败{f_}")


print("== run_creation_batch: 发布路径 ==")
st = Stub(publish_script=[("ok",)] * 3)
s_, f_, fl = run_creation(st)
check("全成功: success=3 failed=0", (s_, f_) == (3, 0), (s_, f_))
check("逐章确认每章都调了", st.confirm_calls == [1, 2, 3], st.confirm_calls)
check("记账章号交给对账", st.reconciled == [1, 2, 3], st.reconciled)
creation_invariant("全成功", s_, f_, fl, 3)

st = Stub(publish_script=[("ok",), ("fail", "超时")])
s_, f_, fl = run_creation(st)
check("第2章失败即停: 不再发第3章", st.publish_calls == [1, 2], st.publish_calls)
check("失败即停: success=1", s_ == 1, s_)
creation_invariant("失败即停", s_, f_, fl, 3)
check("剩余章记为前方中止", any("前方中止" in r for _, r in fl), fl)

st = Stub(publish_script=[("ok",)], confirm_ok=False)
s_, f_, fl = run_creation(st)
check("确认不到即中止: success=0", s_ == 0, s_)
check("原因=提交后平台查不到", any("查不到" in r for _, r in fl), fl)
creation_invariant("确认不到", s_, f_, fl, 3)

prog = []
st = Stub(publish_script=[("limit", "当日上限")])
s_, f_, fl = run_creation(st, progress=lambda d, t: prog.append((d, t)))
check("每日上限: 全部 3 章入清单", f_ == 3 and len(fl) == 3, (f_, fl))
check("每日上限: 进度收在 (3,3)", prog and prog[-1] == (3, 3), prog)
creation_invariant("每日上限", s_, f_, fl, 3)

print("== run_creation_batch: 草稿路径 ==")
st = Stub(draft_script=[(True, "d1", ""), (False, None, "存失败"), (True, "d3", "")])
s_, f_, fl = run_creation(st, is_draft=True)
check("草稿失败不中止: 三章都试了", len(st.publish_calls) == 3, st.publish_calls)
check("草稿: success=2 failed=1", (s_, f_) == (2, 1), (s_, f_))
check("草稿不逐章确认", st.confirm_calls == [], st.confirm_calls)
creation_invariant("草稿", s_, f_, fl, 3)

st = Stub(draft_script=[(True, "dX", ""), (True, "dX", ""), (True, "d3", "")])
s_, f_, fl = run_creation(st, is_draft=True)
check("槽位复用: 被覆盖章移出成功", (s_, f_) == (2, 1), (s_, f_))
check("槽位复用: 原因写明覆盖", any("覆盖" in r for _, r in fl), fl)
creation_invariant("槽位复用", s_, f_, fl, 3)

# 槽位复用 + 批末对账指向同一章: 曾经被计两次，success 能被减成负数
# （_upload_done 把 success<0 当运行异常，一个大体成功的批次会报「定时执行失败」）
st = Stub(draft_script=[(True, "dX", ""), (True, "dX", ""), (True, "d3", "")],
          miss=[1])   # 第1章被覆盖，草稿箱里也确实没有 —— 同一件事
s_, f_, fl = run_creation(st, is_draft=True)
check("覆盖+对账指向同一章: 不重复计数", (s_, f_) == (2, 1), (s_, f_))
check("覆盖章已从对账名单摘除", st.reconciled == [2, 3], st.reconciled)
check("success 不会变成负数", s_ >= 0, s_)
creation_invariant("覆盖+对账同章", s_, f_, fl, 3)

st = Stub(publish_script=[])
s_, f_, fl = run_creation(st, cancel=lambda: True)
check("取消: 一章都不发", st.publish_calls == [], st.publish_calls)
check("取消后仍走收尾对账", st.reconciled == [], st.reconciled)

st = Stub(publish_script=[("ok",)] * 3, miss=[2])
s_, f_, fl = run_creation(st)
check("对账缺 1 章: 成功回撤、失败+1", (s_, f_) == (2, 1), (s_, f_))
creation_invariant("对账缺章", s_, f_, fl, 3)

print("== run_edit_batch: 修改路径 ==")
MATCHED = [(0, {"status": "已发布", "editUrl": "/e1"}, 1, "甲", "一"),
           (1, {"status": "已发布", "editUrl": "/e2"}, 2, "乙", "二"),
           (2, {"status": "已发布", "editUrl": "/e3"}, 3, "丙", "三")]


def run_edit(stub, matched=MATCHED, cancel=None):
    stub.install()
    try:
        return asyncio.run(fu.run_edit_batch(
            FakePage(), matched, delay=0, cancel_check=cancel))
    finally:
        stub.restore()


st = Stub(edit_script=[("ok",)] * 3)
s_, f_, sk, fl = run_edit(st)
check("全成功: 3/0/0", (s_, f_, sk) == (3, 0, 0), (s_, f_, sk))

st = Stub(edit_script=[("ok",), ("fail", "标题重复"), ("ok",), ("ok",)])
s_, f_, sk, fl = run_edit(st)
check("重复标题: 批末二次尝试后成功", (s_, f_) == (3, 0), (s_, f_))
check("二次尝试确实多调了一次", len(st.edit_calls) == 4, st.edit_calls)

m2 = [(0, {"status": "审核中", "editUrl": "/e1"}, 1, "甲", "一")] + MATCHED[1:]
st = Stub(edit_script=[("ok",)] * 2)
s_, f_, sk, fl = run_edit(st, matched=m2)
check("审核中跳过: skipped=1", (s_, f_, sk) == (2, 0, 1), (s_, f_, sk))

MATCHED5 = [(i, {"status": "已发布", "editUrl": f"/e{i}"}, i + 1, f"章{i+1}", "x")
            for i in range(5)]
st = Stub(edit_script=[("fail", "x1"), ("fail", "x2"), ("fail", "x3")])
st.install()
try:
    s_, f_, sk, fl = asyncio.run(fu.run_edit_batch(FakePage(), MATCHED5, delay=0))
finally:
    st.restore()
check("连续3失败熔断: 前3章入账", f_ >= 3, (s_, f_, sk))
# 熔断后剩下的 2 章必须进补传清单，否则 log_fail_list 末尾那行压缩章节号
# 里没有它们，用户拿不到可直接粘贴续跑的清单
check("熔断后剩余章记入补传清单",
      any("流程异常中止" in r for _, r in fl), fl)
check("熔断: 成功+失败=总数（剩余不再只计 skipped）",
      s_ + f_ == 5, (s_, f_, sk))

st = Stub(edit_script=[("limit", "当日上限")])
s_, f_, sk, fl = run_edit(st)
check("每日上限: 本章+剩余全入清单", f_ == 3 and len(fl) == 3, (f_, fl))

# 二次尝试的页面死亡短路（原 CLI 独有，合并后 GUI 也有了）
# 主循环: 前两章"标题重复"进二次队列，第三章成功。
# 二次第 1 条: 抛异常且页面已关 -> dead；第 2 条必须走短路、不再调用 edit。
page = FakePage()
st = Stub(edit_script=[("fail", "标题重复"), ("fail", "标题重复"), ("ok",),
                       ("boom", "页面被关")])
st.install()
try:
    orig_edit2 = fu.edit_one_chapter
    n2 = {"c": 0}

    async def edit_dies_on_4th(pg, *a, **kw):
        n2["c"] += 1
        if n2["c"] == 4:
            page.closed = True   # 二次第 1 条：页面死亡
        return await orig_edit2(pg, *a, **kw)
    fu.edit_one_chapter = edit_dies_on_4th
    s_, f_, sk, fl = asyncio.run(fu.run_edit_batch(page, MATCHED, delay=0))
finally:
    st.restore()
check("页面死亡短路: edit 只被调了 4 次（第 5 次被短路）", n2["c"] == 4, n2["c"])
check("页面死亡短路: 剩余二次条目不再尝试",
      any("未二次尝试" in r for _, r in fl), fl)
check("页面死亡短路: 计数完整 (成功+失败=3)", s_ + f_ == 3, (s_, f_))

# 二次尝试阶段取消（原 GUI 独有，合并后 CLI 也有了）
flip = {"on": False}
st = Stub(edit_script=[("fail", "标题重复"), ("ok",), ("ok",)])
st.install()
try:
    def cancel_after_main():
        return flip["on"]

    # 主循环 3 章跑完后翻转取消标志：在第 3 次 edit 调用后翻转
    orig_edit = fu.edit_one_chapter
    n = {"c": 0}

    async def edit_count(pg, *a, **kw):
        n["c"] += 1
        r = await orig_edit(pg, *a, **kw)
        if n["c"] == 3:
            flip["on"] = True
        return r
    fu.edit_one_chapter = edit_count
    s_, f_, sk, fl = asyncio.run(fu.run_edit_batch(
        FakePage(), MATCHED, delay=0, cancel_check=cancel_after_main))
finally:
    st.restore()
check("二次尝试前取消: 剩余条目记「用户取消」",
      any("用户取消" in r for _, r in fl), fl)
check("取消记账: 计数完整", s_ + f_ == 3, (s_, f_))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
