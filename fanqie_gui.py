#!/usr/bin/env python3
"""
番茄作家 MD/TXT 批量上传工具 - GUI 界面

使用方法:
    python fanqie_gui.py
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import unicodedata
from collections import Counter
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime, timedelta
from pathlib import Path

try:
    from fanqie_upload import (
        load_config,
        parse_md_files, get_md_files, strip_md_formatting,
        deduplicate_titles, compute_schedule, validate_times,
        parse_chapter_spec, filter_by_chapter_spec, parse_time_spec,
        parse_datetime, TIMER_INPUT_FORMATS,
        fetch_chapter_items, audit_chapter_positions, MOVE_WINDOW_H,
        AUDIT_CLEAN_LINE, audit_position_counts,
        review_block_headline, review_block_where, AUDIT_SEG_CLEAN,
        schedule_order_warning, review_block_never_passes, order_break_line,
        batch_summary_line,
        reader_view_around, review_status_lines, schedule_order_lines,
        queue_state, queue_summary, queue_tail_dt, queue_label_text, rows_to_items,
        schedule_order_conflicts, schedule_repair_plan, slot_timestamp,
        SCHEDULE_ORDER_CLEAN_LINE,
        wait_book_list_ready, describe_book_page,
        RENAME_RETRIES, RENAME_BACKOFF_S,
        fetch_draft_list, compress_chapter_nums,
        volume_count,
        log_fail_list, _load_tool_module,
        create_context, save_auth, close_browser_safely, goto_with_login_retry,
        wait_for_editor_ready,
        run_creation_batch, run_edit_batch,
        extract_chapters_from_page, match_chapters,
        reschedule_on_manage_page, build_schedule_map, detect_volumes, select_volume,
        settle_page,
        AUTH_FILE, BOOK_MANAGE_URL, NEW_CHAPTER_URL_TPL,
        CHAPTER_MANAGE_URL_TPL, SCRIPT_DIR, ZONE_URL, CONFIG_FILE, GUI_STATE_FILE,
        BOOKS_JS,
        logger, setup_logging, LOG_FILE as UPLOAD_LOG_FILE, get_browser_timeout,
    )
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError as e:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "缺少依赖",
        f"请先安装依赖:\n  pip install playwright\n  playwright install chromium\n\n{e}",
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_CHAPTERS_DIR = SCRIPT_DIR / "chapters"

# --- 新手引导 ---
GH_URL = "https://github.com/rockbenben/fanqie-publisher"
ONBOARD_STEP_NAMES = ["登录", "选作品", "选章节文件夹", "上传/修改"]
_CIRCLED = ["①", "②", "③", "④"]
# 引导栏与章节预览抢高度的三个阈值，写在一起，别散落成 900/800/860 三个
# 字面量：改一个忘了另一个，行为会悄悄对不上。
#   GUIDE_SCREEN_SHORT_H  屏幕比这矮 → 启动就收起引导栏
#   GUIDE_COLLAPSE_H       窗口比这矮 → 自动收起（把高度让给预览）
#   GUIDE_EXPAND_H         窗口比这高 → 自动展开回来
GUIDE_SCREEN_SHORT_H = 900
GUIDE_COLLAPSE_H = 800
GUIDE_EXPAND_H = 860
# 引导栏自己占多高。实测（150% 缩放的开发机；别的缩放比例下绝对值会变，
# 要看的是量级）：展开 GUIDE_H_PX，收起后那条细条 GUIDE_STUB_PX。
# 收起能省下的高度 = 两者之差，这个差值才是要紧的——省得少了，
# 矮窗口下章节预览就只剩一条缝（预览是上传前唯一的复核环节）。
# 这两个数写在代码里（而不是散在注释里各写一遍）：布局一改，量了更新
# 这两行就行；散着写必然有一处对不上，而对不上的后果是预览被挤没。
GUIDE_H_PX = 89
GUIDE_STUB_PX = 35
# 步骤提示语。只说「该做什么」，不再点名具体按钮——「下一步」按钮就摆在
# 这句话右边，指名道姓反而让用户满界面找那颗按钮（以前写「点「开始上传」」，
# 而那颗按钮在窗口最底下，中间隔着一整屏）。
STEP_HINTS = {
    1: "下一步：登录番茄账号。填的是本地账号名（只是标签），"
       "真正的登录在弹出的浏览器里完成。",
    2: "下一步：刷新作品列表，选中要操作的那一部作品。",
    3: "下一步：选好存放章节的文件夹（默认 chapters/），"
       "确认下方「章节预览」里出现章节。",
    4: "下一步：选好「操作模式」，再点右边的按钮开始。",
}
# 引导栏「下一步」按钮指向界面上哪颗真按钮：键=步号，
# 值=(按钮属性名, 按钮上显示的文字)。文字为 None 表示「跟着那颗按钮自己的
# 文字走」——第 ④ 步的主按钮会在「开始上传 / 开始修改 / 停止」之间变。
# 这里只做转发：判定「能不能点」的是按钮本身，引导栏不另判一套。
# 两套判据迟早对不上，那时用户看到的就是「引导栏说能点、点了却报错」。
GUIDE_CTA = {
    1: ("btn_login", None),
    2: ("btn_books", "刷新作品列表"),   # 目标是个 ↻ 图标按钮，自己没有名字
    3: ("btn_browse_dir", "选择文件夹"),
    4: ("btn_upload", None),
}
# 四个维护/诊断按钮的界面文字。集中在这里是因为它们在代码里被引用了
# 很多次（按钮本身、提示语、弹窗、报错），散成字面量就会出现
# 「按钮上写 A、提示里叫 B」这种对不上。
# 用词跟着工具自己的词汇表走（缺口 / 排期 / 重排 / 草稿箱）——README、
# 审计报告、命令行子命令（remap / --fix-schedule / clean-drafts）说的都是
# 这一套，界面上另起一套说法只会让人多记一份。
# 「什么时候用我」不写在名字里，写在悬停提示的第一句（见下面 TIP_*）：
# 名字只说得出动的是什么，说不出什么时候该用它，而新手正是卡在这一步。
BTN_AUDIT = "检查缺口"
BTN_FIX_SCHED = "修复排期…"
BTN_REMAP = "章节重排…"
BTN_CLEAN = "清空草稿箱…"
# 四颗维护按钮的悬停提示。每条第一句统一是「什么时候用：」——这是回答
# 「这四个我该点哪个」的地方。写成常量而不是就地写字符串，是为了能被
# 测试直接读出来守（见 tests/test_button_ux.py）。
TIP_AUDIT = (
    "什么时候用：想确认章节顺序有没有乱、或者读者说章节顺序不对。\n"
    "只看不改：把平台上每章的「位置」和「章号」对一遍，报出哪里对不上，"
    "并分成三段说清谁能修。\n"
    f"未公开段可自动重排；已公开段超 3 天移动不了位置，"
    f"但还能靠「{BTN_REMAP}」改写内容把顺序修正回来。\n"
    "先点它查一遍——报告里会直接写明该用后面哪一个、改哪一段、"
    "位置区间在哪。")
TIP_FIX_SCHED = (
    "什么时候用：章节本身没写错，只是发布的先后时刻排串了"
    "（比如手动插过几章）。\n"
    "它把排在别人前面的章挪到队列末尾之后，只改发布时间"
    "（位置、章号、正文都不动），读者收不到内容更新。\n"
    "先给方案，确认后才真改；没有排错的章就什么都不做。")
TIP_REMAP = (
    "什么时候用：中间漏发过章，后面的章全串位了——那种情况改发布时间"
    "救不回来，得让位置 i 重新装第 i 章的内容。\n"
    "先预览计划，确认后才真改；默认只动待发布的章，排期不变。\n"
    "点开可选择：改哪一段（含已公开段）、位置区间、本次上限，"
    "以及闭环怎么处理（跳过／真实正文缓冲）。\n"
    "选中「已公开段」会重写线上正文、重新过审，读者可能收到更新。")
TIP_CLEAN = (
    "什么时候用：存草稿模式攒了一堆不要的草稿，想清空平台草稿箱。\n"
    "删前强制核对：每条草稿的章号在本地要有对应文件，"
    "有一条对不上就整批拒绝执行。")

BOOK_PLACEHOLDER = "点右侧 ↻ 刷新作品列表"
LOGIN_FIRST_MSG = "还没有登录番茄账号。点顶部「登录 / 新建」，在弹出的浏览器里完成登录。"
PICK_BOOK_MSG = "点作品行右侧的「↻」刷新列表，再选择要操作的作品。"
# 操作模式代号 → 中文名。一份就够：这张表在界面上抄过四遍（模式单选按钮、
# 预览汇总行、上传确认框、倒计时状态行），改一个忘了另一个，同一个模式在
# 界面上就会出现两种说法。
# 模式清单：代号 → (界面上的名字, 放在哪一行)。这是模式的**唯一**一份清单：
# 启动校验、单选按钮、界面文案都从这里派生。加一个新模式只改这一处，
# 不然会出现「加了名字没加单选按钮」或「启动时被校验挡掉」这类对不上。
MODE_SPEC = {
    "schedule": ("定时发布", "pub"),
    "publish": ("立即发布", "pub"),
    "draft": ("存草稿", "pub"),
    "edit": ("修改内容", "mod"),
    "reschedule": ("修改排期", "mod"),
}
# 只要名字的那几处用这个（值就是 MODE_SPEC 的第一项）
MODE_LABELS = {k: v[0] for k, v in MODE_SPEC.items()}
# 「每天章数」的取值范围。三个地方都要用这一个：界面的微调框、启动时对
# 存盘值的兜底、预览里的报错文案。写死三遍的话，只改一处就会出现
# 「框里能填 99、启动又被悄悄改回 2」这种对不上的情况。
PER_DAY_MIN = 1
PER_DAY_MAX = 20
PICK_DIR_MSG = "点「选择文件夹」指定存放章节的目录，每个 .md / .txt 文件就是一章。"
BOOK_EMPTY_HINT = "还没有作品 · 先去番茄建一本书"
SECTION_HELP = {
    "账号": "给每个番茄账号起一个本地名称用于区分。点「登录/新建」会打开浏览器，"
            "请在其中登录番茄账号——本地名称只是标签，不是番茄笔名。"
            "登录多个账号后用下拉框切换。",
    "作品与章节": "上面选作品：登录后点「↻」拉取你的作品，选中一部后会自动获取最新发布信息，"
                  "「📖」可在浏览器里打开该作品的章节管理页。\n"
                  "下面选章节：指定存放章节的文件夹（含子文件夹），每个 .md 或 .txt（纯文本）"
                  "文件就是一章，按文件名顺序排列（chapter-1 在 chapter-10 之前）。"
                  "「自动处理重名」给本地重名的标题追加章号后缀（番茄不允许同名章节）；"
                  "「按修改日期筛选」只挑近期改过的章节。",
    "操作模式": "五种操作，按需选一：\n"
                "· 定时发布：排好日期时间，到点自动发布\n"
                "· 立即发布：上传后马上发布\n"
                "· 存草稿：只上传存草稿，稍后自己手动发\n"
                "· 修改内容：用本地文件替换平台上已有章节的正文\n"
                "· 修改排期：只改已有章节的发布时间，不动正文\n"
                "所有模式都能「按章节号筛选」，只操作指定范围（如 1,3,5-10）的章节。\n"
                "定时发布还可勾「接着上次往后排」：不用自己填起始日期、也不用挑哪几章——"
                "工具去平台看已经排到哪一刻，先填满那天剩下的时间点再逐日往后排，且只发平台上还没有的章。"
                "旁边「排到 N 天后」留空就全排上去，填 N 则只排到那天为止。",
    "定时执行": "设定一个未来时刻（格式 YYYY-MM-DD HH:MM），到点自动执行当前所选操作（仅一次），"
                "适合无人值守（比如半夜自动上传）。到点若有任务在跑会等它结束再执行，"
                "触发前会重新读取一次章节目录。",
}

# ---------------------------------------------------------------------------
# 视觉规范：稿纸与番茄（Manuscript & Tomato）
#   番茄红 = 品牌与主行动；排期橙 = 一切与"时间/连载节奏"有关的元素。
#   单一暖白底 + 发丝描边卡片，避免 ttk 标签底色错配，也避开奶油+衬线的套路观感。
# ---------------------------------------------------------------------------
CLR_BASE     = "#FCFBF9"   # 全局暖白底（画布与卡片同色，靠描边分区）
CLR_FIELD    = "#FFFFFF"   # 输入框/文本面板
CLR_INK      = "#241F1A"   # 主文字
CLR_INK_SOFT = "#6E675D"   # 次要文字 / 说明
CLR_HAIRLINE = "#E2DBCF"   # 发丝分隔线 / 卡片描边
CLR_TOMATO   = "#D8402B"   # 番茄红：品牌带 + 主按钮 + 当前步骤
CLR_TOMATO_D = "#B02E1D"   # 番茄红按下 / 链接
CLR_SCHED    = "#E5822A"   # 排期橙：排期 / 定时 / 进度（连载节奏）
CLR_SCHED_D  = "#C0691A"
# 排期橙的正文变体：CLR_SCHED 在暖白底上只有 2.69:1，当正文用读不清。
# 这个值同色相同饱和、只压低明度，实测 4.58:1 过 WCAG AA(4.5)。
# 平台状态/倒计时这类小字状态用它，别直接用 CLR_SCHED。
CLR_SCHED_TX = "#C44E00"
CLR_BTN_BG   = "#F0ECE4"   # 普通按钮底
CLR_TAB_BG   = "#EDE8DF"   # 未选中的标签页底
CLR_TIP_BG   = "#2A2622"   # tooltip 深色底
CLR_OK       = "#2E7D46"   # 已登录 / 完成
CLR_WARN     = "#C0392B"   # 未登录 / 错误
CLR_BRAND_TX = "#FFFFFF"   # 品牌带主文字
CLR_BRAND_SUB = "#F7CDC2"  # 品牌带次要文字 / 链接


# 高 DPI 支持 (Windows)
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 日志 Handler — 将 logger 输出写入 GUI 日志面板
# ---------------------------------------------------------------------------
class TextHandler(logging.Handler):
    """将日志消息线程安全地追加到 Tkinter ScrolledText 控件。"""

    def __init__(self, widget, root):
        super().__init__()
        self._widget = widget
        self._root = root

    def emit(self, record):
        msg = self.format(record)
        if not msg.endswith("\n"):
            msg += "\n"
        try:
            self._root.after(0, self._append, msg)
        except tk.TclError:
            pass

    def _append(self, text):
        try:
            self._widget.configure(state="normal")
            self._widget.insert(tk.END, text)
            self._widget.see(tk.END)
            self._widget.configure(state="disabled")
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 后台 Async 工作线程
# ---------------------------------------------------------------------------
class AsyncWorker:
    """后台线程，拥有独立的 asyncio 事件循环。"""

    def __init__(self):
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


# ---------------------------------------------------------------------------
# 复用的无头浏览器（后台查询共享一个 chromium 实例）
# ---------------------------------------------------------------------------
class _SharedBrowser:
    """复用的无头浏览器实例，用于作品列表 / 发布时间等后台查询。"""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        # 串行化 ensure/refresh/close：这些都在 worker 单一事件循环上运行，但
        # 多个后台任务（刷新作品列表 + 切到修改模式拉章节）可在 await 点交错。
        # 无锁时两个并发 ensure() 在浏览器尚未建好（_browser 仍为 None）时会
        # 各自 start() 一套 playwright+chromium，只有最后赋值的被记录，另一套
        # 子进程成为孤儿、连 _on_close 都关不到——进程/内存泄漏。
        self._lock = asyncio.Lock()
        # 已确认内核缺失：让「一键装内核」一个会话只弹一次，而不是每个
        # 失败的功能各弹一次
        self.missing_browser = False

    async def ensure(self):
        """确保浏览器运行中，返回 context。"""
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return self._context
            await self._teardown()
            try:
                self._pw = await async_playwright().start()
                self._browser, self._context = await create_context(
                    self._pw, headless=True)
            except Exception as e:
                # launch() 失败 = 内核没装或损坏。这个错会以完全相同的
                # 形式从每个用到浏览器的功能里冒出来，而原先只有登录路径
                # 做了识别：用户跳过登录直接点「刷新作品列表」，连着看到几次
                # 英文原文，也不知道该干什么（rockbenben/fanqie-publisher#4）。
                # 在这里置一次位，由上层弹一次中文引导 + 一键安装。
                if looks_like_missing_browser(e):
                    self.missing_browser = True
                raise
            return self._context

    async def refresh(self):
        """重新创建（登录后需要刷新 auth）。"""
        async with self._lock:
            await self._teardown()

    async def close(self):
        async with self._lock:
            await self._teardown()

    async def _teardown(self):
        """实际关闭逻辑（不加锁）。仅由持锁的 ensure/refresh/close 调用，
        避免 ensure 内部再次走 close() 重入自身持有的锁而死锁。"""
        if self._browser:
            await close_browser_safely(self._browser)
        if self._pw:
            try:
                # 浏览器进程挂死时 stop() 会等 driver 退出而无限悬停，
                # 加超时防止 ensure()/窗口关闭被卡住
                await asyncio.wait_for(self._pw.stop(), timeout=10)
            except Exception as e:
                logger.debug(f"停止 Playwright: {e}")
        self._browser = self._context = self._pw = None


# ---------------------------------------------------------------------------
# 主 GUI（其下先是两个模块级小辅助 looks_like_missing_browser / _dlg_alive，再是 FanqieGUI 类）
# ---------------------------------------------------------------------------
def looks_like_missing_browser(error) -> bool:
    """判断错误是否为"Playwright 浏览器内核未安装/损坏"。

    首次安装未跑 `playwright install chromium`、或升级后浏览器没同步时，
    launch() 会抛此类错误。它是所有「点下去没反应」里最常见、也最容易自己修的
    一种：识别它就能给出可一键执行的修复，而不是让人对着报错猜。

    放模块级而不是 FanqieGUI 的静态方法：_SharedBrowser（在 GUI 之外、无权
    访问 GUI）也要用它给 ensure() 的失败置位。
    """
    s = str(error).lower()
    return ("executable doesn't exist" in s
            or "playwright install" in s
            or "please run the following command" in s)


def _display_work_area(x, y):
    """屏幕坐标 (x, y) 所在那块显示器的「工作区」——去掉任务栏之后的可用区域。

    返回 (left, top, right, bottom)；问不到时返回 None。

    为什么不用 winfo_screenwidth/height：Windows 的任务栏是贴在屏幕边上的，
    那两个数报的是**整块屏幕**（实测 2560x1440），而任务栏占了底下 48px
    （工作区只有 1392）。按整块屏幕钳位，弹窗底下那排按钮正好压在任务栏
    下面——那排按钮是模态窗唯一的出口，压住了就等于程序卡死。

    别的平台没有「任务栏贴在屏幕里」这回事（dock 一般不遮窗口），退回
    整块屏幕就行，所以只在 Windows 上问系统。
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            # 结构体在这里现建：每次调用重建一次类、重设一次 argtypes，
            # 实测 43 微秒——弹窗一次才调一次，不值得为它把 ctypes 的零件
            # 搬到模块级（那儿还得处理「非 Windows 上没有这些符号」）。
            class _RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", _RECT), ("rcWork", _RECT),
                            ("dwFlags", wintypes.DWORD)]

            _u32 = ctypes.windll.user32
            _u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            _u32.MonitorFromPoint.restype = wintypes.HANDLE
            _u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(_MONITORINFO)]
            _u32.GetMonitorInfoW.restype = wintypes.BOOL
            _mon = _u32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
            _mi = _MONITORINFO()
            _mi.cbSize = ctypes.sizeof(_mi)
            if _u32.GetMonitorInfoW(_mon, ctypes.byref(_mi)):
                _r = _mi.rcWork
                return _r.left, _r.top, _r.right, _r.bottom
        except Exception:
            pass          # 问不到就退回整块屏幕，不影响功能
    return None


def _dlg_alive(w):
    """控件是否还存在。destroy() 之后再问 winfo_exists() 会抛 TclError。

    Enter 这种"可能连着触发两次"的事件要用它挡一下：按钮的绑定先于 Toplevel
    的绑定执行，它已经拆了窗口，后面那次就必须安静地什么都不做。
    """
    try:
        return bool(w.winfo_exists())
    except tk.TclError:
        return False


