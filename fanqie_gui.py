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
import shutil
import subprocess
import unicodedata
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
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
        DISPLAY_PENDING as MOVE_PENDING, DISPLAY_PUBLISHED as MOVE_PUBLISHED,
        fetch_draft_list, compress_chapter_nums,
        volume_count,
        log_fail_list, _load_tool_module,
        create_context, save_auth, close_browser_safely, goto_with_login_retry,
        wait_for_editor_ready,
        run_creation_batch, run_edit_batch,
        extract_chapters_from_page, match_chapters,
        reschedule_on_manage_page, detect_volumes, select_volume,
        settle_page,
        AUTH_FILE, BOOK_MANAGE_URL, NEW_CHAPTER_URL_TPL,
        CHAPTER_MANAGE_URL_TPL, SCRIPT_DIR, ZONE_URL, CONFIG_FILE, GUI_STATE_FILE,
        BOOKS_JS, LAST_PUBLISH_JS,
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
STEP_HINTS = {
    1: "下一步：点「登录/新建」，在弹出的浏览器里登录番茄账号",
    2: "下一步：点作品行右侧的「↻」刷新列表，选择一部作品",
    3: "下一步：选择章节文件夹（默认 chapters/），确认下方「章节预览」里出现章节",
    4: "下一步：选好「操作模式」后点「开始上传」（修改模式为「开始修改」）",
}
BOOK_PLACEHOLDER = "点右侧 ↻ 刷新作品列表"
LOGIN_FIRST_MSG = "还没有登录番茄账号。点顶部「登录 / 新建」，在弹出的浏览器里完成登录。"
PICK_BOOK_MSG = "点作品行右侧的「↻」刷新列表，再选择要操作的作品。"
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
                  "「自动处理重名」避免番茄的同名章节限制；"
                  "「按修改日期筛选」只挑近期改过的章节。",
    "操作模式": "五种操作，按需选一：\n"
                "· 定时发布：排好日期时间，到点自动发布\n"
                "· 立即发布：上传后马上发布\n"
                "· 存草稿：只上传存草稿，稍后自己手动发\n"
                "· 修改内容：用本地文件替换已发布章节的正文\n"
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

    async def ensure(self):
        """确保浏览器运行中，返回 context。"""
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return self._context
            await self._teardown()
            self._pw = await async_playwright().start()
            self._browser, self._context = await create_context(
                self._pw, headless=True)
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
# 主 GUI
# ---------------------------------------------------------------------------
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
        # 高度下限按「预览至少看得见」定，不是随手写的：固定区域(品牌栏+账号栏+
        # 引导栏+双栏卡片+行动条)约占 574px，窗口高 ≤660 时章节预览一行都不剩，
        # 而「上传前确认要发哪些章」是不可逆操作前唯一的复核环节。
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
        self.uploading = False
        self._closing = False
        self._cancel_requested = False
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
        """带发丝描边的分区卡片。返回 (卡片外框, 内容区)；把控件塞进内容区。"""
        card = ttk.Frame(parent, style="Card.TFrame")
        head = ttk.Frame(card)
        head.pack(fill="x", padx=12, pady=(8, 2))
        # 标题前的细色条：统一各卡片的结构感；橙=计时（与进度条同色），红=主流程
        tk.Frame(head, width=3, background=CLR_SCHED if sched else CLR_TOMATO
                 ).pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(head, text=title,
                  style="SecSched.TLabel" if sched else "Sec.TLabel").pack(
                      side="left")
        if help_key:
            self._add_help(head, help_key)
        body = ttk.Frame(card)
        body.pack(fill="both", expand=True, padx=12, pady=(2, 9))
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
        底部一条发丝线与内容分隔。"""
        ff = self._ff
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=12, pady=(8, 0))

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
        bar.pack(fill="x", pady=(6, 6))
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
        frm.pack(side="bottom", fill="x", padx=12, pady=(8, 10))
        self.btn_upload = ttk.Button(
            frm, text="开始上传", style="Primary.TButton", command=self._on_upload)
        self.btn_upload.pack(side="left")
        # 检查缺口：体检章节位置，按「谁能修」分段（详见 _on_audit_gaps）
        self.btn_audit = ttk.Button(
            frm, text="检查缺口", command=self._on_audit_gaps)
        self.btn_audit.pack(side="left", padx=(8, 0))
        self._attach_tooltip(
            self.btn_audit,
            "体检章节位置：未公开段可自动重排，已公开段只能在手机 App 里"
            "申请移动（限发布 3 天内），超期则永久错位")
        # 两项维护操作直接摆出来，不再收进「工具 ▾」下拉。
        # 为两个条目做一个菜单，等于多一次点击、还把它们藏起来；而藏起来的代价
        # 不只是麻烦——「检查缺口」报出未公开段错位之后，紧挨着的下一步正是
        # 「章节重排」，两者本该并排可见。菜单项还挂不了 tooltip，说明只能做成
        # 灰色副标题行，展开才看得到。
        # （续排 keep_ahead 不在这里：它已经是「定时发布」模式下的「接着上次
        # 往后排」勾选框，再开一个入口就是同一功能的第二条路径。）
        self.btn_remap = ttk.Button(
            frm, text="章节重排…", command=self._on_tool_remap)
        self.btn_remap.pack(side="left", padx=(8, 0))
        self._attach_tooltip(
            self.btn_remap,
            "把未公开的待发布章按位置重装内容，消掉中段缺口。\n"
            "先预览计划，确认后才真改；排期不变。")
        self.btn_clean = ttk.Button(
            frm, text="清空草稿箱…", command=self._on_tool_clean_drafts)
        self.btn_clean.pack(side="left", padx=(8, 0))
        self._attach_tooltip(
            self.btn_clean,
            "删掉草稿箱里的草稿。删前强制核对：每条草稿的章号\n"
            "在本地要有对应文件，有一条对不上就整批拒绝执行。")
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
        row1.pack(fill="x", pady=(8, 0))
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
        btn_reload = ttk.Button(
            row1, text="↻", width=2, style="Icon.TButton",
            command=self._reload_chapters)
        btn_reload.pack(side="right", padx=(6, 6))
        self._attach_tooltip(btn_reload, "刷新（重新扫描章节文件夹）")
        ttk.Button(row1, text="选择文件夹", command=self._on_browse_dir).pack(
            side="right", padx=(6, 0))
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
        row_opt.pack(fill="x", pady=(6, 0))
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
        self.filter_op_var = tk.StringVar(value="晚于")
        self.cmb_filter_op = ttk.Combobox(
            row_filter, textvariable=self.filter_op_var,
            values=["早于", "晚于"], width=5, state="readonly")
        self.cmb_filter_op.pack(side="left", padx=(6, 2))
        self.cmb_filter_op.bind("<<ComboboxSelected>>",
                                lambda _: self._apply_date_filter())
        self.filter_date_var = tk.StringVar(
            value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.ent_filter_date = ttk.Entry(
            row_filter, textvariable=self.filter_date_var, width=16)
        self.ent_filter_date.pack(side="left", padx=2)
        self.ent_filter_date.bind(
            "<FocusOut>", lambda _: self._apply_date_filter())
        self.ent_filter_date.bind(
            "<Return>", lambda _: self._apply_date_filter())
        # 命中数量比格式说明更该占这行剩余的宽度：格式已经写在输入框的默认值里，
        # 填错时 lbl_filter_info 会直接说明正确格式。此前那句常驻说明把命中数
        # 挤成一条几像素的色块。
        self.lbl_filter_info = ttk.Label(row_filter, text="", foreground=CLR_INK_SOFT)
        self.lbl_filter_info.pack(side="left", padx=6)
        self._filter_row = row_filter

        # --- 4. 操作模式 ---
        card, frm_mode = self._card(right, "操作模式", "操作模式")
        card.pack(fill="x")

        _mode = self._cfg.get("default_mode", "schedule")
        if _mode not in ("schedule", "publish", "draft", "edit", "reschedule"):
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
        modes = [("定时发布", "schedule", row_pub), ("立即发布", "publish", row_pub),
                 ("存草稿", "draft", row_pub), ("修改内容", "edit", row_mod),
                 ("修改排期", "reschedule", row_mod)]
        for text, val, parent in modes:
            rb = ttk.Radiobutton(
                parent, text=text, variable=self.mode_var,
                value=val, command=self._on_mode_change,
                style="Mode.TRadiobutton")
            rb.pack(side="left", padx=(0, 12))
            self._mode_radios.append(rb)

        # 发布选项（所有非草稿模式可见）
        self._row_opts = row_opts = ttk.Frame(frm_mode)
        self.use_ai_var = tk.BooleanVar(value=bool(self._cfg.get("use_ai", False)))
        self.chk_use_ai = ttk.Checkbutton(
            row_opts, text="稿件使用了 AI 创作", variable=self.use_ai_var)
        self.chk_use_ai.pack(side="left", padx=6)
        self.use_ai_var.trace_add("write", lambda *_: self._schedule_config_save())

        # 无头模式：不弹浏览器窗口，后台静默跑。与 CLI 的 --headless / config
        # 的 headless 同一个开关（写回 config.json，两边一致）。
        self.headless_var = tk.BooleanVar(value=bool(self._cfg.get("headless", False)))
        self.chk_headless = ttk.Checkbutton(
            row_opts, text="后台静默运行", variable=self.headless_var)
        self.chk_headless.pack(side="left", padx=6)
        self.headless_var.trace_add("write", lambda *_: self._schedule_config_save())

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
        # 「排到 N 天后」的控件放在下面的排期参数行（r1）里，不放这一行：
        # 它本来就和「每天章数」同类，而这一行在 1280 宽以下已经装不下第 6 个控件
        # （实测「后台静默运行」被压到只剩勾选框、文字全裁掉）。
        self.days_ahead_var = tk.StringVar(
            value=str(self._cfg.get("days_ahead") or ""))

        # 上次发布信息（所有模式可见）
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
        r1.pack(fill="x", padx=6, pady=2)
        ttk.Label(r1, text="起始日期:").pack(side="left")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.date_var = tk.StringVar(value=tomorrow)
        self.ent_date = ttk.Entry(r1, textvariable=self.date_var, width=14)
        self.ent_date.pack(side="left", padx=4)
        ttk.Label(r1, text="每天章数:").pack(side="left", padx=(20, 0))
        _pd = self._cfg.get("default_per_day", 2)
        if not isinstance(_pd, int) or isinstance(_pd, bool) or not (1 <= _pd <= 20):
            _pd = 2  # 非整数/越界会让 tk.IntVar 在构造时抛 TclError、整个 GUI 起不来
        self.perday_var = tk.IntVar(value=_pd)
        ttk.Spinbox(
            r1, from_=1, to=20, textvariable=self.perday_var,
            width=4).pack(side="left", padx=4)
        # 「排到」：留空=把本地剩下的全排上去；填 N=只排到「今天+N 天」。
        # 只排近几天便于改稿（排到几个月后的章想改剧情就得去平台一章章改），
        # 同时仍留着断更保护。与 CLI 的 --days-ahead 对应。仅定时发布模式显示。
        self.lbl_days_ahead = ttk.Label(r1, text="排到:")
        self.ent_days_ahead = ttk.Entry(r1, textvariable=self.days_ahead_var,
                                        width=3)
        self.lbl_days_ahead.pack(side="left", padx=(14, 0))
        self.ent_days_ahead.pack(side="left", padx=4)
        self.lbl_days_ahead_unit = ttk.Label(r1, text="天后")
        self.lbl_days_ahead_unit.pack(side="left")
        for _w in (self.lbl_days_ahead, self.ent_days_ahead,
                   self.lbl_days_ahead_unit):
            self._attach_tooltip(_w, "留空 = 把本地剩下的全排上去。\n填 7 = 只排到 7 天后为止，剩下的下次再排。\n\n排得近，之后想改剧情只改本地文件就行；\n排到几个月后的章，想改得去平台一章章改。")
        self.days_ahead_var.trace_add("write",
                                      lambda *_: self._schedule_config_save())

        # Row 2: 时间（支持多个时间，逗号分隔）
        r2 = ttk.Frame(self.sched_frame)
        r2.pack(fill="x", padx=6, pady=2)
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
        self.ent_resched_filter_num = ttk.Entry(
            self._resched_filter_row, textvariable=self.resched_filter_num_var, width=8)
        self.ent_resched_filter_num.pack(side="left", padx=2)
        ttk.Label(self._resched_filter_row, text="章").pack(side="left")
        self.resched_filter_num_var.trace_add("write", lambda *_: self._refresh_preview())
        # 持久化筛选设置（防抖写盘）
        for _v in (self.resched_filter_var, self.resched_filter_op_var,
                   self.resched_filter_num_var):
            _v.trace_add("write", lambda *_: self._schedule_config_save())
        self.ent_resched_filter_num.bind("<Return>", lambda _: self.txt_preview.focus_set())
        self.lbl_resched_filter_info = ttk.Label(
            self._resched_filter_row, text="", foreground=CLR_INK_SOFT)
        self.lbl_resched_filter_info.pack(side="left", padx=6)

        # --- 4.5 定时执行 ---
        card, frm_timer = self._card(left, "定时执行", "定时执行", sched=True)
        card.pack(fill="x", pady=(8, 0))
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
        row_timer2.pack(fill="x", pady=(4, 0))
        self.lbl_timer_status = ttk.Label(
            row_timer2, text="未启动", foreground=CLR_INK_SOFT)
        self.lbl_timer_status.pack(side="left")
        ttk.Label(row_timer2, style="Cap.TLabel",
                  text="· 到点自动执行一次当前操作").pack(
                      side="left", padx=(6, 0))

        # --- 5. 章节预览 / 运行日志（选项卡） ---
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(side="top", fill="both", expand=True, padx=12, pady=(8, 0))
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
        """顶部新手引导：步骤轨 + 下一步提示，可收起且记住。"""
        ff = self._ff
        self._guide_frame = ttk.Frame(self.root, style="Card.TFrame")
        top = ttk.Frame(self._guide_frame)
        top.pack(fill="x", padx=12, pady=(9, 2))
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
        self.lbl_next_step = ttk.Label(
            self._guide_frame, text="", foreground=CLR_SCHED_D, font=(ff, 9))
        self.lbl_next_step.pack(anchor="w", padx=12, pady=(0, 9))

        # 收起后的细长再入口
        self._guide_stub = ttk.Frame(self.root)
        link = ttk.Label(self._guide_stub, text="▸ 新手引导",
                         foreground=CLR_TOMATO_D, cursor="hand2",
                         font=(ff, 9, "underline"))
        link.pack(side="left", padx=4)
        link.bind("<Button-1>", lambda _: self._set_guidance_collapsed(False))

        # 按记忆决定初始展开/收起；从未表态过时按屏幕高度定默认——引导栏占 79px，
        # 矮屏上展开它，下方章节预览就只剩两三行。
        _collapsed = self._gui_state.get("guidance_collapsed")
        if _collapsed is None:
            _collapsed = self.root.winfo_screenheight() < 900
        if _collapsed:
            self._guide_stub.pack(fill="x", padx=12, pady=(6, 0))
        else:
            self._guide_frame.pack(fill="x", padx=12, pady=(6, 0))

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
        """按当前状态刷新引导栏的步骤高亮与「下一步」提示。"""
        if not getattr(self, "_step_labels", None):
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
        self.lbl_next_step.configure(text=STEP_HINTS.get(step, ""))

    def _set_guidance_collapsed(self, collapsed: bool):
        """收起/展开引导栏，并记住选择。"""
        self._gui_state["guidance_collapsed"] = collapsed
        self._save_gui_state()
        if collapsed:
            self._guide_frame.pack_forget()
            self._guide_stub.pack(fill="x", padx=12, pady=(6, 0),
                                  before=self._acct_frame)
        else:
            self._guide_stub.pack_forget()
            self._guide_frame.pack(fill="x", padx=12, pady=(6, 0),
                                   before=self._acct_frame)
            self._update_guidance()

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
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        ww, wh = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{(sw - ww) // 2}+{(sh - wh) // 3}")

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

    def _scan_accounts(self) -> list[str]:
        """扫描 SCRIPT_DIR 下所有 .auth_*.json，返回账号名称列表。"""
        names: list[str] = []
        for p in sorted(SCRIPT_DIR.glob(".auth_*.json")):
            fn = p.name                     # .auth_作家A.json
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
            # 登录期间切换账号会 copy2 覆盖 AUTH_FILE，与登录保存的会话争用，
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
            shutil.copy2(str(src), str(AUTH_FILE))
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
        self.lbl_last_publish.pack_forget()
        self._volume_frame.pack_forget()
        self.chk_use_ai.pack_forget()
        self.chk_headless.pack_forget()
        self.chk_autocont.pack_forget()
        for _w in ("lbl_days_ahead", "ent_days_ahead", "lbl_days_ahead_unit"):
            getattr(self, _w).pack_forget()
        self._row_opts.pack_forget()

        # --- 2. 按模式显示组件（注意 pack 顺序决定布局顺序） ---
        #   lbl_last_publish:   all modes
        #   _volume_frame:      edit, reschedule (仅多卷时；勾选"合并所有卷"时隐藏分卷下拉)
        #   sched_frame:        schedule, reschedule
        #   _resched_filter_row: all modes
        #   chk_use_ai:         schedule, publish, edit
        #   chk_headless:       all modes（无头对每种操作都生效，所以常驻；
        #                       与 chk_use_ai 一起 forget/repack 才能保住左右顺序）
        has_vols = bool(self.cmb_volume["values"])
        self.lbl_last_publish.pack(fill="x", padx=12, pady=(0, 4))
        if mode in ("edit", "reschedule") and has_vols:
            # 勾选"合并所有卷"时隐藏分卷下拉，只保留复选框
            self._pack_volume_picker()
            self._volume_frame.pack(fill="x", padx=6, pady=(0, 4))
        if mode in ("schedule", "reschedule"):
            self.sched_frame.pack(fill="x", padx=6, pady=4)
        self._resched_filter_row.pack(fill="x", padx=6, pady=(0, 4))
        self._row_opts.pack(fill="x", padx=6, pady=(0, 4), before=self.lbl_last_publish)
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
        self.chk_headless.pack(side="left", padx=6)

        # --- 3. 上传按钮文字和状态 ---
        btn_text = {"edit": "开始修改", "reschedule": "开始修改"}.get(
            mode, "开始上传")
        if not self.uploading:
            self.btn_upload.configure(text=btn_text)
            if mode in ("edit", "reschedule"):
                idx = self.cmb_book.current()
                book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
                ck = self._chapter_cache_key(book_id) if book_id else None
                if not (ck and ck in self._platform_chapters_cache):
                    self.btn_upload.configure(state="disabled")
                else:
                    self.btn_upload.configure(state="normal")
            else:
                self.btn_upload.configure(state="normal")

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

    def _set_preview(self, text):
        self.txt_preview.configure(state="normal")
        self.txt_preview.delete("1.0", tk.END)
        self.txt_preview.insert("1.0", text)
        self.txt_preview.configure(state="disabled")
        self._update_guidance()

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
        """原子写 JSON：先写临时文件再 replace，避免进程中途被杀留下半截文件。"""
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            tmp.replace(path)  # 同目录 rename，Windows 上原子
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
            logger.debug(f"保存 config.json 失败: {e}")

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
            logger.debug(f"保存 .gui_state.json 失败: {e}")

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
                self.btn_upload.configure(state="normal", text="停止")
            else:
                self.btn_upload.configure(state="disabled", text="进行中…")
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
            self.btn_upload.configure(state="normal", text=btn_text)
            # 修改内容/修改排期模式下如果未载入章节列表则禁用
            if self.mode_var.get() in ("edit", "reschedule"):
                idx = self.cmb_book.current()
                book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
                ck = self._chapter_cache_key(book_id) if book_id else None
                if not (ck and ck in self._platform_chapters_cache):
                    self.btn_upload.configure(state="disabled")
        # 上传期间禁用所有可能影响状态的控件
        ctrl_state = "disabled" if active else "normal"
        self.btn_books.configure(state=ctrl_state)
        self.btn_login.configure(state=ctrl_state)
        self.cmb_book.configure(state="disabled" if active else "readonly")
        self.cmb_account.configure(state="disabled" if active else "readonly")
        self.btn_open_manage.configure(state=ctrl_state)
        # 检查缺口按钮：任务进行中禁用（避免同开第二个浏览器、并发写 AUTH_FILE）
        if hasattr(self, "btn_audit"):
            self.btn_audit.configure(state=ctrl_state)
        for _b in ("btn_remap", "btn_clean"):
            if hasattr(self, _b):
                getattr(self, _b).configure(state=ctrl_state)
        for rb in self._mode_radios:
            rb.configure(state=ctrl_state)

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

        # 修改内容/修改排期模式: 走专用的章节列表获取（同时获取上次发布信息）
        if self.mode_var.get() in ("edit", "reschedule"):
            self.btn_upload.configure(state="disabled")
            self._fetch_platform_chapters_for_edit()
        elif book_id in self._last_publish_cache:
            # 有缓存直接用（_apply_last_publish 会通过 date_var trace 触发预览刷新）
            self._apply_last_publish(self._last_publish_cache[book_id])
        elif AUTH_FILE.exists():
            self._fetch_last_publish(book_id)

        self._refresh_preview()

    def _fetch_last_publish(self, book_id):
        """后台重取「队列排到哪天」（仅章节管理页首页）。

        两个调用时机: 选作品时，以及**发完一批之后**。后者很容易漏:
        上传结束会 _invalidate_caches("chapters") 把缓存清掉，但只有修改/排期
        模式会重拉；定时发布模式清了不补，标签上那个日期也不会变——
        于是刚把队列往后推了一截，界面上还写着推之前的日期，下一批照它填就插队了。
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
                ctx = await self._shared.ensure()
                page = await ctx.new_page()
                if self._fetch_gen != gen:
                    return
                url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)
                await page.goto(url)
                await settle_page(page)
                try:
                    await page.wait_for_selector(
                        "tr td", timeout=get_browser_timeout())
                except PWTimeout:
                    pass
                if self._fetch_gen != gen:
                    return
                # 仅首次检测卷（结果会缓存，含 None 表示无多卷）
                if not volumes_known:
                    vol_info = await detect_volumes(page)
                    self._after(
                        0, self._volumes_detected, book_id, vol_info)
                result = await page.evaluate(LAST_PUBLISH_JS)
                self._after(0, self._last_publish_fetched, book_id, result)
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
            self.lbl_last_publish.configure(
                text="暂无发布记录", foreground=CLR_INK_SOFT)

    def _apply_last_publish(self, info):
        """将「队列排到哪天」显示到 UI 并自动建议次日为起始日期。

        取的是章节表里的**最大时间**，不过滤状态——包含待发布章，所以它是
        「队列排到哪天」而不是「上次发了什么」。早先标成「上次发布」，结果是
        用户看到一个未来日期也不敲他，反而手填一个早得多的日期——新章于是
        插在了队列中间，比它前面的章先公开。

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
        chapter = info.get("chapter", "")
        label = f"队列排到: {date_str} {time_str}"
        if chapter:
            label += f" ({chapter})"
        self.lbl_last_publish.configure(text=label, foreground=CLR_SCHED_TX)

        # 自动建议: 起始日期 = 上次日期 + 1 天
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
        """填充卷选项，在 edit/reschedule 模式下显示（紧跟 lbl_last_publish 之后）。"""
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
                self.btn_upload.configure(state="disabled")
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
                self.btn_upload.configure(state="normal")
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
                self.btn_upload.configure(state="normal")
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
                ctx = await self._shared.ensure()
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
                    last_pub = None
                    for vi, vol in enumerate(vol_list):
                        vol_name = vol["text"] if isinstance(vol, dict) else vol
                        if self._fetch_gen != gen:
                            return
                        msg = f"正在索引分卷 ({vi+1}/{len(vol_list)}): {vol_name}..."
                        self._after(0, lambda m=msg: self.lbl_last_publish.configure(
                            text=m, foreground=CLR_INK_SOFT))
                        await select_volume(page, vol_name)
                        chs, lp = await extract_chapters_from_page(
                            page, book_id, on_progress=on_page_progress)
                        all_chapters.extend(chs)
                        if lp and not last_pub:
                            last_pub = lp
                    chapters = all_chapters
                else:
                    # 单卷模式: 切换到指定卷
                    if selected_vol and has_vols:
                        await select_volume(page, selected_vol)
                    chapters, last_pub = await extract_chapters_from_page(
                        page, book_id, on_progress=on_page_progress)

                if self._fetch_gen != gen:
                    return
                self._after(0, self._on_platform_chapters_fetched,
                            book_id, chapters, None, last_pub, gen)
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
                                      last_pub=None, gen=None):
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
        # 缓存上次发布信息（来自同一浏览器会话）
        if last_pub and book_id not in self._last_publish_cache:
            self._last_publish_cache[book_id] = last_pub
        self.lbl_last_publish.configure(
            text=f"已索引 {len(chapters)} 个章节", foreground=CLR_SCHED_TX)
        # 载入完成，恢复上传按钮
        if not self.uploading and self.mode_var.get() in ("edit", "reschedule"):
            self.btn_upload.configure(state="normal")
        self._refresh_preview()

    # -----------------------------------------------------------------------
    # 登录
    # -----------------------------------------------------------------------
    def _prompt_account_name(self):
        """弹"账号名称"对话框并校验，返回合法名称；取消/名称非法/放弃覆盖时返回 None。"""
        current_acct = self._gui_state.get("current_account", "")
        raw = simpledialog.askstring(
            "账号名称",
            "给这个账号起一个本地名称（如 作家A）：\n"
            "· 仅用于在本工具里区分多个账号，不是番茄笔名\n"
            "· 点「确定」后会打开浏览器，请在浏览器里登录番茄账号",
            parent=self.root,
            initialvalue=current_acct,
        )
        if not raw or not raw.strip():
            return None
        name = self._sanitize_account_name(raw)
        if not name:
            messagebox.showerror("名称无效",
                                 "账号名称包含非法字符或为保留名，请重新输入。")
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
        # 必须在弹"账号名称"对话框【之前】就占住登录态：simpledialog 的 wait_window
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
                                    "保存登录状态失败（浏览器可能已被提前关闭）。"
                                    "请重新点「登录/新建」，登录后先点"
                                    "「✔ 登录完成」再关浏览器。")
                        return

                    # 将活跃 auth 复制为命名文件
                    named = SCRIPT_DIR / f".auth_{name}.json"
                    shutil.copy2(str(AUTH_FILE), str(named))

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
            if self._looks_like_missing_browser(error):
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

    @staticmethod
    def _looks_like_missing_browser(error) -> bool:
        """判断错误是否为"Playwright 浏览器内核未安装/损坏"。

        首次安装未跑 `playwright install chromium`、或升级后浏览器没同步时，
        launch() 会抛此类错误，是新用户"点登录没反应"的最常见根因。
        """
        s = str(error).lower()
        return ("executable doesn't exist" in s
                or "playwright install" in s
                or "please run the following command" in s)

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
                ctx = await self._shared.ensure()
                page = await ctx.new_page()
                # domcontentloaded 比 networkidle 可靠: 平台埋点/轮询会拖死 networkidle
                # 致使 goto 超时被误判为会话失效。
                # goto_with_login_retry 会在跳登录页时重试一次：系统繁忙导致
                # 鉴权 API 超时会被 SPA 误判成未登录，重试即可与真失效区分。
                if not await goto_with_login_retry(
                        page, BOOK_MANAGE_URL, wait_until="domcontentloaded"):
                    self._after(0, self._books_fetched, [], "__SESSION_EXPIRED__")
                    return
                list_timed_out = False
                try:
                    await page.wait_for_selector('a[href*="chapter-manage/"]', timeout=10000)
                except PWTimeout:
                    list_timed_out = True
                books = await page.evaluate(BOOKS_JS)
                if not books and list_timed_out:
                    # 列表迟迟没渲染出来：是加载问题不是登录问题，别误导用户去重新登录
                    self._after(0, self._books_fetched, [], "__PAGE_TIMEOUT__")
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

    def _books_fetched(self, books, error):
        self._books_loading = False
        self.btn_books.configure(state="normal")
        if error == "__PAGE_TIMEOUT__":
            self._log("获取失败: 作品列表长时间未加载出来（多为系统繁忙或网络缓慢，"
                      "例如有其他自动化程序占用资源）。登录状态未必有问题，请稍后重试。")
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

    def _reload_chapters(self):
        """从磁盘重新扫描并解析章节文件。仅在目录变更/用户点刷新时调用。"""
        self._all_files = []
        self._all_parsed = []
        dir_path = self.dir_var.get()
        if not dir_path:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            return
        p = Path(dir_path)
        if not p.is_dir():
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            self._set_preview("这个文件夹不在了。点「选择文件夹」重新指定。")
            return

        try:
            self._all_files = get_md_files(p)
        except OSError as e:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            self._set_preview(f"无法读取目录: {e}")
            return
        if not self._all_files:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            self._set_preview("目录及子文件夹里没有 .md / .txt 文件。\n"
                              "把章节文件放进来后点 ↻ 重新扫描，或换一个文件夹。")
            return

        # 跳过扫描后变得无法读取的文件（云端离线占位/被删/无权限），并保持
        # _all_files 与 _all_parsed 一一对齐——否则解析在列表推导处抛 OSError，
        # 刷新整段中断且二者失配，后续按日期筛选会 IndexError。
        self._all_files, self._all_parsed = parse_md_files(self._all_files)
        if not self._all_files:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
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
                self.files, self.parsed_chapters, self._word_counts = [], [], []
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
                self.progress["maximum"] = 1
                self.progress["value"] = 0
                self.lbl_progress.configure(text="0/0")
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
                    _keep, autocont_start, _gaps, autocont_tail = self._autocont_plan(
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
        for i, (num, title, content) in enumerate(self.parsed_chapters):
            wc = self._word_counts[i] if i < len(self._word_counts) else len(strip_md_formatting(content))
            total_words += wc
            if i in kept_set:
                kept_words += wc
                num_str = f"第{num}章" if num else "  ?  "
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

        mode_labels = {"draft": "存草稿", "publish": "立即发布", "schedule": "定时发布",
                       "edit": "修改内容", "reschedule": "修改排期"}
        display_words = kept_words if filter_active else total_words
        count_str = (f"{kept_count}/{len(self.files)}" if filter_active
                     else str(len(self.files)))
        summary = f"总计: {count_str} 章, {display_words} 字 | 模式: {mode_labels[mode]}"
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

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = max(kept_count, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{kept_count}")

    def _refresh_edit_preview(self):
        """修改模式专用预览: 显示匹配状态。"""
        self.lbl_resched_filter_info.configure(text="", foreground=CLR_INK_SOFT)

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
                    num_str = f"第{num}章" if num else "  ?  "
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
                num_str = f"第{num}章" if num else "  ?  "
                lines.append(
                    f"  {i+1:3d}. {num_str} {title}  ({wc}字)")

        summary = f"总计: {len(self.files)} 章, {total_words} 字 | 模式: 修改内容"
        if platform_chapters:
            summary += f" | 匹配: {matched_count}/{len(self.files)}"
        else:
            # 状态说一次就够；此前每行都挂一个 [待获取章节列表]，几千行全是同一句
            summary += " | 正在获取平台章节列表，匹配结果稍后显示"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = max(matched_count, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{matched_count}")

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
            self.lbl_resched_filter_info.configure(text="", foreground=CLR_INK_SOFT)
            return items, False
        try:
            raw = unicodedata.normalize(
                "NFKC", self.resched_filter_num_var.get()).strip()
        except tk.TclError:
            return items, False
        if not raw:
            self.lbl_resched_filter_info.configure(text="", foreground=CLR_INK_SOFT)
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
            self.lbl_resched_filter_info.configure(
                text="请输入数字、范围或组合(如 1,3,5-10)", foreground="red")
            return items, False
        if not raw.isdigit():
            # 组合/范围合法后才禁用运算符下拉（原逻辑如此；非法输入时保持可用）
            self.cmb_resched_filter_op.configure(state="disabled")
        self.lbl_resched_filter_info.configure(
            text=f"筛选: {len(kept)}/{total} 章", foreground=CLR_INK_SOFT)
        return kept, active

    def _refresh_reschedule_preview(self):
        """修改排期模式预览: 显示平台章节 + 计算的新排期。"""
        self.lbl_resched_filter_info.configure(text="", foreground=CLR_INK_SOFT)

        idx = self.cmb_book.current()
        book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None

        def _early_return(text):
            self._set_preview(text)
            self.progress["maximum"] = 1
            self.progress["value"] = 0
            self.lbl_progress.configure(text="")

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
        summary = f"总计: {count} 章(待发布) | 模式: 修改排期"
        if schedule:
            first_day = schedule[0][0]
            eff = sum(1 for d, _ in schedule if d == first_day)
            summary += f" | 每天{eff}章 | 排期: {schedule[0][0]} ~ {schedule[-1][0]}"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = max(count, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{count}")

    # -----------------------------------------------------------------------
    # 上传
    # -----------------------------------------------------------------------
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
        if mode in ("edit", "reschedule"):
            # 修改/排期模式依赖平台章节列表是否已加载完成
            ck = self._chapter_cache_key(self.books[idx]["bookId"]) if has_book else None
            if not (ck and ck in self._platform_chapters_cache):
                issues.append("平台章节列表尚未加载完成（请等待加载或重选作品）")
            elif mode == "edit" and not self._matched_edit:
                issues.append("没有匹配到可修改的章节")
        elif not self.files:
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
        # 目标时刻就在上方输入框里，状态行只报倒计时——重复写一遍日期会把这行
        # 撑到装不下、末尾被裁
        prefix = "🔄 已刷新目录 ｜ " if self._timer_prerefresh_done else ""
        self.lbl_timer_status.configure(
            text=f"⏰ {prefix}倒计时 {self._fmt_hms(remaining)}",
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

    def _on_upload(self):
        if self.uploading:
            # 正在上传中 -> 请求取消
            self._cancel_requested = True
            self.btn_upload.configure(state="disabled", text="正在停止…")
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
            keep, autocont_start, gaps, autocont_tail = self._autocont_plan(
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
                    + "章…）——这些不能靠发布补回原位，请用底部的「章节重排」")

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
                logger.info(f"自动接续：平台队列排到 {tail_s}，"
                            f"从 {schedule[0][0]} {schedule[0][1]} 起接着排")
            else:
                schedule = compute_schedule(
                    len(parsed), date_str, time_str, per_day)

        # 确认
        count = len(parsed)
        mode_labels = {"draft": "存草稿", "publish": "立即发布", "schedule": "定时发布",
                       "edit": "修改内容", "reschedule": "修改排期"}
        count_str = (f"{count}/{total_before_filter} 章（已筛选）"
                     if filter_active else f"{count} 章")
        msg = f"即将上传 {count_str} 到「{book_name}」\n模式: {mode_labels[mode]}"
        if schedule:
            msg += f"\n排期: {schedule[0][0]} ~ {schedule[-1][0]}"
        if not self._ask_yes_no("确认上传", msg):
            return

        # 开始
        delay = self._begin_task(count, mode_labels[mode])

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
        #   修改/排期 —— 要平台章节列表，否则切筛选器时 _refresh_edit_preview
        #                拿不到数据会把 _matched_edit 置空；
        #   新建类   —— 要「队列排到哪天」。刚把队列往后推了一截，不重取的话
        #                标签和起始日期还停在推之前，下一批照它填就插队了。
        if self.mode_var.get() in ("edit", "reschedule"):
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
                shutil.copy2(str(AUTH_FILE), str(named))
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
                tail = "，续跑章节号见运行日志" if cancelled and failed else ""
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
        msg = (f"即将用本地文件替换「{book_name}」中 {count} 章的正文{rng}。\n"
               f"章节的发布时间不受影响。")
        if not self._ask_yes_no("确认修改", msg):
            return

        delay = self._begin_task(count, "修改内容")
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
                    skip_str = f"  跳过: {skipped}" if skipped else ""
                    logger.info(
                        f"  修改完成! 成功: {success}  失败: {failed}{skip_str}")
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
            per_day = max(1, self.perday_var.get())
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

        # 计算排期并构建 schedule_map
        schedule = compute_schedule(
            len(platform_chapters), date_str, time_str, per_day)
        schedule_map = {}
        dup_titles = []
        for i, ch in enumerate(platform_chapters):
            title = ch.get("title", "")
            if title in schedule_map:
                dup_titles.append(title)
            schedule_map[title] = schedule[i]
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

        count = len(platform_chapters)
        msg = (f"即将修改「{book_name}」{count} 个待发布章节的排期\n"
               f"排期: {schedule[0][0]} ~ {schedule[-1][0]}")
        if not self._ask_yes_no("确认修改排期", msg):
            return

        # 开始
        delay = self._begin_task(count, "修改排期")
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
                    )

                    await save_auth(context)
                    await close_browser_safely(browser)

                    # 汇总与完成通知放在 async with 内：浏览器挂死时
                    # playwright stop（with 退出）可能同样阻塞，不能让它
                    # 挡住结果汇报（实测曾让 GUI"卡死"41 分钟）
                    logger.info(f"{'='*40}")
                    logger.info(f"  修改排期完成! 成功: {success}  失败: {failed}")
                    logger.info(f"{'='*40}")

                    self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"修改排期异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    # -----------------------------------------------------------------------
    # 检查缺口：体检章节位置，按「谁能修」分段（缺章补不回原位，见下方说明）
    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # 工具菜单：核心逻辑一律复用 tools/ 下已有实现，这里只负责取参数/确认/展示
    # -----------------------------------------------------------------------
    @staticmethod
    def _load_tool(name):
        """加载 tools/<name>/<name>.py —— 直接用 fanqie_upload 的带缓存实现。

        原来这里和 fanqie_upload._load_tool_module 是逐字相同的两份，且都不
        缓存：每次自动接续上传都重新 exec 一遍 keep_ahead，模块顶层的
        sys.path.insert 也跟着累积。
        """
        return _load_tool_module(name)

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

    # --- 清空草稿箱 ---------------------------------------------------------
    def _on_autocont_toggle(self):
        """勾了自动接续就置灰起始日期（改由平台队列末尾决定）。

        勾上的同时就去取平台章节：这个功能本就要知道「队列排到哪天」，
        等到用户点了「开始上传」才发现没数据，就只能把人挡回去。
        """
        self._sync_autocont_state()
        if self.autocont_var.get():
            self._ensure_platform_chapters()
        self._refresh_preview()

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
        """自动接续: 返回 (要发的章号集合, 起始日期, 中段缺口)。

        复用 tools/keep_ahead 的纯函数 plan_refill —— 只取平台最大章号之后的
        章，中段缺口靠"发上去"补不回原位（新建只能追加到书尾），那是
        「章节重排」的活，这里只提醒不碰。
        """
        tool = self._load_tool("keep_ahead")
        # 缓存里的章节来自 _EXTRACT_ALL_JS（DOM 抓取），字段是
        # {title, chapterNum, editUrl, status, date, time, rowIndex} ——
        # **没有** display_status / timer_time。之前直接 .get(默认值) 等于把每章
        # 都当成「已发布、无定时」，queue_tail_date 于是永远找不到待发布章、
        # 队列末尾恒等于今天、起始日期恒为明天：队列已排到 9 月底时勾上自动接续，
        # 新章会全堆到明天起的已占用日期上（正是这个功能要防的事）。
        # 这里按 DOM 的中文状态与日期时间换算出这两个字段。
        items = []
        for i, c in enumerate(cached):
            status = c.get("status", "") or ""
            pending = "待发布" in status
            tt = 0
            d, t = c.get("date"), c.get("time")
            if pending and d:
                try:
                    tt = int(datetime.strptime(
                        f"{d} {t or '00:00'}", "%Y-%m-%d %H:%M").timestamp())
                except ValueError:
                    tt = 0
            items.append({
                "index": c.get("rowIndex", i) + 1,
                "pos": c.get("rowIndex", i) + 1,
                "title": c.get("title", ""),
                "display_status": MOVE_PENDING if pending else MOVE_PUBLISHED,
                "timer_time": tt,
            })
        num2path = {}
        for num, _t, _c in parsed:
            if num is None:
                continue
            try:
                num2path[int(num)] = True
            except (TypeError, ValueError):
                pass
        try:
            per_day = max(1, self.perday_var.get())
        except tk.TclError:
            per_day = 1
        raw = getattr(self, "days_ahead_var", None)
        raw = raw.get().strip() if raw is not None else ""
        days_ahead = int(raw) if raw.isdigit() and int(raw) > 0 else None
        nums, start, _days, _tail, gaps = tool.plan_refill(
            items, num2path, all_remaining=days_ahead is None,
            days_ahead=days_ahead or 0, per_day=per_day)
        return set(nums), start, gaps, tool.queue_tail_dt(items)

    def _autocont_schedule(self, tail_dt, n, time_str, per_day):
        """自动接续的排期：接着平台队尾的**时刻**续排——先填满队尾那天剩下的
        槽位再往后，不再整天跳到次日。算法在 keep_ahead.schedule_after（CLI 同款）。"""
        return self._load_tool("keep_ahead").schedule_after(
            tail_dt, n, time_str, per_day)

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

    # --- 章节重排 / 续排发布（复用 CLI，先预览再执行）-----------------------
    def _on_tool_remap(self):
        self._run_cli_tool(
            "remap", "章节重排",
            "把未公开的待发布章按位置重装内容（位置 i 装第 i 章），排期不变。"
            "已公开的一律不碰。")

    def _chapters_dir_for_tools(self):
        """取界面上当前的章节目录，不读 config 缓存。

        _cfg["chapters_dir"] 由 _schedule_config_save 延迟 1 秒写入：刚选完文件夹
        就点「章节重排」，子进程拿到的还是上一本书的目录——remap --run 会拿
        错书的文件改写待发布章节。归属校验只是抽样启发式，拦不住所有情况。
        """
        try:
            d = (self.dir_var.get() or "").strip()
        except Exception:
            d = ""
        fallback = (self._cfg.get("chapters_dir") or "").strip()
        return d or fallback or str(SCRIPT_DIR / "chapters")

    def _run_cli_tool(self, name, title, desc):
        """remap / keep_ahead 共用：先跑 dry-run 预览，确认后再 --run。

        直接调 CLI 而不是 import: 这两个工具是长任务、自带浏览器会话，
        独立进程跑不会和 GUI 的事件循环/浏览器抢资源，输出也天然可展示。
        """
        book_id = self._tool_precheck()
        if not book_id:
            return
        script = SCRIPT_DIR / "tools" / name / (name + ".py")
        base = [sys.executable, str(script), "--book-id", book_id,
                "--content-dir", self._chapters_dir_for_tools()]
        if self.headless_var.get():
            base.append("--headless")
        # remap 重新提交正文走的是和上传/修改同一条发布流程，
        # AI 申报漏传就成了界面上看不见的合规不一致。
        if self.use_ai_var.get():
            base.append("--use-ai")

        def run_cli(extra, on_done):
            def work():
                try:
                    env = dict(os.environ, PYTHONIOENCODING="utf-8")
                    r = subprocess.run(
                        base + extra, capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                        cwd=str(SCRIPT_DIR), env=env, timeout=6 * 3600)
                    out = (r.stdout or "") + (r.stderr or "")
                except Exception as e:
                    out = "启动失败: " + str(e)
                self._after(0, on_done, out)

            # 重排/工具走子进程，读不到 _cancel_requested；预览结果在预览页
            self._set_uploading(True, cancellable=False, show_log=False)
            threading.Thread(target=work, daemon=True).start()

        def after_preview(out):
            self._set_uploading(False)
            self._set_preview(out.strip() or "(无输出)")
            if ("将改写 0 个" in out or "无需补排" in out
                    or "没有可排的章" in out or "没有需要改写" in out):
                self._notify("info", title, "没有需要处理的章节，详见预览面板。")
                return
            tail = "\n".join(out.strip().splitlines()[-12:])
            if not self._ask_yes_no(
                    title, "%s\n\n预览结果：\n%s\n\n确认执行吗？" % (desc, tail)):
                return
            run_cli(["--run"], after_run)

        def after_run(out):
            self._set_uploading(False)
            self._set_preview(out.strip() or "(无输出)")
            self._notify("info", title + "完成", "详见预览面板。")

        self._set_preview("正在预览「%s」…" % title)
        run_cli([], after_preview)

    def _on_audit_gaps(self):
        """缺口体检：抓平台真实状态，按「谁能修」分三段报告。

        A 未公开段位置与章号不符 → 跑 tools/remap/remap.py --run 自动改写。
        B 已公开、发布 3 天内、顺序倒挂 → 只能手机 App 申请+审批+逐章移动，
          所以要在窗口内尽早知道，并显示每章还剩几小时。
        C 已公开、超 3 天 → 永久错位，只登记不吵。
        纯只读，不改平台任何东西。
        """
        if self.uploading or self._login_in_progress:
            self._notify("warning", "任务进行中",
                         "先等当前任务结束再检查缺口。\n"
                         "上传/修改可以点「停止」中断；工具与体检不支持中途取消。")
            return
        if not self._require_login():
            return
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            self._notify("warning", "未选择作品", PICK_BOOK_MSG)
            return
        book_id = self.books[idx]["bookId"]
        hl = self.headless_var.get()
        # 走统一的 self.worker（全 GUI 单一事件循环）并置忙标志：另起线程 +
        # asyncio.run 会开出第二个循环，体检和上传能同时各开一个浏览器、
        # 并发读写 AUTH_FILE；定时器判"有没有任务在跑"看的也是 self.uploading。
        # 体检结果写在「章节预览」页，且不支持取消
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
                logger.error(f"检查缺口失败: {e}")
                self._after(0, self._audit_done, None, None, str(e))
                return
            self._after(0, self._audit_done, items, volumes, None)

        self.worker.submit(task())

    def _audit_done(self, items, volumes, err):
        """体检收尾（主线程）：无论成败都要解除忙标志，否则界面永久卡在运行态。"""
        self._set_uploading(False)
        if err:
            self._notify("error", "检查缺口失败", err)
            return
        self._render_audit(items, volumes)

    def _render_audit(self, items, volumes=None):
        """把体检结果渲染到预览面板（主线程）。"""
        rep = audit_chapter_positions(items)
        pend_bad = rep["pending_bad"]
        lines = ["缺口体检", "=" * 60]
        if volume_count(volumes or {}) > 1:
            # 已跨卷合并；位置按全书连续计（卷序号*10000+卷内位置 → 累加偏移）
            lines.append(f"本作品 {volume_count(volumes)} 卷，已跨卷合并，"
                         f"位置按全书连续计。")
            lines.append("")
        lines.append(f"A 未公开段位置与章号不符：{len(pend_bad)} 个")
        if pend_bad:
            lines.append(f"   位置: {self._compress_nums(pend_bad)}"
                         f"（全书位置，多卷已折算；不是章号）")
            lines.append("   → 跑 python tools/remap/remap.py --run 自动改写"
                         "（只动未公开章，排期不变）")
            first = min(pend_bad)
            # pend_bad 存的是全书位置（pos）；多卷下原始 index 带卷偏移，
            # 拿 index 查会查空，缓冲告警就静默消失了
            tt = next((int(x.get("timer_time") or 0) for x in items
                       if x.get("pos", x["index"]) == first), 0)
            if tt:
                left_h = (tt - datetime.now().timestamp()) / 3600
                warn = "⚠ " if left_h < MOVE_WINDOW_H else ""
                lines.append(
                    f"   {warn}缓冲：位置 {first} 将于 "
                    f"{datetime.fromtimestamp(tt):%Y-%m-%d %H:%M} 发出，"
                    f"还剩 {left_h:.1f} 小时（{left_h / 24:.1f} 天）")
                if left_h < MOVE_WINDOW_H:
                    lines.append("   ⚠ 缓冲已跌破 3 天移动窗口：今天必须补跑，"
                                 "否则错位章发出后只能去 App 申请移动")
        else:
            lines.append("   → 未公开段全部就位")
        lines.append("")
        lines.append(f"B 已公开、还能在 App 申请移动：{len(rep['in_window'])} 章")
        for r in rep["in_window"]:
            lines.append(f"   第{r['num']}章（现在位置 {r['index']}）"
                         f"发布于 {datetime.fromtimestamp(r['pub_at']):%m-%d %H:%M}，"
                         f"窗口还剩 {r['left_h']:.1f} 小时")
        if rep["in_window"]:
            lines.append("   → 手机 App：申请调整 → 通过后选中该章 → 移动到正确位置")
        lines.append("")
        lines.append(f"C 已公开、超 3 天永久错位：{len(rep['expired'])} 章")
        if rep["expired"]:
            lines.append("   " + "、".join(
                f"第{r['num']}章(位置{r['index']})" for r in rep["expired"]))
        self._set_preview("\n".join(lines))
        if rep["in_window"]:
            self._notify(
                "warning", "有章节还能救",
                f"{len(rep['in_window'])} 章已公开但顺序错位，还在 3 天移动窗口内。"
                f"详见预览面板，尽快去手机 App 申请移动。")
        else:
            self._notify(
                "info", "体检完成",
                f"未公开段待重排 {len(pend_bad)} 个；已公开段没有还能救的错位章。")

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
