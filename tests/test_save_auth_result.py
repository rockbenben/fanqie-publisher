# -*- coding: utf-8 -*-
"""save_auth 返回语义测试（零依赖：不需要 pytest / 浏览器）。

回归锁定：登录流程必须能区分"保存成功"与"保存失败"。失败时 AUTH_FILE
还是旧账号会话，GUI 若照常复制成命名账号文件，会把旧账号 cookie 静默
挂到新账号名下（跨账号错位）。修复后 save_auth 返回 bool 供登录路径检查。

运行:  python tests/test_save_auth_result.py
"""
import asyncio
import json
import os
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
from fanqie_gui import FanqieGUI  # noqa: E402

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

        # 4) Windows 上 replace 会被实时扫描短暂挡住（WinError 5）。实测本机
        #    约 10% 概率、且常连着失败几次——必须重试，否则用户登录状态
        #    「保存失败」、设置悄悄回退。
        d3 = tmp / "retry"
        d3.mkdir()
        fu.AUTH_FILE = d3 / ".auth_state.json"
        real_replace = Path.replace
        calls = {"n": 0}

        def flaky_replace(self, target):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(5, "拒绝访问")
            return real_replace(self, target)

        Path.replace = flaky_replace
        try:
            ok = run(fu.save_auth(LiveContext()))
        finally:
            Path.replace = real_replace
        check("rename 被占用时重试后成功", ok is True and calls["n"] == 4,
              f"calls={calls['n']} ok={ok}")
        check("重试后状态正确落盘",
              json.loads(fu.AUTH_FILE.read_text(encoding="utf-8"))["cookies"][0]["value"] == "new")

        # 5) GUI 侧 config.json 的原子写同样要重试：写失败时设置会静默回退
        #    （_save_config 曾把异常吞进 logger.debug，而日志级别是 INFO，
        #    那条 debug 根本不落盘）。
        d4 = tmp / "cfg"
        d4.mkdir()
        cfg = d4 / "config.json"
        calls2 = {"n": 0}

        def flaky_replace2(self, target):
            calls2["n"] += 1
            if calls2["n"] <= 2:
                raise PermissionError(5, "拒绝访问")
            return real_replace(self, target)

        Path.replace = flaky_replace2
        try:
            FanqieGUI._atomic_write_json(cfg, {"k": "v"})
        finally:
            Path.replace = real_replace
        check("GUI 原子写 rename 被占用时重试后成功",
              calls2["n"] == 3 and json.loads(cfg.read_text(encoding="utf-8")) == {"k": "v"},
              f"calls={calls2['n']}")

        # 6) 重试的**等待时长**要用共享常量：GUI 与命令行差着步长的话，谁改
        #    了另一个都不知道（CLI 那边在异步里等，不卡界面；GUI 那边是 Tk
        #    主线程上 sleep，步子大一点界面就僵一下）。
        slept = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *a, **k):
            slept.append(delay)
            return await real_sleep(0, *a, **k)

        d5 = tmp / "backoff"
        d5.mkdir()
        fu.AUTH_FILE = d5 / ".auth_state.json"
        calls3 = {"n": 0}

        def flaky_replace3(self, target):
            calls3["n"] += 1
            if calls3["n"] <= 3:
                raise PermissionError(5, "拒绝访问")
            return real_replace(self, target)

        Path.replace = flaky_replace3
        asyncio.sleep = spy_sleep
        try:
            ok = run(fu.save_auth(LiveContext()))
        finally:
            Path.replace = real_replace
            asyncio.sleep = real_sleep
        check("重试的等待时长用的是共享常量",
              slept == [fu.RENAME_BACKOFF_S * (i + 1) for i in range(3)],
              f"slept={slept} 期望={[fu.RENAME_BACKOFF_S * (i + 1) for i in range(3)]}")
        check("共享常量本身不是 0（真退避）", fu.RENAME_BACKOFF_S > 0, str(fu.RENAME_BACKOFF_S))

        # 7) 上次运行崩溃残留的 tmp（老版本写的、权限是 0644）不能被直接
        #    写进新 cookie：os.open 的 mode 只在「创建」时生效，老文件会
        #    保持宽权限。所以写之前要把它删掉。
        d6 = tmp / "stale"
        d6.mkdir()
        fu.AUTH_FILE = d6 / ".auth_state.json"
        stale = d6 / ".auth_state.json.tmp"
        stale.write_text('{"cookies": [{"name": "old", "value": "stale"}]}',
                        encoding="utf-8")
        os.chmod(stale, 0o644)
        ok = run(fu.save_auth(LiveContext()))
        check("残留的 tmp 被替换掉（不是往里追加）",
              ok is True
              and json.loads(fu.AUTH_FILE.read_text(encoding="utf-8"))["cookies"][0]["value"] == "new",
              fu.AUTH_FILE.read_text(encoding="utf-8")[:80])
        if os.name != "nt":
            mode = stale.stat().st_mode & 0o777
            check("残留 tmp 换成 0600（cookie 不该宽权限）",
                  mode == 0o600, oct(mode))
        else:
            print("     （Windows 上权限位不可观测，跳过权限断言）")

        # 8) GUI 侧的 _copy_auth：复制账号文件时不能把源的宽权限盖到目标上
        #    （copy2 会连源的 0644 一起盖过来）。它不 chmod，而是按 0600 建出
        #    tmp 再 os.replace 换名——所以这里断言的是「目标权限是 0600」，
        #    不是「chmod 有没有被调用」。
        d7 = tmp / "copy"
        d7.mkdir()
        src = d7 / ".auth_作家A.json"
        dst = d7 / ".auth_state.json"
        src.write_text('{"cookies": [{"name": "sid", "value": "abc"}]}',
                       encoding="utf-8")
        os.chmod(src, 0o644)
        FanqieGUI._copy_auth(src, dst)
        check("_copy_auth 复制出来的内容一致",
              json.loads(dst.read_text(encoding="utf-8"))["cookies"][0]["value"] == "abc")
        if os.name != "nt":
            mode = dst.stat().st_mode & 0o777
            check("_copy_auth 的目标不是宽权限（copy2 会把源的 0644 盖过来）",
                  mode == 0o600, oct(mode))
        else:
            print("     （Windows 上权限位不可观测，跳过权限断言）")
    finally:
        fu.AUTH_FILE = orig

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
