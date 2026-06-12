# -*- coding: utf-8 -*-
"""_load_gui_state 容错测试（零依赖：不需要 pytest / 浏览器）。

回归锁定：.gui_state.json 不可读（被占用/云端按需文件离线/同名目录）时
open() 抛 OSError，修复前会逃出 FanqieGUI.__init__ 导致 GUI 整个起不来；
应与 load_config 对 config.json 的处理一致——降级为空状态。

运行:  python tests/test_gui_state_load.py
"""
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_gui as fg  # noqa: E402  (导入不创建 Tk 窗口)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def with_state_file(path):
    """临时替换模块全局 GUI_STATE_FILE 并调用 _load_gui_state。"""
    orig = fg.GUI_STATE_FILE
    fg.GUI_STATE_FILE = Path(path)
    try:
        return fg.FanqieGUI._load_gui_state()
    finally:
        fg.GUI_STATE_FILE = orig


def main():
    tmp = Path(tempfile.mkdtemp())

    # 1) 不可读：exists() 为真但 open() 抛 OSError（用同名目录稳定复现，
    #    与文件被占用/云端离线同属 OSError 路径）
    locked = tmp / "state_as_dir.json"
    locked.mkdir()
    try:
        result = with_state_file(locked)
        check("不可读文件 → 空状态不抛异常", result == {}, f"got {result!r}")
    except Exception as e:
        check("不可读文件 → 空状态不抛异常", False, f"raised {e!r}")

    # 2) 损坏 JSON → 空状态（既有行为不回归）
    broken = tmp / "broken.json"
    broken.write_text('{"current_account": "作家A', encoding="utf-8")
    check("损坏 JSON → 空状态", with_state_file(broken) == {})

    # 3) 合法 JSON 但非对象 → 空状态（既有行为不回归）
    arr = tmp / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    check("非对象 JSON → 空状态", with_state_file(arr) == {})

    # 4) 正常文件原样读出
    good = tmp / "good.json"
    good.write_text('{"current_account": "作家A"}', encoding="utf-8")
    check("正常文件读出", with_state_file(good) == {"current_account": "作家A"})

    # 5) 文件不存在 → 空状态
    check("文件不存在 → 空状态", with_state_file(tmp / "nope.json") == {})

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
