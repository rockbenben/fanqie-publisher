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
        parse_md_file, get_md_files, strip_md_formatting,
        deduplicate_titles, compute_schedule, _validate_times,
        DailyLimitReached, _check_daily_limit,
        create_context, save_auth,
        wait_for_editor_ready, fill_chapter,
        save_draft, publish_scheduled, _navigate_to_publish_settings,
        extract_chapters_from_page, match_chapters, edit_one_chapter,
        reschedule_on_manage_page,
        AUTH_FILE, BASE_URL, BOOK_MANAGE_URL, NEW_CHAPTER_URL_TPL,
        CHAPTER_MANAGE_URL_TPL, SCRIPT_DIR, ZONE_URL, CONFIG_FILE, GUI_STATE_FILE,
        BOOKS_JS, LAST_PUBLISH_JS,
        logger, setup_logging, LOG_FILE as UPLOAD_LOG_FILE, _browser_timeout,
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

    async def ensure(self):
        """确保浏览器运行中，返回 context。"""
        if self._browser and self._browser.is_connected():
            return self._context
        await self.close()
        self._pw = await async_playwright().start()
        self._browser, self._context = await create_context(
            self._pw, headless=True)
        return self._context

    async def refresh(self):
        """重新创建（登录后需要刷新 auth）。"""
        await self.close()

    async def close(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.debug(f"关闭浏览器: {e}")
        if self._pw:
            try:
                await self._pw.stop()
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
        _w, _h = 1100, 920
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
        self.uploading = False
        self._closing = False
        self._cancel_requested = False
        self._log_handler = None
        self._last_publish_cache: dict[str, dict] = {}  # bookId -> {date, time}
        self._platform_chapters_cache: dict[str, list] = {}  # bookId -> [章节列表]
        self._matched_edit: list = []  # 修改模式匹配结果
        self._shared = _SharedBrowser()  # 复用的无头浏览器
        self._fetch_gen = 0  # 防抖: 每次切换作品递增
        self._login_in_progress = False  # 防止并发登录

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # UI 构建
    # -----------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- 1. 账号 ---
        frm = ttk.LabelFrame(self.root, text="账号")
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
        lbl_gh.bind("<Button-1>", lambda _: webbrowser.open(
            "https://github.com/rockbenben/fanqie-publisher"))
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

        # --- 3. 章节目录 ---
        frm_dir = ttk.LabelFrame(self.root, text="章节目录")
        frm_dir.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_dir)
        row1.pack(fill="x")
        self.dir_var = tk.StringVar()
        # 默认使用脚本目录下的 chapters/（不存在则自动创建）
        DEFAULT_CHAPTERS_DIR.mkdir(exist_ok=True)
        self.dir_var.set(str(DEFAULT_CHAPTERS_DIR))
        ttk.Entry(row1, textvariable=self.dir_var, state="readonly").pack(
            side="left", padx=6, pady=4, fill="x", expand=True)
        ttk.Button(row1, text="浏览...", command=self._on_browse_dir).pack(
            side="left", pady=4)
        ttk.Button(row1, text="刷新", command=self._reload_chapters).pack(
            side="left", padx=(4, 6), pady=4)
        self.unique_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row1, text="自动去重标题", variable=self.unique_var,
            command=self._reload_chapters).pack(side="left", padx=6)
        self.filter_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row1, text="筛选修改日期",
            variable=self.filter_var,
            command=self._on_filter_toggle).pack(side="left", padx=6)

        # 筛选选项行（勾选后展开）
        row_filter = ttk.Frame(frm_dir)
        self.filter_op_var = tk.StringVar(value="晚于")
        self.cmb_filter_op = ttk.Combobox(
            row_filter, textvariable=self.filter_op_var,
            values=["早于", "晚于"], width=5, state="readonly")
        self.cmb_filter_op.pack(side="left", padx=(6, 2))
        self.cmb_filter_op.bind("<<ComboboxSelected>>",
                                lambda _: self._reload_chapters())
        self.filter_date_var = tk.StringVar(
            value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.ent_filter_date = ttk.Entry(
            row_filter, textvariable=self.filter_date_var, width=16)
        self.ent_filter_date.pack(side="left", padx=2)
        self.ent_filter_date.bind(
            "<FocusOut>", lambda _: self._reload_chapters())
        self.ent_filter_date.bind(
            "<Return>", lambda _: self._reload_chapters())
        ttk.Label(row_filter, text="(格式: 2025-01-01 08:00)",
                  foreground="gray").pack(side="left", padx=2)
        self.lbl_filter_info = ttk.Label(row_filter, text="", foreground="gray")
        self.lbl_filter_info.pack(side="left", padx=6)
        self._filter_row = row_filter

        # --- 4. 发布模式 ---
        frm_mode = ttk.LabelFrame(self.root, text="发布模式")
        frm_mode.pack(fill="x", **pad)

        row_radios = ttk.Frame(frm_mode)
        row_radios.pack(fill="x", padx=6, pady=4)
        self.mode_var = tk.StringVar(value=self._cfg.get("default_mode", "schedule"))
        self._mode_radios: list[ttk.Radiobutton] = []
        for text, val in [("定时发布", "schedule"), ("立即发布", "publish"),
                          ("存草稿", "draft"), ("修改内容", "edit"),
                          ("修改定时", "reschedule")]:
            rb = ttk.Radiobutton(
                row_radios, text=text, variable=self.mode_var,
                value=val, command=self._on_mode_change)
            rb.pack(side="left", padx=10)
            self._mode_radios.append(rb)

        # 发布选项（所有非草稿模式可见）
        row_opts = ttk.Frame(frm_mode)
        row_opts.pack(fill="x", padx=6, pady=(0, 4))
        self.use_ai_var = tk.BooleanVar(value=False)
        self.chk_use_ai = ttk.Checkbutton(
            row_opts, text="稿件使用了AI创作", variable=self.use_ai_var)
        self.chk_use_ai.pack(side="left", padx=6)

        # 上次发布信息（所有模式可见）
        self.lbl_last_publish = ttk.Label(
            frm_mode, text="上次定时发布: (选择作品后自动获取)", foreground="gray")
        self.lbl_last_publish.pack(fill="x", padx=12, pady=(0, 4))

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
        self.perday_var = tk.IntVar(value=self._cfg.get("default_per_day", 2))
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
        ttk.Label(r2, text="(多时间逗号分隔, 如 07:00,12:00,20:00)").pack(side="left")

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

        # 章节序号筛选（修改定时模式专用）
        self.resched_filter_var = tk.BooleanVar(value=False)
        self._resched_filter_row = ttk.Frame(frm_mode)
        ttk.Checkbutton(
            self._resched_filter_row, text="筛选章节序号",
            variable=self.resched_filter_var,
            command=self._refresh_preview).pack(side="left", padx=(12, 4))
        self.resched_filter_op_var = tk.StringVar(value="≥")
        self.cmb_resched_filter_op = ttk.Combobox(
            self._resched_filter_row, textvariable=self.resched_filter_op_var,
            values=["≤", "≥"], width=3, state="readonly")
        self.cmb_resched_filter_op.pack(side="left", padx=2)
        self.cmb_resched_filter_op.bind("<<ComboboxSelected>>",
                                        lambda _: self._refresh_preview())
        ttk.Label(self._resched_filter_row, text="第").pack(side="left", padx=(4, 0))
        self.resched_filter_num_var = tk.StringVar(value="1")
        self.ent_resched_filter_num = ttk.Entry(
            self._resched_filter_row, textvariable=self.resched_filter_num_var, width=6)
        self.ent_resched_filter_num.pack(side="left", padx=2)
        ttk.Label(self._resched_filter_row, text="章").pack(side="left")
        self.resched_filter_num_var.trace_add("write", lambda *_: self._refresh_preview())
        self.ent_resched_filter_num.bind("<Return>", lambda _: self.txt_preview.focus_set())
        self.lbl_resched_filter_info = ttk.Label(
            self._resched_filter_row, text="", foreground="gray")
        self.lbl_resched_filter_info.pack(side="left", padx=6)

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

    # -----------------------------------------------------------------------
    # UI 辅助
    # -----------------------------------------------------------------------
    def _after(self, ms, func, *args):
        """安全的 root.after 调用，窗口关闭后不再调度。"""
        if self._closing:
            return
        try:
            self.root.after(ms, func, *args)
        except tk.TclError:
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
                    text="登录状态: 已保存", foreground="green")
        else:
            self.lbl_auth.configure(text="登录状态: 未登录", foreground="red")

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

        # 清缓存
        self._last_publish_cache.clear()
        self._platform_chapters_cache.clear()

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
        self.chk_use_ai.pack_forget()

        # --- 2. 按模式显示组件（注意 pack 顺序决定布局顺序） ---
        #   lbl_last_publish: schedule, edit, reschedule
        #   sched_frame:      schedule, reschedule
        #   _resched_filter_row: reschedule only
        #   chk_use_ai:       schedule, publish, edit
        if mode in ("schedule", "edit", "reschedule"):
            self.lbl_last_publish.pack(fill="x", padx=12, pady=(0, 4))
        if mode in ("schedule", "reschedule"):
            self.sched_frame.pack(fill="x", padx=6, pady=4)
        if mode == "reschedule":
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
                if not (book_id and book_id in self._platform_chapters_cache):
                    self.btn_upload.configure(state="disabled")
                else:
                    self.btn_upload.configure(state="normal")
            else:
                self.btn_upload.configure(state="normal")

        # --- 4. 模式特有逻辑 ---
        if mode == "schedule":
            self._on_book_changed()
        elif mode in ("edit", "reschedule"):
            self._fetch_platform_chapters_for_edit()

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

    def _save_config(self):
        """将当前 GUI 设置写入 config.json。"""
        self._cfg["default_mode"] = self.mode_var.get()
        self._cfg["default_time"] = self.time_var.get().strip() or "08:00"
        try:
            self._cfg["default_per_day"] = self.perday_var.get()
        except tk.TclError:
            pass
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except Exception:
            pass

    # --- GUI 内部状态 (.gui_state.json，不含在用户 config 中) ---

    @staticmethod
    def _load_gui_state() -> dict:
        if GUI_STATE_FILE.exists():
            try:
                with open(GUI_STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    def _save_gui_state(self):
        try:
            with open(GUI_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._gui_state, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except Exception:
            pass

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
            # 修改/修改定时模式下如果未载入章节列表则禁用
            if self.mode_var.get() in ("edit", "reschedule"):
                idx = self.cmb_book.current()
                book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
                if not (book_id and book_id in self._platform_chapters_cache):
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

        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            return

        book_id = self.books[idx]["bookId"]

        # 记住选择的作品（按账号区分，存到隐藏状态文件）
        acct = self._gui_state.get("current_account", "")
        key = f"last_book_id_{acct}" if acct else "last_book_id"
        self._gui_state[key] = book_id
        self._save_gui_state()

        # 修改/修改定时模式: 走专用的章节列表获取（同时获取上次发布信息）
        if self.mode_var.get() in ("edit", "reschedule"):
            self.btn_upload.configure(state="disabled")
            self._fetch_platform_chapters_for_edit()
            return

        # 有缓存直接用
        if book_id in self._last_publish_cache:
            self._apply_last_publish(self._last_publish_cache[book_id])
            return

        # 后台获取（仅默认页，一般最新发布在首页即可看到）
        if not AUTH_FILE.exists():
            return

        self.lbl_last_publish.configure(
            text="上次定时发布: 获取中...", foreground="gray")

        gen = self._fetch_gen

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
                    await page.wait_for_selector("tr td", timeout=_browser_timeout)
                except PWTimeout:
                    pass
                if self._fetch_gen != gen:
                    return
                result = await page.evaluate(LAST_PUBLISH_JS)
                self._after(0, self._last_publish_fetched, book_id, result)
            except Exception:
                if self._fetch_gen == gen:
                    self._after(0, self._last_publish_fetched, book_id, None)
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
                text="上次定时发布: 未找到", foreground="gray")

    def _apply_last_publish(self, info):
        """将上次发布信息显示到 UI 并自动建议下一天起始日期。

        只更新 date_var，不覆盖 time_var —— 时间是用户配置项，
        平台单条发布记录不应覆盖用户设定的多时间方案。
        """
        date_str = info["date"]
        time_str = info["time"]
        chapter = info.get("chapter", "")
        label = f"上次定时发布: {date_str} {time_str}"
        if chapter:
            label += f"  ({chapter})"
        self.lbl_last_publish.configure(text=label, foreground="#d35400")

        # 自动建议: 起始日期 = 上次日期 + 1 天
        try:
            last_dt = datetime.strptime(date_str, "%Y-%m-%d")
            next_dt = last_dt + timedelta(days=1)
            self.date_var.set(next_dt.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    # -----------------------------------------------------------------------
    # 修改模式: 获取平台章节列表
    # -----------------------------------------------------------------------
    def _fetch_platform_chapters_for_edit(self):
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            return

        book_id = self.books[idx]["bookId"]

        if book_id in self._platform_chapters_cache:
            self._on_platform_chapters_fetched(
                book_id, self._platform_chapters_cache[book_id], error=None)
            return

        if not AUTH_FILE.exists():
            return

        self.lbl_last_publish.configure(
            text="正在获取平台章节列表...", foreground="gray")

        gen = self._fetch_gen

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

                chapters, last_pub = await extract_chapters_from_page(
                    page, book_id)

                if self._fetch_gen != gen:
                    return
                self._after(0, self._on_platform_chapters_fetched,
                            book_id, chapters, None, last_pub)
            except Exception as e:
                if self._fetch_gen == gen:
                    self._after(0, self._on_platform_chapters_fetched,
                                book_id, [], str(e), None)
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

        self.worker.submit(task())

    def _on_platform_chapters_fetched(self, book_id, chapters, error,
                                      last_pub=None):
        # 检查当前选中的作品是否仍匹配
        idx = self.cmb_book.current()
        current_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
        if current_id != book_id:
            return  # 用户已切换作品，丢弃过期结果

        if error:
            self.lbl_last_publish.configure(
                text=f"获取章节列表失败: {error}", foreground="red")
            return

        self._platform_chapters_cache[book_id] = chapters
        # 缓存上次发布信息（来自同一浏览器会话）
        if last_pub and book_id not in self._last_publish_cache:
            self._last_publish_cache[book_id] = last_pub
        self.lbl_last_publish.configure(
            text=f"平台共 {len(chapters)} 个章节", foreground="#d35400")
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
        # 弹出对话框要求输入账号名称
        raw = simpledialog.askstring(
            "账号名称",
            "请输入账号名称（如 作家A）：\n用于区分多个登录账号",
            parent=self.root,
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
                    await page.goto(ZONE_URL)
                    await page.wait_for_load_state("networkidle")

                    self._after(0, self._show_login_dialog)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._login_event.wait)

                    if self._login_cancelled:
                        await browser.close()
                        self._after(0, self._login_done, "cancelled")
                        return

                    await save_auth(context)

                    # 将活跃 auth 复制为命名文件
                    named = SCRIPT_DIR / f".auth_{name}.json"
                    shutil.copy2(str(AUTH_FILE), str(named))

                    await browser.close()

                self._after(0, self._login_done, None)
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
            body, text="登录完成后点击下方按钮保存会话",
            font=("", 9), bg="#FFF3CD", fg="#856404",
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

    # -----------------------------------------------------------------------
    # 刷新作品列表
    # -----------------------------------------------------------------------
    def _on_refresh_books(self):
        if not AUTH_FILE.exists():
            messagebox.showwarning("提示", "请先登录")
            return
        self.btn_books.configure(state="disabled")
        self._log("正在获取作品列表...")

        async def task():
            page = None
            try:
                ctx = await self._shared.ensure()
                page = await ctx.new_page()
                await page.goto(BOOK_MANAGE_URL)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(3000)
                books = await page.evaluate(BOOKS_JS)
                await save_auth(ctx)
                self._after(0, self._books_fetched, books, None)
            except Exception as e:
                self._after(0, self._books_fetched, [], str(e))
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

        self.worker.submit(task())

    def _books_fetched(self, books, error):
        self.btn_books.configure(state="normal")
        if error:
            self._log(f"获取失败: {error}")
            return
        self.books = books
        self._last_publish_cache.clear()  # 清空缓存
        self._platform_chapters_cache.clear()  # 修改模式章节缓存也一起清
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
        d = filedialog.askdirectory(title="选择章节 MD 文件目录")
        if not d:
            return
        self.dir_var.set(d)
        self._reload_chapters()

    def _on_filter_toggle(self):
        if self.filter_var.get():
            self._filter_row.pack(fill="x", padx=6, pady=(0, 4))
        else:
            self._filter_row.pack_forget()
            self.lbl_filter_info.configure(text="")
        self._reload_chapters()

    def _reload_chapters(self):
        """从磁盘重新扫描并解析章节文件。仅在目录变更/用户点刷新时调用。"""
        dir_path = self.dir_var.get()
        if not dir_path:
            return
        p = Path(dir_path)
        if not p.is_dir():
            self.files = []
            self.parsed_chapters = []
            self._set_preview("目录不存在")
            return

        self.files = get_md_files(p)
        if not self.files:
            self.parsed_chapters = []
            self._set_preview("目录中没有 .md/.txt 文件")
            return

        # 按修改日期筛选
        if self.filter_var.get():
            raw = self.filter_date_var.get().strip()
            try:
                # 支持 "2025-01-01 08:00" 或 "2025-01-01"
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        cutoff = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError("bad format")
                op = self.filter_op_var.get()
                total = len(self.files)
                if op == "早于":
                    self.files = [
                        f for f in self.files
                        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff]
                else:
                    self.files = [
                        f for f in self.files
                        if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff]
                self.lbl_filter_info.configure(
                    text=f"(筛选: {len(self.files)}/{total} 个文件)",
                    foreground="gray")
            except ValueError:
                self.lbl_filter_info.configure(
                    text="(日期格式错误)", foreground="red")

        if not self.files:
            self.parsed_chapters = []
            self._set_preview("没有符合筛选条件的文件")
            return

        self.parsed_chapters = [parse_md_file(f) for f in self.files]
        if self.unique_var.get():
            self.parsed_chapters = deduplicate_titles(self.parsed_chapters)

        self._refresh_preview()

    def _refresh_preview(self):
        """仅重新计算排期和刷新预览文本，不重新读取文件。"""
        mode = self.mode_var.get()

        # 修改定时模式: 不依赖本地文件，使用平台章节
        if mode == "reschedule":
            self._refresh_reschedule_preview()
            return

        if not self.files or not self.parsed_chapters:
            return

        # 修改内容模式: 专用预览
        if mode == "edit":
            self._refresh_edit_preview()
            return

        # 计算排期
        schedule = None
        if mode == "schedule":
            try:
                date_str = self.date_var.get()
                datetime.strptime(date_str, "%Y-%m-%d")
                time_str = self.time_var.get().strip() or "08:00"
                per_day = self.perday_var.get()
                schedule = compute_schedule(
                    len(self.parsed_chapters), date_str, time_str, per_day)
            except (ValueError, tk.TclError):
                pass

        lines = []
        total_words = 0
        for i, (num, title, content) in enumerate(self.parsed_chapters):
            wc = len(strip_md_formatting(content))
            total_words += wc
            num_str = f"第{num}章" if num else "  ?  "
            sched_str = ""
            if schedule:
                sched_str = f"  [{schedule[i][0]} {schedule[i][1]}]"
            lines.append(f"  {i+1:3d}. {num_str} {title}  ({wc}字){sched_str}")

        mode_labels = {"draft": "存草稿", "publish": "立即发布", "schedule": "定时发布",
                       "edit": "修改内容", "reschedule": "修改定时"}
        summary = f"总计: {len(self.files)} 章, {total_words} 字 | 模式: {mode_labels[mode]}"
        if schedule:
            # 统计首天章数即为 effective per_day
            first_day = schedule[0][0]
            eff = sum(1 for d, _ in schedule if d == first_day)
            summary += f" | 每天{eff}章 | 排期: {date_str} ~ {schedule[-1][0]}"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = len(self.files)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{len(self.files)}")

    def _refresh_edit_preview(self):
        """修改模式专用预览: 显示匹配状态。"""
        idx = self.cmb_book.current()
        book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None

        platform_chapters = []
        if book_id and book_id in self._platform_chapters_cache:
            platform_chapters = self._platform_chapters_cache[book_id]

        lines = []
        matched_count = 0
        total_words = 0

        if platform_chapters:
            matched, unmatched = match_chapters(
                self.parsed_chapters, platform_chapters)
            self._matched_edit = matched
            matched_count = len(matched)
            matched_indices = {m[0] for m in matched}

            for i, (num, title, content) in enumerate(self.parsed_chapters):
                wc = len(strip_md_formatting(content))
                total_words += wc
                num_str = f"第{num}章" if num else "  ?  "
                if i in matched_indices:
                    status = "[匹配]"
                elif num is None:
                    status = "[跳过:无章节号]"
                else:
                    status = "[跳过:平台无此章]"
                lines.append(
                    f"  {i+1:3d}. {num_str} {title}  ({wc}字)  {status}")
        else:
            self._matched_edit = []
            # 缓存为空但有有效作品: 自动重新获取平台章节
            if book_id and book_id not in self._platform_chapters_cache:
                self._fetch_platform_chapters_for_edit()
            for i, (num, title, content) in enumerate(self.parsed_chapters):
                wc = len(strip_md_formatting(content))
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

    def _filter_reschedule_chapters(self, chapters):
        """按章节序号筛选待修改定时的章节。"""
        if not self.resched_filter_var.get():
            self.lbl_resched_filter_info.configure(text="")
            return chapters
        try:
            num = int(self.resched_filter_num_var.get())
        except (ValueError, tk.TclError):
            self.lbl_resched_filter_info.configure(text="(序号无效)")
            return chapters
        op = self.resched_filter_op_var.get()
        total = len(chapters)
        if op == "≤":
            filtered = [ch for ch in chapters if (ch.get("chapterNum") or 0) <= num]
        else:  # ≥
            filtered = [ch for ch in chapters if (ch.get("chapterNum") or 0) >= num]
        self.lbl_resched_filter_info.configure(
            text=f"筛选: {len(filtered)}/{total} 章")
        return filtered

    def _refresh_reschedule_preview(self):
        """修改定时模式预览: 显示平台章节 + 计算的新排期。"""
        idx = self.cmb_book.current()
        book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None

        if not book_id:
            self._set_preview("请先选择作品")
            return

        if book_id not in self._platform_chapters_cache:
            self._set_preview("正在获取平台章节列表...")
            self._fetch_platform_chapters_for_edit()
            return

        all_chapters = self._platform_chapters_cache[book_id]
        if not all_chapters:
            self._set_preview("平台无章节")
            return

        # 反转顺序（章节管理页最新在前）+ 只保留"待发布"章节
        platform_chapters = [
            ch for ch in reversed(all_chapters)
            if "待发布" in ch.get("status", "")
        ]
        if not platform_chapters:
            self._set_preview("无待发布章节（仅待发布状态的章节可修改定时）")
            return

        # 按章节序号筛选
        platform_chapters = self._filter_reschedule_chapters(platform_chapters)
        if not platform_chapters:
            self._set_preview("筛选后无待发布章节")
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
            num = ch.get("chapterNum")
            title = ch.get("title", "")
            num_str = f"第{num}章" if num else "  ?  "
            sched_str = ""
            if schedule:
                sched_str = f"  [{schedule[i][0]} {schedule[i][1]}]"
            lines.append(f"  {i+1:3d}. {num_str} {title}{sched_str}")

        count = len(platform_chapters)
        summary = f"总计: {count} 章(待发布) | 模式: 修改定时"
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
    def _on_upload(self):
        if self.uploading:
            # 正在上传中 -> 请求取消
            self._cancel_requested = True
            self.btn_upload.configure(state="disabled", text="正在停止...")
            return

        # 验证
        if not AUTH_FILE.exists():
            messagebox.showwarning("提示", "请先登录")
            return
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            messagebox.showwarning("提示", "请先刷新并选择作品")
            return
        mode = self.mode_var.get()
        book_id = self.books[idx]["bookId"]
        book_name = self.books[idx]["name"]

        # 修改定时模式: 不需要本地文件
        if mode == "reschedule":
            self._on_upload_reschedule(book_id, book_name)
            return

        if not self.files or not self.parsed_chapters:
            messagebox.showwarning("提示", "请先选择章节目录")
            return

        # 修改内容模式
        if mode == "edit":
            self._on_upload_edit(book_id, book_name)
            return

        use_ai = self.use_ai_var.get()

        # 定时发布参数
        schedule = None
        if mode == "schedule":
            try:
                date_str = self.date_var.get()
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("日期错误", "请输入正确的日期: YYYY-MM-DD")
                return
            try:
                per_day = max(1, self.perday_var.get())
            except tk.TclError:
                messagebox.showerror("参数错误", "请输入有效的每天章数")
                return
            time_str = self.time_var.get().strip() or "08:00"
            if not _validate_times(time_str):
                messagebox.showerror(
                    "时间格式错误",
                    "请输入有效的发布时间 (HH:MM)\n"
                    "多个时间用逗号分隔, 如: 08:00,12:00,20:00")
                return
            schedule = compute_schedule(
                len(self.parsed_chapters), date_str, time_str, per_day)

        # 确认
        count = len(self.parsed_chapters)
        mode_labels = {"draft": "存草稿", "publish": "立即发布", "schedule": "定时发布",
                       "edit": "修改内容", "reschedule": "修改定时"}
        msg = f"即将上传 {count} 章到「{book_name}」\n模式: {mode_labels[mode]}"
        if schedule:
            msg += f"\n排期: {schedule[0][0]} ~ {schedule[-1][0]}"
        if not messagebox.askyesno("确认上传", msg):
            return

        # 开始
        self._set_uploading(True)
        self.progress["value"] = 0

        self._install_log_handler()

        delay = self._cfg.get("delay_between_chapters", 3)

        # 复制数据避免主线程修改
        parsed = list(self.parsed_chapters)
        files = list(self.files)

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
                        await browser.close()
                        self._after(0, self._upload_done, 0, 0)
                        return

                    success = 0
                    failed = 0
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
                                    await btn.first.click()
                                    await page.wait_for_timeout(2000)
                                    await _check_daily_limit(page)
                                    logger.info("  -> 已发布")
                                else:
                                    await save_draft(page)
                                    logger.info("  -> 已存草稿")

                                ok = True
                                break

                            except DailyLimitReached as e:
                                logger.warning(f"{e}")
                                daily_limit = True
                                break

                            except Exception as e:
                                if attempt <= max_retries:
                                    logger.warning(f"第{attempt}次失败: {e}，重试中...")
                                    await page.wait_for_timeout(2000)
                                else:
                                    logger.error(f"失败: {e}")
                                    try:
                                        err = SCRIPT_DIR / f"error_{i}_{files[i].stem}.png"
                                        await page.screenshot(path=str(err))
                                        logger.error(f"  截图: {err}")
                                    except Exception:
                                        pass

                        if daily_limit:
                            failed += 1
                            break

                        if ok:
                            success += 1
                        else:
                            failed += 1

                        self._after(0, self._update_progress, i + 1, total)

                        if i < total - 1 and delay > 0:
                            await page.wait_for_timeout(delay * 1000)

                    await save_auth(context)
                    await browser.close()

                logger.info(f"{'='*40}")
                logger.info(f"  上传完成! 成功: {success}  失败: {failed}")
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
        self._remove_log_handler()
        self._set_uploading(False)
        self._last_publish_cache.clear()  # 上传后清除缓存，下次获取最新数据
        self._platform_chapters_cache.clear()

        # 上传完成后将 .auth_state.json 回写到命名账号文件（保持 cookie 新鲜）
        acct = self._gui_state.get("current_account", "")
        if acct and AUTH_FILE.exists():
            named = SCRIPT_DIR / f".auth_{acct}.json"
            try:
                shutil.copy2(str(AUTH_FILE), str(named))
            except Exception:
                pass

        if success >= 0:
            messagebox.showinfo("完成", f"成功: {success}  失败: {failed}")

    # -----------------------------------------------------------------------
    # 修改模式上传
    # -----------------------------------------------------------------------
    def _on_upload_edit(self, book_id, book_name):
        if not self._matched_edit:
            messagebox.showwarning("提示", "没有匹配到任何章节。\n请先选择作品并等待章节列表获取完成。")
            return

        matched = self._matched_edit
        count = len(matched)

        msg = f"即将修改「{book_name}」的 {count} 个章节\n模式: 修改已有章节"
        if not messagebox.askyesno("确认修改", msg):
            return

        self._set_uploading(True)
        self.progress["value"] = 0
        self.progress["maximum"] = count

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
                    total = len(matched_copy)

                    for i, (local_idx, plat_ch, ch_num, title, content) in enumerate(matched_copy):
                        if self._cancel_requested:
                            logger.info("用户取消修改。")
                            break

                        logger.info(f"[{i+1}/{total}] 修改第{ch_num}章 {title}")

                        edit_url = plat_ch.get("editUrl")
                        if not edit_url:
                            logger.error("无法获取编辑链接，跳过")
                            failed += 1
                            self._after(0, self._update_progress, i + 1, total)
                            continue

                        if edit_url.startswith("/"):
                            edit_url = BASE_URL + edit_url

                        try:
                            if await edit_one_chapter(
                                    page, edit_url, ch_num, title, content,
                                    use_ai=use_ai,
                                    max_retries=self._cfg.get("max_retries", 2)):
                                success += 1
                            else:
                                failed += 1
                        except DailyLimitReached as e:
                            logger.warning(f"{e}")
                            failed += 1
                            break

                        self._after(0, self._update_progress, i + 1, total)

                        if i < total - 1 and delay > 0:
                            await page.wait_for_timeout(delay * 1000)

                    await save_auth(context)
                    await browser.close()

                logger.info(f"{'='*40}")
                logger.info(f"  修改完成! 成功: {success}  失败: {failed}")
                logger.info(f"{'='*40}")

                self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"修改异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    # -----------------------------------------------------------------------
    # 修改定时模式上传
    # -----------------------------------------------------------------------
    def _on_upload_reschedule(self, book_id, book_name):
        """修改定时: 在章节管理页批量修改待发布章节的定时发布设置。"""
        # 验证排期参数
        try:
            date_str = self.date_var.get()
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("日期错误", "请输入正确的日期: YYYY-MM-DD")
            return
        try:
            per_day = max(1, self.perday_var.get())
        except tk.TclError:
            messagebox.showerror("参数错误", "请输入有效的每天章数")
            return
        time_str = self.time_var.get().strip() or "08:00"
        if not _validate_times(time_str):
            messagebox.showerror(
                "时间格式错误",
                "请输入有效的发布时间 (HH:MM)\n"
                "多个时间用逗号分隔, 如: 08:00,12:00,20:00")
            return

        # 获取平台章节，反转顺序 + 只保留"待发布"
        all_chapters = self._platform_chapters_cache.get(book_id, [])
        if not all_chapters:
            messagebox.showwarning("提示", "没有平台章节数据。\n请先选择作品并等待章节列表获取完成。")
            return
        platform_chapters = [
            ch for ch in reversed(all_chapters)
            if "待发布" in ch.get("status", "")
        ]
        if not platform_chapters:
            messagebox.showinfo("提示", "没有待发布状态的章节可修改定时。")
            return

        # 按章节序号筛选
        platform_chapters = self._filter_reschedule_chapters(platform_chapters)
        if not platform_chapters:
            messagebox.showinfo("提示", "筛选后无待发布章节可修改定时。")
            return

        # 计算排期并构建 schedule_map
        schedule = compute_schedule(
            len(platform_chapters), date_str, time_str, per_day)
        schedule_map = {}
        for i, ch in enumerate(platform_chapters):
            schedule_map[ch["title"]] = schedule[i]

        count = len(platform_chapters)
        msg = (f"即将修改「{book_name}」的 {count} 个待发布章节的定时发布设置\n"
               f"排期: {schedule[0][0]} ~ {schedule[-1][0]}")
        if not messagebox.askyesno("确认修改定时", msg):
            return

        # 开始
        self._set_uploading(True)
        self.progress["value"] = 0
        self.progress["maximum"] = count

        self._install_log_handler()

        delay = self._cfg.get("delay_between_chapters", 3)
        smap = dict(schedule_map)

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
                    )

                    await save_auth(context)
                    await browser.close()

                logger.info(f"{'='*40}")
                logger.info(f"  修改定时完成! 成功: {success}  失败: {failed}")
                logger.info(f"{'='*40}")

                self._after(0, self._upload_done, success, failed)

            except Exception as e:
                logger.error(f"修改定时异常: {e}")
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
        self._remove_log_handler()
        # 关闭共享浏览器
        try:
            future = self.worker.submit(self._shared.close())
            future.result(timeout=5)
        except Exception as e:
            logger.debug(f"关闭共享浏览器: {e}")
        self.worker.stop()
        self.root.destroy()

    # -----------------------------------------------------------------------
    # 启动
    # -----------------------------------------------------------------------
    def run(self):
        if AUTH_FILE.exists():
            self.root.after(500, self._on_refresh_books)
        self.root.mainloop()


if __name__ == "__main__":
    setup_logging(UPLOAD_LOG_FILE)
    try:
        app = FanqieGUI()
        app.run()
    except Exception:
        logger.exception("启动异常")
        raise
