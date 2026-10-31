# -*- coding: utf-8 -*-
"""重排进度：每章一行进度 + 静默期心跳 + CLI 与 GUI 共用同一份进度格式。

2026-10-31 用户反馈：「重排中一直不知道是否再动」。两处会让人以为程序死了：
  · CLI：改写一章要开编辑器、清空旧稿、灌正文、等平台保存，这一段可能一分
    多钟一个字都不吐；
  · GUI：重排跑在子进程里，原来用 capture_output 一次性收结果、跑完才显示，
    几十章要跑一两个小时，屏幕全程一动不动。
所以这里守住三件事：进度行有、慢操作有心跳、两边的进度格式是同一份。

运行: python tests/test_remap_progress.py
"""
import asyncio
import contextlib
import importlib.util
import inspect
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fanqie_upload as fu  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_remap_progress_under_test", ROOT / "tools" / "remap" / "remap.py")
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)

GUI = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")

FAILED = []


def check(label, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f"  <- {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILED.append(label)


print("== 时长/预估说得像人话 ==")
check("秒级", rm.fmt_dur(48) == "48 秒", rm.fmt_dur(48))
check("分钟级", rm.fmt_dur(192) == "3 分 12 秒", rm.fmt_dur(192))
check("小时级", rm.fmt_dur(4800) == "1 小时 20 分", rm.fmt_dur(4800))
check("整小时不带零头", rm.fmt_dur(3600) == "1 小时", rm.fmt_dur(3600))
check("负数按 0 算", rm.fmt_dur(-5) == "0 秒", rm.fmt_dur(-5))
check("样本少于 2 个不给预估（两个点的平均是骗人）",
      rm.eta_hint(120, 1, 5) == "", rm.eta_hint(120, 1, 5))
check("没有剩余就不提预估", rm.eta_hint(120, 5, 0) == "", rm.eta_hint(120, 5, 0))
_h = rm.eta_hint(120, 2, 4)
check("有样本时给出「已用/预计还需」",
      "已用" in _h and "预计还需" in _h, _h)

print("== 慢操作有心跳：别让人以为卡死 ==")


async def _slow():
    await asyncio.sleep(0.05)
    return "done"


async def _boom():
    await asyncio.sleep(0)
    raise RuntimeError("炸了")


_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _r = asyncio.run(rm.with_heartbeat(_slow(), "测试动作", interval_s=0.01))
check("慢操作照常返回结果", _r == "done", _r)
check("慢操作期间报了心跳", "还在进行" in _buf.getvalue(), _buf.getvalue())
check("心跳带上已用时间", "已用" in _buf.getvalue(), _buf.getvalue())

_buf2 = io.StringIO()
with contextlib.redirect_stdout(_buf2):
    _r2 = asyncio.run(rm.with_heartbeat(_slow(), "测试动作", interval_s=5))
check("快操作不报心跳（不刷屏）",
      _r2 == "done" and "还在进行" not in _buf2.getvalue(), _buf2.getvalue())

_buf3 = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf3):
        asyncio.run(rm.with_heartbeat(_boom(), "测试动作", interval_s=0.01))
    _raised = False
except RuntimeError:
    _raised = True
check("异常照常往上抛（心跳不能把失败吞掉）", _raised)

print("== 每章一行进度 ==")


class _FakePage:
    async def wait_for_timeout(self, ms):
        return None


_steps = [{"entry": {"index": i, "want_num": i, "item_id": f"i{i}",
                     "now_title": f"第{i + 1}章"},
           "placeholder": False} for i in (1, 2, 3)]
_num2path = {i: f"{i}.md" for i in (1, 2, 3)}


def _fake_parse(path):
    return Path(path).stem, f"标题{Path(path).stem}", "正文"


async def _fake_edit(page, url, num, title, content, **kw):
    return True, ""


_saved_parse, _saved_edit = fu.parse_md_file, fu.edit_one_chapter
fu.parse_md_file, fu.edit_one_chapter = _fake_parse, _fake_edit
try:
    _out = io.StringIO()
    with contextlib.redirect_stdout(_out):
        _ok, _fail, _paused = asyncio.run(
            rm.run_remap(_FakePage(), "b", _steps, _num2path))
finally:
    fu.parse_md_file, fu.edit_one_chapter = _saved_parse, _saved_edit
_txt = _out.getvalue()
check("三章都写成功", _ok == [1, 2, 3], _ok)
check("每章一行进度（带序号/总数）",
      all(f"[{i}/3]" in _txt for i in (1, 2, 3)), _txt)
check("进度行带上章号与新标题",
      "index 1" in _txt and "→ 第1章" in _txt, _txt)
check("跑到第 3 步时给出预估",
      "已用" in _txt and "预计还需" in _txt, _txt)

print("== CLI 打印的进度行，GUI 认得出来 ==")
_first = next(ln for ln in _txt.splitlines() if ln.startswith("[1/3]"))
_m = rm.STEP_PROGRESS_RE.search(_first)
check("共享正则能解析 CLI 自己打印的行",
      bool(_m) and (int(_m.group(1)), int(_m.group(2))) == (1, 3),
      _first)
check("共享正则不把别的方括号当进度",
      rm.STEP_PROGRESS_RE.search("[dry-run] 本次将改写 85 个 item") is None)
check("GUI 用的是工具模块里同一份正则（不另写一份）",
      "STEP_PROGRESS_RE" in GUI and "_on_tool_line" in GUI)
check("GUI 的子进程改成流式读（不再一次性收）",
      "subprocess.Popen" in GUI and "stdout=subprocess.PIPE" in GUI)
check("GUI 不再用 capture_output 收工具输出",
      "capture_output=True" not in GUI.split("def _run_cli_tool")[1][:4000])
check("重排写入阶段确实走了心跳",
      "with_heartbeat" in inspect.getsource(rm.run_remap))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
