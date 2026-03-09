#!/usr/bin/env python3
"""
番茄作家 MD 批量上传工具 - GUI 界面

使用方法:
    python fanqie_gui.py
"""

import asyncio
import logging
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime, timedelta
from pathlib import Path

try:
    from fanqie_upload import (
        load_config,
        parse_md_file, get_md_files, strip_md_formatting,
        deduplicate_titles, compute_schedule,
        create_context, save_auth,
        wait_for_editor_ready, fill_chapter, clear_editor, dismiss_edit_hint,
        save_draft, publish_scheduled, _navigate_to_publish_settings,
        extract_chapters_from_page, match_chapters,
        AUTH_FILE, BASE_URL, BOOK_MANAGE_URL, NEW_CHAPTER_URL_TPL,
        CHAPTER_MANAGE_URL_TPL, SCRIPT_DIR, ZONE_URL,
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
LOG_FILE = SCRIPT_DIR / "fanqie_error.log"


# ---------------------------------------------------------------------------
# Stdout 重定向 — 捕获 print() 到 GUI 日志
# ---------------------------------------------------------------------------
class StdoutRedirector:
    def __init__(self, widget, root):
        self._widget = widget
        self._root = root
        self._original = sys.stdout

    def write(self, text):
        if self._original:
            self._original.write(text)
        if text:
            try:
                self._root.after(0, self._append, text)
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

    def flush(self):
        if self._original:
            self._original.flush()


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
# JS: 获取作品列表
# ---------------------------------------------------------------------------
BOOKS_JS = r"""() => {
    const results = [];
    const links = document.querySelectorAll('a[href*="chapter-manage/"]');
    for (const link of links) {
        const href = link.getAttribute('href') || '';
        const m = href.match(/chapter-manage\/(\d+)&([^?]*)/);
        if (!m) continue;
        const bookId = m[1];
        let name;
        try { name = decodeURIComponent(m[2]); }
        catch { name = m[2]; }
        let container = link;
        for (let i = 0; i < 12; i++) {
            if (!container.parentElement) break;
            container = container.parentElement;
            if (container.textContent.length > 30 &&
                container.textContent.includes('万字')) break;
        }
        const text = container.textContent || '';
        const chapterMatch = text.match(/(\d+)\s*章/);
        const wordMatch = text.match(/([\d.]+)\s*万字/);
        const statusMatch = text.match(/(连载中|已完结)/);
        results.push({
            bookId, name,
            chapters: chapterMatch ? chapterMatch[1] : '?',
            words: wordMatch ? wordMatch[1] + '万' : '?',
            status: statusMatch ? statusMatch[1] : '',
        });
    }
    return results;
}"""

# ---------------------------------------------------------------------------
# JS: 从章节管理页提取最后一个定时发布时间
# ---------------------------------------------------------------------------
LAST_PUBLISH_JS = r"""() => {
    // 遍历表格行，提取章节名称和发布时间，取日期最新的一行
    const re = /(\d{4}[-/]\d{2}[-/]\d{2})\s+(\d{2}:\d{2})/;
    let best = null, bestKey = '';
    for (const row of document.querySelectorAll('tr')) {
        const cells = row.querySelectorAll('td');
        if (cells.length < 2) continue;
        const m = row.textContent.match(re);
        if (!m) continue;
        const d = m[1].replace(/\//g, '-');
        const t = m[2];
        const key = d + ' ' + t;
        if (key > bestKey) {
            best = {date: d, time: t, chapter: cells[0].textContent.trim()};
            bestKey = key;
        }
    }
    return best;
}"""


# ---------------------------------------------------------------------------
# 主 GUI
# ---------------------------------------------------------------------------
class FanqieGUI:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("番茄作家 MD 批量上传工具")
        self.root.geometry("820x720")
        self.root.minsize(720, 600)

        self.worker = AsyncWorker()
        self.worker.start()

        # 状态
        self.books: list[dict] = []
        self.parsed_chapters: list[tuple] = []
        self.files: list[Path] = []
        self.uploading = False
        self._closing = False
        self._redirector = None
        self._last_publish_cache: dict[str, dict] = {}  # bookId -> {date, time}
        self._platform_chapters_cache: dict[str, list] = {}  # bookId -> [章节列表]
        self._matched_edit: list = []  # 修改模式匹配结果

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
        ttk.Button(frm, text="登录番茄作家", command=self._on_login).pack(
            side="left", padx=6, pady=4)
        self.lbl_auth = ttk.Label(frm, text="")
        self.lbl_auth.pack(side="left", padx=6)
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
        frm = ttk.LabelFrame(self.root, text="章节目录")
        frm.pack(fill="x", **pad)
        self.dir_var = tk.StringVar()
        # 默认使用脚本目录下的 chapters/（不存在则自动创建）
        DEFAULT_CHAPTERS_DIR.mkdir(exist_ok=True)
        self.dir_var.set(str(DEFAULT_CHAPTERS_DIR))
        ttk.Entry(frm, textvariable=self.dir_var, state="readonly").pack(
            side="left", padx=6, pady=4, fill="x", expand=True)
        ttk.Button(frm, text="浏览...", command=self._on_browse_dir).pack(
            side="left", pady=4)
        ttk.Button(frm, text="刷新", command=self._refresh_preview).pack(
            side="left", padx=(4, 6), pady=4)
        self.unique_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, text="自动去重标题", variable=self.unique_var,
            command=self._refresh_preview).pack(side="left", padx=6)

        # --- 4. 发布模式 ---
        frm_mode = ttk.LabelFrame(self.root, text="发布模式")
        frm_mode.pack(fill="x", **pad)

        row_radios = ttk.Frame(frm_mode)
        row_radios.pack(fill="x", padx=6, pady=4)
        self.mode_var = tk.StringVar(value="schedule")
        for text, val in [("存草稿", "draft"), ("立即发布", "publish"),
                          ("定时发布", "schedule"), ("修改", "edit")]:
            ttk.Radiobutton(
                row_radios, text=text, variable=self.mode_var,
                value=val, command=self._on_mode_change).pack(side="left", padx=10)

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
        self.perday_var = tk.IntVar(value=2)
        ttk.Spinbox(
            r1, from_=1, to=20, textvariable=self.perday_var,
            width=4).pack(side="left", padx=4)

        # Row 2: 时间
        r2 = ttk.Frame(self.sched_frame)
        r2.pack(fill="x", padx=6, pady=2)
        ttk.Label(r2, text="发布时间:").pack(side="left")
        self.hour_var = tk.IntVar(value=8)
        ttk.Spinbox(
            r2, from_=0, to=23, textvariable=self.hour_var,
            width=3, format="%02.0f", wrap=True).pack(side="left", padx=4)
        ttk.Label(r2, text="时").pack(side="left")
        self.minute_var = tk.IntVar(value=0)
        ttk.Spinbox(
            r2, from_=0, to=59, textvariable=self.minute_var,
            width=3, format="%02.0f", wrap=True).pack(side="left", padx=4)
        ttk.Label(r2, text="分").pack(side="left")

        # 默认模式为定时发布，显示定时设置面板
        self.sched_frame.pack(fill="x", padx=6, pady=4)

        # 参数变化时刷新预览
        for var in (self.date_var, self.perday_var, self.hour_var, self.minute_var):
            var.trace_add("write", lambda *_: self._refresh_preview())

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
        self.txt_log = scrolledtext.ScrolledText(
            frm, height=8, state="disabled",
            font=("Consolas", 9))
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

        # 启动时自动加载预览
        if self.dir_var.get():
            self.root.after(100, self._refresh_preview)

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
        if AUTH_FILE.exists():
            self.lbl_auth.configure(text="登录状态: 已保存", foreground="green")
        else:
            self.lbl_auth.configure(text="登录状态: 未登录", foreground="red")

    def _on_mode_change(self):
        mode = self.mode_var.get()
        # 切离修改模式时恢复上传按钮
        if mode != "edit" and not self.uploading:
            self.btn_upload.configure(state="normal")
        if mode == "schedule":
            self.sched_frame.pack(fill="x", padx=6, pady=4)
            self.chk_use_ai.pack(side="left", padx=6)
            self._on_book_changed()  # 触发获取上次发布时间
        elif mode == "edit":
            self.sched_frame.pack_forget()
            self.chk_use_ai.pack(side="left", padx=6)
            # 未载入章节列表前禁用上传按钮
            idx = self.cmb_book.current()
            book_id = self.books[idx]["bookId"] if idx >= 0 and self.books else None
            if not (book_id and book_id in self._platform_chapters_cache):
                self.btn_upload.configure(state="disabled")
            self._fetch_platform_chapters_for_edit()
        else:
            self.sched_frame.pack_forget()
            self.chk_use_ai.pack(side="left", padx=6)
        self._refresh_preview()

    def _log(self, msg):
        self.txt_log.configure(state="normal")
        self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.configure(state="disabled")

    def _set_preview(self, text):
        self.txt_preview.configure(state="normal")
        self.txt_preview.delete("1.0", tk.END)
        self.txt_preview.insert("1.0", text)
        self.txt_preview.configure(state="disabled")

    def _set_uploading(self, active):
        self.uploading = active
        state = "disabled" if active else "normal"
        self.btn_upload.configure(
            state=state, text="上传中..." if active else "开始上传")
        # 上传期间禁用所有可能影响状态的控件
        self.btn_books.configure(state=state)
        self.cmb_book.configure(state="disabled" if active else "readonly")
        self.btn_open_manage.configure(state=state)

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
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            return

        book_id = self.books[idx]["bookId"]

        # 修改模式: 走专用的章节列表获取（同时获取上次发布信息）
        if self.mode_var.get() == "edit":
            self.btn_upload.configure(state="disabled")
            self._fetch_platform_chapters_for_edit()
            return

        # 有缓存直接用
        if book_id in self._last_publish_cache:
            self._apply_last_publish(self._last_publish_cache[book_id])
            return

        # 后台获取
        if not AUTH_FILE.exists():
            return

        self.lbl_last_publish.configure(
            text="上次定时发布: 获取中...", foreground="gray")

        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=True)
                    page = await context.new_page()
                    url = CHAPTER_MANAGE_URL.format(book_id=book_id)
                    await page.goto(url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(2000)

                    # 尝试滚动到页面底部加载更多章节
                    await page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1000)

                    result = await page.evaluate(LAST_PUBLISH_JS)
                    await browser.close()

                self._after(0, self._last_publish_fetched, book_id, result)
            except Exception:
                self._after(0, self._last_publish_fetched, book_id, None)

        self.worker.submit(task())

    def _last_publish_fetched(self, book_id, result):
        """后台获取完成，更新缓存和 UI。"""
        if result:
            self._last_publish_cache[book_id] = result
            self._apply_last_publish(result)
        else:
            self.lbl_last_publish.configure(
                text="上次定时发布: 未找到", foreground="gray")

    def _apply_last_publish(self, info):
        """将上次发布信息显示到 UI 并自动建议下一天日期和相同时间。"""
        date_str = info["date"]
        time_str = info["time"]
        chapter = info.get("chapter", "")
        label = f"上次定时发布: {date_str} {time_str}"
        if chapter:
            label += f"  ({chapter})"
        self.lbl_last_publish.configure(text=label, foreground="#d35400")

        # 自动建议: 起始日期 = 上次日期 + 1 天，时间 = 上次相同
        try:
            last_dt = datetime.strptime(date_str, "%Y-%m-%d")
            next_dt = last_dt + timedelta(days=1)
            self.date_var.set(next_dt.strftime("%Y-%m-%d"))
        except ValueError:
            pass

        try:
            parts = time_str.split(":")
            self.hour_var.set(int(parts[0]))
            self.minute_var.set(int(parts[1]))
        except (ValueError, IndexError):
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
                book_id, self._platform_chapters_cache[book_id], None)
            return

        if not AUTH_FILE.exists():
            return

        self.lbl_last_publish.configure(
            text="正在获取平台章节列表...", foreground="gray")

        async def task():
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=True)
                    page = await context.new_page()
                    url = CHAPTER_MANAGE_URL.format(book_id=book_id)
                    await page.goto(url)
                    await page.wait_for_load_state("networkidle")

                    chapters, last_pub = await extract_chapters_from_page(
                        page, book_id)
                    await browser.close()

                self._after(0, self._on_platform_chapters_fetched,
                            book_id, chapters, None, last_pub)
            except Exception as e:
                self._after(0, self._on_platform_chapters_fetched,
                            book_id, [], str(e), None)

        self.worker.submit(task())

    def _on_platform_chapters_fetched(self, book_id, chapters, error,
                                      last_pub=None):
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
        if not self.uploading and self.mode_var.get() == "edit":
            self.btn_upload.configure(state="normal")
        self._refresh_preview()

    # -----------------------------------------------------------------------
    # 登录
    # -----------------------------------------------------------------------
    def _on_login(self):
        self._login_event = threading.Event()

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

                    await save_auth(context)
                    await browser.close()

                self._after(0, self._login_done, None)
            except Exception as e:
                self._after(0, self._login_done, str(e))

        self.worker.submit(task())

    def _show_login_dialog(self):
        # 将主窗口提到最前，确保对话框不被浏览器窗口遮挡
        self.root.lift()
        self.root.attributes("-topmost", True)
        messagebox.showinfo(
            "登录", "请在浏览器中登录番茄作家账号\n完成后点击「确定」保存会话")
        self.root.attributes("-topmost", False)
        self._login_event.set()

    def _login_done(self, error):
        self._refresh_auth_status()
        if error:
            self._log(f"登录失败: {error}")
        else:
            self._log("登录状态已保存。")
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
            try:
                async with async_playwright() as p:
                    browser, context = await create_context(p, headless=True)
                    page = await context.new_page()
                    await page.goto(BOOK_MANAGE_URL)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(3000)
                    books = await page.evaluate(BOOKS_JS)
                    await save_auth(context)
                    await browser.close()
                self._after(0, self._books_fetched, books, None)
            except Exception as e:
                self._after(0, self._books_fetched, [], str(e))

        self.worker.submit(task())

    def _books_fetched(self, books, error):
        self.btn_books.configure(state="normal")
        if error:
            self._log(f"获取失败: {error}")
            return
        self.books = books
        self._last_publish_cache.clear()  # 清空缓存
        if not books:
            self._log("未找到作品，请检查登录状态。")
            return
        display = [
            f"{b['name']}  ({b['chapters']}章, {b['words']}字)"
            for b in books
        ]
        self.cmb_book["values"] = display
        self.cmb_book.current(0)
        self._log(f"找到 {len(books)} 部作品。")
        self._on_book_changed()  # 自动获取首部作品的最后发布时间

    # -----------------------------------------------------------------------
    # 目录选择 + 预览
    # -----------------------------------------------------------------------
    def _on_browse_dir(self):
        d = filedialog.askdirectory(title="选择章节 MD 文件目录")
        if not d:
            return
        self.dir_var.set(d)
        self._refresh_preview()

    def _refresh_preview(self):
        dir_path = self.dir_var.get()
        if not dir_path:
            return
        p = Path(dir_path)
        if not p.is_dir():
            self._set_preview("目录不存在")
            return

        self.files = get_md_files(p)
        if not self.files:
            self._set_preview("目录中没有 .md/.txt 文件")
            return

        self.parsed_chapters = [parse_md_file(f) for f in self.files]
        if self.unique_var.get():
            self.parsed_chapters = deduplicate_titles(self.parsed_chapters)

        mode = self.mode_var.get()

        # 修改模式: 专用预览
        if mode == "edit":
            self._refresh_edit_preview()
            return

        # 计算排期
        schedule = None
        if mode == "schedule":
            try:
                date_str = self.date_var.get()
                datetime.strptime(date_str, "%Y-%m-%d")
                time_str = f"{self.hour_var.get():02d}:{self.minute_var.get():02d}"
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

        mode_labels = {"draft": "存草稿", "publish": "立即发布", "schedule": "定时发布"}
        summary = f"总计: {len(self.files)} 章, {total_words} 字 | 模式: {mode_labels[mode]}"
        if schedule:
            summary += f" | 排期: {date_str} ~ {schedule[-1][0]}"

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
        self._matched_edit = []

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
            for i, (num, title, content) in enumerate(self.parsed_chapters):
                wc = len(strip_md_formatting(content))
                total_words += wc
                num_str = f"第{num}章" if num else "  ?  "
                lines.append(
                    f"  {i+1:3d}. {num_str} {title}  ({wc}字)  [待获取章节列表]")

        summary = f"总计: {len(self.files)} 章, {total_words} 字 | 模式: 修改"
        if platform_chapters:
            summary += f" | 匹配: {matched_count}/{len(self.files)}"

        self._set_preview(summary + "\n" + "-" * 60 + "\n" + "\n".join(lines))
        self.progress["maximum"] = max(matched_count, 1)
        self.progress["value"] = 0
        self.lbl_progress.configure(text=f"0/{matched_count}")

    # -----------------------------------------------------------------------
    # 上传
    # -----------------------------------------------------------------------
    def _on_upload(self):
        if self.uploading:
            return

        # 验证
        if not AUTH_FILE.exists():
            messagebox.showwarning("提示", "请先登录")
            return
        idx = self.cmb_book.current()
        if idx < 0 or not self.books:
            messagebox.showwarning("提示", "请先刷新并选择作品")
            return
        if not self.files or not self.parsed_chapters:
            messagebox.showwarning("提示", "请先选择章节目录")
            return

        mode = self.mode_var.get()
        book_id = self.books[idx]["bookId"]
        book_name = self.books[idx]["name"]

        # 修改模式
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
                time_str = f"{self.hour_var.get():02d}:{self.minute_var.get():02d}"
                per_day = max(1, self.perday_var.get())
            except tk.TclError:
                messagebox.showerror("参数错误", "请输入有效的时间和每天章数")
                return
            schedule = compute_schedule(
                len(self.parsed_chapters), date_str, time_str, per_day)

        # 确认
        count = len(self.parsed_chapters)
        mode_labels = {"draft": "存草稿", "publish": "立即发布", "schedule": "定时发布"}
        msg = f"即将上传 {count} 章到「{book_name}」\n模式: {mode_labels[mode]}"
        if schedule:
            msg += f"\n排期: {schedule[0][0]} ~ {schedule[-1][0]}"
        if not messagebox.askyesno("确认上传", msg):
            return

        # 开始
        self._set_uploading(True)
        self.progress["value"] = 0

        # 安装 stdout 重定向
        self._redirector = StdoutRedirector(self.txt_log, self.root)
        sys.stdout = self._redirector

        cfg = load_config()
        delay = cfg.get("delay_between_chapters", 3)

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
                        await wait_for_editor_ready(page, timeout=20000)
                    except PWTimeout:
                        print("无法进入编辑器，请检查 Book ID 和登录状态。")
                        await browser.close()
                        self._after(0, self._upload_done, 0, 0)
                        return

                    success = 0
                    failed = 0
                    total = len(files)

                    for i in range(total):
                        chapter_num, title, content = parsed[i]
                        num_str = f"第{chapter_num}章 " if chapter_num else ""
                        sched_info = ""
                        if schedule:
                            sched_info = f" -> {schedule[i][0]} {schedule[i][1]}"
                        print(f"\n[{i+1}/{total}] {num_str}{title}{sched_info}")

                        try:
                            if i > 0:
                                await page.goto(url)
                                await wait_for_editor_ready(page)

                            await fill_chapter(page, chapter_num, title, content)

                            if schedule:
                                d, t = schedule[i]
                                await publish_scheduled(page, d, t, use_ai=use_ai)
                                print(f"  -> 定时发布 {d} {t}")
                            elif mode == "publish":
                                await _navigate_to_publish_settings(page, use_ai=use_ai)
                                btn = page.locator("button", has_text="确认发布")
                                if await btn.count() == 0:
                                    raise RuntimeError("未找到确认发布按钮")
                                await btn.first.click()
                                await page.wait_for_timeout(2000)
                                print("  -> 已发布")
                            else:
                                await save_draft(page)
                                print("  -> 已存草稿")

                            success += 1
                        except Exception as e:
                            print(f"  !! 失败: {e}")
                            failed += 1
                            try:
                                err = SCRIPT_DIR / f"error_{i}_{files[i].stem}.png"
                                await page.screenshot(path=str(err))
                                print(f"  截图: {err}")
                            except Exception:
                                pass

                        self._after(0, self._update_progress, i + 1, total)

                        if i < total - 1 and delay > 0:
                            await page.wait_for_timeout(delay * 1000)

                    await save_auth(context)
                    await browser.close()

                print(f"\n{'='*40}")
                print(f"  上传完成! 成功: {success}  失败: {failed}")
                print(f"{'='*40}")

                self._after(0, self._upload_done, success, failed)

            except Exception as e:
                print(f"\n!! 上传异常: {e}")
                self._after(0, self._upload_done, -1, -1)

        self.worker.submit(task())

    def _update_progress(self, current, total):
        self.progress["value"] = current
        self.lbl_progress.configure(text=f"{current}/{total}")

    def _upload_done(self, success, failed):
        if self._redirector:
            sys.stdout = self._redirector._original or sys.__stdout__
        self._redirector = None
        self._set_uploading(False)
        self._last_publish_cache.clear()  # 上传后清除缓存，下次获取最新数据
        self._platform_chapters_cache.clear()
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

        msg = f"即将修改「{book_name}」的 {count} 个章节\n模式: 修改已有章节（存草稿）"
        if not messagebox.askyesno("确认修改", msg):
            return

        self._set_uploading(True)
        self.progress["value"] = 0
        self.progress["maximum"] = count

        self._redirector = StdoutRedirector(self.txt_log, self.root)
        sys.stdout = self._redirector

        cfg = load_config()
        delay = cfg.get("delay_between_chapters", 3)
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
                        print(f"\n[{i+1}/{total}] 修改第{ch_num}章 {title}")

                        try:
                            edit_url = plat_ch.get("editUrl")
                            if not edit_url:
                                print("  !! 无法获取编辑链接，跳过")
                                failed += 1
                                self._after(0, self._update_progress, i + 1, total)
                                continue

                            if edit_url.startswith("/"):
                                edit_url = BASE_URL + edit_url

                            await page.goto(edit_url)
                            await wait_for_editor_ready(page)
                            await dismiss_edit_hint(page)

                            await clear_editor(page)
                            await fill_chapter(page, str(ch_num), title, content)

                            # 走发布流程保存修改
                            await _navigate_to_publish_settings(page, use_ai=use_ai)
                            confirm_btn = page.locator("button", has_text="确认发布")
                            if await confirm_btn.count() == 0:
                                raise RuntimeError("未找到确认发布按钮")
                            await confirm_btn.first.click()
                            await page.wait_for_timeout(2000)
                            print("  -> 已保存修改")
                            success += 1

                        except Exception as e:
                            print(f"  !! 失败: {e}")
                            failed += 1
                            try:
                                err = SCRIPT_DIR / f"error_edit_{ch_num}.png"
                                await page.screenshot(path=str(err))
                                print(f"  截图: {err}")
                            except Exception:
                                pass

                        self._after(0, self._update_progress, i + 1, total)

                        if i < total - 1 and delay > 0:
                            await page.wait_for_timeout(delay * 1000)

                    await save_auth(context)
                    await browser.close()

                print(f"\n{'='*40}")
                print(f"  修改完成! 成功: {success}  失败: {failed}")
                print(f"{'='*40}")

                self._after(0, self._upload_done, success, failed)

            except Exception as e:
                print(f"\n!! 修改异常: {e}")
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
        # 恢复 stdout 以防重定向仍在生效
        if self._redirector:
            sys.stdout = self._redirector._original or sys.__stdout__
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
    logging.basicConfig(filename=str(LOG_FILE), level=logging.ERROR)
    try:
        app = FanqieGUI()
        app.run()
    except Exception:
        logging.exception("启动异常")
        raise
