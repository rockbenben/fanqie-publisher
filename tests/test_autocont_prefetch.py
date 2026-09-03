# -*- coding: utf-8 -*-
"""「接着上次往后排」从配置恢复时，选作品就该拉整份平台章节表。

回归锁定：此前续排只在用户手点勾选框或点「开始上传」时才抓章节表；勾选状态
从 config 恢复的启动路径两者都不经过——启动抓了一次（队尾日期），点「开始上传」
又抓一次（章节表），还要求用户再点一次。现在选作品 / 发完一批后统一按
_needs_chapter_list 判断该拉什么，抓回来的 last_pub 同时喂给「队列排到」标签。

运行: python tests/test_autocont_prefetch.py
"""
import inspect
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_gui as fg  # noqa: E402  (导入不创建 Tk 窗口)

FAILED = []


def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


class _Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


def needs(mode, autocont):
    obj = fg.FanqieGUI.__new__(fg.FanqieGUI)
    obj.mode_var, obj.autocont_var = _Var(mode), _Var(autocont)
    return obj._needs_chapter_list()


print("== 判据 ==")
check("定时发布 + 续排 → 拉章节表", needs("schedule", True) is True)
check("定时发布不勾续排 → 不拉", needs("schedule", False) is False)
check("非定时模式勾了续排也不拉", needs("publish", True) is False)
check("修改内容 → 拉", needs("edit", False) is True)
check("修改排期 → 拉", needs("reschedule", False) is True)

print("== 两个重拉点都走同一判据（修一边漏另一边是惯犯） ==")
for fn in ("_on_book_changed", "_upload_done"):
    src = inspect.getsource(getattr(fg.FanqieGUI, fn))
    check(f"{fn} 用 _needs_chapter_list", "self._needs_chapter_list()" in src)
    check(f"{fn} 不再只看 edit/reschedule 决定拉章节表",
          'if self.mode_var.get() in ("edit", "reschedule"):\n            self._fetch_platform_chapters_for_edit()' not in src)

print("== 定时（无人值守）路径同一判据 ==")
src = inspect.getsource(fg.FanqieGUI._timer_preflight_issues)
check("启动定时前置检查用 _needs_chapter_list", "self._needs_chapter_list()" in src)
check("上传类模式仍检查本地文件", 'mode not in ("edit", "reschedule") and not self.files' in src)
src = inspect.getsource(fg.FanqieGUI._timer_tick)
check("触发前刷新窗口补抓平台章节表", "self._ensure_platform_chapters()" in src)

print("== 章节表抓回来也要给出队尾时刻 ==")
src = inspect.getsource(fg.FanqieGUI._on_platform_chapters_fetched)
check("非修改类模式调用 _apply_last_publish", "self._apply_last_publish(" in src)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("全部通过")
