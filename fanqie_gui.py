#!/usr/bin/env python3
"""
番茄作家 MD/TXT 批量上传工具 - GUI 界面

使用方法:
    python fanqie_gui.py
"""

import asyncio
import json
import logging
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
        parse_md_file, parse_md_files, get_md_files, strip_md_formatting,
        deduplicate_titles, compute_schedule, _validate_times,
        DailyLimitReached, _check_daily_limit, _wait_publish_result,
        _log_fail_list,
        create_context, save_auth, close_browser_safely,
        wait_for_editor_ready, fill_chapter,
        save_draft, publish_scheduled, _navigate_to_publish_settings,
        extract_chapters_from_page, match_chapters, edit_one_chapter,
        reschedule_on_manage_page, detect_volumes, select_volume,
        AUTH_FILE, BASE_URL, BOOK_MANAGE_URL, NEW_CHAPTER_URL_TPL,
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
CHAPTER_MANAGE_URL = CHAPTER_MANAGE_URL_TPL
DEFAULT_CHAPTERS_DIR = SCRIPT_DIR / "chapters"

# --- 新手引导 ---
GH_URL = "https://github.com/rockbenben/fanqie-publisher"
ONBOARD_STEP_NAMES = ["登录", "选作品", "选章节文件夹", "上传/修改"]
_CIRCLED = ["①", "②", "③", "④"]
STEP_HINTS = {
    1: "下一步：点「登录/新建」，在弹出的浏览器里登录番茄账号",
    2: "下一步：点「刷新作品列表」并选择一部作品",
    3: "下一步：选择章节文件夹（默认 chapters/），确认下方「章节预览」里出现章节",
    4: "下一步：选好「操作模式」后点「开始上传」（修改模式为「开始修改」）",
}
SECTION_HELP = {
    "账号": "给每个番茄账号起一个本地名称用于区分。点「登录/新建」会打开浏览器，"
            "请在其中登录番茄账号——本地名称只是标签，不是番茄笔名。"
            "登录多个账号后用下拉框切换。",
    "作品选择": "登录后点「刷新作品列表」拉取你的作品，选中一部后会自动获取最新发布信息。"
                "「章节管理 ↗」可在浏览器里打开该作品的章节管理页。",
    "章节文件夹": "选择存放章节的文件夹（含子文件夹）。每个 .md 或 .txt（纯文本）"
                  "文件就是一章，按文件名顺序排列（chapter-1 在 chapter-10 之前）。"
                  "「自动处理重名」避免番茄的同名章节限制；可按修改日期只挑近期改过的章节。",
    "操作模式": "五种操作，按需选一：\n"
                "· 定时发布：排好日期时间，到点自动发布\n"
                "· 立即发布：上传后马上发布\n"
                "· 存草稿：只上传存草稿，稍后自己手动发\n"
                "· 修改内容：用本地文件替换已发布章节的正文\n"
                "· 修改排期：只改已有章节的发布时间，不动正文\n"
                "所有模式都能「按章节号筛选」，只操作指定范围（如 1,3,5-10）的章节。",
    "定时执行": "设定一个未来时刻，到点自动执行当前所选操作（仅一次），"
                "适合无人值守（比如半夜自动上传）。到点若有任务在跑会等它结束再执行，"
                "触发前会重新读取一次章节目录。",
}

