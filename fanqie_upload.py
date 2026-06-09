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
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

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
    "auto_unique": True,           # GUI 自动处理重名 开关
    "use_ai": False,               # GUI 稿件使用了AI创作 开关
    "resched_filter_on": False,    # GUI 按章节号筛选 开关
    "resched_filter_op": "≥",      # GUI 章节号筛选运算符 (≤/≥)
    "resched_filter_num": "1",     # GUI 章节号筛选阈值/区间表达式
}

# 平台修饰键 (macOS = Meta/Cmd, 其他 = Control)
_MOD_KEY = "Meta" if sys.platform == "darwin" else "Control"
_browser_timeout = DEFAULT_CONFIG["browser_timeout"]  # 模块级超时(ms)


def _safe_filename(name: str, max_len: int = 40) -> str:
    """移除 Windows 文件名非法字符并截断。"""
    return re.sub(r'[\\/:*?"<>|\r\n]', '_', name)[:max_len]


LOG_FILE = SCRIPT_DIR / "fanqie_error.log"

logger = logging.getLogger("fanqie")


def setup_logging(log_file=None, level=logging.INFO):
    """初始化日志: 控制台 + 可选的滚动文件日志。"""
    if logger.handlers:
        return
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        fh = RotatingFileHandler(
            str(log_file), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        fh.setLevel(logging.INFO)
        logger.addHandler(fh)


class DailyLimitReached(RuntimeError):
    """本章提交触发平台"当日发布字数上限"。

    注意：上限按字数计、不是硬墙——各章字数不同，本章超限后，
    字数较短的后续章节可能仍发得出去（2026-06-06 实测：79-81 章失败、
    82 章成功）。因此上层不中止整批，记录原因后继续后续章节。
    """


# 平台"每日字数上限"toast 文案不止一种（均为实测）:
#   「已到达当日发布字数上限」 -- 新章节发布路径
#   「提交字数超出每日上限」   -- 章节修改提交路径 (2026-06-06)
# 用宽松正则匹配，避免平台换文案后检测失效。
_DAILY_LIMIT_RE = re.compile(r"(每日|当日|今日|本日|单日|今天)[^，。;；]{0,12}上限")
# toast 含这些词 → 视为发布失败，立即抛错（不再傻等按钮超时）。
# 注意 _classify_toasts 先全量匹配每日上限正则，故此处的"超出/上限"只接住
# 非每日类的限制错误（如"标题字数超出限制"），按单章失败处理。
_TOAST_ERROR_RE = re.compile(
    r"失败|错误|异常|敏感|违规|驳回|无法|频繁|稍后再试|审核不通过|超出|超过|上限"
    r"|不能|早于|已过期|不支持"   # 定时发布"不能早于当前时间"类拒绝也要秒级失败
    r"|重复")  # "本书中存在重复标题，请修改后再发布" (2026-06-07 实测)


async def _visible_toast_texts(page) -> dict:
    """单次 evaluate 原子抓取当前可见的 Arco toast 文本，按组件类型分组。

    返回 {"messages": [...], "notifications": [...]}：
    - message: 瞬态提示（~3s 自动消失），平台实测用它弹失败/上限提示
    - notification: 可常驻（公告类）。分开返回是为了让调用方区分角色——
      常驻公告若与瞬态提示同权，一条含"失败"字样的公告会团灭整批，
      一条良性常驻公告会让静默自愈永不触发

    实现要点:
    - 一次 CDP 往返拿一致快照（locator count+nth 在 toast 自动消失下有
      detach 竞态且每条空等 300ms）
    - 优先取最内层 -content 节点：宽选择器会同时命中 wrapper 容器，其
      innerText 是全部子 toast 的换行拼接，去重失效且污染日志；content
      节点不存在时回退宽选择器以兼容平台改版
    - getClientRects 过滤未渲染节点：display:none 的退场残留 innerText
      仍返回旧文案，会把已消失的错误反复算成当前提示；不能用
      offsetParent 判定——toast 容器是 fixed 定位，offsetParent 恒为 null
    出错返回空组——本函数只做观测。
    """
    empty = {"messages": [], "notifications": []}
    try:
        result = await page.evaluate(
            """() => {
                const grab = (contentSel, broadSel) => {
                    let els = document.querySelectorAll(contentSel);
                    if (els.length === 0) els = document.querySelectorAll(broadSel);
                    const out = [];
                    for (const el of els) {
                        if (el.getClientRects().length === 0) continue;
                        const t = (el.innerText || '').trim();
                        if (t && !out.includes(t)) out.push(t);
                        if (out.length >= 6) break;
                    }
                    return out;
                };
                return {
                    messages: grab('.arco-message-content',
                                   "[class*='arco-message']"),
                    notifications: grab('.arco-notification-content',
                                        "[class*='arco-notification']"),
                };
            }""")
        return {
            "messages": [t for t in result.get("messages", [])
                         if isinstance(t, str)],
            "notifications": [t for t in result.get("notifications", [])
                              if isinstance(t, str)],
        }
    except Exception:
        return empty


def _classify_toasts(messages: list[str], notifications: list[str] = ()):
    """对 toast 文本分类抛错: 上限 → DailyLimitReached; 其他错误 → RuntimeError。

    两段式：先全量扫上限、再扫一般错误——同 tick 多条 toast 同时可见时
    （如「操作过于频繁」+「提交字数超出每日上限」），保证上限分类不被
    排在前面的一般错误抢先，避免该章被误判为可重试普通失败。

    上限正则扫 message+notification（万一平台某天用常驻通知发上限）；
    错误正则只扫瞬态 message——常驻公告含"失败/异常"等字样不该团灭整批。
    """
    for t in (*messages, *notifications):
        if _DAILY_LIMIT_RE.search(t):
            raise DailyLimitReached(f"当日发布字数已达上限: {t}")
    for t in messages:
        if _TOAST_ERROR_RE.search(t):
            raise RuntimeError(f"发布失败，页面提示: {t}")


async def _check_daily_limit(page):
    """检测平台"当日发布字数上限"toast，若存在则抛出 DailyLimitReached。

    只扫 toast 元素、不做全页文本匹配，避免误中正文内容。
    """
    toasts = await _visible_toast_texts(page)
    for t in toasts["messages"] + toasts["notifications"]:
        if _DAILY_LIMIT_RE.search(t):
            raise DailyLimitReached(f"当日发布字数已达上限: {t}")


def _compress_chapter_nums(nums) -> str:
    """把章节号集合压缩成筛选表达式: [79,80,81,83] -> "79-81,83"。

    输出与 GUI「按章节号筛选」的组合写法完全兼容，可直接粘贴补传。
    """
    uniq = sorted(set(nums))
    parts = []
    i = 0
    while i < len(uniq):
        j = i
        while j + 1 < len(uniq) and uniq[j + 1] == uniq[j] + 1:
            j += 1
        parts.append(str(uniq[i]) if i == j else f"{uniq[i]}-{uniq[j]}")
        i = j + 1
    return ",".join(parts)


def _log_fail_list(fail_list):
    """批量结束时打印失败章节及原因清单（CLI/GUI 上传与修改共用）。

    末尾追加按筛选语法压缩的失败章节号（如 "79-81,83-114"），
    可直接粘贴到「按章节号筛选」输入框补传失败章节。
    """
    if not fail_list:
        return
    logger.info("  失败章节及原因:")
    for label, reason in fail_list:
        logger.info(f"    - {label}: {reason}")
    nums = []
    for label, _ in fail_list:
        m = re.match(r"第(\d+)章", label)
        if m:
            nums.append(int(m.group(1)))
    if nums:
        logger.info(
            f"  失败章节号: {_compress_chapter_nums(nums)}"
            f"（可直接粘贴到「按章节号筛选」补传）")


# _wait_publish_result 行为参数
_PUBLISH_POLL_MS = 200        # 轮询间隔
_RECLICK_SILENT_S = 5.0       # 按钮在、且无瞬态 toast 持续此秒数 → 判定点击被吞
_RECLICK_MAX = 2              # 自愈重点击次数上限
_RECLICK_TIMEOUT_MS = 2000    # 重点击的 actionability 超时——按钮处于 loading/disabled
                              # （首次点击其实已生效）时必须快速失败，不能用 Playwright
                              # 默认 30s 阻塞整个轮询，也避免真点下去造成重复提交
_RECLICK_BUDGET_S = 7.0       # 发起重点击所需的最少剩余预算（2s 点击 + 5s 观察）。
                              # 不点白点：点完就超时的重点击观察不到结果，
                              # 还会在新建章节流程留下重复提交风险


async def _wait_publish_result(page, confirm_btn, *, timeout: int | None = None):
    """点击「确认发布」后判定发布结果，每 200ms 轮询，按真实时钟控制超时。

    判定规则（实测）:
    - 按钮消失 → 提交成功，立即返回（按钮消失是可靠的成功信号，无需再看 toast）
    - toast 含上限文案 → 抛 DailyLimitReached（上层记录原因后继续后续章节）
    - 瞬态 toast 含失败/错误等 → 立即抛 RuntimeError（不再傻等超时）
    - 按钮在、且连续 5s 无瞬态 toast（常驻公告不算响应）→ 疑似点击被
      遮挡/吞掉（与"下一步"被吞同类问题），自愈：限时重点击；
      仅在剩余预算 ≥ 点击+观察窗时才发起，避免点完即超时的无效点击
    - 超时按钮仍在 → 抛 RuntimeError，注明期间有无页面提示
    - 按钮可见性检测出错 → 状态未知，继续轮询（不当成功）

    平台失败提示（上限/敏感词等）是 Arco Message toast，~3 秒自动消失，
    必须趁还在时捕获。所有捕获到的 toast 文本都写入日志。

    TODO: 更深层的判定是监听章节提交接口的 response JSON 错误码（不依赖
    toast 文案与时机）；待抓到接口 URL/格式后接入。
    """
    if timeout is None:
        timeout = _browser_timeout
    # 真实时钟截止：每 tick 除 200ms 睡眠外还有 CDP 往返耗时，
    # 按固定迭代数算会让实际超时膨胀到名义值的 1.5 倍以上（日志实测 23s vs 15s）
    deadline = time.monotonic() + timeout / 1000
    seen_toasts: list[str] = []
    last_activity = time.monotonic()  # 最近一次"页面有响应"（瞬态 toast 在场）的时刻
    reclicks = 0
    # 接口探针：窗口期内记录提交类接口的 (status, url)，只在失败时输出。
    # 纯观测不判定——为将来切换到"按接口响应码判定"积累真实格式数据。
    api_probe: list[str] = []
    grab_tasks: list = []  # 跟踪 body 异步补抓任务，收尾统一取消，避免孤儿任务告警

    def _on_response(resp):
        try:
            url = resp.url
            if len(api_probe) < 20 and re.search(
                    r"draft|publish|chapter|submit|create|article", url, re.I):
                idx = len(api_probe)
                api_probe.append(f"{resp.status} {url[:160]}")
                # 提交接口(实测 /api/author/publish_article/v0/，业务错误藏在
                # 200 响应的 JSON body 里)——异步补抓 body 前 300 字符，
                # 为切换到"按响应码判定"积累格式数据。
                if "publish_article" in url:
                    async def _grab(i=idx, r=resp):
                        try:
                            body = (await r.text())[:300]
                            api_probe[i] += f" body={body}"
                        except Exception:
                            pass
                    try:
                        grab_tasks.append(asyncio.create_task(_grab()))
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        page.on("response", _on_response)
    except Exception:
        pass
    try:
        while True:
            toasts = await _visible_toast_texts(page)
            for t in toasts["messages"] + toasts["notifications"]:
                if t not in seen_toasts:
                    seen_toasts.append(t)
                    logger.info(f"    页面提示: {t}")
            _classify_toasts(toasts["messages"], toasts["notifications"])
            try:
                visible = await confirm_btn.is_visible()
            except Exception:
                visible = None  # 状态未知（页面跳转/上下文销毁等），不能当成功
            if visible is False:
                return  # 按钮消失 = 提交成功
            now = time.monotonic()
            if visible:
                if toasts["messages"]:
                    # 瞬态 toast 在场=页面有响应（含同文本重复弹出）；
                    # 常驻 notification 不算，否则一条公告会让自愈永不触发
                    last_activity = now
                elif (now - last_activity >= _RECLICK_SILENT_S
                        and reclicks < _RECLICK_MAX
                        and deadline - now >= _RECLICK_BUDGET_S):
                    reclicks += 1
                    last_activity = now
                    logger.warning(
                        f"    按钮未消失且无页面提示，疑似点击未生效，"
                        f"重新点击 (第{reclicks}次)")
                    try:
                        await confirm_btn.click(
                            no_wait_after=True, timeout=_RECLICK_TIMEOUT_MS)
                    except Exception as e:
                        logger.debug(f"    重新点击失败: {e}")
            if time.monotonic() >= deadline:
                break
            await page.wait_for_timeout(_PUBLISH_POLL_MS)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        # 取消未完成的 body 补抓任务，避免函数返回后任务被 GC "destroyed but pending"
        for _t in grab_tasks:
            if not _t.done():
                _t.cancel()
    # ---- 超时失败：尽量多带现场信息（只在失败路径付出这些开销） ----
    if seen_toasts:
        extra = (f"；期间页面提示: {'; '.join(seen_toasts)}"
                 f"（未命中已知失败文案，如确为失败原因请补充词库）")
    else:
        extra = "；期间无任何页面提示"
    try:
        extra += f"；当前URL: {page.url}"
    except Exception:
        pass
    try:
        btn_html = await confirm_btn.evaluate(
            "el => (el.outerHTML || '').slice(0, 160)", timeout=1000)
        if btn_html:
            extra += f"；按钮状态: {btn_html}"
    except Exception:
        pass
    if api_probe:
        logger.info(f"    窗口期接口响应: {'; '.join(api_probe[:8])}")
    raise RuntimeError(f"确认发布按钮未消失，发布可能失败{extra}")


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------
def load_config() -> dict:
    global _browser_timeout
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
            else:
                # 合法 JSON 但不是对象（如 [] / 数字）→ 用默认配置，避免 update 抛 TypeError
                logger.warning("config.json 顶层不是对象，使用默认配置")
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            logger.warning("config.json 格式错误，使用默认配置")
    val = cfg.get("browser_timeout", DEFAULT_CONFIG["browser_timeout"])
    # 单位是毫秒；<1000 几乎必然是把"秒"误填成了毫秒（如 15），会让所有
    # 页面操作瞬间超时、整批失败——按无效处理。
    if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 1000:
        logger.warning(
            f"browser_timeout 无效({val}，单位应为毫秒且 ≥1000)，"
            f"使用默认值 {DEFAULT_CONFIG['browser_timeout']}")
        val = DEFAULT_CONFIG["browser_timeout"]
    _browser_timeout = int(val)

    # 校验其余数值型配置项，避免手改 config.json 写入字符串/负数后在
    # range()、wait_for_timeout() 等处抛 TypeError 中断整个上传任务。
    for key, minimum in (("delay_between_chapters", 0),
                          ("max_retries", 0),
                          ("default_per_day", 1)):
        v = cfg.get(key, DEFAULT_CONFIG[key])
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < minimum:
            logger.warning(f"{key} 无效({v})，使用默认值 {DEFAULT_CONFIG[key]}")
            v = DEFAULT_CONFIG[key]
        cfg[key] = int(v)
    return cfg


def get_browser_timeout() -> int:
    """返回当前 browser_timeout 值（ms），供外部模块使用。"""
    return _browser_timeout


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

    支持格式（前导零自动去除）:
        "001_标题"           -> "1"
        "046_标题"           -> "46"
        "第27章_标题"        -> "27"
        "第 27 章 标题"      -> "27"
        "第十六章 发布会"    -> "16"
        "第一百二十三章 标题" -> "123"
        "第27回 黛玉葬花"    -> "27"
        "第十六话 出发"      -> "16"
        "chapter-027"        -> "27"
        "Chapter 3 - Title"  -> "3"
    """
    # 1) 纯数字开头: 001_xxx, 027 xxx, "39 标题", "39章/话"
    #    要求数字后是结尾/分隔符/章回节话，避免把 "2023年的夏天" 误判成章节号 2023
    m = re.match(r"^(\d+)(?=$|[\s:：_\-.、章回节话])", text)
    if m:
        return str(int(m.group(1)))
    # 2) 第X章/回/节/话 - 阿拉伯数字: 第27章, 第 27 章, 第27回
    m = re.match(r"^第\s*(\d+)\s*[章回节话]", text)
    if m:
        return str(int(m.group(1)))
    # 3) 第X章/回/节/话 - 中文数字: 第十六章, 第一百二十三回
    m = re.match(r"^第([零〇一二两三四五六七八九十百千]+)[章回节话]", text)
    if m:
        num = _cn_to_int(m.group(1))
        if num > 0:
            return str(num)
    # 4) chapter-027, Chapter 3
    m = re.match(r"^chapter[_\-\s]*(\d+)", text, re.IGNORECASE)
    if m:
        return str(int(m.group(1)))
    return None


def _strip_chapter_prefix(text: str) -> str:
    """
    去掉标题中的章节号前缀，只保留标题文字。

    "第 27 章 重新开始"  -> "重新开始"
    "第27章重新开始"      -> "重新开始"
    "第27回 黛玉葬花"    -> "黛玉葬花"
    "第十六话 出发"      -> "出发"
    "001 新的旅程"        -> "新的旅程"
    "001：新的旅程"       -> "新的旅程"
    "chapter-3 出发"      -> "出发"
    "Chapter 3 - Hello"  -> "Hello"
    """
    original = text.strip()
    patterns = [
        r"^第\s*\d+\s*[章回节话][\s:：_\-]*",     # 第 27 章 / 第27章 / 第27回
        r"^第[零〇一二两三四五六七八九十百千]+[章回节话][\s:：_\-]*",  # 第十六章 / 第一百二十三回
        r"^\d+[\s:：_\-]+",                        # 001_xxx / 001:标题 / 001：标题
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
            logger.warning(f"{fp.name}: 编码异常，部分内容可能损坏")
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


def parse_md_files(files: list) -> tuple:
    """解析多个 MD 文件，跳过无法读取的，返回对齐的 (files, parsed)。

    parse_md_file 只兜底编码错误；磁盘读取 OSError（文件在扫描后被删、
    云端按需文件离线、权限不足）若不处理，会让整次刷新/上传在列表推导处
    整段崩掉——GUI 在 pythonw 下无可见报错（刷新像没反应），且崩溃点之后
    _all_parsed 与 _all_files 失配，后续按日期筛选会在索引处 IndexError。
    逐个解析、跳过坏文件，并保持两个返回列表一一对齐。
    """
    kept_files: list[Path] = []
    parsed: list[tuple] = []
    for f in files:
        try:
            parsed.append(parse_md_file(f))
            kept_files.append(f)
        except OSError as e:
            logger.warning(f"跳过无法读取的文件 {f.name}: {e}")
    return kept_files, parsed


def get_md_files(directory: Path) -> list:
    exts = (".md", ".txt")
    files: list[Path] = []
    subdirs: list[Path] = []
    for item in directory.iterdir():
        if item.is_dir():
            subdirs.append(item)
        elif item.is_file() and item.suffix.lower() in exts:
            files.append(item)
    files.sort(key=natural_sort_key)
    # 子文件夹中的文件也视为有效章节
    subdirs.sort(key=natural_sort_key)
    for sub in subdirs:
        try:
            sub_files = [f for f in sub.iterdir()
                         if f.is_file() and f.suffix.lower() in exts]
        except OSError:
            logger.warning(f"无法访问子文件夹: {sub.name}")
            continue
        sub_files.sort(key=natural_sort_key)
        files.extend(sub_files)
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
    # 移除删除线
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    # 移除标题标记
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 移除引用标记
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 移除分隔线
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # 移除代码块标记
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # 移除 HTML 注释
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 移除 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 移除任务列表标记 (- [ ] / - [x]，须在普通列表标记之前处理)
    text = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+\[[ xX]\]\s*", "", text, flags=re.MULTILINE)
    # 移除无序列表标记 (- / * / + 开头)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    # 移除有序列表标记 (1. / 2) 等)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    # 合并连续空行为单个空行
    text = re.sub(r"\n{3,}", "\n\n", text)
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
    # used 跟踪所有已使用的标题，防止后缀后仍然碰撞
    used: set[str] = {t for _, t, _ in parsed_chapters if t not in dup_titles}
    seen: dict[str, int] = {}
    result = []
    for chapter_num, title, content in parsed_chapters:
        if title not in dup_titles:
            result.append((chapter_num, title, content))
            continue
        suffix = chapter_num if chapter_num else str(seen.get(title, 1))
        new_title = f"{title}（{suffix}）"
        seen[title] = seen.get(title, 1) + 1
        while new_title in used:
            new_title = f"{title}（{seen[title]}）"
            seen[title] += 1
        used.add(new_title)
        result.append((chapter_num, new_title, content))
    return result


# ---------------------------------------------------------------------------
# 浏览器操作
# ---------------------------------------------------------------------------
async def create_context(p, headless=False):
    """创建浏览器上下文，如有已保存的登录状态则加载。"""
    browser = await p.chromium.launch(headless=headless)
    try:
        if AUTH_FILE.exists():
            try:
                context = await browser.new_context(storage_state=str(AUTH_FILE))
            except Exception as e:
                # 登录状态文件损坏（半截 JSON 等）→ 降级为全新会话，
                # 而不是让整个任务裸崩；用户重新 login 即可。
                logger.warning(f"登录状态文件无法加载({e})，已忽略——请重新运行 login")
                context = await browser.new_context()
        else:
            context = await browser.new_context()
    except Exception:
        await close_browser_safely(browser)
        raise
    # 授予剪贴板权限，用于可靠的粘贴操作
    await context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=BASE_URL
    )
    return browser, context


async def save_auth(context):
    """保存当前登录状态（原子写：tmp+rename，防进程中断留下半截 JSON）。

    保存失败不应影响本次任务结果，只告警。storage_state 走 CDP，
    浏览器挂死时会无限悬停，故加超时（超时走同一条告警路径）。
    """
    try:
        state = await asyncio.wait_for(context.storage_state(), timeout=30)
        tmp = AUTH_FILE.with_suffix(AUTH_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(AUTH_FILE)
    except Exception as e:
        logger.warning(f"保存登录状态失败(不影响本次结果): {e}")


async def close_browser_safely(browser, timeout_s: float = 30):
    """关闭浏览器，带超时保护——关不掉就放弃等待，不让收尾阻塞结果汇报。

    实测(2026-06-08): 修改任务最后一章保存后浏览器窗口立即消失，但个别
    chrome 子进程卡在退出阶段 41 分钟；Playwright 的 close() 要等浏览器
    进程整体退出、driver 回执后才返回——期间无日志无汇总，GUI 看起来
    像卡死。章节早已提交成功，关浏览器不应把结果汇报当人质。

    注意: close() 超时说明浏览器进程已挂死，随后的 playwright stop
    （async with 退出时的 transport 等待）也可能同样阻塞——所以 GUI 的
    完成汇报必须放在 async with 块内、本函数之后立即发出。
    """
    try:
        await asyncio.wait_for(browser.close(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        # TimeoutError 的 str() 为空，需给出明确文案
        logger.warning(
            f"关闭浏览器超过 {timeout_s:g} 秒未完成（浏览器进程疑似挂死），"
            f"放弃等待，不影响本次结果")
    except Exception as e:
        logger.warning(f"关闭浏览器失败(不影响本次结果): {e}")


async def dismiss_overlays(page, draft_action="放弃"):
    """
    关闭可能遮挡按钮的弹窗:
      1. "提示" 草稿恢复弹窗 -> 点 draft_action 指定的按钮
      2. React Tour 新手引导  -> 用 JS 直接移除
    注意: fill_chapter 已改用 page.evaluate 操作 DOM，不受弹窗影响。
          此函数主要确保 "存草稿"/"下一步" 等按钮可以被 Playwright 点击。

    draft_action: 草稿恢复弹窗按哪个按钮。
      - "放弃"    : 丢弃草稿。开页时用——清掉上次遗留的旧草稿，从已发布内容开始。
      - "继续编辑": 保留草稿。填入新内容后点"下一步"再弹此窗时用——此时草稿正是我们
                    刚填的新内容，必须保留，否则本次编辑会被丢掉（见 edit_one_chapter）。
    """
    await page.wait_for_timeout(800)

    # 1. 草稿恢复弹窗: "有刚刚更新的草稿/章节，是否继续编辑？"
    try:
        draft_hint = page.locator("text=是否继续编辑")
        if await draft_hint.count() > 0:
            action_btn = page.locator("button", has_text=draft_action)
            if await action_btn.count() > 0:
                await action_btn.first.click()
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


async def wait_for_editor_ready(page, timeout=None, draft_action="放弃"):
    """等待章节编辑器加载完成。

    draft_action: 草稿恢复弹窗的处理方式，透传给 dismiss_overlays。
                  新建/修改章节均传"放弃"（修改流程丢弃残留草稿后再 clear+fill 重填）。
    """
    if timeout is None:
        timeout = _browser_timeout
    await page.wait_for_load_state("networkidle", timeout=timeout)
    # 等待 ProseMirror 编辑器出现
    await page.wait_for_selector(".ProseMirror", timeout=timeout)
    # 等待标题输入框出现
    await page.wait_for_selector("input[placeholder='请输入标题']", timeout=timeout)
    await page.wait_for_timeout(500)
    # 关闭弹窗/引导层
    await dismiss_overlays(page, draft_action=draft_action)


async def _get_word_count(page) -> int:
    """从页面顶部获取正文字数，返回整数。"""
    try:
        el = page.locator("text=正文字数")
        if await el.count() > 0:
            txt = await el.text_content()
            # text_content() 可能返回 None（节点暂无文本/正在重渲染）——直接喂给
            # re.search 会抛 TypeError 被下面 except 吞掉、错当成"字数=0 粘贴失败"。
            if txt:
                # 去掉千分位逗号，避免 "1,234" 被截成 1。
                m = re.search(r"(\d[\d,]*)", txt)
                if m:
                    return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


def _prepare_body(content: str) -> str:
    """正文入框前的预处理: 去 Markdown 标记 + 去空行(空段落)。

    番茄编辑器粘贴纯文本时, 空行会生成多余空段落, 故逐行剔除纯空白行,
    段落之间用单个换行衔接。"""
    text = strip_md_formatting(content)
    return "\n".join(ln for ln in text.splitlines() if ln.strip())


async def fill_chapter(page, chapter_num: str | None, title: str, content: str):
    """
    在编辑器页面填入章节内容。

    全部通过 page.evaluate 直接操作 DOM，不使用 Playwright 的
    locator.click()/fill()，这样即使有弹窗/引导层遮挡也不会失败。
    """
    plain_content = _prepare_body(content)

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

            // 2. 填写标题（合成 nativeSetter+input+change；CDP 实测在真实 React 字段
            //    上能 stick 不回灌，与正文一样可靠）
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
        logger.info(f"    正文字数 {wc}")
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
        await page.wait_for_selector("text=已保存", timeout=_browser_timeout)
    except PWTimeout:
        logger.warning("未检测到保存确认，草稿可能未保存成功")
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

        // 清空标题（章节号不动：已发布章节的号是现成的，修改模式不该改它）
        const titleInput = document.querySelector('input[placeholder="请输入标题"]');
        if (titleInput) {
            nativeSetter.call(titleInput, '');
            titleInput.dispatchEvent(new Event('input', { bubbles: true }));
            titleInput.dispatchEvent(new Event('change', { bubbles: true }));
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
        const m = href.match(/chapter-manage\/(\d+)(?:&([^?]*))?/);
        if (!m) continue;
        const bookId = m[1];
        let name;
        if (m[2]) {
            try { name = decodeURIComponent(m[2]); }
            catch { name = m[2]; }
        } else {
            name = '';
        }
        let container = link;
        for (let i = 0; i < 12; i++) {
            if (!container.parentElement) break;
            container = container.parentElement;
            const ct = container.textContent || '';
            if (ct.length > 30 &&
                (ct.includes('万字') || /\d+\s*章/.test(ct))) break;
        }
        const text = container.textContent || '';
        const chapterMatch = text.match(/(\d+)\s*章/);
        const wordMatch = text.match(/([\d.]+)\s*万字/);
        const statusMatch = text.match(/(连载中|已完结)/);
        const signMatch = text.match(/(已签约|未签约)/);
        if (!name) {
            const linkText = link.textContent.trim();
            if (linkText) name = linkText;
            else name = '未命名作品';
        }
        results.push({
            bookId, name,
            chapters: chapterMatch ? chapterMatch[1] : '0',
            words: wordMatch ? wordMatch[1] + '万' : '0',
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
_EXTRACT_ALL_JS = r"""async (opts) => {
    const WAIT_TIMEOUT = (opts && opts.waitTimeout) || 10000;
    const MAX_TIME = (opts && opts.maxTime) || 120000;

    // 等待表格出现
    const t0 = Date.now();
    while (!document.querySelector('tr td')) {
        if (Date.now() - t0 > WAIT_TIMEOUT) break;
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
            let m = title.match(/^第\s*(\d+)\s*[章回节话]/);
            if (m) chapterNum = parseInt(m[1], 10);
            else { m = title.match(/^(\d+)/); if (m) chapterNum = parseInt(m[1], 10); }

            const key = chapterNum + '|' + title;
            if (seenKeys.has(key)) continue;
            seenKeys.add(key);

            // 审核状态（待发布/已发布/审核中 等）
            let status = '';
            for (let ci = 1; ci < cells.length; ci++) {
                const ct = cells[ci].textContent.trim();
                if (/待发布|已发布|审核中|草稿|已拒绝/.test(ct)) {
                    status = ct; break;
                }
            }

            allChapters.push({ title, chapterNum, editUrl, status, rowIndex: allChapters.length });
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


# ---------------------------------------------------------------------------
# JS: 检测章节管理页的卷列表
# ---------------------------------------------------------------------------
DETECT_VOLUMES_JS = r"""async () => {
    const selectEl = document.querySelector(
        '.chapter-select-left .serial-select.byte-select:not(.chapter-status-select)');
    if (!selectEl) return { hasVolumes: false, volumes: [], currentVolume: '' };

    const valueEl = selectEl.querySelector('.byte-select-view-value');
    const currentVolume = valueEl ? valueEl.textContent.trim() : '';

    // 展开下拉读取选项，然后关闭
    selectEl.click();
    await new Promise(r => setTimeout(r, 500));

    const volumes = [];
    for (const opt of document.querySelectorAll(
            '.byte-select-option.chapter-select-option')) {
        volumes.push({
            text: opt.textContent.trim(),
            isActive: opt.classList.contains('byte-select-option-selected'),
        });
    }

    // 关闭下拉
    selectEl.click();
    await new Promise(r => setTimeout(r, 300));

    return { hasVolumes: volumes.length > 1, volumes, currentVolume };
}"""


# ---------------------------------------------------------------------------
# JS: 选择指定卷（直接展开 → 点击目标 → 等待刷新）
# ---------------------------------------------------------------------------
SELECT_VOLUME_JS = r"""async (targetText) => {
    const selectEl = document.querySelector(
        '.chapter-select-left .serial-select.byte-select:not(.chapter-status-select)');
    if (!selectEl) return false;

    selectEl.click();
    await new Promise(r => setTimeout(r, 500));

    for (const opt of document.querySelectorAll(
            '.byte-select-option.chapter-select-option')) {
        if (opt.textContent.trim() === targetText) {
            opt.click();
            await new Promise(r => setTimeout(r, 800));
            return true;
        }
    }

    // 未找到目标卷，关闭下拉
    selectEl.click();
    await new Promise(r => setTimeout(r, 300));
    return false;
}"""


async def detect_volumes(page) -> dict:
    """检测章节管理页是否有多卷，返回 {hasVolumes, volumes, currentVolume}。"""
    try:
        return await page.evaluate(DETECT_VOLUMES_JS)
    except Exception as e:
        logger.debug(f"检测卷列表失败: {e}")
        return {"hasVolumes": False, "volumes": [], "currentVolume": ""}


async def select_volume(page, volume_text: str) -> bool:
    """在章节管理页选择指定卷，返回是否成功。选择后等待表格刷新。"""
    try:
        ok = await page.evaluate(SELECT_VOLUME_JS, volume_text)
        if ok:
            await page.wait_for_timeout(1000)
            logger.info(f"  已切换到: {volume_text}")
        else:
            logger.warning(f"  未找到卷: {volume_text}")
        return ok
    except Exception as e:
        logger.warning(f"选择卷失败: {e}")
        return False


async def extract_chapters_from_page(
    page, book_id: str = "",
) -> tuple[list[dict], dict | None]:
    """从章节管理页提取全部章节列表（单次 JS 调用完成全部翻页）。

    返回 (chapters, last_publish_info)。
    last_publish_info: {date, time, chapter} 或 None。
    """
    result = await page.evaluate(
        _EXTRACT_ALL_JS,
        # maxTime = 8x: 自动翻页可能需要遍历多页，总时长需大于单页超时
        {"waitTimeout": _browser_timeout, "maxTime": _browser_timeout * 8},
    )
    chapters = result.get("chapters", [])
    total_pages = result.get("totalPages", 0)
    page_count = result.get("pageCount", 0)
    last_pub = result.get("lastPublish")

    if total_pages:
        logger.info(f"  共 {page_count}/{total_pages} 页, {len(chapters)} 个章节")
    elif chapters:
        logger.info(f"  共 {page_count} 页, {len(chapters)} 个章节")

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
    dup_nums: list = []
    for ch in platform_chapters:
        num = ch.get("chapterNum")
        if num is None:
            continue
        if num in platform_map:
            # 多卷作品分卷重新编号时可能出现重复章节号，保留首个会导致改错章节
            dup_nums.append(num)
            continue
        platform_map[num] = ch
    if dup_nums:
        logger.warning(
            f"平台存在重复章节号 {sorted(set(dup_nums))}（可能是多卷分别编号）；"
            f"按章节号匹配时仅取首个，建议按卷分别操作以免改错章节。")

    matched = []
    unmatched = []
    for i, (num, title, content) in enumerate(local_parsed):
        int_num = int(num) if num else None
        if int_num is not None and int_num in platform_map:
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

    兼容: 全角标点 (，：；)、单位数小时 (8:00 -> 08:00)。
    """
    # 标准化分隔符: 全角逗号/分号 → 半角逗号
    raw = raw.replace("\uff0c", ",").replace("\uff1b", ",").replace(";", ",")
    result = []
    for t in raw.split(","):
        t = t.strip().replace("\uff1a", ":")  # 全角冒号 → 半角
        m = re.match(r"^(\d{1,2}):(\d{2})$", t)
        if not m:
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            result.append(f"{h:02d}:{mi:02d}")
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
      - 时间点不足时, 均匀分配到各时间点, 同一时间内每章 +1 分钟
      - 每个时间段上限为下一时间点前 1 分钟 (末尾为 23:59), 防止重叠
      - 保序保证: 同日内各章发布时刻严格递增（即「章节顺序 = 发布顺序」，
        且同日时刻唯一）。临近午夜挤在一起时把该串整体前移以放下而不打乱顺序
        （如 23:58×3 → 23:57/23:58/23:59），不跨日、不改每天章数

    返回: [(date_str, time_str), ...] 长度等于 file_count
    """
    per_day = max(1, per_day)
    base = datetime.strptime(start_date, "%Y-%m-%d")
    times = _validate_times(pub_time)
    if not times:
        times = ["08:00"]
    # 时间点数量 > per_day 时，以时间点为准
    effective = max(per_day, len(times))
    # 时间点不足时，均匀分配到各时间点，每个时间点内 +1 分钟递增
    if len(times) < effective:
        n_times = len(times)
        cap_global = datetime.strptime("23:59", "%H:%M")
        parsed_times = [datetime.strptime(t, "%H:%M") for t in times]
        expanded = []
        for t_idx in range(n_times):
            count = effective // n_times + (1 if t_idx < effective % n_times else 0)
            base_t = parsed_times[t_idx]
            # 每个时间段的上限: 下一时间点前 1 分钟, 末尾为 23:59
            slot_cap = (parsed_times[t_idx + 1] - timedelta(minutes=1)
                        if t_idx + 1 < n_times else cap_global)
            for j in range(count):
                nxt = base_t + timedelta(minutes=j)
                if nxt > slot_cap:
                    nxt = slot_cap
                expanded.append(nxt.strftime("%H:%M"))
        times = expanded
    schedule = []
    for i in range(file_count):
        day_offset = i // effective
        d = base + timedelta(days=day_offset)
        slot = i % effective
        t = times[slot]
        schedule.append((d.strftime("%Y-%m-%d"), t))

    # 同日时刻必须唯一，且要保持「章节顺序 = 发布时刻顺序」（读者按序读）。
    # 时间点过近 / 临近午夜且每天章数过多时，槽位 +分钟 会被截顶为相同时刻。
    # 旧实现按冲突逐个向后顺延、排满再「向前回填」——回填会把靠后的章节塞进更早
    # 的分钟，导致同日章节乱序（如 23:58×3 → 第3章 23:57 反而早于第1、2章）。
    # 改为按天做「保序修复」（不跨日、不改每天章数，只前调临近午夜挤住的那一串）：
    #   正向：t_i = max(理想_i, t_{i-1}+1)  —— 严格递增（即唯一），且不早于理想
    #   末章越过 23:59 时再反向：t_i = min(t_i, t_{i+1}-1)，把尾部整体前移到放得下；
    #   早间/前面时间点的章节不受影响。严格递增 ⇒ 同日时刻天然唯一。
    DAY_LAST = 24 * 60 - 1   # 23:59

    def _to_min(t):
        return int(t[:2]) * 60 + int(t[3:])

    def _to_hhmm(m):
        return f"{m // 60:02d}:{m % 60:02d}"

    fixed = []
    adjusted = False
    saturated = False
    # schedule 中同日章节天然连续（day_offset = i // effective 单调不减），逐日分组
    i = 0
    n = len(schedule)
    while i < n:
        j = i
        d = schedule[i][0]
        while j < n and schedule[j][0] == d:
            j += 1
        ideals = [_to_min(t) for _, t in schedule[i:j]]
        mins = list(ideals)
        # 正向：严格递增、不早于理想
        for k in range(1, len(mins)):
            if mins[k] <= mins[k - 1]:
                mins[k] = mins[k - 1] + 1
        # 末章越界 → 反向把临近午夜的尾部整体前移
        if mins and mins[-1] > DAY_LAST:
            mins[-1] = DAY_LAST
            for k in range(len(mins) - 2, -1, -1):
                if mins[k] >= mins[k + 1]:
                    mins[k] = mins[k + 1] - 1
            if mins[0] < 0:
                # 当天章节多到一天 1440 分钟都排不下（per_day 极端，正常 UI 不可达）：
                # 夹回 [0,23:59] 后同日时刻不再保证唯一，告警；其余路径不受影响。
                saturated = True
                cur = 0
                for k in range(len(mins)):
                    m = min(max(ideals[k], cur), DAY_LAST)
                    mins[k] = m
                    cur = m + 1
        if mins != ideals:
            adjusted = True
        for m in mins:
            fixed.append((d, _to_hhmm(max(0, min(m, DAY_LAST)))))
        i = j

    if saturated:
        logger.warning(
            "排期：当天章节过多，一天 24 小时排不下唯一时刻，部分章节时刻可能重复，"
            "平台可能拒绝。建议减少每天章数或拉开发布时间点。")
    elif adjusted:
        logger.warning(
            "排期：个别时刻冲突/临近午夜，已自动微调以保证同日时刻唯一且顺序不乱。")
    return fixed


async def _navigate_to_publish_settings(page, *, use_ai: bool = False, draft_action="放弃"):
    """
    从编辑器完整走到"发布设置"对话框。

    点击"下一步"后可能出现两种流程:
      A) 直接弹出对话框序列（常见）:
         发布提示(错别字确认) -> 是否进行内容风险检测 -> 发布设置
      B) 先打开右侧智能纠错面板:
         纠错面板 -> 忽略全部 -> 再次下一步 -> 对话框序列

    本函数统一处理两种情况。

    draft_action: 点"下一步"后弹出"是否继续编辑"草稿弹窗时，按此按钮处理（状态机分支 2）。
                  修改流程必须传"继续编辑"——此时草稿是我们刚填的新内容，保留它才能把
                  新标题/正文发布出去；传"放弃"会丢弃本次编辑、最终发布的还是原章节。
                  新建/定时发布流程一般不弹此窗，默认值仅作兜底。
    """
    # 统一状态机：轮询页面状态并按状态推进，直到到达"发布设置"。
    # 初次"下一步"与被吞后的自愈走同一条 "仍在编辑器 -> 点下一步" 分支；
    # 草稿恢复弹窗由分支 2 用非阻塞的 .count()+click 处理（不用 add_locator_handler：
    # 它会让每个动作都强制等弹窗消失、关不掉时死等 30s 超时）。
    for _ in range(14):
        # 平台当日字数上限检测
        await _check_daily_limit(page)

        # 1) 已到达发布设置 -> 应用选项后完成
        if await page.locator("text=发布设置").count() > 0:
            await _apply_publish_options(page, use_ai=use_ai)
            return

        # 2) 草稿恢复弹窗"是否继续编辑" -> 按 draft_action 关闭
        if await page.locator("text=是否继续编辑").count() > 0:
            draft_btn = page.locator("button", has_text=draft_action)
            if await draft_btn.count() > 0:
                await draft_btn.first.click()
                await page.wait_for_timeout(500)
                continue

        # 3) 智能纠错面板 -> "忽略全部"
        try:
            ignore_btn = page.locator("button", has_text="忽略全部")
            if await ignore_btn.count() > 0 and await ignore_btn.first.is_visible():
                await ignore_btn.first.click()
                await page.wait_for_timeout(600)
                continue
        except Exception:
            pass

        # 4) 错别字确认"是否确定提交" -> 提交
        if await page.locator("text=是否确定提交").count() > 0:
            submit_btn = page.locator("button", has_text="提交")
            if await submit_btn.count() > 0:
                await submit_btn.first.click()
                await page.wait_for_timeout(800)
                continue

        # 5) 内容风险检测"是否进行内容风险检测" -> 取消跳过
        if await page.locator("text=是否进行内容风险检测").count() > 0:
            cancel_btn = page.locator("button", has_text="取消")
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
                await page.wait_for_timeout(800)
                continue

        # 6) 仍停在编辑器（含首次进入、以及"下一步"被吞的情况）-> 点"下一步"推进。
        #    仅当编辑器的 next 按钮可见时才点，避免误点其它流程的"下一步"。
        editor_next = page.locator("button.auto-editor-next")
        try:
            if await editor_next.count() > 0 and await editor_next.first.is_visible():
                await click_next_step(page)
                continue
        except Exception:
            pass

        # 7) 未知中间态 -> 短等后重查
        await page.wait_for_timeout(800)

    # 兜底: 仍未到达发布设置则等待超时，交由上层重试
    await page.wait_for_selector("text=发布设置", timeout=_browser_timeout)
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
    logger.info(f"    定时发布开关: {switched}")
    if switched == "not_found":
        # 开关没找到，日期框必然不出现——直接快速失败，不空等整个 timeout，
        # 也给出真因而非误导性的"等待日期输入框超时"
        raise RuntimeError("未找到定时发布开关，页面结构可能已变更")
    # 等待日期输入框出现
    try:
        await page.wait_for_selector("input[placeholder='请选择日期']", timeout=_browser_timeout)
    except PWTimeout:
        raise RuntimeError("等待日期输入框超时")
    await page.wait_for_timeout(300)

    # 3. 填写日期 (Arco DatePicker)
    #    键盘方式: 点击输入框 -> 全选 -> 输入日期 -> Enter 确认
    date_input = page.locator("input[placeholder='请选择日期']")
    if await date_input.count() == 0:
        raise RuntimeError("未找到日期输入框")
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
        raise RuntimeError("未找到时间输入框")
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
    await confirm_btn.first.click(no_wait_after=True, timeout=_browser_timeout)
    await _wait_publish_result(page, confirm_btn.first)


# ---------------------------------------------------------------------------
# 命令: login
# ---------------------------------------------------------------------------
async def cmd_login():
    logger.info("正在打开浏览器，请在网页中完成登录...")
    async with async_playwright() as p:
        browser, context = await create_context(p, headless=False)
        page = await context.new_page()
        await page.goto(ZONE_URL)
        await page.wait_for_load_state("networkidle")

        logger.info("")
        logger.info("=" * 50)
        logger.info("  请在浏览器中登录番茄作家账号")
        logger.info("  登录成功后回到此处按 Enter 保存会话")
        logger.info("=" * 50)
        await asyncio.get_running_loop().run_in_executor(None, input)

        await save_auth(context)
        await close_browser_safely(browser)
        logger.info("登录状态已保存。")


# ---------------------------------------------------------------------------
# 命令: books
# ---------------------------------------------------------------------------
async def cmd_books():
    if not AUTH_FILE.exists():
        logger.warning("请先运行 login 命令登录。")
        return

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=True)
        page = await context.new_page()

        await page.goto(BOOK_MANAGE_URL)
        await page.wait_for_load_state("networkidle")
        try:
            await page.wait_for_selector('a[href*="chapter-manage/"]', timeout=5000)
        except PWTimeout:
            pass

        books = await page.evaluate(BOOKS_JS)

        logger.info("")
        if not books:
            logger.error("未找到作品，请检查登录状态 (重新运行 login)")
        else:
            logger.info(f"找到 {len(books)} 部作品:")
            logger.info("-" * 60)
            for i, b in enumerate(books):
                logger.info(f"  {i+1}. {b['name']}")
                logger.info(f"     ID: {b['bookId']}")
                logger.info(f"     {b['chapters']}章 | {b['words']}字 | {b['status']}")
                logger.info("")
            logger.info("-" * 60)
            logger.info("上传时使用:  python fanqie_upload.py upload <目录> --book-id <ID>")

        await save_auth(context)
        await close_browser_safely(browser)


# ---------------------------------------------------------------------------
# 命令: upload
# ---------------------------------------------------------------------------
async def cmd_upload(directory: Path, book_id: str, publish: bool, args):
    if not AUTH_FILE.exists():
        logger.warning("请先运行 login 命令登录。")
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
        logger.error(f"目录不存在: {directory}")
        return

    files = get_md_files(directory)
    if not files:
        logger.warning(f"在 {directory} 及其子文件夹中没有找到 .md/.txt 文件")
        return

    # 解析所有文件（跳过扫描后变得无法读取的文件，保持 files/parsed 对齐）
    files, parsed = parse_md_files(files)
    if not files:
        logger.warning("目录中的文件均无法读取（可能是云端离线文件或权限不足）")
        return

    # 检测重复标题
    title_counts = Counter(title for _, title, _ in parsed)
    dup_titles = {t: c for t, c in title_counts.items() if c > 1}

    if dup_titles:
        logger.warning("检测到重复标题 (番茄作家不允许同名章节):")
        for t, c in dup_titles.items():
            indices = [
                i + 1 for i, (_, title, _) in enumerate(parsed) if title == t
            ]
            logger.info(f'  "{t}" × {c} 次  (第 {", ".join(map(str, indices))} 章)')

        if unique_titles:
            parsed = deduplicate_titles(parsed)
            logger.info("  -> 已自动追加章节号后缀去重")
        else:
            logger.info("  提示: 使用 --unique-titles 可自动追加章节号去重")

    # 计算排期
    schedule = None
    if schedule_date:
        try:
            datetime.strptime(schedule_date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"日期格式错误: {schedule_date}  (应为 YYYY-MM-DD)")
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
    logger.info(f"找到 {len(files)} 个 MD 文件:")
    logger.info("-" * 60)
    total_words = 0
    for i, (num, title, content) in enumerate(parsed):
        wc = len(strip_md_formatting(content))
        total_words += wc
        num_str = f"第{num}章" if num else "   ?  "
        sched_str = f"  [{schedule[i][0]} {schedule[i][1]}]" if schedule else ""
        logger.info(f"  {i+1:3d}. {num_str} {title}  ({wc} 字){sched_str}")
    logger.info("-" * 60)
    logger.info(f"总计: {len(files)} 章, {total_words} 字")
    logger.info(f"目标: Book ID {book_id}")
    logger.info(f"模式: {mode_str}")
    if schedule:
        last_date = schedule[-1][0]
        total_days = (datetime.strptime(last_date, "%Y-%m-%d")
                      - datetime.strptime(schedule_date, "%Y-%m-%d")).days + 1
        logger.info(f"排期: {schedule_date} ~ {last_date} ({total_days} 天)")
    logger.info("")

    confirm = input("确认上传? (y/N): ").strip().lower()
    if confirm != "y":
        logger.info("已取消。")
        return

    # 构造新建章节 URL（直接导航即可创建，无需点按钮）
    new_chapter_url = NEW_CHAPTER_URL_TPL.format(book_id=book_id)

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()

        # 先验证登录态：打开新建章节页看是否能进入编辑器
        await page.goto(new_chapter_url)
        try:
            await wait_for_editor_ready(page)
        except Exception as e:
            # 不止 PWTimeout：dismiss_overlays/evaluate 等可能抛非超时错误，
            # 漏接会让异常逃出 async with、浏览器未 close 即 pw.stop → 收尾挂死
            logger.error(f"无法进入编辑器（{e}），请检查:")
            logger.info("  1. Book ID 是否正确")
            logger.info("  2. 登录状态是否有效 (重新运行 login)")
            try:
                await page.screenshot(path=str(SCRIPT_DIR / "error_navigate.png"))
            except Exception:
                pass
            await close_browser_safely(browser)
            return

        success = 0
        failed = 0
        consec_fail = 0
        fail_list: list[tuple[str, str]] = []  # (章节标签, 失败原因)
        max_retries = cfg.get("max_retries", 2)

        for i, file in enumerate(files):
            chapter_num, title, content = parsed[i]
            num_str = f"第{chapter_num}章 " if chapter_num else ""
            sched_info = f" -> {schedule[i][0]} {schedule[i][1]}" if schedule else ""
            logger.info(f"[{i+1}/{len(files)}] {num_str}{title}{sched_info}")

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
                        logger.info(f"  -> 定时发布 {date_str} {time_str}")
                    elif publish:
                        await _navigate_to_publish_settings(page, use_ai=use_ai)
                        confirm_btn = page.locator("button", has_text="确认发布")
                        if await confirm_btn.count() == 0:
                            raise RuntimeError("未找到确认发布按钮")
                        await confirm_btn.first.click(no_wait_after=True, timeout=_browser_timeout)
                        # 判定结果: 按钮消失=成功；toast 分类失败原因
                        await _wait_publish_result(page, confirm_btn.first)
                        logger.info(f"  -> 已发布")
                    else:
                        await save_draft(page)
                        logger.info(f"  -> 已存草稿")

                    ok = True
                    break

                except DailyLimitReached as e:
                    # 上限按字数计、非硬墙：字数较短的后续章节可能仍发得出去，
                    # 故只记录原因、跳过本章，不中止整批。
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
                            err_path = SCRIPT_DIR / f"error_{i}_{file.stem}.png"
                            await page.screenshot(path=str(err_path))
                            logger.error(f"截图: {err_path}")
                        except Exception:
                            pass

            if daily_limit:
                failed += 1
                # 上限失败重置熔断计数：toast 被正确捕获说明整条流程健康，
                # 它不该和"原因不明失败"拼成"连续 3 章"误触发熔断
                consec_fail = 0
            elif ok:
                success += 1
                consec_fail = 0
            else:
                failed += 1
                consec_fail += 1
                if consec_fail >= 3:
                    rest = len(files) - (i + 1)
                    logger.error(
                        f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                        f"中止任务，剩余 {rest} 章未处理")
                    break

            if i < len(files) - 1 and delay > 0:
                try:
                    await page.wait_for_timeout(delay * 1000)
                except Exception:
                    # 章节间等待时页面已死：停止循环，但仍走到下面的
                    # save_auth/close，避免收尾被跳过导致 pw.stop 挂死
                    break

        await save_auth(context)
        await close_browser_safely(browser)

        logger.info("")
        logger.info("=" * 40)
        logger.info(f"  上传完成!")
        logger.info(f"  成功: {success}  失败: {failed}")
        _log_fail_list(fail_list)
        logger.info("=" * 40)


# ---------------------------------------------------------------------------
# 修改单章（CLI 和 GUI 共用）
# ---------------------------------------------------------------------------
async def edit_one_chapter(
    page, edit_url: str, ch_num: int, title: str, content: str,
    *, use_ai: bool = False, max_retries: int = 2,
) -> tuple[bool, str]:
    """编辑单个已有章节（含重试）。返回 (是否成功, 最后一次错误信息)。

    错误信息供上层写入失败清单（真实原因优于"见日志"），并用于识别
    "重复标题"类可二次尝试的失败。
    DailyLimitReached 不在此处捕获（本章重试无意义，字数不会变），
    直接向上抛出，由上层记录原因并继续后续章节。
    """
    last_err = ""
    for attempt in range(1, max_retries + 2):
        try:
            await page.goto(edit_url)
            # 打开时若有「上次遗留」的旧草稿 -> "放弃"，从已发布内容开始干净重填。
            await wait_for_editor_ready(page, draft_action="放弃")
            await dismiss_edit_hint(page)
            # 只清/填标题+正文，章节号不动（已发布章节的号是现成的）。
            # _prepare_body 已去 md+空行。
            await clear_editor(page)
            await fill_chapter(page, None, title, content)
            await page.wait_for_timeout(800)
            # 关键修复：填入新内容后，番茄会把它自动存成草稿；点"下一步"时会弹
            # 「有刚刚更新的章节，是否继续编辑？」。这里必须点"继续编辑"保留我们刚填的
            # 新标题+新正文；若点"放弃"会把这次编辑整个丢掉、最终发布的还是原章节
            # （这正是"标题/正文改不动"的根因，CDP 实测确认）。
            await _navigate_to_publish_settings(
                page, use_ai=use_ai, draft_action="继续编辑")
            await _check_daily_limit(page)
            confirm_btn = page.locator("button", has_text="确认发布")
            if await confirm_btn.count() == 0:
                raise RuntimeError("未找到确认发布按钮")
            await confirm_btn.first.click(no_wait_after=True, timeout=_browser_timeout)
            await _wait_publish_result(page, confirm_btn.first)
            logger.info("  -> 已保存修改")
            return True, ""
        except DailyLimitReached:
            raise
        except Exception as e:
            last_err = str(e)
            if attempt <= max_retries:
                logger.warning(f"第{attempt}次失败: {e}，重试中...")
                await page.wait_for_timeout(2000)
            else:
                logger.error(f"失败: {e}")
                try:
                    err_path = SCRIPT_DIR / f"error_edit_{ch_num}.png"
                    await page.screenshot(path=str(err_path))
                    logger.error(f"截图: {err_path}")
                except Exception:
                    pass
    return False, last_err


async def reschedule_on_manage_page(
    page,
    book_id: str,
    schedule_map: dict[str, tuple[str, str]],
    *,
    max_retries: int = 2,
    delay: float = 1,
    cancel_check=None,
    progress_cb=None,
    volume_text: str = "",
    volume_texts: list[str] | None = None,
) -> tuple[int, int]:
    """在章节管理页上批量修改待发布章节的定时发布设置。

    schedule_map: {章节标题: (date_str, time_str), ...}
    cancel_check: 返回 True 时中止
    progress_cb:  (done, total) 回调
    volume_text:  多卷时选择的卷名（空字符串表示不切换）
    volume_texts: 多卷索引模式时传入所有卷名列表（优先级高于 volume_text）
    返回 (success, failed)。
    """
    total = len(schedule_map)
    success = 0
    failed = 0
    remaining = dict(schedule_map)  # 未处理的

    chapter_manage_url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)
    await page.goto(chapter_manage_url)
    await page.wait_for_load_state("networkidle")

    # 等待表格出现
    try:
        await page.wait_for_selector("tr td", timeout=_browser_timeout)
    except Exception:
        logger.error("章节管理页表格未加载")
        return 0, total

    # 多卷索引模式: 逐卷处理
    if volume_texts:
        for vi, vt in enumerate(volume_texts):
            if not remaining:
                break
            if cancel_check and cancel_check():
                break
            logger.info(f"切换到分卷 ({vi+1}/{len(volume_texts)}): {vt}")
            if not await select_volume(page, vt):
                # 切换失败若不拦截，下一步会扫到当前(错误的)卷，把这一卷
                # 的章节当"未处理"统计且诊断误导——跳过本卷，留待"未处理"汇报
                logger.error(f"  切换到分卷失败，跳过本卷: {vt}")
                continue
            s, f = await _reschedule_current_volume(
                page, remaining, total,
                max_retries=max_retries, delay=delay,
                cancel_check=cancel_check, progress_cb=progress_cb,
                success_so_far=success, failed_so_far=failed)
            success += s
            failed += f
        if remaining:
            for title in remaining:
                logger.error(f"未处理: {title}")
            failed += len(remaining)
        return success, failed

    # 单卷模式
    if volume_text:
        await select_volume(page, volume_text)

    s, f = await _reschedule_current_volume(
        page, remaining, total,
        max_retries=max_retries, delay=delay,
        cancel_check=cancel_check, progress_cb=progress_cb,
        success_so_far=success, failed_so_far=failed)
    success += s
    failed += f

    if remaining:
        for title in remaining:
            logger.error(f"未处理: {title}")
        failed += len(remaining)

    return success, failed


async def _reschedule_current_volume(
    page,
    remaining: dict[str, tuple[str, str]],
    total: int,
    *,
    max_retries: int = 2,
    delay: float = 1,
    cancel_check=None,
    progress_cb=None,
    success_so_far: int = 0,
    failed_so_far: int = 0,
) -> tuple[int, int]:
    """扫描当前卷的所有页面，处理 remaining 中匹配到的章节。

    会直接从 remaining 中删除已处理的条目。
    返回本轮 (success, failed)。
    """
    success = 0
    failed = 0

    # 诊断行结构，找出时钟图标的选择器
    icon_selector = await page.evaluate(r"""() => {
        for (const row of document.querySelectorAll('tr')) {
            const cells = row.querySelectorAll('td');
            if (cells.length < 3) continue;
            for (let i = 1; i < cells.length - 1; i++) {
                const cell = cells[i];
                const el = cell.querySelector('svg')
                    || cell.querySelector('i[class]')
                    || cell.querySelector('span[class*="icon"]')
                    || cell.querySelector('button')
                    || cell.querySelector('[role="button"]')
                    || cell.querySelector('[role="img"]');
                if (el) {
                    const tag = el.tagName.toLowerCase();
                    const cls = el.className || '';
                    if (tag === 'svg') return 'svg';
                    if (tag === 'i' && cls) return 'i.' + cls.split(' ')[0];
                    if (cls) return tag + '.' + cls.split(' ')[0];
                    return tag;
                }
            }
        }
        return null;
    }""")
    logger.debug(f"  时钟图标元素: {icon_selector or '未检测到'}")

    page_num = 0
    while remaining:
        page_num += 1
        if cancel_check and cancel_check():
            logger.info("用户取消修改定时。")
            break

        # 扫描当前页所有行的标题
        page_titles = await page.evaluate(r"""() => {
            const result = [];
            for (const row of document.querySelectorAll('tr')) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 3) continue;
                const title = cells[0].textContent.trim();
                if (title) result.push(title);
            }
            return result;
        }""")

        # 去重：同一标题在本页若出现多行，remaining 是按标题键的字典只能存一条，
        # 处理后会 del remaining[title]，重复迭代会在 remaining[title] 处抛 KeyError。
        matched_on_page = list(dict.fromkeys(
            t for t in page_titles if t in remaining))

        for title in matched_on_page:
            if cancel_check and cancel_check():
                logger.info("用户取消修改定时。")
                break

            date_str, time_str = remaining[title]
            done_so_far = success_so_far + failed_so_far + success + failed
            logger.info(f"[{done_so_far + 1}/{total}] {title} -> {date_str} {time_str}")

            ok = False
            for attempt in range(1, max_retries + 2):
                try:
                    # 点击时钟图标: 在匹配行的中间列中查找可点击元素
                    clicked = await page.evaluate(r"""(targetTitle) => {
                        for (const row of document.querySelectorAll('tr')) {
                            const cells = row.querySelectorAll('td');
                            if (cells.length < 3) continue;
                            if (cells[0].textContent.trim() !== targetTitle)
                                continue;
                            for (let i = 1; i < cells.length - 1; i++) {
                                const cell = cells[i];
                                const el = cell.querySelector('svg')
                                    || cell.querySelector('i[class]')
                                    || cell.querySelector('span[class*="icon"]')
                                    || cell.querySelector('button')
                                    || cell.querySelector('[role="button"]')
                                    || cell.querySelector('[role="img"]');
                                if (el) { el.click(); return true; }
                            }
                            return false;
                        }
                        return false;
                    }""", title)

                    if not clicked:
                        raise RuntimeError("未找到时钟图标")

                    # 等待"修改定时"对话框出现
                    confirm_btn = page.locator(
                        "button", has_text="确认修改")
                    await confirm_btn.wait_for(timeout=_browser_timeout)
                    await page.wait_for_timeout(300)

                    # 填写日期
                    date_input = page.locator(
                        "input[placeholder='请选择日期']")
                    await date_input.click()
                    await page.wait_for_timeout(200)
                    await page.keyboard.press(f"{_MOD_KEY}+a")
                    await page.keyboard.type(date_str, delay=50)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(500)

                    # 填写时间（点击时间输入框会自动关闭日期面板）
                    time_input = page.locator(
                        "input[placeholder='请选择时间']")
                    await time_input.click()
                    await page.wait_for_timeout(200)
                    await page.keyboard.press(f"{_MOD_KEY}+a")
                    await page.keyboard.type(time_str, delay=50)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(500)

                    # 点击"确认修改"，判定结果: 按钮消失=成功；toast 分类失败原因
                    await confirm_btn.first.click(no_wait_after=True, timeout=_browser_timeout)
                    await _wait_publish_result(page, confirm_btn.first)

                    logger.info(f"  -> 已修改定时 {date_str} {time_str}")
                    ok = True
                    break

                except DailyLimitReached as e:
                    # 改期不提交字数，上限 toast 多为相邻操作残留；
                    # 重试无意义，按失败记录并继续后续章节（不截图）。
                    logger.warning(f"  跳过本章（{e}）")
                    break

                except Exception as e:
                    # 尝试关闭可能残留的弹窗
                    try:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(300)
                    except Exception:
                        pass
                    if attempt <= max_retries:
                        logger.warning(f"第{attempt}次失败: {e}，重试中...")
                        await page.wait_for_timeout(1000)
                    else:
                        logger.error(f"失败: {e}")
                        try:
                            err_path = SCRIPT_DIR / f"error_resched_{_safe_filename(title, 20)}.png"
                            await page.screenshot(path=str(err_path))
                            logger.error(f"截图: {err_path}")
                        except Exception:
                            pass

            if ok:
                success += 1
            else:
                failed += 1
            del remaining[title]

            if progress_cb:
                progress_cb(success_so_far + failed_so_far + success + failed, total)

            if delay > 0 and remaining:
                await page.wait_for_timeout(int(delay * 1000))

        # cancel_check 在内部 break 后也需要退出外层
        if cancel_check and cancel_check():
            break

        if not remaining:
            break

        # 翻页
        next_btn = page.locator(
            "li.arco-pagination-item-next:not(.arco-pagination-item-disabled)")
        if await next_btn.count() == 0:
            break
        if page_num >= 500:
            # 硬上限：防止异常情况下（按钮永不 disabled 等）无限翻页
            logger.warning("翻页超过 500 页，停止扫描本卷")
            break
        first_title = await page.evaluate(
            "() => document.querySelector('tr td')?.textContent?.trim() || ''")
        await next_btn.click()
        # 等待表格内容变化
        changed = False
        for _ in range(30):
            await page.wait_for_timeout(300)
            cur = await page.evaluate(
                "() => document.querySelector('tr td')?.textContent?.trim() || ''")
            if cur and cur != first_title:
                changed = True
                break
        if not changed:
            # 9 秒内首格未变：翻页卡住（或跨页首格同名），再扫只会原地打转
            logger.warning("翻页未检测到内容变化，停止扫描本卷")
            break

    return success, failed


# ---------------------------------------------------------------------------
# 命令: edit (修改已有章节)
# ---------------------------------------------------------------------------
async def cmd_edit(directory: Path, book_id: str, args):
    """按章节号匹配并修改已有章节内容。"""
    if not AUTH_FILE.exists():
        logger.warning("请先运行 login 命令登录。")
        return

    cfg = load_config()
    headless = args.headless or cfg.get("headless", False)
    delay = args.delay if args.delay is not None else cfg.get("delay_between_chapters", 3)
    unique_titles = getattr(args, "unique_titles", False)
    use_ai = getattr(args, "use_ai", False)

    if not directory.is_dir():
        logger.error(f"目录不存在: {directory}")
        return

    files = get_md_files(directory)
    if not files:
        logger.warning(f"在 {directory} 及其子文件夹中没有找到 .md/.txt 文件")
        return

    files, parsed = parse_md_files(files)
    if not files:
        logger.warning("目录中的文件均无法读取（可能是云端离线文件或权限不足）")
        return
    if unique_titles:
        parsed = deduplicate_titles(parsed)

    # 获取平台章节列表
    logger.info("正在获取平台章节列表...")
    chapter_manage_url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()

        await page.goto(chapter_manage_url)
        await page.wait_for_load_state("networkidle")

        platform_chapters, _ = await extract_chapters_from_page(page, book_id)

        if not platform_chapters:
            logger.warning("未在平台找到章节。请检查 Book ID 和登录状态。")
            await close_browser_safely(browser)
            return

        logger.info(f"平台共有 {len(platform_chapters)} 个章节。")

        # 匹配
        matched, unmatched = match_chapters(parsed, platform_chapters)

        if not matched:
            logger.warning("没有匹配到任何章节！请检查本地文件是否包含章节号。")
            await close_browser_safely(browser)
            return

        # 预览
        logger.info(f"匹配到 {len(matched)} 个章节:")
        logger.info("-" * 60)
        total_words = 0
        for local_idx, plat_ch, ch_num, title, content in matched:
            wc = len(strip_md_formatting(content))
            total_words += wc
            logger.info(f"  第{ch_num}章 {title} ({wc}字) -> {plat_ch['title']}")
        logger.info("-" * 60)
        logger.info(f"总计: {len(matched)} 章, {total_words} 字")

        if unmatched:
            logger.warning(f"未匹配 (跳过) {len(unmatched)} 个本地文件:")
            for local_idx, ch_num, title in unmatched:
                reason = "无章节号" if ch_num is None else "平台无此章"
                logger.info(f"  {title} ({reason})")

        logger.info("")
        confirm = input("确认修改? (y/N): ").strip().lower()
        if confirm != "y":
            logger.info("已取消。")
            await close_browser_safely(browser)
            return

        # 执行修改
        success = 0
        failed = 0
        skipped = 0
        consec_fail = 0
        fail_list: list[tuple[str, str]] = []  # (章节标签, 失败原因)
        dup_pending: list[tuple] = []  # "重复标题"暂存，批末二次尝试
        total = len(matched)

        for i, (local_idx, plat_ch, ch_num, title, content) in enumerate(matched):
            logger.info(f"[{i+1}/{total}] 修改第{ch_num}章 {title}")

            status = plat_ch.get("status", "")
            if "审核中" in status:
                logger.warning(f"  状态「{status}」审核中，不可编辑，跳过")
                skipped += 1
                continue

            edit_url = plat_ch.get("editUrl")
            if not edit_url:
                logger.error("无法获取编辑链接，跳过（可能审核中或平台未提供编辑入口）")
                skipped += 1
                continue

            if edit_url.startswith("/"):
                edit_url = BASE_URL + edit_url

            try:
                ok, err = await edit_one_chapter(
                    page, edit_url, ch_num, title, content,
                    use_ai=use_ai, max_retries=cfg.get("max_retries", 2))
                if ok:
                    success += 1
                    consec_fail = 0
                elif "重复" in err:
                    # 标题在章节间搬移的临时冲突（实测: 本地重新编号后，
                    # 新章先于旧章提交同名标题被拒；旧章稍后更新即释放）。
                    # 留待批末二次尝试；属已识别原因，不计熔断。
                    logger.info("  标题暂被其他章节占用，留待批末二次尝试")
                    dup_pending.append((ch_num, title, content, edit_url))
                    consec_fail = 0
                else:
                    failed += 1
                    fail_list.append((f"第{ch_num}章 {title}",
                                      err or "重试后仍失败(见日志/截图)"))
                    consec_fail += 1
                    if consec_fail >= 3:
                        rest = total - (i + 1)
                        logger.error(
                            f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                            f"中止任务，剩余 {rest} 章未处理")
                        skipped += rest
                        break
            except DailyLimitReached as e:
                # 上限按字数计、非硬墙：字数较短的后续章节可能仍发得出去，
                # 故只记录原因、跳过本章，不中止整批。toast 被正确捕获说明
                # 流程健康，重置熔断计数，避免与原因不明失败拼成误熔断。
                logger.warning(f"  跳过本章（{e}），继续后续章节")
                fail_list.append((f"第{ch_num}章 {title}", str(e)))
                failed += 1
                consec_fail = 0
            except Exception as e:
                # edit_one_chapter 正常不会泄漏非上限异常（内部已含重试+吞错），
                # 这里兜底浏览器崩溃/页面被关等意外，按原因不明失败计入熔断，
                # 保证 save_auth/汇总仍能执行而不是整批裸抛中止。
                logger.error(f"  本章发生未预期异常: {e}")
                fail_list.append((f"第{ch_num}章 {title}", f"未预期异常: {e}"))
                failed += 1
                consec_fail += 1
                if consec_fail >= 3:
                    rest = total - (i + 1)
                    logger.error(
                        f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                        f"中止任务，剩余 {rest} 章未处理")
                    skipped += rest
                    break

            if i < total - 1 and delay > 0:
                try:
                    await page.wait_for_timeout(delay * 1000)
                except Exception:
                    break  # 页面已死：停止循环，仍走到 save_auth/close 收尾

        # 批末二次尝试: 主循环跑完后，占用旧标题的章节多已更新、标题已释放
        if dup_pending:
            logger.info("")
            logger.info(f"二次尝试 {len(dup_pending)} 个标题重复的章节"
                        f"（标题搬移的临时冲突，此时多已解除）...")
            dead = False
            for ch_num, title, content, edit_url in dup_pending:
                if dead:
                    # 页面已死，剩余条目逐个 goto 只会重复快速失败+刷屏，
                    # 直接如实记失败，不再尝试
                    failed += 1
                    fail_list.append((f"第{ch_num}章 {title}", "页面已失效，未二次尝试"))
                    continue
                logger.info(f"[二次] 修改第{ch_num}章 {title}")
                try:
                    ok, err = await edit_one_chapter(
                        page, edit_url, ch_num, title, content,
                        use_ai=use_ai, max_retries=0)
                    if ok:
                        success += 1
                    else:
                        failed += 1
                        fail_list.append((f"第{ch_num}章 {title}",
                                          err or "标题重复，二次尝试仍失败"))
                except DailyLimitReached as e:
                    logger.warning(f"  跳过本章（{e}）")
                    fail_list.append((f"第{ch_num}章 {title}", str(e)))
                    failed += 1
                except Exception as e:
                    logger.error(f"  二次尝试异常: {e}")
                    fail_list.append((f"第{ch_num}章 {title}", f"二次尝试异常: {e}"))
                    failed += 1
                    if page.is_closed():
                        dead = True
                if delay > 0:
                    try:
                        await page.wait_for_timeout(delay * 1000)
                    except Exception:
                        pass  # 页面已死也要走完计数与汇总

        await save_auth(context)
        await close_browser_safely(browser)

        logger.info("")
        logger.info("=" * 40)
        skip_str = f"  跳过: {skipped}" if skipped else ""
        logger.info(f"  修改完成! 成功: {success}  失败: {failed}{skip_str}")
        _log_fail_list(fail_list)
        logger.info("=" * 40)


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
    setup_logging(LOG_FILE)

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
