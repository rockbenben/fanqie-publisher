# -*- coding: utf-8 -*-
"""save_auth 返回语义测试（零依赖：不需要 pytest / 浏览器）。

回归锁定：登录流程必须能区分"保存成功"与"保存失败"。失败时 AUTH_FILE
还是旧账号会话，GUI 若照常复制成命名账号文件，会把旧账号 cookie 静默
挂到新账号名下（跨账号错位）。修复后 save_auth 返回 bool 供登录路径检查。

运行:  python tests/test_save_auth_result.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fanqie_upload as fu  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class LiveContext:
    async def storage_state(self):
        return {"cookies": [{"name": "sid", "value": "new"}], "origins": []}


class DeadContext:
    """浏览器被用户提前关闭后的 context：storage_state 抛错。"""
    async def storage_state(self):
        raise RuntimeError("Target page, context or browser has been closed")


def run(coro):
    return asyncio.run(coro)


def main():
    tmp = Path(tempfile.mkdtemp())
    orig = fu.AUTH_FILE
    fu.AUTH_FILE = tmp / ".auth_state.json"
    try:
        # 1) 正常保存 → True 且文件写入
        ok = run(fu.save_auth(LiveContext()))
        check("正常保存返回 True", ok is True)
        data = json.loads(fu.AUTH_FILE.read_text(encoding="utf-8"))
        check("状态已写入", data["cookies"][0]["value"] == "new")

        # 2) 预置旧账号会话，context 已死 → False 且旧文件原样保留
        fu.AUTH_FILE.write_text(
            json.dumps({"cookies": [{"name": "sid", "value": "old-account"}]}),
            encoding="utf-8")
        ok = run(fu.save_auth(DeadContext()))
        check("死 context 返回 False（登录路径据此中止复制）", ok is False)
        data = json.loads(fu.AUTH_FILE.read_text(encoding="utf-8"))
        check("失败时旧会话未被破坏", data["cookies"][0]["value"] == "old-account")
        check("失败时无 tmp 残留",
              not list(tmp.glob("*.tmp")), str(list(tmp.glob('*.tmp'))))

        # 3) tmp 已写出但 replace 失败（目标是目录）→ False 且 tmp 被清理。
        #    残留的 .auth_state.json.tmp 含完整登录 cookie，会有被
        #    git add . 连带提交的泄露风险。
        d2 = tmp / "authdir"
        d2.mkdir()
        fu.AUTH_FILE = d2 / "as_dir.json"
        (d2 / "as_dir.json").mkdir()  # 让 replace 失败
        ok = run(fu.save_auth(LiveContext()))
        check("replace 失败返回 False", ok is False)
        check("replace 失败后 tmp 已清理",
              not list(d2.glob("*.tmp")), str(list(d2.glob('*.tmp'))))
    finally:
        fu.AUTH_FILE = orig

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
