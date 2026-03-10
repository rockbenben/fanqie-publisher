#!/usr/bin/env python3
"""
番茄作家 MD 批量上传工具

将本地 Markdown 文件批量上传到番茄作家平台作为小说章节。

用法:
    python fanqie_upload.py login                              登录并保存会话
    python fanqie_upload.py books                              列出你的作品
    python fanqie_upload.py upload ./chapters --book-id ID     批量上传章节(存草稿)
    python fanqie_upload.py upload ./chapters --book-id ID --publish  批量上传并发布
    python fanqie_upload.py upload ./chapters --book-id ID --schedule 2026-03-14 --per-day 3
                                                               定时发布(每天3章)

MD 文件格式:
    文件名: 001_章节标题.md  或  第1章_标题.md  或  任意名称.md
    内容: 纯文本或 Markdown，第一个 # 标题可作为章节标题
    排序: 按文件名自然排序决定上传顺序
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("请先安装依赖:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_URL = "https://fanqienovel.com"
ZONE_URL = f"{BASE_URL}/writer/zone/"

# 持久化文件（与脚本同目录）
SCRIPT_DIR = Path(__file__).parent
AUTH_FILE = SCRIPT_DIR / ".auth_state.json"
GUI_STATE_FILE = SCRIPT_DIR / ".gui_state.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

# 页面路径
BOOK_MANAGE_URL = f"{BASE_URL}/main/writer/book-manage"
NEW_CHAPTER_URL_TPL = BASE_URL + "/main/writer/{book_id}/publish/?enter_from=newchapter_1"
CHAPTER_MANAGE_URL_TPL = BASE_URL + "/main/writer/chapter-manage/{book_id}"

# 默认配置
DEFAULT_CONFIG = {
    "delay_between_chapters": 3,   # 章节之间等待秒数
    "headless": False,             # 是否无头模式
    "max_retries": 2,              # 单章失败最大重试次数
    "default_mode": "schedule",    # GUI 默认发布模式
    "default_per_day": 2,          # GUI 默认每天章数
    "default_time": "08:00",       # GUI 默认发布时间（支持逗号分隔多时间）
    "browser_timeout": 15000,      # 浏览器操作超时 (ms)
}

# 平台修饰键 (macOS = Meta/Cmd, 其他 = Control)
_MOD_KEY = "Meta" if sys.platform == "darwin" else "Control"


class DailyLimitReached(RuntimeError):
    """当日发布字数已达平台上限，无法继续发布。"""


async def _check_daily_limit(page):
    """检测平台"当日发布字数上限"提示，若存在则抛出 DailyLimitReached。"""
    try:
        tip = page.locator("text=已到达当日发布字数上限")
        if await tip.count() > 0:
            raise DailyLimitReached("已到达当日发布字数上限，无法继续发布")
    except DailyLimitReached:
        raise
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, ValueError):
            print(f"⚠ config.json 格式错误，使用默认配置")
    return cfg


# ---------------------------------------------------------------------------
# MD 文件解析
# ---------------------------------------------------------------------------
def natural_sort_key(path: Path):
    """自然排序: 001 < 2 < 10"""
    return [
        int(s) if s.isdigit() else s.lower()
        for s in re.split(r"(\d+)", path.name)
    ]


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
               "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
               "十": 10, "百": 100, "千": 1000}


def _cn_to_int(cn: str) -> int:
    """中文数字转阿拉伯数字: 十六->16, 一百二十三->123, 二十->20"""
    result, current = 0, 0
    for ch in cn:
        val = _CN_DIGITS.get(ch)
        if val is None:
            return 0
        if val >= 10:  # 十百千
            if current == 0:
                current = 1
            result += current * val
            current = 0
        else:
            current = val
    result += current
    return result


def _extract_chapter_num(text: str) -> str | None:
    """
    从文本中提取章节号（纯数字字符串）。

    支持格式:
        "001_标题"           -> "001"
        "第27章_标题"        -> "27"
        "第 27 章 标题"      -> "27"
        "第十六章 发布会"    -> "16"
        "第一百二十三章 标题" -> "123"
        "chapter-027"        -> "027"
        "Chapter 3 - Title"  -> "3"
    """
    # 1) 纯数字开头: 001_xxx, 027 xxx
    m = re.match(r"^(\d+)", text)
    if m:
        return m.group(1)
    # 2) 第X章 - 阿拉伯数字: 第27章, 第 27 章
    m = re.match(r"^第\s*(\d+)\s*章", text)
    if m:
        return m.group(1)
    # 3) 第X章 - 中文数字: 第十六章, 第一百二十三章
    m = re.match(r"^第([零〇一二两三四五六七八九十百千]+)章", text)
    if m:
        num = _cn_to_int(m.group(1))
        if num > 0:
            return str(num)
    # 4) chapter-027, Chapter 3
    m = re.match(r"^chapter[_\-\s]*(\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _strip_chapter_prefix(text: str) -> str:
    """
    去掉标题中的章节号前缀，只保留标题文字。

    "第 27 章 重新开始"  -> "重新开始"
    "第27章重新开始"      -> "重新开始"
    "001 新的旅程"        -> "新的旅程"
    "chapter-3 出发"      -> "出发"
    "Chapter 3 - Hello"  -> "Hello"
    """
    original = text.strip()
    patterns = [
        r"^第\s*\d+\s*章[\s:：_\-]*",             # 第 27 章 / 第27章
        r"^第[零〇一二两三四五六七八九十百千]+章[\s:：_\-]*",  # 第十六章 / 第一百二十三章
        r"^\d+[\s_\-]+",                           # 001_xxx / 001 xxx（必须有分隔符）
        r"^chapter[\s_\-]*\d+[\s_\-]*",            # chapter-3 / Chapter 3 -
    ]
    for pat in patterns:
        cleaned = re.sub(pat, "", original, flags=re.IGNORECASE).strip()
        if cleaned and cleaned != original:
            return cleaned
    return original


def parse_md_file(fp: Path) -> tuple:
    """
    解析 MD 文件，返回 (chapter_num, title, content)。

    章节号提取优先级: 文件名 > 标题中 "第X章"
    标题提取优先级:  第一个 # 标题(去前缀) > 文件名(去前缀)

    支持的文件名:
        001_标题.md / 第27章.md / chapter-027.md / 第 3 章 出发.md

    支持的 # 标题:
        # 第 27 章 重新开始 / # 重新开始 / # 001 新的旅程
    """
    try:
        text = fp.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError:
        text = fp.read_text(encoding="gbk", errors="replace").strip()
        if "\ufffd" in text:
            print(f"  ⚠ {fp.name}: 编码异常，部分内容可能损坏")
    lines = text.split("\n")

    heading = None      # 原始 # 标题
    content_start = 0

    # 从第一个 # heading 提取标题
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            content_start = i + 1
            break

    content = "\n".join(lines[content_start:]).strip()

    # ---- 提取章节号 ----
    # 优先从文件名提取
    chapter_num = _extract_chapter_num(fp.stem)
    # 其次从 heading 提取
    if chapter_num is None and heading:
        chapter_num = _extract_chapter_num(heading)

    # ---- 提取标题 ----
    if heading:
        title = _strip_chapter_prefix(heading)
    else:
        title = _strip_chapter_prefix(fp.stem)

    # 兜底
    if not title:
        title = fp.stem

    return chapter_num, title, content


def get_md_files(directory: Path) -> list:
    files = [f for f in directory.iterdir() if f.suffix.lower() in (".md", ".txt")]
    files.sort(key=natural_sort_key)
    return files


def strip_md_formatting(text: str) -> str:
    """去掉 Markdown 格式标记，保留纯文本段落。"""
    # 移除图片
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    # 移除链接，保留文字
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # 移除加粗/斜体
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text)
    # 移除标题标记
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 移除引用标记
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 移除分隔线
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # 移除代码块标记
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def deduplicate_titles(
    parsed_chapters: list[tuple[str | None, str, str]],
) -> list[tuple[str | None, str, str]]:
    """
    检测并处理重复标题。

    对于重复的标题，追加章节号后缀使其唯一:
      "选择" (第33章) -> "选择（33）"
      "选择" (第39章) -> "选择（39）"

    如果没有章节号，则追加序号:
      "选择" (无章节号, 第2个) -> "选择（2）"

    不重复的标题不做任何修改。
    """
    # 统计标题出现次数
    title_counts = Counter(title for _, title, _ in parsed_chapters)
    dup_titles = {t for t, c in title_counts.items() if c > 1}

    if not dup_titles:
        return parsed_chapters

    # 给重复的标题加后缀
    seen: dict[str, int] = {}
    result = []
    for chapter_num, title, content in parsed_chapters:
        if title in dup_titles:
            suffix = chapter_num if chapter_num else str(seen.get(title, 1))
            new_title = f"{title}（{suffix}）"
            seen[title] = seen.get(title, 1) + 1
            result.append((chapter_num, new_title, content))
        else:
            result.append((chapter_num, title, content))
    return result


# ---------------------------------------------------------------------------
# 浏览器操作
# ---------------------------------------------------------------------------
async def create_context(p, headless=False):
    """创建浏览器上下文，如有已保存的登录状态则加载。"""
    browser = await p.chromium.launch(headless=headless)
    try:
        if AUTH_FILE.exists():
            context = await browser.new_context(storage_state=str(AUTH_FILE))
        else:
            context = await browser.new_context()
    except Exception:
        await browser.close()
        raise
    # 授予剪贴板权限，用于可靠的粘贴操作
    await context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=BASE_URL
    )
    return browser, context


async def save_auth(context):
    """保存当前登录状态。"""
    await context.storage_state(path=str(AUTH_FILE))


async def dismiss_overlays(page):
    """
    关闭可能遮挡按钮的弹窗:
      1. "提示" 草稿恢复弹窗 -> 点 "放弃"
      2. React Tour 新手引导  -> 用 JS 直接移除
    注意: fill_chapter 已改用 page.evaluate 操作 DOM，不受弹窗影响。
          此函数主要确保 "存草稿"/"下一步" 等按钮可以被 Playwright 点击。
    """
    await page.wait_for_timeout(800)

    # 1. 草稿恢复弹窗: "有刚刚更新的草稿，是否继续编辑？" -> 放弃
    try:
        draft_hint = page.locator("text=是否继续编辑")
        if await draft_hint.count() > 0:
            abandon_btn = page.locator("button", has_text="放弃")
            if await abandon_btn.count() > 0:
                await abandon_btn.first.click()
                await page.wait_for_timeout(800)
    except Exception:
        pass

    # 2. React Tour 新手引导 -> 直接用 JS 移除 DOM 节点（比逐步点击更可靠）
    try:
        await page.evaluate("""() => {
            const tour = document.getElementById('___reactour');
            if (tour) tour.remove();
            // 同时移除可能的遮罩层
            const masks = document.querySelectorAll('[class*="reactour"], [class*="mask"]');
            for (const m of masks) {
                if (m.style && (m.style.position === 'fixed' || m.style.position === 'absolute')) {
                    m.remove();
                }
            }
        }""")
    except Exception:
        pass


async def wait_for_editor_ready(page, timeout=15000):
    """等待章节编辑器加载完成。"""
    await page.wait_for_load_state("networkidle", timeout=timeout)
    # 等待 ProseMirror 编辑器出现
    await page.wait_for_selector(".ProseMirror", timeout=timeout)
    # 等待标题输入框出现
    await page.wait_for_selector("input[placeholder='请输入标题']", timeout=timeout)
    await page.wait_for_timeout(500)
    # 关闭弹窗/引导层
    await dismiss_overlays(page)


async def _get_word_count(page) -> int:
    """从页面顶部获取正文字数，返回整数。"""
    try:
        el = page.locator("text=正文字数")
        if await el.count() > 0:
            txt = await el.text_content()
            m = re.search(r"(\d+)", txt)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0


async def fill_chapter(page, chapter_num: str | None, title: str, content: str):
    """
    在编辑器页面填入章节内容。

    全部通过 page.evaluate 直接操作 DOM，不使用 Playwright 的
    locator.click()/fill()，这样即使有弹窗/引导层遮挡也不会失败。
    """
    plain_content = strip_md_formatting(content)

    await page.evaluate(
        """([chapterNum, title, content]) => {
            const nativeSetter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;

            // 1. 填写章节号
            if (chapterNum) {
                const inputs = document.querySelectorAll('input');
                for (const inp of inputs) {
                    if (inp.type === 'text'
                        && inp.placeholder !== '请输入标题'
                        && inp.offsetParent !== null) {
                        nativeSetter.call(inp, chapterNum);
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        break;
                    }
                }
            }

            // 2. 填写标题
            const titleInput = document.querySelector(
                'input[placeholder="请输入标题"]'
            );
            if (titleInput) {
                nativeSetter.call(titleInput, title);
                titleInput.dispatchEvent(new Event('input', { bubbles: true }));
                titleInput.dispatchEvent(new Event('change', { bubbles: true }));
            }

            // 3. 粘贴正文 (ClipboardEvent -> ProseMirror)
            const editor = document.querySelector('.ProseMirror');
            if (editor) {
                editor.focus();
                const dt = new DataTransfer();
                dt.setData('text/plain', content);
                const evt = new ClipboardEvent('paste', {
                    clipboardData: dt,
                    bubbles: true,
                    cancelable: true,
                });
                editor.dispatchEvent(evt);
            }
        }""",
        [chapter_num or "", title, plain_content],
    )
    # 轮询等待正文写入完成（最多 5 秒）
    wc = 0
    for _ in range(10):
        await page.wait_for_timeout(500)
        wc = await _get_word_count(page)
        if wc > 0:
            break
    if wc > 0:
        print(f"    正文字数 {wc}")
    else:
        raise RuntimeError("正文粘贴失败 (字数=0)，请重试")


async def save_draft(page):
    """点击存草稿按钮并等待保存完成。"""
    save_btn = page.locator("button", has_text="存草稿")
    if await save_btn.count() == 0:
        raise RuntimeError("未找到存草稿按钮")
    await save_btn.first.click()
    # 等待 "已保存" 出现
    try:
        await page.wait_for_selector("text=已保存", timeout=10000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(1000)


async def dismiss_edit_hint(page):
    """关闭编辑已发布章节时的提示弹窗: '请在发布时间前30分钟提交修改内容'。"""
    try:
        hint = page.locator("text=请在发布时间前30分钟提交修改内容")
        if await hint.count() > 0:
            btn = page.locator("button", has_text="我知道了")
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(800)
    except Exception:
        pass


async def clear_editor(page):
    """清空编辑器中的标题和正文内容（修改模式用）。"""
    await page.evaluate("""() => {
        const nativeSetter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype, 'value'
        ).set;

        // 清空标题
        const titleInput = document.querySelector('input[placeholder="请输入标题"]');
        if (titleInput) {
            nativeSetter.call(titleInput, '');
            titleInput.dispatchEvent(new Event('input', { bubbles: true }));
            titleInput.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // 清空章节号
        const inputs = document.querySelectorAll('input');
        for (const inp of inputs) {
            if (inp.type === 'text'
                && inp.placeholder !== '请输入标题'
                && inp.offsetParent !== null) {
                nativeSetter.call(inp, '');
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
                break;
            }
        }

        // 选中 ProseMirror 编辑器全部内容
        const editor = document.querySelector('.ProseMirror');
        if (editor) {
            editor.focus();
        }
    }""")
    # 全选并删除正文
    await page.keyboard.press(f"{_MOD_KEY}+a")
    await page.wait_for_timeout(200)
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(500)


# ---------------------------------------------------------------------------
# JS: 获取作品列表（CLI 和 GUI 共用）
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
        const signMatch = text.match(/(已签约|未签约)/);
        results.push({
            bookId, name,
            chapters: chapterMatch ? chapterMatch[1] : '?',
            words: wordMatch ? wordMatch[1] + '万' : '?',
            status: (statusMatch ? statusMatch[1] : '') +
                    (signMatch ? ' · ' + signMatch[1] : ''),
        });
    }
    return results;
}"""