class FanqieGUI:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("番茄作家 · 连载发布工作台")
        # 首次尺寸按屏幕来算：写死 1280x980 时，1366×768 的笔记本会得到一个比屏幕
        # 还高的窗口，标题栏被顶出屏幕外、底部按钮压在任务栏下面，两头都够不着。
        self.root.update_idletasks()
        _sw, _sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 上限 1400 是按**版式的自然宽度**定的，不是按某块屏幕：选项行需 534px，
        # 双栏卡片与预览行到 1400 已经宽松，再宽主要是加空白。大屏用户拖一下
        # 就行，而开箱就占满屏幕对小桌面是冒犯。减 80 给边框：1366 的本子
        # 算出 1286，不比旧值差。下限 1000 与 minsize 一致。
        _w = max(1000, min(1400, _sw - 80))
        # 下限 680 与 minsize 一致；预留 100 给任务栏+标题栏。
        # 原来是 max(560, _sh-120)，1366x768 的笔记本上算出 648 —— 开箱即「看不到任何章节预览」。
        _h = max(680, min(980, _sh - 100))
        _sx = max(0, (_sw - _w) // 2)
        _sy = max(0, (_sh - _h) // 3)
        self.root.geometry(f"{_w}x{_h}+{_sx}+{_sy}")
        # 高度下限按「预览至少看得见」定，不是随手写的：固定区域（页眉+引导栏+
        # 双栏卡片+行动条）加起来比一屏还多，而「上传前确认要发哪些章」是
        # 不可逆操作前唯一的复核环节。
        # 实测：1366×768 的本子按 680 开窗、引导栏展开时，预览区只剩 10px，
        # 连「总计 N 章」都看不见。现在靠两件事把它拿回来：① 各处留白收紧
        # （约 30px）；② 窗口太矮时引导栏自动收起（见 _fit_guidance_to_height，
        # 再省 GUIDE_H_PX - GUIDE_STUB_PX ≈ 54px）。这里不再写「固定区域约多少
        # px」——那个数会被下一次的改版推翻，而上面那句是量出来的。
        self.root.minsize(1000, 680)

        self.worker = AsyncWorker()
        self.worker.start()

        # 配置
        self._cfg = load_config()
        self._gui_state = self._load_gui_state()

        # 状态
        self.books: list[dict] = []
        self.parsed_chapters: list[tuple] = []
        self.files: list[Path] = []
        self._word_counts: list[int] = []
        self._all_files: list[Path] = []      # 目录全部文件（扫描缓存）
        self._all_parsed: list[tuple] = []    # 对应的解析结果缓存
        # 引导栏的两种「收起」要分清：用户自己收的（记住，不再自动干预）
        # 与窗口太矮自动收的（不写盘，窗口变高就自动展开）。
        # 「用户自己表态过」只认**本次会话**里的点击：状态文件里的
        # guidance_collapsed 只是上次的选择，不代表用户此刻仍想要展开。
        # 否则展开过一次的人再也享受不到窗口变矮时的自动收起。
        # 初值只在这里给一次；_build_guidance_bar 会按屏幕高度把 _guide_auto
        # 改成 True，那是状态更新不是重复初始化。
        self._guide_user_set = False
        self._guide_auto = False
        self.uploading = False
        self._closing = False
        self._cancel_requested = False
        # 工具子进程正在吐实时输出：这期间别让别的东西把预览面板整个换掉
        # （跑着的时候改目录/筛选会重画面板，会把正在滚的进度冲没）。
        self._tool_streaming = False
        # 上一次「检查缺口」的结论，供「章节重排…」预填区间用。
        # 带上 book_id：换书后位置含义完全不同，拿上一本的区间去填会写错书。
        self._last_audit = None
        self._log_handler = None
        # --- 缓存 ---
        # 失效时机: 切换账号 / 刷新作品列表 / 上传完成 → _invalidate_caches("all")
        #           切换作品 → 按需重新获取（缓存仍保留其他作品数据）
        self._last_publish_cache: dict[str, dict] = {}  # bookId -> {date, time}
        self._platform_chapters_cache: dict[str, list] = {}  # "bookId:vol" -> [章节列表]
        self._volumes_cache: dict[str, list | None] = {}  # bookId -> list | None(无卷)
        self._matched_edit: list = []  # 修改模式匹配结果
        self._shared = _SharedBrowser()  # 复用的无头浏览器
        self._fetch_gen = 0  # 防抖: 每次切换作品/卷递增
        self._login_in_progress = False  # 防止并发登录
        # --- 定时执行 ---
        self.timer_enabled = False        # 定时是否启动
        self._timer_target = None         # datetime: 目标执行时刻
        self._timer_after_id = None       # 轮询 after id
        self._timer_waiting_busy = False  # 已到点但有任务在运行，等待空闲
        self._timer_prerefresh_done = False  # 触发前 60 秒目录刷新是否已执行
        self._auto_run = False            # 守护同步弹窗阶段（定时触发时为 True）
        self._auto_run_pending = False    # 跨异步任务，供 _upload_done 抑制完成弹窗

        self._setup_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # 视觉主题
    # -----------------------------------------------------------------------
    def _install_check_indicator(self, style):
        """把勾选框的指示器换成对勾。

        clam 主题自带的指示器在选中态画的是一个 **✗**（叉），语义上像"否定/
        删除"，勾上反而像被划掉。这里用三张自绘小图替换 indicator 元素：
        未选=空框、选中=番茄红对勾、禁用=灰框。
        图必须挂在实例上保持引用，否则会被 GC 回收、控件变空白。
        """
        box, pad = 16, 2          # 16px 见方，留 2px 描边余量
        def mk(checked, disabled=False):
            img = tk.PhotoImage(width=box, height=box)
            edge = CLR_HAIRLINE if not disabled else "#EFEAE1"
            fill = "#FFFFFF" if not disabled else "#F5F1EA"
            img.put(fill, to=(0, 0, box, box))
            # 描边
            for i in range(box):
                img.put(edge, to=(i, 0, i + 1, 1))
                img.put(edge, to=(i, box - 1, i + 1, box))
                img.put(edge, to=(0, i, 1, i + 1))
                img.put(edge, to=(box - 1, i, box, i + 1))
            if checked:
                c = CLR_TOMATO if not disabled else "#B9B2A6"
                # 对勾：左下短笔下行 + 右上长笔上行，笔宽 3px 才看得清
                for k in range(4):          # 左下短笔
                    x, y = 3 + k, 6 + k
                    img.put(c, to=(x, y, x + 3, y + 3))
                for k in range(6):          # 右上长笔
                    x, y = 6 + k, 9 - k
                    img.put(c, to=(x, y, x + 3, y + 3))
            return img

        self._chk_imgs = (mk(False), mk(True), mk(False, True), mk(True, True))
        try:
            style.element_create("Tick.indicator", "image", self._chk_imgs[0],
                                 ("disabled", "selected", self._chk_imgs[3]),
                                 ("disabled", self._chk_imgs[2]),
                                 ("selected", self._chk_imgs[1]),
                                 sticky="", padding=pad)
            style.layout("TCheckbutton", [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("Tick.indicator", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w",
                                           "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        except tk.TclError:
            pass      # 元素已存在或主题不支持：保持默认外观，不影响功能

    def _setup_style(self):
        """套用 clam 主题 + 稿纸与番茄配色体系。所有控件底色统一为 CLR_BASE，
        卡片靠发丝描边分区，避免 ttk 标签在不同底色上出现色块错配。"""
        import tkinter.font as tkfont
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")  # 仅 clam 允许完整改色
        except tk.TclError:
            pass
        fams = set(tkfont.families())
        self._ff = next(
            (f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑")
             if f in fams), "TkDefaultFont")
        ff = self._ff

        base_sz = 10  # 界面主字号（原 9 偏小，操作区读起来吃力）
        self.root.configure(background=CLR_BASE)
        style.configure(".", background=CLR_BASE, foreground=CLR_INK,
                        font=(ff, base_sz))
        style.configure("TFrame", background=CLR_BASE)
        style.configure("TLabel", background=CLR_BASE, foreground=CLR_INK,
                        font=(ff, base_sz))
        style.configure("TCheckbutton", background=CLR_BASE, foreground=CLR_INK,
                        font=(ff, base_sz))
        style.map("TCheckbutton",
                  background=[("active", CLR_BASE)],
                  foreground=[("selected", CLR_INK)])
        self._install_check_indicator(style)
        style.configure("TRadiobutton", background=CLR_BASE, foreground=CLR_INK,
                        font=(ff, base_sz))
        style.map("TRadiobutton",
                  background=[("active", CLR_BASE)],
                  foreground=[("selected", CLR_TOMATO_D)])
        # 操作模式单选：核心决策，字号加大 + 选中番茄红
        style.configure("Mode.TRadiobutton", background=CLR_BASE,
                        foreground=CLR_INK, font=(ff, 13))
        style.map("Mode.TRadiobutton",
                  background=[("active", CLR_BASE)],
                  foreground=[("selected", CLR_TOMATO_D)])

        # 输入类
        for s in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(s, fieldbackground=CLR_FIELD, background=CLR_FIELD,
                            bordercolor=CLR_HAIRLINE, foreground=CLR_INK,
                            arrowcolor=CLR_INK_SOFT, padding=3, font=(ff, base_sz))
        style.map("TCombobox", fieldbackground=[("readonly", CLR_FIELD)],
                  foreground=[("readonly", CLR_INK)])
        self.root.option_add("*TCombobox*Listbox.font", (ff, base_sz))

        # 普通按钮：扁平 + 发丝描边
        style.configure("TButton", background=CLR_BTN_BG, foreground=CLR_INK,
                        bordercolor=CLR_HAIRLINE, relief="flat",
                        padding=(11, 6), font=(ff, base_sz))
        style.map("TButton",
                  background=[("active", "#E7E1D7"), ("pressed", "#DCD5C9"),
                              ("disabled", "#F3F1EC")],
                  foreground=[("disabled", "#B8B2A8")])
        # 主行动按钮：唯一的番茄红实心
        style.configure("Primary.TButton", background=CLR_TOMATO,
                        foreground=CLR_BRAND_TX, bordercolor=CLR_TOMATO,
                        relief="flat", padding=(20, 8), font=(ff, 11, "bold"))
        style.map("Primary.TButton",
                  background=[("active", CLR_TOMATO_D), ("pressed", CLR_TOMATO_D),
                              ("disabled", "#E6B6AC")],
                  foreground=[("disabled", "#FCEEEA")])
        # 图标按钮：单字形放大 + 留白，避免显得太小、点不准
        style.configure("Icon.TButton", font=(ff, 14), padding=(10, 4))
        style.map("Icon.TButton",
                  background=[("active", "#E7E1D7"), ("pressed", "#DCD5C9")])
        # emoji 图标（📖📂💾）在 YaHei 里回退到 Segoe UI Emoji，字形基线偏低、
        # 看着"下沉"；单给一档：底部多留白把字形顶上来，与 ↻ 等符号视觉居中对齐。
        style.configure("EmojiIcon.TButton", font=(ff, 14), padding=(10, 0, 10, 12))
        style.map("EmojiIcon.TButton",
                  background=[("active", "#E7E1D7"), ("pressed", "#DCD5C9")])

        # 选项卡条右端的次要动作（导出日志）：比普通按钮矮一档，塞得进标签行
        style.configure("TabAction.TButton", font=(ff, 9), padding=(9, 3))
        style.map("TabAction.TButton",
                  background=[("active", "#E7E1D7"), ("pressed", "#DCD5C9")])

        # 维护/诊断按钮（次级行动）：比普通按钮小一档，字也小一号。
        # 主按钮是唯一的红底实心，这排按钮若还用普通 TButton 的深底描边，
        # 五个按钮看上去一样重——新手分不出哪个是日常流程、哪个是出问题时
        # 才用的那一组。缩小一号 + 浅底，注意力自然回到左边那颗主按钮上。
        style.configure("Tool.TButton", background=CLR_BTN_BG,
                        foreground=CLR_INK_SOFT, bordercolor=CLR_HAIRLINE,
                        relief="flat", padding=(9, 4), font=(ff, 9))
        style.map("Tool.TButton",
                  background=[("active", "#E7E1D7"), ("pressed", "#DCD5C9"),
                              ("disabled", "#F3F1EC")],
                  foreground=[("active", CLR_INK), ("disabled", "#B8B2A8")])

        # 引导栏的「下一步」按钮：番茄红描边 + 红字，不做成红实心。
        # 实心红只留给底部那颗主按钮——两颗红实心并排，用户会分不清哪颗
        # 才是「正式开始」，反而把主行动稀释了。描边红同样醒目，一眼能看出
        # 它是引导区里的那颗、点了就做当前这一步。
        style.configure("Guide.TButton", background=CLR_BASE,
                        foreground=CLR_TOMATO_D, bordercolor=CLR_TOMATO,
                        relief="flat", borderwidth=1, padding=(13, 5),
                        font=(ff, 10, "bold"))
        style.map("Guide.TButton",
                  background=[("active", "#FBEAE6"), ("pressed", "#F7DCD6"),
                              ("disabled", CLR_BASE)],
                  foreground=[("disabled", "#BFB8AD")],
                  bordercolor=[("disabled", CLR_HAIRLINE)])
        # 同一颗按钮在「收起后的细条」里用的小一档版本：那条本来就只有一行，
        # 用大按钮会把细条撑高十几像素——那十几像素是从章节预览区里抢的
        # （预览是上传前唯一的复核环节，矮屏上只剩一条缝）。
        style.configure("GuideSlim.TButton", background=CLR_BASE,
                        foreground=CLR_TOMATO_D, bordercolor=CLR_TOMATO,
                        relief="flat", borderwidth=1, padding=(9, 2),
                        font=(ff, 9, "bold"))
        style.map("GuideSlim.TButton",
                  background=[("active", "#FBEAE6"), ("pressed", "#F7DCD6"),
                              ("disabled", CLR_BASE)],
                  foreground=[("disabled", "#BFB8AD")],
                  bordercolor=[("disabled", CLR_HAIRLINE)])

        # 卡片 / 标题
        style.configure("Card.TFrame", background=CLR_BASE,
                        bordercolor=CLR_HAIRLINE, relief="solid", borderwidth=1)
        style.configure("Sec.TLabel", background=CLR_BASE, foreground=CLR_INK,
                        font=(ff, 11, "bold"))
        style.configure("SecSched.TLabel", background=CLR_BASE,
                        foreground=CLR_SCHED_D, font=(ff, 11, "bold"))
        style.configure("Cap.TLabel", background=CLR_BASE,
                        foreground=CLR_INK_SOFT, font=(ff, 9))

        # 选项卡（章节预览 / 运行日志）
        style.configure("TNotebook", background=CLR_BASE, borderwidth=0,
                        tabmargins=(2, 4, 2, 0))
        style.configure("TNotebook.Tab", background=CLR_TAB_BG,
                        foreground=CLR_INK_SOFT, padding=(16, 7), font=(ff, 10))
        style.map("TNotebook.Tab",
                  background=[("selected", CLR_BASE)],
                  foreground=[("selected", CLR_TOMATO_D)])

        # 进度条：排期橙（连载节奏）
        style.configure("Horizontal.TProgressbar", background=CLR_SCHED,
                        troughcolor="#ECE6DC", bordercolor=CLR_HAIRLINE,
                        lightcolor=CLR_SCHED, darkcolor=CLR_SCHED)

    # -----------------------------------------------------------------------
    # UI 构建
    # -----------------------------------------------------------------------
    def _card(self, parent, title, help_key=None, sched=False):
        """带发丝描边的分区卡片。返回 (卡片外框, 内容区)；把控件塞进内容区。

        卡片的上下留白只留够呼吸的量：这些像素是从章节预览的高度里扣的，
        窗口高度下限 680 时预览区只剩一条缝（见 __init__ 里的说明）。
        """
        card = ttk.Frame(parent, style="Card.TFrame")
        head = ttk.Frame(card)
        head.pack(fill="x", padx=12, pady=(6, 1))
        # 标题前的细色条：统一各卡片的结构感；橙=计时（与进度条同色），红=主流程
        tk.Frame(head, width=3, background=CLR_SCHED if sched else CLR_TOMATO
                 ).pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(head, text=title,
                  style="SecSched.TLabel" if sched else "Sec.TLabel").pack(
                      side="left")
        if help_key:
            self._add_help(head, help_key)
        body = ttk.Frame(card)
        body.pack(fill="both", expand=True, padx=12, pady=(1, 7))
        return card, body

    def _attach_tooltip(self, widget, text):
        """给控件加悬停提示（图标按钮无文字时用来说明用途）。

        text 可传字符串，或传一个返回字符串的函数——内容随状态变化时（如路径框
        显示当前完整路径）用后者，悬停那一刻才取值。
        """
        state = {"tip": None}

        def show(_):
            if state["tip"] is not None or self._closing:
                return
            msg = text() if callable(text) else text
            if not msg:
                return
            x = widget.winfo_rootx() + 6
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            try:
                tw.attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(tw, text=msg, background=CLR_TIP_BG, foreground=CLR_BRAND_TX,
                     font=(self._ff, 9), padx=7, pady=3).pack()
            state["tip"] = tw

        def hide(_=None):
            if state["tip"] is not None:
                state["tip"].destroy()
                state["tip"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)
        widget.bind("<Destroy>", hide)

    def _build_header(self):
        """页眉：番茄红字标（不再用整条色带）+ 顶层链接 + 账号工具行，
        底部一条发丝线与内容分隔。

        字标与账号行**没有**合并成一行：合并后这一行需要约 1100px，而窗口
        最小宽度 1000px（内容区 976px）放不下——实测 1000px 时「❓ 帮助」
        被挤没、「GitHub ↗」被切掉一半。宁可多占一行，也不能把入口裁掉。
        """
        ff = self._ff
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=12, pady=(6, 0))

        row = ttk.Frame(header)
        row.pack(fill="x")
        brand = ttk.Frame(row)
        brand.pack(side="left")
        ttk.Label(brand, text="番茄作家 · 连载发布工作台",
                  foreground=CLR_INK, font=(ff, 14, "bold")).pack(side="left")
        links = ttk.Frame(row)
        links.pack(side="right")
        gh = ttk.Label(links, text="GitHub ↗", foreground=CLR_TOMATO_D,
                       cursor="hand2", font=(ff, 9, "underline"))
        gh.pack(side="right")
        gh.bind("<Button-1>", lambda _: webbrowser.open(GH_URL))
        hlp = ttk.Label(links, text="❓ 帮助", foreground=CLR_TOMATO_D,
                        cursor="hand2", font=(ff, 9))
        hlp.pack(side="right", padx=(0, 14))
        hlp.bind("<Button-1>", lambda _: self._show_welcome())

        bar = ttk.Frame(header)
        bar.pack(fill="x", pady=(4, 6))
        ttk.Label(bar, text="账号").pack(side="left")
        self.account_var = tk.StringVar()
        self.cmb_account = ttk.Combobox(
            bar, textvariable=self.account_var, state="readonly", width=16)
        self.cmb_account.pack(side="left", padx=6)
        self.cmb_account.bind("<<ComboboxSelected>>", self._on_account_selected)
        self.btn_login = ttk.Button(bar, text="登录 / 新建", command=self._on_login)
        self.btn_login.pack(side="left", padx=(0, 8))
        self.lbl_auth = ttk.Label(bar, text="")
        self.lbl_auth.pack(side="left", padx=2)
        self._add_help(bar, "账号", side="left")
        self._refresh_account_list()
        self._refresh_auth_status()

        # 与下方内容分隔的发丝线
        tk.Frame(self.root, height=1, background=CLR_HAIRLINE).pack(
            fill="x", padx=12)

    def _build_ui(self):
        # --- 0. 主行动条：最先 pack，永远占住底部一条 ---
        # pack 按调用顺序分配空间。先 pack 底栏（side="bottom"），窗口再矮也先
        # 分到它那一条；之后的页眉/卡片/选项卡只能瓜分剩余空间。此前底栏排在
        # cols 之后，窗口高度 < ~700px（如 1366×768 笔记本开 125% 缩放）时
        # 「开始上传」会被整条挤出可视区，用户无路可点。
        frm = ttk.Frame(self.root)
        frm.pack(side="bottom", fill="x", padx=12, pady=(6, 8))
        self.btn_upload = ttk.Button(
            frm, text="开始上传", style="Primary.TButton", command=self._on_upload)
        self.btn_upload.pack(side="left")
        # 主行动与维护工具之间画一条竖发丝线。这四个按钮和主按钮同排、又都
        # 是普通按钮时，一眼看过去就是「五个平级按钮」，新手分不出哪个是
        # 日常流程、哪个是出问题时才用的。一条线加上次级样式就分得开了。
        # 这里原先还有个「出问题时用」/「排错」的小标签，但它是多余的一层：
        # 标签一占宽度，进度条就被挤没了（实测 1000px 窄窗口下从 80px
        # 掉到 27px），而「什么时候用我」写在每颗按钮的悬停提示第一句里，
        # 比一个分组标签管用，也不占地方。
        # 分隔线比卡片描边深一档：卡片那圈发丝线是围一圈、有底色衬着才看得出，
        # 这里是一条 1px 竖线孤零零夹在两个按钮之间，用同一个色等于看不见，
        # 分组就白画了。
        tk.Frame(frm, width=1, background="#C9BFAE").pack(
            side="left", fill="y", padx=10, pady=3)
        # 检查缺口：只读的体检，按「谁能修」分段（详见 _on_audit_gaps）
        self.btn_audit = ttk.Button(
            frm, text=BTN_AUDIT, style="Tool.TButton", command=self._on_audit_gaps)
        self.btn_audit.pack(side="left", padx=(2, 0))
        # 传函数而非字符串：文字随检测结果变，悬停那一刻现取，不用反复 bind
        self._attach_tooltip(self.btn_audit, self._audit_tip)
        self._refresh_audit_badge()      # 上次的检测结论先摆出来
        # 修复排期：体检报出「排期顺序有错」之后的直接下一步。
        # 放在「检查缺口」旁边而不是收进菜单 —— 报错之后的下一步就该
        # 一眼看得见。
        self.btn_fix_sched = ttk.Button(
            frm, text=BTN_FIX_SCHED, style="Tool.TButton",
            command=self._on_fix_schedule)
        self.btn_fix_sched.pack(side="left", padx=(6, 0))
        self._attach_tooltip(self.btn_fix_sched, TIP_FIX_SCHED)
        # 章节重排与清空草稿箱这两项维护操作直接摆出来，不再收进「工具 ▾」下拉。
        # 为两个条目做一个菜单，等于多一次点击、还把它们藏起来；而藏起来的代价
        # 不只是麻烦——「检查缺口」报出未公开段错位之后，紧挨着的下一步正是
        # 「章节重排」，两者本该并排可见。菜单项还挂不了 tooltip，说明只能做成
        # 灰色副标题行，展开才看得到。
        # 四个按钮的**先后顺序就是排查的顺序**：先查（检查缺口）→ 改时刻
        # （修复排期）→ 改内容归属（章节重排）→ 清理（清空草稿箱）。
        # 顺序乱了用户就得自己猜该先点哪个。
        # （续排 keep_ahead 不在这里：它已经是「定时发布」模式下的「接着上次
        # 往后排」勾选框，再开一个入口就是同一功能的第二条路径。）
        self.btn_remap = ttk.Button(
            frm, text=BTN_REMAP, style="Tool.TButton", command=self._on_tool_remap)
        self.btn_remap.pack(side="left", padx=(6, 0))
        self._attach_tooltip(self.btn_remap, TIP_REMAP)
        self.btn_clean = ttk.Button(
            frm, text=BTN_CLEAN, style="Tool.TButton",
            command=self._on_tool_clean_drafts)
        self.btn_clean.pack(side="left", padx=(6, 0))
        self._attach_tooltip(self.btn_clean, TIP_CLEAN)
        self.progress = ttk.Progressbar(frm, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        self.lbl_progress = ttk.Label(frm, text="")
        self.lbl_progress.pack(side="left")

        # --- 页眉：番茄红字标 + 账号工具行 ---
        self._build_header()

        # --- 新手引导：步骤轨 ---
        self._build_guidance_bar()

        # --- 上部两栏：左=「发什么」（作品与章节 + 定时执行），右=「怎么发」（操作模式）---
        # 两栏高度要尽量持平：整块 cols 是固定高度、不可滚动，它多占一像素，
        # 下方章节预览/日志就少一像素。此前定时执行挂在右栏，定时发布模式下
        # 右栏 485px、左栏 326px，白白抬高了 160px 的窗口下限。
        cols = ttk.Frame(self.root)
        cols.pack(fill="x", padx=12, pady=(2, 0))
        cols.columnconfigure(0, weight=1, uniform="c")
        cols.columnconfigure(1, weight=1, uniform="c")
        self._acct_frame = cols  # 引导栏收起/展开的定位锚点（须在其上方）
        left = ttk.Frame(cols)
        left.grid(row=0, column=0, sticky="new", padx=(0, 6))
        right = ttk.Frame(cols)
        right.grid(row=0, column=1, sticky="new", padx=(6, 0))

        # --- 作品与章节（两者都在回答「发哪部作品的哪些文件」，合成一张卡）---
        card, frm = self._card(left, "作品与章节", "作品与章节")
        card.pack(fill="x")
        frm_dir = frm  # 章节文件夹各行与作品行同卡

        # 作品行
        row_book = ttk.Frame(frm)
        row_book.pack(fill="x")
        # 图标按钮先靠右占位，输入控件再填充中间；两行的动作按钮因此都在右侧
        # 同一条竖线上（作品行 ↻📖 / 目录行 选择文件夹 ↻📂），不再一左一右各摆一个
        self.btn_open_manage = ttk.Button(
            row_book, text="📖", width=2, style="EmojiIcon.TButton",
            command=self._open_chapter_manage)
        self.btn_open_manage.pack(side="right")
        self._attach_tooltip(self.btn_open_manage, "章节管理（在浏览器打开）")
        self.btn_books = ttk.Button(
            row_book, text="↻", width=2, style="Icon.TButton",
            command=self._on_refresh_books)
        self.btn_books.pack(side="right", padx=(6, 6))
        self._attach_tooltip(self.btn_books, "刷新作品列表")
        self.book_var = tk.StringVar()
        self.cmb_book = ttk.Combobox(
            row_book, textvariable=self.book_var, state="readonly", width=24)
        self.cmb_book.pack(side="left", fill="x", expand=True)
        self.cmb_book.bind("<<ComboboxSelected>>", lambda _: self._on_book_changed())
        # 空下拉框看不出该做什么——先摆一句行动指引，刷新出列表后被真数据顶掉
        self.cmb_book.set(BOOK_PLACEHOLDER)

        # 章节文件夹：路径 + 浏览
        row1 = ttk.Frame(frm_dir)
        row1.pack(fill="x", pady=(6, 0))
        self.dir_var = tk.StringVar()
        # 优先使用 config 中保存的路径，否则使用默认 chapters/
        DEFAULT_CHAPTERS_DIR.mkdir(exist_ok=True)
        saved_dir = self._cfg.get("chapters_dir", "")
        if saved_dir and Path(saved_dir).is_dir():
            self.dir_var.set(saved_dir)
        else:
            self.dir_var.set(str(DEFAULT_CHAPTERS_DIR))
            if saved_dir:  # 保存的路径已失效，更新内存配置
                self._cfg["chapters_dir"] = str(DEFAULT_CHAPTERS_DIR)
        # 图标按钮先靠右占位，路径框再填充中间——否则长路径会把按钮挤出卡片
        btn_opendir = ttk.Button(
            row1, text="📂", width=2, style="EmojiIcon.TButton",
            command=self._open_chapters_dir)
        btn_opendir.pack(side="right")
        self._attach_tooltip(btn_opendir, "在文件管理器中打开目录")
        self.btn_reload_chapters = ttk.Button(
            row1, text="↻", width=2, style="Icon.TButton",
            command=self._reload_chapters)
        self.btn_reload_chapters.pack(side="right", padx=(6, 6))
        self._attach_tooltip(self.btn_reload_chapters, "刷新（重新扫描章节文件夹）")
        # 存成属性：引导栏第 ③ 步的「下一步」按钮要指向它。
        # 存属性是为了转发（点引导栏那颗 = 点这颗），不是另写一套逻辑。
        self.btn_browse_dir = ttk.Button(
            row1, text="选择文件夹", command=self._on_browse_dir)
        self.btn_browse_dir.pack(side="right", padx=(6, 0))
        self.ent_dir = ttk.Entry(row1, textvariable=self.dir_var, state="readonly")
        self.ent_dir.pack(side="left", fill="x", expand=True)
        # 路径过长时默认只看得到盘符，作品名恰好在末尾——滚到尾部并挂全路径提示
        self.dir_var.trace_add("write", lambda *_: self._show_dir_tail())
        # 窗口变窄后可见区变小，重新滚到尾部，否则又只剩盘符
        self.ent_dir.bind("<Configure>", lambda _: self._show_dir_tail())
        self._attach_tooltip(self.ent_dir, lambda: self.dir_var.get())
        self._show_dir_tail()

        # 选项
        row_opt = ttk.Frame(frm_dir)
        row_opt.pack(fill="x", pady=(4, 0))
        self.unique_var = tk.BooleanVar(value=bool(self._cfg.get("auto_unique", True)))
        ttk.Checkbutton(
            row_opt, text="自动处理重名", variable=self.unique_var,
            command=self._apply_date_filter).pack(side="left")
        self.unique_var.trace_add("write", lambda *_: self._schedule_config_save())
        self.filter_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_opt, text="按修改日期筛选",
            variable=self.filter_var,
            command=self._on_filter_toggle).pack(side="left", padx=(16, 0))

        # 筛选选项行（勾选后展开）
        row_filter = ttk.Frame(frm_dir)
        # 输入控件与反馈分成两行：反馈跟输入框挤在同一行时，文字会被卡片右
        # 边缘裁掉（实测「格式错误，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM」只
        # 显示到「或 YYY」）。而两个格式写法正是用户此刻唯一需要的信息，
        # 裁掉等于没提示。反馈独占一行后整句都在。
        row_filter_in = ttk.Frame(row_filter)
        row_filter_in.pack(fill="x")
        self.filter_op_var = tk.StringVar(value="晚于")
        self.cmb_filter_op = ttk.Combobox(
            row_filter_in, textvariable=self.filter_op_var,
            values=["早于", "晚于"], width=5, state="readonly")
        self.cmb_filter_op.pack(side="left", padx=(6, 2))
        self.cmb_filter_op.bind("<<ComboboxSelected>>",
                                lambda _: self._apply_date_filter())
        self.filter_date_var = tk.StringVar(
            value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.ent_filter_date = ttk.Entry(
            row_filter_in, textvariable=self.filter_date_var, width=16)
        self.ent_filter_date.pack(side="left", padx=2)
        self.ent_filter_date.bind(
            "<FocusOut>", lambda _: self._apply_date_filter())
        self.ent_filter_date.bind(
            "<Return>", lambda _: self._apply_date_filter())
        # 命中数量比格式说明更该占这行剩余的宽度：格式已经写在输入框的默认值里，
        # 填错时 lbl_filter_info 会直接说明正确格式。此前那句常驻说明把命中数
        # 挤成一条几像素的色块。
        row_filter_info = ttk.Frame(row_filter)
        row_filter_info.pack(fill="x")
        self.lbl_filter_info = ttk.Label(row_filter_info, text="", foreground=CLR_INK_SOFT)
        self.lbl_filter_info.pack(side="left", padx=6)
        self._filter_row = row_filter

        # --- 4. 操作模式 ---
        card, frm_mode = self._card(right, "操作模式", "操作模式")
        card.pack(fill="x")

        _mode = self._cfg.get("default_mode", "schedule")
        if _mode not in MODE_SPEC:
            _mode = "schedule"  # 损坏/手改的 config 不应让单选组空选、模式分发错乱
        self.mode_var = tk.StringVar(value=_mode)
        self._mode_radios: list[ttk.Radiobutton] = []
        # 两行分组：上=发布新章，下=修改已发布章。字号加大突出这一核心决策。
        row_pub = ttk.Frame(frm_mode)
        row_pub.pack(fill="x", pady=(0, 2))
        ttk.Label(row_pub, text="发布", style="Cap.TLabel", width=4).pack(
            side="left")
        row_mod = ttk.Frame(frm_mode)
        row_mod.pack(fill="x", pady=(0, 4))
        ttk.Label(row_mod, text="修改", style="Cap.TLabel", width=4).pack(
            side="left")
        for val, (label, row) in MODE_SPEC.items():
            parent = row_pub if row == "pub" else row_mod
            rb = ttk.Radiobutton(
                parent, text=label, variable=self.mode_var,
                value=val, command=self._on_mode_change,
                style="Mode.TRadiobutton")
            rb.pack(side="left", padx=(0, 12))
            self._mode_radios.append(rb)

        # 发布选项（两行）。三个勾选 + 排期参数原先挤在两行里，窗口宽度低于
        # ~1300px 就装不下：实测 1000px 时「排到 N 天后」整组消失、
        # 「后台静默运行」被压到只剩勾选框（1366×768 笔记本开 125% 缩放
        # 得到的就是这个宽度）。现在每行最多两个勾选 + 一组排期参数。
        self._row_opts = row_opts = ttk.Frame(frm_mode)
        self._row_opts2 = row_opts2 = ttk.Frame(frm_mode)
        self.use_ai_var = tk.BooleanVar(value=bool(self._cfg.get("use_ai", False)))
        self.chk_use_ai = ttk.Checkbutton(
            row_opts, text="稿件使用了 AI 创作", variable=self.use_ai_var)
        self.chk_use_ai.pack(side="left", padx=6)
        self.use_ai_var.trace_add("write", lambda *_: self._schedule_config_save())

        # 自动接续队列：起始时刻和章号范围都不用自己算——接在平台队列末尾之后
        # 开始排，只发平台最大章号之后的章。勾上后起始日期输入框置灰。
        # （这就是原来独立的"续排发布"工具在 GUI 里该有的样子，不必另开入口）
        self.autocont_var = tk.BooleanVar(
            value=bool(self._cfg.get("auto_continue", False)))
        self.chk_autocont = ttk.Checkbutton(
            row_opts, text="接着上次往后排",
            variable=self.autocont_var, command=self._on_autocont_toggle)
        self.chk_autocont.pack(side="left", padx=6)
        self._attach_tooltip(self.chk_autocont, "不用自己填起始日期、也不用挑哪几章：\n工具去平台看已经排到哪一刻，先填满那天剩下的时间点，\n再逐日往后排，并且只发平台上还没有的章。\n\n→ 攒了一批新稿，想直接接在现有排期后面时用。\n→ 要自己定日期、或只补某几章，就别勾。")
        self.autocont_var.trace_add("write", lambda *_: self._schedule_config_save())

        # 无头模式：不弹浏览器窗口，后台静默跑。与 CLI 的 --headless / config
        # 的 headless 同一个开关（写回 config.json，两边一致）。挂第二行：
        # 它是所有模式共用的开关，所以第二行也一直显示。
        self.headless_var = tk.BooleanVar(value=bool(self._cfg.get("headless", False)))
        self.chk_headless = ttk.Checkbutton(
            row_opts2, text="后台静默运行", variable=self.headless_var)
        self.chk_headless.pack(side="left", padx=6)
        self.headless_var.trace_add("write", lambda *_: self._schedule_config_save())
        # 「排到 N 天后」挂在选项第二行（挨着「后台静默运行」），不挂起始日期行：
        # 起始日期行已经有两组控件，窄窗口下第一个被挤掉的就是它。
        # （「接着上次往后排」在选项第一行——它和「排到 N 天后」是同一件事的
        #   两个旋钮，但一行塞不下三组控件，所以分了两行。）
        self.days_ahead_var = tk.StringVar(
            value=str(self._cfg.get("days_ahead") or ""))

        # 上次发布信息（所有模式可见）。留在右栏：撑高这块的是左栏
        # （作品与章节 + 定时执行 两张卡），右栏本来就有富余，挪去左栏
        # 反而白吃掉章节预览的一行高度。
        self.lbl_last_publish = ttk.Label(
            frm_mode, text="队列排到：选好作品后自动获取", foreground=CLR_INK_SOFT)
        self.lbl_last_publish.pack(fill="x", padx=12, pady=(0, 4))

        # 卷选择器 + 合并所有卷（同一行，仅 edit/reschedule 模式 + 多卷时显示）
        self._volume_frame = ttk.Frame(frm_mode)
        self.all_volumes_var = tk.BooleanVar(value=False)
        self.chk_all_volumes = ttk.Checkbutton(
            self._volume_frame, text="合并所有卷",
            variable=self.all_volumes_var,
            command=self._on_all_volumes_changed)
        self.chk_all_volumes.pack(side="left", padx=(12, 4))
        self._lbl_volume_sep = ttk.Label(
            self._volume_frame, text="选择分卷:")
        self._lbl_volume_sep.pack(side="left", padx=(12, 0))
        self.volume_var = tk.StringVar()
        self.cmb_volume = ttk.Combobox(
            self._volume_frame, textvariable=self.volume_var,
            state="readonly", width=28)
        self.cmb_volume.pack(side="left", padx=2, pady=4)
        self.cmb_volume.bind("<<ComboboxSelected>>",
                             lambda _: self._on_volume_changed())

        # 定时发布设置子面板
        self.sched_frame = ttk.Frame(frm_mode)

        # Row 1: 日期 + 每天章数
        r1 = ttk.Frame(self.sched_frame)
        r1.pack(fill="x", padx=6, pady=1)
        ttk.Label(r1, text="起始日期:").pack(side="left")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.date_var = tk.StringVar(value=tomorrow)
        self.ent_date = ttk.Entry(r1, textvariable=self.date_var, width=14)
        self.ent_date.pack(side="left", padx=4)
        ttk.Label(r1, text="每天章数:").pack(side="left", padx=(20, 0))
        _pd = self._cfg.get("default_per_day", 2)
        if (not isinstance(_pd, int) or isinstance(_pd, bool)
                or not (PER_DAY_MIN <= _pd <= PER_DAY_MAX)):
            _pd = 2  # 非整数/越界会让 tk.IntVar 在构造时抛 TclError、整个 GUI 起不来
        self.perday_var = tk.IntVar(value=_pd)
        ttk.Spinbox(
            r1, from_=PER_DAY_MIN, to=PER_DAY_MAX, textvariable=self.perday_var,
            width=4).pack(side="left", padx=4)
        # 「排到」：留空=把本地剩下的全排上去；填 N=只排到「今天+N 天」。
        # 只排近几天便于改稿（排到几个月后的章想改剧情就得去平台一章章改），
        # 同时仍留着断更保护。与 CLI 的 --days-ahead 对应。仅定时发布模式显示。
        # 挂在选项第二行（挨着「后台静默运行」），不挂起始日期行：起始日期行
        # 已经有两组控件，窄窗口下第一个被挤掉的就是它。
        self.lbl_days_ahead = ttk.Label(row_opts2, text="排到:")
        self.ent_days_ahead = ttk.Entry(row_opts2, textvariable=self.days_ahead_var,
                                        width=3)
        self.lbl_days_ahead.pack(side="left", padx=(14, 0))
        self.ent_days_ahead.pack(side="left", padx=4)
        self.lbl_days_ahead_unit = ttk.Label(row_opts2, text="天后")
        self.lbl_days_ahead_unit.pack(side="left")
        for _w in (self.lbl_days_ahead, self.ent_days_ahead,
                   self.lbl_days_ahead_unit):
            self._attach_tooltip(_w, "留空 = 把本地剩下的全排上去。\n填 7 = 只排到 7 天后为止，剩下的下次再排。\n\n排得近，之后想改剧情只改本地文件就行；\n排到几个月后的章，想改得去平台一章章改。")
        self.days_ahead_var.trace_add("write",
                                      lambda *_: self._schedule_config_save())

        # Row 2: 时间（支持多个时间，逗号分隔）
        r2 = ttk.Frame(self.sched_frame)
        r2.pack(fill="x", padx=6, pady=1)
        ttk.Label(r2, text="发布时间:").pack(side="left")
        self.time_var = tk.StringVar(value=self._cfg.get("default_time", "08:00"))
        ttk.Entry(r2, textvariable=self.time_var, width=22).pack(
            side="left", padx=4)
        ttk.Label(r2, text="逗号分隔",
                  style="Cap.TLabel").pack(side="left")

        # 初始模式的面板可见性由 _on_mode_change 统一处理（在所有组件创建后调用）

        # 参数变化时刷新预览
        for var in (self.date_var, self.perday_var, self.time_var):
            var.trace_add("write", lambda *_: self._refresh_preview())
        # 时间点数量 > 每天章数时，自动上调 per_day
        self.time_var.trace_add("write", lambda *_: self._sync_perday_from_times())
        # 持久化可配置项（不含日期，日期每天变化）
        for var in (self.perday_var, self.time_var):
            var.trace_add("write", lambda *_: self._schedule_config_save())
        # 启动时同步: config 可能有 per_day=2 但 times=3 个
        self._sync_perday_from_times()

        # 章节序号筛选
        self.resched_filter_var = tk.BooleanVar(
            value=bool(self._cfg.get("resched_filter_on", False)))
        self._resched_filter_row = ttk.Frame(frm_mode)
        ttk.Checkbutton(
            self._resched_filter_row, text="按章节号筛选",
            variable=self.resched_filter_var,
            command=self._refresh_preview).pack(side="left", padx=(12, 4))
        # 损坏/手改的 config 可能给出非法运算符，readonly 下拉会显示空白，回退默认
        _saved_op = self._cfg.get("resched_filter_op", "≥")
        if _saved_op not in ("≤", "≥"):
            _saved_op = "≥"
        self.resched_filter_op_var = tk.StringVar(value=_saved_op)
        self.cmb_resched_filter_op = ttk.Combobox(
            self._resched_filter_row, textvariable=self.resched_filter_op_var,
            values=["≤", "≥"], width=3, state="readonly")
        self.cmb_resched_filter_op.pack(side="left", padx=2)
        self.cmb_resched_filter_op.bind("<<ComboboxSelected>>",
                                        lambda _: self._refresh_preview())
        ttk.Label(self._resched_filter_row, text="第").pack(side="left", padx=(4, 0))
        self.resched_filter_num_var = tk.StringVar(
            value=str(self._cfg.get("resched_filter_num", "1")))
        # 宽度 12：框只宽 8 时，「4783-4783」这种 9 位区间会被裁成
        # 「783-4783」——框里显示的数和程序实际用的不是同一个数，照着框里的
        # 再敲一遍就变成另一段章节。
        self.ent_resched_filter_num = ttk.Entry(
            self._resched_filter_row, textvariable=self.resched_filter_num_var, width=12)
        self.ent_resched_filter_num.pack(side="left", padx=2)
        ttk.Label(self._resched_filter_row, text="章").pack(side="left")
        self.resched_filter_num_var.trace_add("write", lambda *_: self._refresh_preview())
        # 持久化筛选设置（防抖写盘）
        for _v in (self.resched_filter_var, self.resched_filter_op_var,
                   self.resched_filter_num_var):
            _v.trace_add("write", lambda *_: self._schedule_config_save())
        self.ent_resched_filter_num.bind("<Return>", lambda _: self.txt_preview.focus_set())
        # 反馈独占一行（理由同按修改日期筛选那一处）：跟输入框挤在一行时，
        # 「请输入数字、范围或组合(如 1,3,5-10)」只显示到「(如」为止，
        # 格式示例正好被卡片右边缘裁掉。平时没有反馈，这一行不占高度。
        self._resched_info_row = ttk.Frame(frm_mode)
        self.lbl_resched_filter_info = ttk.Label(
            self._resched_info_row, text="", foreground=CLR_INK_SOFT)
        self.lbl_resched_filter_info.pack(side="left", padx=6)

        # --- 4.5 定时执行 ---
        card, frm_timer = self._card(left, "定时执行", "定时执行", sched=True)
        card.pack(fill="x", pady=(6, 0))
        row_timer = ttk.Frame(frm_timer)
        row_timer.pack(fill="x")
        ttk.Label(row_timer, text="执行时间:").pack(side="left")
        # 回填上次时间；若已过期或无效，回退到当前时间 +1 小时
        _saved_timer = self._parse_timer_input(self._cfg.get("timer_time", ""))
        if _saved_timer is None or _saved_timer <= datetime.now():
            default_timer = (datetime.now() + timedelta(hours=1)).strftime(
                "%Y-%m-%d %H:%M")
        else:
            default_timer = _saved_timer.strftime("%Y-%m-%d %H:%M")
        self.timer_time_var = tk.StringVar(value=default_timer)
        self.ent_timer = ttk.Entry(
            row_timer, textvariable=self.timer_time_var, width=16)
        self.ent_timer.pack(side="left", padx=(6, 8))
        self.btn_timer = ttk.Button(
            row_timer, text="启动定时", command=self._toggle_timer)
        self.btn_timer.pack(side="left", padx=(0, 8))
        # 状态与说明同挂第二行：窄窗口下第一行放不下「输入框+按钮+状态」，
        # 状态会被裁成半截（此前 1060px 宽时只剩「定」字）。
        row_timer2 = ttk.Frame(frm_timer)
        row_timer2.pack(fill="x", pady=(3, 0))
        self.lbl_timer_status = ttk.Label(
            row_timer2, text="未启动", foreground=CLR_INK_SOFT)
        self.lbl_timer_status.pack(side="left")
        ttk.Label(row_timer2, style="Cap.TLabel",
                  text="· 到点自动执行一次").pack(
                      side="left", padx=(6, 0))

        # --- 5. 章节预览 / 运行日志（选项卡） ---
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(side="top", fill="both", expand=True, padx=12, pady=(5, 0))
        tab_prev = ttk.Frame(self._nb)
        self._nb.add(tab_prev, text="章节预览")
        self.txt_preview = scrolledtext.ScrolledText(
            tab_prev, height=11, state="disabled", wrap="none",
            font=("Consolas", 10), background=CLR_FIELD, foreground=CLR_INK,
            insertbackground=CLR_INK, relief="flat", borderwidth=0)
        self.txt_preview.pack(fill="both", expand=True, padx=1, pady=6)

        tab_log = ttk.Frame(self._nb)
        self._nb.add(tab_log, text="运行日志")
        self.txt_log = scrolledtext.ScrolledText(
            tab_log, height=10, state="disabled",
            font=("Consolas", 10), background=CLR_FIELD, foreground=CLR_INK,
            insertbackground=CLR_INK, relief="flat", borderwidth=0)
        self.txt_log.pack(fill="both", expand=True, padx=1, pady=(6, 0))
        # 导出按钮放在选项卡条右端的空白处：此前浮在日志正文右上角，长日志行会被
        # 它盖住一截。选项卡条那一行本来就是空的，白拿一个不遮挡的位置。
        self.btn_export = ttk.Button(
            self._nb, text="导出日志", style="TabAction.TButton",
            command=self._export_log)
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._on_tab_changed()

        # 所有组件创建完毕，统一设置初始模式的面板可见性
        self._on_mode_change()

        # 启动时自动加载预览
        if self.dir_var.get():
            self.root.after(100, self._reload_chapters)

    def _build_guidance_bar(self):
        """顶部新手引导：步骤轨 + 下一步提示 + 一颗可点的「下一步」按钮，可收起且记住。"""
        ff = self._ff
        self._guide_frame = ttk.Frame(self.root, style="Card.TFrame")
        top = ttk.Frame(self._guide_frame)
        top.pack(fill="x", padx=12, pady=(7, 1))
        self._step_labels = []
        for i, name in enumerate(ONBOARD_STEP_NAMES):
            lbl = ttk.Label(top, text=f"{_CIRCLED[i]} {name}", font=(ff, 10))
            lbl.pack(side="left")
            self._step_labels.append(lbl)
            if i < len(ONBOARD_STEP_NAMES) - 1:
                ttk.Label(top, text="───▸", foreground=CLR_HAIRLINE).pack(
                    side="left", padx=8)
        collapse = ttk.Label(top, text="收起 ✕", style="Cap.TLabel",
                             cursor="hand2")
        collapse.pack(side="right")
        collapse.bind("<Button-1>",
                      lambda _: self._set_guidance_collapsed(True))
        # 下一步 = 一句提示 + 一颗能点的按钮。提示语负责说「该做什么」，
        # 按钮负责把它做掉。以前只有提示语，还点名道姓地写「点「开始上传」」——
        # 而那颗按钮在窗口最底下，中间隔着一整屏，新用户根本不知道指哪一颗。
        row_next = ttk.Frame(self._guide_frame)
        row_next.pack(fill="x", padx=12, pady=(0, 7))
        # 先 pack 按钮、再 pack 提示语：pack 按调用顺序分配空间，按钮在右、
        # 提示语在左并吃掉剩余宽度。顺序反了的话提示语一长，按钮会被挤没。
        self.btn_guide_cta = ttk.Button(
            row_next, text="下一步", style="Guide.TButton",
            command=self._on_guide_cta)
        self.btn_guide_cta.pack(side="right")
        self._attach_tooltip(
            self.btn_guide_cta,
            "就做当前这一步：按钮上的字会随进度变（登录 → 刷新作品 → "
            "选文件夹 → 开始上传）。\n点它等于点了界面上那颗真按钮，"
            "不是另走一套逻辑。")
        self.lbl_next_step = ttk.Label(
            row_next, text="", foreground=CLR_SCHED_D, font=(ff, 9))
        self.lbl_next_step.pack(side="left", fill="x", expand=True)

        # 收起后的细长再入口：左边一个链接（点开引导栏）+ 右边那颗「下一步」。
        self._guide_stub = ttk.Frame(self.root)
        link = ttk.Label(self._guide_stub, text="▸ 新手引导",
                         foreground=CLR_TOMATO_D, cursor="hand2",
                         font=(ff, 9, "underline"))
        link.pack(side="left", padx=4)
        link.bind("<Button-1>", lambda _: self._set_guidance_collapsed(False))
        # 收起状态下同样留一颗「下一步」按钮：矮屏（1366×768 的笔记本）启动
        # 就把引导栏收起了，只剩一句「▸ 新手引导」的话，新用户连一个能点的
        # 下一步都没有——那正是这个按钮要解决的事。
        # 用小一档的样式（GuideSlim）：这条本来就只有一行，用大按钮会把细条
        # 撑高十几像素，而那十几像素是从章节预览区里抢的。
        self.btn_guide_cta_stub = ttk.Button(
            self._guide_stub, text="下一步", style="GuideSlim.TButton",
            command=self._on_guide_cta)
        self.btn_guide_cta_stub.pack(side="right", padx=4)
        # 两处入口合起来是一颗「下一步」：同步时一起更新（见 _sync_guide_cta）
        self._guide_ctas = [self.btn_guide_cta, self.btn_guide_cta_stub]

        # 按记忆决定初始展开/收起；从未表态过时按屏幕高度定默认——引导栏占
        # GUIDE_H_PX px，矮屏上展开它，下方章节预览就只剩两三行。
        _collapsed = self._gui_state.get("guidance_collapsed")
        if _collapsed is None:
            # 没人表态过 → 是「屏幕太矮」才收的，不是「用户要收起」。记成
            # 自动收起：窗口之后高过 GUIDE_EXPAND_H 就让它自己回来（和「用户把窗口
            # 拖矮」那条路径一个口径）。
            # 注意矮屏上这条兑现不了：768px 的屏最多把窗口拉到 ~730px，
            # 够不到 GUIDE_EXPAND_H 的展开线——那种屏上引导栏就保持收起，用户点
            # 「▸ 新手引导」随时能打开。记这个号是为了「拖矮」和「启动就矮」
            # 两条路径口径一致，不是承诺矮屏也能自动展开。
            _collapsed = self.root.winfo_screenheight() < GUIDE_SCREEN_SHORT_H
            self._guide_auto = _collapsed
        if _collapsed:
            self._guide_stub.pack(fill="x", padx=12, pady=(6, 0))
        else:
            self._guide_frame.pack(fill="x", padx=12, pady=(6, 0))
        # 窗口被拖矮时自动收起引导栏（把高度让给章节预览）
        self.root.bind("<Configure>", self._fit_guidance_to_height)

    # -----------------------------------------------------------------------
    # 缓存管理
    # -----------------------------------------------------------------------
    def _invalidate_caches(self, scope: str = "all"):
        """清除缓存。

        scope:
            "all"      — 全部清除（切换账号、刷新作品列表时调用）
            "chapters" — 仅清除章节/发布缓存，保留卷结构（上传完成后调用）
        """
        self._last_publish_cache.clear()
        self._platform_chapters_cache.clear()
        if scope == "all":
            self._volumes_cache.clear()

    # -----------------------------------------------------------------------
    # UI 辅助
    # -----------------------------------------------------------------------
    def _after(self, ms, func, *args):
        """安全的 root.after 调用，窗口关闭后不再调度。"""
        if self._closing:
            return
        try:
            self.root.after(ms, func, *args)
        except (tk.TclError, RuntimeError):
            # destroy() 后跨线程调度可能抛 RuntimeError("main thread is not in
            # main loop") 而非 TclError——_closing 只是快速路径，真正兜底靠这里
            pass

    def _on_tab_changed(self, event=None):
        """「导出日志」只在运行日志页出现——在章节预览页它无事可做。"""
        if self._nb.index("current") == 1:
            self.btn_export.place(relx=1.0, y=3, x=-4, anchor="ne")
            self.btn_export.lift()
        else:
            self.btn_export.place_forget()

    def _show_dir_tail(self):
        """路径框滚到末尾：长路径里有辨识度的是尾部的作品名，头部盘符没有信息量。"""
        self._after(0, lambda: self.ent_dir.xview_moveto(1.0))

    def _refresh_auth_status(self):
        acct = self._gui_state.get("current_account", "")
        # L1: 命名文件已被删除 → 清除残留记录
        if acct and not (SCRIPT_DIR / f".auth_{acct}.json").exists():
            self._gui_state.pop("current_account", None)
            self._save_gui_state()
            acct = ""
        if AUTH_FILE.exists():
            if acct:
                self.lbl_auth.configure(
                    text="● 已登录", foreground=CLR_OK)
            else:
                self.lbl_auth.configure(
                    text="● 已登录", foreground=CLR_OK)
        else:
            self.lbl_auth.configure(text="○ 未登录", foreground=CLR_WARN)
        self._update_guidance()

    # -----------------------------------------------------------------------
    # 新手引导
    # -----------------------------------------------------------------------
    @staticmethod
    def _compute_onboarding_step(logged_in: bool, has_book: bool,
                                 has_chapters: bool) -> int:
        """纯函数：当前状态对应新手引导第几步（1..4）。

        顺序门槛：登录 → 选作品 → 选章节 → 上传/修改。前一道未过，步号不前进。
        """
        if not logged_in:
            return 1
        if not has_book:
            return 2
        if not has_chapters:
            return 3
        return 4

    def _onboarding_state(self):
        """读取当前真实状态（对尚未构建的控件做容错）。"""
        logged_in = AUTH_FILE.exists()
        cmb = getattr(self, "cmb_book", None)
        has_book = bool(self.books) and cmb is not None and cmb.current() >= 0
        mode_var = getattr(self, "mode_var", None)
        mode = mode_var.get() if mode_var is not None else ""
        if mode == "reschedule":
            # 修改排期不依赖本地章节文件夹（操作的是平台章节），选好作品即就绪，
            # 否则会一直卡在第③步误导用户去选不需要的文件夹
            has_chapters = has_book
        else:
            has_chapters = bool(self.files)
        return logged_in, has_book, has_chapters

    def _update_guidance(self):
        """按当前状态刷新引导栏的步骤高亮、「下一步」提示与那颗按钮。"""
        if not getattr(self, "_step_labels", None):
            self._sync_guide_cta()   # 步骤轨还没建出来，但按钮状态仍要跟上
            return
        step = self._compute_onboarding_step(*self._onboarding_state())
        ff = self._ff
        for i, lbl in enumerate(self._step_labels, start=1):
            name = ONBOARD_STEP_NAMES[i - 1]
            if i < step:                       # 已完成
                lbl.configure(text=f"✓ {name}", foreground=CLR_OK,
                              font=(ff, 10))
            elif i == step:                    # 当前
                lbl.configure(text=f"{_CIRCLED[i-1]} {name}",
                              foreground=CLR_TOMATO, font=(ff, 10, "bold"))
            else:                              # 未到
                lbl.configure(text=f"{_CIRCLED[i-1]} {name}",
                              foreground=CLR_INK_SOFT, font=(ff, 10))
        self.lbl_next_step.configure(text=self._guide_hint(step))
        self._sync_guide_cta()

    def _guide_hint(self, step):
        """「下一步」那行提示语。跟着运行状态走，别在屏幕上留过期的话。

        任务进行中时底栏主按钮已经变成「停止」，提示语却还在说
        「点开始上传」——用户照着提示点下去，把自己正在跑的活给停了。
        """
        if self.uploading:
            return "进行中：下方「运行日志」有逐条进度；想中断点「停止」。"
        if step == 4 and self._upload_btn_disabled():
            # 修改内容/修改排期要平台的章节表才动得了：按钮灰着就说清在等什么
            return ("下一步：正在取平台章节列表（修改类操作要用它），"
                    "取回后按钮就能点了。")
        return STEP_HINTS.get(step, "")

    def _upload_btn_disabled(self):
        """主按钮当前是不是灰的。问按钮本身，不另判一套。"""
        btn = getattr(self, "btn_upload", None)
        if btn is None:
            return False
        try:
            return str(btn.cget("state")) == "disabled"
        except tk.TclError:
            return False

    def _sync_guide_cta(self):
        """把引导栏的「下一步」按钮同步成界面上那颗真按钮的样子。

        同步的只有两样：文字、能不能点。两样都取自目标按钮本身——
        引导栏不另判一套「能不能做」。两套判据一旦对不上，用户看到的就是
        「引导栏说能点、点了却弹『未登录』」，比没有引导更劝退。

        两处（展开的引导栏、收起后的细条）都要同步：它们是同一颗按钮的
        两个入口，矮屏下用户看到的是收起那一条。
        """
        ctas = getattr(self, "_guide_ctas", None)
        if not ctas:
            return
        step = self._compute_onboarding_step(*self._onboarding_state())
        spec = GUIDE_CTA.get(step)
        if spec is None:
            for cta in ctas:
                cta.configure(text="下一步", state="disabled")
            return
        label = spec[1]
        target = getattr(self, spec[0], None)
        if target is None:
            # 目标控件还没建出来（界面搭建中）：先摆着，但不许点
            for cta in ctas:
                cta.configure(text=label or "下一步", state="disabled")
            return
        try:
            text = str(target.cget("text"))
            state = str(target.cget("state"))
        except tk.TclError:
            return
        for cta in ctas:
            cta.configure(text=label if label is not None else text,
                          state="disabled" if state == "disabled" else "normal")

    def _on_guide_cta(self):
        """引导栏「下一步」按钮：转发给界面上那颗真按钮。

        不在这里重做一遍「该不该做、怎么做」——直接 invoke 目标按钮，
        于是引导栏和界面上那颗永远是同一件事、同一套前置校验。
        """
        step = self._compute_onboarding_step(*self._onboarding_state())
        spec = GUIDE_CTA.get(step)
        if not spec:
            return
        target = getattr(self, spec[0], None)
        if target is None:
            return
        try:
            target.invoke()
        except tk.TclError:
            pass

    def _set_guidance_collapsed(self, collapsed: bool, user: bool = True):
        """收起/展开引导栏。

        user=True 是用户点的（记住选择并写盘）；user=False 是窗口太矮时
        自动收起的——这种不写盘，免得把用户在大屏上留下的展开状态改掉。
        """
        if user:
            self._gui_state["guidance_collapsed"] = collapsed
            self._save_gui_state()
            self._guide_user_set = True
            self._guide_auto = False
        else:
            self._guide_auto = collapsed
        if collapsed:
            self._guide_frame.pack_forget()
            self._guide_stub.pack(fill="x", padx=12, pady=(6, 0),
                                  before=self._acct_frame)
        else:
            self._guide_stub.pack_forget()
            self._guide_frame.pack(fill="x", padx=12, pady=(6, 0),
                                   before=self._acct_frame)
            self._update_guidance()

    def _fit_guidance_to_height(self, event=None):
        """窗口被拖矮时自动收起引导栏，把高度让给章节预览。

        引导栏是可选内容（展开约 GUIDE_H_PX px，收起后那条细条只有
        GUIDE_STUB_PX px），而下方章节预览是不可逆操作前唯一的复核
        环节——窗口不够高时它会被挤成一条缝（实测 680px 高、引导栏展开时
        预览区只剩 10px，连「总计 N 章」都看不见）。屏幕矮时启动就已收起
        （见 _build_guidance_bar），这里补上「用户自己把窗口拖矮」这一种：
        缩小窗口不该换来一个看不见的预览区。

        用户手动收起/展开过就不再自动干预——那是他的选择。

        只认主窗口自己的 Configure：Tk 的 bindtags 含 toplevel，子控件重排
        也会把 <Configure> 递到主窗口（实测子控件的 bindtags 是
        ('.!label','Label','.','all')），不加这层过滤的话每次重绘都会跑
        一次这里、还会顺手改几何管理器。改几何用 after(0) 推迟一轮事件循环
        （self._after 的零延迟定时器），免得在布局过程中重排。
        """
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        h = self.root.winfo_height()
        if h <= 0 or self._guide_user_set:
            return
        if not hasattr(self, "_acct_frame"):
            return          # 界面还没建完（首帧 Configure），先不管
        # 用 winfo_manager() 判「引导栏现在是不是 pack 着」，不用
        # winfo_ismapped()：后者要窗口已映射才算 1，窗口还没显示/最小化时
        # 一律返回 0，于是「窗口太矮该收起」这条会平白失效。
        packed = bool(self._guide_frame.winfo_manager())
        if h < GUIDE_COLLAPSE_H and packed:
            self._after(0, self._collapse_guide_if_still_short)
        elif (h >= GUIDE_EXPAND_H and self._guide_auto and not packed):
            self._after(0, self._expand_guide_if_still_tall)

    def _collapse_guide_if_still_short(self):
        """推迟一轮之后**再核对一次**才收。

        决策是按当时的窗口高度做的，等真执行时窗口可能已经又变高了（一次
        快速拖拽会连着来好几个 Configure）。照旧执行就会把引导栏收在一个
        高窗口上，而且收栏本身不改变窗口高度，不会再有 Configure 来纠正它
        ——「下一步」那句话就这么没了。
        """
        if self._guide_user_set:
            return          # 用户刚自己点过，别替他决定
        if (self.root.winfo_height() < GUIDE_COLLAPSE_H
                and self._guide_frame.winfo_manager()):
            self._set_guidance_collapsed(True, False)

    def _expand_guide_if_still_tall(self):
        """同上：窗口又变矮了就别展开（那正是要避免的挤占）。"""
        if self._guide_user_set:
            return
        if (self.root.winfo_height() >= GUIDE_EXPAND_H and self._guide_auto
                and not self._guide_frame.winfo_manager()):
            self._set_guidance_collapsed(False, False)

    def _section_help(self, key: str, event=None):
        """弹出区块帮助。用自定义小窗而非 messagebox：

        messagebox.showinfo 在 Windows 上会触发系统提示音，对纯参考文本是多余的；
        自定义 Toplevel 无声、出现在鼠标旁、Esc/按钮即关，观感更像贴士卡片。
        """
        win = tk.Toplevel(self.root)
        win.title(f"帮助 · {key}")
        win.resizable(False, False)
        win.transient(self.root)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=key, font=(self._ff, 10, "bold")).pack(anchor="w")
        ttk.Label(frm, text=SECTION_HELP.get(key, ""), wraplength=480,
                  justify="left").pack(anchor="w", pady=(4, 8))
        ttk.Button(frm, text="知道了", command=win.destroy).pack(anchor="e")
        win.bind("<Escape>", lambda _: win.destroy())
        # 出现在鼠标点击处旁边，并夹到屏幕内（右对齐的 (?) 贴近边缘，防溢出）
        win.update_idletasks()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = (event.x_root + 12) if event is not None else self.root.winfo_pointerx()
        y = (event.y_root + 12) if event is not None else self.root.winfo_pointery()
        x = max(8, min(x, sw - ww - 8))
        y = max(8, min(y, sh - wh - 8))
        win.geometry(f"+{x}+{y}")
        win.focus_set()

    def _add_help(self, parent, key: str, side="right"):
        """在某区块行内放一个 (?) 帮助入口。"""
        lbl = ttk.Label(parent, text="(?)", foreground=CLR_INK_SOFT,
                        cursor="hand2")
        lbl.pack(side=side, padx=6)
        lbl.bind("<Button-1>", lambda e: self._section_help(key, e))
        return lbl

    def _show_welcome(self):
        """首次/手动触发的总览弹窗。"""
        # 防重入：已有一个欢迎弹窗时不再叠开
        existing = getattr(self, "_welcome_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        win = tk.Toplevel(self.root)
        self._welcome_win = win
        win.title("新手引导")
        win.resizable(False, False)
        win.transient(self.root)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="欢迎使用番茄作家连载发布工作台",
                  font=(self._ff, 13, "bold")).pack(anchor="w", pady=(0, 8))
        steps = [
            "① 登录：点「登录 / 新建」，在弹出的浏览器里登录番茄账号。",
            "② 选作品：点作品行右侧的「↻」刷新列表，选择一部作品。",
            "③ 选章节文件夹：默认 chapters/，每个 .md 或 .txt（纯文本）文件是一章。",
            "④ 上传/修改：选好「操作模式」后点「开始上传」。",
        ]
        for s in steps:
            ttk.Label(body, text=s, font=(self._ff, 10)).pack(anchor="w", pady=1)
        ttk.Label(
            body,
            text="顶部「新手引导」那行右边有颗「下一步」按钮，"
                 "它会跟着进度变成当前该做的那一步（登录 → 刷新作品 → "
                 "选文件夹 → 开始上传），点它就行。",
            font=(self._ff, 9), foreground=CLR_TOMATO_D, wraplength=420,
            justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(
            body,
            text="窗口最底下那条行动条：左边红底的是「开始上传」（跑起来后变成"
                 "「停止」）；竖线之后那四个是出问题时才用的工具——"
                 "先点「检查缺口」查一遍，报告里会直接写明该用后面哪一个。",
            font=(self._ff, 9), foreground=CLR_TOMATO_D, wraplength=420,
            justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Label(
            body,
            text="提示：登录框里填的是「本地账号名称」（用于区分多个账号），"
                 "不是番茄笔名；真正的登录在浏览器里完成。",
            font=(self._ff, 9), foreground=CLR_SCHED_D, wraplength=420,
            justify="left").pack(anchor="w", pady=(10, 12))
        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x")
        lbl_doc = ttk.Label(btn_row, text="查看完整说明 (GitHub)",
                            foreground=CLR_TOMATO_D, cursor="hand2",
                            font=(self._ff, 9, "underline"))
        lbl_doc.pack(side="left")
        lbl_doc.bind("<Button-1>", lambda _: webbrowser.open(GH_URL))
        ttk.Button(btn_row, text="开始使用", command=win.destroy).pack(
            side="right")
        self._place_over_parent(win)

    # -----------------------------------------------------------------------
    # 多账号管理
    # -----------------------------------------------------------------------
    @staticmethod
    def _sanitize_account_name(name: str) -> str:
        """移除 Windows 文件名非法字符，返回清理后的名称。"""
        # Windows 文件名禁止字符: \ / : * ? " < > |
        cleaned = re.sub(r'[\\/:*?"<>|]', "", name).strip()
        # 防止与活跃 auth 文件冲突
        if cleaned.lower() == "state":
            cleaned = ""
        return cleaned

    @staticmethod
    def _copy_auth(src, dst):
        """把登录状态文件复制一份，新文件的权限只有本人可读。

        不用 shutil.copy2：它会把**源文件**的权限位一并盖到目标上（源是老
        账号文件、默认 0644 时，刚加固过的 .auth_state.json 就被降回 0644，
        同机别的用户又能读走 cookie），而且它是「先按源的权限建出目标、
        再把内容写进去」——内容落盘的那一瞬间，目标还是宽的。

        这里写 tmp（按 0600 建）再 os.replace 换名：换名换的是目录项，目标
        是个符号链接时换掉的是链接本身，不会顺着链接把 cookie 写到别处；
        中途崩了也不会把用户当前的登录状态写坏。
        """
        with open(src, "rb") as f:
            data = f.read()
        # 写 tmp 再换名，而不是直接 O_TRUNC 目标（两条理由见上面的 docstring）。
        tmp = dst.with_name(dst.name + ".tmp")
        if os.path.islink(tmp):
            os.unlink(tmp)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        flags |= getattr(os, "O_NOFOLLOW", 0)   # POSIX 上不跟符号链接
        fd = os.open(str(tmp), flags, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        # 换名在 Windows 上可能被实时扫描短暂挡住（WinError 5）：最多试 3 次，
        # 前两次失败各等 50ms 再换。
        for _attempt in range(3):
            try:
                os.replace(str(tmp), str(dst))
                break
            except PermissionError:
                if _attempt == 2:
                    raise
                time.sleep(0.05)

    def _scan_accounts(self) -> list[str]:
        """扫描 SCRIPT_DIR 下所有 .auth_*.json，返回账号名称列表。"""
        names: list[str] = []
        for p in sorted(SCRIPT_DIR.glob(".auth_*.json")):
            fn = p.name                     # .auth_作家A.json
            # 顺手把权限收一遍：这些文件是「只给自己看的登录 cookie」，
            # 但老版本写出来时用的是默认权限（POSIX 上 0644），同机别的
            # 用户能读走就等于账号被接管。Windows 上 chmod 不起作用
            # （靠目录 ACL），失败就算了，不影响功能。
            # .auth_state.json（正在用的那个）也要收——它恰恰是同机别的
            # 用户最想读的那一份，不能因为「它不是账号文件」就放过。
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            if fn == ".auth_state.json":
                continue
            # 提取名称: 去掉 ".auth_" 前缀和 ".json" 后缀
            name = fn[len(".auth_"):-len(".json")]
            if name:
                names.append(name)
        return names

    def _refresh_account_list(self):
        """刷新 combobox 的账号列表，并恢复之前的选中项。"""
        names = self._scan_accounts()
        values = names + ["(新建)"]
        self.cmb_account["values"] = values

        current = self._gui_state.get("current_account", "")
        if current and current in names:
            self.cmb_account.set(current)
        elif names:
            # 当前账号不在列表中 — 不自动选中，留空
            pass
        # 如果没有任何命名账号且无 current，combobox 自然留空

    def _on_account_selected(self, event=None):
        """Combobox 选中事件。"""
        selected = self.account_var.get()
        if selected == "(新建)":
            # 回退选中值（避免 combobox 停留在 "(新建)"）
            prev = self._gui_state.get("current_account", "")
            names = self._scan_accounts()
            if prev and prev in names:
                self.cmb_account.set(prev)
            elif names:
                self.cmb_account.set(names[0])
            else:
                self.cmb_account.set("")
            # 触发登录流程
            self._on_login()
            return
        # 跳过：已是当前账号
        if selected == self._gui_state.get("current_account", ""):
            return
        # 切换到选中的账号
        if selected:
            self._switch_account(selected)

    def _switch_account(self, name: str):
        """切换到指定的命名账号: 复制 auth 文件 → 刷新浏览器 + 作品列表。"""
        if self._login_in_progress:
            # 登录期间切换账号会 _copy_auth 覆盖 AUTH_FILE，与登录保存的会话争用，
            # 可能把错账号 cookie 写进命名文件。回退下拉到当前账号、待登录结束。
            messagebox.showinfo("登录进行中", "先完成或取消当前登录，再切换账号。")
            self.cmb_account.set(self._gui_state.get("current_account", ""))
            return
        src = SCRIPT_DIR / f".auth_{name}.json"
        if not src.exists():
            messagebox.showerror("账号文件丢失",
                                 f"找不到 {src.name}，它可能已被删除或改名。\n"
                                 "请点「登录 / 新建」重新登录这个账号。")
            return
        try:
            self._copy_auth(src, AUTH_FILE)
        except Exception as e:
            messagebox.showerror(
                "切换账号失败",
                f"无法写入登录状态文件：{e}\n\n"
                f"多为文件被占用或磁盘只读。关掉其他正在运行的本工具后重试，"
                f"仍不行就重新登录这个账号。")
            return

        self._gui_state["current_account"] = name
        self._save_gui_state()
        self._refresh_auth_status()

        # 切换账号: 清除全部缓存
        self._invalidate_caches("all")
        self._hide_volumes()

        # 刷新共享浏览器 + 作品列表
        self._log(f"已切换到账号: {name}")
        self.worker.submit(self._shared.refresh())
        self._after(300, self._on_refresh_books)

    def _on_mode_change(self):
        mode = self.mode_var.get()

        # --- 1. 先隐藏所有可选组件 ---
        self.sched_frame.pack_forget()
        self._resched_filter_row.pack_forget()
        # 反馈行也跟着藏：它排在筛选行下面，切模式后重新 pack 会把筛选行
        # 排到它后面，反馈行就跑到筛选行上面去了。
        # 收它用 _set_resched_filter_info("")：那个方法管的就是「有文字就
        # pack 出来、没文字就收起来」，别处都用它，这里手抄一份迟早对不上。
        self._set_resched_filter_info("")
        self.lbl_last_publish.pack_forget()
        self._volume_frame.pack_forget()
        self.chk_use_ai.pack_forget()
        self.chk_headless.pack_forget()
        self.chk_autocont.pack_forget()
        for _w in ("lbl_days_ahead", "ent_days_ahead", "lbl_days_ahead_unit"):
            getattr(self, _w).pack_forget()
        self._row_opts.pack_forget()
        self._row_opts2.pack_forget()

        # --- 2. 按模式显示组件（注意 pack 顺序决定布局顺序） ---
        #   _row_opts:   勾选行一 —— AI 申报(schedule/publish/edit) +
        #                接着上次往后排(schedule)
        #   _row_opts2:  勾选行二 —— 后台静默运行(所有模式，无头对每种操作都
        #                生效) + 排到 N 天后(schedule)
        #   _volume_frame:      edit, reschedule (仅多卷时；勾选"合并所有卷"时隐藏分卷下拉)
        #   sched_frame:        schedule, reschedule
        #   _resched_filter_row: all modes
        #   lbl_last_publish:   all modes
        has_vols = bool(self.cmb_volume["values"])
        self._row_opts.pack(fill="x", padx=6, pady=(0, 2))
        self._row_opts2.pack(fill="x", padx=6, pady=(0, 2))
        # 无头开关排第二行行首：它所有模式都在，后面才是只在定时发布出现的
        # 「排到 N 天后」，顺序反过来会让这一行看起来像排期参数的一部分。
        self.chk_headless.pack(side="left", padx=6)
        self.lbl_last_publish.pack(fill="x", padx=12, pady=(0, 2))
        if mode in ("edit", "reschedule") and has_vols:
            # 勾选"合并所有卷"时隐藏分卷下拉，只保留复选框
            self._pack_volume_picker()
            self._volume_frame.pack(fill="x", padx=6, pady=(0, 4))
        if mode in ("schedule", "reschedule"):
            self.sched_frame.pack(fill="x", padx=6, pady=2)
        self._resched_filter_row.pack(fill="x", padx=6, pady=(0, 2))
        if mode in ("schedule", "publish", "edit"):
            self.chk_use_ai.pack(side="left", padx=6)
        if mode == "schedule":
            self.chk_autocont.pack(side="left", padx=6)
            self.lbl_days_ahead.pack(side="left", padx=(14, 0))
            self.ent_days_ahead.pack(side="left", padx=4)
            self.lbl_days_ahead_unit.pack(side="left")
        # 无论哪个模式都同步一次起始日期的置灰:
        # ① _on_autocont_toggle 只在「点击」时触发，config 里 auto_continue:true
        #    时启动后勾是勾上的、输入框却还可编辑；
        # ② 从定时发布切到修改排期时必须把灰取掉（那边没有自动接续）。
        self._sync_autocont_state()

        # --- 3. 上传按钮文字和状态 ---
        btn_text = {"edit": "开始修改", "reschedule": "开始修改"}.get(
            mode, "开始上传")
        if not self.uploading:
            self._set_upload_btn(text=btn_text)
            if mode in ("edit", "reschedule"):
                idx = self.cmb_book.current()
                book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
                ck = self._chapter_cache_key(book_id) if book_id else None
                if not (ck and ck in self._platform_chapters_cache):
                    self._set_upload_btn(state="disabled")
                else:
                    self._set_upload_btn(state="normal")
            else:
                self._set_upload_btn(state="normal")

        # --- 4. 模式特有逻辑 ---
        if mode in ("edit", "reschedule"):
            self._fetch_platform_chapters_for_edit()
            self._refresh_preview()
            self._schedule_config_save()
            return

        self._on_book_changed()
        # 预览头部要立刻反映新模式：_on_book_changed 在未选作品时提前 return，
        # 不刷新的话「模式: 修改内容」会一直挂在切走之后的预览上
        self._refresh_preview()
        self._schedule_config_save()

    def _install_log_handler(self):
        """安装 GUI 日志 handler, 将 logger 输出显示到日志面板。"""
        if self._log_handler is not None:
            return
        handler = TextHandler(self.txt_log, self.root)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        self._log_handler = handler

    def _remove_log_handler(self):
        """移除 GUI 日志 handler。"""
        if self._log_handler is not None:
            logger.removeHandler(self._log_handler)
            self._log_handler = None

    def _log(self, msg):
        """写入 GUI 日志面板（仅 GUI 内部消息用, 不经过 logger）。"""
        self.txt_log.configure(state="normal")
        self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.configure(state="disabled")

    def _set_preview(self, text, *, tail=False):
        if self._tool_streaming:
            # 工具正在跑：实时输出不能被别的东西冲掉。改成追加，中间用横线
            # 分隔，让人看得出这是另一段内容（多半是改了目录/筛选）。
            self._append_preview("\n" + "-" * 46 + "\n" + text + "\n")
            return
        self.txt_preview.configure(state="normal")
        self.txt_preview.delete("1.0", tk.END)
        self.txt_preview.insert("1.0", text)
        self.txt_preview.configure(state="disabled")
        # tail=True：流式输出结束后把视图留在末尾（汇总在最后一段）。
        # 不然替换内容的瞬间视图会弹回顶部，正在看进度的人会以为跑完了。
        if tail:
            self.txt_preview.see(tk.END)
        self._update_guidance()

    def _append_preview(self, text):
        """往「章节预览」面板追加内容（子进程工具的实时输出）。

        为什么要有这个：这些工具跑在子进程里，原来用 capture_output 一次性
        收结果、跑完才显示。几十章的重排要跑一两个小时，中途屏幕一动不动，
        用户根本没法判断程序是死了还是在动（2026-10-31 反馈）。
        """
        if not text:
            return
        self.txt_preview.configure(state="normal")
        self.txt_preview.insert(tk.END, text)
        self.txt_preview.see(tk.END)
        self.txt_preview.configure(state="disabled")

    def _set_tool_progress(self, done, total):
        """子进程工具的进度条（重排按章数推进）。"""
        total = max(int(total), 1)
        self.progress["maximum"] = total
        self.progress["value"] = min(int(done), total)
        self.lbl_progress.configure(text=f"{done}/{total}")

    def _on_tool_line(self, step_rx, line):
        """子进程吐一行：进预览面板；是进度行就推进度条。

        step_rx 是工具模块自己定义的 STEP_PROGRESS_RE（CLI 打印、这里解析，
        同一份格式定义）。认不出就只当普通日志行。
        """
        self._append_preview(line)
        if step_rx is None:
            return
        m = step_rx.search(line)
        if m:
            self._set_tool_progress(int(m.group(1)), int(m.group(2)))

    def _export_log(self):
        """导出运行日志到文件。"""
        content = self.txt_log.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("日志为空", "运行日志还没有内容，执行一次操作后再导出。")
            return
        fp = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile=f"fanqie_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        if fp:
            Path(fp).write_text(content, encoding="utf-8")
            self._log(f"日志已导出: {fp}")

    # -----------------------------------------------------------------------
    # 配置持久化
    # -----------------------------------------------------------------------
    def _sync_perday_from_times(self):
        """时间点数量 > 每天章数时，自动上调 per_day。"""
        validated = validate_times(self.time_var.get())
        n_times = len(validated)
        if n_times < 1:
            return
        try:
            cur = self.perday_var.get()
        except tk.TclError:
            cur = 1
        if n_times > cur:
            self.perday_var.set(n_times)

    def _schedule_config_save(self):
        """延迟保存配置（防抖 1 秒，避免频繁写盘）。"""
        if hasattr(self, "_config_save_after"):
            self.root.after_cancel(self._config_save_after)
        self._config_save_after = self.root.after(1000, self._save_config)

    @staticmethod
    def _atomic_write_json(path, data):
        """原子写 JSON：先写临时文件再 replace，避免进程中途被杀留下半截文件。

        Windows 上 replace 会被实时扫描/索引器短暂挡住：刚写出的 .tmp 还没
        被扫完就被 rename，Windows 直接回 WinError 5（拒绝访问）。实测本机
        约 10% 的概率，而且常常连着失败好几下——这不是内容写坏了，是文件
        被别的进程短暂占着。重试几次就能落盘；写的是完整内容，每次重试都
        是从 tmp 重新 replace，不需要重写。

        退避步长只有 10ms：这个函数跑在 **Tk 主线程**上，阻塞 0.75 秒会让
        整个界面僵住——重绘、输入、每秒一次的定时倒计时全都停摆。实测
        （仓库目录，60 次换名）6 次重试在 10ms 步长下 0 次失败、最坏耗时
        33ms；步长再大也救不回来更多，只是把界面冻住。
        真实的最坏情况是**六次全被挡**：那要睡 10+20+30+40+50 = 150ms，
        界面会僵一下（实测 154ms）。要彻底不僵得把重试改成 after 异步，
        那是另一件事（失败会延后到下一轮才报），先记在这儿。
        """
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            for attempt in range(RENAME_RETRIES):
                try:
                    tmp.replace(path)  # 同目录 rename，Windows 上原子
                    return
                except PermissionError:
                    if attempt == RENAME_RETRIES - 1:
                        raise
                    time.sleep(RENAME_BACKOFF_S * (attempt + 1))
        except Exception:
            # 写一半失败（磁盘满/只读）时清掉残留 tmp，别留垃圾；原文件因
            # 尚未 replace 仍完好。异常继续上抛由调用方告警。
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _save_config(self):
        """将当前 GUI 设置写入 config.json。"""
        self._cfg["default_mode"] = self.mode_var.get()
        self._cfg["default_time"] = self.time_var.get().strip() or "08:00"
        self._cfg["chapters_dir"] = self.dir_var.get()
        if hasattr(self, "timer_time_var"):
            self._cfg["timer_time"] = self.timer_time_var.get().strip()
        try:
            self._cfg["default_per_day"] = self.perday_var.get()
        except tk.TclError:
            pass
        if hasattr(self, "unique_var"):
            self._cfg["auto_unique"] = self.unique_var.get()
        if hasattr(self, "use_ai_var"):
            self._cfg["use_ai"] = self.use_ai_var.get()
        if hasattr(self, "headless_var"):
            self._cfg["headless"] = self.headless_var.get()
        if hasattr(self, "autocont_var"):
            self._cfg["auto_continue"] = self.autocont_var.get()
        if hasattr(self, "days_ahead_var"):
            raw = self.days_ahead_var.get().strip()
            self._cfg["days_ahead"] = int(raw) if raw.isdigit() else None
        if hasattr(self, "resched_filter_var"):
            self._cfg["resched_filter_on"] = self.resched_filter_var.get()
            self._cfg["resched_filter_op"] = self.resched_filter_op_var.get()
            self._cfg["resched_filter_num"] = self.resched_filter_num_var.get().strip()
        try:
            self._atomic_write_json(CONFIG_FILE, self._cfg)
        except Exception as e:
            # 失败必须留痕：级别用 warning 而不是 debug——日志级别是 INFO，
            # debug 根本不落盘、也不进「运行日志」面板。设置悄悄回退到旧值
            # 是最难查的一类问题（用户只会觉得「改了没用」）。
            logger.warning(f"保存 config.json 失败（本次设置没有保存）: {e}")

    # --- GUI 内部状态 (.gui_state.json，不含在用户 config 中) ---

    @staticmethod
    def _load_gui_state() -> dict:
        if GUI_STATE_FILE.exists():
            try:
                with open(GUI_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 合法 JSON 但非对象（如 []）会让后续 .get() 抛 AttributeError
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError, OSError):
                # OSError: 文件被占用/云端按需文件离线时 open 会抛错，不该让
                # GUI 整个起不来（load_config 对 config.json 同因已处理）
                pass
        return {}

    def _save_gui_state(self):
        try:
            self._atomic_write_json(GUI_STATE_FILE, self._gui_state)
        except Exception as e:
            # 失败必须留痕：级别用 warning 而不是 debug——日志级别是 INFO，
            # debug 根本不落盘、也不进「运行日志」面板（config.json 那边同理）。
            logger.warning(f"保存 .gui_state.json 失败（本次界面状态没有保存）: {e}")

    def _set_upload_btn(self, **kw):
        """改主按钮（开始上传 / 开始修改 / 停止）的文字或状态，并同步引导栏。

        凡是改主按钮的地方都走这个函数，别直接 configure：
        引导栏的「下一步」按钮跟主按钮说的是同一件事。主按钮变成「停止」，
        引导栏那颗也得跟着变——否则用户按着提示去点「开始上传」，
        把自己正在跑的活给停了。两处各改各的，迟早会对不上。
        """
        # 注意这里调的是 ttk 的 configure，不是 _set_upload_btn 自己（那会递归）。
        ttk.Button.configure(self.btn_upload, **kw)
        self._sync_guide_cta()

    def _set_uploading(self, active, *, cancellable=True, show_log=True):
        """锁定/解锁 UI。

        cancellable=False: 工具与体检类任务不读 _cancel_requested，点「停止」
            只会把按钮变灰、任务照跑（用户看到永久「正在停止…」）。这类任务
            按钮直接置灰、不写「停止」，不给做不到的承诺。
        show_log=False: 结果写在「章节预览」页的任务（体检/重排预览/清草稿箱
            安全检查）——强切到「运行日志」会让用户对着空日志，而弹窗还说
            「详见预览面板」。
        """
        self.uploading = active
        self._cancel_requested = False
        if active:
            if cancellable:
                self._set_upload_btn(state="normal", text="停止")
            else:
                self._set_upload_btn(state="disabled", text="进行中…")
            nb = getattr(self, "_nb", None)
            if nb is not None:
                try:
                    nb.select(1 if show_log else 0)
                except tk.TclError:
                    pass
        else:
            mode = self.mode_var.get()
            btn_text = {"edit": "开始修改", "reschedule": "开始修改"}.get(
                mode, "开始上传")
            self._set_upload_btn(state="normal", text=btn_text)
            # 修改内容/修改排期模式下如果未载入章节列表则禁用
            if self.mode_var.get() in ("edit", "reschedule"):
                idx = self.cmb_book.current()
                book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
                ck = self._chapter_cache_key(book_id) if book_id else None
                if not (ck and ck in self._platform_chapters_cache):
                    self._set_upload_btn(state="disabled")
        # 上传期间禁用所有可能影响状态的控件
        ctrl_state = "disabled" if active else "normal"
        self.btn_books.configure(state=ctrl_state)
        self.btn_login.configure(state=ctrl_state)
        self.cmb_book.configure(state="disabled" if active else "readonly")
        self.cmb_account.configure(state="disabled" if active else "readonly")
        self.btn_open_manage.configure(state=ctrl_state)
        # 章节文件夹这一组也要锁：跑到一半换了目录/重新扫描，
        # 正在发的那一批和下一批的来源就不一致了（而且引导栏第 ③ 步的
        # 「下一步」按钮正指向「选择文件夹」，不锁它就会出现
        # 「引导栏说能点、点了就把目录换了」）。
        for _b in ("btn_browse_dir", "btn_reload_chapters"):
            if hasattr(self, _b):
                getattr(self, _b).configure(state=ctrl_state)
        # 检查缺口按钮：任务进行中禁用（避免同开第二个浏览器、并发写 AUTH_FILE）
        if hasattr(self, "btn_audit"):
            self.btn_audit.configure(state=ctrl_state)
        for _b in ("btn_remap", "btn_clean", "btn_fix_sched"):
            if hasattr(self, _b):
                getattr(self, _b).configure(state=ctrl_state)
        for rb in self._mode_radios:
            rb.configure(state=ctrl_state)
        # 登录/刷新作品这几个按钮的状态变了，引导栏的「下一步」要跟着变；
        # 提示语也得跟着换——跑着的时候不能还在说「点开始上传」。
        self._update_guidance()

    # -----------------------------------------------------------------------
    # 快捷链接: 打开章节管理
    # -----------------------------------------------------------------------
    def _open_chapter_manage(self):
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            messagebox.showwarning("未选择作品",
                                   "先在「作品与章节」里选择一部作品，"
                                   "才能打开它的章节管理页。")
            return
        book_id = self.books[idx]["bookId"]
        url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)
        webbrowser.open(url)

    # -----------------------------------------------------------------------
    # 作品切换 → 获取上次发布时间
    # -----------------------------------------------------------------------
    def _on_book_changed(self):
        self._fetch_gen += 1
        self._update_guidance()

        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            return

        book_id = self.books[idx]["bookId"]

        # 换书后徽标要重算：卡队列是「这本书的这一章卡住了」，位置和章号
        # 只对那本书有意义，上一本的 ⚠ 不该跟过来（见 _audit_badge）。
        self._refresh_audit_badge()

        # 记住选择的作品（按账号区分，存到隐藏状态文件）
        acct = self._gui_state.get("current_account", "")
        key = f"last_book_id_{acct}" if acct else "last_book_id"
        self._gui_state[key] = book_id
        self._save_gui_state()

        # 恢复"合并所有卷"状态（按作品持久化）
        self.all_volumes_var.set(
            self._gui_state.get(f"all_volumes_{book_id}", False))

        # 恢复卷选择器（如有缓存；None = 已检测过但无多卷）
        if book_id in self._volumes_cache:
            vols = self._volumes_cache[book_id]
            if vols:
                self._show_volumes(vols)
            else:
                self._hide_volumes()
        else:
            self._hide_volumes()

        # 需要整份平台章节表的模式走章节表抓取（它顺带带回队尾时刻）；
        # 其余模式只看首页拿「队列排到哪天」
        if self._needs_chapter_list():
            if self.mode_var.get() in ("edit", "reschedule"):
                self._set_upload_btn(state="disabled")
            self._fetch_platform_chapters_for_edit()
        elif book_id in self._last_publish_cache:
            # 有缓存直接用（_apply_last_publish 会通过 date_var trace 触发预览刷新）
            self._apply_last_publish(self._last_publish_cache[book_id])
        elif AUTH_FILE.exists():
            self._fetch_last_publish(book_id)

        self._refresh_preview()

    def _fetch_last_publish(self, book_id):
        """后台重取「队列排到哪天」（走章节列表接口，一次拿全书）。

        两个调用时机: 选作品时，以及**发完一批之后**。后者很容易漏:
        上传结束会 _invalidate_caches("chapters") 把缓存清掉，标签上那个日期
        也就跟着作废——刚把队列往后推了一截，界面上还写着推之前的日期，
        下一批照它填就插队了。发完一批后两条分支都会重拉（见 _upload_done）：
        要整份平台章节表的模式重拉章节表，其余模式重拉队尾。

        2026-10-31 改：过去只扫章节管理页**首页**的十几行，按「最大时刻」取
        一行 —— 几千章的书，首页那点数据跟真实队尾毫无关系（实测：标签写着
        第5191章，平台实际有5196章，而末章第5196章排在更早的时刻）。
        现在走 chapter_list 接口一次拿全书（实测 5196 章约 1 秒），队尾、末章、
        排期倒挂一次算齐。
        """
        if not AUTH_FILE.exists():
            return
        self.lbl_last_publish.configure(
            text="正在获取发布信息…", foreground=CLR_INK_SOFT)  # noqa

        gen = self._fetch_gen
        volumes_known = book_id in self._volumes_cache

        async def task():
            page = None
            try:
                ctx = await self._ensure_browser()
                page = await ctx.new_page()
                if self._fetch_gen != gen:
                    return
                items, _sig, _vols = await fetch_chapter_items(page, book_id)
                if self._fetch_gen != gen:
                    return
                # 卷名仍用 DOM 那份（select_volume 是按下拉里的文字匹配的，
                # 用接口返回的 volume_name 万一对不上就切不了卷）。
                if not volumes_known:
                    vol_info = await detect_volumes(page)
                    self._after(0, self._volumes_detected, book_id, vol_info)
                self._after(0, self._last_publish_fetched, book_id,
                            queue_summary(queue_state(items)))
            except Exception:
                if self._fetch_gen == gen:
                    self._after(
                        0, self._last_publish_fetched, book_id, None)
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

        self.worker.submit(task())


    def _last_publish_fetched(self, book_id, result):
        """后台获取完成，更新缓存和 UI。"""
        # 检查当前选中的作品是否仍匹配
        idx = self.cmb_book.current()
        current_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
        if current_id != book_id:
            return  # 用户已切换作品，丢弃过期结果

        if result:
            self._last_publish_cache[book_id] = result
            self._apply_last_publish(result)
        else:
            # 读不到 ≠ 平台没有记录。宁可说「读不到」，也不能显示一个猜的
            # 日期（那会让用户照着一个错的起点排章）。
            self.lbl_last_publish.configure(
                text="读不到平台队列（重新选择作品可重试）", foreground=CLR_INK_SOFT)

    @staticmethod
    def _last_published(chapters):
        """平台章节表里最后一章**已发布**的 {date, time, chapter}；没有则 None。

        与 api_items_to_rows 的 last_publish 同形，但只看已发布章——修改排期
        要挪的就是待发布章，起点得接在没被挪的那些后面。
        """
        best, best_key = None, ""
        for c in chapters:
            d = c.get("date")
            if not d or "已发布" not in (c.get("status") or ""):
                continue
            key = f"{d} {c.get('time') or ''}"
            if key > best_key:
                best, best_key = {"date": d, "time": c.get("time"),
                                  "chapter": c.get("title", "")}, key
        return best

    def _queue_tail_datetime(self, book_id):
        """平台队列末尾时刻（datetime）；读不到返回 None。

        来源是「队列排到」那次抓取算出来的摘要（缓存里有）。拿不到就返回
        None —— 校验跳过一次，不拿猜的数去挡用户的操作。
        """
        info = self._last_publish_cache.get(book_id) or {}
        d, t = info.get("date"), info.get("time")
        if not d or not t:
            return None
        try:
            return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _apply_last_publish(self, info, label="队列排到"):
        """将「队列排到哪天」显示到 UI 并自动建议次日为起始日期。

        时间和章号是**两个不同的数**，分开报：
          · 时间 = 队尾时刻（最新被占用的发布时刻）—— 起始日期按它推，
            新章必须排在它后面；
          · 章号 = 末章（位置最后那章）—— 读者最后看到的是它。
        健康的书上两者重合；排期错乱的书上不重合（2026-10-31 用户反馈：标签
        写着第5191章，平台实际有5196章，末章排在更早的时刻）。不重合时第二
        行把话说清楚，不静默挑一个冒充另一个。

        文案在核心里（queue_label_text）：GUI 与命令行共用同一份，也不用开
        窗口就能测。

        只更新 date_var，不覆盖 time_var —— 时间是用户配置项，
        平台单条发布记录不应覆盖用户设定的多时间方案。
        """
        date_str = info.get("date")
        time_str = info.get("time")
        if not date_str or not time_str:
            # 平台记录不完整（DOM 变动 / 部分抓取），不要让 KeyError
            # 冒泡进 after() 回调把标签卡在"正在获取..."。
            self.lbl_last_publish.configure(text="暂无发布记录", foreground=CLR_INK_SOFT)
            return
        text = queue_label_text(info, label=label)
        broken = bool(int(info.get("inverted") or 0)
                      or int(info.get("clash") or 0))
        self.lbl_last_publish.configure(
            text=text, foreground=CLR_WARN if broken else CLR_SCHED_TX)

        # 自动建议: 起始日期 = 队尾日期 + 1 天
        try:
            last_dt = datetime.strptime(date_str, "%Y-%m-%d")
            next_dt = last_dt + timedelta(days=1)
            self.date_var.set(next_dt.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    # -----------------------------------------------------------------------
    # 卷选择
    # -----------------------------------------------------------------------
    def _volumes_detected(self, book_id, vol_info):
        """后台检测到卷信息后更新缓存和 UI。"""
        idx = self.cmb_book.current()
        current_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
        if current_id != book_id:
            return

        volumes = vol_info.get("volumes", [])
        if vol_info.get("hasVolumes"):
            self._volumes_cache[book_id] = volumes
            self._show_volumes(volumes)
        else:
            # 缓存 None 表示"已检测，无多卷"，避免重复检测
            self._volumes_cache[book_id] = None
            self._hide_volumes()

    def _show_volumes(self, volumes):
        """填充卷选项，在 edit/reschedule 模式下显示（紧跟勾选行之后）。"""
        # DOM 漂移时卷条目可能缺 text，用 .get 容错而非 KeyError
        texts = [v.get("text", "") if isinstance(v, dict) else str(v)
                 for v in volumes]
        self.cmb_volume["values"] = texts
        # 恢复优先级: 当前选择 > 平台活跃卷 > 首卷
        current = self.volume_var.get()
        if not (current and current in texts):
            active = [v.get("text", "") for v in volumes
                      if isinstance(v, dict) and v.get("isActive")]
            if active:
                self.cmb_volume.set(active[0])
            elif texts:
                self.cmb_volume.set(texts[0])
        # 仅 edit/reschedule 模式显示，用 after 保证位于 lbl_last_publish 之后
        if self.mode_var.get() in ("edit", "reschedule"):
            self._volume_frame.pack_forget()
            self._pack_volume_picker()
            self._volume_frame.pack(
                fill="x", padx=6, pady=(0, 4), after=self.lbl_last_publish)

    def _hide_volumes(self):
        """清空卷选项并隐藏。"""
        self._volume_frame.pack_forget()
        self.cmb_volume.set("")
        self.cmb_volume["values"] = []

    def _on_volume_changed(self):
        """用户切换了卷选择。"""
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            return

        self._fetch_gen += 1  # 使正在进行的后台任务过期

        mode = self.mode_var.get()
        if mode in ("edit", "reschedule"):
            # 任务运行中此按钮是「停止」——不能禁用，否则批次无法取消
            # （分卷下拉未被 _set_uploading 禁用，运行中仍可切换）
            if not self.uploading:
                self._set_upload_btn(state="disabled")
            self._fetch_platform_chapters_for_edit()
            self._refresh_preview()

    def _on_all_volumes_changed(self):
        """用户切换了"合并所有卷"复选框，持久化并刷新。"""
        # 持久化
        idx = self.cmb_book.current()
        if idx >= 0 and self.books:
            book_id = self.books[idx]["bookId"]
            self._gui_state[f"all_volumes_{book_id}"] = self.all_volumes_var.get()
            self._save_gui_state()

        self._fetch_gen += 1
        # 刷新布局: 勾选时隐藏卷选择器，取消时显示
        self._on_mode_change()

    def _get_selected_volume(self) -> str:
        """返回当前选中的卷名（无卷或未选择时返回空字符串）。"""
        return self.volume_var.get().strip()

    def _chapter_cache_key(self, book_id: str) -> str:
        """章节缓存键: book_id + 当前选中的卷（或 __ALL__ 表示合并所有卷）。"""
        if self.all_volumes_var.get():
            return f"{book_id}:__ALL__"
        vol = self._get_selected_volume()
        return f"{book_id}:{vol}" if vol else book_id

    # -----------------------------------------------------------------------
    # 修改模式: 获取平台章节列表
    # -----------------------------------------------------------------------
    def _fetch_platform_chapters_for_edit(self):
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            # 无作品可获取：恢复按钮可用，避免停留在禁用态卡死用户
            # （真正的前置校验由 _on_upload 统一兜底提示）
            if not self.uploading:
                self._set_upload_btn(state="normal")
            return

        book_id = self.books[idx]["bookId"]
        cache_key = self._chapter_cache_key(book_id)

        if cache_key in self._platform_chapters_cache:
            self._on_platform_chapters_fetched(
                book_id, self._platform_chapters_cache[cache_key], error=None)
            return

        if not AUTH_FILE.exists():
            # 未登录：不会发起抓取，恢复按钮可用，否则修改/排期模式按钮永久禁用
            self.lbl_last_publish.configure(
                text="请先登录后再获取章节列表", foreground=CLR_SCHED_TX)
            if not self.uploading:
                self._set_upload_btn(state="normal")
            return

        self._start_elapsed(self.lbl_last_publish, "正在获取章节列表…")

        gen = self._fetch_gen
        selected_vol = self._get_selected_volume()
        volumes_known = book_id in self._volumes_cache
        known_vols = self._volumes_cache.get(book_id)  # 主线程快照，供工作线程使用
        fetch_all_vols = self.all_volumes_var.get()

        def on_page_progress(done, total, n):
            # 从 Playwright 线程回调，必须经 _after 回主线程改 UI。
            # 一旦有真实进度就不再显示秒表——「第3/12页 · 已240章」比「已18秒」
            # 有用得多：它说明还剩多少，而不只是过去了多久。
            if self._fetch_gen != gen:
                return
            tot = f"/{total}" if total else ""
            self._after(0, self._show_fetch_progress,
                        f"正在获取章节列表… 第 {done}{tot} 页 · 已 {n} 章")

        async def task():
            page = None
            try:
                ctx = await self._ensure_browser()
                page = await ctx.new_page()
                if self._fetch_gen != gen:
                    return
                url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)
                if not await goto_with_login_retry(page, url):
                    raise RuntimeError(
                        "被重定向到登录页，登录状态可能已失效（请重新登录）")
                await settle_page(page)
                try:
                    await page.wait_for_selector(
                        "tr td", timeout=get_browser_timeout())
                except PWTimeout:
                    pass

                # 仅首次检测卷
                if not volumes_known:
                    vol_info = await detect_volumes(page)
                    if self._fetch_gen != gen:
                        return
                    self._after(0, self._volumes_detected, book_id, vol_info)
                    has_vols = vol_info.get("hasVolumes")
                    vol_list = vol_info.get("volumes", [])
                else:
                    # 必须用主线程那份快照，不能在这里重读 _volumes_cache：
                    # 它随时可能被 _invalidate_caches() 清空（用户切作品/切卷），于是
                    # volumes_known 是 True、vol_list 却成了空 —— 多卷分支静默退化成
                    # 单卷，只抓当前卷却按「合并所有卷」的键缓存。
                    has_vols = bool(known_vols)
                    vol_list = known_vols or []

                # "合并所有卷" 模式: 遍历每个卷并合并章节
                if fetch_all_vols and has_vols and vol_list:
                    all_chapters = []
                    for vi, vol in enumerate(vol_list):
                        vol_name = vol["text"] if isinstance(vol, dict) else vol
                        if self._fetch_gen != gen:
                            return
                        msg = f"正在索引分卷 ({vi + 1}/{len(vol_list)}): {vol_name}..."
                        self._after(0, lambda m=msg: self.lbl_last_publish.configure(
                            text=m, foreground=CLR_INK_SOFT))
                        await select_volume(page, vol_name)
                        chs = await extract_chapters_from_page(
                            page, book_id, on_progress=on_page_progress)
                        # 位置要换算成全书连续：接口那条路的 index 是**卷内**
                        # 位置（多卷书每卷都从 1 开始），合并时必须加上前面
                        # 各卷的章数偏移 —— 否则末章和排期倒挂会按卷内位置算，
                        # 跨卷的先后关系全错。
                        _off = len(all_chapters)
                        for _c in chs:
                            _p = _c.get("pos")
                            if isinstance(_p, int) and not isinstance(_p, bool):
                                _c["pos"] = _p + _off
                        all_chapters.extend(chs)
                    chapters = all_chapters
                else:
                    # 单卷模式: 切换到指定卷
                    if selected_vol and has_vols:
                        await select_volume(page, selected_vol)
                    chapters = await extract_chapters_from_page(
                        page, book_id, on_progress=on_page_progress)

                if self._fetch_gen != gen:
                    return
                self._after(0, self._on_platform_chapters_fetched,
                            book_id, chapters, None, gen)
            except Exception as e:
                if self._fetch_gen == gen:
                    self._after(0, self._on_platform_chapters_fetched,
                                book_id, [], str(e), None, gen)
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

        self.worker.submit(task())

    def _show_fetch_progress(self, text):
        """有真实进度就停掉秒表，改显示页码/章数。"""
        stop = getattr(self, "_elapsed_stop", None)
        if stop:
            stop()
        try:
            self.lbl_last_publish.configure(text=text, foreground=CLR_INK_SOFT)
        except tk.TclError:
            pass

    def _start_elapsed(self, widget, base_text, color=CLR_INK_SOFT):
        """给长任务的状态标签挂个秒表，返回停止函数。

        为什么需要: 抓一次平台章节要开浏览器、导航、翻页提取，正常也要十几秒。
        一行静止不动的「正在获取章节列表…」在用户眼里和卡死没有区别——加上
        「（已 N 秒）」才看得出它在动。同一时刻只允许一个秒表，启动时先停掉上一个。
        """
        stop_prev = getattr(self, "_elapsed_stop", None)
        if stop_prev:
            stop_prev()
        state = {"n": 0, "on": True}

        def tick():
            if not state["on"]:
                return
            state["n"] += 1
            try:
                widget.configure(text=f"{base_text}（已 {state['n']} 秒）",
                                 foreground=color)
            except tk.TclError:
                return          # 控件已销毁（关窗），静默收手
            try:
                self.root.after(1000, tick)
            except tk.TclError:
                pass            # 主窗已销毁，不必再排下一拍

        try:
            widget.configure(text=base_text, foreground=color)
        except tk.TclError:
            return lambda: None
        self.root.after(1000, tick)

        def stop():
            state["on"] = False
        self._elapsed_stop = stop
        return stop

    def _on_platform_chapters_fetched(self, book_id, chapters, error,
                                      gen=None):
        # gen 过期 = 期间切了作品或卷（_fetch_gen 已自增）。必须在这里(应用时)
        # 再判一次：仅判 book_id 不够——切卷时 book_id 不变，但缓存键
        # _chapter_cache_key 读实时 volume_var，会把旧卷章节写到新卷键下
        stop_tick = getattr(self, "_elapsed_stop", None)
        if stop_tick:
            stop_tick()   # 先停秒表，否则它下一秒会把结果文案盖掉
        if gen is not None and gen != self._fetch_gen:
            return
        # 检查当前选中的作品是否仍匹配
        idx = self.cmb_book.current()
        current_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
        if current_id != book_id:
            return  # 用户已切换作品，丢弃过期结果

        if error:
            self.lbl_last_publish.configure(
                text=f"获取章节列表失败: {error}（重新选择作品可重试）",
                foreground="red")
            self._refresh_preview()
            return

        self._platform_chapters_cache[self._chapter_cache_key(book_id)] = chapters
        # 「队列排到」按平台真状态算（队尾时刻 + 末章 + 排期倒挂）：这次抓
        # 回来的行带 display_status/timer_time/pos，算得出来。2026-10-31 改：
        # 过去用的是抓取时顺带返回的「最大时刻那一行」，末章排在它前面时
        # 标签就报错了章号（实测：写着第5191章，平台实际有5196章）。
        _lp = queue_summary(queue_state(rows_to_items(chapters)))
        if _lp.get("date") and _lp.get("time"):
            self._last_publish_cache[book_id] = _lp
        lp = self._last_publish_cache.get(book_id)
        mode = self.mode_var.get()
        if mode == "reschedule":
            # 修改排期挪的正是待发布章，起始日期不能接在它们（队尾）后面，
            # 要接在最后一章**已发布**之后。此前这条路从不碰起始日期，
            # 框里一直是启动时的「明天」或定时发布模式留下的旧值
            lp = self._last_published(chapters)
        if mode == "edit" or not lp:
            self.lbl_last_publish.configure(
                text=f"已索引 {len(chapters)} 个章节", foreground=CLR_SCHED_TX)
        else:
            # 定时发布（续排）也走这条抓取：标签和起始日期要像 _fetch_last_publish
            # 那样给出「队列排到哪天」，否则勾了续排就只剩一句「已索引 N 章」
            self._apply_last_publish(
                lp, label="最后发布" if mode == "reschedule" else "队列排到")
        # 载入完成，恢复上传按钮
        if not self.uploading and self.mode_var.get() in ("edit", "reschedule"):
            self._set_upload_btn(state="normal")
        self._refresh_preview()

    # -----------------------------------------------------------------------
    # 登录
    # -----------------------------------------------------------------------
    def _place_over_parent(self, win):
        """把弹窗摆在主窗口正中，而不是 Tk 默认的屏幕左上角。

        Tk 新建的 Toplevel 落在屏幕 (0,0) 附近，而主窗口通常开在屏幕中间
        （实测「章节重排…」在 0,0、主窗口在 1231,438）：点完按钮看不到任何
        变化，人只会以为没点上、或者程序没反应。

        摆位量的是**客户区**，但真正会跑到屏幕外面去的是外框：标题栏在客
        户区上面，任务栏在屏幕下面。所以这里按外框算，并且用那块屏幕的
        「工作区」（去掉任务栏）而不是整块屏幕来钳位——不然弹窗底下那排
        按钮正好压在任务栏下面。欢迎引导是 grab_set 的模态窗，按钮点不到、
        主窗口又被 grab 住，程序就卡死了。
        """
        win.update_idletasks()
        ww = win.winfo_width()
        wh = win.winfo_height()
        # 外框比客户区大出来的那一圈（标题栏 + 边框）：Tk 报的 wm_geometry
        # 是外框的位置，winfo_rootx/rooty 是客户区的位置，两者一减就是它。
        _fg = win.wm_geometry().split("+")
        try:
            _fx, _fy = int(_fg[1]), int(_fg[2])
        except (IndexError, ValueError):
            _fx = _fy = 0
        _bw = max(0, win.winfo_rootx() - _fx)      # 左右边框
        _bh = max(0, win.winfo_rooty() - _fy)      # 标题栏
        _fw = ww + _bw * 2
        # 高度也要算上下边框：外框比客户区多出来的是「标题栏 + 上下边框」。
        # 少算下边框的话，留给屏幕边缘的余量就比说的 12px 少一截边框。
        _fh = wh + _bh + _bw

        px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
        _wa = _display_work_area(px, py)
        if _wa:
            left, top, right, bottom = _wa
        else:
            left, top = 0, 0
            right, bottom = win.winfo_screenwidth(), win.winfo_screenheight()

        x = px + max(0, (self.root.winfo_width() - ww) // 2)
        y = py + max(0, (self.root.winfo_height() - wh) // 3)
        x = max(left + 12, min(x, right - _fw - 12))
        y = max(top + 12, min(y, bottom - _fh - 12))
        win.geometry(f"+{x}+{y}")
        # 摆完再核一遍实际落点：多显示器/缩放时 Tk 算出来的位置不一定就是
        # 我们要的。底下溢出工作区就往上挪——别让唯一那颗按钮掉到屏幕外。
        win.update_idletasks()
        _over = (win.winfo_rooty() + wh + _bw) - (bottom - 12)
        if _over > 0:
            # 挪的幅度按**实际**位置算：这里量的是窗口管理器真正摆的位置，
            # 减的却是我们请求的位置——两者不一致时（X11 上窗口管理器异步摆
            # 窗口）会差一截。
            _act_y = win.winfo_rooty() - _bh
            win.geometry(f"+{x}+{max(top + 12, _act_y - _over)}")
        win.lift()

    def _prompt_account_name(self):
        """弹"账号名称"对话框并校验，返回合法名称；取消/放弃覆盖时返回 None。"""
        current_acct = self._gui_state.get("current_account", "")
        got = {}
        dlg = tk.Toplevel(self.root)
        dlg.title("账号名称")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        try:
            dlg.grab_set()
        except tk.TclError:
            pass
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="给这个账号起一个本地名称（如 作家A）：",
                  justify="left").pack(anchor="w")
        name_var = tk.StringVar(value=current_acct)
        ent = ttk.Entry(frm, textvariable=name_var, width=36)
        ent.pack(fill="x", pady=(6, 0))
        # 提示走 StringVar：输错时留在原地改，别让人关掉重开再找一遍输入框
        err = tk.StringVar(value="")
        ttk.Label(frm, textvariable=err, foreground=CLR_WARN,
                  justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Label(frm, style="Cap.TLabel", justify="left",
                  text="· 只是本工具里区分多个账号用的标签，不是番茄笔名\n"
                       "· 点「确定」后会打开浏览器，请在浏览器里登录番茄账号"
                  ).pack(anchor="w", pady=(8, 0))
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(14, 0))

        def on_ok():
            if not _dlg_alive(dlg):
                return
            raw = (name_var.get() or "").strip()
            if not raw:
                err.set("请填一个名称（例如 作家A），只在本工具里用来区分账号。")
                return
            cleaned = self._sanitize_account_name(raw)
            if not cleaned:
                # 被拒有两个原因，说错原因等于让用户瞎猜：① 名字里有 Windows
                # 文件名的非法字符；② 名字正好是保留名 state（.auth_state.json
                # 是工具自己的状态文件，叫这名会把它覆盖掉）。
                if raw.lower() == "state":
                    err.set("「state」是保留名（工具的状态文件用它），换一个。")
                else:
                    err.set("名称里不能有 \\ / : * ? \" < > | 这些字符，换一个。")
                return
            got["value"] = cleaned
            dlg.destroy()

        def on_cancel():
            got["value"] = None
            dlg.destroy()

        ttk.Button(btns, text="确定", style="Primary.TButton",
                   command=on_ok).pack(side="left")
        ttk.Button(btns, text="取消", command=on_cancel).pack(
            side="left", padx=(8, 0))
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: on_cancel())
        self._place_over_parent(dlg)
        ent.focus_set()
        self.root.wait_window(dlg)
        name = got.get("value")
        if not name:
            return None
        # L2: 已存在同名账号时提示确认
        named_path = SCRIPT_DIR / f".auth_{name}.json"
        if named_path.exists():
            if not messagebox.askyesno(
                    "账号已存在",
                    f"账号「{name}」已存在。\n继续将覆盖其登录状态，是否继续？"):
                return None
        return name

    def _on_login(self):
        # 防止并发登录
        if self._login_in_progress:
            messagebox.showinfo("登录进行中", "先完成或取消当前登录，再开始新的登录。")
            return
        # 必须在弹"账号名称"对话框【之前】就占住登录态：那个对话框用 wait_window
        # 会继续泵 Tk after 事件，定时器 _timer_tick 会在对话框开着时触发——若此刻
        # _login_in_progress 仍为 False，忙碌闸门放行，会并发起一个定时上传任务
        # （两个浏览器同开 + 争用 AUTH_FILE，登录保存的会话可能盖掉上传在用的账号）。
        self._login_in_progress = True
        self.btn_login.configure(state="disabled")
        try:
            name = self._prompt_account_name()
        except Exception:
            # 对话框异常（如 root 被销毁）不能让登录态泄漏：否则按钮永久
            # 禁用、定时器永远被"正在登录"挡住。复位后把异常照常抛出。
            self._login_in_progress = False
            self.btn_login.configure(state="normal")
            raise
        if name is None:
            # 取消/名称非法/放弃覆盖：未起任务，必须释放登录态，
            # 否则登录按钮永久禁用、定时器永远被"正在登录"挡住。
            self._login_in_progress = False
            self.btn_login.configure(state="normal")
            return

        self._pending_account_name = name
        self._login_event = threading.Event()
        self._login_cancelled = False

        async def task():
            try:
                async with async_playwright() as p:
                    # 登录必须有头：要你在浏览器里手动扫码/输密码，
                    # 无头开关对这里不生效（否则登录永远等不到人操作）。
                    browser, context = await create_context(p, headless=False)
                    page = await context.new_page()
                    # domcontentloaded 而非 networkidle：番茄的埋点/轮询会拖死
                    # networkidle，使登录浮窗迟迟不弹（取作品列表处同因已改）。
                    # 登录页只需渲染出来供用户手动登录，不必等网络空闲。
                    await page.goto(ZONE_URL, wait_until="domcontentloaded")

                    self._after(0, self._show_login_dialog)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._login_event.wait)

                    if self._login_cancelled:
                        await close_browser_safely(browser)
                        self._after(0, self._login_done, "cancelled")
                        return

                    if not await save_auth(context):
                        # 保存失败（多为用户提前关掉了浏览器窗口）：此时
                        # AUTH_FILE 还是上一个账号的旧会话，绝不能复制成
                        # 新账号的命名文件——那会把旧账号 cookie 静默挂到
                        # 新账号名下，之后的批量操作会发到错误的账号。
                        # 与取消分支同构：先安全关浏览器、在 with 内汇报，
                        # 不让异常带着未关闭的浏览器去赌 pw.stop 不挂
                        await close_browser_safely(browser)
                        self._after(0, self._login_done,
                                    "保存登录状态失败。可能是浏览器被提前关闭，"
                                    "也可能是状态文件正被占用（Windows 上偶发）。"
                                    "请重新点「登录/新建」，登录后先点"
                                    "「✔ 登录完成」再关浏览器。")
                        return

                    # 将活跃 auth 复制为命名文件
                    named = SCRIPT_DIR / f".auth_{name}.json"
                    self._copy_auth(AUTH_FILE, named)

                    await close_browser_safely(browser)

                    # 完成通知放在 async with 内：浏览器挂死时 playwright
                    # stop（with 退出）可能同样阻塞，不能让它挡住结果汇报
                    self._after(0, self._login_done, None)
                    return
            except Exception as e:
                self._after(0, self._login_done, str(e))

        self.worker.submit(task())

    def _show_login_dialog(self):
        # 最小化主窗口，避免遮挡浏览器
        self.root.iconify()

        # 醒目的浮动窗口（非模态），钉在屏幕右下角、始终置顶：主窗口已最小化，
        # 它是用户在浏览器前面唯一能看到的入口
        win = tk.Toplevel(self.root)
        win.title("等待登录")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(bg=CLR_SCHED)   # 外框即排期橙描边，靠 padx/pady 露出 3px

        body = ttk.Frame(win, padding=16)
        body.pack(padx=3, pady=3, fill="both", expand=True)

        ttk.Label(body, text="⏳ 请在浏览器中登录",
                  font=(self._ff, 12, "bold"),
                  foreground=CLR_SCHED_D).pack(anchor="w", pady=(0, 6))
        ttk.Label(body, justify="left", font=(self._ff, 9),
                  foreground=CLR_INK_SOFT,
                  text="在浏览器里登录番茄账号（没有账号请先注册并开通作家），\n"
                       "完成后回到这里点「登录完成」").pack(anchor="w", pady=(0, 12))

        btn_frame = ttk.Frame(body)
        btn_frame.pack(fill="x")

        def on_confirm():
            win.destroy()
            self.root.deiconify()
            self.root.lift()
            self._login_event.set()

        def on_cancel():
            self._login_cancelled = True
            win.destroy()
            self.root.deiconify()
            self.root.lift()
            self._login_event.set()

        ttk.Button(btn_frame, text="登录完成，保存会话",
                   style="Primary.TButton", command=on_confirm).pack(side="left")
        ttk.Button(btn_frame, text="取消登录", command=on_cancel).pack(
            side="left", padx=(8, 0))

        # 关闭按钮 = 取消
        win.protocol("WM_DELETE_WINDOW", on_cancel)

        # 定位到屏幕右下角
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        ww = win.winfo_width()
        wh = win.winfo_height()
        win.geometry(f"+{sw - ww - 40}+{sh - wh - 80}")

    async def _ensure_browser(self):
        """拿共享浏览器；内核缺失时先给一次中文引导，再把异常原样抛回去。

        守卫只存在这一处：所有浏览器入口的第一句都是这句。逐个 except 去贴
        判断迟早会漏 —— 三处的 except 结构还不一样，有一处甚至没有 `as e`。
        """
        try:
            return await self._shared.ensure()
        except Exception as e:
            # messagebox 不能在 worker 上弹，_after 切回主线程；
            # 引导是模态的，等用户答完再继续往上抛，调用方照常走自己的错误分支。
            self._after(0, self._guard_missing_browser, e)
            raise

    def _guard_missing_browser(self, error):
        """内核缺失时给中文引导；同一会话只弹一次。"""
        if not (getattr(self._shared, "missing_browser", False)
                or looks_like_missing_browser(error)):
            return
        self._shared.missing_browser = False    # 清掉，保证只弹一次
        self._prompt_install_browser()

    def _login_done(self, error):
        self._login_in_progress = False
        self.btn_login.configure(state="normal")
        name = getattr(self, "_pending_account_name", "")
        self._pending_account_name = ""
        if error == "cancelled":
            self._log("登录已取消。")
            return
        if error:
            self._refresh_auth_status()
            self._log(f"登录失败: {error}")
            # 错误必须显眼：登录是新用户的第一步，只往日志里塞一行很容易被忽略，
            # 表现成"点了没反应"。缺浏览器内核是最常见原因，单独给出可一键修复的引导。
            if looks_like_missing_browser(error):
                self._prompt_install_browser()
            else:
                messagebox.showerror(
                    "登录失败",
                    f"登录未能完成：\n\n{error}\n\n"
                    f"重新点「登录 / 新建」再试一次。若浏览器根本没弹出来，"
                    f"多半是浏览器内核缺失，登录时会给出一键安装。")
        else:
            if name:
                self._gui_state["current_account"] = name
                self._save_gui_state()
            self._refresh_account_list()
            self._refresh_auth_status()
            self._log(f"登录状态已保存。账号: {name}" if name else "登录状态已保存。")
            # 刷新共享浏览器以加载新的登录状态
            self.worker.submit(self._shared.refresh())
            self._after(300, self._on_refresh_books)

    def _prompt_install_browser(self):
        """缺浏览器内核时弹窗引导：可一键自动下载，或给出手动命令。"""
        if messagebox.askyesno(
                "缺少浏览器内核",
                "检测到 Playwright 浏览器内核（chromium）未安装或损坏，"
                "这通常是首次安装未完成导致的。\n\n"
                "是否现在自动下载安装？（需要联网，约几十 MB）"):
            self._install_browser_async()
        else:
            messagebox.showinfo(
                "手动安装",
                "你也可以手动在命令行运行：\n"
                "    python -m playwright install chromium\n\n"
                "或重新运行 run.bat / run.sh（会自动补装）。")

    def _install_browser_async(self):
        self._log("正在下载安装浏览器内核（chromium）…完成前请勿操作。")
        self.btn_login.configure(state="disabled")

        def work():
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    capture_output=True, text=True)
                ok = proc.returncode == 0
                msg = (proc.stdout or "") + (proc.stderr or "")
            except Exception as e:
                ok, msg = False, str(e)
            self._after(0, self._install_browser_done, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _install_browser_done(self, ok, msg):
        self.btn_login.configure(state="normal")
        if ok:
            self._log("浏览器内核安装完成，请重新点击「登录/新建」。")
            messagebox.showinfo(
                "安装完成", "浏览器内核已安装，请重新点击「登录/新建」。")
        else:
            tail = msg.strip()[-400:]
            self._log(f"浏览器内核安装失败: {tail}")
            messagebox.showerror(
                "安装失败",
                "自动安装未成功，请手动在命令行运行：\n"
                "    python -m playwright install chromium\n\n"
                f"错误信息（末尾）：\n{tail}")

    # -----------------------------------------------------------------------
    # 刷新作品列表
    # -----------------------------------------------------------------------
    def _on_refresh_books(self):
        if not self._require_login():
            return
        if self._login_in_progress:
            # 登录期间刷新会 save_auth 写 AUTH_FILE，与登录保存的会话争用，
            # 可能让命名 auth 文件存入错账号。等登录结束再刷新。
            messagebox.showinfo("登录进行中", "先完成或取消当前登录，再刷新作品列表。")
            return
        # 防重入：多个入口可能近乎同时调度刷新（切账号 + 登录完成），
        # 避免并发任务竞争 self.books / 共享浏览器上下文
        if getattr(self, "_books_loading", False):
            return
        self._books_loading = True
        self.btn_books.configure(state="disabled")
        self._log("正在获取作品列表…")

        async def task():
            page = None
            try:
                ctx = await self._ensure_browser()
                page = await ctx.new_page()
                # domcontentloaded 比 networkidle 可靠: 平台埋点/轮询会拖死 networkidle
                # 致使 goto 超时被误判为会话失效。
                # goto_with_login_retry 会在跳登录页时重试一次：系统繁忙导致
                # 鉴权 API 超时会被 SPA 误判成未登录，重试即可与真失效区分。
                if not await goto_with_login_retry(
                        page, BOOK_MANAGE_URL, wait_until="domcontentloaded"):
                    self._after(0, self._books_fetched, [], "__SESSION_EXPIRED__")
                    return
                # 判据换成「页面渲染完了」，而不是「等某条业务链接出现」：后者把
                # 「这账号一本都没抓到」和「页面还没渲染完」混成同一件事，用户
                # 就会被反复告知"系统繁忙"却永远出不来（GitHub issue #4）。
                ready, state = await wait_book_list_ready(page)
                books = await page.evaluate(BOOKS_JS)
                if not books:
                    # 空结果先取证再说：诊断 + 现场截图写进日志，下一个报同样
                    # 问题的人不用再靠猜。以前这两样都没有，只能问三轮。
                    diag = describe_book_page(state, books)
                    shot = await self._shot(page, "booklist_empty")
                    if shot:
                        diag += f"\n  截图: {shot}"
                    if not ready:
                        self._after(0, self._books_fetched, [],
                                    "__PAGE_TIMEOUT__", diag)
                        return
                    # 本工具的选择器命中了链接、却没产出条目 → 解析坏了；
                    # 或者页面上引用过作品 id 却没有 chapter-manage 链接 →
                    # 结构变了、或书不在「我的小说」页。两种都说明"这个账号
                    # 没有作品"站不住，不能再把人往建书页上引。
                    # 传 __EMPTY__ 走取证提示；两个旁证都没有才传空错误码，
                    # 落回原有的"还没有作品"引导。
                    suspect = bool(state.get("cards")) or bool(state.get("ids"))
                    self._after(0, self._books_fetched, [],
                                "__EMPTY__" if suspect else "", diag)
                    return
                await save_auth(ctx)
                self._after(0, self._books_fetched, books, None)
            except Exception as e:
                # 异常分支再次确认 URL: 落在登录页才判失效, 否则透传错误
                if page and "/login" in (page.url or ""):
                    self._after(0, self._books_fetched, [], "__SESSION_EXPIRED__")
                else:
                    self._after(0, self._books_fetched, [], str(e))
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

        self.worker.submit(task())

    def _shot(self, page, tag):
        """失败现场截图，落到 error_<tag>_<时间戳>.png（.gitignore 已排除）。

        以前没有任何失败截图：用户只能截自己那一屏 GUI，看不到浏览器里平台
        到底渲染成了什么样，定位全靠猜。空列表/超时这两条路径尤其需要。
        """
        try:
            path = SCRIPT_DIR / f"error_{tag}_{int(time.time())}.png"
            page.screenshot(path=str(path))
            return str(path)
        except Exception as e:
            logger.debug(f"失败现场截图未保存: {e}")
            return ""

    def _books_fetched(self, books, error, diag=""):
        self._books_loading = False
        self.btn_books.configure(state="normal")
        if diag:
            self._log("—— 作品列表页取证 ——")
            for line in diag.split("\n"):
                self._log("  " + line)
        if error == "__PAGE_TIMEOUT__":
            self._log("获取失败: 作品列表在 15 秒内没加载完（多为系统繁忙或网络缓慢，"
                      "例如有其他自动化程序占用资源）。登录状态未必有问题，请稍后"
                      "重试；上面的取证会写明页面到底停在哪一步。")
            return
        if error == "__EMPTY__":
            # 这里只判「页面上到底有没有书的痕迹」（有 id / 有章节管理链接），
            # 不判定是哪一种原因。取证行已经写清了：页面上出现过几个 id、有没有
            # chapter-manage 链接、平台自己的空态文案是什么。issue #4 的用户明明
            # 登着录、书也建好了，却被反复告知"系统繁忙"——差别就在不替人下结论。
            self._log("获取失败: 页面已渲染完成却没抓到作品。上面的取证显示"
                      "页面上其实有内容，所以这不是「这个账号没有作品」——"
                      "具体是哪一种，看上面那几行取证。")
            messagebox.showwarning(
                "作品列表抓不到",
                "页面已经加载完成，但没有抓到任何作品。\n\n"
                "这不一定是「这个账号没有作品」。常见两种：\r\n"
                "  · 这本书建在「短故事」分区，不在「我的小说」页\n"
                "  · 平台改了页面结构，工具的选择器跟不上\n\n"
                "日志里的「—— 作品列表页取证 ——」那段写明了是哪种"
                "（页面自带的空态文案、出现过几个作品 id、有没有章节管理链接、"
                "现场截图）。请把它一并反馈到 issue，谢谢。")
            return
        if error == "__SESSION_EXPIRED__":
            self._log("获取失败: 登录状态可能已失效")
            acct = self._gui_state.get("current_account", "")
            hint = f"，点击「登录/新建」重新登录{f'账号「{acct}」' if acct else ''}" if acct else ""
            if messagebox.askyesno(
                    "登录失效",
                    f"无法访问作品管理页面，登录状态可能已过期。\n是否立即重新登录{hint}？"):
                self._on_login()
            return
        if error:
            self._log(f"获取失败: {error}")
            return
        self.books = books
        # 刷新作品列表: 清除全部缓存
        self._invalidate_caches("all")
        self._hide_volumes()
        if not books:
            # 页面正常打开、接口也返回了，只是一本书都没有（超时/失效已在上面提前
            # return）。本工具只能往**已存在的作品**里传章节，建书必须在网页端完成——
            # 所以这里不是报错，是把人送到建书页，否则新作者到这一步就卡死了。
            self.cmb_book["values"] = []
            self.cmb_book.set(BOOK_EMPTY_HINT)
            self._log("当前账号名下还没有作品。本工具只负责往已有的书里传章节，"
                      "建书要在番茄网页端完成：先去新建一本书，回来点「↻」刷新即可。")
            if messagebox.askyesno(
                    "还没有作品",
                    "当前账号名下还没有作品。\n\n"
                    "本工具只能往已有的书里传章节，新建作品要在番茄网页端做。\n\n"
                    "现在打开番茄的作品管理页去新建吗？\n"
                    "（建好后回到本窗口点「↻」刷新）"):
                webbrowser.open(BOOK_MANAGE_URL)
            return
        display = [
            f"{b['name']}  ({b['chapters']}章, {b['words']}字)"
            for b in books
        ]
        self.cmb_book["values"] = display
        # 恢复上次选择的作品（按账号区分），找不到则默认第一部
        acct = self._gui_state.get("current_account", "")
        key = f"last_book_id_{acct}" if acct else "last_book_id"
        last_id = self._gui_state.get(key, "")
        target_idx = 0
        if last_id:
            for i, b in enumerate(books):
                if b["bookId"] == last_id:
                    target_idx = i
                    break
        self.cmb_book.current(target_idx)
        self._log(f"找到 {len(books)} 部作品。")
        self._on_book_changed()

    # -----------------------------------------------------------------------
    # 目录选择 + 预览
    # -----------------------------------------------------------------------
    def _on_browse_dir(self):
        d = filedialog.askdirectory(
            title="选择章节 MD 文件目录",
            initialdir=self.dir_var.get() or None)
        if not d:
            return
        self.dir_var.set(d)
        self._schedule_config_save()
        self._reload_chapters()

    def _open_chapters_dir(self):
        """在系统文件管理器中打开当前章节文件夹。"""
        dir_path = self.dir_var.get()
        if not dir_path:
            messagebox.showwarning("未选择章节文件夹", PICK_DIR_MSG)
            return
        p = Path(dir_path)
        if not p.is_dir():
            messagebox.showwarning("目录不存在",
                                   f"这个文件夹已经不在了：\n{dir_path}\n\n"
                                   "请点「选择文件夹」重新指定。")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(p))  # noqa: S606  # Windows 资源管理器
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)], check=False)
            else:
                subprocess.run(["xdg-open", str(p)], check=False)
        except Exception as e:
            messagebox.showerror(
                "打开目录失败",
                f"{e}\n\n目录可能已被移动或删除。点「选择文件夹」重新指定。")

    def _on_filter_toggle(self):
        if self.filter_var.get():
            self._filter_row.pack(fill="x", padx=6, pady=(0, 4))
        else:
            self._filter_row.pack_forget()
            self.lbl_filter_info.configure(text="")
        self._apply_date_filter()

    def _show_progress_total(self, total: int):
        """把右下角的「已完成/总数」重置成 0/total。

        扫描失败或目录为空时必须一起归零：预览已经改口说「没有文件」，
        计数还停在上一批（实测 0/297），两句自相矛盾，用户不知道信哪个。
        """
        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{total}")

    def _clear_scan_result(self):
        """清空扫描结果并把右下角的计数归零。

        计数必须一起归零：预览已经改口说「没有文件」，计数还停在上一批
        （实测 0/297），两句自相矛盾，用户不知道信哪个。
        预览文字由调用方自己更新（本函数不碰它）。
        """
        self.files, self.parsed_chapters, self._word_counts = [], [], []
        self._show_progress_total(0)

    def _reload_chapters(self):
        """从磁盘重新扫描并解析章节文件。仅在目录变更/用户点刷新时调用。"""
        self._all_files = []
        self._all_parsed = []
        dir_path = self.dir_var.get()
        if not dir_path:
            self._clear_scan_result()
            return
        p = Path(dir_path)
        if not p.is_dir():
            self._clear_scan_result()
            self._set_preview("这个文件夹不在了。点「选择文件夹」重新指定。")
            return

        try:
            self._all_files = get_md_files(p)
        except OSError as e:
            self._clear_scan_result()
            self._set_preview(f"无法读取目录: {e}")
            return
        if not self._all_files:
            self._clear_scan_result()
            self._set_preview("目录及子文件夹里没有 .md / .txt 文件。\n"
                              "把章节文件放进来后点 ↻ 重新扫描，或换一个文件夹。")
            return

        # 跳过扫描后变得无法读取的文件（云端离线占位/被删/无权限），并保持
        # _all_files 与 _all_parsed 一一对齐——否则解析在列表推导处抛 OSError，
        # 刷新整段中断且二者失配，后续按日期筛选会 IndexError。
        self._all_files, self._all_parsed = parse_md_files(self._all_files)
        if not self._all_files:
            self._clear_scan_result()
            self._set_preview("目录里的文件都读不出来。\n"
                              "常见原因：云盘文件还没下载到本地，或没有读取权限。")
            return
        self._apply_date_filter()

    def _apply_date_filter(self):
        """应用日期筛选 + 去重 + 刷新预览（复用已缓存的解析结果，不重新读取文件）。"""
        if not self._all_files:
            return

        if self.filter_var.get():
            raw = self.filter_date_var.get().strip()
            # 解析与边界语义都跟 CLI --modified-after/before 共用一份:
            # 同一串日期在两个入口必须筛出同一批文件，否则同一份补传清单
            # 换个入口跑就会差一章。晚于=含边界(>=)，早于=不含(<)。
            cutoff_ts = parse_time_spec(raw)
            if cutoff_ts is None:
                self.lbl_filter_info.configure(
                    text="格式错误，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM",
                    foreground="red")
                # 计数也要一起归零：预览说「格式不对」，右下角却还挂着上一批的
                # 总数（实测 0/297），两句自相矛盾。
                self._clear_scan_result()
                self._set_preview("日期筛选的格式不对，改成 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。")
                return
            op = self.filter_op_var.get()
            total = len(self._all_files)
            kept, mtimes = [], []
            for i, f in enumerate(self._all_files):
                try:
                    mt = datetime.fromtimestamp(f.stat().st_mtime)
                except (OSError, OverflowError, ValueError):
                    continue  # 损坏/越界 mtime 会抛 Overflow/ValueError，非仅 OSError
                mtimes.append(mt)
                if (mt.timestamp() >= cutoff_ts) == (op != "早于"):
                    kept.append(i)
            self.files = [self._all_files[i] for i in kept]
            self.parsed_chapters = [self._all_parsed[i] for i in kept]
            if not self.files:
                self._word_counts = []
                self.lbl_filter_info.configure(
                    text=f"筛选: 0/{total} 个文件", foreground="orange")
                if mtimes:
                    lo = min(mtimes).strftime("%Y-%m-%d %H:%M")
                    hi = max(mtimes).strftime("%Y-%m-%d %H:%M")
                    self._set_preview(
                        f"没有符合筛选条件的文件\n"
                        f"条件: {op} {raw} | 文件日期: {lo} ~ {hi}\n"
                        f"把时间调到这个区间内，或切换「早于 / 晚于」。")
                else:
                    self._set_preview(
                        "没有符合筛选条件的文件\n"
                        "调整日期，或取消勾选「按修改日期筛选」。")
                # 底部计数还停在筛选前的数字，与预览里的"没有文件"自相矛盾
                self._show_progress_total(0)
                return
            self.lbl_filter_info.configure(
                text=f"筛选: {len(self.files)}/{total} 个文件",
                foreground=CLR_INK_SOFT)
        else:
            self.files = list(self._all_files)
            self.parsed_chapters = list(self._all_parsed)

        if self.unique_var.get():
            self.parsed_chapters = deduplicate_titles(self.parsed_chapters)
        self._word_counts = [
            len(strip_md_formatting(c)) for _, _, c in self.parsed_chapters]

        self._refresh_preview()

    def _schedule_param_warnings(self) -> list[str]:
        """排期输入框里已经填了、但不会被算进排期的值（空列表 = 没问题）。

        这些值过去是静默处理的：日期写错 → 整块排期不显示；时间里的
        「25:00」→ 被 validate_times 丢掉，一天少一个时段；「排到」填
        「一周」→ 当成留空 = 不限。三处都不吭声，用户要么点「开始上传」
        才被弹窗拦下，要么直接带着和自己填的不一样的排期发出去。
        上传前的预览面板是唯一的复核地方，所以问题写在这儿。
        """
        warns = []
        _perday_msg = f"「每天章数」要填 {PER_DAY_MIN}-{PER_DAY_MAX} 的整数"
        date_str = (self.date_var.get() or "").strip()
        if date_str:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                warns.append(f"起始日期「{date_str}」无效，应为 YYYY-MM-DD")
        raw_time = (self.time_var.get() or "").strip()
        if raw_time:
            # 判据只有一份：整串先问 validate_times 本人。它认的分隔符只有
            # 半角逗号/分号（和全角「，；」），**中文顿号「、」不算**，所以
            # 「08:00、12:00」在它眼里是**一整串非法值** → 整串被丢掉、排期
            # 一个时段都没有。原先这里自己按 [,;、] 切分再逐条问，两边口径
            # 不一致，恰恰漏掉了「整串被丢掉」这一种（也就是这个函数存在的
            # 理由）。先看整串，再逐条看是哪个 token 被丢。
            whole = validate_times(raw_time)
            # 分一次就够：整串和逐条都用这一份切分结果。
            _norm = unicodedata.normalize("NFKC", raw_time)
            _toks = [t.strip() for t in re.split(r"[,;、，；]", _norm) if t.strip()]
            if not whole:
                # 一个都没剩下。可能是两件事之一，也可能两件都有：
                #   ① 值本身不合法（25:00、八点）→ 点名是哪几个
                #   ② 分隔符不认（中文顿号「、」）→ 说清换成什么
                # 两件都有时一起说：只说一件的话，用户改完那一件还会再撞
                # 另一件，白折腾两轮。
                # 「什么算合法的时间」只有一个判据：validate_times 本人。
                # 这里不再自己抄一份正则和范围（抄一份就多一处会对不上）。
                _bad = [t for t in _toks if not validate_times(t)]
                _dun = "、" in _norm      # validate_times 不认的分隔符
                if _bad and _dun:
                    warns.append(
                        f"发布时间「{raw_time}」里有两个问题："
                        f"「{'、'.join(_bad)}」无效（小时 00-23、分钟 00-59）；"
                        f"分隔符「、」也不认，请用半角逗号（如 08:00,12:00）")
                elif _bad:
                    warns.append(f"发布时间「{'、'.join(_bad)}」无效，"
                                 f"应为 HH:MM（小时 00-23、分钟 00-59）")
                elif _dun:
                    warns.append(f"发布时间「{raw_time}」里的分隔符「、」不认，"
                                 f"请用半角逗号（如 08:00,12:00）")
                else:
                    warns.append(f"发布时间「{raw_time}」无法识别，"
                                 f"应为 HH:MM（小时 00-23、分钟 00-59）")
            else:
                dropped = [t for t in _toks if not validate_times(t)]
                if dropped:
                    warns.append("发布时间「" + "、".join(dropped)
                                 + "」无效，应为 HH:MM")
        raw_days = (self.days_ahead_var.get() or "").strip()
        # 只在「排到」这一栏真的摆在界面上时才说它：修改排期模式下它不显示，
        # 提示一个看不见的输入框只会让人去找那个不存在的东西。
        if (raw_days and not raw_days.isdigit()
                and self.ent_days_ahead.winfo_manager() != ""):
            warns.append("「排到…天后」要填整数天数，留空=不限")
        try:
            # 上下限都要查：框里手输 99 不会被微调框拦下，超范围的会被原样
            # 存进 config.json，下一次启动又被悄悄改回 2——用户看着自己填的
            # 数和实际排的数对不上，却没人告诉他。
            _pd = self.perday_var.get()
            if not (PER_DAY_MIN <= _pd <= PER_DAY_MAX):
                warns.append(_perday_msg)
        except tk.TclError:
            warns.append(_perday_msg)
        return warns

    def _refresh_preview(self):
        """仅重新计算排期和刷新预览文本，不重新读取文件。"""
        self._update_guidance()
        if self.uploading:
            # 任务运行期间不许重算预览: 这个方法挂在 date/per_day/time 的
            # trace 上，而那几个输入框在任务期间并没有被禁用。工具与体检的
            # 结果(安全检查明细、缺口报告)正是写在预览面板里的 —— 用户此时
            # 碰一下任何一个输入框，结果就被章节列表盖掉了。
            return
        mode = self.mode_var.get()

        # 修改排期模式: 不依赖本地文件，使用平台章节
        if mode == "reschedule":
            self._refresh_reschedule_preview()
            return

        if not self.files or not self.parsed_chapters:
            return

        # 修改内容模式: 专用预览
        if mode == "edit":
            self._refresh_edit_preview()
            return

        # 按章节序号筛选
        all_indices = list(range(len(self.parsed_chapters)))
        kept_indices, filter_active = self._filter_by_chapter_num(
            all_indices, key=lambda i: self.parsed_chapters[i][0])
        kept_set = set(kept_indices)

        kept_count = len(kept_set)

        # 自动接续: 预览必须跟真正发出去的一致。这段计算原来只在 _on_upload 里跑，
        # 于是勾上之后预览显示的是「按你手填的日期排的全部本地章」，而实际发的是
        # 另一批章、另一个日期——所见非所得，用户只能靠确认框里那个数字兜底。
        autocont_start = None
        autocont_tail = None
        autocont_wait = False
        if mode == "schedule" and self.autocont_var.get():
            _idx = self.cmb_book.current()
            _bid = self.books[_idx]["bookId"] if _idx >= 0 and self.books else None
            _cached = (self._platform_chapters_cache.get(self._chapter_cache_key(_bid))
                       if _bid else None)
            if _cached:
                try:
                    _keep, autocont_start, _gaps, autocont_tail, _qs = self._autocont_plan(
                        _cached, self.parsed_chapters)
                except Exception:
                    _keep, autocont_start, autocont_tail = None, None, None
                if _keep is not None:
                    kept_set = {
                        i for i in kept_set
                        if self.parsed_chapters[i][0] is not None
                        and str(self.parsed_chapters[i][0]).isdigit()
                        and int(self.parsed_chapters[i][0]) in _keep
                    }
                    kept_count = len(kept_set)
                    filter_active = True
            else:
                autocont_wait = True

        # 计算排期（仅筛选后的章节）
        schedule = None
        if mode == "schedule":
            try:
                time_str = self.time_var.get().strip() or "08:00"
                per_day = self.perday_var.get()
                if autocont_start:
                    schedule = self._autocont_schedule(
                        autocont_tail, kept_count, time_str, per_day)
                else:
                    date_str = self.date_var.get()
                    datetime.strptime(date_str, "%Y-%m-%d")
                    schedule = compute_schedule(kept_count, date_str, time_str, per_day)
            except (ValueError, tk.TclError):
                pass

        lines = []
        total_words = 0
        kept_words = 0
        sched_idx = 0
        skip_no_num = 0
        skip_filter = 0
        kept_items = []
        for i, (num, title, content) in enumerate(self.parsed_chapters):
            wc = self._word_counts[i] if i < len(self._word_counts) else len(strip_md_formatting(content))
            total_words += wc
            if i in kept_set:
                kept_words += wc
                kept_items.append((num, title))
                num_str = f"第{num}章" if num else "无章号"
                sched_str = ""
                if schedule:
                    sched_str = f"  [{schedule[sched_idx][0]} {schedule[sched_idx][1]}]"
                sched_idx += 1
                lines.append(f"  {sched_idx:3d}. {num_str} {title}  ({wc}字){sched_str}")
            else:
                if num is None:
                    skip_no_num += 1
                else:
                    skip_filter += 1
        if skip_no_num or skip_filter:
            parts = []
            if skip_no_num:
                parts.append(f"{skip_no_num} 无章节号")
            if skip_filter:
                parts.append(f"{skip_filter} 筛选")
            lines.append(
                f"  [跳过 {skip_no_num + skip_filter} 章: {', '.join(parts)}]")

        display_words = kept_words if filter_active else total_words
        count_str = (f"{kept_count}/{len(self.files)}" if filter_active
                     else str(len(self.files)))
        summary = f"总计: {count_str} 章, {display_words} 字 | 模式: {MODE_LABELS[mode]}"
        if autocont_wait:
            summary += " | 自动接续: 正在读平台队列，稍候"
        elif autocont_start:
            summary += " | 自动接续: 只发平台还没有的章"
        if schedule:
            # 自动接续会先填满队尾那天剩下的槽位，首天章数不再等于每天章数，
            # 按 compute_schedule 的公式算（与 CLI 的 mode_str 同款）
            eff = max(per_day, len(validate_times(time_str)) or 1)
            summary += (f" | 每天{eff}章 | 排期: {schedule[0][0]} {schedule[0][1]}"
                        f" ~ {schedule[-1][0]}")
        if mode == "schedule":
            summary += "".join(f"\n⚠ {w}"
                               for w in self._schedule_param_warnings())
        summary += "".join(f"\n{n}" for n in self._preview_notes(kept_items, mode))

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self._show_progress_total(kept_count)

    @staticmethod
    def _preview_notes(items: list[tuple], mode: str = "schedule") -> list[str]:
        """预览里要提醒用户的两件事：没有章号的文件、重复的章号。

        都是「会被当成一章发出去」的问题，所以只提示、不拦截：
          · 无章号的文件（番外/设定/大纲）默认会被发布，位置按文件名排，
            可能排到全书最前面——而位置 3 天后就移不回来了。以前的预览只写
            一个没人解释的「?」，现在写「无章号」并在这里说明白。
          · 两个文件都是「第1章」时，番茄不会拦，读者会看到两个第 1 章。

        存草稿模式说的是「存成草稿」而不是「发布」：草稿不进章节管理页，
        排到什么位置都不影响目录顺序（之后是你自己手动发），但章号照样会
        写进草稿标题，所以两条提醒仍然有用。

        items 只放**这次真要发出去**的章：被「按章节号筛选」排除掉的就不该
        再提醒（实测筛选器本来就会排除无章号的文件）。
        """
        notes = []
        verb = "存成草稿" if mode == "draft" else "按当前顺序发布"
        no_num = [t for num, t in items if num is None]
        if no_num:
            names = "、".join(no_num[:3])
            if len(no_num) > 3:
                names += "…"
            # 位置后果要说出来：这些文件按文件名排在编号章节的前后不定，
            # 插到前面就会把后面的章整体后移——而位置 3 天后就移不回来了。
            # 存草稿模式不写这句：草稿不进目录，占不到位置。
            if mode == "draft":
                tail = "草稿不进章节目录，不影响章节位置"
            else:
                tail = "它们可能排到编号章节前面，把后面的章整体后移"
            notes.append(f"⚠ {len(no_num)} 个文件没有章号，会{verb}：{names}。"
                         f"{tail}（可用「按章节号筛选」排除）")
        counts = Counter(num for num, _t in items if num is not None)
        dups = [(k, v) for k, v in counts.items() if v > 1]
        if dups:
            desc = "、".join(f"第{k}章 × {v}" for k, v in dups[:3])
            if len(dups) > 3:
                desc += "…"
            notes.append(f"⚠ 章号重复 {len(dups)} 处：{desc}")
        return notes

    def _refresh_edit_preview(self):
        """修改模式专用预览: 显示匹配状态。"""
        self._set_resched_filter_info("")

        idx = self.cmb_book.current()
        book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
        cache_key = self._chapter_cache_key(book_id) if book_id else None

        platform_chapters = []
        if cache_key and cache_key in self._platform_chapters_cache:
            platform_chapters = self._platform_chapters_cache[cache_key]

        lines = []
        matched_count = 0
        total_words = 0

        if platform_chapters:
            matched, unmatched = match_chapters(
                self.parsed_chapters, platform_chapters)

            # 按章节序号筛选
            all_matched_indices = {m[0] for m in matched}
            matched, filter_active = self._filter_by_chapter_num(
                matched, key=lambda m: m[2])
            filtered_out_indices = (all_matched_indices - {m[0] for m in matched}
                                    if filter_active else set())

            self._matched_edit = matched
            matched_count = len(matched)
            matched_indices = {m[0] for m in matched}

            show_idx = 0
            skip_filter = 0
            skip_no_num = 0
            skip_not_found = 0
            for i, (num, title, content) in enumerate(self.parsed_chapters):
                wc = self._word_counts[i] if i < len(self._word_counts) else len(strip_md_formatting(content))
                total_words += wc
                if i in matched_indices:
                    show_idx += 1
                    num_str = f"第{num}章" if num else "无章号"
                    lines.append(
                        f"  {show_idx:3d}. {num_str} {title}  ({wc}字)")
                elif i in filtered_out_indices:
                    skip_filter += 1
                elif num is None:
                    skip_no_num += 1
                else:
                    skip_not_found += 1
            skip_total = skip_filter + skip_no_num + skip_not_found
            if skip_total:
                parts = []
                if skip_no_num:
                    parts.append(f"{skip_no_num} 无章节号")
                if skip_filter:
                    parts.append(f"{skip_filter} 筛选")
                if skip_not_found:
                    parts.append(f"{skip_not_found} 未找到")
                lines.append(
                    f"  [跳过 {skip_total} 章: {', '.join(parts)}]")
        else:
            self._matched_edit = []
            for i, (num, title, content) in enumerate(self.parsed_chapters):
                wc = self._word_counts[i] if i < len(self._word_counts) else len(strip_md_formatting(content))
                total_words += wc
                num_str = f"第{num}章" if num else "无章号"
                lines.append(
                    f"  {i+1:3d}. {num_str} {title}  ({wc}字)")

        summary = f"总计: {len(self.files)} 章, {total_words} 字 | 模式: {MODE_LABELS['edit']}"
        if platform_chapters:
            summary += f" | 匹配: {matched_count}/{len(self.files)}"
        else:
            # 状态说一次就够；此前每行都挂一个 [待获取章节列表]，几千行全是同一句
            summary += " | 正在获取平台章节列表，匹配结果稍后显示"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self._show_progress_total(matched_count)

    # 章节号表达式解析用 fanqie_upload 的共用实现（CLI 的 --chapters 同一套）。
    # 保留这个别名不是历史包袱: test_chapter_filter / test_adversarial_campaign
    # 都拿 FanqieGUI._parse_chapter_spec 当入口攻击解析器（极端表达式、
    # 全角、悬空区间），删掉会让那三个套件直接崩。
    _parse_chapter_spec = staticmethod(parse_chapter_spec)

    def _filter_by_chapter_num(self, items, key):
        """按章节序号筛选列表。

        key(item) 提取章节序号 (int 或 None, None 视为不匹配)。
        - 纯数字单值: 按 ≤ / ≥ 运算符做阈值筛选
        - 含逗号或范围: 集合命中模式，支持 "1,3,5-10" 混用，运算符不适用
        返回 (filtered_items, is_active)。同时更新筛选信息标签。

        命中判定全部交给 fanqie_upload.filter_by_chapter_spec —— CLI 的
        --chapters 走的是同一个函数。这里只把 UI 状态(运算符下拉、提示标签)
        套在外面。两侧对同一表达式筛出不同的章集 = 补传漏章，判定只能有一份。
        """
        # 默认恢复运算符下拉框（组合/范围格式时会覆盖为 disabled）
        self.cmb_resched_filter_op.configure(state="readonly")

        if not self.resched_filter_var.get():
            self._set_resched_filter_info("")
            return items, False
        try:
            raw = unicodedata.normalize(
                "NFKC", self.resched_filter_num_var.get()).strip()
        except tk.TclError:
            return items, False
        if not raw:
            self._set_resched_filter_info("")
            return items, False

        total = len(items)
        if raw.isdigit():
            # 单值走下拉框选的运算符；下拉框只可能是 ≤/≥（构造时已兜底）
            spec = ("≤" if self.resched_filter_op_var.get() == "≤" else "≥") + raw
        else:
            spec = raw
        try:
            kept, active = filter_by_chapter_spec(items, spec, key=key)
        except ValueError:
            self._set_resched_filter_info(
                "请输入数字、范围或组合(如 1,3,5-10)", "red")
            return items, False
        if not raw.isdigit():
            # 组合/范围合法后才禁用运算符下拉（原逻辑如此；非法输入时保持可用）
            self.cmb_resched_filter_op.configure(state="disabled")
        self._set_resched_filter_info(f"筛选: {len(kept)}/{total} 章")
        return kept, active

    def _set_resched_filter_info(self, text="", color=None):
        """写章节号筛选的反馈文字；没有文字时把反馈行收起来，不占高度。

        反馈行与输入框分行（见 _build_ui 的说明），所以平时它不出现，
        只有筛选命中数或格式错误时才占一行。
        """
        self.lbl_resched_filter_info.configure(
            text=text, foreground=color or CLR_INK_SOFT)
        # 用 winfo_manager() 判「这一行是不是被 pack 着」，不用
        # winfo_ismapped()：后者还要窗口已经映射才算 1，窗口还没显示时
        # 一律返回 0，于是「该收起时」会以为它没被 pack 而跳过收起，白留
        # 一行占高度（这行省下来的高度本来就是给预览用的）。
        shown = bool(self._resched_info_row.winfo_manager())
        if text and not shown:
            self._resched_info_row.pack(fill="x", padx=6, pady=(0, 2))
        elif not text and shown:
            self._resched_info_row.pack_forget()

    def _refresh_reschedule_preview(self):
        """修改排期模式预览: 显示平台章节 + 计算的新排期。"""
        self._set_resched_filter_info("")

        idx = self.cmb_book.current()
        book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None

        def _early_return(text):
            self._set_preview(text)
            # 计数也归零：这里没有可改的章，但上一批的「0/297」还挂在右下角
            # 会让人以为还有 297 章。归零由 _show_progress_total 统一管，
            # 不要再手抄一份（清空标签还是写 0/0 的差别也省得各处不一致）。
            self._show_progress_total(0)

        if not book_id:
            _early_return("先在上方选择一部作品，这里会列出它可改排期的章节。")
            return

        cache_key = self._chapter_cache_key(book_id)
        if cache_key not in self._platform_chapters_cache:
            _early_return("正在获取平台章节列表…")
            return

        all_chapters = self._platform_chapters_cache[cache_key]
        if not all_chapters:
            _early_return("这部作品在平台上还没有章节。")
            return

        # 反转顺序（章节管理页最新在前）+ 只保留"待发布"章节
        platform_chapters = [
            ch for ch in reversed(all_chapters)
            if "待发布" in ch.get("status", "")
        ]
        if not platform_chapters:
            _early_return("没有可改排期的章节：只有「待发布」状态的章节能改时间，已发布的不能。")
            return

        # 按章节序号筛选
        platform_chapters, _ = self._filter_by_chapter_num(
            platform_chapters, key=lambda ch: ch.get("chapterNum"))
        if not platform_chapters:
            _early_return("当前章节号筛选把待发布章节都排除了，放宽筛选条件再看。")
            return

        # 计算排期
        schedule = None
        try:
            date_str = self.date_var.get()
            datetime.strptime(date_str, "%Y-%m-%d")
            time_str = self.time_var.get().strip() or "08:00"
            per_day = self.perday_var.get()
            schedule = compute_schedule(
                len(platform_chapters), date_str, time_str, per_day)
        except (ValueError, tk.TclError):
            pass

        lines = []
        for i, ch in enumerate(platform_chapters):
            title = ch.get("title", "")
            sched_str = ""
            if schedule:
                sched_str = f"  [{schedule[i][0]} {schedule[i][1]}]"
            lines.append(f"  {i+1:3d}. {title}{sched_str}")

        count = len(platform_chapters)
        summary = f"总计: {count} 章(待发布) | 模式: {MODE_LABELS['reschedule']}"
        if schedule:
            first_day = schedule[0][0]
            eff = sum(1 for d, _ in schedule if d == first_day)
            summary += f" | 每天{eff}章 | 排期: {schedule[0][0]} ~ {schedule[-1][0]}"
        summary += "".join(f"\n⚠ {w}" for w in self._schedule_param_warnings())

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self._show_progress_total(count)

    # -----------------------------------------------------------------------
    # 定时执行
    # -----------------------------------------------------------------------
    TIMER_PREREFRESH_SEC = 60  # 触发前多少秒刷新一次章节目录

    def _ask_yes_no(self, title, msg):
        """确认对话框。定时(无人值守)模式下不弹窗，记日志并默认继续。"""
        if self._auto_run:
            logger.info(f"[定时] {title}：{msg}（自动继续）")
            return True
        return messagebox.askyesno(title, msg)

    def _begin_task(self, count, label=""):
        """任务开始前的统一开场：锁 UI、重置进度条、接日志，返回章节间延时。

        三条上传/修改路径原来各写一份。漏掉其中一步不会报错，只会表现成
        "任务在跑但日志框一直空白"这种难查的怪相。

        开场先画一条分隔线：日志框不清空（历史对排查有用），而点「开始上传」
        会自动切到日志页——没有分隔的话，开机那些「正在获取作品列表…」
        正好排在本次任务的行前面，看起来就像是上传触发的。
        """
        self._install_log_handler()
        bar = "─" * 46
        self._log(bar)
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log(f"▶ {label or '任务'} · {count} 章 · {stamp}")
        self._log(bar)
        self._set_uploading(True)
        self.progress["value"] = 0
        self.progress["maximum"] = max(count, 1)
        return self._cfg.get("delay_between_chapters", 3)

    def _require_login(self):
        """没登录就提示并返回 False。四个入口原来各写一份这个判断。

        提示统一走 _notify：其中一处原来直接用 messagebox，定时模式下会弹出
        没人点的模态框，把无人值守的整批任务堵死在那儿。
        """
        if AUTH_FILE.exists():
            return True
        self._notify("warning", "需要先登录", LOGIN_FIRST_MSG)
        return False

    def _pack_volume_picker(self):
        """按「合并所有卷」勾选状态显示/隐藏分卷下拉。两处布局刷新共用。"""
        if self.all_volumes_var.get():
            self._lbl_volume_sep.pack_forget()
            self.cmb_volume.pack_forget()
        else:
            self._lbl_volume_sep.pack(side="left", padx=(12, 0))
            self.cmb_volume.pack(side="left", padx=2, pady=4)

    def _notify(self, level, title, msg):
        """提示对话框。定时模式下用日志替代弹窗。level: info/warning/error。"""
        if self._auto_run:
            logfn = {"error": logger.error,
                     "warning": logger.warning}.get(level, logger.info)
            logfn(f"[定时] {title}：{msg}")
            return
        {"info": messagebox.showinfo,
         "warning": messagebox.showwarning,
         "error": messagebox.showerror}[level](title, msg)

    @staticmethod
    def _fmt_hms(secs):
        secs = max(0, int(secs))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _parse_timer_input(raw):
        """解析定时时间字符串，成功返回 datetime，失败返回 None。

        格式集与「按修改日期筛选」不同（定时必须带时分，见
        fanqie_upload.TIMER_INPUT_FORMATS 处的说明），但解析共用一份。
        """
        return parse_datetime(raw, TIMER_INPUT_FORMATS)

    def _timer_preflight_issues(self):
        """返回启动定时前发现的潜在问题列表（用于无人值守前的即时提醒）。"""
        issues = []
        if not AUTH_FILE.exists():
            issues.append("尚未登录")
        idx = self.cmb_book.current()
        has_book = idx >= 0 and bool(self.books)
        if not has_book:
            issues.append("尚未选择作品")
        mode = self.mode_var.get()
        if self._needs_chapter_list():
            # 修改/排期/续排都依赖平台章节列表是否已加载完成。到点执行时缓存
            # 为空，_on_upload 只会发起抓取并 return，这次定时就白等了
            ck = self._chapter_cache_key(self.books[idx]["bookId"]) if has_book else None
            if not (ck and ck in self._platform_chapters_cache):
                issues.append("平台章节列表尚未加载完成（请等待加载或重选作品）")
            elif mode == "edit" and not self._matched_edit:
                issues.append("没有匹配到可修改的章节")
        if mode not in ("edit", "reschedule") and not self.files:
            # 上传类模式需要本地章节文件
            issues.append("尚未选择章节文件夹或无可用章节")
        return issues

    def _toggle_timer(self):
        if self.timer_enabled:
            self._stop_timer()
        else:
            self._start_timer()

    def _start_timer(self):
        target = self._parse_timer_input(self.timer_time_var.get())
        if target is None:
            messagebox.showerror(
                "时间格式错误", "请输入正确的时间，格式: YYYY-MM-DD HH:MM")
            return
        if target <= datetime.now():
            messagebox.showwarning("时间无效", "执行时间必须晚于当前时间。")
            return
        # 防御：取消可能残留的轮询，避免重复 after 链
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None
        # 无人值守前置检查：常见疏漏（未登录/未选作品/未选章节）提前提醒
        issues = self._timer_preflight_issues()
        if issues:
            if not messagebox.askyesno(
                    "启动定时确认",
                    "检测到以下问题，到点可能无法执行：\n  · "
                    + "\n  · ".join(issues)
                    + "\n\n请在执行时间前处理。仍要启动定时吗？"):
                return
        self._timer_target = target
        self.timer_enabled = True
        self._timer_prerefresh_done = False
        self.btn_timer.configure(text="取消定时")
        self.ent_timer.configure(state="disabled")
        self._save_config()
        logger.info(
            f"[定时] 已启动，将于 {target:%Y-%m-%d %H:%M} 自动执行"
            f"（模式={self.mode_var.get()}）。请保持程序运行。")
        self._timer_tick()

    def _stop_timer(self, triggered=False):
        self.timer_enabled = False
        self._timer_waiting_busy = False
        self._timer_prerefresh_done = False
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None
        self.btn_timer.configure(text="启动定时")
        self.ent_timer.configure(state="normal")
        if triggered:
            self.lbl_timer_status.configure(text="✅ 已触发执行", foreground="green")
        else:
            self.lbl_timer_status.configure(text="未启动", foreground=CLR_INK_SOFT)

    def _timer_tick(self):
        if self._closing:
            return
        if not self.timer_enabled or self._timer_target is None:
            return
        now = datetime.now()
        if now >= self._timer_target:
            # 到点但有任务在运行 / 正在登录：保持等待，待其完成后再执行（不丢弃本次定时）
            busy_reason = None
            if self.uploading:
                busy_reason = "有任务正在运行"
            elif self._login_in_progress:
                busy_reason = "正在登录"
            if busy_reason:
                if not self._timer_waiting_busy:
                    self._timer_waiting_busy = True
                    logger.info(f"[定时] 已到执行时间，但{busy_reason}，等待其完成后再执行。")
                self.lbl_timer_status.configure(
                    text=f"⏰ 已到时间，等待（{busy_reason}）…", foreground=CLR_SCHED_TX)
                self._timer_after_id = self.root.after(1000, self._timer_tick)
                return
            self._stop_timer(triggered=True)
            self._trigger_scheduled_run()
            return
        remaining = (self._timer_target - now).total_seconds()
        # 触发前 60 秒刷新一次章节目录，确保使用最新文件（等待期间目录可能有更新）
        if (remaining <= self.TIMER_PREREFRESH_SEC
                and not self._timer_prerefresh_done
                and not self.uploading):
            self._timer_prerefresh_done = True
            logger.info("[定时] 触发前刷新章节目录…")
            try:
                self._reload_chapters()
                logger.info("[定时] 目录刷新完成。")
            except Exception as e:
                logger.warning(f"[定时] 目录刷新失败: {e}")
            # 平台章节表缺着（启动时抓失败/登录晚于选作品）就趁这一分钟补上，
            # 否则到点 _on_upload 只发起抓取就 return，这次定时白等
            if self._needs_chapter_list() and not self._ensure_platform_chapters():
                logger.info("[定时] 平台章节列表缺失，触发前补抓…")
        # 目标时刻就在上方输入框里，状态行不再重复日期（会把这行撑到装不下）。
        # 但要说清「到点跑的是哪个操作」：定时是无人值守跑的，确认框在那条路上
        # 自动放行，而操作模式随时可以在右边一格单选里改掉 —— 只报倒计时，用户
        # 无从知道armed 的到底是「定时发布 12 章」还是「修改内容 5291 章」。
        # 每秒刷新一次，所以改完模式这行立刻跟着变。
        what = MODE_LABELS.get(self.mode_var.get(), "")
        prefix = "🔄 已刷新目录 ｜ " if self._timer_prerefresh_done else ""
        self.lbl_timer_status.configure(
            text=f"⏰ {prefix}倒计时 {self._fmt_hms(remaining)} → {what}",
            foreground=CLR_SCHED_TX)
        self._timer_after_id = self.root.after(1000, self._timer_tick)

    def _trigger_scheduled_run(self):
        """定时到点：自动执行一次当前界面配置的操作。"""
        if self.uploading:
            # 正常路径下 _timer_tick 已拦截忙碌情况；此处为防御性兜底。
            logger.info("[定时] 触发时已有任务在运行，本次跳过。")
            return
        # 先装好日志面板 handler，确保无人值守时的诊断信息（含校验失败原因）可见。
        # 任务真正启动后由 _on_upload/_upload_done 接管 handler 生命周期；
        # 若同步阶段就失败未启动任务，则在下方 finally 中撤回。
        self._install_log_handler()
        logger.info(f"[定时] 到达执行时间，自动开始（模式={self.mode_var.get()}）。")
        self._auto_run = True
        self._auto_run_pending = True
        try:
            self._on_upload()
        finally:
            self._auto_run = False
            # 同步阶段未能启动任务（校验失败等）→ 清除 pending，避免污染后续手动操作
            if not self.uploading:
                self._auto_run_pending = False
                self.lbl_timer_status.configure(
                    text="⚠️ 已触发但未启动（见日志）", foreground="red")
                logger.info("[定时] 本次未启动任务（请检查上方日志中的原因）。")
                self._remove_log_handler()

    # -----------------------------------------------------------------------
    # 上传
    # -----------------------------------------------------------------------
    def _on_upload(self):
        if self.uploading:
            # 正在上传中 -> 请求取消
            self._cancel_requested = True
            self._set_upload_btn(state="disabled", text="正在停止…")
            return
        if self._login_in_progress:
            # 登录期间另起上传会同开第二个浏览器、并发写 AUTH_FILE，
            # 可能把登录正在保存的会话与上传账号互相覆盖（错配账号）。
            self._notify("warning", "登录进行中", "先完成或取消当前登录，再开始上传。")
            return

        # 验证
        if not self._require_login():
            return
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            self._notify("warning", "未选择作品", PICK_BOOK_MSG)
            return
        mode = self.mode_var.get()
        book_id = self.books[idx]["bookId"]
        book_name = self.books[idx]["name"]

        # 修改排期模式: 不需要本地文件
        if mode == "reschedule":
            self._on_upload_reschedule(book_id, book_name)
            return

        if not self.files or not self.parsed_chapters:
            self._notify("warning", "未选择章节文件夹", PICK_DIR_MSG)
            return

        # 修改内容模式
        if mode == "edit":
            self._on_upload_edit(book_id, book_name)
            return

        use_ai = self.use_ai_var.get()

        # 复制数据避免主线程修改
        # 自动接续队列（仅定时发布）：范围和起始日期都由平台队列决定，
        # 不用手填。需要平台章节数据——用「修改内容/修改排期」那套缓存，
        # 没有就提示先刷新，避免在这里再开一次浏览器。
        autocont = (mode == "schedule" and getattr(self, "autocont_var", None)
                    and self.autocont_var.get())
        autocont_start = None
        autocont_tail = None
        autocont_qs = None
        # 自动接续筛出的子集只放局部变量，**绝不写回 self.***：用户在下面的确认框
        # 点「否」（或在「起始日期已过去」提醒里取消）时，若 self.parsed_chapters
        # 已被裁剪，预览就永久只剩那几章 —— 下次哪怕没勾自动接续也只发这几章，
        # 而 total_before_filter 也跟着变小，界面上看不出任何异常。
        autocont_subset = None
        if autocont:
            cached = self._platform_chapters_cache.get(
                self._chapter_cache_key(book_id))
            if not cached:
                # 不再叫用户去别的模式暿缓存（那是让人替工具跑腿）——直接发起抓取。
                # 原先担心「在这里再开一次浏览器」，但上传下一步本来就要开。
                self._ensure_platform_chapters()
                self._notify(
                    "info", "正在获取平台章节",
                    "「自动接续队列」要先知道平台排到哪天了。\n"
                    "已开始获取，等它跑完再点一次「开始上传」即可。")
                return
            keep, autocont_start, gaps, autocont_tail, autocont_qs = self._autocont_plan(
                cached, self.parsed_chapters)
            if not keep:
                self._notify("info", "没有可接续的章节",
                             "本地章节都已经在平台上了。\n\n"
                             "写好新章节放进章节文件夹，点目录旁的「↻」重新扫描后再试。")
                return
            parsed_new, files_new = [], []
            for p_, f_ in zip(self.parsed_chapters, self.files):
                try:
                    n = int(p_[0]) if p_[0] is not None else None
                except (TypeError, ValueError):
                    n = None
                if n in keep:
                    parsed_new.append(p_)
                    files_new.append(f_)
            autocont_subset = (parsed_new, files_new)
            if gaps:
                logger.warning(
                    f"平台中段还缺 {len(gaps)} 章（如 第"
                    + "、第".join(str(n) for n in gaps[:5])
                    + "章…）——这些不能靠发布补回原位，"
                    + f"请用底部的「{BTN_REMAP}」")

        # 按章节序号筛选
        if autocont_subset is not None:
            parsed, files = list(autocont_subset[0]), list(autocont_subset[1])
        else:
            parsed = list(self.parsed_chapters)
            files = list(self.files)
        total_before_filter = len(parsed)
        all_indices = list(range(len(parsed)))
        kept_indices, filter_active = self._filter_by_chapter_num(
            all_indices, key=lambda i: parsed[i][0])
        parsed = [parsed[i] for i in kept_indices]
        files = [files[i] for i in kept_indices]
        if not parsed:
            self._notify("warning", "筛选后没有章节",
                         "当前筛选条件把所有章节都排除了。\n"
                         "放宽「按章节号筛选」或「按修改日期筛选」后再试。")
            return

        # 定时发布参数
        schedule = None
        if mode == "schedule":
            params = self._read_schedule_params(start=autocont_start)
            if params is None:
                return
            date_str, per_day, time_str = params
            if autocont_start:
                schedule = self._autocont_schedule(
                    autocont_tail, len(parsed), time_str, per_day)
                tail_s = (autocont_tail.strftime("%Y-%m-%d %H:%M")
                          if autocont_tail else "空")
                # 队尾时刻和末章分开报：两者重合时只报一次；不重合（排期错乱）
                # 时都报出来 —— 只报一个会让用户以为队列没问题。
                _last = (autocont_qs or {}).get("chapter") or ""
                _last_s = f"（末章 {_last}）" if _last else ""
                logger.info(f"自动接续：平台队列排到 {tail_s}{_last_s}，"
                            f"从 {schedule[0][0]} {schedule[0][1]} 起接着排")
                _inv = int((autocont_qs or {}).get("inverted") or 0)
                _cla = int((autocont_qs or {}).get("clash") or 0)
                if _inv or _cla:
                    logger.warning(schedule_order_warning(_inv, _cla))
            else:
                schedule = compute_schedule(
                    len(parsed), date_str, time_str, per_day)
                # 排期后「读者的阅读顺序 = 目录顺序」还成不成立？
                # 新章只能追加到全书末尾 —— 排在已有章前面，读者会先看到它
                # （2026-10-10 那次续排就是这么把 5 章排到别人前面的）。
                _tail = self._queue_tail_datetime(book_id)
                _conf = schedule_order_conflicts(
                    new_slots=schedule, tail_dt=_tail)
                if _conf:
                    _msg = ("新章只能追加到全书末尾，排在已排好的章前面会让读者"
                            "先看到它。\n")
                    if _tail:
                        _safe = (_tail + timedelta(days=1)).strftime("%Y-%m-%d")
                        _msg += (f"平台队列排到 {_tail:%Y-%m-%d %H:%M} —— "
                                 f"起始日期请填 {_safe} 或更晚。\n")
                    _msg += "\n".join(_conf[:4])
                    self._notify("error", "这个起始日期会打乱发布顺序", _msg)
                    return

        # 确认
        count = len(parsed)
        count_str = (f"{count}/{total_before_filter} 章（已筛选）"
                     if filter_active else f"{count} 章")
        msg = f"即将上传 {count_str} 到「{book_name}」\n模式: {MODE_LABELS[mode]}"
        if schedule:
            msg += f"\n排期: {schedule[0][0]} ~ {schedule[-1][0]}"
        if not self._ask_yes_no("确认上传", msg):
            return

        # 开始
        delay = self._begin_task(count, MODE_LABELS[mode])

        # 无头开关在主线程读取（Tk 变量不能在事件循环线程里取）
        hl = self.headless_var.get()
        async def task():
            try:
                url = NEW_CHAPTER_URL_TPL.format(book_id=book_id)

                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=hl)
                    page = await context.new_page()

                    await page.goto(url)
                    try:
                        await wait_for_editor_ready(page)
                    except Exception as e:
                        # 不止 PWTimeout：页面被关/evaluate 失败等非超时错误若逃出，
                        # 会在浏览器未 close 时直达 pw.stop（无超时保护），浏览器
                        # 挂死场景下任务永不结束、GUI 永久停在上传态（CLI 同位点同因已改）
                        logger.error(f"无法进入编辑器（{e}），请检查 Book ID 和登录状态。")
                        await close_browser_safely(browser)
                        self._after(0, self._upload_done, 0, 0)
                        return

                    # 批次循环与收尾对账在共用执行器里（CLI 同一份实现），
                    # GUI 只注入取消检查与进度回调。
                    success, failed, fail_list = await run_creation_batch(
                        page, parsed, url, book_id=book_id,
                        schedule=schedule, is_draft=(mode == "draft"),
                        use_ai=use_ai,
                        max_retries=self._cfg.get("max_retries", 2),
                        delay=delay,
                        cancel_check=lambda: self._cancel_requested,
                        progress_cb=lambda done, tot: self._after(
                            0, self._update_progress, done, tot),
                        err_tag_fn=lambda i: f"gui_{i}")

                    await save_auth(context)
                    await close_browser_safely(browser)

                    # 汇总与完成通知放在 async with 内：浏览器挂死时
                    # playwright stop（with 退出）可能同样阻塞，不能让它
                    # 挡住结果汇报（实测曾让 GUI"卡死"41 分钟）
                    logger.info(f"{'='*40}")
                    logger.info(f"  上传完成! 成功: {success}  失败: {failed}")
                    log_fail_list(fail_list)
                    logger.info(f"{'='*40}")

                    self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"上传异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    def _update_progress(self, current, total):
        self.progress["value"] = current
        self.lbl_progress.configure(text=f"{current}/{total}")

    def _upload_done(self, success, failed):
        auto = self._auto_run_pending
        # 必须在 _set_uploading(False) 之前取——那里会把标志清掉。
        # 用户主动点「停止」时，剩余章节在执行器里按"用户取消，未处理"记进了
        # 失败清单（口径与其余中止路径一致，续跑章号才生成得出来），但对着
        # 用户不能叫"失败 3269 章"：他自己按的停止键。
        cancelled = self._cancel_requested
        self._auto_run_pending = False
        self._remove_log_handler()
        # 先清缓存再切换上传状态，使 _set_uploading 在缓存为空时正确禁用按钮
        self._invalidate_caches("chapters")
        self._set_uploading(False)

        # 缓存刚被清掉，两种模式都得重拉，只是重拉的东西不同:
        #   修改/排期/续排 —— 要平台章节列表，否则切筛选器时 _refresh_edit_preview
        #                拿不到数据会把 _matched_edit 置空、续排点「开始上传」
        #                又得先抓一遍；
        #   新建类   —— 要「队列排到哪天」。刚把队列往后推了一截，不重取的话
        #                标签和起始日期还停在推之前，下一批照它填就插队了。
        if self._needs_chapter_list():
            self._fetch_platform_chapters_for_edit()
        else:
            _idx = self.cmb_book.current()
            if _idx >= 0 and self.books:
                self._fetch_last_publish(self.books[_idx]["bookId"])

        # 上传完成后将 .auth_state.json 回写到命名账号文件（保持 cookie 新鲜）
        acct = self._gui_state.get("current_account", "")
        if acct and AUTH_FILE.exists():
            named = SCRIPT_DIR / f".auth_{acct}.json"
            try:
                self._copy_auth(AUTH_FILE, named)
            except Exception:
                pass

        if success >= 0:
            word = "未处理" if cancelled else "失败"
            if auto:
                logger.info(f"[定时] {'已停止' if cancelled else '操作完成'}："
                            f"成功 {success} 章，{word} {failed} 章")
                self.lbl_timer_status.configure(
                    text=(f"{'⏹ 已停止' if cancelled else '✅ 定时执行完成'}"
                          f" · 成功 {success} {word} {failed}"),
                    foreground="green")
            else:
                title = "已停止" if cancelled else {
                    "edit": "修改完成", "reschedule": "排期修改完成"}.get(
                        self.mode_var.get(), "上传完成")
                # 失败也得给指路：弹窗只报个数字，用户关掉就再也不知道
                # 是哪一章、原定几点（改期失败只能照原定时间去平台手改）
                tail = ("，续跑章节号见运行日志" if cancelled and failed
                        else "，失败章节及原因见运行日志" if failed else "")
                messagebox.showinfo(
                    title, f"成功 {success} 章，{word} {failed} 章{tail}")
        elif auto:
            # success < 0 表示运行期异常
            self.lbl_timer_status.configure(
                text="❌ 定时执行出错（见日志）", foreground="red")

    # -----------------------------------------------------------------------
    # 修改内容
    # -----------------------------------------------------------------------
    def _on_upload_edit(self, book_id, book_name):
        if not self._matched_edit:
            self._notify("warning", "无匹配章节", "未匹配到任何章节，请确认:\n1. 已选择正确的作品\n2. 章节列表已加载完成\n3. 本地文件包含有效章节号")
            return

        matched = self._matched_edit
        count = len(matched)

        nums = sorted(m[2] for m in matched if isinstance(m[2], int))
        rng = f"（第 {nums[0]}–{nums[-1]} 章）" if nums else ""
        # 重过审是这一步唯一的不可逆代价，确认框原来不说：改完读者端不会立刻
        # 变，而「章节重排…」那条同样的改写是写明了代价的，两处口径得一致。
        msg = (f"即将用本地文件替换「{book_name}」中 {count} 章的正文{rng}。\n"
               f"发布时间不受影响；已公开的章改完要重新过审，读者可能收到更新。")
        if not self._ask_yes_no("确认修改", msg):
            return

        delay = self._begin_task(count, MODE_LABELS["edit"])
        use_ai = self.use_ai_var.get()
        matched_copy = list(matched)

        # 无头开关在主线程读取（Tk 变量不能在事件循环线程里取）
        hl = self.headless_var.get()
        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=hl)
                    page = await context.new_page()

                    # 批次循环（含批末二次尝试）在共用执行器里（CLI 同一份实现），
                    # GUI 只注入取消检查与进度回调。合并后两边同时获得对方原有的防护:
                    # 取消记账（原 GUI 独有）与二次尝试的页面死亡短路（原 CLI 独有）。
                    success, failed, skipped, fail_list = await run_edit_batch(
                        page, matched_copy, use_ai=use_ai,
                        max_retries=self._cfg.get("max_retries", 2), delay=delay,
                        cancel_check=lambda: self._cancel_requested,
                        progress_cb=lambda done, tot: self._after(
                            0, self._update_progress, done, tot))

                    await save_auth(context)
                    await close_browser_safely(browser)

                    # 汇总与完成通知放在 async with 内：浏览器挂死时
                    # playwright stop（with 退出）可能同样阻塞，不能让它
                    # 挡住结果汇报（实测曾让 GUI"卡死"41 分钟）
                    logger.info(f"{'='*40}")
                    logger.info(batch_summary_line(success, failed, skipped))
                    log_fail_list(fail_list)
                    logger.info(f"{'='*40}")

                    self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"修改异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    # -----------------------------------------------------------------------
    # 修改排期
    def _read_schedule_params(self, *, start=None):
        """读取并校验排期三参数，返回 (date_str, per_day, time_str)。

        任一项不合法、或用户在"日期已过去"的确认框里选了取消，弹窗后返回
        None，调用方直接 return。start 给定时跳过日期输入框；注意自动接续时
        返回的 date_str 只用于"日期已过去"校验，真正的起排时刻由
        keep_ahead.schedule_after 接着队尾算（会先填满队尾那天剩下的时间点）。

        定时发布和修改排期两条路曾各抄一份这 20 多行，改一句提示语就得记得
        改两处，漏一处两边行为就不一样。
        """
        if start is not None:
            # 自动接续：这里只做"日期已过去"校验；真实起点（接着队尾的时刻）
            # 由调用方按 schedule_after 算好后再记日志
            date_str = start.strftime("%Y-%m-%d")
            start_dt = datetime.combine(start, datetime.min.time())
        else:
            try:
                date_str = self.date_var.get()
                start_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                self._notify("error", "日期错误", "请输入正确的日期: YYYY-MM-DD")
                return None
        if start_dt.date() < datetime.now().date():
            if not self._ask_yes_no(
                    "日期提醒",
                    f"起始日期 {date_str} 已过去，"
                    f"平台可能拒绝定时发布到过去的日期。\n是否继续？"):
                return None
        try:
            per_day = max(PER_DAY_MIN, self.perday_var.get())
        except tk.TclError:
            self._notify("error", "参数错误", "请输入有效的每天章数")
            return None
        time_str = self.time_var.get().strip() or "08:00"
        if not validate_times(time_str):
            self._notify(
                "error", "时间格式错误",
                "请输入有效的发布时间 (HH:MM)\n"
                "多个时间用逗号分隔, 如: 08:00,12:00,20:00")
            return None
        return date_str, per_day, time_str

    # -----------------------------------------------------------------------
    def _on_upload_reschedule(self, book_id, book_name):
        """修改排期: 在章节管理页批量修改待发布章节的排期设置。"""
        params = self._read_schedule_params()
        if params is None:
            return
        date_str, per_day, time_str = params

        # 获取平台章节，反转顺序 + 只保留"待发布"
        cache_key = self._chapter_cache_key(book_id)
        all_chapters = self._platform_chapters_cache.get(cache_key, [])
        if not all_chapters:
            self._notify("warning", "无章节数据", "章节列表尚未加载，请等待加载完成后重试。")
            return
        platform_chapters = [
            ch for ch in reversed(all_chapters)
            if "待发布" in ch.get("status", "")
        ]
        if not platform_chapters:
            self._notify("info", "没有可改排期的章节",
                         "这部作品里没有「待发布」状态的章节。\n"
                         "已发布的章节不能再改排期。")
            return

        # 按章节序号筛选
        platform_chapters, _ = self._filter_by_chapter_num(
            platform_chapters, key=lambda ch: ch.get("chapterNum"))
        if not platform_chapters:
            self._notify("info", "筛选后没有章节",
                         "当前的章节号筛选把待发布章节都排除了，放宽后再试。")
            return

        # 计算排期并构建 schedule_map（排期未变的章不进 map）
        schedule = compute_schedule(
            len(platform_chapters), date_str, time_str, per_day)
        schedule_map, dup_titles, unchanged, order = build_schedule_map(
            platform_chapters, schedule)
        # 改完之后「读者的阅读顺序 = 目录顺序」还成不成立？
        # 位置不动（位置=章号是对的），动的是时刻 —— 时刻一倒，读者就会先看到
        # 后面那章。2026-10-31 实测全书有 5 处这样的错位。
        _items = rows_to_items(platform_chapters)
        _changes = {}
        for _ch, (_d, _t) in zip(platform_chapters, schedule):
            _pos = _ch.get("pos")
            if isinstance(_pos, int) and not isinstance(_pos, bool):
                _changes[_pos] = slot_timestamp(_d, _t)
        _conf = schedule_order_conflicts(_items, changes=_changes)
        if _conf:
            self._notify(
                "error", "这样改会把发布顺序弄乱",
                "改完之后读者会先看到后面的章（目录顺序 = 位置顺序，发布时刻"
                "必须跟着位置一起往后走）。\n"
                + "\n".join(_conf[:4])
                + "\n\n把起始日期往后挪到队尾之后即可。")
            return
        if dup_titles:
            names = "、".join(dict.fromkeys(dup_titles))  # 去重保序
            # 无人值守(定时)模式下同名章节会导致排期被覆盖、错配，直接中止本次
            if self._auto_run:
                self._notify(
                    "error", "同名章节",
                    f"存在同名章节: {names}，排期可能错配，定时执行已中止。"
                    f"请先在平台修改章节标题后再试。")
                return
            self._notify(
                "warning", "同名章节",
                f"存在同名章节: {names}\n同名章节的排期可能不准确，建议先在平台修改章节标题。")

        count = len(schedule_map)
        if not count:
            self._notify("info", "排期无变化",
                         f"{len(platform_chapters)} 个待发布章节的排期"
                         f"与平台现有一致，无需修改。")
            return
        msg = (f"即将修改「{book_name}」{count} 个待发布章节的排期\n"
               f"排期: {schedule[0][0]} ~ {schedule[-1][0]}")
        if unchanged:
            msg += f"\n另有 {unchanged} 章排期未变，跳过"
        if not self._ask_yes_no("确认修改排期", msg):
            return

        # 开始
        delay = self._begin_task(count, MODE_LABELS["reschedule"])
        smap = dict(schedule_map)
        vol = self._get_selected_volume()
        # "合并所有卷"模式: 传入所有卷名列表
        all_vol_names = None
        if self.all_volumes_var.get():
            vols = self._volumes_cache.get(book_id) or []
            all_vol_names = [
                v["text"] if isinstance(v, dict) else v for v in vols
            ] or None

        # 无头开关在主线程读取（Tk 变量不能在事件循环线程里取）
        hl = self.headless_var.get()
        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=hl)
                    page = await context.new_page()

                    success, failed = await reschedule_on_manage_page(
                        page, book_id, smap,
                        max_retries=self._cfg.get("max_retries", 2),
                        delay=delay,
                        cancel_check=lambda: self._cancel_requested,
                        progress_cb=lambda done, total: self._after(
                            0, self._update_progress, done, total),
                        volume_text=vol,
                        volume_texts=all_vol_names,
                        order=order,
                    )

                    await save_auth(context)
                    await close_browser_safely(browser)

                    # 完成通知放在 async with 内：浏览器挂死时 playwright stop
                    # （with 退出）可能同样阻塞，不能让它挡住结果汇报（实测曾让
                    # GUI"卡死"41 分钟）。横幅和失败清单由 reschedule_on_manage_page
                    # 自己打，比这里更早，同样不受收尾阻塞影响。
                    self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"修改排期异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    # -----------------------------------------------------------------------
    # 一键修复排期
    # -----------------------------------------------------------------------
    def _on_fix_schedule(self):
        """一键修复排期：把排在别人前面的那几章挪到队列末尾之后。

        改的是**时刻**（位置不动、章号不动、正文不动）—— 代价只是这几章的
        发布时间变了，读者不会收到内容更新，也不用重新过审。

        为什么要挪「一整段」而不是只挪错的那几章：挪走一章，它后面的章就变成
        「排在它前面」了 —— 得从第一个错排的位置一直挪到末尾（判据见
        fanqie_upload.schedule_repair_plan）。
        """
        if self.uploading or self._login_in_progress:
            self._notify("warning", "任务进行中",
                         "先等当前任务结束再修复排期。")
            return
        if not self._require_login():
            return
        book_id = self._current_book_or_warn()
        if not book_id:
            return
        self._fetch_platform_items(
            book_id,
            lambda items, vols: self._after(
                0, self._fix_schedule_done, items, vols, None, book_id),
            lambda msg: self._after(
                0, self._fix_schedule_done, None, None, msg, book_id))

    def _current_book_or_warn(self):
        """当前选中的作品 ID；没选就提示一句并返回 None。

        「检查缺口」「修复排期」等入口都要这一步 —— 各写一份的话，改了提示
        措辞只记得改一边。
        """
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            self._notify("warning", "未选择作品", PICK_BOOK_MSG)
            return None
        return self.books[idx]["bookId"]

    def _fetch_platform_items(self, book_id, done_cb, err_cb):
        """后台抓平台章节状态（全书、跨卷）。

        回调：done_cb(items, volumes) / err_cb(错误文案)。
        体检和「修复排期」都要这一份数据 —— 各写一份的话，两边的判据迟早会漂
        （一个按 items 算、一个按 rows 算，报出来的数就对不上）。
        """
        hl = self.headless_var.get()
        self._set_uploading(True, cancellable=False, show_log=False)
        self._set_preview("正在抓取平台章节状态…")

        async def task():
            items = None
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=hl)
                    page = await context.new_page()
                    try:
                        items, _signed, volumes = await fetch_chapter_items(
                            page, book_id)
                    finally:
                        await close_browser_safely(browser)
            except Exception as e:
                logger.error(f"抓取平台章节状态失败: {e}")
                self._after(0, err_cb, str(e))
                return
            self._after(0, done_cb, items, volumes)

        self.worker.submit(task())

    def _fix_schedule_done(self, items, volumes, err, book_id=None):
        """抓完平台状态：算方案 → 给用户确认 → 真改。"""
        self._set_uploading(False)
        if err:
            self._notify("error", "修复排期失败", err)
            return
        try:
            per_day = max(PER_DAY_MIN, self.perday_var.get())
        except tk.TclError:
            per_day = PER_DAY_MIN
        time_str = self.time_var.get().strip() or "08:00"
        plan, lines = schedule_repair_plan(
            items, pub_time=time_str, per_day=per_day)
        if not plan:
            self._set_preview(SCHEDULE_ORDER_CLEAN_LINE)
            self._notify("info", "没有要修的排期",
                         "未公开段的发布时刻与位置顺序一致，读者看到的目录顺序是对的。")
            return
        # 注意别写 markdown 的 **加粗**：这是 Tk 弹窗，不渲染 markdown，
        # 星号会原样显示给用户。
        msg = ("将修改「%s」%d 章的发布时间（位置、章号、正文都不动）：\n"
               % (self._book_name(book_id), len(plan)))
        msg += "\n".join(lines)
        msg += ("\n\n每天 %d 章、时间点 %s；读者不会收到内容更新。"
                % (per_day, time_str))
        if not self._ask_yes_no("确认修复排期", msg):
            self._set_preview("\n".join(lines))
            return
        self._set_preview("\n".join(lines))
        smap = {title: (d, t) for _pos, title, d, t in plan}
        # 推后按「新→旧」处理（平台按目标日累计字数，新→旧能避开旧占位没搬走
        # 时的每日上限；判据与改排期那条路同一份 build_schedule_map）。
        order = "desc"
        vol_names = None
        # 多卷时逐卷处理（卷名用这次抓平台时带回来的那份，不靠界面缓存）
        vols = volumes or []
        if len(vols) > 1:
            vol_names = [v.get("volume_name") or v.get("text") or ""
                         for v in vols]
        self._set_uploading(True, cancellable=False, show_log=True)
        hl = self.headless_var.get()

        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=hl)
                    page = await context.new_page()
                    try:
                        ok, bad = await reschedule_on_manage_page(
                            page, book_id, smap,
                            max_retries=self._cfg.get("max_retries", 2),
                            delay=self._cfg.get("delay_between_chapters", 3),
                            cancel_check=lambda: self._cancel_requested,
                            progress_cb=lambda done, tot: self._after(
                                0, self._update_progress, done, tot),
                            volume_texts=vol_names,
                            order=order)
                    finally:
                        await save_auth(context)
                        await close_browser_safely(browser)
                self._after(0, self._upload_done, ok, bad)
            except Exception as e:
                logger.error(f"修复排期异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    # -----------------------------------------------------------------------
    # 工具类操作：核心逻辑一律复用 tools/ 下已有实现，这里只负责取参数/确认/展示
    # -----------------------------------------------------------------------
    @staticmethod
    def _load_tool(name):
        """加载 tools/<name>/<name>.py —— 直接用 fanqie_upload 的带缓存实现。

        原来这里和 fanqie_upload._load_tool_module 是逐字相同的两份，且都不
        缓存：每次自动接续上传都重新 exec 一遍 keep_ahead，模块顶层的
        sys.path.insert 也跟着累积。
        """
        return _load_tool_module(name)

    def _reader_view_around(self, items, pos, span=2):
        """读者目录里位置 pos 前后看到的章号。

        实现在 fanqie_upload（共享核心）：GUI 的体检报告和 CLI 的重排收尾
        要的是同一份，别各写一遍。
        """
        return reader_view_around(items, pos, span=span)

    def _current_book_id(self):
        """当前选中作品的 book_id；没选返回 None。不弹窗、不做登录检查。

        与 _tool_precheck 分开：后者带一串前置检查和提示框，只适合「准备执行」
        的入口。这里只是读一眼当前选中项，供体检结论按书隔离这类场合使用。

        界面还没建完时也返回 None：底部行动条是最先建的（先 pack 的先分到
        空间），建它的时候就会调 _refresh_audit_badge()，那时作品下拉框还
        不存在——直接读属性会 AttributeError，程序连窗口都开不出来。
        """
        cmb = getattr(self, "cmb_book", None)
        if cmb is None:
            return None
        idx = cmb.current()
        if idx < 0 or not self.books:
            return None
        return self.books[idx]["bookId"]

    def _tool_precheck(self):
        """工具类操作的共同前置：没在跑任务、已登录、选了作品。返回 book_id 或 None。"""
        if self.uploading or self._login_in_progress:
            self._notify("warning", "任务进行中",
                         "先等当前任务结束再用工具。\n"
                         "上传/修改可以点「停止」中断；工具与体检不支持中途取消。")
            return None
        if not self._require_login():
            return None
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            self._notify("warning", "未选择作品", PICK_BOOK_MSG)
            return None
        return self.books[idx]["bookId"]

    def _run_tool_task(self, coro_factory, done_cb=None):
        """把工具协程放到统一的 worker 上跑；无论成败都解除忙标志。"""
        # 工具任务不读 _cancel_requested；安全检查结果写预览页
        self._set_uploading(True, cancellable=False, show_log=False)

        async def task():
            result, err = None, None
            try:
                result = await coro_factory()
            except Exception as e:
                logger.error("操作失败: " + str(e))
                err = str(e)
            self._after(0, self._tool_done, result, err, done_cb)

        self.worker.submit(task())

    def _tool_done(self, result, err, done_cb):
        self._set_uploading(False)
        if err:
            self._notify("error", "操作失败", err)
        elif done_cb:
            done_cb(result)

    def _on_autocont_toggle(self):
        """勾了自动接续就置灰起始日期（改由平台队列末尾决定）。

        勾上的同时就去取平台章节：这个功能本就要知道「队列排到哪天」，
        等到用户点了「开始上传」才发现没数据，就只能把人挡回去。
        """
        self._sync_autocont_state()
        if self.autocont_var.get():
            self._ensure_platform_chapters()
        self._refresh_preview()

    def _needs_chapter_list(self) -> bool:
        """选作品 / 发完一批后是否该直接拉整份平台章节表。

        修改/排期本来就靠它；定时发布勾了「接着上次往后排」也靠它算起点和
        范围。此前续排只在用户手点勾选框或点「开始上传」时才去拉——勾选状态
        从配置恢复的启动路径两者都不经过，于是启动抓了一次（队尾日期），
        点「开始上传」又抓一次（章节表），用户被要求再点一次。
        """
        mode = self.mode_var.get()
        return mode in ("edit", "reschedule") or (
            mode == "schedule" and bool(self.autocont_var.get()))

    def _ensure_platform_chapters(self):
        """平台章节缓存没有就发起一次抓取；已有则 no-op。

        返回 True = 缓存已就绪。不阻塞：抓取在后台跑，调用方自己决定怎么提示。
        """
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            return False
        ck = self._chapter_cache_key(self.books[idx]["bookId"])
        if ck and ck in self._platform_chapters_cache:
            return True
        self._fetch_platform_chapters_for_edit()
        return False

    def _sync_autocont_state(self):
        """按当前模式 + 勾选状态置灰/恢复起始日期输入框（不触发预览刷新）。

        只有「定时发布 + 勾了自动接续」才该置灰——sched_frame 在「修改排期」
        模式下同样会 pack，而那个模式没有自动接续。不看模式的话，在定时发布
        里勾了再切过去，起始日期会一直灰着不能改，而排期照旧值跑。
        """
        on = self.autocont_var.get() and self.mode_var.get() == "schedule"
        # 只有 ent_date 这一个控件。早先还猜了 entry_date / date_entry 两个名字，
        # 那只会让 ent_date 被改名时静默失效。
        try:
            self.ent_date.configure(state="disabled" if on else "normal")
        except tk.TclError:
            pass

    def _autocont_plan(self, cached, parsed):
        """自动接续: 返回 (要发的章号集合, 起始日期, 中段缺口, 队尾时刻, 队列摘要)。

        第四项（队尾时刻）才是排期的真正起点——第一章排在几点由它算；
        「起始日期」只用来做「日期已过去」的校验（plan_refill 自己也是这么
        定义的：start_date 只用来算 need_days）。
        第五项是队列摘要（末章、排期倒挂计数），拿来跟用户说清楚平台的
        真实状态 —— 续排只能保证新章排在队尾之后，已有的错位它救不了。

        复用 tools/keep_ahead 的纯函数 plan_refill —— 只取平台最大章号之后的
        章，中段缺口靠"发上去"补不回原位（新建只能追加到书尾），那是
        「章节重排」的活，这里只提醒不碰。

        队列状态（哪些章还占着坑位、各占哪个时刻）由核心里那一份判据算
        （rows_to_items + queue_tail_dt）—— 这里不再自己转一份：两份判据
        早晚会漂，漂了就出现「标签说排到 9 月底、续排却从明天接着排」。
        """
        tool = self._load_tool("keep_ahead")
        # 缓存里的章节来自 extract_chapters_from_page（接口那条路带
        # display_status/timer_time/pos；DOM 兜底那条路只有中文状态和
        # 日期时间），转换规则只有一份：rows_to_items。
        items = rows_to_items(cached)
        num2path = {}
        for num, _t, _c in parsed:
            if num is None:
                continue
            try:
                num2path[int(num)] = True
            except (TypeError, ValueError):
                pass
        try:
            per_day = max(PER_DAY_MIN, self.perday_var.get())
        except tk.TclError:
            per_day = PER_DAY_MIN
        raw = getattr(self, "days_ahead_var", None)
        raw = raw.get().strip() if raw is not None else ""
        days_ahead = int(raw) if raw.isdigit() and int(raw) > 0 else None
        nums, start, _days, _tail, gaps = tool.plan_refill(
            items, num2path, all_remaining=days_ahead is None,
            days_ahead=days_ahead or 0, per_day=per_day)
        # 队尾时刻：核心里那一份判据（GUI 与命令行必须算出同一个队尾）
        return (set(nums), start, gaps, queue_tail_dt(items),
                queue_summary(queue_state(items)))

    def _autocont_schedule(self, tail_dt, n, time_str, per_day):
        """自动接续的排期：接着平台队尾的**时刻**续排——先填满队尾那天剩下的
        槽位再往后，不再整天跳到次日。算法在 keep_ahead.schedule_after（CLI 同款）。"""
        return self._load_tool("keep_ahead").schedule_after(
            tail_dt, n, time_str, per_day)

    # --- 清空草稿箱 ---------------------------------------------------------
    def _on_tool_clean_drafts(self):
        """先做安全检查（草稿内容本地有没有），确认后再删。删除不可恢复。"""
        book_id = self._tool_precheck()
        if not book_id:
            return
        tool = self._load_tool("clean_drafts")
        hl = self.headless_var.get()
        cdir = self._chapters_dir_for_tools()

        async def survey():
            async with async_playwright() as p:
                browser, context = await create_context(p, headless=hl)
                page = await context.new_page()
                try:
                    drafts, total = await fetch_draft_list(page, book_id)
                finally:
                    await close_browser_safely(browser)
            ok, why, risky = tool.safety_check(
                drafts, tool.local_chapter_nums(cdir))
            return total, ok, why, risky

        def after_survey(res):
            total, ok, why, risky = res
            if not total:
                self._set_preview("草稿箱是空的，没有需要清理的草稿。")
                self._notify("info", "草稿箱是空的", "没有需要清理的草稿。")
                return
            detail = "\n".join("· " + r for r in risky[:8])
            if len(risky) > 8:
                detail += "\n… 共 %d 条" % len(risky)
            self._set_preview(
                "草稿箱清理\n" + "=" * 60 +
                "\n共 %d 条草稿\n%s %s\n%s" % (
                    total, "✓" if ok else "⚠", why, detail))
            if not ok:
                self._notify(
                    "warning", "有草稿本地没有备份",
                    "%s\n\n这些内容只存在于草稿里，删了就找不回来。\n"
                    "已中止——请先把它们存到本地再来清理。" % why)
                return
            if not self._ask_yes_no(
                    "清空草稿箱",
                    "共 %d 条草稿，%s。\n\n删除不可恢复，确定清空吗？" % (total, why)):
                return
            self._run_tool_task(
                lambda: self._clean_drafts_run(book_id, hl, total, tool),
                lambda r: self._notify(
                    "info", "清理完成",
                    "已删除 %d 条，草稿箱现有 %d 条。" % (r[0], r[1])))

        self._set_preview("正在读取草稿箱…")
        self._run_tool_task(survey, after_survey)

    async def _clean_drafts_run(self, book_id, hl, total, tool):
        async with async_playwright() as p:
            browser, context = await create_context(p, headless=hl)
            page = await context.new_page()
            try:
                await tool.open_draft_box(page, book_id)
                done, stop = await tool.delete_drafts(page, book_id, total)
                if stop:
                    logger.warning("提前停止: " + stop)
                _d, after = await fetch_draft_list(page, book_id)
                logger.info("已删除 %d 条，草稿箱现有 %d 条" % (done, after))
                return done, after
            finally:
                await close_browser_safely(browser)

    # --- 章节重排（复用 CLI，先预览再执行）----------------------------------
    def _on_tool_remap(self):
        opts = self._ask_remap_options()
        if opts is None:
            return
        extra, desc = opts
        self._run_cli_tool("remap", BTN_REMAP.rstrip("…"), desc, extra=extra)

    def _ask_remap_options(self):
        """点「章节重排…」先问清范围。返回 (CLI 额外参数, 确认框说明)；取消 None。

        为什么不在底栏加勾选框: 「把已发布的章一起改」是有后果的开关，天天杵在
        「开始上传」旁边迟早被误点；而没有这一步就只能去敲命令行，GUI 用户根本
        够不着这些参数（改哪一段、位置区间、本次上限、闭环怎么处理）。
        选项一次问完，后面的 dry-run → 确认 → 写入照旧。
        """
        got = {}
        audit = self._last_audit if getattr(self, "_last_audit", None) else {}
        # 换过书就别用旧结论：位置是每本书各自从 1 数起的，拿上一本的区间填进来
        # 会指向完全无关的章节。
        if audit.get("book_id") != self._current_book_id():
            audit = {}
        pend_bad = audit.get("pend_bad") or []
        pub_bad = audit.get("pub_bad") or []

        dlg = tk.Toplevel(self.root)
        dlg.title(BTN_REMAP.rstrip("…"))
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        pad = dict(padx=8, pady=5)

        # 默认一律选安全的未公开段。已发布段虽然体检里常常才是真正出问题的那段，
        # 但默认勾上一个"会重写线上内容"的选项是坏习惯——这里给足信息 + 一键填入，
        # 让人自己按，而不是替他按。
        pub = tk.BooleanVar(value=False)
        rng = tk.StringVar(value="")
        lim = tk.StringVar(value="")
        # 闭环处理：自动缓冲（默认）/ 跳过 / 选一个真实文本文件。
        # 合成提示会被平台按内容安全驳回，所以缓冲必须是真实正文。
        # 默认自动解：闭环不解开，那一段的顺序就永远是错的，而且用户看不出
        # 「为什么还有 9 章没改」。代价（换位期间某章正文短暂露出来）写在
        # 下面的红字里，选「跳过」可以不接受这个代价。
        buf_mode = tk.StringVar(value="auto")
        buf_file = tk.StringVar(value="")

        # 控件只建一次。place() 只负责摆放 —— 它被 pub 的 trace 每次切换都调一遍，
        # 在里面 new 控件的话点几下单选框就会把整行摞成两层。
        lb_range = ttk.Label(frm, text="修哪一段", font=("", 10, "bold"))
        rb_pending = ttk.Radiobutton(
            frm, text="未公开段（日常用这个）", variable=pub, value=False)
        rb_published = ttk.Radiobutton(
            frm, text="已公开段", variable=pub, value=True)
        lb_rng = ttk.Label(frm, text="位置区间")
        en_rng = ttk.Entry(frm, textvariable=rng, width=14)
        bt_fill = ttk.Button(frm, text="按体检结论填入")
        lb_rng_hint = ttk.Label(frm, text="全书位置，不是章号；留空=全部")
        lb_lim = ttk.Label(frm, text="本次最多")
        en_lim = ttk.Entry(frm, textvariable=lim, width=14)
        lb_lim_hint = ttk.Label(frm, text="留空=不限；先跑一批看效果")
        head = ttk.Label(frm, text="", foreground="#5a5a5a", justify="left")
        lb_buf = ttk.Label(frm, text="闭环处理", font=("", 10, "bold"))
        rb_buf_skip = ttk.Radiobutton(
            frm, text="跳过（闭环留着不动）", variable=buf_mode, value="skip")
        rb_buf_auto = ttk.Radiobutton(
            frm, text="自动用本地未发布章节缓冲（会短暂露出该章正文）",
            variable=buf_mode, value="auto")
        rb_buf_file = ttk.Radiobutton(
            frm, text="选择真实文本文件（不剧透）",
            variable=buf_mode, value="file")
        en_buf = ttk.Entry(frm, textvariable=buf_file, width=40,
                           state="disabled")
        bt_buf = ttk.Button(frm, text="选择…", state="disabled")
        warn = ttk.Label(frm, foreground="#c0392b", wraplength=500,
                         justify="left", text="")
        # 错误提示走 StringVar + textvariable：Label 没有 set()。写成 Label 再
        # err.set(...) 会在校验分支里抛 AttributeError —— 而异常恰好又发生在
        # 放行之前，于是"看起来拦住了"，实际上一行提示都没显示过。
        err = tk.StringVar(value="")
        err_lb = ttk.Label(frm, foreground="#c0392b", justify="left",
                           textvariable=err)
        row = {}

        def _seg():
            """当前选项对应的错位位置列表。"""
            return pub_bad if pub.get() else pend_bad

        def _fmt(pos):
            return f"{min(pos)}-{max(pos)}" if pos else ""

        def on_pick_buf():
            p = filedialog.askopenfilename(
                parent=dlg, title="选择真实文本缓冲",
                filetypes=[("章节/文本", "*.md *.txt"), ("所有文件", "*.*")])
            if p:
                buf_file.set(p)
                buf_mode.set("file")

        def _buf_state(*_):
            is_file = buf_mode.get() == "file"
            en_buf.configure(state="normal" if is_file else "disabled")
            bt_buf.configure(state="normal" if is_file else "disabled")

        def place():
            """只重新摆放 + 刷新文案，绝不新建控件（见上面那句）。"""
            seg = _seg()
            lb_range.grid(row=0, column=0, sticky="w", **pad)
            rb_pending.grid(row=1, column=1, sticky="w", **pad)
            rb_published.grid(row=2, column=1, sticky="w", **pad)
            # 这两行本身就是"现在到底有什么问题"的答案，比任何解释性文案都准。
            # 两段都列出来，免得以为另一段没事其实有。
            # 但空段写成一句话，不报「错位 0 个」——那像一张填了零的表，
            # 用户得自己判断「0 是不是好消息」（2026-10-31 用户反馈）。
            def _line(lbl, pos):
                return (f"{lbl}错位 {len(pos)} 个（位置 {_fmt(pos)}）" if pos
                        else f"{lbl}没有错位")
            head.configure(
                text=(f"上次检查缺口（{audit['at']:%m-%d %H:%M}）：\n"
                      f"   {_line('未公开段', pend_bad)}\n"
                      f"   {_line('已公开段', pub_bad)}")
                if (pend_bad or pub_bad) else "")
            head.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
            # 按钮要说清「当前这一段」有没有东西。原来固定写「体检没查到可改的」，
            # 而体检报告刚说完另一段有 215 个 —— 那句当场就是假话。
            _other = pub_bad if not pub.get() else pend_bad
            _other_name = "已公开段" if not pub.get() else "未公开段"
            bt_fill.configure(
                state="normal" if seg else "disabled",
                text=(f"填入 {_fmt(seg)}" if seg
                      else (f"『{_other_name}』有 {len(_other)} 个" if _other
                            else "体检没查到可改的")))
            lb_rng.grid(row=4, column=0, sticky="w", **pad)
            en_rng.grid(row=4, column=1, sticky="w", **pad)
            bt_fill.grid(row=4, column=2, sticky="w", **pad)
            lb_rng_hint.grid(row=5, column=1, columnspan=2, sticky="w", **pad)
            lb_lim.grid(row=6, column=0, sticky="w", **pad)
            en_lim.grid(row=6, column=1, sticky="w", **pad)
            lb_lim_hint.grid(row=7, column=1, columnspan=2, sticky="w", **pad)
            lb_buf.grid(row=8, column=0, sticky="w", **pad)
            rb_buf_skip.grid(row=8, column=1, columnspan=2, sticky="w", **pad)
            rb_buf_auto.grid(row=9, column=1, columnspan=2, sticky="w", **pad)
            rb_buf_file.grid(row=10, column=1, sticky="w", **pad)
            en_buf.grid(row=10, column=2, sticky="w", **pad)
            bt_buf.grid(row=10, column=3, sticky="w", **pad)
            cost = (f"   体检查到这一段要改 {len(seg)} 个位置。" if seg
                    else "   这一段体检没查到错位。")
            warn.configure(
                text=(("⚠ 已发布的内容会被整章替换、重新送审，读者可能收到更新；"
                       "要等审核通过才在读者端生效。\n" if pub.get()
                       else "待发布章只改内容、排期不动，读者暂时看不到影响。\n")
                      + "闭环（位置互相占着章号）默认自动用真实正文缓冲解开："
                        "本地未发布章节会短暂露出正文，选文本文件则不剧透。"
                        "合成提示会被平台按内容安全驳回，不能用。\n"
                      + cost))
            warn.grid(row=11, column=0, columnspan=4, sticky="w", **pad)
            err_lb.grid(row=12, column=0, columnspan=4, sticky="w", **pad)
            row["ok"].grid(row=13, column=2, sticky="e", **pad)
            row["cancel"].grid(row=13, column=3, sticky="w", **pad)

        def on_fill():
            seg = _seg()
            if seg:
                rng.set(_fmt(seg))
                err.set("")

        def on_ok():
            # Enter 既绑在这里、又绑在按钮上；按钮那条先执行完会把 dlg 拆掉，
            # 事件再冒上来时若不挡一下就是对已销毁控件操作，Tcl 直接报错。
            if not _dlg_alive(dlg):
                return
            r = (rng.get() or "").strip().replace(" ", "")
            if r and not re.fullmatch(r"\d+-\d+", r):
                # 内联报错而不是弹框：弹框会把焦点踢出对话框，输错一次得重新
                # 找一遍刚才填到哪儿了。
                err.set("位置区间请填「起始-结束」，例如 480-694；留空表示全部。")
                return
            a, b = r.split("-") if r else ("", "")
            if r and int(a) > int(b):
                err.set(f"区间起止颠倒了：起点 {a} 比终点 {b} 大。")
                return
            n = (lim.get() or "").strip()
            if n and not n.isdigit():
                err.set("「本次最多」请填一个正整数，例如 60；留空表示不限。")
                return
            if n and int(n) <= 0:
                err.set("「本次最多」要大于 0；留空表示不限。")
                return
            args = ["--include-published"] if pub.get() else []
            if r:
                args += ["--range", r]
            if n:
                args += ["--limit", n]
            bdesc = ""
            if buf_mode.get() == "skip":
                args += ["--no-buffer"]
                bdesc = "；闭环不自动解（留着不动）"
            elif buf_mode.get() == "auto":
                args += ["--buffer-chapter", "auto"]
                bdesc = "；闭环用本地未发布章节作真实缓冲（可能短暂露出）"
            elif buf_mode.get() == "file":
                p = (buf_file.get() or "").strip()
                if not p:
                    err.set("「选择真实文本文件」模式下请先点「选择…」。")
                    return
                if not Path(p).is_file():
                    err.set(f"缓冲文件不存在：{p}")
                    return
                args += ["--buffer-file", p]
                bdesc = "；闭环用指定真实文本文件作缓冲"
            got["value"] = (
                args,
                ("已发布的章改成『位置 i 装第 i 章』——只改内容，位置不动。"
                 if pub.get() else
                 "把未公开的待发布章按位置重装内容（位置 i 装第 i 章），排期不变。")
                + bdesc,
            )
            dlg.destroy()

        def on_cancel():
            got["value"] = None
            dlg.destroy()

        row["ok"] = ttk.Button(frm, text="查看改写计划", command=on_ok)
        row["cancel"] = ttk.Button(frm, text="取消", command=on_cancel)
        bt_fill.configure(command=on_fill)
        bt_buf.configure(command=on_pick_buf)
        pub.trace_add("write", lambda *_: place())
        buf_mode.trace_add("write", lambda *_: (_buf_state(), place()))
        _buf_state()
        place()
        self._place_over_parent(dlg)
        dlg.focus_force()
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: on_cancel())
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        self.root.wait_window(dlg)
        return got.get("value")

    def _chapters_dir_for_tools(self):
        """取界面上当前的章节目录，不读 config 缓存。

        _cfg["chapters_dir"] 由 _schedule_config_save 延迟 1 秒写入：刚选完文件夹
        就点「章节重排…」，子进程拿到的还是上一本书的目录——remap --run 会拿
        错书的文件改写待发布章节。归属校验只是抽样启发式，拦不住所有情况。
        """
        try:
            d = (self.dir_var.get() or "").strip()
        except Exception:
            d = ""
        fallback = (self._cfg.get("chapters_dir") or "").strip()
        return d or fallback or str(SCRIPT_DIR / "chapters")

    @staticmethod
    def _exec_summary(out, desc):
        """预览之后的确认框文案：只放做决定要用的那几行。

        完整输出已经在预览面板里，确认框再倒一遍 12 行原始输出只会让人以为
        这是个命令行工具——而它其实全程都在 GUI 里点。从输出里摘「要改多少 /
        有多少要等第二轮 / 代价」，其余交给预览面板。
        """
        m = re.search(r"将改写\s*(\d+)\s*个\s*item", out)
        n = int(m.group(1)) if m else 0
        rows = [f"将改写 {n} 个章节"]
        bm = re.search(r"用真实正文缓冲解\s*(\d+)\s*个闭环", out)
        if bm:
            rows.append(f"· 用真实正文缓冲解 {bm.group(1)} 个闭环"
                        f"（换位期间那个位置会短暂露出该章正文，最后写回"
                        f"正确章节）")
        cyc = re.search(r"跳过\s*(\d+)\s*个闭环", out)
        if cyc:
            rows.append(f"· 还有 {cyc.group(1)} 个闭环没动：位置互相占着章号，"
                        f"纯改写解不开；工具不写合成占位（平台会按内容安全驳回），"
                        f"要解就在「闭环处理」里选一种真实正文缓冲")
        if "已发布" in (desc or ""):
            rows.append("· 已发布章的修改要过审：每章提交后等它真正生效再写下一章，"
                        "审核没跑完本次会暂停，等审完重跑继续")
        if desc:
            rows.append(f"· {desc}")
        rows.append("· 完整计划见下方预览面板")
        return "\r\n".join(rows)

    def _run_cli_tool(self, name, title, desc, extra=()):
        """remap 的执行入口：先跑 dry-run 预览，确认后再 --run。

        extra 是这次任务特有的 CLI 参数（如 --include-published --range）。
        预览和真写入都要带上 —— 两边参数不一致的话，预览就预览了个假的。

        直接调 CLI 而不是 import: remap 是长任务、自带浏览器会话，
        独立进程跑不会和 GUI 的事件循环/浏览器抢资源，输出也天然可展示。

        keep_ahead 不走这条路：它是「接着上次往后排」那个勾选框的实现，
        在 GUI 里直接 import 工具模块（见 _load_tool），不起子进程。
        """
        book_id = self._tool_precheck()
        if not book_id:
            return
        script = SCRIPT_DIR / "tools" / name / (name + ".py")
        # -u：子进程的输出必须实时到。管道下 Python 默认按块缓冲，不加 -u
        # 的话进度会攒在缓冲区里一次性吐出来，界面看着还是「没动」——
        # 那正是这次要解决的事。
        base = [sys.executable, "-u", str(script), "--book-id", book_id,
                "--content-dir", self._chapters_dir_for_tools()]
        if self.headless_var.get():
            base.append("--headless")
        # remap 重新提交正文走的是和上传/修改同一条发布流程，
        # AI 申报漏传就成了界面上看不见的合规不一致。
        if self.use_ai_var.get():
            base.append("--use-ai")

        state = {"extra": list(extra)}
        # 进度行的格式跟工具模块共用一份（工具里定义的 STEP_PROGRESS_RE）：
        # 两边各写一份必然对不上，界面会显示错的进度——比没有进度更糟。
        step_rx = getattr(self._load_tool(name), "STEP_PROGRESS_RE", None)

        def run_cli(more, on_done):
            def work():
                buf = []
                try:
                    env = dict(os.environ, PYTHONIOENCODING="utf-8")
                    proc = subprocess.Popen(
                        base + state["extra"] + list(more),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        cwd=str(SCRIPT_DIR), env=env)
                    # 6 小时上限照旧（原来靠 subprocess.run 的 timeout=）：
                    # 改成流式读就拿不到那个参数了，用看门狗线程补上。
                    timed_out = []
                    def watchdog():
                        try:
                            proc.wait(timeout=6 * 3600)
                        except subprocess.TimeoutExpired:
                            timed_out.append(True)
                            proc.kill()
                    threading.Thread(target=watchdog, daemon=True).start()
                    for line in proc.stdout:
                        buf.append(line)
                        self._after(0, self._on_tool_line, step_rx, line)
                    proc.wait()
                    out = "".join(buf)
                    if timed_out:
                        out += "\n⚠ 超过 6 小时仍未结束，已终止。"
                except Exception as e:
                    out = "".join(buf) + "\n启动失败: " + str(e)
                self._after(0, on_done, out)

            # 预览和真写入是两段输出，混在一起看不出哪段是刚跑的：
            # 每次开跑先清空面板，再打这一段的开头，后面是实时追加。
            # 顺序要紧：先清空（此时还没开流），再打开"流"（之后谁都不许冲掉它）。
            self._set_preview(
                (f"正在执行「{title}」（--run）…"
                 if "--run" in list(more)
                 else f"正在预览「{title}」…") + "\n")
            self._tool_streaming = True
            # 重排/工具走子进程，读不到 _cancel_requested；预览结果在预览页
            self._set_uploading(True, cancellable=False, show_log=False)
            threading.Thread(target=work, daemon=True).start()

        def after_preview(out):
            self._set_uploading(False)
            self._tool_streaming = False
            self._set_preview(out.strip() or "(无输出)", tail=True)
            # --limit 截断闭环等：工具明确说这次不会写，别再问"要不要执行"。
            if "本次不会写入" in out and "将改写" not in out:
                self._notify("warning", title,
                             "这段计划没法按当前参数执行，详见预览面板。")
                return
            if "将改写 0 个" in out and "闭环" in out:
                m = re.search(r"跳过\s*(\d+)\s*个闭环", out)
                self._notify("warning", title,
                             f"{m.group(1) if m else '若干'} 个闭环会跳过："
                             f"工具不写合成占位（平台会按内容安全驳回）。"
                             f"想解开就在「闭环处理」里选一种真实正文缓冲，"
                             f"再预览一次。详见预览面板。")
                return
            if ("将改写 0 个" in out or "无需补排" in out
                    or "没有可排的章" in out or "没有需要改写" in out):
                self._notify("info", title, "没有需要处理的章节，详见预览面板。")
                return
            if not self._ask_yes_no(title, self._exec_summary(out, desc)):
                return
            run_cli(["--run"], after_run)

        def after_run(out):
            self._set_uploading(False)
            self._tool_streaming = False
            self._set_preview(out.strip() or "(无输出)", tail=True)
            m2 = re.search(r"成功\s*(\d+)\s*失败\s*(\d+)", out)
            cyc = re.search(r"跳过\s*(\d+)\s*个闭环", out)
            skip = (f"；另有 {cyc.group(1)} 个闭环跳过，"
                    f"可在「闭环处理」里选真实正文缓冲"
                    if cyc else "")
            if "本次暂停" in out:
                m = re.search(r"位置\s*(\d+)\s*的(真实缓冲|改写)已提交，"
                              r"平台审核中", out)
                where = (f"位置 {m.group(1)} 的{m.group(2)}" if m else "有一章")
                msg = (f"{where}已提交、平台审核中——本次暂停；"
                       f"等审核结束重跑本工具即可继续{skip}。详见预览面板。")
            elif "临时借用了真实章节" in out:
                m = re.search(r"位置\s*([\d、]+)\s*临时借用了真实章节", out)
                where = f"位置 {m.group(1)} " if m else ""
                msg = (f"{where}临时借用了真实章节，尚未换回；重跑本工具写回。"
                       f"{skip}详见预览面板。")
            elif m2 and m2.group(1) != "0":
                msg = f"已改写 {m2.group(1)} 个章节{skip}，详见预览面板。"
            else:
                msg = f"详见预览面板{skip}。"
            self._notify("info", title + "完成", msg)

        run_cli([], after_preview)

    def _audit_badge(self):
        """当前这本书的审核卡队列结论（可能来自上一次运行），没有就 None。

        按作品隔离：卡队列是「**这本书**的这一章卡住了」，位置和章号都只对
        那本书有意义。换一部作品还挂着上一本的 ⚠，用户会以为新书也卡住了
        ——而点开体检查的是新书，越查越对不上。
        """
        cur = self._current_book_id()
        if not cur:
            # 没选作品时不显示：这时候点「检查缺口」只会回一句「未选择作品」，
            # 挂着个点不动的 ⚠ 反而让人以为程序卡住了。
            return None
        blk = self._review_blocks().get(cur)
        if isinstance(blk, dict):
            return blk
        # 老数据（加这一栏之前写的）没有 book_id：判不出属于哪本，先当真话
        # 说——等下一次体检写上 book_id，就按书隔离了。
        legacy = self._gui_state.get("review_block")
        if isinstance(legacy, dict) and not legacy.get("book_id"):
            return legacy
        return None

    def _review_blocks(self):
        """每本书的卡队列结论：{book_id: {...}}。

        一本书一份：两个作品都卡着的时候，给 A 体检不该把 B 的警告也抹掉
        ——那正是用户唯一能看见「这一本卡住了」的提示。
        老格式是单个 "review_block"（加这一栏之前写的），读的时候并进来。
        """
        blocks = self._gui_state.get("review_blocks")
        if not isinstance(blocks, dict):
            blocks = {}
        else:
            blocks = dict(blocks)      # 副本：调用方会往里改，原地改就比不出变化
        legacy = self._gui_state.get("review_block")
        if isinstance(legacy, dict):
            _bid = legacy.get("book_id")
            if _bid and _bid not in blocks:
                blocks[_bid] = legacy
        return blocks

    def _audit_tip(self):
        blk = self._audit_badge()
        if not blk:
            # 没有待处理的结论时，说通用的那一条：什么时候该点它。
            return TIP_AUDIT
        at = (blk.get("at") or "")[5:16].replace("T", " ")
        which = self._book_name(blk.get("book_id"))
        where = f"在「{which}」" if which else ""
        # 老数据没有 book_id，判不出是哪本书 —— 那就不说「在哪本书」，别写成
        # 「在发现审核卡队列」（半句话，读着像缺了东西）。
        return (f"什么时候用：最近一次检测发现有一章卡在审核中，后面会连着卡。\n"
                f"最近一次检测（{at}）{where}发现审核卡队列：位置 "
                f"{blk.get('index')}「{(blk.get('title') or '')[:20]}」"
                f"没过审核，后面 {blk.get('blocked')} 章卡在审核中，"
                f"不会自己过审。\n"
                f"用「修改内容」模式改掉那一章就能解（工具里就能改）；"
                f"点开体检看详情。")

    def _book_name(self, book_id):
        """按 book_id 找作品名；找不到返回空串。"""
        if not book_id:
            return ""
        for b in (self.books or []):
            if b.get("bookId") == book_id:
                return b.get("name") or ""
        return ""

    def _refresh_audit_badge(self):
        """有卡队列就在按钮上加个 ⚠，把"最近一次查出来还没解决"摆到眼前。"""
        self.btn_audit.configure(
            text=f"{BTN_AUDIT} ⚠" if self._audit_badge() else BTN_AUDIT)

    def _note_review_block(self, blk, book_id=None):
        """记下这次检测的审核卡队列结论，并刷新按钮上的 ⚠。

        这次没查出来就把**这本书**的旧结论撤掉：徽标要反映最近一次检测
        的结果。以前只增不减——用户改完那一章再体检，按钮上还挂着旧的 ⚠，
        悬停提示还在说「后面 413 章卡在审核中」，而那句已经是假的了（探针
        实测）。别的书的结论不动：给 A 体检不该抹掉 B 的警告。
        """
        blocks = self._review_blocks()
        key = book_id or ""
        # 老格式那一条（没有 book_id，判不出属于哪本书）就当它属于**这次体检
        # 的这本书**，在这儿一起清掉：不清的话它会永远留在状态文件里，在每一
        # 本书上显示一个假警告——而且没人清得掉它（只有查出卡队列才会写盘）。
        legacy = self._gui_state.get("review_block")
        legacy_stale = isinstance(legacy, dict) and not legacy.get("book_id")
        if blk:
            blocks[key] = dict(
                blk, at=datetime.now().isoformat(timespec="seconds"),
                book_id=key)
        else:
            blocks.pop(key, None)
        changed = (blocks != (self._gui_state.get("review_blocks") or {})
                   or legacy_stale)
        if changed:
            self._gui_state["review_blocks"] = blocks
            # 老格式那一条已经被并进来了（或本来就判不出来），删掉，
            # 免得下次又并一遍。
            self._gui_state.pop("review_block", None)
            self._save_gui_state()
        self._refresh_audit_badge()

    # -----------------------------------------------------------------------
    # 检查缺口：体检章节位置，按「谁能修」分段（缺章补不回原位，见下方说明）
    # -----------------------------------------------------------------------
    def _on_audit_gaps(self):
        """缺口体检：抓平台真实状态，按「谁能修」分三段报告。

        A 未公开段位置与章号不符 → 跑 tools/remap/remap.py --run 自动改写。
        B 已公开、发布 3 天内、顺序倒挂 → 只能手机 App 申请+审批+逐章移动，
          所以要在窗口内尽早知道，并显示每章还剩几小时。
        C 已公开、超 3 天 → App 移动窗口关了，位置动不了；但内容改写得动，
          走「章节重排… → 连已发布的章一起改」把位置 i 改回装第 i 章即可。
        纯只读，不改平台任何东西。
        """
        if self.uploading or self._login_in_progress:
            self._notify("warning", "任务进行中",
                         "先等当前任务结束再检查缺口。\n"
                         "上传/修改可以点「停止」中断；工具与体检不支持中途取消。")
            return
        if not self._require_login():
            return
        book_id = self._current_book_or_warn()
        if not book_id:
            return
        # 走统一的 self.worker（全 GUI 单一事件循环）并置忙标志：另起线程 +
        # asyncio.run 会开出第二个循环，体检和上传能同时各开一个浏览器、
        # 并发读写 AUTH_FILE；定时器判"有没有任务在跑"看的也是 self.uploading。
        # 体检结果写在「章节预览」页，且不支持取消。
        # 抓平台状态这段与「修复排期」共用（_fetch_platform_items）。
        self._fetch_platform_items(
            book_id,
            lambda items, vols: self._after(
                0, self._audit_done, items, vols, None, book_id),
            lambda msg: self._after(
                0, self._audit_done, None, None, msg, book_id))

    def _audit_done(self, items, volumes, err, book_id=None):
        """体检收尾（主线程）：无论成败都要解除忙标志，否则界面永久卡在运行态。"""
        self._set_uploading(False)
        if err:
            self._notify("error", "检查缺口失败", err)
            return
        self._render_audit(items, volumes, book_id)

    def _render_audit(self, items, volumes=None, book_id=None):
        """把体检结果渲染到预览面板（主线程）。"""
        rep = audit_chapter_positions(items)
        pend_bad = rep["pending_bad"]
        # 已公开段里「位置 ≠ 章号」的整段 —— 就是 --include-published 要改的范围。
        # 判据在 fanqie_upload.audit_chapter_positions 里（CLI 的体检用同一份），
        # 这里不再自己算一遍：两份判据早晚会漂，漂了就出现「GUI 说 32 章、
        # 改写时却改了 33 章」这种对不上的场面。
        misplaced = rep.get("misplaced_pub") or []
        pub_bad = sorted(r["index"] for r in misplaced)
        # 存给「章节重排…」的选项对话框预填。带上 book_id：换书后位置含义完全
        # 不同，拿上一本的区间去填等于写错书。
        self._last_audit = {"at": datetime.now(), "book_id": self._current_book_id(),
                            "pend_bad": list(pend_bad), "pub_bad": pub_bad}
        blk = rep.get("review_block")
        # 用**这次**报告里的结论，不用别处的旧结论：徽标要反映最近一次检测。
        self._note_review_block(blk, book_id)   # 存 + 刷按钮
        lines = []
        if blk:
            # 放最前面，并加醒目分隔：这是唯一一条「工具修不了、不处理就一直卡着」
            # 的故障，混在 A/B/C 后面会被当成背景噪音。
            lines += ["!" * 60,
                      review_block_headline(blk),
                      review_block_where(blk),
                      "   怎么办：用「修改内容」模式把这一章改掉重新提交"
                      "（工具里就能改），或去番茄后台改这一章；"
                      "改完重新送审，后面这批跟着过",
                      review_block_never_passes(blk),
                      "!" * 60, ""]
        lines += ["缺口体检", "=" * 60]
        # 未公开段排期错位（倒挂 / 撞时刻）：位置体检看不出这个，而它正是
        # 续排出事的直接后果（2026-10-31 实测：位置全对，时刻全乱）。
        _sched_lines = schedule_order_lines(rep)
        if _sched_lines:
            lines += _sched_lines + [""]
        # 审核状态单列一段：原来只有「卡队列」那种会报，散着的审核中/被驳回
        # 章一个字都不提，作者不知道有几章动不了（2026-10-31 用户反馈）。
        _rev_lines = review_status_lines(rep)
        if _rev_lines:
            lines += ["审核状态", "-" * 60] + _rev_lines + [""]
        # 整份报告的组织方式：三段各自只回答四件事——坏在哪 / 改多少 /
        # 怎么点 / 代价，一行一样。取证信息（页面旁证、命令行写法、原理推导）
        # 只在可疑时才展开：那是排查用的，正常读报告的人不需要，
        # 也不该被十几行淹没。
        if volume_count(volumes or {}) > 1:
            # 已跨卷合并；位置按全书连续计（卷序号*10000+卷内位置 → 累加偏移）
            lines.append(f"本作品 {volume_count(volumes)} 卷，已跨卷合并，"
                         f"位置按全书连续计。")
            lines.append("")
        movable = rep["in_window"]
        n_bad = len(pub_bad)
        # 三段各有多少章，问核心里那一份（GUI 与 CLI 共用）——两边各数一遍的话，
        # 迟早会数出不一样的数（「面板说 32 章、真改时改了 33 章」）。
        n_pend, n_mov, n_pub = audit_position_counts(rep)
        # 位置与章号这一维：三段各答一句「有没有错位、错几章、怎么点」。
        # 原来三段恒定输出，空段也占一行（「A …：0 个」「B …：0 章」
        # 「C 已公开段：正常」），像一张填了零的表 —— 用户得自己判断
        # 「0 是不是好消息」（2026-10-31 用户反馈）。所以：全清时只留一句结论；
        # 有错位时空段写成一句话（而不是「0 个」），有内容的段照旧带修法。
        if not (n_pend or n_mov or n_pub):
            lines.append(AUDIT_CLEAN_LINE)
            lines.append("")
        else:
            pend_lo = min(pend_bad) if pend_bad else 0
            pend_hi = max(pend_bad) if pend_bad else 0
            if pend_bad:
                lines.append(f"A 未公开段位置与章号不符：{len(pend_bad)} 个"
                             f"（位置 {self._compress_nums(pend_bad)}）")
                lines.append(f"   修：点「{BTN_REMAP}」保持『未公开段』，"
                             f"再点「填入 {pend_lo}-{pend_hi}」")
                # pend_bad 存的是全书位置（pos）；多卷下原始 index 带卷偏移，
                # 拿 index 查会查空，余量告警就静默消失了
                tt = next((int(x.get("timer_time") or 0) for x in items
                           if x.get("pos", x["index"]) == pend_lo), 0)
                left_h = (tt - datetime.now().timestamp()) / 3600 if tt else None
                if left_h is not None and left_h < MOVE_WINDOW_H:
                    lines.append(f"   ⚠ 位置 {pend_lo} 只剩 {left_h / 24:.1f} 天就发出，"
                                 f"余量已不足 3 天 —— 今天就得补跑")
            else:
                lines.append(AUDIT_SEG_CLEAN["A"])
            lines.append("")
            # B 段只列**真要动**的章：位置不对、确实被挤到后面、且还在 3 天窗口内。
            # 原来这里用的是「章号 < 前面最大章号」的倒挂集合，于是把位置明明正确
            # 的章（实测：第1121章在1121）也列进来，还教用户去 App 移动它——
            # 移动一个位置正确的章没有任何用处（2026-10-31 用户反馈）。
            if movable:
                lines.append(f"B 已公开、位置与章号不符、发布 3 天内"
                             f"（App 里还能移动位置）：{len(movable)} 章")
                for r in movable:
                    lines.append(f"   第{r['num']}章（现在位置 {r['index']}，"
                                 f"应该在位置 {r['num']}），"
                                 f"窗口还剩 {r['left_h']:.1f} 小时")
                lines.append("   修：手机 App 申请调整 → 通过后选中该章 → 移动位置"
                             "（只动这一章，不用重写内容）")
            else:
                lines.append(AUDIT_SEG_CLEAN["B"])
            lines.append("")
            lo, hi = (min(pub_bad), max(pub_bad)) if n_bad else (0, 0)
            if n_bad:
                # 首行报的是**要改的章数**（位置与章号对不上的那些），不是倒挂数：
                # 两者差两个数量级，只报倒挂会让人以为「修 3 章就完事」。
                lines.append(f"C 已公开段位置与章号不符：{n_bad} 章（位置 {lo}-{hi}）")
                brk = rep.get("order_break") or {}
                if brk.get("at"):
                    view = self._reader_view_around(items, brk["at"], span=2)
                    if view:
                        lines.append("   读者目录现在看到：… " + " → ".join(view) + " …")
                    # 断点只用来解释「顺序从哪儿开始乱的」。被隔断的那 N 章里，
                    # 绝大多数位置本身是对的（实测：第481章在481）——把它们说成
                    # 「顺序断裂的章」就是报假错（2026-10-31 用户反馈）。
                    lines.append(f"   {order_break_line(brk)}")
                # 点名要改的是哪几章：位置 + 它现在装的是什么。只给个数字的话，
                # 用户得自己回目录里一个个找。
                _bad = "、".join(f"位置{r['index']} 装第{r['num']}章"
                                 for r in misplaced[:5])
                _more = f"…共{n_bad}章" if n_bad > 5 else ""
                lines.append(f"   要改的就是这 {n_bad} 章：{_bad}{_more}")
                lines.append(f"   修：点「{BTN_REMAP}」选『已公开段』，"
                             f"再点「填入 {lo}-{hi}」")
                cost = "要重写线上内容并重新过审，读者可能收到更新"
                cost += ("；章数多会撞提交上限，分批跑" if n_bad >= 30 else "")
                lines.append("   代价：" + cost)
                # 以下两条只在可疑时才输出：正常错位不需要看这些
                span_n = hi - lo + 1
                if span_n != n_bad:
                    lines.append(f"   注：区间里另有 {span_n - n_bad} 章本来就对，"
                                 f"会自动跳过")
                if movable:
                    lines.append(f"   注：B 段那 {len(movable)} 章窗口一关"
                                 f"就落进 C 段，本次改写也会覆盖它们")
            else:
                lines.append(AUDIT_SEG_CLEAN["C"])
        self._set_preview("\n".join(lines))
        _inv_n = len(rep.get("sched_inverted") or [])
        _cla_n = len(rep.get("sched_clash") or {})
        if _inv_n or _cla_n:
            # 排期错位比「位置错位」更要紧：位置错了读者看不出来（内容还是
            # 对的），排期错了读者直接看到目录顺序是乱的。
            self._notify(
                "warning", "发布顺序有问题",
                f"未公开段有 {_inv_n} 章排在比它前面那章更早的时刻，"
                f"{_cla_n} 个时刻被两章以上占用。\n"
                "读者会先看到排在前面的那几章 —— 用「修改排期」把时刻挪到"
                "队尾之后即可，详见预览面板。")
        elif movable:
            self._notify(
                "warning", "有章节还能救",
                f"{len(movable)} 章已公开但位置与章号不符，还在 3 天移动窗口内。"
                f"详见预览面板，尽快去手机 App 申请移动。")
        else:
            # 位置维度：有多少说多少。原来的写法恒定把「未公开段待重排 0 个」
            # 也报一遍，读着像一张填了零的表（2026-10-31 用户反馈）；全清时
            # 更是「待重排 0 个；已公开段没有还能救的错位章」——「没有还能救的」
            # 这句暗示本来有一批救不了的，读者会以为漏了消息。
            _bits = []
            if pend_bad:
                _bits.append(f"未公开段有 {len(pend_bad)} 章位置与章号不符，"
                             f"可以用「{BTN_REMAP}」改回来")
            if pub_bad:
                _bits.append(f"已公开段有 {len(pub_bad)} 章位置与章号不符"
                             f"（位置 {min(pub_bad)}-{max(pub_bad)}），"
                             f"可以在「{BTN_REMAP}」里选『已公开段』改回来")
            if not _bits:
                self._notify("info", "体检完成", AUDIT_CLEAN_LINE)
            else:
                # 弹窗比面板短，只说工作量 + 去哪儿点。细节留面板。
                self._notify("info", "体检完成",
                             "。\n".join(_bits) + "。\n详见预览面板。")

    # 章号压缩用 fanqie_upload 的共用实现（原来这里有一份逐字相同的拷贝）
    _compress_nums = staticmethod(compress_chapter_nums)

    def _on_close(self):
        if self.uploading:
            if not messagebox.askyesno("任务未完成",
                                       "上传正在进行中，现在退出会中断本次任务。\n"
                                       "确定退出吗？"):
                return
        self._closing = True
        # 若正在等待登录，唤醒被阻塞的登录线程并标记取消，
        # 否则后台线程会一直阻塞在 _login_event.wait()，残留线程与浏览器进程
        if self._login_in_progress:
            self._login_cancelled = True
            try:
                self._login_event.set()
            except Exception:
                pass
        # 取消挂起的定时轮询
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None
        # 刷新待保存的配置，防止防抖期间关闭导致丢失
        if hasattr(self, "_config_save_after"):
            self.root.after_cancel(self._config_save_after)
            self._save_config()
        self._remove_log_handler()
        # 关闭共享浏览器。超时给到 8s 覆盖正常的 pw.stop；若仍超时，主动
        # cancel 协程再 stop 事件循环，避免把 close 丢在半途（孤儿子进程/线程）
        future = None
        try:
            future = self.worker.submit(self._shared.close())
            future.result(timeout=8)
        except Exception as e:
            logger.debug(f"关闭共享浏览器: {e}")
            if future is not None:
                try:
                    future.cancel()
                except Exception:
                    pass
        self.worker.stop()
        self.root.destroy()

    # -----------------------------------------------------------------------
    # 启动
    # -----------------------------------------------------------------------
    def run(self):
        if AUTH_FILE.exists():
            self.root.after(500, self._on_refresh_books)
        # 首次启动自动弹一次欢迎引导，之后仅「❓帮助」按钮触发
        if not self._gui_state.get("onboarded"):
            self._gui_state["onboarded"] = True
            self._save_gui_state()
            self.root.after(400, self._show_welcome)
        self.root.mainloop()


if __name__ == "__main__":
    setup_logging(UPLOAD_LOG_FILE)
    try:
        app = FanqieGUI()
        app.run()
    except Exception as e:
        logger.exception("启动异常")
        # 隐藏控制台（run.bat 后台静默启动）或 pythonw 下 raise 无可见输出，改用 messagebox 告知用户
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror(
                "启动异常",
                f"程序异常退出:\n{e}\n\n详细日志: {UPLOAD_LOG_FILE}")
        except Exception:
            pass
