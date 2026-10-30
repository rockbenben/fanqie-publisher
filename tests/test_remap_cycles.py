# -*- coding: utf-8 -*-
"""remap 写入顺序 / 闭环跳过 / 审核等待 / 重试分类的回归测试。零依赖。

2026-10-30 实测事故：已公开段 480-694 的 215 个位置是一个 206 环 + 一个 9 环。
旧实现把 480/524/670 排到最后“再走一趟”，首笔却去写 481（第481章还在 480）
→ 平台报「重复标题和段落」；两个白重试又把自己打进「提交频繁」，整批中止。
随后合成「调整中」占位又被平台按内容安全规范驳回（本章打卡字数清零）。
现在的做法：依赖拓扑排序；闭环整环跳过、不写任何合成占位；已发布章写完等
平台审核真正生效，再写依赖它的下一章。

运行: python tests/test_remap_cycles.py
"""
import asyncio
import importlib.util
import inspect
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_remap_under_test", ROOT / "tools" / "remap" / "remap.py")
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)

FAILED = []


def check(label, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f"  <- {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILED.append(label)


def live_items():
    """线上实测的 215 个位置映射（位置 -> 它现在装着的章号）。"""
    items = []
    for pos in range(480, 695):
        if pos == 692:
            n = 480
        elif pos == 693:
            n = 524
        elif pos == 694:
            n = 670
        elif pos <= 522:
            n = pos + 1
        elif pos <= 667:
            n = pos + 2
        else:
            n = pos + 3
        items.append({"index": pos, "pos": pos, "title": f"第{n}章 标题"})
    return items


def live_entries():
    return [{"index": p, "want_num": p, "now_title": f"位置{p}"}
            for p in range(480, 695)]


print("== 闭环: 整环跳过，不生成任何占位正文 ==")
_items = live_items()
_holders = rm.build_chapter_holders(_items)
check("215 个章号各有一个持有位置",
      len(_holders) == 215 and all(len(v) == 1 for v in _holders.values()),
      len(_holders))
_steps, _dropped, _cycles = rm.resolve_write_plan(live_entries(), _holders)
check("原始 215 结构是两个闭环，整环跳过",
      sorted(len(c) for c in _cycles) == [9, 206] and _steps == [],
      [len(c) for c in _cycles])
check("闭环不生成任何写入步骤/占位", _steps == [], _steps)

# 当前线上：480 已被占位（标题成了 6676，第481章腾出来了）→ 206 环断成链，
# 只剩 9 环；工具应修 206 链（480 在队尾写回），跳过 9 环。
_broken = []
for _pos in range(480, 695):
    _n = (6676 if _pos == 480 else
          480 if _pos == 692 else 524 if _pos == 693 else 670 if _pos == 694
          else _pos + 1 if _pos <= 522 else _pos + 2 if _pos <= 667 else _pos + 3)
    _broken.append({"index": _pos, "pos": _pos, "title": f"第{_n}章 x"})
_hb = rm.build_chapter_holders(_broken)
_st7, _d7, _c7 = rm.resolve_write_plan(live_entries(), _hb)
check("当前结构: 只剩 9 环，206 链可写",
      _d7 == [] and sorted(len(c) for c in _c7) == [9]
      and len(_st7) == 206, (_d7, [len(c) for c in _c7], len(_st7)))
_idx7 = [s["entry"]["index"] for s in _st7]
check("206 链首笔是 481，480 在队尾写回",
      _idx7[0] == 481 and _idx7.index(480) > _idx7.index(692), _idx7[:3] + _idx7[-3:])
check("9 环位置不在写入步骤里",
      not any(i in _idx7 for i in _c7[0]), _c7[0])
_done = set()
_ok = True
for _s in _st7:
    _e = _s["entry"]
    if not all(h in _done for h in _hb.get(_e["want_num"], [])):
        _ok = False
        break
    _done.add(_e["index"])
check("206 链每次写入时目标持有者都写过", _ok)
check("206 个位置都排到了真实写入", len(_done) == 206, len(_done))

print("== 真实正文缓冲: 解第一个闭环 ==")
_ring_items_b = [{"index": 1, "title": "第2章 甲"},
                 {"index": 2, "title": "第1章 乙"}]
_h_b = rm.build_chapter_holders(_ring_items_b)
_ring_b = [{"index": 1, "want_num": 1}, {"index": 2, "want_num": 2}]
_st_b, _dr_b, _cy_b = rm.resolve_write_plan(
    _ring_b, _h_b, buffer_num=5197)
check("缓冲步 + 写2 + 写1；闭环不再跳过",
      [(s["entry"]["index"], s["placeholder"]) for s in _st_b]
      == [(1, True), (2, False), (1, False)]
      and _dr_b == [] and _cy_b == [], (_st_b, _cy_b))

_cycle9 = _c7[0]
_items9 = [x for x in _broken if x["index"] in _cycle9]
_h9 = rm.build_chapter_holders(_items9)
_entries9 = [{"index": p, "want_num": p, "now_title": "x"} for p in _cycle9]
_st9, _d9, _c9 = rm.resolve_write_plan(
    _entries9, _h9, buffer_num=5197)
check("9 环用真实缓冲: 10 步，首步缓冲 670，末步写回 670",
      len(_st9) == 10 and _st9[0]["placeholder"]
      and _st9[0]["entry"]["index"] == 670
      and _st9[-1]["entry"]["index"] == 670
      and not _st9[-1]["placeholder"]
      and _d9 == [] and _c9 == [], (len(_st9), _st9[0], _st9[-1], _c9))
_done9, _ph9 = set(), set()
_ok9 = True
for _s in _st9:
    _e = _s["entry"]
    if _s["placeholder"]:
        _ph9.add(_e["index"])
        continue
    if not all(h in _done9 or h in _ph9
               for h in _h9.get(_e["want_num"], [])):
        _ok9 = False
        break
    _done9.add(_e["index"])
check("9 环每次真实写入时目标持有者都写过（缓冲算腾出）",
      _ok9 and len(_done9) == 9, (_ok9, len(_done9)))

_buf_dir = Path(tempfile.mkdtemp(prefix="fanqie_buf_"))
_bp = _buf_dir / "chapter-5197.md"
_bp.write_text("# 第5197章 缓冲标题\n\n" + "真实的正文。" * 300,
               encoding="utf-8")
_bp_short = _buf_dir / "chapter-5198.md"
_bp_short.write_text("# 第5198章 短\n\n短正文", encoding="utf-8")
_ns_auto = types.SimpleNamespace(buffer_chapter="auto", buffer_file=None)
_ns_file = types.SimpleNamespace(buffer_chapter=None,
                                 buffer_file=str(_bp))
_bp536 = _buf_dir / "chapter-0536.md"
_bp536.write_text("# 第536章 中段缺号\n\n" + "真实的正文。" * 300,
                  encoding="utf-8")
_num, _path, _err = rm.resolve_buffer(
    _ns_auto, {536: str(_bp536), 5197: str(_bp)}, {}, 5196)
check("auto 只从平台末尾之后选（不拿中段缺号 536）",
      _num == 5197 and _err is None and _path == str(_bp),
      (_num, _path, _err))
_num, _path, _err = rm.resolve_buffer(
    types.SimpleNamespace(buffer_chapter="536", buffer_file=None),
    {536: str(_bp536)}, {}, 5196)
check("显式选中段缺号: 拒绝", _num is None and "末尾" in (_err or ""),
      (_num, _err))
_num, _path, _err = rm.resolve_buffer(_ns_file, {}, {})
check("文件缓冲: 章号解析/长度通过", _num == 5197 and _err is None,
      (_num, _err))
_num, _path, _err = rm.resolve_buffer(
    types.SimpleNamespace(buffer_chapter="5197", buffer_file=None),
    {5197: str(_bp)}, {5197: [670]})
check("缓冲章号已在平台: 拒绝", _num is None and "已存在" in (_err or ""),
      (_num, _err))
_num, _path, _err = rm.resolve_buffer(
    types.SimpleNamespace(buffer_chapter="5198", buffer_file=None),
    {5198: str(_bp_short)}, {})
check("缓冲正文不足 1000 字: 拒绝",
      _num is None and "1000" in (_err or ""), (_num, _err))
_num, _path, _err = rm.resolve_buffer(_ns_auto, {}, {})
check("没有未发布章节可用: 拒绝", _num is None and "没有" in (_err or ""),
      (_num, _err))
_num, _path, _err = rm.resolve_buffer(
    types.SimpleNamespace(buffer_chapter=None,
                          buffer_file=str(_buf_dir / "nope.md")), {}, {})
check("缓冲文件不存在: 拒绝", _num is None and "不存在" in (_err or ""),
      (_num, _err))

print("== 小结构: 环 / 链 / 连锁踢 ==")
_ring_items = [{"index": 1, "title": "第2章 甲"},
               {"index": 2, "title": "第1章 乙"}]
_ring_holders = rm.build_chapter_holders(_ring_items)
_ring = [{"index": 1, "want_num": 1}, {"index": 2, "want_num": 2}]
_st, _dr, _cy = rm.resolve_write_plan(_ring, _ring_holders)
check("环形互换: 整环跳过，不写占位",
      _st == [] and _dr == [] and [sorted(c) for c in _cy] == [[1, 2]],
      (_st, _dr, _cy))

_orphan_items = [{"index": 0, "title": "第1章 甲"},
                 {"index": 1, "title": "第9章 乙"},
                 {"index": 5, "title": "第7章 丙"}]
_ho = rm.build_chapter_holders(_orphan_items)
_orphan = [{"index": 1, "want_num": 1}, {"index": 5, "want_num": 5}]
_st2, _d2, _c2 = rm.resolve_write_plan(_orphan, _ho)
check("目标章号占在范围外: 只踢那一个，另一个照写",
      [s["entry"]["index"] for s in _st2] == [5]
      and [e["index"] for e in _d2] == [1] and _c2 == [], (_st2, _d2))

_cas_items = [{"index": 0, "title": "第1章 甲"},
              {"index": 1, "title": "第2章 乙"}]
_hc = rm.build_chapter_holders(_cas_items)
_cas = [{"index": 1, "want_num": 1}, {"index": 2, "want_num": 2}]
_st3, _d3, _c3 = rm.resolve_write_plan(_cas, _hc)
check("连锁踢到不动点: 两个都写不了",
      _st3 == [] and [e["index"] for e in _d3] == [1, 2] and _c3 == [],
      (_st3, _d3))

_chain_items = [{"index": 10, "title": "第11章 甲"},
                {"index": 20, "title": "第21章 乙"},
                {"index": 30, "title": "第31章 丙"},
                {"index": 40, "title": "第10章 丁"},
                {"index": 41, "title": "第20章 戊"},
                {"index": 42, "title": "第30章 己"}]
_hh = rm.build_chapter_holders(_chain_items)
_chain = [{"index": i, "want_num": i}
          for i in (10, 20, 30, 40, 41, 42)]
_st4, _d4, _c4 = rm.resolve_write_plan(_chain, _hh)
_pos4 = {s["entry"]["index"]: k for k, s in enumerate(_st4)}
check("无环置换: 不踢、不占位、依赖先行",
      _d4 == [] and _c4 == []
      and all(not s["placeholder"] for s in _st4)
      and _pos4[40] < _pos4[10] and _pos4[41] < _pos4[20]
      and _pos4[42] < _pos4[30], (_d4, _c4, _st4))

print("== 同名多份: 只腾出一份不算腾出 ==")
_dup_items = [{"index": 0, "title": "第1章 甲"},
              {"index": 1, "title": "第9章 乙"},
              {"index": 3, "title": "第1章 丙"}]
_hdu = rm.build_chapter_holders(_dup_items)
check("第1章的两个持有位置都记下", _hdu.get(1) == [0, 3], _hdu.get(1))
_st5, _d5, _c5 = rm.resolve_write_plan(
    [{"index": 1, "want_num": 1}, {"index": 3, "want_num": 3}], _hdu)
check("范围外还剩一份同名 → 永久重名踢掉",
      [e["index"] for e in _d5] == [1] and _c5 == [], (_d5, _c5))

check("闭环不再生成占位正文函数",
      not hasattr(rm, "placeholder_content"), dir(rm))

print("== 重试分类: 内容/字段/限流不白重试 ==")
check("重复标题和段落不重试",
      not fu.is_retryable_publish_error("本书中存在重复标题和段落，请修改后再发布"))
check("大段落重复不重试", not fu.is_retryable_publish_error("文章内容有大段落重复"))
check("提交频繁不重试", not fu.is_retryable_publish_error("提交频繁，请稍后再试"))
check("章节号非数字仍可重试（编辑器填充可能瞬时失败）",
      fu.is_retryable_publish_error("章节序号只支持阿拉伯数字"))
check("正文字数不足仍可重试（可能是粘贴未完成）",
      fu.is_retryable_publish_error("正文至少输入1000字"))
check("超时/网络类照常重试", fu.is_retryable_publish_error("net::ERR_TIMED_OUT"))
check("服务器小差照常重试", fu.is_retryable_publish_error("服务器开小差了，请稍后再试"))
check("空错误照常重试", fu.is_retryable_publish_error(""))
check("edit_one_chapter 真的用了这个判据",
      "is_retryable_publish_error" in inspect.getsource(fu.edit_one_chapter))
check("非重试失败不会继续下一轮",
      "if not retryable:" in inspect.getsource(fu.edit_one_chapter))


class _FakePage:
    async def goto(self, url):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def screenshot(self, path=None):
        return None


def _edit_attempts(errmsg):
    calls = {"fill": 0}

    async def _noop(*_a, **_kw):
        return None

    async def _fill(*_a, **_kw):
        calls["fill"] += 1

    async def _nav(*_a, **_kw):
        raise RuntimeError(errmsg)

    saved = {n: getattr(fu, n) for n in (
        "wait_for_editor_ready", "dismiss_edit_hint", "clear_editor",
        "fill_chapter", "_navigate_to_publish_settings")}
    fu.wait_for_editor_ready = _noop
    fu.dismiss_edit_hint = _noop
    fu.clear_editor = _noop
    fu.fill_chapter = _fill
    fu._navigate_to_publish_settings = _nav
    try:
        ok, err = asyncio.run(fu.edit_one_chapter(
            _FakePage(), "http://example.invalid", 481, "标题", "正文",
            max_retries=2))
    finally:
        for _n, _v in saved.items():
            setattr(fu, _n, _v)
    return ok, err, calls["fill"]


_ok, _err, _tries = _edit_attempts("本书中存在重复标题和段落，请修改后再发布")
check("撞重名: 只提交一次就返回，不把自己打进限流",
      _ok is False and _tries == 1 and "重复" in _err,
      (_ok, _err, _tries))

print("== 已发布章的修改审核中: 不能当没看见 ==")
_it5 = [{"index": 480, "pos": 480, "title": "第481章 old",
         "display_status": fu.DISPLAY_REVIEWING_PUBLISHED,
         "timer_time": 0, "item_id": "x",
         "cant_modify_reason": "审核中暂不支持修改"}]
_plan5, _skip5 = rm.compute_remap(_it5, include_published=True)
check("status 5 被跳过，并说清是审核中",
      _plan5 == [] and "审核" in dict(_skip5)[480], (_plan5, _skip5))

# 审核不通过、但平台没给「不可修改」理由 → 编辑器可用，重排要能把它写回来
_it7 = [{"index": 480, "pos": 480, "title": "第6676章 占位",
         "display_status": fu.DISPLAY_REVIEW_FAILED,
         "timer_time": 0, "item_id": "x", "cant_modify_reason": ""}]
_plan7, _skip7 = rm.compute_remap(_it7, include_published=True)
check("status 7 无理由: 可改写", [e["index"] for e in _plan7] == [480]
      and _plan7[0]["review_failed"] is True
      and _plan7[0]["needs_review"] is True, (_plan7, _skip7))
_it7r = [dict(_it7[0], cant_modify_reason="完结作品不可修改")]
_plan7r, _skip7r = rm.compute_remap(_it7r, include_published=True)
check("status 7 有理由: 跳过并照抄理由",
      _plan7r == [] and dict(_skip7r)[480] == "完结作品不可修改",
      (_plan7r, _skip7r))
check("审核中的错位章会被提前拦住",
      rm.reviewing_in_scope(_it5, 480, 694)
      == [(480, "第481章 old", "审核中暂不支持修改")],
      rm.reviewing_in_scope(_it5, 480, 694))
check("位置本身对的不挡重排",
      rm.reviewing_in_scope([dict(_it5[0], title="第480章 x")], 480, 694) == [])
check("范围外的不挡", rm.reviewing_in_scope(_it5, 1, 479) == [])

print("== 等平台真正生效: 审核中不算腾出章号 ==")


class _WaitPage:
    async def wait_for_timeout(self, ms):
        return None


def _wait_seq(seq):
    calls = {"n": 0}

    async def _fetch(page, book_id):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return (seq[i], {}, [])

    return _fetch, calls


_saved_fetch = fu.fetch_chapter_items
try:
    _fetch, _calls = _wait_seq([
        [{"pos": 480, "index": 480, "title": "第481章 old", "display_status": 5}],
        [{"pos": 480, "index": 480, "title": "第480章 new", "display_status": 1}],
    ])
    fu.fetch_chapter_items = _fetch
    _state, _detail = asyncio.run(rm.wait_applied(
        _WaitPage(), "b", 480, 480, window_s=1, poll_s=0))
    check("审核通过后返回 applied", _state == "applied", (_state, _detail))
    check("确实轮询了两次", _calls["n"] == 2, _calls)

    _fetch, _calls = _wait_seq([
        [{"pos": 480, "index": 480, "title": "第481章 old", "display_status": 7}]])
    fu.fetch_chapter_items = _fetch
    _state, _detail = asyncio.run(rm.wait_applied(
        _WaitPage(), "b", 480, 480, window_s=1, poll_s=0))
    check("审核不通过: 返回 rejected", _state == "rejected", (_state, _detail))

    _fetch, _calls = _wait_seq([
        [{"pos": 480, "index": 480, "title": "第481章 old", "display_status": 5}]])
    fu.fetch_chapter_items = _fetch
    _state, _detail = asyncio.run(rm.wait_applied(
        _WaitPage(), "b", 480, 480, window_s=0, poll_s=0))
    check("审核没跑完返回 pending", _state == "pending", (_state, _detail))
finally:
    fu.fetch_chapter_items = _saved_fetch

print("== 已发布章审核没跑完时，绝不写依赖它的下一章 ==")
_entry1 = {"index": 1, "want_num": 1, "item_id": "i1",
           "now_title": "第2章", "needs_review": True}
_entry2 = {"index": 2, "want_num": 2, "item_id": "i2",
           "now_title": "第3章", "needs_review": False}
_steps = [{"entry": _entry1, "placeholder": False},
          {"entry": _entry2, "placeholder": False}]
_edit_calls = []


async def _fake_edit(page, url, num, title, content, **kw):
    _edit_calls.append(num)
    return True, ""


async def _fake_wait(page, book_id, pos, want_num, **kw):
    return "pending", "标题第2章/状态5"


def _fake_parse(path):
    return Path(path).name[-1], "标题", "正文"


_saved_parse = fu.parse_md_file
_saved_edit, _saved_wait = fu.edit_one_chapter, rm.wait_applied
fu.parse_md_file = _fake_parse
fu.edit_one_chapter, rm.wait_applied = _fake_edit, _fake_wait
try:
    _ok, _fail, _paused = asyncio.run(rm.run_remap(
        _WaitPage(), "b", _steps, {1: "dummy1", 2: "dummy2"}))
finally:
    fu.parse_md_file = _saved_parse
    fu.edit_one_chapter, rm.wait_applied = _saved_edit, _saved_wait
check("第一笔真实改写审核未完成: 后面的章没发",
      _edit_calls == [1] and _ok == [1], (_edit_calls, _ok))
check("暂停点记在位置 1",
      bool(_paused) and _paused[0] == 1 and _paused[1] == "write", _paused)
check("后面的章记为前方暂停，不算失败",
      _fail == [(2, "前方暂停，未处理")], _fail)

print("== 真实缓冲写入: 用真实章节正文，最后换回 ==")
_ring_items_c = [{"index": 1, "title": "第2章 甲"},
                 {"index": 2, "title": "第1章 乙"}]
_h_c = rm.build_chapter_holders(_ring_items_c)
_ring_c = [{"index": 1, "want_num": 1, "item_id": "i1", "now_title": "第2章"},
           {"index": 2, "want_num": 2, "item_id": "i2", "now_title": "第3章"}]
_steps_c = rm.resolve_write_plan(
    _ring_c, _h_c, buffer_num=5197)[0]
_buf_calls = []


async def _fake_edit_c(page, url, num, title, content, **kw):
    _buf_calls.append(num)
    return True, ""


async def _fake_wait_c(page, book_id, pos, want_num, **kw):
    return "applied", "标题第5197章/状态1"


def _fake_parse_c(path):
    if str(path) == str(_bp):
        return "5197", "缓冲标题", "真实正文。" * 300
    return Path(path).name[-1], "标题", "正文"


_saved_parse_c = fu.parse_md_file
_saved_edit_c, _saved_wait_c = fu.edit_one_chapter, rm.wait_applied
fu.parse_md_file = _fake_parse_c
fu.edit_one_chapter, rm.wait_applied = _fake_edit_c, _fake_wait_c
try:
    _okc, _failc, _pausedc = asyncio.run(rm.run_remap(
        _WaitPage(), "b", _steps_c, {1: "dummy1", 2: "dummy2"},
        buffer_path=str(_bp)))
finally:
    fu.parse_md_file = _saved_parse_c
    fu.edit_one_chapter, rm.wait_applied = _saved_edit_c, _saved_wait_c
check("真实缓冲: 先写缓冲第5197章，再按依赖写 2、1",
      _buf_calls == [5197, 2, 1], _buf_calls)
check("真实缓冲不计成功；最终 1 在成功里",
      _okc == [2, 1] and _pausedc is None, (_okc, _pausedc))
check("真实缓冲没有失败",
      _failc == [], _failc)

print("== CLI/GUI 入口: 闭环跳过/真实缓冲可选 ==")


def _opts(*cmd):
    out = subprocess.run([sys.executable] + list(cmd) + ["-h"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", cwd=str(ROOT)).stdout or ""
    return {ln.strip().split()[0] for ln in out.splitlines()
            if ln.strip().startswith("--")}


check("工具不再暴露 --break-cycles",
      "--break-cycles" not in _opts(str(ROOT / "tools/remap/remap.py")))
check("主 CLI 也不再暴露 --break-cycles",
      "--break-cycles" not in _opts(str(ROOT / "fanqie_upload.py"), "remap"))
check("主 CLI 不再传 break_cycles",
      "break_cycles" not in inspect.getsource(fu._run_tool_cmd))
GUI = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")
check("GUI 不再用 --break-cycles", "--break-cycles" not in GUI)
check("GUI 只有一次确认", "按这个方案重新预览吗" not in GUI)
check("GUI 确认框说明闭环会跳过", "闭环会跳过" in GUI)
check("GUI 确认框说明审核暂停", "审核没跑完本次会暂停" in GUI)
check("GUI 完成提示会说暂停", "本次暂停" in GUI)
check("GUI 不再承诺『自动再走一趟』", "本趟会撞名" not in GUI)
check("工具源码: 闭环默认跳过，给真实缓冲才解",
      "闭环默认跳过" in inspect.getsource(rm.resolve_write_plan)
      and "buffer_num" in inspect.getsource(rm.resolve_write_plan)
      and not hasattr(rm, "placeholder_content"))
check("工具在审核没跑完时会暂停，不硬写下一章",
      "本次暂停" in inspect.getsource(rm.run_remap)
      and "wait_applied" in inspect.getsource(rm.run_remap))
_rm_opts = _opts(str(ROOT / "tools/remap/remap.py"))
_cli_opts = _opts(str(ROOT / "fanqie_upload.py"), "remap")
check("工具暴露真实缓冲参数",
      "--buffer-chapter" in _rm_opts and "--buffer-file" in _rm_opts)
check("主 CLI 暴露真实缓冲参数",
      "--buffer-chapter" in _cli_opts and "--buffer-file" in _cli_opts)
check("GUI 提供闭环缓冲三选一",
      "闭环处理" in GUI and "--buffer-chapter" in GUI
      and "--buffer-file" in GUI and "用本地未发布章节自动缓冲" in GUI)
# 「缓冲章还没换回」的位置号靠 GUI 从工具输出里取。工具写成列表 repr
# （`[670]`）时正则匹配不上，弹窗就只剩半句、位置丢了还不报错——所以钉形状。
_BUF_SHAPE = r"位置\s*([\d、]+)\s*临时借用了真实章节"
check("GUI 取位置的正如测试内所钉", _BUF_SHAPE in GUI)
check("工具按同一形状打印未换回的位置",
      re.search(r"位置 \{[^}]*\} 临时借用了真实章节",
                inspect.getsource(rm.main_async)) is not None)
check("该形状能被 GUI 正则取到位置号",
      re.search(_BUF_SHAPE, "位置 " + "、".join(map(str, [670, 692]))
                + " 临时借用了真实章节（尚未换回）") is not None)
shutil.rmtree(_buf_dir, ignore_errors=True)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