# ---------------------------------------------------------------------------
# JS: 从章节管理页提取最新一条发布时间（仅当前页，不翻页）
# ---------------------------------------------------------------------------
LAST_PUBLISH_JS = r"""() => {
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
# 章节列表提取（修改模式用）— 单次 JS 调用完成全部翻页
# ---------------------------------------------------------------------------
_EXTRACT_ALL_JS = r"""async () => {
    // 等待表格出现
    const t0 = Date.now();
    while (!document.querySelector('tr td')) {
        if (Date.now() - t0 > 10000) break;
        await new Promise(r => requestAnimationFrame(r));
    }

    const allChapters = [];
    const seenKeys = new Set();
    let totalPages = 0;
    let pageCount = 0;

    // 同时提取最新发布时间
    const dateRe = /(\d{4}[-\/]\d{2}[-\/]\d{2})\s+(\d{2}:\d{2})/;
    let lastPub = null;
    let lastPubKey = '';

    // 获取总页数
    for (const li of document.querySelectorAll('li.arco-pagination-item')) {
        const n = parseInt(li.textContent);
        if (!isNaN(n) && n > totalPages) totalPages = n;
    }

    const MAX_TIME = 120000;
    const start = Date.now();

    for (let i = 0; i < 500 && Date.now() - start < MAX_TIME; i++) {
        let newCount = 0;
        for (const row of document.querySelectorAll('tr')) {
            const cells = row.querySelectorAll('td');
            if (cells.length < 2) continue;
            const title = cells[0].textContent.trim();
            if (!title) continue;

            // 编辑链接
            let editUrl = null;
            for (const a of row.querySelectorAll('a')) {
                const href = a.getAttribute('href') || '';
                if (/\/publish\//.test(href) || /chapter_id/.test(href)) {
                    editUrl = href; break;
                }
                const text = a.textContent.trim();
                if (text === '编辑' || text === '修改') {
                    editUrl = href; break;
                }
            }

            // 章节号
            let chapterNum = null;
            let m = title.match(/^第\s*(\d+)\s*章/);
            if (m) chapterNum = parseInt(m[1], 10);
            else { m = title.match(/^(\d+)/); if (m) chapterNum = parseInt(m[1], 10); }

            const key = chapterNum + '|' + title;
            if (seenKeys.has(key)) continue;
            seenKeys.add(key);

            allChapters.push({ title, chapterNum, editUrl, rowIndex: allChapters.length });
            newCount++;

            // 发布日期
            const dm = row.textContent.match(dateRe);
            if (dm) {
                const d = dm[1].replace(/\//g, '-');
                const t = dm[2];
                const pk = d + ' ' + t;
                if (pk > lastPubKey) {
                    lastPub = { date: d, time: t, chapter: title };
                    lastPubKey = pk;
                }
            }
        }

        pageCount++;
        if (newCount === 0 && pageCount > 1) break;

        // 下一页
        let nextBtn = document.querySelector(
            'li.arco-pagination-item-next:not(.arco-pagination-item-disabled)');
        if (!nextBtn) {
            nextBtn = document.querySelector(
                "button[aria-label='next'], .next-page");
            if (nextBtn && (nextBtn.disabled
                || nextBtn.classList.contains('disabled'))) nextBtn = null;
        }
        if (!nextBtn) break;

        const firstTitle = document.querySelector('tr td')?.textContent?.trim() || '';
        nextBtn.click();

        // RAF 轮询等待表格变化（~60fps, 零 IPC 开销）
        await new Promise(resolve => {
            const deadline = Date.now() + 8000;
            (function check() {
                const c = document.querySelector('tr td')?.textContent?.trim() || '';
                if ((c && c !== firstTitle) || Date.now() > deadline) {
                    resolve(); return;
                }
                requestAnimationFrame(check);
            })();
        });
    }

    return { chapters: allChapters, totalPages, pageCount, lastPublish: lastPub };
}"""


async def extract_chapters_from_page(
    page, book_id: str = "",
) -> tuple[list[dict], dict | None]:
    """从章节管理页提取全部章节列表（单次 JS 调用完成全部翻页）。

    返回 (chapters, last_publish_info)。
    last_publish_info: {date, time, chapter} 或 None。
    """
    result = await page.evaluate(_EXTRACT_ALL_JS)
    chapters = result.get("chapters", [])
    total_pages = result.get("totalPages", 0)
    page_count = result.get("pageCount", 0)
    last_pub = result.get("lastPublish")

    if total_pages:
        print(f"  共 {page_count}/{total_pages} 页, {len(chapters)} 个章节")
    elif chapters:
        print(f"  共 {page_count} 页, {len(chapters)} 个章节")

    return chapters, last_pub


def match_chapters(
    local_parsed: list[tuple],
    platform_chapters: list[dict],
) -> tuple[list, list]:
    """
    按章节号匹配本地文件与平台章节。

    返回: (matched, unmatched_local)
      matched: [(local_idx, platform_ch, int_num, title, content), ...]
      unmatched_local: [(local_idx, chapter_num, title), ...]
    """
    # 平台章节按 chapterNum(int) 建字典
    platform_map: dict[int, dict] = {}
    for ch in platform_chapters:
        num = ch.get("chapterNum")
        if num is not None and num not in platform_map:
            platform_map[num] = ch

    matched = []
    unmatched = []
    for i, (num, title, content) in enumerate(local_parsed):
        int_num = int(num) if num else None
        if int_num and int_num in platform_map:
            matched.append((i, platform_map[int_num], int_num, title, content))
        else:
            unmatched.append((i, num, title))
    return matched, unmatched


async def click_next_step(page):
    """点击下一步按钮（进入发布流程）。"""
    # 精确定位发布按钮（class 含 publish-button），避开 React Tour 引导中的同名按钮
    next_btn = page.locator("button.auto-editor-next")
    if await next_btn.count() > 0:
        await next_btn.click()
    else:
        # 兜底：排除 React Tour 中的按钮
        next_btn = page.locator("button", has_text="下一步").locator(
            "visible=true"
        ).first
        await next_btn.click()
    await page.wait_for_timeout(2000)


# ---------------------------------------------------------------------------
# 定时发布
# ---------------------------------------------------------------------------
def _validate_times(raw: str) -> list[str]:
    """解析、校验、排序、去重时间字符串。

    输入: 逗号分隔的时间 (如 "20:00, 08:00, 12:00")
    输出: 合法的 HH:MM 列表, 已排序去重 (如 ["08:00", "12:00", "20:00"])
    不合法的条目静默丢弃。
    """
    result = []
    for t in raw.split(","):
        t = t.strip()
        if not re.match(r"^\d{2}:\d{2}$", t):
            continue
        h, m = int(t[:2]), int(t[3:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            result.append(t)
    # 字符串排序对 HH:MM 格式等同时间排序; dict.fromkeys 保序去重
    return list(dict.fromkeys(sorted(result)))


def compute_schedule(
    file_count: int, start_date: str, pub_time: str, per_day: int
) -> list[tuple[str, str]]:
    """
    计算每章的定时发布日期和时间。

    pub_time 支持逗号分隔的多个时间（如 "08:00,12:00,20:00"），
    每天内的章节按顺序使用各时间点。

    规则:
      - 时间点数量 > per_day 时, 以时间点数量为准
      - 时间点不足时, 从末尾 +1 分钟补齐, 最多到 23:59
      - 超过 23:59 的槽位复用 23:59

    返回: [(date_str, time_str), ...] 长度等于 file_count
    """
    per_day = max(1, per_day)
    base = datetime.strptime(start_date, "%Y-%m-%d")
    times = _validate_times(pub_time)
    if not times:
        times = ["08:00"]
    # 时间点数量 > per_day 时，以时间点为准
    effective = max(per_day, len(times))
    # 时间点不足时，从最后一个时间点起每隔 1 分钟补齐，上限 23:59
    if len(times) < effective:
        last = datetime.strptime(times[-1], "%H:%M")
        cap = datetime.strptime("23:59", "%H:%M")
        while len(times) < effective:
            nxt = last + timedelta(minutes=1)
            if nxt > cap:
                nxt = cap            # 跨午夜: 使用相同时间
            last = nxt
            times.append(nxt.strftime("%H:%M"))
    schedule = []
    for i in range(file_count):
        day_offset = i // effective
        d = base + timedelta(days=day_offset)
        slot = i % effective
        t = times[slot]
        schedule.append((d.strftime("%Y-%m-%d"), t))
    return schedule


async def _navigate_to_publish_settings(page, *, use_ai: bool = False):
    """
    从编辑器完整走到"发布设置"对话框。

    点击"下一步"后可能出现两种流程:
      A) 直接弹出对话框序列（常见）:
         发布提示(错别字确认) -> 是否进行内容风险检测 -> 发布设置
      B) 先打开右侧智能纠错面板:
         纠错面板 -> 忽略全部 -> 再次下一步 -> 对话框序列

    本函数统一处理两种情况。
    """
    # --- Step 1: 点击"下一步" ---
    await click_next_step(page)

    # --- Step 2: 循环处理所有可能出现的弹窗/面板 ---
    for _ in range(10):
        # 平台当日字数上限检测
        await _check_daily_limit(page)

        # 已经到达发布设置?
        if await page.locator("text=发布设置").count() > 0:
            await _apply_publish_options(page, use_ai=use_ai)
            return

        # 纠错面板: 如果出现"忽略全部"按钮 -> 点击它，再点"下一步"
        try:
            ignore_btn = page.locator("button", has_text="忽略全部")
            if await ignore_btn.count() > 0 and await ignore_btn.first.is_visible():
                await ignore_btn.first.click()
                await page.wait_for_timeout(800)
                await click_next_step(page)
                await page.wait_for_timeout(1500)
                continue
        except Exception:
            pass

        # 错别字确认: "检测到你还有错别字未修改，是否确定提交?"
        if await page.locator("text=是否确定提交").count() > 0:
            submit_btn = page.locator("button", has_text="提交")
            if await submit_btn.count() > 0:
                await submit_btn.first.click()
                await page.wait_for_timeout(1000)
                continue

        # 内容风险检测: "是否进行内容风险检测?" -> 取消跳过
        if await page.locator("text=是否进行内容风险检测").count() > 0:
            cancel_btn = page.locator("button", has_text="取消")
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
                await page.wait_for_timeout(1000)
                continue

        # 还没匹配到任何已知状态，等一下再检查
        await page.wait_for_timeout(1000)

    # 兜底: 等发布设置出现
    await page.wait_for_selector("text=发布设置", timeout=10000)

    # --- 到达发布设置后，应用选项 ---
    await _apply_publish_options(page, use_ai=use_ai)


async def _apply_publish_options(page, *, use_ai: bool = False):
    """在发布设置对话框中，设置各选项。"""
    # 是否使用AI
    target = "否" if not use_ai else "是"
    await page.evaluate("""(target) => {
        const labels = document.querySelectorAll('label, span');
        for (const el of labels) {
            const text = el.textContent.trim();
            if (text === target) {
                let parent = el;
                for (let i = 0; i < 6; i++) {
                    if (!parent.parentElement) break;
                    parent = parent.parentElement;
                    if (parent.textContent.includes('是否使用AI')) {
                        const radio = el.querySelector('input[type="radio"]');
                        if (radio) { radio.click(); return; }
                        el.click();
                        return;
                    }
                }
            }
        }
    }""", target)
    await page.wait_for_timeout(500)


async def publish_scheduled(page, date_str: str, time_str: str, *, use_ai: bool = False):
    """
    完整的定时发布流程:
    1. 通过纠错面板和弹窗走到"发布设置"对话框
    2. 开启定时发布开关
    3. 设置日期和时间（Arco DatePicker/TimePicker）
    4. 点击确认发布
    """
    # 1. 走完纠错流程，到达发布设置对话框
    await _navigate_to_publish_settings(page, use_ai=use_ai)

    # 2. 开启定时发布 (Arco Switch)
    #    精确定位: 找到"定时发布"文字旁边的 switch，避免点到"是否使用AI"等其他开关
    switched = await page.evaluate("""() => {
        // 找到包含"定时发布"文字的元素
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null
        );
        while (walker.nextNode()) {
            if (walker.currentNode.textContent.includes('定时发布')) {
                // 从该文本节点向上找共同父容器，再在其中找 switch
                let parent = walker.currentNode.parentElement;
                for (let i = 0; i < 5; i++) {
                    if (!parent) break;
                    const sw = parent.querySelector('button[role="switch"]');
                    if (sw) {
                        if (sw.getAttribute('aria-checked') !== 'true') {
                            sw.click();
                            return 'clicked';
                        }
                        return 'already_on';
                    }
                    parent = parent.parentElement;
                }
            }
        }
        // 兜底: 点击第一个 switch
        const sw = document.querySelector('button[role="switch"]');
        if (sw && sw.getAttribute('aria-checked') !== 'true') {
            sw.click();
            return 'clicked_fallback';
        }
        return 'not_found';
    }""")
    print(f"    定时发布开关: {switched}")
    # 等待日期输入框出现
    try:
        await page.wait_for_selector("input[placeholder='请选择日期']", timeout=5000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(300)

    # 3. 填写日期 (Arco DatePicker)
    #    键盘方式: 点击输入框 -> 全选 -> 输入日期 -> Enter 确认
    date_input = page.locator("input[placeholder='请选择日期']")
    if await date_input.count() == 0:
        print("  ⚠ 未找到日期输入框，定时发布日期可能不正确")
    else:
        await date_input.click()
        await page.wait_for_timeout(300)
        await page.keyboard.press(f"{_MOD_KEY}+a")
        await page.keyboard.type(date_str, delay=50)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)
        # Escape 关闭可能残留的日期选择下拉面板
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

    # 4. 填写时间 (Arco TimePicker)
    time_input = page.locator("input[placeholder='请选择时间']")
    if await time_input.count() == 0:
        print("  ⚠ 未找到时间输入框，定时发布时间可能不正确")
    else:
        await time_input.click()
        await page.wait_for_timeout(300)
        await page.keyboard.press(f"{_MOD_KEY}+a")
        await page.keyboard.type(time_str, delay=50)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)
        # Escape 关闭可能残留的时间选择下拉面板
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

    # 5. 确认发布
    await _check_daily_limit(page)
    confirm_btn = page.locator("button", has_text="确认发布")
    if await confirm_btn.count() == 0:
        raise RuntimeError("未找到确认发布按钮")
    await confirm_btn.first.click()
    await page.wait_for_timeout(2000)
    await _check_daily_limit(page)


# ---------------------------------------------------------------------------
# 命令: login
# ---------------------------------------------------------------------------
async def cmd_login():
    print("正在打开浏览器，请在网页中完成登录...")
    async with async_playwright() as p:
        browser, context = await create_context(p, headless=False)
        page = await context.new_page()
        await page.goto(ZONE_URL)
        await page.wait_for_load_state("networkidle")

        print()
        print("=" * 50)
        print("  请在浏览器中登录番茄作家账号")
        print("  登录成功后回到此处按 Enter 保存会话")
        print("=" * 50)
        await asyncio.get_running_loop().run_in_executor(None, input)

        await save_auth(context)
        await browser.close()
        print("登录状态已保存。")


# ---------------------------------------------------------------------------
# 命令: books
# ---------------------------------------------------------------------------
async def cmd_books():
    if not AUTH_FILE.exists():
        print("请先运行 login 命令登录。")
        return

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=True)
        page = await context.new_page()

        await page.goto(BOOK_MANAGE_URL)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

        books = await page.evaluate(BOOKS_JS)

        print()
        if not books:
            print("未找到作品，请检查登录状态 (重新运行 login)")
        else:
            print(f"找到 {len(books)} 部作品:")
            print("-" * 60)
            for i, b in enumerate(books):
                print(f"  {i+1}. {b['name']}")
                print(f"     ID: {b['bookId']}")
                print(f"     {b['chapters']}章 | {b['words']}字 | {b['status']}")
                print()
            print("-" * 60)
            print("上传时使用:  python fanqie_upload.py upload <目录> --book-id <ID>")

        await save_auth(context)
        await browser.close()


# ---------------------------------------------------------------------------
# 命令: upload
# ---------------------------------------------------------------------------
async def cmd_upload(directory: Path, book_id: str, publish: bool, args):
    if not AUTH_FILE.exists():
        print("请先运行 login 命令登录。")
        return

    cfg = load_config()
    headless = args.headless or cfg.get("headless", False)
    delay = args.delay if args.delay is not None else cfg.get("delay_between_chapters", 3)

    # 定时发布参数
    schedule_date = getattr(args, "schedule", None)
    schedule_time = getattr(args, "time", "08:00") or "08:00"
    per_day = getattr(args, "per_day", 1) or 1
    unique_titles = getattr(args, "unique_titles", False)
    use_ai = getattr(args, "use_ai", False)

    if not directory.is_dir():
        print(f"目录不存在: {directory}")
        return

    files = get_md_files(directory)
    if not files:
        print(f"在 {directory} 中没有找到 .md/.txt 文件")
        return

    # 解析所有文件
    parsed = [parse_md_file(f) for f in files]

    # 检测重复标题
    title_counts = Counter(title for _, title, _ in parsed)
    dup_titles = {t: c for t, c in title_counts.items() if c > 1}

    if dup_titles:
        print("\n⚠ 检测到重复标题 (番茄作家不允许同名章节):")
        for t, c in dup_titles.items():
            indices = [
                i + 1 for i, (_, title, _) in enumerate(parsed) if title == t
            ]
            print(f'  "{t}" × {c} 次  (第 {", ".join(map(str, indices))} 章)')

        if unique_titles:
            parsed = deduplicate_titles(parsed)
            print("  -> 已自动追加章节号后缀去重")
        else:
            print("  提示: 使用 --unique-titles 可自动追加章节号去重")

    # 计算排期
    schedule = None
    if schedule_date:
        try:
            datetime.strptime(schedule_date, "%Y-%m-%d")
        except ValueError:
            print(f"日期格式错误: {schedule_date}  (应为 YYYY-MM-DD)")
            return
        schedule = compute_schedule(len(parsed), schedule_date, schedule_time, per_day)

    # 确定模式
    if schedule:
        validated = _validate_times(schedule_time)
        eff = max(per_day, len(validated)) if validated else per_day
        mode_str = f"定时发布 (从 {schedule_date} 起, 每天 {eff} 章, {schedule_time})"
    elif publish:
        mode_str = "立即发布"
    else:
        mode_str = "存草稿"

    # 预览文件列表
    print(f"\n找到 {len(files)} 个 MD 文件:")
    print("-" * 60)
    total_words = 0
    for i, (num, title, content) in enumerate(parsed):
        wc = len(strip_md_formatting(content))
        total_words += wc
        num_str = f"第{num}章" if num else "   ?  "
        sched_str = f"  [{schedule[i][0]} {schedule[i][1]}]" if schedule else ""
        print(f"  {i+1:3d}. {num_str} {title}  ({wc} 字){sched_str}")
    print("-" * 60)
    print(f"总计: {len(files)} 章, {total_words} 字")
    print(f"目标: Book ID {book_id}")
    print(f"模式: {mode_str}")
    if schedule:
        last_date = schedule[-1][0]
        total_days = (datetime.strptime(last_date, "%Y-%m-%d")
                      - datetime.strptime(schedule_date, "%Y-%m-%d")).days + 1
        print(f"排期: {schedule_date} ~ {last_date} ({total_days} 天)")
    print()

    confirm = input("确认上传? (y/N): ").strip().lower()
    if confirm != "y":
        print("已取消。")
        return

    # 构造新建章节 URL（直接导航即可创建，无需点按钮）
    new_chapter_url = NEW_CHAPTER_URL_TPL.format(book_id=book_id)

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()

        # 先验证登录态：打开新建章节页看是否能进入编辑器
        await page.goto(new_chapter_url)
        try:
            await wait_for_editor_ready(page, timeout=20000)
        except PWTimeout:
            print("无法进入编辑器，请检查:")
            print("  1. Book ID 是否正确")
            print("  2. 登录状态是否有效 (重新运行 login)")
            await page.screenshot(path=str(SCRIPT_DIR / "error_navigate.png"))
            await browser.close()
            return

        success = 0
        failed = 0
        max_retries = cfg.get("max_retries", 2)

        for i, file in enumerate(files):
            chapter_num, title, content = parsed[i]
            num_str = f"第{chapter_num}章 " if chapter_num else ""
            sched_info = f" -> {schedule[i][0]} {schedule[i][1]}" if schedule else ""
            print(f"\n[{i+1}/{len(files)}] {num_str}{title}{sched_info}")

            ok = False
            daily_limit = False
            for attempt in range(1, max_retries + 2):
                try:
                    # 首章首次复用当前页面，其余情况导航到新建 URL
                    if i > 0 or attempt > 1:
                        await page.goto(new_chapter_url)
                        await wait_for_editor_ready(page)

                    await fill_chapter(page, chapter_num, title, content)

                    if schedule:
                        date_str, time_str = schedule[i]
                        await publish_scheduled(page, date_str, time_str, use_ai=use_ai)
                        print(f"  -> 定时发布 {date_str} {time_str}")
                    elif publish:
                        await _navigate_to_publish_settings(page, use_ai=use_ai)
                        confirm_btn = page.locator("button", has_text="确认发布")
                        if await confirm_btn.count() == 0:
                            raise RuntimeError("未找到确认发布按钮")
                        await confirm_btn.first.click()
                        await page.wait_for_timeout(2000)
                        await _check_daily_limit(page)
                        print(f"  -> 已发布")
                    else:
                        await save_draft(page)
                        print(f"  -> 已存草稿")

                    ok = True
                    break

                except DailyLimitReached as e:
                    print(f"\n⚠ {e}")
                    daily_limit = True
                    break

                except Exception as e:
                    if attempt <= max_retries:
                        print(f"  !! 第{attempt}次失败: {e}，重试中...")
                        await page.wait_for_timeout(2000)
                    else:
                        print(f"  !! 失败: {e}")
                        try:
                            err_path = SCRIPT_DIR / f"error_{i}_{file.stem}.png"
                            await page.screenshot(path=str(err_path))
                            print(f"  截图: {err_path}")
                        except Exception:
                            pass

            if daily_limit:
                failed += 1
                break

            if ok:
                success += 1
            else:
                failed += 1

            if i < len(files) - 1 and delay > 0:
                await page.wait_for_timeout(delay * 1000)

        await save_auth(context)
        await browser.close()

        print()
        print("=" * 40)
        print(f"  上传完成!")
        print(f"  成功: {success}  失败: {failed}")
        print("=" * 40)


# ---------------------------------------------------------------------------
# 修改单章（CLI 和 GUI 共用）
# ---------------------------------------------------------------------------
async def edit_one_chapter(
    page, edit_url: str, ch_num: int, title: str, content: str,
    *, use_ai: bool = False, max_retries: int = 2,
) -> bool:
    """编辑单个已有章节（含重试）。成功返回 True，失败返回 False。

    DailyLimitReached 不在此处捕获，直接向上抛出以停止整个循环。
    """
    for attempt in range(1, max_retries + 2):
        try:
            await page.goto(edit_url)
            await wait_for_editor_ready(page)
            await dismiss_edit_hint(page)
            await clear_editor(page)
            await fill_chapter(page, str(ch_num), title, content)
            await _navigate_to_publish_settings(page, use_ai=use_ai)
            await _check_daily_limit(page)
            confirm_btn = page.locator("button", has_text="确认发布")
            if await confirm_btn.count() == 0:
                raise RuntimeError("未找到确认发布按钮")
            await confirm_btn.first.click()
            await page.wait_for_timeout(2000)
            await _check_daily_limit(page)
            print("  -> 已保存修改")
            return True
        except DailyLimitReached:
            raise
        except Exception as e:
            if attempt <= max_retries:
                print(f"  !! 第{attempt}次失败: {e}，重试中...")
                await page.wait_for_timeout(2000)
            else:
                print(f"  !! 失败: {e}")
                try:
                    err_path = SCRIPT_DIR / f"error_edit_{ch_num}.png"
                    await page.screenshot(path=str(err_path))
                    print(f"  截图: {err_path}")
                except Exception:
                    pass
    return False


# ---------------------------------------------------------------------------
# 命令: edit (修改已有章节)
# ---------------------------------------------------------------------------
async def cmd_edit(directory: Path, book_id: str, args):
    """按章节号匹配并修改已有章节内容。"""
    if not AUTH_FILE.exists():
        print("请先运行 login 命令登录。")
        return

    cfg = load_config()
    headless = args.headless or cfg.get("headless", False)
    delay = args.delay if args.delay is not None else cfg.get("delay_between_chapters", 3)
    unique_titles = getattr(args, "unique_titles", False)
    use_ai = getattr(args, "use_ai", False)

    if not directory.is_dir():
        print(f"目录不存在: {directory}")
        return

    files = get_md_files(directory)
    if not files:
        print(f"在 {directory} 中没有找到 .md/.txt 文件")
        return

    parsed = [parse_md_file(f) for f in files]
    if unique_titles:
        parsed = deduplicate_titles(parsed)

    # 获取平台章节列表
    print("正在获取平台章节列表...")
    chapter_manage_url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()

        await page.goto(chapter_manage_url)
        await page.wait_for_load_state("networkidle")

        platform_chapters, _ = await extract_chapters_from_page(page, book_id)

        if not platform_chapters:
            print("未在平台找到章节。请检查 Book ID 和登录状态。")
            await browser.close()
            return

        print(f"平台共有 {len(platform_chapters)} 个章节。")

        # 匹配
        matched, unmatched = match_chapters(parsed, platform_chapters)

        if not matched:
            print("没有匹配到任何章节！请检查本地文件是否包含章节号。")
            await browser.close()
            return

        # 预览
        print(f"\n匹配到 {len(matched)} 个章节:")
        print("-" * 60)
        total_words = 0
        for local_idx, plat_ch, ch_num, title, content in matched:
            wc = len(strip_md_formatting(content))
            total_words += wc
            print(f"  第{ch_num}章 {title} ({wc}字) -> {plat_ch['title']}")
        print("-" * 60)
        print(f"总计: {len(matched)} 章, {total_words} 字")

        if unmatched:
            print(f"\n未匹配 (跳过) {len(unmatched)} 个本地文件:")
            for local_idx, ch_num, title in unmatched:
                reason = "无章节号" if ch_num is None else "平台无此章"
                print(f"  {title} ({reason})")

        print()
        confirm = input("确认修改? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消。")
            await browser.close()
            return

        # 执行修改
        success = 0
        failed = 0
        total = len(matched)

        for i, (local_idx, plat_ch, ch_num, title, content) in enumerate(matched):
            print(f"\n[{i+1}/{total}] 修改第{ch_num}章 {title}")

            edit_url = plat_ch.get("editUrl")
            if not edit_url:
                print("  !! 无法获取编辑链接，跳过")
                failed += 1
                continue

            if edit_url.startswith("/"):
                edit_url = BASE_URL + edit_url

            try:
                if await edit_one_chapter(page, edit_url, ch_num, title, content,
                                          use_ai=use_ai,
                                          max_retries=cfg.get("max_retries", 2)):
                    success += 1
                else:
                    failed += 1
            except DailyLimitReached as e:
                print(f"\n⚠ {e}")
                failed += 1
                break

            if i < total - 1 and delay > 0:
                await page.wait_for_timeout(delay * 1000)

        await save_auth(context)
        await browser.close()

        print()
        print("=" * 40)
        print(f"  修改完成! 成功: {success}  失败: {failed}")
        print("=" * 40)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="番茄作家 MD 批量上传工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s login                               登录番茄作家
  %(prog)s books                               列出你的作品
  %(prog)s upload ./chapters --book-id 12345   上传章节(存草稿)
  %(prog)s upload ./chapters --book-id 12345 --publish  上传并发布

定时发布:
  %(prog)s upload ./chapters --book-id 12345 --schedule 2026-03-14
      从 3/14 起每天 1 章, 默认 08:00 发布

  %(prog)s upload ./chapters --book-id 12345 --schedule 2026-03-14 --per-day 3
      从 3/14 起每天 3 章

修改已有章节:
  %(prog)s upload ./chapters --book-id 12345 --edit
      按章节号匹配并修改已有章节内容
        """,
    )
    sub = parser.add_subparsers(dest="command")

    # login
    sub.add_parser("login", help="登录番茄作家并保存会话")

    # books
    sub.add_parser("books", help="列出你的作品及 Book ID")

    # upload
    up = sub.add_parser("upload", help="批量上传 MD 文件到指定作品")
    up.add_argument("directory", type=Path, help="MD 文件所在目录")
    up.add_argument("--book-id", required=True, help="目标作品 ID")
    up.add_argument("--publish", action="store_true", help="直接发布 (默认仅存草稿)")
    up.add_argument("--headless", action="store_true", help="无头模式 (不显示浏览器)")
    up.add_argument(
        "--delay", type=int, default=None, help="章节间等待秒数 (默认 3)"
    )
    up.add_argument(
        "--schedule", metavar="DATE",
        help="定时发布起始日期, 格式 YYYY-MM-DD (如 2026-03-14)",
    )
    up.add_argument(
        "--time", default="08:00",
        help="定时发布时间, 如 08:00 或 08:00,12:00,20:00 (多时间逗号分隔)",
    )
    up.add_argument(
        "--per-day", type=int, default=1,
        help="每天发布章数 (默认 1)",
    )
    up.add_argument(
        "--unique-titles", action="store_true",
        help="自动给重复标题追加章节号后缀 (如 '选择' -> '选择（39）')",
    )
    up.add_argument(
        "--use-ai", action="store_true",
        help="发布时选择使用AI (默认不使用)",
    )
    up.add_argument(
        "--edit", action="store_true",
        help="修改已有章节 (按章节号匹配, 不可与 --publish/--schedule 同时使用)",
    )

    args = parser.parse_args()

    if args.command == "login":
        asyncio.run(cmd_login())
    elif args.command == "books":
        asyncio.run(cmd_books())
    elif args.command == "upload":
        if getattr(args, "edit", False):
            if getattr(args, "publish", False) or getattr(args, "schedule", None):
                parser.error("--edit 不可与 --publish 或 --schedule 同时使用")
            asyncio.run(cmd_edit(args.directory, args.book_id, args))
        else:
            asyncio.run(
                cmd_upload(args.directory, args.book_id, args.publish, args)
            )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