def _as_int(n):
    """把章节号安全转 int；None/非数字返回 None（视为不匹配，不抛异常）。

    筛选的 key 可能来自平台章节(理论上 int)或本地解析(数字字符串)；万一
    DOM 漂移给出 "番外" 等非数字，旧代码 int(n) 会让整个预览/筛选崩掉。
    """
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


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
        self.root.title("番茄作家 MD/TXT 批量上传工具")
        # 高度含顶部新手引导栏（~96px）：1010 让两个文本面板在 1080p 上仍约 8 行；
        # 小屏可拖动，或「✕ 收起」引导栏拿回空间（minsize 保持不变以兼容小屏）
        _w, _h = 1100, 1010
        self.root.update_idletasks()
        _sx = (self.root.winfo_screenwidth() - _w) // 2
        _sy = (self.root.winfo_screenheight() - _h) // 2
        self.root.geometry(f"{_w}x{_h}+{_sx}+{_sy}")
        self.root.minsize(960, 820)

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

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # UI 构建
    # -----------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- 0. 新手引导栏 ---
        self._build_guidance_bar()

        # --- 1. 账号 ---
        frm = ttk.LabelFrame(self.root, text="账号")
        self._acct_frame = frm
        frm.pack(fill="x", **pad)
        self.account_var = tk.StringVar()
        self.cmb_account = ttk.Combobox(
            frm, textvariable=self.account_var, state="readonly", width=16)
        self.cmb_account.pack(side="left", padx=6, pady=4)
        self.cmb_account.bind("<<ComboboxSelected>>", self._on_account_selected)
        self.btn_login = ttk.Button(frm, text="登录/新建", command=self._on_login)
        self.btn_login.pack(side="left", padx=6, pady=4)
        self.lbl_auth = ttk.Label(frm, text="")
        self.lbl_auth.pack(side="left", padx=6)
        lbl_gh = ttk.Label(frm, text="GitHub", foreground="royalblue",
                           cursor="hand2", font=("", 9, "underline"))
        lbl_gh.pack(side="right", padx=8)
        lbl_gh.bind("<Button-1>", lambda _: webbrowser.open(GH_URL))
        ttk.Button(frm, text="❓帮助", command=self._show_welcome).pack(
            side="right", padx=2)
        self._add_help(frm, "账号", side="left")
        self._refresh_account_list()
        self._refresh_auth_status()

        # --- 2. 作品选择 ---
        frm = ttk.LabelFrame(self.root, text="作品选择")
        frm.pack(fill="x", **pad)
        self.btn_books = ttk.Button(
            frm, text="刷新作品列表", command=self._on_refresh_books)
        self.btn_books.pack(side="left", padx=6, pady=4)
        self.book_var = tk.StringVar()
        self.cmb_book = ttk.Combobox(
            frm, textvariable=self.book_var, state="readonly", width=48)
        self.cmb_book.pack(side="left", padx=6, pady=4, fill="x", expand=True)
        self.cmb_book.bind("<<ComboboxSelected>>", lambda _: self._on_book_changed())
        self.btn_open_manage = ttk.Button(
            frm, text="章节管理 ↗", command=self._open_chapter_manage)
        self.btn_open_manage.pack(side="left", padx=(0, 6), pady=4)
        self._add_help(frm, "作品选择")

        # --- 3. 章节文件夹 ---
        frm_dir = ttk.LabelFrame(self.root, text="章节文件夹")
        frm_dir.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_dir)
        row1.pack(fill="x")
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
        ttk.Entry(row1, textvariable=self.dir_var, state="readonly").pack(
            side="left", padx=6, pady=4, fill="x", expand=True)
        ttk.Button(row1, text="浏览...", command=self._on_browse_dir).pack(
            side="left", pady=4)
        ttk.Button(row1, text="刷新", command=self._reload_chapters).pack(
            side="left", padx=(4, 6), pady=4)
        self.unique_var = tk.BooleanVar(value=bool(self._cfg.get("auto_unique", True)))
        ttk.Checkbutton(
            row1, text="自动处理重名", variable=self.unique_var,
            command=self._apply_date_filter).pack(side="left", padx=6)
        self.unique_var.trace_add("write", lambda *_: self._schedule_config_save())
        self.filter_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row1, text="按修改日期筛选",
            variable=self.filter_var,
            command=self._on_filter_toggle).pack(side="left", padx=6)
        self._add_help(row1, "章节文件夹")

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
        ttk.Label(row_filter, text="格式: YYYY-MM-DD 或 YYYY-MM-DD HH:MM",
                  foreground="gray").pack(side="left", padx=2)
        self.lbl_filter_info = ttk.Label(row_filter, text="", foreground="gray")
        self.lbl_filter_info.pack(side="left", padx=6)
        self._filter_row = row_filter

        # --- 4. 操作模式 ---
        frm_mode = ttk.LabelFrame(self.root, text="操作模式")
        frm_mode.pack(fill="x", **pad)

        row_radios = ttk.Frame(frm_mode)
        row_radios.pack(fill="x", padx=6, pady=4)
        _mode = self._cfg.get("default_mode", "schedule")
        if _mode not in ("schedule", "publish", "draft", "edit", "reschedule"):
            _mode = "schedule"  # 损坏/手改的 config 不应让单选组空选、模式分发错乱
        self.mode_var = tk.StringVar(value=_mode)
        self._mode_radios: list[ttk.Radiobutton] = []
        for text, val in [("定时发布", "schedule"), ("立即发布", "publish"),
                          ("存草稿", "draft"), ("修改内容", "edit"),
                          ("修改排期", "reschedule")]:
            rb = ttk.Radiobutton(
                row_radios, text=text, variable=self.mode_var,
                value=val, command=self._on_mode_change)
            rb.pack(side="left", padx=10)
            self._mode_radios.append(rb)
        self._add_help(row_radios, "操作模式")

        # 发布选项（所有非草稿模式可见）
        row_opts = ttk.Frame(frm_mode)
        row_opts.pack(fill="x", padx=6, pady=(0, 4))
        self.use_ai_var = tk.BooleanVar(value=bool(self._cfg.get("use_ai", False)))
        self.chk_use_ai = ttk.Checkbutton(
            row_opts, text="稿件使用了AI创作", variable=self.use_ai_var)
        self.chk_use_ai.pack(side="left", padx=6)
        self.use_ai_var.trace_add("write", lambda *_: self._schedule_config_save())

        # 上次发布信息（所有模式可见）
        self.lbl_last_publish = ttk.Label(
            frm_mode, text="选择作品后自动获取", foreground="gray")
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

        # Row 2: 时间（支持多个时间，逗号分隔）
        r2 = ttk.Frame(self.sched_frame)
        r2.pack(fill="x", padx=6, pady=2)
        ttk.Label(r2, text="发布时间:").pack(side="left")
        self.time_var = tk.StringVar(value=self._cfg.get("default_time", "08:00"))
        ttk.Entry(r2, textvariable=self.time_var, width=24).pack(
            side="left", padx=4)
        ttk.Label(r2, text="多时间逗号分隔，如 07:00,12:00,20:00",
                  foreground="gray").pack(side="left")

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
            self._resched_filter_row, text="", foreground="gray")
        self.lbl_resched_filter_info.pack(side="left", padx=6)

        # --- 4.5 定时执行 ---
        frm_timer = ttk.LabelFrame(self.root, text="定时执行（到点自动执行当前所选操作，单次）")
        frm_timer.pack(fill="x", **pad)
        row_timer = ttk.Frame(frm_timer)
        row_timer.pack(fill="x", padx=6, pady=4)
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
            row_timer, textvariable=self.timer_time_var, width=18)
        self.ent_timer.pack(side="left", padx=4)
        ttk.Label(row_timer, text="格式: YYYY-MM-DD HH:MM",
                  foreground="gray").pack(side="left", padx=2)
        self.btn_timer = ttk.Button(
            row_timer, text="启动定时", command=self._toggle_timer)
        self.btn_timer.pack(side="left", padx=8)
        self.lbl_timer_status = ttk.Label(
            row_timer, text="定时未启动", foreground="gray")
        self.lbl_timer_status.pack(side="left", padx=6)
        self._add_help(row_timer, "定时执行")

        # --- 5. 章节预览 ---
        frm = ttk.LabelFrame(self.root, text="章节预览")
        frm.pack(fill="both", expand=True, **pad)
        self.txt_preview = scrolledtext.ScrolledText(
            frm, height=8, state="disabled", wrap="none",
            font=("Consolas", 9))
        self.txt_preview.pack(fill="both", expand=True, padx=4, pady=4)

        # --- 6. 上传控制 ---
        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)
        self.btn_upload = ttk.Button(
            frm, text="开始上传", command=self._on_upload)
        self.btn_upload.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(frm, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=6)
        self.lbl_progress = ttk.Label(frm, text="")
        self.lbl_progress.pack(side="left", padx=6)

        # --- 7. 运行日志 ---
        frm = ttk.LabelFrame(self.root, text="运行日志")
        frm.pack(fill="both", expand=True, **pad)
        log_bar = ttk.Frame(frm)
        log_bar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(log_bar, text="导出日志", command=self._export_log).pack(
            side="right")
        self.txt_log = scrolledtext.ScrolledText(
            frm, height=8, state="disabled",
            font=("Consolas", 9))
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

        # 所有组件创建完毕，统一设置初始模式的面板可见性
        self._on_mode_change()

        # 启动时自动加载预览
        if self.dir_var.get():
            self.root.after(100, self._reload_chapters)

    def _build_guidance_bar(self):
        """顶部新手引导栏：步骤指示 + 下一步提示，可收起且记住。"""
        self._guide_frame = ttk.LabelFrame(self.root, text="新手引导")
        top = ttk.Frame(self._guide_frame)
        top.pack(fill="x", padx=8, pady=(4, 0))
        self._step_labels = []
        for i, name in enumerate(ONBOARD_STEP_NAMES):
            lbl = ttk.Label(top, text=f"{_CIRCLED[i]} {name}", font=("", 10))
            lbl.pack(side="left")
            self._step_labels.append(lbl)
            if i < len(ONBOARD_STEP_NAMES) - 1:
                ttk.Label(top, text="──▸", foreground="gray").pack(
                    side="left", padx=6)
        ttk.Button(top, text="✕ 收起",
                   command=lambda: self._set_guidance_collapsed(True)).pack(
                       side="right")
        self.lbl_next_step = ttk.Label(
            self._guide_frame, text="", foreground="#1565c0", font=("", 9))
        self.lbl_next_step.pack(anchor="w", padx=10, pady=(2, 6))

        # 收起后的细长再入口
        self._guide_stub = ttk.Frame(self.root)
        link = ttk.Label(self._guide_stub, text="▸ 新手引导",
                         foreground="royalblue", cursor="hand2",
                         font=("", 9, "underline"))
        link.pack(side="left", padx=4)
        link.bind("<Button-1>", lambda _: self._set_guidance_collapsed(False))

        # 按记忆决定初始展开/收起（首次默认展开）
        if self._gui_state.get("guidance_collapsed"):
            self._guide_stub.pack(fill="x", padx=8, pady=(6, 0))
        else:
            self._guide_frame.pack(fill="x", padx=8, pady=(6, 0))

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
                    text=f"当前: {acct}", foreground="green")
            else:
                self.lbl_auth.configure(
                    text="已登录", foreground="green")
        else:
            # 新用户第一步就是登录——把空标签变成明确的行动指引
            self.lbl_auth.configure(
                text="未登录 ← 请先点「登录/新建」", foreground="red")
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
        for i, lbl in enumerate(self._step_labels, start=1):
            name = ONBOARD_STEP_NAMES[i - 1]
            if i < step:                       # 已完成
                lbl.configure(text=f"✓ {name}", foreground="#2e7d32",
                              font=("", 10))
            elif i == step:                    # 当前
                lbl.configure(text=f"{_CIRCLED[i-1]} {name}",
                              foreground="#1565c0", font=("", 10, "bold"))
            else:                              # 未到
                lbl.configure(text=f"{_CIRCLED[i-1]} {name}",
                              foreground="gray", font=("", 10))
        self.lbl_next_step.configure(text=STEP_HINTS.get(step, ""))

    def _set_guidance_collapsed(self, collapsed: bool):
        """收起/展开引导栏，并记住选择。"""
        self._gui_state["guidance_collapsed"] = collapsed
        self._save_gui_state()
        if collapsed:
            self._guide_frame.pack_forget()
            self._guide_stub.pack(fill="x", padx=8, pady=(6, 0),
                                  before=self._acct_frame)
        else:
            self._guide_stub.pack_forget()
            self._guide_frame.pack(fill="x", padx=8, pady=(6, 0),
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
        ttk.Label(frm, text=key, font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(frm, text=SECTION_HELP.get(key, ""), wraplength=380,
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
        lbl = ttk.Label(parent, text="(?)", foreground="royalblue",
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
        ttk.Label(body, text="欢迎使用番茄作家批量上传工具 👋",
                  font=("", 13, "bold")).pack(anchor="w", pady=(0, 8))
        steps = [
            "① 登录：点「登录/新建」，在弹出的浏览器里登录番茄账号。",
            "② 选作品：点「刷新作品列表」并选择一部作品。",
            "③ 选章节文件夹：默认 chapters/，每个 .md 或 .txt（纯文本）文件是一章。",
            "④ 上传/修改：选好「操作模式」后点「开始上传」。",
        ]
        for s in steps:
            ttk.Label(body, text=s, font=("", 10)).pack(anchor="w", pady=1)
        ttk.Label(
            body,
            text="提示：登录框里填的是「本地账号名称」（用于区分多个账号），"
                 "不是番茄笔名；真正的登录在浏览器里完成。",
            font=("", 9), foreground="#856404", wraplength=420,
            justify="left").pack(anchor="w", pady=(10, 12))
        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x")
        lbl_doc = ttk.Label(btn_row, text="查看完整说明 (GitHub)",
                            foreground="royalblue", cursor="hand2",
                            font=("", 9, "underline"))
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
        src = SCRIPT_DIR / f".auth_{name}.json"
        if not src.exists():
            messagebox.showerror("错误", f"账号文件不存在: {src.name}")
            return
        try:
            shutil.copy2(str(src), str(AUTH_FILE))
        except Exception as e:
            messagebox.showerror("错误", f"切换账号失败: {e}")
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

        # --- 2. 按模式显示组件（注意 pack 顺序决定布局顺序） ---
        #   lbl_last_publish:   all modes
        #   _volume_frame:      edit, reschedule (仅多卷时；勾选"合并所有卷"时隐藏分卷下拉)
        #   sched_frame:        schedule, reschedule
        #   _resched_filter_row: all modes
        #   chk_use_ai:         schedule, publish, edit
        has_vols = bool(self.cmb_volume["values"])
        self.lbl_last_publish.pack(fill="x", padx=12, pady=(0, 4))
        if mode in ("edit", "reschedule") and has_vols:
            # 勾选"合并所有卷"时隐藏分卷下拉，只保留复选框
            if self.all_volumes_var.get():
                self._lbl_volume_sep.pack_forget()
                self.cmb_volume.pack_forget()
            else:
                self._lbl_volume_sep.pack(side="left", padx=(12, 0))
                self.cmb_volume.pack(side="left", padx=2, pady=4)
            self._volume_frame.pack(fill="x", padx=6, pady=(0, 4))
        if mode in ("schedule", "reschedule"):
            self.sched_frame.pack(fill="x", padx=6, pady=4)
        self._resched_filter_row.pack(fill="x", padx=6, pady=(0, 4))
        if mode in ("schedule", "publish", "edit"):
            self.chk_use_ai.pack(side="left", padx=6)

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
            messagebox.showinfo("提示", "暂无日志内容")
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
        validated = _validate_times(self.time_var.get())
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
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    def _save_gui_state(self):
        try:
            self._atomic_write_json(GUI_STATE_FILE, self._gui_state)
        except Exception as e:
            logger.debug(f"保存 .gui_state.json 失败: {e}")

    def _set_uploading(self, active):
        self.uploading = active
        self._cancel_requested = False
        if active:
            self.btn_upload.configure(state="normal", text="停止")
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
        for rb in self._mode_radios:
            rb.configure(state=ctrl_state)

    # -----------------------------------------------------------------------
    # 快捷链接: 打开章节管理
    # -----------------------------------------------------------------------
    def _open_chapter_manage(self):
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            messagebox.showwarning("提示", "请先选择作品")
            return
        book_id = self.books[idx]["bookId"]
        url = CHAPTER_MANAGE_URL.format(book_id=book_id)
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
            # 后台获取（仅默认页，一般最新发布在首页即可看到）
            self.lbl_last_publish.configure(
                text="正在获取发布信息...", foreground="gray")

            gen = self._fetch_gen
            volumes_known = book_id in self._volumes_cache

            async def task():
                page = None
                try:
                    ctx = await self._shared.ensure()
                    page = await ctx.new_page()
                    if self._fetch_gen != gen:
                        return
                    url = CHAPTER_MANAGE_URL.format(book_id=book_id)
                    await page.goto(url)
                    await page.wait_for_load_state("networkidle")
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

        self._refresh_preview()

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
                text="暂无发布记录", foreground="gray")

    def _apply_last_publish(self, info):
        """将上次发布信息显示到 UI 并自动建议下一天起始日期。

        只更新 date_var，不覆盖 time_var —— 时间是用户配置项，
        平台单条发布记录不应覆盖用户设定的多时间方案。
        """
        date_str = info.get("date")
        time_str = info.get("time")
        if not date_str or not time_str:
            # 平台记录不完整（DOM 变动 / 部分抓取），不要让 KeyError
            # 冒泡进 after() 回调把标签卡在"正在获取..."。
            self.lbl_last_publish.configure(text="暂无发布记录", foreground="gray")
            return
        chapter = info.get("chapter", "")
        label = f"上次发布: {date_str} {time_str}"
        if chapter:
            label += f" ({chapter})"
        self.lbl_last_publish.configure(text=label, foreground="#d35400")

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
            if self.all_volumes_var.get():
                self._lbl_volume_sep.pack_forget()
                self.cmb_volume.pack_forget()
            else:
                self._lbl_volume_sep.pack(side="left", padx=(12, 0))
                self.cmb_volume.pack(side="left", padx=2, pady=4)
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
                text="请先登录后再获取章节列表", foreground="#d35400")
            if not self.uploading:
                self.btn_upload.configure(state="normal")
            return

        self.lbl_last_publish.configure(
            text="正在获取章节列表...", foreground="gray")

        gen = self._fetch_gen
        selected_vol = self._get_selected_volume()
        volumes_known = book_id in self._volumes_cache
        known_vols = self._volumes_cache.get(book_id)  # 主线程快照，供工作线程使用
        fetch_all_vols = self.all_volumes_var.get()

        async def task():
            page = None
            try:
                ctx = await self._shared.ensure()
                page = await ctx.new_page()
                if self._fetch_gen != gen:
                    return
                url = CHAPTER_MANAGE_URL.format(book_id=book_id)
                await page.goto(url)
                await page.wait_for_load_state("networkidle")

                # 仅首次检测卷
                if not volumes_known:
                    vol_info = await detect_volumes(page)
                    if self._fetch_gen != gen:
                        return
                    self._after(0, self._volumes_detected, book_id, vol_info)
                    has_vols = vol_info.get("hasVolumes")
                    vol_list = vol_info.get("volumes", [])
                else:
                    has_vols = bool(self._volumes_cache.get(book_id))
                    vol_list = self._volumes_cache.get(book_id) or []

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
                            text=m, foreground="gray"))
                        await select_volume(page, vol_name)
                        chs, lp = await extract_chapters_from_page(page, book_id)
                        all_chapters.extend(chs)
                        if lp and not last_pub:
                            last_pub = lp
                    chapters = all_chapters
                else:
                    # 单卷模式: 切换到指定卷
                    if selected_vol and has_vols:
                        await select_volume(page, selected_vol)
                    chapters, last_pub = await extract_chapters_from_page(
                        page, book_id)

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

    def _on_platform_chapters_fetched(self, book_id, chapters, error,
                                      last_pub=None, gen=None):
        # gen 过期 = 期间切了作品或卷（_fetch_gen 已自增）。必须在这里(应用时)
        # 再判一次：仅判 book_id 不够——切卷时 book_id 不变，但缓存键
        # _chapter_cache_key 读实时 volume_var，会把旧卷章节写到新卷键下
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
            text=f"已索引 {len(chapters)} 个章节", foreground="#d35400")
        # 载入完成，恢复上传按钮
        if not self.uploading and self.mode_var.get() in ("edit", "reschedule"):
            self.btn_upload.configure(state="normal")
        self._refresh_preview()

    # -----------------------------------------------------------------------
    # 登录
    # -----------------------------------------------------------------------
    def _on_login(self):
        # 防止并发登录
        if self._login_in_progress:
            messagebox.showinfo("提示", "登录正在进行中，请先完成或取消当前登录。")
            return
        # 弹出对话框要求输入账号名称，默认填充当前账号
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
            return
        name = self._sanitize_account_name(raw)
        if not name:
            messagebox.showerror("名称无效",
                                 "账号名称包含非法字符或为保留名，请重新输入。")
            return

        # L2: 已存在同名账号时提示确认
        named_path = SCRIPT_DIR / f".auth_{name}.json"
        if named_path.exists():
            if not messagebox.askyesno(
                    "账号已存在",
                    f"账号「{name}」已存在。\n继续将覆盖其登录状态，是否继续？"):
                return

        self._pending_account_name = name
        self._login_event = threading.Event()
        self._login_cancelled = False
        self._login_in_progress = True
        self.btn_login.configure(state="disabled")

        async def task():
            try:
                async with async_playwright() as p:
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

                    await save_auth(context)

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

        # 创建醒目的浮动窗口（非模态），放在屏幕右下角
        win = tk.Toplevel(self.root)
        win.title("等待登录")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(bg="#FFF3CD")  # 醒目的暖黄色背景

        body = tk.Frame(win, bg="#FFF3CD")
        body.pack(padx=16, pady=12)

        tk.Label(
            body, text="⏳ 请在浏览器中登录",
            font=("", 12, "bold"), bg="#FFF3CD", fg="#856404",
        ).pack(pady=(0, 6))
        tk.Label(
            body, text="在浏览器里登录番茄账号（没有账号请先注册并开通作家），\n"
                       "完成后点下方按钮保存会话",
            font=("", 9), bg="#FFF3CD", fg="#856404", justify="left",
        ).pack(pady=(0, 10))

        btn_frame = tk.Frame(body, bg="#FFF3CD")
        btn_frame.pack()

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

        tk.Button(
            btn_frame, text="✔ 登录完成，保存会话",
            font=("", 10, "bold"), fg="white", bg="#28A745",
            activebackground="#218838", activeforeground="white",
            padx=12, pady=4, cursor="hand2",
            command=on_confirm,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btn_frame, text="取消",
            font=("", 10), fg="#856404", bg="#FFEEBA",
            activebackground="#FFE083", padx=12, pady=4,
            cursor="hand2", command=on_cancel,
        ).pack(side="left")

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
                messagebox.showerror("登录失败", f"登录未能完成：\n\n{error}")
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
        if not AUTH_FILE.exists():
            messagebox.showwarning("提示", "请先登录")
            return
        # 防重入：多个入口可能近乎同时调度刷新（切账号 + 登录完成），
        # 避免并发任务竞争 self.books / 共享浏览器上下文
        if getattr(self, "_books_loading", False):
            return
        self._books_loading = True
        self.btn_books.configure(state="disabled")
        self._log("正在获取作品列表...")

        async def task():
            page = None
            try:
                ctx = await self._shared.ensure()
                page = await ctx.new_page()
                # domcontentloaded 比 networkidle 可靠: 平台埋点/轮询会拖死 networkidle
                # 致使 goto 超时被误判为会话失效。
                try:
                    await page.goto(BOOK_MANAGE_URL, wait_until="domcontentloaded")
                except PWTimeout:
                    pass  # 即便 goto 超时, 后续仍可能拿到作品
                # 仅在被重定向到登录页时直接判失效
                if "/login" in page.url:
                    self._after(0, self._books_fetched, [], "__SESSION_EXPIRED__")
                    return
                try:
                    await page.wait_for_selector('a[href*="chapter-manage/"]', timeout=10000)
                except PWTimeout:
                    pass
                books = await page.evaluate(BOOKS_JS)
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
            self._log("未找到作品，请检查登录状态。")
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
            self._set_preview("目录不存在")
            return

        try:
            self._all_files = get_md_files(p)
        except OSError as e:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            self._set_preview(f"无法读取目录: {e}")
            return
        if not self._all_files:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            self._set_preview("目录及子文件夹中没有 .md/.txt 文件")
            return

        # 跳过扫描后变得无法读取的文件（云端离线占位/被删/无权限），并保持
        # _all_files 与 _all_parsed 一一对齐——否则解析在列表推导处抛 OSError，
        # 刷新整段中断且二者失配，后续按日期筛选会 IndexError。
        self._all_files, self._all_parsed = parse_md_files(self._all_files)
        if not self._all_files:
            self.files, self.parsed_chapters, self._word_counts = [], [], []
            self._set_preview("目录中的文件均无法读取（可能是云端离线文件或权限不足）")
            return
        self._apply_date_filter()

    def _apply_date_filter(self):
        """应用日期筛选 + 去重 + 刷新预览（复用已缓存的解析结果，不重新读取文件）。"""
        if not self._all_files:
            return

        if self.filter_var.get():
            raw = self.filter_date_var.get().strip()
            cutoff = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    cutoff = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if cutoff is None:
                self.lbl_filter_info.configure(
                    text="格式错误，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM",
                    foreground="red")
                self.files, self.parsed_chapters, self._word_counts = [], [], []
                self._set_preview("日期筛选格式错误，请修正后重试")
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
                if (op == "早于" and mt < cutoff) or (op != "早于" and mt >= cutoff):
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
                        f"条件: {op} {raw} | 文件日期: {lo} ~ {hi}")
                else:
                    self._set_preview("没有符合筛选条件的文件")
                return
            self.lbl_filter_info.configure(
                text=f"筛选: {len(self.files)}/{total} 个文件",
                foreground="gray")
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

        # 计算排期（仅筛选后的章节）
        schedule = None
        if mode == "schedule":
            try:
                date_str = self.date_var.get()
                datetime.strptime(date_str, "%Y-%m-%d")
                time_str = self.time_var.get().strip() or "08:00"
                per_day = self.perday_var.get()
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
        if schedule:
            # 统计首天章数即为 effective per_day
            first_day = schedule[0][0]
            eff = sum(1 for d, _ in schedule if d == first_day)
            summary += f" | 每天{eff}章 | 排期: {date_str} ~ {schedule[-1][0]}"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = max(kept_count, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{kept_count}")

    def _refresh_edit_preview(self):
        """修改模式专用预览: 显示匹配状态。"""
        self.lbl_resched_filter_info.configure(text="", foreground="gray")

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
                    f"  {i+1:3d}. {num_str} {title}  ({wc}字)  [待获取章节列表]")

        summary = f"总计: {len(self.files)} 章, {total_words} 字 | 模式: 修改内容"
        if platform_chapters:
            summary += f" | 匹配: {matched_count}/{len(self.files)}"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = max(matched_count, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{matched_count}")

    @staticmethod
    def _parse_chapter_spec(raw):
        """解析章节筛选表达式 → 区间列表 [(lo, hi), ...]；非法返回 None。

        支持逗号分隔的单号与范围混用: "1,3,5-10"（范围亦可用 ~，
        分隔符兼容 , ; 、以及 NFKC 归一后的全角逗号/分号）。
        """
        intervals = []
        for token in re.split(r'[,;、]', raw):
            token = token.strip()
            if not token:
                continue
            m = re.match(r'^(\d+)\s*[-~]\s*(\d+)$', token)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if lo > hi:
                    lo, hi = hi, lo
                intervals.append((lo, hi))
                continue
            if token.isdigit():
                n = int(token)
                intervals.append((n, n))
                continue
            return None  # 含非法 token
        return intervals

    def _filter_by_chapter_num(self, items, key):
        """按章节序号筛选列表。

        key(item) 提取章节序号 (int 或 None, None 视为不匹配)。
        - 纯数字单值: 按 ≤ / ≥ 运算符做阈值筛选（原有行为）
        - 含逗号或范围: 集合命中模式，支持 "1,3,5-10" 混用，运算符不适用
        返回 (filtered_items, is_active)。同时更新筛选信息标签。
        """
        # 默认恢复运算符下拉框（组合/范围格式时会覆盖为 disabled）
        self.cmb_resched_filter_op.configure(state="readonly")

        if not self.resched_filter_var.get():
            self.lbl_resched_filter_info.configure(text="", foreground="gray")
            return items, False
        try:
            raw = unicodedata.normalize("NFKC", self.resched_filter_num_var.get()).strip()
        except tk.TclError:
            return items, False
        if not raw:
            self.lbl_resched_filter_info.configure(text="", foreground="gray")
            return items, False

        total = len(items)

        # 组合/范围格式: "5-10"、"1,3,5-10"、"3、7~9" → 集合命中，运算符不适用
        if not raw.isdigit():
            intervals = self._parse_chapter_spec(raw)
            if not intervals:  # None=含非法 token；[]=只有分隔符
                self.lbl_resched_filter_info.configure(
                    text="请输入数字、范围或组合(如 1,3,5-10)", foreground="red")
                return items, False
            self.cmb_resched_filter_op.configure(state="disabled")
            kept = [x for x in items
                    if (v := _as_int(key(x))) is not None
                    and any(lo <= v <= hi for lo, hi in intervals)]
            self.lbl_resched_filter_info.configure(
                text=f"筛选: {len(kept)}/{total} 章", foreground="gray")
            return kept, True

        # 单值格式: ≤ / ≥
        try:
            threshold = int(raw)
        except ValueError:
            self.lbl_resched_filter_info.configure(
                text="请输入数字、范围或组合(如 1,3,5-10)", foreground="red")
            return items, False
        op = self.resched_filter_op_var.get()
        if op == "≤":
            kept = [x for x in items
                    if (v := _as_int(key(x))) is not None and v <= threshold]
        else:
            kept = [x for x in items
                    if (v := _as_int(key(x))) is not None and v >= threshold]
        self.lbl_resched_filter_info.configure(
            text=f"筛选: {len(kept)}/{total} 章", foreground="gray")
        return kept, True

    def _refresh_reschedule_preview(self):
        """修改排期模式预览: 显示平台章节 + 计算的新排期。"""
        self.lbl_resched_filter_info.configure(text="", foreground="gray")

        idx = self.cmb_book.current()
        book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None

        def _early_return(text):
            self._set_preview(text)
            self.progress["maximum"] = 1
            self.progress["value"] = 0
            self.lbl_progress.configure(text="")

        if not book_id:
            _early_return("请先选择作品")
            return

        cache_key = self._chapter_cache_key(book_id)
        if cache_key not in self._platform_chapters_cache:
            _early_return("正在获取章节列表...")
            return

        all_chapters = self._platform_chapters_cache[cache_key]
        if not all_chapters:
            _early_return("平台无章节")
            return

        # 反转顺序（章节管理页最新在前）+ 只保留"待发布"章节
        platform_chapters = [
            ch for ch in reversed(all_chapters)
            if "待发布" in ch.get("status", "")
        ]
        if not platform_chapters:
            _early_return("无待发布章节（仅「待发布」状态可修改排期）")
            return

        # 按章节序号筛选
        platform_chapters, _ = self._filter_by_chapter_num(
            platform_chapters, key=lambda ch: ch.get("chapterNum"))
        if not platform_chapters:
            _early_return("筛选后无待发布章节")
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
        """解析定时时间字符串，成功返回 datetime，失败返回 None。"""
        raw = (raw or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

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
            self.lbl_timer_status.configure(text="定时未启动", foreground="gray")

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
                    text=f"⏰ 已到时间，等待（{busy_reason}）…", foreground="#d35400")
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
        prefix = "🔄 已刷新目录 ｜ " if self._timer_prerefresh_done else ""
        self.lbl_timer_status.configure(
            text=(f"⏰ {prefix}将于 {self._timer_target:%Y-%m-%d %H:%M} 执行"
                  f" ｜ 倒计时 {self._fmt_hms(remaining)}"),
            foreground="#d35400")
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
            self.btn_upload.configure(state="disabled", text="正在停止...")
            return

        # 验证
        if not AUTH_FILE.exists():
            self._notify("warning", "提示", "请先登录")
            return
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            self._notify("warning", "提示", "请先刷新并选择作品")
            return
        mode = self.mode_var.get()
        book_id = self.books[idx]["bookId"]
        book_name = self.books[idx]["name"]

        # 修改排期模式: 不需要本地文件
        if mode == "reschedule":
            self._on_upload_reschedule(book_id, book_name)
            return

        if not self.files or not self.parsed_chapters:
            self._notify("warning", "提示", "请先选择章节文件夹")
            return

        # 修改内容模式
        if mode == "edit":
            self._on_upload_edit(book_id, book_name)
            return

        use_ai = self.use_ai_var.get()

        # 复制数据避免主线程修改
        parsed = list(self.parsed_chapters)
        files = list(self.files)

        # 按章节序号筛选
        total_before_filter = len(parsed)
        all_indices = list(range(len(parsed)))
        kept_indices, filter_active = self._filter_by_chapter_num(
            all_indices, key=lambda i: parsed[i][0])
        parsed = [parsed[i] for i in kept_indices]
        files = [files[i] for i in kept_indices]
        if not parsed:
            self._notify("warning", "提示", "筛选后无可上传章节")
            return

        # 定时发布参数
        schedule = None
        if mode == "schedule":
            try:
                date_str = self.date_var.get()
                start_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                self._notify("error", "日期错误", "请输入正确的日期: YYYY-MM-DD")
                return
            if start_dt.date() < datetime.now().date():
                if not self._ask_yes_no(
                        "日期提醒",
                        f"起始日期 {date_str} 已过去，"
                        f"平台可能拒绝定时发布到过去的日期。\n是否继续？"):
                    return
            try:
                per_day = max(1, self.perday_var.get())
            except tk.TclError:
                self._notify("error", "参数错误", "请输入有效的每天章数")
                return
            time_str = self.time_var.get().strip() or "08:00"
            if not _validate_times(time_str):
                self._notify(
                    "error", "时间格式错误",
                    "请输入有效的发布时间 (HH:MM)\n"
                    "多个时间用逗号分隔, 如: 08:00,12:00,20:00")
                return
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
        self._set_uploading(True)
        self.progress["maximum"] = max(count, 1)
        self.progress["value"] = 0

        self._install_log_handler()

        delay = self._cfg.get("delay_between_chapters", 3)

        async def task():
            try:
                url = NEW_CHAPTER_URL_TPL.format(book_id=book_id)

                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=False)
                    page = await context.new_page()

                    await page.goto(url)
                    try:
                        await wait_for_editor_ready(page)
                    except PWTimeout:
                        logger.error("无法进入编辑器，请检查 Book ID 和登录状态。")
                        await close_browser_safely(browser)
                        self._after(0, self._upload_done, 0, 0)
                        return

                    success = 0
                    failed = 0
                    consec_fail = 0
                    fail_list = []  # (章节标签, 失败原因)
                    total = len(files)
                    max_retries = self._cfg.get("max_retries", 2)

                    for i in range(total):
                        if self._cancel_requested:
                            logger.info("用户取消上传。")
                            break

                        chapter_num, title, content = parsed[i]
                        num_str = f"第{chapter_num}章 " if chapter_num else ""
                        sched_info = ""
                        if schedule:
                            sched_info = f" -> {schedule[i][0]} {schedule[i][1]}"
                        logger.info(f"[{i+1}/{total}] {num_str}{title}{sched_info}")

                        ok = False
                        daily_limit = False
                        for attempt in range(1, max_retries + 2):
                            try:
                                if i > 0 or attempt > 1:
                                    await page.goto(url)
                                    await wait_for_editor_ready(page)

                                await fill_chapter(page, chapter_num, title, content)

                                if schedule:
                                    d, t = schedule[i]
                                    await publish_scheduled(page, d, t, use_ai=use_ai)
                                    logger.info(f"  -> 定时发布 {d} {t}")
                                elif mode == "publish":
                                    await _navigate_to_publish_settings(page, use_ai=use_ai)
                                    btn = page.locator("button", has_text="确认发布")
                                    if await btn.count() == 0:
                                        raise RuntimeError("未找到确认发布按钮")
                                    await btn.first.click(
                                        no_wait_after=True, timeout=get_browser_timeout())
                                    await _wait_publish_result(page, btn.first)
                                    logger.info("  -> 已发布")
                                else:
                                    await save_draft(page)
                                    logger.info("  -> 已存草稿")

                                ok = True
                                break

                            except DailyLimitReached as e:
                                # 上限按字数计、非硬墙：字数较短的后续章节可能仍
                                # 发得出去，故只记录原因、跳过本章，不中止整批。
                                logger.warning(f"  跳过本章（{e}），继续后续章节")
                                fail_list.append((f"{num_str}{title}", str(e)))
                                daily_limit = True
                                break

                            except Exception as e:
                                if attempt <= max_retries:
                                    logger.warning(f"第{attempt}次失败: {e}，重试中...")
                                    await page.wait_for_timeout(2000)
                                else:
                                    logger.error(f"失败: {e}")
                                    fail_list.append((f"{num_str}{title}", str(e)))
                                    try:
                                        err = SCRIPT_DIR / f"error_{i}_{files[i].stem}.png"
                                        await page.screenshot(path=str(err))
                                        logger.error(f"  截图: {err}")
                                    except Exception:
                                        pass

                        if daily_limit:
                            failed += 1
                            # 上限失败重置熔断计数：toast 被正确捕获说明流程健康
                            consec_fail = 0
                        elif ok:
                            success += 1
                            consec_fail = 0
                        else:
                            failed += 1
                            consec_fail += 1
                            if consec_fail >= 3:
                                rest = total - (i + 1)
                                logger.error(
                                    f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                                    f"中止任务，剩余 {rest} 章未处理")
                                self._after(0, self._update_progress, i + 1, total)
                                break

                        self._after(0, self._update_progress, i + 1, total)

                        if i < total - 1 and delay > 0:
                            await page.wait_for_timeout(delay * 1000)

                    await save_auth(context)
                    await close_browser_safely(browser)

                    # 汇总与完成通知放在 async with 内：浏览器挂死时
                    # playwright stop（with 退出）可能同样阻塞，不能让它
                    # 挡住结果汇报（实测曾让 GUI"卡死"41 分钟）
                    logger.info(f"{'='*40}")
                    logger.info(f"  上传完成! 成功: {success}  失败: {failed}")
                    _log_fail_list(fail_list)
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
        self._auto_run_pending = False
        self._remove_log_handler()
        # 先清缓存再切换上传状态，使 _set_uploading 在缓存为空时正确禁用按钮
        self._invalidate_caches("chapters")
        self._set_uploading(False)

        # 修改/排期模式：缓存已清，主动重拉一次平台章节，避免后续切换筛选器时
        # _refresh_edit_preview 拿不到平台章节而把 _matched_edit 置空
        if self.mode_var.get() in ("edit", "reschedule"):
            self._fetch_platform_chapters_for_edit()

        # 上传完成后将 .auth_state.json 回写到命名账号文件（保持 cookie 新鲜）
        acct = self._gui_state.get("current_account", "")
        if acct and AUTH_FILE.exists():
            named = SCRIPT_DIR / f".auth_{acct}.json"
            try:
                shutil.copy2(str(AUTH_FILE), str(named))
            except Exception:
                pass

        if success >= 0:
            if auto:
                logger.info(f"[定时] 操作完成：成功 {success} 章，失败 {failed} 章")
                self.lbl_timer_status.configure(
                    text=f"✅ 定时执行完成：成功 {success} 失败 {failed}",
                    foreground="green")
            else:
                messagebox.showinfo("操作完成", f"成功 {success} 章，失败 {failed} 章")
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

        msg = f"即将修改「{book_name}」的 {count} 个章节内容"
        if not self._ask_yes_no("确认修改", msg):
            return

        self._set_uploading(True)
        self.progress["value"] = 0
        self.progress["maximum"] = max(count, 1)

        self._install_log_handler()

        delay = self._cfg.get("delay_between_chapters", 3)
        use_ai = self.use_ai_var.get()
        matched_copy = list(matched)

        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=False)
                    page = await context.new_page()

                    success = 0
                    failed = 0
                    skipped = 0
                    consec_fail = 0
                    fail_list = []  # (章节标签, 失败原因)
                    dup_pending = []  # "重复标题"暂存，批末二次尝试
                    total = len(matched_copy)

                    for i, (local_idx, plat_ch, ch_num, title, content) in enumerate(matched_copy):
                        if self._cancel_requested:
                            logger.info("用户取消修改。")
                            break

                        logger.info(f"[{i+1}/{total}] 修改第{ch_num}章 {title}")

                        status = plat_ch.get("status", "")
                        if "审核中" in status:
                            logger.warning(f"  状态「{status}」审核中，不可编辑，跳过")
                            skipped += 1
                            self._after(0, self._update_progress, i + 1, total)
                            continue

                        edit_url = plat_ch.get("editUrl")
                        if not edit_url:
                            logger.error("无法获取编辑链接，跳过（可能审核中或平台未提供编辑入口）")
                            skipped += 1
                            self._after(0, self._update_progress, i + 1, total)
                            continue

                        if edit_url.startswith("/"):
                            edit_url = BASE_URL + edit_url

                        try:
                            ok, err = await edit_one_chapter(
                                page, edit_url, ch_num, title, content,
                                use_ai=use_ai,
                                max_retries=self._cfg.get("max_retries", 2))
                            if ok:
                                success += 1
                                consec_fail = 0
                            elif "重复" in err:
                                # 标题搬移的临时冲突（本地重新编号后新章先于
                                # 旧章提交同名标题被拒），留待批末二次尝试
                                logger.info("  标题暂被其他章节占用，留待批末二次尝试")
                                dup_pending.append((ch_num, title, content, edit_url))
                                consec_fail = 0
                            else:
                                failed += 1
                                fail_list.append(
                                    (f"第{ch_num}章 {title}",
                                     err or "重试后仍失败(见日志/截图)"))
                                consec_fail += 1
                                if consec_fail >= 3:
                                    rest = total - (i + 1)
                                    logger.error(
                                        f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                                        f"中止任务，剩余 {rest} 章未处理")
                                    skipped += rest
                                    self._after(0, self._update_progress, i + 1, total)
                                    break
                        except DailyLimitReached as e:
                            # 上限按字数计、非硬墙：字数较短的后续章节可能仍发得
                            # 出去，故只记录原因、跳过本章，不中止整批。toast 被
                            # 正确捕获说明流程健康，重置熔断计数。
                            logger.warning(f"  跳过本章（{e}），继续后续章节")
                            fail_list.append((f"第{ch_num}章 {title}", str(e)))
                            failed += 1
                            consec_fail = 0
                        except Exception as e:
                            # 兜底浏览器崩溃/页面被关等意外，按原因不明失败计入
                            # 熔断，保证 save_auth/汇总仍能执行而不是整批裸抛中止。
                            logger.error(f"  本章发生未预期异常: {e}")
                            fail_list.append(
                                (f"第{ch_num}章 {title}", f"未预期异常: {e}"))
                            failed += 1
                            consec_fail += 1
                            if consec_fail >= 3:
                                rest = total - (i + 1)
                                logger.error(
                                    f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                                    f"中止任务，剩余 {rest} 章未处理")
                                skipped += rest
                                self._after(0, self._update_progress, i + 1, total)
                                break

                        self._after(0, self._update_progress, i + 1, total)

                        if i < total - 1 and delay > 0:
                            await page.wait_for_timeout(delay * 1000)

                    # 批末二次尝试: 主循环跑完后，占用旧标题的章节多已更新
                    if dup_pending:
                        if not self._cancel_requested:
                            logger.info("")
                            logger.info(
                                f"二次尝试 {len(dup_pending)} 个标题重复的章节"
                                f"（标题搬移的临时冲突，此时多已解除）...")
                        for idx2, (ch_num, title, content, edit_url) in \
                                enumerate(dup_pending):
                            if self._cancel_requested:
                                # 中途/事前取消: 剩余章节如实计入失败清单
                                for ch_num2, title2, *_ in dup_pending[idx2:]:
                                    fail_list.append(
                                        (f"第{ch_num2}章 {title2}",
                                         "标题重复(用户取消，未二次尝试)"))
                                    failed += 1
                                break
                            logger.info(f"[二次] 修改第{ch_num}章 {title}")
                            try:
                                ok, err = await edit_one_chapter(
                                    page, edit_url, ch_num, title, content,
                                    use_ai=use_ai, max_retries=0)
                                if ok:
                                    success += 1
                                else:
                                    failed += 1
                                    fail_list.append(
                                        (f"第{ch_num}章 {title}",
                                         err or "标题重复，二次尝试仍失败"))
                            except DailyLimitReached as e:
                                logger.warning(f"  跳过本章（{e}）")
                                fail_list.append((f"第{ch_num}章 {title}", str(e)))
                                failed += 1
                            except Exception as e:
                                logger.error(f"  二次尝试异常: {e}")
                                fail_list.append(
                                    (f"第{ch_num}章 {title}", f"二次尝试异常: {e}"))
                                failed += 1
                            if delay > 0:
                                try:
                                    await page.wait_for_timeout(delay * 1000)
                                except Exception:
                                    pass  # 页面已死也要走完计数与汇总

                    await save_auth(context)
                    await close_browser_safely(browser)

                    # 汇总与完成通知放在 async with 内：浏览器挂死时
                    # playwright stop（with 退出）可能同样阻塞，不能让它
                    # 挡住结果汇报（实测曾让 GUI"卡死"41 分钟）
                    logger.info(f"{'='*40}")
                    skip_str = f"  跳过: {skipped}" if skipped else ""
                    logger.info(
                        f"  修改完成! 成功: {success}  失败: {failed}{skip_str}")
                    _log_fail_list(fail_list)
                    logger.info(f"{'='*40}")

                    self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"修改异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    # -----------------------------------------------------------------------
    # 修改排期
    # -----------------------------------------------------------------------
    def _on_upload_reschedule(self, book_id, book_name):
        """修改排期: 在章节管理页批量修改待发布章节的排期设置。"""
        # 验证排期参数
        try:
            date_str = self.date_var.get()
            start_dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            self._notify("error", "日期错误", "请输入正确的日期: YYYY-MM-DD")
            return
        if start_dt.date() < datetime.now().date():
            if not self._ask_yes_no(
                    "日期提醒",
                    f"起始日期 {date_str} 已过去，"
                    f"平台可能拒绝定时发布到过去的日期。\n是否继续？"):
                return
        try:
            per_day = max(1, self.perday_var.get())
        except tk.TclError:
            self._notify("error", "参数错误", "请输入有效的每天章数")
            return
        time_str = self.time_var.get().strip() or "08:00"
        if not _validate_times(time_str):
            self._notify(
                "error", "时间格式错误",
                "请输入有效的发布时间 (HH:MM)\n"
                "多个时间用逗号分隔, 如: 08:00,12:00,20:00")
            return

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
            self._notify("info", "提示", "没有「待发布」状态的章节可修改排期。")
            return

        # 按章节序号筛选
        platform_chapters, _ = self._filter_by_chapter_num(
            platform_chapters, key=lambda ch: ch.get("chapterNum"))
        if not platform_chapters:
            self._notify("info", "提示", "筛选后无待发布章节可修改排期。")
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
        self._set_uploading(True)
        self.progress["value"] = 0
        self.progress["maximum"] = max(count, 1)

        self._install_log_handler()

        delay = self._cfg.get("delay_between_chapters", 3)
        smap = dict(schedule_map)
        vol = self._get_selected_volume()
        # "合并所有卷"模式: 传入所有卷名列表
        all_vol_names = None
        if self.all_volumes_var.get():
            vols = self._volumes_cache.get(book_id) or []
            all_vol_names = [
                v["text"] if isinstance(v, dict) else v for v in vols
            ] or None

        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=False)
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
    # 窗口关闭
    # -----------------------------------------------------------------------
    def _on_close(self):
        if self.uploading:
            if not messagebox.askyesno("确认", "上传正在进行中，确定退出吗？"):
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
        # pythonw 下 raise 无可见输出，改用 messagebox 告知用户
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror(
                "启动异常",
                f"程序异常退出:\n{e}\n\n详细日志: {UPLOAD_LOG_FILE}")
        except Exception:
            pass
