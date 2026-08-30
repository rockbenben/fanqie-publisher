#!/usr/bin/env python3
"""
番茄作家 MD 批量上传工具

将本地 Markdown 文件批量上传到番茄作家平台作为小说章节。

用法:
    python fanqie_upload.py login                              登录并保存会话
    python fanqie_upload.py books                              列出你的作品
    python fanqie_upload.py upload ./chapters --book-id ID     批量上传章节(存草稿)
    python fanqie_upload.py upload ./chapters --book-id ID --publish  批量上传并发布
    python fanqie_upload.py upload ./chapters --book-id ID --schedule 2026-09-01 --per-day 3
                                                               定时发布(每天3章)
    python fanqie_upload.py upload ./chapters --book-id ID --chapters 79-114
                                                               补传指定章节
    python fanqie_upload.py reschedule --book-id ID --schedule 2026-09-01
                                                               批量改待发布章的排期
    python fanqie_upload.py audit                              缺口体检(只读)
    python fanqie_upload.py remap --run                        修未公开段的位置错位
    python fanqie_upload.py clean-drafts --run                 清空草稿箱

MD 文件格式:
    文件名: 001_章节标题.md  或  第1章_标题.md  或  任意名称.md
    内容: 纯文本或 Markdown，首行的 # 标题可作为章节标题
    排序: 按文件名自然排序决定上传顺序
"""

import argparse
import asyncio
import json
import re
import sys
import time
import unicodedata
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

# networkidle 的等待上限。番茄的埋点/轮询一直在发请求，这个页面**永远不会**
# 进入 networkidle —— 每一处都必然走到超时。Playwright 默认是 30 秒，于是
# 「正在获取章节列表…」这类状态每次都白等 30 秒才继续。
# 真正的就绪判据是它后面的 wait_for_selector("tr td") / 提取 JS 自带的表格等待，
# 所以这里只给一个「页面碰巧很快静下来就用上」的短窗口。
NETWORKIDLE_MS = 3000


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

    虽然上限按字数计、非硬墙（2026-06-06 曾见 79-81 失败、82 字数较短仍
    成功），但实践中继续提交后续章节多半重复撞限、还会触发别的拦截，徒增
    额外错误。故上层捕获后中止整批，把本章与所有剩余未处理章节如实记入失败
    清单（log_fail_list 会压缩成章节号，可直接粘贴补传），留待明天接着发。
    """


# 平台"发布字数上限"toast/接口文案不止一种（均为实测）:
#   「已到达当日发布字数上限」 -- 新章节发布路径
#   「提交字数超出每日上限」   -- 章节修改提交路径 (2026-06-06)
#   「提交字数超出每月上限」   -- 接口 code!=0 返回 (2026-07-25 真机)
# 每月/每日上限语义一致：额度耗尽，中止整批、记录剩余、重试无意义。
# 用宽松正则匹配，避免平台换文案后检测失效。
_DAILY_LIMIT_RE = re.compile(
    r"(每日|当日|今日|本日|单日|今天|每月|本月|单月|月度)[^，。;；]{0,12}上限")
# toast 含这些词 → 视为发布失败，立即抛错（不再傻等按钮超时）。
# 注意 _classify_toasts 先全量匹配每日上限正则，故此处的"超出/上限"只接住
# 非每日类的限制错误（如"标题字数超出限制"），按单章失败处理。
_TOAST_ERROR_RE = re.compile(
    r"失败|错误|异常|敏感|违规|驳回|无法|频繁|稍后再试|审核不通过|超出|超过|上限"
    r"|不能|早于|已过期|不支持"   # 定时发布"不能早于当前时间"类拒绝也要秒级失败
    r"|重复")  # "本书中存在重复标题，请修改后再发布" (2026-06-07 实测)
# 编辑器字段校验失败 toast（点"下一步"/存草稿后平台拦截未填好的字段，均实测）：
#   「章节序号只支持阿拉伯数字」 -- 章节号没写进去（fill 第一次未生效）
#   「正文至少输入1000字」       -- 正文没写进去/字数不足
# 这些文案不含 _TOAST_ERROR_RE 的任何关键词，状态机不专门识别就会空转 14 轮 +
# 15s 误报"发布设置超时"，真实原因（哪个字段没填好）丢失。
_EDITOR_VALIDATION_RE = re.compile(
    r"只支持阿拉伯数字|序号[^，。]{0,6}数字"
    r"|至少[^，。]{0,4}\d|字数不足|不能为空"
    r"|请输入(标题|正文|章节|内容)")


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


def _interpret_publish_response(body: str):
    """解析提交类接口响应 body，返回 (verdict, message)。

    覆盖两个接口（两者的业务结果都在 200 响应的 JSON `code` 里）：
      · /api/author/publish_article/v0/   新建/修改内容的提交
      · /api/author/article/modify_timer/v0/  改期

    业务结果藏在 HTTP 200 的 JSON `code` 字段（2026-06-26 真机抓包实测）：
      成功 {"code":0,"data":{"item_id":"...","tips":""},"message":"success"}
      失败 {"code":-3026,"data":null,"message":"文章内容有大段落重复，请修改后提交"}
    这是比"确认发布按钮消失"更权威的信号——按钮消失区分不了 code!=0 的被拒
    （编辑被拒时对话框可能也关、按钮也消失），会把失败误报成"成功"。

    verdict ∈ {'success','daily_limit','fail'}；body 无法解析或不含 code 时返回
    (None, '')，调用方回退到原有按钮/ toast 判定，保持向后兼容。code 兼容 int 0
    与字符串 "0"（防平台序列化差异）。失败文案命中每日上限正则时归为
    'daily_limit'，让上层中止整批、记录剩余章节，而非整章重试。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return (None, "")
    if not isinstance(data, dict) or "code" not in data:
        return (None, "")
    code = data.get("code")
    msg = data.get("message") or data.get("msg") or ""
    if not isinstance(msg, str):
        msg = str(msg)
    msg = msg.strip()
    if code in (0, "0"):
        return ("success", msg)
    if _DAILY_LIMIT_RE.search(msg):
        return ("daily_limit", msg)
    return ("fail", msg or f"接口 code={code}")


async def _check_daily_limit(page):
    """检测平台"当日发布字数上限"toast，若存在则抛出 DailyLimitReached。

    只扫 toast 元素、不做全页文本匹配，避免误中正文内容。
    """
    toasts = await _visible_toast_texts(page)
    for t in toasts["messages"] + toasts["notifications"]:
        if _DAILY_LIMIT_RE.search(t):
            raise DailyLimitReached(f"当日发布字数已达上限: {t}")


async def _check_editor_validation(page):
    """检测编辑器字段校验失败 toast（章节号非数字/正文字数不足等），命中即抛错。

    只扫瞬态 message（不扫常驻 notification，避免公告误杀）。带上平台原文，
    让真实原因可见、并让上层快速重试，而不是空转到"发布设置超时"。
    """
    toasts = await _visible_toast_texts(page)
    for t in toasts["messages"]:
        if _EDITOR_VALIDATION_RE.search(t):
            raise RuntimeError(f"章节字段校验未通过，页面提示: {t}")


# 退出码契约（计划任务/cron 读它）: 0=正常、1=崩溃、3=跑完了但需要人处理。
# 定义在这里而不是各工具里：退出码是对外契约，而真正 exit 的只有
# run_unattended 这一处。各工具曾各自定义一份，外壳合并后它们就成了死常量。
EXIT_NEEDS_ATTENTION = 3


def run_unattended(main_async, args, *, log_dir, name, hint="",
                   readonly=False):
    """跑一个工具的 main_async，带上无人值守该有的一切。

    --daily 时: 接管日志到文件、崩溃弹窗兜底、需人工处理时弹窗并以退出码 3 退出。
    非 --daily 时: 给 logger 挂控制台 handler，否则过程日志一条都看不见。

    这套外壳 remap / keep_ahead / 主 CLI 曾各写一遍——它恰恰是最不能有分歧的
    地方: 定时任务没人看着，哪一份漏了崩溃兜底，就会一天天静默失败。
    """
    log_path = None
    if getattr(args, "daily", False):
        if not readonly:      # 只读子命令（audit）绝不能被置成真改写
            args.run = True
        args.headless = True
        log_path = start_task_log(log_dir, name)
    else:
        setup_logging()
    try:
        attn = asyncio.run(main_async(args))
    except Exception as e:
        if getattr(args, "daily", False):
            import traceback
            traceback.print_exc()
            alert(f"番茄{name}今天没跑成",
                  f"运行中断: {e}\n\n请尽快手动补跑。{hint}\n日志: {log_path}")
            sys.exit(1)
        raise
    if attn and getattr(args, "daily", False):
        why = ""
        try:
            for line in Path(log_path).read_text(encoding="utf-8").splitlines():
                if "需要人工处理:" in line:
                    why = line.split("需要人工处理:", 1)[1].strip()
        except Exception:
            pass
        alert(f"番茄{name}告警",
              "需要你处理：\n\n" + (why or "详见日志") + f"\n\n日志: {log_path}")
    if attn:
        sys.exit(EXIT_NEEDS_ATTENTION)


def resolve_target(args=None):
    """定时作业/工具的共同入口参数: (book_id, content_dir)。

    book_id 缺省取 .gui_state.json 的 last_book_id，章节目录取 config.json 的
    chapters_dir——但**无人值守作业强烈建议显式传**: 这两个值会跟着 GUI 里
    "上次选的作品/目录"漂，切一次作品就可能让定时任务写到另一本书上。
    """
    book_id = getattr(args, "book_id", None) if args else None
    content_dir = getattr(args, "content_dir", None) if args else None
    if not book_id and GUI_STATE_FILE.exists():
        try:
            book_id = json.loads(
                GUI_STATE_FILE.read_text(encoding="utf-8")).get("last_book_id")
        except Exception:
            pass
    if not content_dir:
        content_dir = load_config().get("chapters_dir")
    return book_id, content_dir


def local_chapter_index(content_dir):
    """本地目录的 章号 -> 文件路径。同号取首个（与修改内容模式规则一致）。

    复用 get_md_files + parse_md_file，覆盖 .md/.txt、子目录，以及
    第X章/回/节/话、中文数字、数字前缀、chapter-N 等全部既有命名。
    """
    index = {}
    for f in get_md_files(Path(content_dir)):
        cnum, _title, _content = parse_md_file(f)
        if cnum is None:
            continue
        try:
            index.setdefault(int(cnum), str(f))
        except (TypeError, ValueError):
            continue
    return index


def tool_startup(args):
    """三个工具共同的启动序幕: 定位目标 → 建本地章节索引 → 决定 headless。

    返回 (book_id, num2path, headless)；定位不到目标时打印提示并返回
    (None, None, None)。退出动作留在调用方——三个工具的返回值语义不同
    （False / None / True 各有含义），这里只统一"怎么定位、怎么报"。
    """
    book_id, content_dir = resolve_target(args)
    if not book_id or not content_dir:
        print("缺 book_id 或 content_dir，请用 --book-id/--content-dir 指定")
        return None, None, None
    num2path = local_chapter_index(content_dir)
    print(f"本地章节文件 {len(num2path)} 个  |  book_id {book_id}", flush=True)
    return book_id, num2path, resolve_headless(args)


def book_mismatch_abort(items, num2path):
    """内容归属校验 + 统一的中止提示。返回 True 表示不匹配、必须中止。

    book_id 和章节目录都可能来自 GUI 的"上次选择"，切过作品就会指向另一本
    书。真写入前必须确认这批稿子确实属于这本书——写错书是补不回来的。
    """
    ok, detail = verify_content_matches_book(items, num2path)
    print(f"归属校验: {detail}", flush=True)
    if not ok:
        print("⚠ 已中止，未做任何改动。请用 --book-id/--content-dir "
              "显式指定，或检查 config.json 的 chapters_dir。", flush=True)
    return not ok


def resolve_headless(args=None):
    """无头设置: config 的 headless 打底，--headless / --show-browser 覆盖。"""
    headless = load_config().get("headless", False)
    if getattr(args, "headless", False):
        headless = True
    if getattr(args, "show_browser", False):
        headless = False
    return headless


async def reconcile_batch_auto(page, book_id, claimed_nums, fail_list,
                               *, is_draft):
    """批次收尾对账的统一入口：按模式挑对账方式，返回漏掉的章号。

    发布类走章节列表对账，存草稿走草稿箱接口对账（后者覆盖"草稿ID读不到"
    的推断盲区）。调用方拿返回值修正成功/失败计数——CLI 和 GUI 曾各写一遍
    这段选择逻辑，加一种模式就得改两处。
    """
    if is_draft:
        return await reconcile_drafts_after_batch(
            page, book_id, claimed_nums, fail_list)
    return await reconcile_after_batch(page, book_id, claimed_nums, fail_list)


# 两套格式集是故意不同的，别合并:
#   筛选允许纯日期（"某天之后改过的"，按当天 00:00 算是对的）
#   定时必须带时分（只给日期会被静默当成凌晨 00:00 启动，是事故）
# 不同的只是格式集，解析循环本身只能有一份 —— 曾经有三份。
TIME_SPEC_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")
TIMER_INPUT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def parse_datetime(raw, formats):
    """按给定格式依次尝试解析，返回 datetime；都不匹配返回 None。"""
    raw = (raw or "").strip()
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_time_spec(raw):
    """解析 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"，返回时间戳；非法返回 None。

    与 GUI「按修改日期筛选」同一套语义: 只给日期时按当天 00:00 算。
    """
    dt = parse_datetime(raw, TIME_SPEC_FORMATS)
    return dt.timestamp() if dt is not None else None


def parse_chapter_spec(raw):
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


def filter_by_chapter_spec(items, spec, key=lambda x: x):
    """按章节号表达式筛选。spec 支持 "30" / "5-10" / "1,3,5-10" / "≥30" / "<=30"。

    与 GUI「按章节号筛选」同一套解析（parse_chapter_spec），所以补传清单里
    压缩出来的章节号可以直接粘到 CLI 的 --chapters 上——CLI 曾经只在日志里
    教用户"粘贴到按章节号筛选"，自己却没有这个入口。
    返回 (筛选后的 items, 是否生效)。spec 非法时抛 ValueError。
    """
    if not spec:
        return items, False
    raw = unicodedata.normalize("NFKC", str(spec)).strip()
    if not raw:
        return items, False
    op = None
    m = re.match(r"^(≤|<=|≥|>=|<|>)\s*(\d+)$", raw)
    if m:
        op, raw = m.group(1), m.group(2)
    if raw.isdigit():
        n = int(raw)
        # op 为 None = 裸数字，精确匹配那一章（不是阈值）。
        # < 和 > 是严格的，不能并进 <= / >=：在不可逆的创建批次里，
        # 「--chapters "<30"」多带一章第30章 = 多发一章用户明确排除的章节。
        _OPS = {
            None: lambda v: v == n,
            "≤": lambda v: v <= n, "<=": lambda v: v <= n, "<": lambda v: v < n,
            "≥": lambda v: v >= n, ">=": lambda v: v >= n, ">": lambda v: v > n,
        }
        _hit = _OPS[op]
        kept = [x for x in items
                if (_v := _spec_int(key(x))) is not None and _hit(_v)]
        return kept, True
    intervals = parse_chapter_spec(raw)
    if not intervals:
        raise ValueError(f"章节号筛选格式错误: {spec}（应为 30 / 5-10 / 1,3,5-10）")
    kept = [x for x in items
            if (v := _spec_int(key(x))) is not None
            and any(lo <= v <= hi for lo, hi in intervals)]
    return kept, True


def _spec_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def compress_chapter_nums(nums) -> str:
    """把章节号集合压缩成筛选表达式: [79,80,81,83] -> "79-81,83"。

    输出与 GUI「按章节号筛选」的组合写法完全兼容，可直接粘贴补传。
    CLI 的补传清单、GUI 的体检报告都用这一份——曾经两边各写一份逐字相同的
    实现，属于"改一边漏一边"的典型温床。
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
    out = ",".join(parts)
    # 只有一章失败时输出会是裸数字，而裸数字在两个入口含义不同:
    # CLI 的 --chapters 83 = 只第83章；GUI 的「按章节号筛选」会把单值跟下拉框
    # 的 ≤/≥ 拼起来，默认就成了 ≥83 = 第83章到末尾。这串号是发给用户直接
    # 粘贴去补传的——在新建类模式下粘错了会把后面所有章再发一遍，
    # 而番茄只能往书尾追加，那些重复章永远移不回去。
    # 写成 83-83（区间）两边都是集合命中，语义才真的一致。
    if out.isdigit():
        out = f"{out}-{out}"
    return out


# 连续同一原因的条目超过这么多章就折成一行。中止整批时剩余章节会被逐章记入
# 清单（要的是补传章节号），但逐条打出来就是几千行同一句"前方中止，未处理"——
# 真正的失败原因被冲到屏幕外了。折叠只影响显示，记账与末尾的章节号压缩不变。
_FAIL_RUN_COLLAPSE = 3


def log_fail_list(fail_list):
    """批量结束时打印失败章节及原因清单（上传 / 修改 / 改期共用，CLI+GUI）。

    连续同一原因的条目会折成一行（见 _FAIL_RUN_COLLAPSE）——中止整批时剩余
    章节是逐章记进来的，不折叠就是几千行同一句"前方中止，未处理"，唯一有
    信息量的那条真实原因反而被冲出屏幕。
    末尾追加按筛选语法压缩的失败章节号（如 "79-81,83-114"）。同一串号在两个
    入口都能直接用: GUI 粘进「按章节号筛选」，CLI 传给 --chapters。
    单章会输出成 "83-83" 而不是 "83"：裸数字在 GUI 会跟 ≤/≥ 下拉框拼成阈值，
    两边含义就不一样了（见 compress_chapter_nums）。
    """
    if not fail_list:
        return
    logger.info("  失败章节及原因:")
    i, n = 0, len(fail_list)
    while i < n:
        reason = fail_list[i][1]
        j = i
        while j + 1 < n and fail_list[j + 1][1] == reason:
            j += 1
        run = j - i + 1
        if run > _FAIL_RUN_COLLAPSE:
            logger.info(f"    - {fail_list[i][0]} … {fail_list[j][0]}"
                        f"（共 {run} 章）: {reason}")
        else:
            for label, r in fail_list[i:j + 1]:
                logger.info(f"    - {label}: {r}")
        i = j + 1
    nums = []
    for label, _ in fail_list:
        m = re.match(r"第(\d+)章", label)
        if m:
            nums.append(int(m.group(1)))
    if nums:
        logger.info(
            f"  失败章节号: {compress_chapter_nums(nums)}"
            f"（补传: GUI 粘进「按章节号筛选」，命令行加 --chapters）")


def record_unprocessed(fail_list, remaining, reason="每日字数上限，未处理"):
    """中止整批后，把剩余未处理章节如实记入失败清单（不静默丢弃）。

    remaining: 可迭代的 (章节号, 标题) 二元组，章节号可为 None/空字符串。
    reason: 记入清单的原因文案（字数上限 / 流程异常中止 / 用户取消等）。
            撞上限时用 limit_label 生成，别一律写"每日"——每月额度耗尽时
            "明天接着发"是错的。
    返回追加的条数，供上层据此累加 failed 计数，使「成功+失败=总数」对得上。

    这些条目带"第N章"标签，会被 log_fail_list 末尾的章节号压缩收进去，
    用户把那串号粘进 GUI 的「按章节号筛选」或 CLI 的 --chapters 就能接着发——
    这正是"记录剩余"的落点。
    """
    n = 0
    for ch_num, title in remaining:
        label = f"第{ch_num}章 " if ch_num else ""
        fail_list.append((f"{label}{title}", reason))
        n += 1
    return n


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
_SUBMIT_CONFIRM_GRACE_S = 5.0  # 按钮消失后等待「提交确认」信号（接口 code /
                               # 页面导航离开编辑器）的宽限窗


def _is_publish_success_url(url) -> bool:
    """判断 URL 是否是「提交成功后应落到」的页面：章节管理页。

    只认 chapter-manage，不认"任意非编辑器页"——会话中途掉线会跳 /login，
    /login 也离开了编辑器，若把它当成功就又制造静默漏章（正是本次要消灭的
    bug 类）。故成功导航必须是显式的 chapter-manage，掉登录页/错误页一律
    不算成功、按未提交处理触发重试。

    曾经旁边还有个 _is_editor_url（"URL 还在 /publish 就算编辑器页"），
    2026-08-30 复查时已无任何调用方——它的语义正是上面那段警告要防的
    "任意非编辑器页都算成功"，留着只会被人顺手捡起来重演漏章，故删除。
    """
    return "chapter-manage" in (url or "")


async def _await_submit_confirmation(page, verdict_holder, *,
                                     grace_s: float = _SUBMIT_CONFIRM_GRACE_S):
    """「确认发布」按钮消失后，等待提交真正落地的确认信号。

    按钮消失 ≠ 提交成功：对话框被异常关闭（Escape 残留、点击被吞后 DOM
    重建、弹窗抢焦点）时按钮同样消失，而章节根本没提交。2026-07-24 实测
    该假成功让 748 章的定时发布批量漏掉 151 章——日志全记"成功"，平台上
    却无此章，正文只留在自动草稿里（草稿箱大量堆积是同一根因的另一面）。

    确认信号二选一（宽限窗内轮询）：
      a) publish_article 接口 code 判定已写入 verdict_holder（响应可能在途，
         这也顺带修掉了"按钮消失抢在 body 补抓完成之前返回"的竞态）；
      b) 页面已导航到 chapter-manage（实测提交成功后 SPA 跳回章节管理页；
         修改排期流程本就在 chapter-manage 页上弹窗，天然立即满足）。
         只认 chapter-manage：会话掉线跳 /login 也离开了编辑器，但绝不能
         当成功——否则又是静默漏章。见 _is_publish_success_url。
    返回 verdict（'success'/'fail'/'daily_limit'）或 'navigated'；
    宽限窗耗尽仍无任何信号返回 None，调用方按未提交处理（宁可失败重试，
    重复章可见可删，静默漏章不可见——151 章即为代价）。
    """
    deadline = time.monotonic() + grace_s
    while True:
        v = verdict_holder.get("verdict")
        if v:
            return v
        try:
            url = page.url
        except Exception:
            url = ""
        if _is_publish_success_url(url):
            return "navigated"
        if time.monotonic() >= deadline:
            return None
        await page.wait_for_timeout(_PUBLISH_POLL_MS)


async def _wait_publish_result(page, confirm_btn, *, timeout: int | None = None):
    """点击「确认发布」后判定发布结果，每 200ms 轮询，按真实时钟控制超时。

    判定规则（实测）:
    - publish_article 接口回 code==0 → 成功；code!=0 → 失败/上限（权威信号，
      优先于按钮，见 _interpret_publish_response）
    - 按钮消失 → 进入确认宽限窗（_await_submit_confirmation）：等接口 code
      或页面导航离开编辑器；两者皆无 → 判未提交失败。按钮消失本身不再
      直接算成功——对话框被异常关闭时按钮同样消失而章节没提交
      （2026-07-24 定时发布批量漏 151 章的根因）
    - toast 含上限文案 → 抛 DailyLimitReached（上层中止整批、记录剩余章节）
    - 瞬态 toast 含失败/错误等 → 立即抛 RuntimeError（不再傻等超时）
    - 按钮在、且连续 5s 无瞬态 toast（常驻公告不算响应）→ 疑似点击被
      遮挡/吞掉（与"下一步"被吞同类问题），自愈：限时重点击；
      仅在剩余预算 ≥ 点击+观察窗时才发起，避免点完即超时的无效点击
    - 超时按钮仍在 → 抛 RuntimeError，注明期间有无页面提示
    - 按钮可见性检测出错 → 状态未知，继续轮询（不当成功）

    平台失败提示（上限/敏感词等）是 Arco Message toast，~3 秒自动消失，
    必须趁还在时捕获。所有捕获到的 toast 文本都写入日志。

    接口判定 2026-06-26 真机抓包接入：提交最终调 /api/author/publish_article/v0/，
    业务结果在 200 响应的 JSON `code` 里（按钮消失区分不了 code!=0 的被拒）。

    注意本函数只是**单章实时判定**，依据是页面/接口层面的信号。批次收尾还有
    一道 reconcile_after_batch：拿平台真实章节列表核对"日志记成功的章"是否
    真的存在。两道都要有——2026-07-24 漏 151 章那次，日志全程"成功"。
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
    # 提交接口的权威判定结果（由 publish_article 响应 body 的 code 解析得到）。
    # 一旦填入即优先于"按钮消失"启发式——见下方主循环。
    verdict_holder: dict = {}

    def _on_response(resp):
        try:
            url = resp.url
            if len(api_probe) < 20 and re.search(
                    r"draft|publish|chapter|submit|create|article", url, re.I):
                idx = len(api_probe)
                api_probe.append(f"{resp.status} {url[:160]}")
                # 提交接口(实测 /api/author/publish_article/v0/，业务结果藏在
                # 200 响应的 JSON `code` 里)——异步补抓 body，解析 code 写入
                # verdict_holder 作为权威判定信号。
                # 改期走 /api/author/article/modify_timer/v0/，业务结果同样在
                # 200 响应的 code 里。不认它的话改期只剩"按钮消失"这一个启发式：
                # 2026-08-20 改期 609 章时，第1396章 接口已 200、toast 已"修改成功"，
                # 只因按钮没消失被判失败，还白重试两次（去改一个已改好的章，
                # 反而引出"服务器开小差了"）。平台真实排期核对确认那次是成功的。
                if "publish_article" in url or "modify_timer" in url:
                    async def _grab(i=idx, r=resp):
                        try:
                            full = await r.text()
                            api_probe[i] += f" body={full[:300]}"
                            verdict, vmsg = _interpret_publish_response(full)
                            if verdict and "verdict" not in verdict_holder:
                                verdict_holder["verdict"] = verdict
                                verdict_holder["message"] = vmsg
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
            # 接口响应是权威信号，优先于"按钮消失"启发式：编辑被拒(code!=0)时
            # 对话框可能也关、按钮也消失，仅看按钮会把失败误报成"成功"。
            iv = verdict_holder.get("verdict")
            if iv == "success":
                return
            if iv == "daily_limit":
                raise DailyLimitReached(
                    f"当日发布字数已达上限: {verdict_holder.get('message', '')}")
            if iv == "fail":
                raise RuntimeError(
                    f"发布失败，接口返回: {verdict_holder.get('message', '')}")
            try:
                visible = await confirm_btn.is_visible()
            except Exception:
                visible = None  # 状态未知（页面跳转/上下文销毁等），不能当成功
            if visible is False:
                # 按钮消失只是必要条件——还需接口 code=0 或页面导航离开
                # 编辑器确认提交真正落地，否则按未提交失败（触发上层重试）。
                # 详见 _await_submit_confirmation（2026-07-24 定时发布漏 151 章根因）。
                outcome = await _await_submit_confirmation(page, verdict_holder)
                if outcome in ("success", "navigated"):
                    return
                if outcome == "daily_limit":
                    raise DailyLimitReached(
                        f"当日发布字数已达上限: {verdict_holder.get('message', '')}")
                if outcome == "fail":
                    raise RuntimeError(
                        f"发布失败，接口返回: {verdict_holder.get('message', '')}")
                if api_probe:
                    # 输出窗口期接口响应，供排查定时发布实际命中的提交端点
                    logger.info(f"    窗口期接口响应: {'; '.join(api_probe[:8])}")
                raise RuntimeError(
                    "确认发布按钮已消失但提交未获确认"
                    "（无接口 code=0 响应且页面仍停留在编辑器）——"
                    "对话框疑似被异常关闭，按未提交处理")
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
    标题提取优先级:  首行 # 标题(去前缀) > 文件名(去前缀)

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

    # 标题只认首行的 "# "（text 读入时已 strip，首行即首个非空行）。
    # 不能扫全文找第一个 "# "：正文中部的 "# 场景X" 会被当成标题，
    # 且它之前的全部正文被静默丢弃——发布出去的章节缺前半截。
    if lines:
        first = lines[0].strip()
        if first.startswith("# "):
            heading = first[2:].strip()
            content_start = 1

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
    整段崩掉——GUI 在隐藏控制台（run.bat 后台静默启动）/pythonw 下无可见报错（刷新像没反应），且崩溃点之后
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
    # 移除 HTML 标签——只认真实标签形状（字母/斜杠开头、不跨行）。
    # 宽松的 <[^>]+> 会把正文里成对颜文字 (>_<)…(>_<) 或散落的 < … >
    # 之间的内容当成"标签"整段删除（[^>] 还能匹配换行，可跨段误删几千字）
    text = re.sub(r"</?[a-zA-Z][^>\n]*>", "", text)
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


async def settle_page(page, timeout=None):
    """给页面一个「碰巧很快静下来就用上」的短窗口，静不下来立刻返回。

    番茄的埋点/轮询一直在发请求，作家后台**永远不会**进入 networkidle —— 每次
    都必然走到超时。Playwright 默认 30 秒，于是「正在获取章节列表…」这类状态
    每次白等 30 秒。真正的就绪判据是调用方后面的 wait_for_selector / 提取 JS
    自带的表格等待，所以这里超时不是错误，是常态。

    各调用点原来各写一遍这个 try/except，改一次上限就得挨个改。
    """
    try:
        await page.wait_for_load_state(
            "networkidle", timeout=timeout or NETWORKIDLE_MS)
    except PWTimeout:
        pass


def _at_target_path(current_url, target_url) -> str:
    """current_url 是否已经到了 target_url 那个页（只比 path，不比 query）。

    宽松匹配: 目标 path 是当前 path 的前缀即可——SPA 可能在后面追东西，
    但不会把你送到一个不相干的 path。宁可放过一个变形，也不能把正常导航判成失败。

    真机实测(2026-08-21): 番茄会在 path 后面拼上 **&书名**（URL 编码），落地后是
        /main/writer/chapter-manage/7613749318914149401&%E8%AF%B8%E5%A4%A9...
    前一版只允许后面跟 "/"，于是把这种正常导航判成失败，连带批末对账
    那道安全网一起挂掉（且报成“会话失效”，而登录好好的）。所以分隔符要包含 & 。
    """
    from urllib.parse import urlsplit
    cur = urlsplit(current_url or "").path.rstrip("/")
    want = urlsplit(target_url or "").path.rstrip("/")
    if not want:
        return False
    if cur == want:
        return True
    # 要求紧跟着一个分隔符，而不是裸 startswith：否则 …/123 会匹配到 …/1234。
    return cur.startswith(want) and cur[len(want):len(want) + 1] in ("/", "&", "?", "#")


async def goto_with_login_retry(page, url, *, wait_until="load"):
    """打开作家后台页面，并区分"会话真失效"与"误跳登录页"。

    实测(2026-06-10): 机器高负载时（如另一个自动化程序占满 CPU/网络），
    作家后台 SPA 的鉴权请求（/api/user/info 等）超时，前端会把仍然有效的
    会话误判为未登录并跳转 /login。重试一次即可区分：瞬态失败第二次就能
    进入目标页，真失效则两次都被重定向。

    返回 True=已进入目标页；False=没进去（被重定向到登录页，或两次都没到目标页）。

    goto 超时本身不算失败（页面可能已部分加载），但**必须确认真的到了目标页**：
    自从 goto 加上超时上限后，它可能在导航**提交之前**就超时，此时 page.url
    还停在上一个页面——而“不在登录页”并不等于“到了目标页”。早先只看
    /login 的写法会在旧页面上报成功，调用方接着就在错页上干活：
    fetch_chapter_items 拿到陈旧签名或直接报“没抓到 chapter_list”，而后者在
    run_creation_batch 里等于整批静默关掉逐章防漏章的守卫。

    注意: SPA 的鉴权跳转是异步的——domcontentloaded 时 URL 往往还停在
    目标页，立刻检查会漏掉随后才发生的 /login 跳转（实测如此）。所以
    每次导航后留一个观察窗，等跳转发生或确认没有跳转再下结论。
    """
    for attempt in (1, 2):
        try:
            # 必须给上限: wait_until="load" 要等所有子资源，而平台的埋点/广告连接
            # 可能一直不结束；超时又在下面被吞掉，于是每次白等 Playwright 默认的
            # 30 秒。本函数本来就"以最终 URL 为准、不把超时当失败"，短上限无损。
            await page.goto(url, wait_until=wait_until,
                            timeout=get_browser_timeout())
        except PWTimeout:
            pass
        if "/login" not in page.url:
            try:
                await page.wait_for_url("**/login**", timeout=4000)
            except PWTimeout:
                pass  # 观察窗内没跳登录页 = 真的进来了
        if "/login" not in page.url:
            # 到没到目标页看 path（忽略 query：SPA 会自己改 query）。
            if _at_target_path(page.url, url):
                return True
            if attempt == 1:
                await page.wait_for_timeout(1000)
                continue
            logger.error(
                f"  导航未到达目标页（现在在 {page.url}）——多为 goto 在提交前超时，"
                f"可适当调大 config.json 里的 browser_timeout")
            return False
        if attempt == 1:
            await page.wait_for_timeout(3000)
    return False


async def save_auth(context) -> bool:
    """保存当前登录状态（原子写：tmp+rename，防进程中断留下半截 JSON）。

    保存失败不应影响本次任务结果，只告警。storage_state 走 CDP，
    浏览器挂死时会无限悬停，故加超时（超时走同一条告警路径）。

    返回是否保存成功——登录流程必须检查：失败时 AUTH_FILE 还是旧账号的
    会话，若照常复制成命名账号文件，会把旧账号 cookie 静默挂到新账号名下。
    其余调用方（任务收尾的顺手保存）可忽略返回值。
    """
    tmp = AUTH_FILE.with_suffix(AUTH_FILE.suffix + ".tmp")
    try:
        state = await asyncio.wait_for(context.storage_state(), timeout=30)
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(AUTH_FILE)
        return True
    except Exception as e:
        logger.warning(f"保存登录状态失败(不影响本次结果): {e}")
        # 清掉残留 tmp（与 GUI _atomic_write_json 对称）：它含完整登录
        # cookie，留在目录里有被 git add . 连带提交的泄露风险
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


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
    # networkidle 只当"碰巧静下来就用上"的加速，静不下来不是错误（settle_page
    # 的注释里写了原因: 番茄的埋点/轮询让作家后台永远不进 networkidle）。
    # 这里原来是硬等且超时即抛——2026-08-30 第1385章就是这么挂的: 前一次尝试
    # 留下弹窗后页面一直有请求，两次重试都在这一行超时（15000ms），
    # 连"正文字数"都没打出来，最后 3710 章全部未处理。
    # 真正的就绪判据是下面两个选择器。
    await settle_page(page)
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
        # 字数远超粘贴内容 = 编辑器里有未清空的旧内容（清空被弹窗抢焦点等），
        # 拼接稿（约 2 倍字数）绝不能发布出去；1.5 倍+100 的余量足以容纳
        # 平台字数口径差异，又必拦截旧+新拼接
        limit = int(len(plain_content) * 1.5) + 100
        if wc > limit:
            raise RuntimeError(
                f"正文字数异常({wc}，预期约 {len(plain_content)})，"
                f"疑似旧内容未清空被拼接，请重试")
        logger.info(f"    正文字数 {wc}")
    else:
        raise RuntimeError("正文粘贴失败 (字数=0)，请重试")

    # 章节号回读校验：第一次进编辑器时章节号输入框可能尚未就绪/被 React 回灌，
    # 写入静默失效 → 章节号留空 → 发布时平台弹"章节序号只支持阿拉伯数字"，
    # 整章超时重试才偶然成功。这里像正文一样回读验证，没写进去就单独重写该字段
    # （nativeSetter 是覆盖赋值、不会与正文一样拼接），自愈而非把失败拖到发布阶段。
    if chapter_num:
        for _ in range(4):
            got = await page.evaluate(_READ_CHAPTER_NUM_JS)
            if got == str(chapter_num):
                break
            await page.evaluate(_WRITE_CHAPTER_NUM_JS, str(chapter_num))
            await page.wait_for_timeout(400)
        else:
            got = await page.evaluate(_READ_CHAPTER_NUM_JS)
            if got != str(chapter_num):
                raise RuntimeError(
                    f"章节号未能写入(读到 {got!r}，应为 {chapter_num})，请重试")


# 章节号输入框：编辑器里第一个可见的、非"标题"的文本输入框（与 fill 写入同口径）。
_READ_CHAPTER_NUM_JS = r"""() => {
    for (const inp of document.querySelectorAll('input')) {
        if (inp.type === 'text'
            && inp.placeholder !== '请输入标题'
            && inp.offsetParent !== null) {
            return inp.value || '';
        }
    }
    return null;  // 没有可见的章节号输入框（尚未就绪）
}"""

_WRITE_CHAPTER_NUM_JS = r"""(num) => {
    const nativeSetter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype, 'value').set;
    for (const inp of document.querySelectorAll('input')) {
        if (inp.type === 'text'
            && inp.placeholder !== '请输入标题'
            && inp.offsetParent !== null) {
            nativeSetter.call(inp, num);
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
    }
    return false;
}"""


def _extract_draft_id(url: str) -> str | None:
    """从编辑器 URL 提取草稿 ID（.../publish/<id>?...）；全新空白章无 ID 时返回 None。

    存草稿时番茄有时把"新建章"复用到同一个进行中的草稿上（URL 落到同一个
    /publish/<id>），导致连续章节互相覆盖、每隔一章丢章（实测确认）。上层据此
    检测复用：两章落到同一 draftId ⇒ 先存的那章已被后一章覆盖、未独立保存，
    必须如实记入补传清单，绝不"报存成功却实际丢章"。
    """
    m = re.search(r"/publish/(\d+)", url or "")
    return m.group(1) if m else None


async def save_draft(page):
    """点击存草稿按钮并轮询保存结果。

    不能只等"已保存"——番茄编辑器有常驻的自动保存指示器也含"已保存"字样，
    `wait_for_selector("text=已保存")` 会立即命中它，把"字段校验被拒、本次其实
    没存成"误判为成功（实测：存草稿计数虚高，报存了 2 章实际只存 1 章）。
    故每轮先扫错误/校验 toast（命中即抛真实原因），再看是否出现保存确认。
    """
    save_btn = page.locator("button", has_text="存草稿")
    if await save_btn.count() == 0:
        raise RuntimeError("未找到存草稿按钮")
    await save_btn.first.click()
    deadline = time.monotonic() + _browser_timeout / 1000
    saved = False
    while time.monotonic() < deadline:
        toasts = await _visible_toast_texts(page)
        for t in toasts["messages"]:
            if _EDITOR_VALIDATION_RE.search(t) or _TOAST_ERROR_RE.search(t):
                raise RuntimeError(f"存草稿失败，页面提示: {t}")
        if await page.locator("text=已保存").count() > 0:
            saved = True
            break
        await page.wait_for_timeout(300)
    if not saved:
        logger.warning("未检测到保存确认，草稿可能未保存成功")
    await page.wait_for_timeout(500)


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
    """清空编辑器中的标题和正文内容（修改模式用）。

    清空靠键盘全选+删除，会被晚到的提示弹窗（如「请在发布时间前30分钟…」
    的"我知道了"）抢走焦点而静默失效——旧正文残留，fill_chapter 的 DOM
    粘贴不受弹窗影响，结果把"旧+新拼接稿"发布出去（2026-06-12 线上实证；
    此前该错误被弹窗挡"下一步"的超时重试掩盖，弹窗自动点掉后掩护消失）。
    故清空后必须校验字数归零：未归零则点掉弹窗重试，仍失败抛错交上层
    整章重试，绝不静默放行。
    """
    for _attempt in range(3):
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
        # 校验清空生效（字数指示器有刷新延迟，短轮询；正常路径首查即 0）
        wc = await _get_word_count(page)
        for _ in range(4):
            if wc == 0:
                break
            await page.wait_for_timeout(500)
            wc = await _get_word_count(page)
        if wc == 0:
            return
        # 未清掉：大概率是弹窗抢了键盘焦点——点掉已知的"我知道了"再试
        try:
            ack = page.locator("button", has_text="我知道了")
            if await ack.count() > 0 and await ack.first.is_visible():
                await ack.first.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass
    raise RuntimeError("清空编辑器失败(疑似弹窗抢占焦点)，请重试")


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

            // 章节号。裸数字兜底带前视守卫，与本地侧 _extract_chapter_num
            // 对称——否则 "2024新春番外" 这类数字开头的特殊章节会被误编号，
            // 在按章节号筛选/匹配时被错误纳入（排期错位、内容写错章）
            let chapterNum = null;
            let m = title.match(/^第\s*(\d+)\s*[章回节话]/);
            if (m) chapterNum = parseInt(m[1], 10);
            else {
                m = title.match(/^(\d+)(?=$|[\s:：_\-.、章回节话])/);
                if (m) chapterNum = parseInt(m[1], 10);
            }

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

            // 发布/排期日期（修改排期要用；「检查缺口」据此判断已发布章
            // 是否还在 3 天可移动窗口内）
            const dm = row.textContent.match(dateRe);
            const rowDate = dm ? dm[1].replace(/\//g, '-') : null;
            const rowTime = dm ? dm[2] : null;

            allChapters.push({ title, chapterNum, editUrl, status,
                               date: rowDate, time: rowTime,
                               rowIndex: allChapters.length });
            newCount++;

            if (dm) {
                const pk = rowDate + ' ' + rowTime;
                if (pk > lastPubKey) {
                    lastPub = { date: rowDate, time: rowTime, chapter: title };
                    lastPubKey = pk;
                }
            }
        }

        pageCount++;
        // 章节多的书要翻十几页，逐页回报进度，界面才不会看着像卡死。
        // 没暴露这个函数的调用方（CLI）自然跳过。
        if (typeof __fanqiePageProgress === 'function') {
            try { await __fanqiePageProgress(pageCount, totalPages,
                                             allChapters.length); } catch (e) {}
        }
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
    page, book_id: str = "", on_progress=None,
) -> tuple[list[dict], dict | None]:
    """从章节管理页提取全部章节列表（单次 JS 调用完成全部翻页）。

    返回 (chapters, last_publish_info)。
    last_publish_info: {date, time, chapter} 或 None。

    on_progress(已翻页数, 总页数, 已抓章数): 可选。章节多的书要翻十几页、耗时十
    几秒，没有进度的话界面看着像卡死。CLI 不传，行为完全不变。
    """
    if on_progress is not None:
        try:
            await page.expose_function(
                "__fanqiePageProgress",
                lambda done, total, n: on_progress(done, total, n))
        except Exception:
            pass   # 同一个 page 上已暴露过（换卷时会复用），忽略即可
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
    used_nums: set[int] = set()
    dup_local: list[int] = []
    for i, (num, title, content) in enumerate(local_parsed):
        int_num = int(num) if num else None
        if int_num is not None and int_num in platform_map:
            if int_num in used_nums:
                # 本地重复章节号（多卷子文件夹各自从 1 编号 / 残留旧副本）：
                # 若都执行，会对同一平台章节先后写入两份内容、后写的静默
                # 覆盖前者——与平台侧重复处理对称，仅取首个，其余跳过
                dup_local.append(int_num)
                unmatched.append((i, num, title))
                continue
            used_nums.add(int_num)
            matched.append((i, platform_map[int_num], int_num, title, content))
        else:
            unmatched.append((i, num, title))
    if dup_local:
        logger.warning(
            f"本地存在重复章节号 {sorted(set(dup_local))}（多为分卷子文件夹"
            f"各自编号或残留旧副本）；按章节号匹配时仅取首个文件，其余跳过，"
            f"以免同一平台章节被后写的文件覆盖。")
    return matched, unmatched


async def click_next_step(page):
    """点击下一步按钮（进入发布流程）。"""
    # 精确定位发布按钮（class 含 publish-button），避开 React Tour 引导中的同名按钮
    # 点击限时 5s：唯一调用方是 _navigate_to_publish_settings 的轮询状态机，按钮被
    # 未知弹窗遮罩挡住时应快速失败回到轮询识别弹窗，而不是烧满 Playwright 默认 30s
    # （正常点击 <1s，5s 余量充足；超时后状态机下一轮会重试点击）。
    next_btn = page.locator("button.auto-editor-next")
    if await next_btn.count() > 0:
        await next_btn.click(timeout=5000)
    else:
        # 兜底：排除 React Tour 中的按钮
        next_btn = page.locator("button", has_text="下一步").locator(
            "visible=true"
        ).first
        await next_btn.click(timeout=5000)
    await page.wait_for_timeout(2000)


# ---------------------------------------------------------------------------
# 定时发布
# ---------------------------------------------------------------------------
def validate_times(raw: str) -> list[str]:
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
    times = validate_times(pub_time)
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


# ---------------------------------------------------------------------------
# 章节位置体检（CLI 工具 tools/remap 与 GUI「检查缺口」共用）
#
# 2026-08-20 实测: 番茄的目录顺序 = 章节 item 在卷内的位置（接口 chapter_list
# 的 index），既不是标题里的「第N章」（接口里根本没有章节号字段，编辑器那个框
# 只是拼进标题文本），也不是定时发布时间。新建章一律追加到全书末尾，网页端没有
# 任何插入/排序入口。已发布章只能在手机 App 里「申请→审批→单章选中→移动位置」，
# 且**只对发布 3 天内的章有效**，超期即永久错位。
# ---------------------------------------------------------------------------
DISPLAY_PUBLISHED = 1      # display_status: 已公开，网页端动不了
DISPLAY_PENDING = 10       # display_status: 待发布，可改内容
MOVE_WINDOW_H = 72         # App 里能申请移动的窗口（3 天）


def chapter_title_num(t):
    """平台章节标题里的章节号 —— 只是文本，不是平台的排序依据。

    判据与 _EXTRACT_ALL_JS 保持一致（两处都在解析同一批平台标题，不对称会让
    对账把「1 开端」这类裸数字标题当成"平台上没有第1章"而误报漏章）：
    先认「第N章/回/节/话」，再退到裸数字开头且后面是结尾/分隔符——后者的守卫
    是为了不把「2023年的夏天」误判成第 2023 章。
    """
    t = t or ""
    m = re.match(r"^第\s*(\d+)\s*[章回节话]", t)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)(?=$|[\s:：_\-.、章回节话])", t)
    return int(m.group(1)) if m else None


# 复用页面自己发出的已签名 chapter_list 请求，只换 page_index/page_count 翻页。
# 关键: 不能把"这一页没数据"一律当成翻完了。限流、鉴权失效、data=null 等异常
# 响应同样返回空 item_list，静默截断会让上层把"没抓到的章"当成"平台上没有"，
# 反过来制造大规模假漏章（对账要靠这份数据当真相，宁可报错也不能给半份）。
_FETCH_ITEMS_JS = r"""async (u) => {
    const out = [];
    const seen = new Set();
    let total = null;
    for (let pg = 0; pg < 60; pg++) {
        // replace 匹配不上是静默 no-op：那样每一轮都在取同一页（或服务端默认
        // 页大小），第 0 页短于预期就被当成"全书就这么多"。对账拿这份当真相，
        // 半份数据会把几百章报成"平台上没有"，用户照单补传就是几百章重复。
        if (!/page_index=\d+/.test(u) || !/page_count=\d+/.test(u)) {
            return {error: '签名 URL 里没有 page_index/page_count，无法翻页'};
        }
        const uu = u.replace(/page_index=\d+/, 'page_index=' + pg)
                    .replace(/page_count=\d+/, 'page_count=100');
        let r, j;
        try {
            r = await fetch(uu, {credentials: 'include'});
            j = await r.json();
        } catch (e) {
            return {error: '第' + pg + '页请求失败: ' + e};
        }
        if (!r.ok) return {error: '第' + pg + '页 HTTP ' + r.status};
        if (j && j.code !== undefined && j.code !== 0 && j.code !== '0') {
            return {error: '第' + pg + '页接口 code=' + j.code +
                           ' ' + (j.message || '')};
        }
        if (!j || !j.data) return {error: '第' + pg + '页响应无 data 字段'};
        if (typeof j.data.total === 'number') total = j.data.total;
        const list = j.data.item_list || [];
        if (!list.length) break;          // 真的翻完了
        // 按 item_id 去重再累加：既能拼出全量，也能发现"服务端没理会 page_index、
        // 每页都返回同一批"这种情况（下面 grew===0 就会停）。
        let grew = 0;
        for (const x of list) {
            if (x.item_id !== undefined && seen.has(x.item_id)) continue;
            if (x.item_id !== undefined) seen.add(x.item_id);
            out.push({index: x.index, title: x.title,
                display_status: x.display_status, timer_time: x.timer_time,
                create_time: x.create_time, item_id: x.item_id,
                cant_modify_reason: x.cant_modify_reason});
            grew++;
        }
        if (!grew) break;                 // 整页都是见过的 = 分页没生效，别空转
        // 这里**不能**用 list.length < 100 提前收工：平台会把页大小压到请求值
        // 以下（草稿接口实测 page_count>=50 就 code=-100），那样第 0 页就"短"，
        // 整本书只取到半份。对账拿这份当真相，半份数据会把几百章报成"平台上
        // 没有"，用户照单补传就是几百章永远移不回去的重复。多发一个空页请求
        // 是这里唯一划算的代价。
    }
    // 平台给了总数就必须对上——宁可报错也不能给半份（这是本文件的一贯原则）。
    if (total !== null && out.length < total) {
        return {error: '章节列表只取到 ' + out.length + '/' + total +
                       ' 条（分页被平台截断？），拒绝返回半份数据'};
    }
    return {items: out};
}"""


VOLUME_INDEX_STRIDE = 10000    # index = 卷序号(0起) * 10000 + 卷内位置


def global_position(index, offsets):
    """把 chapter_list 的 index 换算成「全书第几个位置」。

    2026-08-20 在真多卷作品（3 卷 100+150+115=365 章）上实测:
      卷1 index 1~100、卷2 index 10001~10150、卷3 index 20001~20115
    即 index = 卷序号(0 起) * 10000 + 卷内位置。全书位置 = 前面各卷章数之和
    + 卷内位置，365 章逐条比对「全局位置 == 标题第N章」零偏差。

    offsets: {卷序号: 前面各卷 item_count 之和}。单卷书 offsets={0:0}，
    此时返回值就等于 index，与单卷逻辑完全一致（所以调用方不必分叉）。
    """
    vol = index // VOLUME_INDEX_STRIDE
    return offsets.get(vol, 0) + index % VOLUME_INDEX_STRIDE


async def fetch_volume_list(page, signed_volume_url):
    """按 index 升序返回 [{volume_id, volume_name, item_count, index}]。"""
    data = await page.evaluate(
        "async (u) => (await (await fetch(u, {credentials:'include'})).json())",
        signed_volume_url)
    vols = ((data or {}).get("data") or {}).get("volume_list") or []
    return sorted(vols, key=lambda v: v.get("index", 0))


async def fetch_chapter_items(page, book_id):
    """抓平台章节的真实状态（全书、跨卷），返回 (items, signed_url, volumes)。

    章节列表接口带 volume_id，一次只返回一卷。这里遍历每一卷（把签名 URL 里
    的 volume_id 换掉，实测接口认），拼成全书视图——漏章对账必须看全书，
    只看当前卷会把别卷的章全判成"平台上没有"。

    每个 item 额外带:
      pos         全书第几个位置（跨卷连续，见 global_position）
      volume_id / volume_name
    单卷作品 pos == index，与单卷逻辑完全一致。

    signed_url 供调用方继续按页回读做对账；volumes 是卷列表（长度即卷数）。

    防错位铁律（错了会让 remap 把章改写成不相干正文，必须炸而不是猜）:
      · 每卷抓回的 item 必须满足 index//10000 == 卷序号——这同时抓住
        「volume_id 替换没生效、其实每次都在抓当前卷」和「平台改了编号规则」；
      · 抓到带卷偏移的 index 却没有卷列表 → 多卷书按单卷算，pos 全错，直接报错。
    """
    seen_ch, seen_vol = [], []

    def _grab(r):
        if "chapter/chapter_list" in r.url:
            seen_ch.append(r.url)
        elif "volume/volume_list" in r.url:
            seen_vol.append(r.url)

    # 用完必须摘掉：本函数在长任务里会被反复调用（每次对账都可能重取签名），
    # 监听器只加不减会一路累积到页面销毁。
    page.on("request", _grab)
    try:
        if not await goto_with_login_retry(
                page, CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)):
            raise RuntimeError("打不开章节管理页（登录失效或导航未到达），具体原因见上一条日志")
        for _ in range(20):
            await page.wait_for_timeout(500)
            if seen_ch:
                break
        if not seen_ch:
            raise RuntimeError("没抓到 chapter_list 请求，页面结构可能已变")
        # volume_list 通常先于 chapter_list 发出，但别赌时序——再宽限几秒。
        # 多卷书漏了卷列表不是"降级"，是 pos 全错（见铁律②），所以必须等。
        for _ in range(6):
            if seen_vol:
                break
            await page.wait_for_timeout(500)
        ch_url = seen_ch[-1]

        vols = []
        if seen_vol:
            try:
                vols = await fetch_volume_list(page, seen_vol[-1])
            except Exception as e:
                logger.debug(f"取分卷列表失败，按单卷处理: {e}")

        # 逐卷抓；偏移量按卷序累加，得到跨卷连续的全书位置
        items, acc = [], 0
        targets = vols or [None]
        for ordinal, v in enumerate(targets):
            # volume_id=\d* 兼容空参数值（\d+ 匹配不上会静默抓成当前卷）
            url = ch_url if v is None else re.sub(
                r"volume_id=\d*", f"volume_id={v['volume_id']}", ch_url, count=1)
            res = await page.evaluate(_FETCH_ITEMS_JS, url)
            if res.get("error"):
                raise RuntimeError(f"抓取章节列表失败: {res['error']}")
            got = res.get("items") or []
            bad = [it["index"] for it in got
                   if it["index"] // VOLUME_INDEX_STRIDE != ordinal]
            if bad:
                raise RuntimeError(
                    f"卷{ordinal + 1} 抓回的 index 不符（如 {bad[:3]}），"
                    f"疑似 volume_id 替换未生效或平台编号规则已变——中止以防错位")
            if v is not None and got and len(got) != (v.get("item_count") or len(got)):
                logger.warning(f"  卷「{v.get('volume_name')}」抓到 {len(got)} 章，"
                               f"与卷信息声明的 {v.get('item_count')} 不一致")
            for it in got:
                # 走 global_position 而不是内联公式: demo 里的多卷断言测的就是它，
                # 内联一份等于断言盖不住生产路径（卷偏移算错=全书错位，代价极大）
                it["pos"] = global_position(it["index"], {ordinal: acc})
                if v is not None:
                    it["volume_id"] = v.get("volume_id")
                    it["volume_name"] = v.get("volume_name")
            items.extend(got)
            acc += len(got)
        if not vols and any(it["index"] >= VOLUME_INDEX_STRIDE for it in items):
            raise RuntimeError(
                "抓到带卷偏移的 index 但没取到卷列表——多卷作品按单卷算会全错，"
                "请重试（多为 volume_list 请求未捕获）")
        return items, ch_url, vols
    finally:
        try:
            page.remove_listener("request", _grab)
        except Exception:
            pass


def volume_count(volumes):
    """卷数（拿不到就当 1 卷）。兼容 detect_volumes 结果与 fetch 返回的卷列表。"""
    try:
        if isinstance(volumes, dict):
            return max(1, len(volumes.get("volumes") or []))
        return max(1, len(volumes or []))
    except Exception:
        return 1


def audit_chapter_positions(items, *, now_ts=None, window_h=MOVE_WINDOW_H):
    """缺口体检: 按「谁能修」把问题分三段。

    A pending_bad 未公开段位置与章号不符 → 可用 tools/remap 自动改写内容。
    B in_window  已公开、发布在 window_h 内、且排在了它该在的位置之后
                 → 只能手机 App 申请+审批+逐章移动，很贵，必须在窗口内知道。
    C expired    已公开、超窗口 → 永久错位，只能登记在案。

    "排在了该在的位置之后"判据: 该 item 的章号 < 它前面所有位置出现过的最大
    章号。均匀后移（前面漏章导致整段偏移）不算——那只是缺号，阅读顺序仍单调；
    真正伤读者的是顺序倒挂（读到第699章之后突然接第480章）。
    已发布章的 create_time 就是实际发布时刻（平台在发布时改写该字段）。
    """
    now_ts = int(time.time()) if now_ts is None else now_ts
    pend_bad, in_window, expired = [], [], []
    running_max = 0
    # 按全书位置排序/比对：多卷作品的 index 带卷序号偏移（见 global_position），
    # 直接拿 index 和「第N章」比会把整卷判成错位。单卷时 pos 缺省等于 index。
    for it in sorted(items, key=lambda x: x.get("pos", x["index"])):
        pos = it.get("pos", it["index"])
        n = chapter_title_num(it["title"])
        if it["display_status"] == DISPLAY_PUBLISHED:
            if n is not None and n < running_max:
                try:
                    pub_at = int(it.get("create_time") or 0)
                except (TypeError, ValueError):
                    pub_at = 0
                left_h = (pub_at + window_h * 3600 - now_ts) / 3600
                row = {"index": pos, "num": n, "title": it["title"],
                       "pub_at": pub_at, "left_h": left_h}
                (in_window if left_h > 0 else expired).append(row)
            if n is not None:
                running_max = max(running_max, n)
        elif (it["display_status"] == DISPLAY_PENDING
              and n is not None and n != pos):
            # n is None = 标题里没有「第N章」（楔子/番外/作者的话）。它本来就
            # 不参与主线编号，拿 pos 去比必然不等 —— 判成错位会让 remap 把它
            # 排进改写计划，标题和正文被第pos章的内容覆盖。
            pend_bad.append(pos)
    return {"pending_bad": pend_bad, "in_window": in_window, "expired": expired}


# ---------------------------------------------------------------------------
# 无人值守外壳（tools/ 下的定时作业共用：remap 重排、keep_ahead 续排…）
#
# 放在这里而不是各工具各写一份：日志接管、告警弹窗、崩溃兜底这几件事每个
# 定时作业都要，抄第二遍就会漂移（一个改了另一个没改）。
# ---------------------------------------------------------------------------
def start_task_log(log_dir, prefix):
    """把本进程输出接到带时间戳的日志文件，并接管 logger 的控制台 handler。

    无人值守跑（Windows 计划任务用 pythonw / cron）时没有终端接输出：
    print 会打到不存在的 stdout，logger 的 StreamHandler 也写不出去。这里
    统一换成写同一个文件对象——共用一个句柄，两路输出不会互相截断。
    返回日志路径。
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M}.log"
    f = open(path, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    for h in list(logger.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            logger.removeHandler(h)
    fh = logging.StreamHandler(f)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                      datefmt="%H:%M:%S"))
    logger.addHandler(fh)
    return path


def alert(title, text):
    """弹窗提醒。tkinter 是本项目已有依赖（GUI 就用它），跨平台可用。

    没有图形环境（headless 服务器 / cron）时静默失败——日志里已经写了原因，
    弹不出来不该把整次运行搞崩。
    """
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showwarning(title, text)
        root.destroy()
    except Exception as e:
        print(f"（弹窗失败，仅记日志: {e}）", flush=True)


def verify_content_matches_book(items, num2path, *, sample=8):
    """确认「本地这批稿子」确实属于「平台这本书」。返回 (ok, 说明)。

    为什么必须查: 无人值守作业的 book_id 取自 .gui_state.json 的 last_book_id、
    章节目录取自 config.json 的 chapters_dir——**在 GUI 里切一次作品，今晚的
    定时任务就换了目标**。而这些作业都是真写入（改写正文 / 发布新章），把 A 书
    的稿子发进 B 书，后果和漏章一个量级且更难收拾。

    做法: 取平台上已发布的若干章，跟本地同章号文件的标题比。标题是作者自己
    写的、跨书重合概率极低，比对不上就是拿错目录。平台标题形如
    「第N章 标题」，本地 parse 出来的是纯标题，所以做包含比较。
    """
    pub = [x for x in items if x.get("display_status") == DISPLAY_PUBLISHED]
    checked = matched = 0
    misses = []
    for it in sorted(pub, key=lambda x: x.get("pos", x.get("index", 0)),
                     reverse=True):
        if checked >= sample:
            break
        n = chapter_title_num(it.get("title"))
        if n is None or n not in num2path:
            continue
        checked += 1
        try:
            _c, local_title, _b = parse_md_file(Path(num2path[n]))
        except Exception:
            continue
        if local_title and local_title.strip() and local_title.strip() in it["title"]:
            matched += 1
        else:
            misses.append(f"第{n}章 平台「{it['title'][:22]}」≠ 本地「{local_title[:16]}」")
    if checked == 0:
        return True, "无可比对样本（平台已发布章都不在本地），跳过校验"
    if matched * 2 < checked:      # 过半对不上 = 拿错目录
        return False, (f"抽查 {checked} 章仅 {matched} 章标题吻合，"
                       f"本地目录疑似不属于这本书：" + "；".join(misses[:3]))
    return True, f"抽查 {checked} 章，{matched} 章标题吻合"


_MONTHLY_LIMIT_RE = re.compile(r"(每月|本月|单月|月度)[^，。;；]{0,12}上限")


def is_monthly_limit(msg):
    """这条上限提示是「每月」而不是「每日」吗？

    两者的处置完全不同，不该混为一谈:
      · 撞每日上限 —— 常态，明天接着跑，进度不受影响（每月额度才是硬顶）。
      · 撞每月上限 —— 本月到此为止，后面十几二十天一章都改不动，而发布前沿
        仍在推进。此时必须立刻检查"余量够不够撑到下月"，不够就得再降速。
    """
    return bool(_MONTHLY_LIMIT_RE.search(str(msg or "")))


def limit_label(msg) -> str:
    """撞的是「每月」还是「每日」上限——两者的下一步完全不同，别混着说。

    每日: 常态，明天接着跑。每月: 本月剩下的日子一章都发不动，"明天再来"
    是错的，得当场算余量够不够撑到下月（tools/remap 据此提醒降速）。
    is_monthly_limit 早就在了，却只有 remap 用——三条中止分支一律硬写"每日"，
    于是 2026-08-06/08-10/08-13 三次撞的其实是**每月**上限，日志却说每日。
    """
    return "每月字数上限" if is_monthly_limit(msg) else "每日字数上限"


# 新章确认: 只拉列表第一页（新建章必是最新的），1 个请求就够
# 必须区分「平台确实没这章」和「这次问不出来」: 接口 429 / code=-100
# （平台真会返回"服务器开小差了"）时 j.data 为空，若当成"没有"就会判定
# 新章没落地 -> 中止整批 + 记入补传清单 -> 用户照单补传 -> 书尾多一章重复，
# 而重复章按番茄的追加序永远移不回去。宁可报 unknown 让上层继续轮询。
_PROBE_LATEST_JS = r"""async (u) => {
    const uu = u.replace(/page_index=\d+/, 'page_index=0')
                .replace(/page_count=\d+/, 'page_count=30');
    try {
        const r = await fetch(uu, {credentials: 'include'});
        if (!r.ok) return {error: 'http ' + r.status};
        const j = await r.json();
        if (j && j.code !== undefined && j.code !== 0)
            return {error: 'code ' + j.code + ' ' + (j.message || '')};
        if (!j || !j.data || !Array.isArray(j.data.item_list))
            return {error: 'no item_list'};
        return {titles: j.data.item_list.map(x => x.title)};
    } catch (e) {
        return {error: String(e)};
    }
}"""


def watch_chapter_list_url(page):
    """持续记录页面自己发出的、最新的已签名 chapter_list 请求 URL。

    返回 (holder, detach)。holder[-1] 就是最新可用的签名 URL。
    发布成功后 SPA 会跳回 chapter-manage，那次跳转天然带来一条新鲜签名——
    所以不必为了确认新章而额外导航，也不怕签名过期。
    """
    holder = []

    def _grab(r):
        if "chapter/chapter_list" in r.url:
            # 只留最新一条: 调用方只读 holder[-1]，而每次确认轮询都会
            # 触发一次 chapter_list 请求，748 章的批次会攒下几千条长 URL。
            holder[:] = [r.url]

    page.on("request", _grab)

    def detach():
        try:
            page.remove_listener("request", _grab)
        except Exception:
            pass

    return holder, detach


async def confirm_chapter_on_platform(page, url_holder, num, *,
                                      window_s=90, poll_s=10, volume_id=None):
    """确认「第num章」真的出现在平台上了。返回 True/False。

    **只有"新建"类操作需要它**（定时发布/立即发布/续排），因为失败不可逆:
    章节根本不存在，而新建只能追加到全书末尾，中段缺口再也补不回原位
    （2026-07-24 那次 748 章漏 151 章正是如此: "按钮消失=成功"误判，
    日志全绿、平台没有，三周后发现时已超过手机 App 的 3 天移动窗口）。
    改内容/改排期则相反——item 还在，重来一次即可，不该为确认牺牲吞吐。

    拿不到签名 URL 时返回 True（无从确认，不阻断任务）；调用方已有批末对账兜底。

    volume_id: 多卷作品必须传。章节列表接口一次只返回一卷，而签名 URL 里
    烘的是 chapter-manage 当时渲染的那一卷。新建章永远追加到全书末尾，也就是
    **最后一卷**——两者对不上时，探针会在卷 1 里找卷 3 的章，永远找不到。
    阴险之处在于接口好好的、clean_probe 为 True，于是 90 秒后当成真的没落地，
    把一个已经发成功的章判成失败并中止整批。单卷作品传与不传等价。
    """
    if not url_holder:
        # 静默返回 True 等于「这一章不确认了」。整批都没捕获到签名请求时，
        # 防漏章的守卫就整批关闭了却没人知道 —— 至少要留一条日志，
        # 让批末对账的告警有迹可循。
        logger.warning(
            f"  第{num}章 无法确认: 还没捕获到章节列表的签名请求，"
            f"本章跳过确认（批末对账仍会核对）")
        return True
    deadline = time.monotonic() + window_s
    clean_probe = False   # 窗口内是否至少成功问到过一次章节列表
    last_err = ""
    while True:
        try:
            probe_url = url_holder[-1]
            if volume_id:
                # 与 fetch_chapter_items 用同一条替换规则（\d* 兼容空参数值）
                probe_url = re.sub(r"volume_id=\d*",
                                   f"volume_id={volume_id}", probe_url, count=1)
            res = await page.evaluate(_PROBE_LATEST_JS, probe_url)
        except Exception as e:
            res = {"error": str(e)}
        if isinstance(res, dict) and res.get("error"):
            # 问不出来 ≠ 平台上没有。继续轮询，别拿一次网络抖动去中止整批。
            last_err = str(res["error"])
        else:
            clean_probe = True
            titles = (res or {}).get("titles") or []
            if any(chapter_title_num(t) == num for t in titles):
                return True
        if time.monotonic() >= deadline:
            if not clean_probe:
                # 整个窗口一次都没问通（接口挂了/限流）——此时判"没落地"会造成
                # 假失败并诱发重复补传，而重复章补不回原位。放行交给批末对账，
                # 那条路会拿完整章节列表核对，代价只是晚一点发现。
                logger.warning(
                    f"  确认第{num}章时始终读不到章节列表（{last_err}），"
                    f"本章不判失败，交由批末对账核实")
                return True
            return False
        await page.wait_for_timeout(poll_s * 1000)


def find_missing_after_batch(items, claimed_nums):
    """纯函数: 我们以为发成功的章号里，平台上实际不存在的是哪些（升序）。

    claimed_nums: 本批"日志记成功"的章节号集合。
    items: 平台真实章节（fetch_chapter_items 的返回）。
    平台上一章是否存在，只看它的标题里有没有那个「第N章」——章节号在番茄
    只是标题文本，但对"这一章到底发出去没有"这个问题，它就是唯一可比的键。
    """
    present = {chapter_title_num(x.get("title")) for x in items}
    present.discard(None)
    return sorted(n for n in claimed_nums if n is not None and n not in present)


# 草稿接口的每页上限比章节接口低: 实测 page_count=30 可以、50 起就返回
# code=-100「服务器开小差了」（章节接口 100 没问题）。别照抄那边的 100。
_DRAFT_PAGE = 30
_DRAFT_LIST_JS = r"""async ([u, per]) => {
    const out = [];
    let total = null;
    for (let pg = 0; pg < 200; pg++) {
        const uu = u.replace(/page_index=\d+/, 'page_index=' + pg)
                    .replace(/page_count=\d+/, 'page_count=' + per);
        let r, j;
        try {
            r = await fetch(uu, {credentials: 'include'});
            // 先判 r.ok 再 json(): 502/429 常返回 HTML，先 json() 会直接抛，
            // 而外层没有 try 包 evaluate，于是走不到下面那句友好报错，
            // clean_drafts 拿到的是一条生的 Playwright 异常。
            if (!r.ok) return {error: '第' + pg + '页 HTTP ' + r.status};
            j = await r.json();
        } catch (e) {
            return {error: '第' + pg + '页请求失败: ' + e};
        }
        if (j && j.code !== undefined && j.code !== 0 && j.code !== '0') {
            return {error: '第' + pg + '页接口 code=' + j.code};
        }
        const d = j && j.data;
        if (!d) return {error: '第' + pg + '页响应无 data'};
        if (total === null) total = d.total_count;
        const list = d.draft_list || [];
        if (!list.length) break;
        out.push(...list.map(x => ({title: x.title, word_number: x.word_number,
                                    item_id: x.item_id})));
        if (list.length < per) break;
    }
    // 平台自己报了总数就必须对上。per 是调过的（实测 page_count>=50 会 code=-100），
    // 所以短页收尾通常是对的；但万一平台又把页大小往下压，上面那句就会在第 0 页
    // 收工、只返回半份。clean_drafts 的安全检查拿这份逐条比对本地源文件——半份
    // 意味着没被看见的那些草稿从未参与检查，而 --force 会照删不误。
    if (typeof total === 'number' && out.length < total) {
        return {error: '草稿列表只取到 ' + out.length + '/' + total +
                       ' 条（分页被平台截断？），拒绝返回半份数据'};
    }
    return {drafts: out, total: total};
}"""


async def fetch_draft_list(page, book_id):
    """抓草稿箱真实内容，返回 (drafts, total_count)。

    草稿箱走 /api/author/chapter/draft_list/v1（与章节列表不是同一个接口）。
    """
    seen = []

    def _grab(r):
        if "chapter/draft_list" in r.url:
            seen.append(r.url)

    page.on("request", _grab)
    try:
        if not await goto_with_login_retry(
                page, CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)):
            raise RuntimeError("打不开章节管理页（登录失效或导航未到达），具体原因见上一条日志")
        await page.wait_for_timeout(2000)
        # 点「草稿箱」标签，让页面自己发出带签名的 draft_list 请求
        await page.evaluate("""() => {
            for (const el of document.querySelectorAll('*')) {
                if (el.children.length === 0 &&
                    (el.textContent || '').trim() === '草稿箱') { el.click(); return; }
            } }""")
        for _ in range(20):
            await page.wait_for_timeout(500)
            if seen:
                break
        if not seen:
            raise RuntimeError("没抓到 draft_list 请求，页面结构可能已变")
        res = await page.evaluate(_DRAFT_LIST_JS, [seen[-1], _DRAFT_PAGE])
        if res.get("error"):
            raise RuntimeError(f"抓取草稿列表失败: {res['error']}")
        _drafts = res.get("drafts") or []
        # total 必须是 int: 接口偶尔不给 total_count，返回 None 会让调用方的
        # min(limit, total) 直接崩，GUI 的 if not total 又会误报「草稿箱是
        # 空的」而不去清理。拿不到就退回已抓到的条数。
        _total = res.get("total")
        if not isinstance(_total, int):
            _total = len(_drafts)
        return _drafts, _total
    finally:
        try:
            page.remove_listener("request", _grab)
        except Exception:
            pass


async def reconcile_drafts_after_batch(page, book_id, claimed_nums, fail_list):
    """存草稿批次收尾对账: 拿草稿箱真实内容核对"日志记已存"的章。

    **草稿只做批末对账，不做逐章确认**——判据仍是"失败可不可逆":
    草稿丢了重传即可、不影响正文顺序，为一次确认失败中止整批得不偿失；
    而定时/立即发布漏一章是永久缺口，才值得逐章确认+失败即停。

    这里补的是一个真盲区: 原本只能靠"草稿ID 被复用"**推断**上一章被覆盖，
    读不到草稿ID 时只能提示"请到草稿箱核对"。改成拿平台真实草稿列表比对，
    漏了哪几章直接列出来（番茄会把连续两次新建章草稿并到同一槽位，
    覆盖丢失是这条路径的常见故障）。
    """
    if not claimed_nums:
        return []
    try:
        drafts, total = await fetch_draft_list(page, book_id)
    except Exception as e:
        logger.warning(f"草稿对账失败（不影响已存草稿）: {e}")
        return []
    present = {chapter_title_num(d.get("title")) for d in drafts}
    present.discard(None)
    missing = sorted(n for n in claimed_nums if n not in present)
    logger.info(f"草稿对账: 本批 {len(claimed_nums)} 章，草稿箱共 {total} 条")
    if missing:
        logger.error(f"⚠ 有 {len(missing)} 章日志记已存草稿但草稿箱里没有: "
                     f"{'、'.join(f'第{n}章' for n in missing[:20])}"
                     f"{' …' if len(missing) > 20 else ''}")
        logger.error("  多为番茄把相邻两章并到同一草稿槽导致覆盖；重存这些章即可"
                     "（草稿无顺序问题，重传无副作用）")
        for n in missing:
            fail_list.append((f"第{n}章 ", "对账: 日志记已存草稿但草稿箱里没有"))
    else:
        logger.info("草稿对账通过: 本批章节在草稿箱里都能查到")
    return missing


async def reconcile_after_batch(page, book_id, claimed_nums, fail_list):
    """批次收尾对账：拿平台真实数据核对"日志说发成功的章"是不是真的在。

    为什么必须有: 提交判定再严也只是页面/接口层面的信号。2026-07-24 那次
    748 章定时发布，日志记"成功 736"，平台上却少 151 章——直到三周后人工
    只读枚举才发现，那时早已超过手机 App 的 3 天移动窗口，全部永久错位。
    发完立刻对一次账，漏章当天就暴露，窗口还剩 72 小时、要手动移的是 1 章。

    漏掉的章写入 fail_list，由 log_fail_list 压进可直接补传的章节号。
    返回漏掉的章号列表；对账本身失败（网络/会话）只告警，不影响批次结果。
    """
    if not claimed_nums:
        return []
    try:
        items, _, volumes = await fetch_chapter_items(page, book_id)
    except Exception as e:
        logger.warning(f"批次对账失败（不影响已发章节）: {e}")
        return []
    if volume_count(volumes) > 1:
        logger.info(f"  （本作品 {volume_count(volumes)} 卷，已跨卷合并 {len(items)} 章对账）")
    missing = find_missing_after_batch(items, claimed_nums)
    if missing:
        logger.error(f"⚠ 对账发现 {len(missing)} 章日志记成功但平台上没有: "
                     f"{'、'.join(f'第{n}章' for n in missing[:20])}"
                     f"{' …' if len(missing) > 20 else ''}")
        logger.error("  这些章需要重发；番茄新建章只会追加到全书末尾，"
                     "所以越早补越好（已公开章的顺序只能在手机 App 里申请移动，限 3 天）")
        for n in missing:
            fail_list.append((f"第{n}章 ", "对账: 日志记成功但平台上不存在"))
    else:
        logger.info(f"对账通过: 本批 {len(claimed_nums)} 章在平台上都能查到")
    return missing


# 按【精确文本】点击可见元素：用于选项不是标准 <button> 的弹窗（如内容检测方式
# 的"仅基础检测"卡片/单选项）。优先交互元素，再退到 span/div；只点最内层叶子，
# 避免点到把整段文本聚合进来的父容器误触其它选项。
_CLICK_BY_TEXT_JS = r"""(label) => {
    const sels = ['button', '[role="button"]', '.arco-radio', 'label',
                  '.arco-card', 'li', 'span', 'div'];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            if (el.getClientRects().length === 0) continue;
            if ((el.textContent || '').trim() !== label) continue;
            // 叶子优先：若有同样只含该文本的子节点，留给更内层的迭代
            const inner = el.querySelector(sel);
            if (inner && (inner.textContent || '').trim() === label) continue;
            el.click();
            return true;
        }
    }
    return false;
}"""


async def _navigate_to_publish_settings(page, *, use_ai: bool = False, draft_action="继续编辑"):
    """
    从编辑器完整走到"发布设置"对话框。

    点击"下一步"后可能出现两种流程:
      A) 直接弹出对话框序列（常见）:
         发布提示(错别字确认) -> 是否进行内容风险检测 -> 发布设置
      B) 先打开右侧智能纠错面板:
         纠错面板 -> 忽略全部 -> 再次下一步 -> 对话框序列

    本函数统一处理两种情况。

    draft_action: 点"下一步"后弹出"是否继续编辑"草稿弹窗时，按此按钮处理（状态机分支 2）。
                  本函数的所有调用方（新建/定时/立即发布/修改）都是在 fill_chapter 填好
                  内容【之后】才调用——此时弹窗里的草稿正是我们刚填的内容，必须"继续编辑"
                  保留它。"放弃"会把刚填的标题/正文/章节号整个丢弃，表单变空，之后点
                  "下一步"被平台校验拦住（"标题至少输入5个字/正文至少输入1000字"），空转
                  14 轮 + 15s 误报"发布设置超时"（2026-07-02 截图实证：定时发布每章如此）。
                  故默认即"继续编辑"；新建/定时一般不弹此窗，但失败重试会留下草稿、再跑就弹。
                  （进编辑器【之前】丢弃上次遗留的旧草稿是另一回事，由 wait_for_editor_ready
                  里的 dismiss_overlays 用"放弃"处理，与本参数无关。）
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

        # 1.5) 字段校验失败 toast（章节号非数字/正文字数不足等）-> 立即抛错。
        #      未到发布设置才查（上面已 return），故可见的校验 toast 是真实拦截。
        #      不识别它就会反复点"下一步"被拦、空转 14 轮 + 15s 误报"发布设置超时"，
        #      真实原因（哪个字段没填好）丢失（实测每章空转 ~43s）。
        await _check_editor_validation(page)

        # 2) 草稿恢复弹窗"是否继续编辑" -> 按 draft_action 关闭
        if await page.locator("text=是否继续编辑").count() > 0:
            draft_btn = page.locator("button", has_text=draft_action)
            if await draft_btn.count() > 0:
                await draft_btn.first.click()
                await page.wait_for_timeout(500)
                continue

        # 2.5) 信息提示弹窗（如"请在发布时间前30分钟提交修改内容，否则无法完成修改"）
        #      -> 点"我知道了"关闭。此类弹窗可能晚于编辑器就绪才出现，入口处的
        #      dismiss_edit_hint 会漏掉；不点掉则遮罩挡住"下一步"，每轮点击烧满
        #      可操作性超时（曾导致每章空转 448s 后才进入整章重试）。
        try:
            ack_btn = page.locator("button", has_text="我知道了")
            if await ack_btn.count() > 0 and await ack_btn.first.is_visible():
                await ack_btn.first.click()
                logger.info("    已关闭提示弹窗(我知道了)")
                await page.wait_for_timeout(500)
                continue
        except Exception:
            pass

        # 3) 智能纠错面板 -> "忽略全部"
        try:
            ignore_btn = page.locator("button", has_text="忽略全部")
            if await ignore_btn.count() > 0 and await ignore_btn.first.is_visible():
                await ignore_btn.first.click()
                await page.wait_for_timeout(600)
                continue
        except Exception:
            pass

        # 4) 错别字确认对话框（实测文案："检测到你还有错别字未修改，是否确定提交？"
        #    按钮：取消 / 提交）-> 点"提交"，带着错别字照常提交（不采纳平台纠错改动）。
        #    触发认"错别字未修改"或"是否确定提交"两种锚点：只认死文案易因平台微调
        #    （确认/确定提交）漏匹配 -> 对话框没人确认 -> 一直点不动 -> 误报"发布设置超时"。
        if (await page.locator("text=错别字未修改").count() > 0
                or await page.locator("text=是否确定提交").count() > 0):
            submit_btn = page.locator("button", has_text="提交")
            n = await submit_btn.count()
            clicked = False
            # 只点【可见】的"提交"，绝不点"取消"。逐个挑可见的（背景里可能另有
            # 含"提交"二字、不可见或非当前对话框的按钮，.first 会点错）。
            for idx in range(n):
                try:
                    btn = submit_btn.nth(idx)
                    if await btn.is_visible():
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                logger.info("    错别字确认 -> 提交")
                await page.wait_for_timeout(800)
                continue

        # 5) 内容风险检测"是否进行内容风险检测" -> 取消跳过（旧版弹窗）
        if await page.locator("text=是否进行内容风险检测").count() > 0:
            cancel_btn = page.locator("button", has_text="取消")
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
                await page.wait_for_timeout(800)
                continue

        # 5.5) 内容检测方式选择"请选择内容检测方式"（仅基础检测 / 全面检测）
        #      -> 选"仅基础检测"。平台新版用它取代了旧的"是否进行内容风险检测?取消"，
        #      不再有"跳过"选项、必须二选一；基础检测最快、干预最少。
        #      选项可能是按钮/卡片/单选项，故用按文本精确点击（不限 button 标签）。
        if await page.locator("text=内容检测方式").count() > 0:
            picked = await page.evaluate(_CLICK_BY_TEXT_JS, "仅基础检测")
            if picked:
                logger.info("    内容检测方式 -> 仅基础检测")
                await page.wait_for_timeout(500)
                # 选完可能还需点"确定/确认"才推进（标准 Arco 弹窗页脚）
                for label in ("确定", "确认"):
                    btn = page.locator("button", has_text=label)
                    try:
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            break
                    except Exception:
                        continue
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
    await _submit_confirm_publish(page)


async def _submit_confirm_publish(page):
    """点「确认发布」并等接口判定。三条提交路径共用同一份动作。

    调用方: publish_scheduled（定时发布）、publish_one_chapter（立即发布）、
    edit_one_chapter（修改内容）。

    这是全流程最关键的一步——判定"是否真的提交成功"。曾经按"按钮消失"算成功，
    对话框异常关闭时按钮同样消失，748 章漏了 151 章。散成三份写就意味着以后改
    判定逻辑可能只改到其中一两处。
    """
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
        await settle_page(page)

        logger.info("")
        logger.info("=" * 50)
        logger.info("  请在浏览器中登录番茄作家账号")
        logger.info("  登录成功后回到此处按 Enter 保存会话")
        logger.info("=" * 50)
        await asyncio.get_running_loop().run_in_executor(None, input)

        saved = await save_auth(context)
        await close_browser_safely(browser)
        if saved:
            logger.info("登录状态已保存。")
        else:
            logger.error(
                "登录状态未能保存（浏览器可能已被提前关闭），请重新运行 login")


# ---------------------------------------------------------------------------
# 命令: books
def record_rest_unprocessed(fail_list, parsed, start, stop, **kw):
    """中止整批时把 [start, stop) 的剩余章节记入补传清单，返回失败增量。

    CLI 和 GUI 的每条中止分支（每日上限、提交后查不到、不可逆失败、
    连续失败熔断、页面已死、用户取消 等）原来两边各写一份这个生成器表达式。漏掉一处的
    表现是汇总里"成功 + 失败 < 总数"，而且补传清单缺这几章——创建路径缺章
    补不回原位（番茄目录按追加序排，后补的只会吊在书尾）。
    """
    return record_unprocessed(
        fail_list, ((parsed[j][0], parsed[j][1]) for j in range(start, stop)),
        **kw)


async def run_creation_batch(page, parsed, new_chapter_url, *, book_id,
                             schedule=None, is_draft=False, use_ai=False,
                             max_retries=2, delay=3,
                             cancel_check=None, progress_cb=None,
                             err_tag_fn=None):
    """新建类批次（定时发布 / 立即发布 / 存草稿）的完整执行循环。

    这段循环就是当初 748 章漏 151 章的那条路径。它曾在 CLI 和 GUI 各写一份
    （约 190 行逐行相同），历史上的每一类 bug 都是修一边漏一边；现在只有这一份，
    两个入口只做参数收集和结果展示。

    策略（判据 = 失败可不可逆）:
      · 发布类逐章确认新章真落地；确认不到或提交失败 → 中止整批——新建只能
        追加到书尾，跳过失败继续发会把缺口永久卡在中段，停在队尾才是无害的。
      · 存草稿保留连续 3 次熔断（草稿丢失不影响正文顺序），批末拿草稿箱对账。
      · 撞字数上限（每日或每月，措辞走 limit_label）→ 中止整批，本章与
        剩余章节全部记入补传清单。
      · 原语泄漏的意外异常（浏览器崩溃/页面被关）在循环里兜住，按不可逆
        规则处理——绝不让它穿透执行器，否则汇总与补传清单一起丢。
      · 收尾统一走 reconcile_batch_auto: 日志记成功 ≠ 平台上真有。

    cancel_check(): 返回 True 则在章节边界停下（GUI 的「停止」按钮；CLI 不传）。
        停下时剩余章节同样记入 fail_list（原因写明是取消）并计入 failed，
        口径与其余中止路径一致——补传章节号就是靠它生成的。
    progress_cb(done, total): 每章之后与各中止点回报进度（GUI 进度条；CLI 不传）。
    err_tag_fn(i): 第 i 章失败截图的文件名标签。
    返回 (success, failed, fail_list)。
    """
    success = 0
    failed = 0
    consec_fail = 0
    fail_list: list[tuple[str, str]] = []  # (章节标签, 失败原因)
    draft_owner: dict[str, tuple] = {}  # 防覆盖漏账: draftId -> (章节标签, 章号)
    claimed_nums: list[int] = []      # 日志记成功的章号，收尾与平台对账
    # 新建类模式要逐章确认新章真落地了；签名 URL 从页面自己的请求里捡。
    # 必须先主动取一条: watch 只记录「挂上之后」页面自己发的请求，而发布走
    # 接口判定成功时 SPA 未必会跳回 chapter-manage —— 那样整批都捡不到签名，
    # confirm_chapter_on_platform 每章都走 not url_holder 的放行分支，
    # 防漏章的守卫整批静默关闭。keep_ahead 一直是先 append 再跑的。
    sig_urls, detach_sig = watch_chapter_list_url(page)
    # 调用方进来时页面停在编辑器（它们用 goto+wait_for_editor_ready
    # 做登录校验），所以第一章本可以省一次导航。但下面的签名预取会把
    # 页面导到 chapter-manage，那个前提就不成立了——必须跟着改，否则
    # 第 1 章会在章节管理页上填正文：max_retries>0 时白燒一个超时再重试成功，
    # max_retries=0 时直接失败——而新建类是失败即停，整批当场中止。
    on_editor = True
    tail_vol = None        # 新章追加到的那一卷（全书最后一卷）；单卷书为 None
    if not is_draft and not sig_urls:
        try:
            _, _seed, _vols = await fetch_chapter_items(page, book_id)
            on_editor = False      # fetch_chapter_items 内部 goto 了 chapter-manage
            if _vols and len(_vols) > 1:
                # 章节列表接口一次只回一卷，而签名里烘的是 chapter-manage
                # 当时看的那一卷。不指定的话，逐章确认会在卷 1 里找卷 3 的章，
                # 永远找不到 → 把发成功的章判成失败 → 中止整批。
                tail_vol = _vols[-1].get("volume_id")
                logger.info(f"  本作品 {len(_vols)} 卷，逐章确认针对末卷"
                            f"「{_vols[-1].get('volume_name') or tail_vol}」")
            if _seed:
                sig_urls.append(_seed)
                logger.info("  已预取章节列表签名，逐章确认就绪")
        except Exception as e:
            on_editor = False      # 异常也可能发生在导航之后，不能再当停在编辑器
            logger.warning(
                f"  预取章节列表签名失败（逐章确认将依赖页面自发请求）: {e}")
    total = len(parsed)
    if err_tag_fn is None:
        err_tag_fn = str
    _progress = progress_cb or (lambda done, tot: None)

    for i in range(total):
        if cancel_check and cancel_check():
            # 与其余中止路径同款记账。取消确实不是"失败"，但这里的
            # fail_list 本质是「没做成的都在这」——log_fail_list 末尾那行压缩
            # 章节号（可直接粘回筛选框续跑）就是靠它生成的，原因文案已写明
            # 是取消。曾短暂改成"只打日志不记账"，结果 run_edit_batch 主循环
            # 与它自己的批末二次尝试对同一次点击给出两种数，是更糟的分叉。
            logger.info("用户取消，中止批次。")
            failed += record_rest_unprocessed(
                fail_list, parsed, i, total, reason="用户取消，未处理")
            break

        chapter_num, title, content = parsed[i]
        num_str = f"第{chapter_num}章 " if chapter_num else ""
        sched_info = f" -> {schedule[i][0]} {schedule[i][1]}" if schedule else ""
        logger.info(f"[{i+1}/{total}] {num_str}{title}{sched_info}")

        ok = False
        daily_limit = False
        this_draft_id = None
        try:
            if is_draft:
                ok, this_draft_id, err = await draft_one_chapter(
                    page, new_chapter_url, chapter_num, title, content,
                    max_retries=max_retries, skip_first_goto=(i == 0 and on_editor),
                    err_tag=err_tag_fn(i))
            else:
                ok, err = await publish_one_chapter(
                    page, new_chapter_url, chapter_num, title, content,
                    schedule=schedule[i] if schedule else None,
                    use_ai=use_ai, max_retries=max_retries,
                    skip_first_goto=(i == 0 and on_editor), err_tag=err_tag_fn(i))
            if not ok:
                # 重试与失败截图都在原语里做过了，这里只记账
                fail_list.append((f"{num_str}{title}", err))
        except DailyLimitReached as e:
            # 字数额度耗尽。继续提交后续章节只会重复撞限或触发别的拦截
            # （实测续发会产生额外错误），故中止整批；本章与所有剩余章节
            # 如实记入清单。**每日**明天接着发，**每月**得等下月——所以
            # 措辞走 limit_label，别一律说成"每日"（见那里的注释）。
            lab = limit_label(e)
            logger.warning(f"  达{lab}（{e}），中止整批")
            if lab.startswith("每月"):
                logger.warning("  这是**每月**额度耗尽，明天再跑同样发不动；"
                               "请先核对余量够不够撑到下月（tools/remap 会算）")
            fail_list.append((f"{num_str}{title}", str(e)))
            daily_limit = True
        except Exception as e:
            # 兜底浏览器崩溃/页面被关等意外——原语正常不会漏非上限异常，但它
            # 重试间隔的 wait_for_timeout 在页面已死时就会抛，而这里原先只接
            # DailyLimitReached：异常穿透整个执行器 → 入口的外层 except →
            # **汇总与补传清单一起丢掉**。日志里 5 次
            # "上传异常: Target page, context or browser has been closed"
            # 全是这么没的账，用户根本不知道哪些章没发。
            # run_edit_batch 早有这道兜底，创建路径漏了——而创建缺章补不回原位。
            # ok 仍为 False，下面的失败分支会照常记账、按不可逆规则中止，并把
            # 剩余章记入补传清单。
            logger.error(f"  本章发生未预期异常: {e}")
            fail_list.append((f"{num_str}{title}", f"未预期异常: {e}"))

        if daily_limit:
            failed += 1
            # 中止整批：把所有剩余未处理章节如实记入清单（不是静默丢弃）。
            rest = record_rest_unprocessed(fail_list, parsed, i + 1, total,
                                           reason=f"{lab}，未处理")
            failed += rest
            if rest:
                logger.warning(f"  剩余 {rest} 章未处理（{lab}），已记入清单")
            _progress(total, total)
            break
        elif ok:
            # 新建类（定时/立即发布）必须逐章确认: 失败不可逆——章节根本
            # 不存在，而新建只能追加到书尾，中段缺口补不回原位。改内容/
            # 改排期不需要（item 还在，重来即可），存草稿也不需要。
            cnum_int = None
            if not is_draft and chapter_num is not None:
                try:
                    cnum_int = int(chapter_num)
                except (TypeError, ValueError):
                    cnum_int = None
            if cnum_int is not None and not await confirm_chapter_on_platform(
                    page, sig_urls, cnum_int, volume_id=tail_vol):
                logger.error(
                    f"  ✗ 提交说成功，但平台上查不到 第{cnum_int}章 —— 中止整批"
                    f"（继续发下去会把缺口永久卡在中段）")
                failed += 1
                fail_list.append((f"{num_str}{title}", "提交后平台查不到该章"))
                failed += record_rest_unprocessed(
                    fail_list, parsed, i + 1, total, reason="前方中止，未处理")
                _progress(total, total)
                break
            success += 1
            consec_fail = 0
            if cnum_int is not None:
                claimed_nums.append(cnum_int)
            elif is_draft and chapter_num is not None:
                # 草稿也记账，收尾拿草稿箱真实内容对账（不逐章确认）
                try:
                    claimed_nums.append(int(chapter_num))
                except (TypeError, ValueError):
                    pass
            # 存草稿防覆盖漏账：番茄有时把"新建章"复用到同一个进行中的草稿上，
            # 本章会覆盖上一章。若检测到 draftId 被复用，说明上一占用者其实已被
            # 覆盖、未独立保存——把它移出成功、记入补传清单（第N章号会被
            # log_fail_list 压进补传号），避免"报存成功却实际丢章"。
            if is_draft and this_draft_id:
                prev = draft_owner.get(this_draft_id)
                if prev is not None:
                    prev_label, prev_num = prev
                    logger.warning(
                        f"  ⚠ 本章复用草稿ID {this_draft_id}，"
                        f"覆盖了上一章「{prev_label.strip()}」")
                    fail_list.append(
                        (prev_label, "草稿被后续章节覆盖（平台复用草稿ID），未独立保存"))
                    success -= 1
                    failed += 1
                    # 必须同时从对账名单里摘掉：它已经在这里记过一次失败了，
                    # 而批末对账在草稿箱里同样找不到它（本来就是同一件事），
                    # 不摘就会被重复计一次 —— 成功数能被减成负的，
                    # 而 _upload_done 把 success<0 当成运行异常报「定时执行失败」。
                    if prev_num is not None and prev_num in claimed_nums:
                        claimed_nums.remove(prev_num)
                _cur_num = None
                if chapter_num is not None:
                    try:
                        _cur_num = int(chapter_num)
                    except (TypeError, ValueError):
                        _cur_num = None
                draft_owner[this_draft_id] = (f"{num_str}{title}", _cur_num)
            elif is_draft and not this_draft_id:
                logger.warning("  ⚠ 未能读取草稿ID，无法确认是否独立保存，请到草稿箱核对")
        else:
            failed += 1
            consec_fail += 1
            if not is_draft:
                # 新建类失败即停: 跳过这一章继续发，缺口就永久卡在中段了
                # （新建只能追加到书尾，补不回原位）。停在队尾无害——
                # 修好原因后按补传清单接着发即可。
                logger.error(
                    f"  发布失败且不可逆（缺章补不回原位），中止整批，"
                    f"剩余 {total - (i + 1)} 章未处理")
                failed += record_rest_unprocessed(
                    fail_list, parsed, i + 1, total, reason="前方中止，未处理")
                _progress(total, total)
                break
            if consec_fail >= 3:
                rest = total - (i + 1)
                logger.error(
                    f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
                    f"中止任务，剩余 {rest} 章未处理")
                # 与每日上限路径一致：剩余章节记入清单并计数，
                # 否则汇总"成功+失败<总数"、且补传清单缺这些章节。
                failed += record_rest_unprocessed(
                    fail_list, parsed, i + 1, total, reason="流程异常中止，未处理")
                _progress(total, total)
                break

        _progress(i + 1, total)

        if i < total - 1 and delay > 0:
            try:
                await page.wait_for_timeout(delay * 1000)
            except Exception:
                # 章节间等待时页面已死（如用户关掉浏览器窗口）：停止循环，
                # 但仍走收尾对账与汇总，保住失败清单。剩余未发章节记入清单，
                # 否则汇总漏账、补传清单缺这些章。
                failed += record_rest_unprocessed(
                    fail_list, parsed, i + 1, total, reason="页面已失效，未处理")
                break

    # 解绑请求监听：每次确认轮询都会往 sig_urls 追加一条完整签名 URL，
    # 748 章的批次会攒下几千条字符串，监听器还要对页面的每个请求做子串判断。
    # fetch_chapter_items / _wait_publish_result 都在 finally 里解绑，这里同理。
    try:
        detach_sig()
    except Exception:
        pass

    # 收尾对账：日志记成功 ≠ 平台上真有（漏 151 章的教训）
    miss = await reconcile_batch_auto(
        page, book_id, claimed_nums, fail_list, is_draft=is_draft)
    success -= len(miss)
    failed += len(miss)
    return success, failed, fail_list


async def run_edit_batch(page, matched, *, use_ai=False, max_retries=2,
                         delay=3, cancel_check=None, progress_cb=None):
    """修改内容批次的完整执行循环（含批末二次尝试）。

    曾在 CLI 和 GUI 各写一份，且各自带着对方没有的防护——CLI 的二次尝试有
    「页面死亡短路」（页面关了不再逐个 goto 刷屏），GUI 有「用户取消」记账；
    合并后两个入口都同时具备。

    策略: 修改可逆（item 还在，重来即可）→ 单章失败记清单继续，连续 3 次
    原因不明失败才熔断；「标题重复」是本地重新编号的临时冲突，留待批末二次
    尝试（此时占用旧标题的章多已更新、冲突自然解除）。

    每条中止路径（熔断 / 字数上限 / 页面已死 / 用户取消）都把剩余章记入
    fail_list 并计入 failed，口径与主循环、批末二次尝试三处一致。

    matched: [(local_idx, plat_ch, ch_num, title, content), ...]
    返回 (success, failed, skipped, fail_list)。
    """
    success = 0
    failed = 0
    skipped = 0
    consec_fail = 0
    fail_list: list[tuple[str, str]] = []  # (章节标签, 失败原因)
    dup_pending: list[tuple] = []  # "重复标题"暂存，批末二次尝试
    total = len(matched)
    _progress = progress_cb or (lambda done, tot: None)

    def _breaker_abort(i):
        """连续失败熔断: 记账剩余章并推满进度，返回失败增量。

        必须记入清单 —— 只累加 skipped 的话，这些章不会出现在 log_fail_list
        末尾那行压缩章节号里，用户就拿不到可直接粘贴续跑的补传清单
        （创建路径的每条中止分支都是这么做的）。
        """
        rest = total - (i + 1)
        logger.error(
            f"连续 {consec_fail} 章原因不明失败，疑似流程异常，"
            f"中止任务，剩余 {rest} 章未处理")
        n = record_unprocessed(
            fail_list, ((m[2], m[3]) for m in matched[i + 1:]),
            reason="流程异常中止，未处理")
        _progress(total, total)
        return n

    for i, (local_idx, plat_ch, ch_num, title, content) in enumerate(matched):
        if cancel_check and cancel_check():
            # 记账口径与批末二次尝试的取消分支保持一致（见那里）
            logger.info("用户取消，中止批次。")
            failed += record_unprocessed(
                fail_list, ((m[2], m[3]) for m in matched[i:]),
                reason="用户取消，未处理")
            break

        logger.info(f"[{i+1}/{total}] 修改第{ch_num}章 {title}")

        status = plat_ch.get("status", "")
        if "审核中" in status:
            logger.warning(f"  状态「{status}」审核中，不可编辑，跳过")
            skipped += 1
            _progress(i + 1, total)
            continue

        edit_url = plat_ch.get("editUrl")
        if not edit_url:
            logger.error("无法获取编辑链接，跳过（可能审核中或平台未提供编辑入口）")
            skipped += 1
            _progress(i + 1, total)
            continue

        if edit_url.startswith("/"):
            edit_url = BASE_URL + edit_url

        try:
            ok, err = await edit_one_chapter(
                page, edit_url, ch_num, title, content,
                use_ai=use_ai, max_retries=max_retries)
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
                    failed += _breaker_abort(i)
                    break
        except DailyLimitReached as e:
            # 每日字数上限 = 平台当日额度已耗尽。继续提交后续章节只会重复
            # 撞限或触发别的拦截（实测续发产生额外错误），故中止整批；
            # 本章与所有剩余章节（含批末待二次尝试的）如实记入清单。
            lab = limit_label(e)
            logger.warning(f"  达{lab}（{e}），中止整批")
            fail_list.append((f"第{ch_num}章 {title}", str(e)))
            failed += 1
            # 剩余主循环章节 + 批末待二次尝试的章节都记为未处理。
            failed += record_unprocessed(
                fail_list, ((m[2], m[3]) for m in matched[i + 1:]),
                reason=f"{lab}，未处理")
            failed += record_unprocessed(
                fail_list, ((d[0], d[1]) for d in dup_pending),
                reason=f"{lab}，未处理")
            dup_pending = []
            rest = total - (i + 1)
            if rest:
                logger.warning(f"  剩余 {rest} 章未处理（每日字数上限），已记入清单")
            _progress(total, total)
            break
        except Exception as e:
            # edit_one_chapter 正常不会泄漏非上限异常（内部已含重试+吞错），
            # 这里兜底浏览器崩溃/页面被关等意外，按原因不明失败计入熔断，
            # 保证 save_auth/汇总仍能执行而不是整批裸抛中止。
            logger.error(f"  本章发生未预期异常: {e}")
            fail_list.append((f"第{ch_num}章 {title}", f"未预期异常: {e}"))
            failed += 1
            consec_fail += 1
            if consec_fail >= 3:
                failed += _breaker_abort(i)
                break

        _progress(i + 1, total)

        if i < total - 1 and delay > 0:
            try:
                await page.wait_for_timeout(delay * 1000)
            except Exception:
                # 章节间等待时页面已死：停止循环，仍走收尾与汇总，保住失败
                # 清单。剩余未改章节记入清单（dup_pending 由其专属循环计数）。
                failed += record_unprocessed(
                    fail_list, ((m[2], m[3]) for m in matched[i + 1:]),
                    reason="页面已失效，未处理")
                break

    # 批末二次尝试: 主循环跑完后，占用旧标题的章节多已更新、标题已释放
    if dup_pending:
        if not (cancel_check and cancel_check()):
            logger.info("")
            logger.info(f"二次尝试 {len(dup_pending)} 个标题重复的章节"
                        f"（标题搬移的临时冲突，此时多已解除）…")
        dead = False
        for k, (ch_num, title, content, edit_url) in enumerate(dup_pending):
            if cancel_check and cancel_check():
                # 中途/事前取消: 剩余章节如实计入失败清单
                for ch_num2, title2, *_ in dup_pending[k:]:
                    fail_list.append((f"第{ch_num2}章 {title2}",
                                      "标题重复(用户取消，未二次尝试)"))
                    failed += 1
                break
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
                # 二次尝试阶段撞每日上限：与主循环一致，中止整批，
                # 本条与剩余二次条目如实记入清单。
                lab = limit_label(e)
                logger.warning(f"  达{lab}（{e}），中止二次尝试")
                fail_list.append((f"第{ch_num}章 {title}", str(e)))
                failed += 1
                failed += record_unprocessed(
                    fail_list, ((d[0], d[1]) for d in dup_pending[k + 1:]),
                    reason=f"{lab}，未处理")
                break
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

    return success, failed, skipped, fail_list


def apply_cli_filters(files, parsed, args):
    """对 (files, parsed) 应用 --modified-after/before 与 --chapters。

    返回 (files, parsed)；筛完为空或参数非法时打印原因并返回 None，调用方直接
    return。**upload 和 edit 都必须调它**：这两个筛选参数挂在 upload 子命令上，
    而 --edit 也走同一个子命令 —— 只在 cmd_upload 里做筛选的话，
    `upload ... --edit --chapters 5-10` 会照单全收地把平台上**每一个**匹配到的
    章节正文都覆盖掉（而正文覆盖是不可逆的），且一声不吭。
    """
    for flag, newer in (("modified_after", True), ("modified_before", False)):
        spec = getattr(args, flag, None)
        if not spec:
            continue
        ts = parse_time_spec(spec)
        if ts is None:
            logger.error(f"时间格式错误: {spec}（应为 YYYY-MM-DD 或 "
                         f"YYYY-MM-DD HH:MM）")
            return None

        def mtime_ok(f, _ts=ts, _newer=newer):
            # 云盘「仅在线」占位文件 stat 会抛 OSError，越界 mtime 会抛
            # Overflow/ValueError —— GUI 的同一段一直有这个兜底，CLI 曾经没有，
            # 一个占位文件就能让整条命令在上传前裸崩。
            try:
                return (f.stat().st_mtime >= _ts) == _newer
            except (OSError, OverflowError, ValueError):
                logger.warning(f"  跳过读不到修改时间的文件: {f.name}")
                return False

        before = len(parsed)
        pairs = [(f, p) for f, p in zip(files, parsed) if mtime_ok(f)]
        files = [f for f, _ in pairs]
        parsed = [p for _, p in pairs]
        logger.info(f"按修改日期筛选（{'晚于' if newer else '早于'} {spec}）: "
                    f"{len(parsed)}/{before} 章")
        if not parsed:
            logger.warning("筛选后没有章节，退出。")
            return None

    if getattr(args, "chapters", None):
        try:
            pairs, active = filter_by_chapter_spec(
                list(zip(files, parsed)), args.chapters, key=lambda pf: pf[1][0])
        except ValueError as e:
            logger.error(str(e))
            return None
        if active:
            before = len(parsed)
            files = [f for f, _ in pairs]
            parsed = [p for _, p in pairs]
            logger.info(f"按章节号筛选「{args.chapters}」: {len(parsed)}/{before} 章")
            if not parsed:
                logger.warning("筛选后没有章节，退出。")
                return None
    return files, parsed


def require_login_cli():
    """没登录就提示并返回 False。四个 CLI 命令原来各写一份这个判断。"""
    if AUTH_FILE.exists():
        return True
    logger.warning("请先运行 login 命令登录。")
    return False


def load_local_chapters(directory, args):
    """upload / edit 共同的开场：读配置 → 扫目录 → 解析文件。

    返回 (cfg, headless, delay, files, parsed)；任一步走不下去返回 None，
    提示已经打过了，调用方直接 return。两条命令曾各抄一份，加一种文件类型
    或改一句提示就得改两处。
    """
    cfg = load_config()
    headless = args.headless or cfg.get("headless", False)
    delay = args.delay if args.delay is not None else cfg.get(
        "delay_between_chapters", 3)
    if not directory.is_dir():
        logger.error(f"目录不存在: {directory}")
        return None
    files = get_md_files(directory)
    if not files:
        logger.warning(f"在 {directory} 及其子文件夹中没有找到 .md/.txt 文件")
        return None
    # 跳过扫描后变得无法读取的文件，保持 files/parsed 对齐
    files, parsed = parse_md_files(files)
    if not files:
        logger.warning("目录中的文件均无法读取（可能是云端离线文件或权限不足）")
        return None
    return cfg, headless, delay, files, parsed


# ---------------------------------------------------------------------------
async def cmd_books():
    if not require_login_cli():
        return

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=True)
        page = await context.new_page()

        if not await goto_with_login_retry(page, BOOK_MANAGE_URL):
            logger.error("登录状态已失效，请重新运行 login")
            await close_browser_safely(browser)
            return
        await settle_page(page)
        list_timed_out = False
        try:
            await page.wait_for_selector('a[href*="chapter-manage/"]', timeout=5000)
        except PWTimeout:
            list_timed_out = True

        books = await page.evaluate(BOOKS_JS)

        logger.info("")
        if not books and list_timed_out:
            logger.error("作品列表加载超时（多为系统繁忙或网络缓慢），登录状态未必有问题，请稍后重试")
        elif not books:
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
    if not require_login_cli():
        return

    got = load_local_chapters(directory, args)
    if got is None:
        return
    cfg, headless, delay, files, parsed = got

    # 定时发布参数
    schedule_date = getattr(args, "schedule", None)
    schedule_time = getattr(args, "time", "08:00") or "08:00"
    per_day = getattr(args, "per_day", 1) or 1
    unique_titles = getattr(args, "unique_titles", False)
    use_ai = getattr(args, "use_ai", False)

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

    got = apply_cli_filters(files, parsed, args)
    if got is None:
        return
    files, parsed = got

    # 自动接续队列: 起始日期和章号范围都由平台队列决定，不用手填。
    # 与 GUI 的「自动接续队列」勾选框对等，共用 tools/keep_ahead 的纯函数。
    if getattr(args, "auto_continue", False):
        parsed, files, schedule_date = await _auto_continue_plan(
            book_id, parsed, files, per_day, headless, args)
        if not parsed:
            logger.info("本地章节都已经在平台上了，没有可接续的。")
            return

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
        validated = validate_times(schedule_time)
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

        max_retries = cfg.get("max_retries", 2)
        is_draft = not publish and not schedule
        # 批次循环与收尾对账都在共用执行器里（CLI/GUI 同一份实现）；
        # 这里只保留 CLI 特有的 err_tag（失败截图按文件名命名）。
        success, failed, fail_list = await run_creation_batch(
            page, parsed, new_chapter_url, book_id=book_id,
            schedule=schedule, is_draft=is_draft, use_ai=use_ai,
            max_retries=max_retries, delay=delay,
            err_tag_fn=lambda i: f"{i}_{files[i].stem}")

        await save_auth(context)
        await close_browser_safely(browser)

        logger.info("")
        logger.info("=" * 40)
        logger.info("  上传完成!")
        logger.info(f"  成功: {success}  失败: {failed}")
        log_fail_list(fail_list)
        logger.info("=" * 40)


# ---------------------------------------------------------------------------
# 发布单章（CLI / GUI / tools 共用）
# ---------------------------------------------------------------------------
async def publish_one_chapter(page, new_chapter_url, chapter_num, title, content,
                              *, schedule=None, use_ai=False, max_retries=2,
                              skip_first_goto=False, err_tag=None):
    """新建并发布单章（schedule=(日期,时间) 走定时发布，否则立即发布）。

    与 edit_one_chapter 对称的原语: 一个负责"改已有章"，一个负责"发新章"，
    CLI 上传、GUI 上传、tools/keep_ahead 续排都调这里，不各写一遍循环。

    返回 (是否成功, 最后一次错误信息)。DailyLimitReached 直接向上抛——
    本章重试无意义（字数不会变），该由上层中止整批并记录剩余章节。
    """
    # 别再抬这个次数: 翻过两份日志共 225 段重试，第 2 次救回 151 段、第 3 次
    # 救回 21 段，**没有一段活到第 4 次**（第1385章 2026-07-24 那次正是第 3 次
    # 成的）。剩下 53 段的终局原因是错别字/重复内容、浏览器已关这类确定性失败，
    # 多试 7 次只是每章白烧 5 分钟。真要调，config.json 的 max_retries 就是旋钮。
    last_err = ""
    for attempt in range(1, max_retries + 2):
        try:
            # 首章首次可复用当前页面，其余情况都要导航到干净的新建页
            if not (skip_first_goto and attempt == 1):
                await page.goto(new_chapter_url)
                await wait_for_editor_ready(page)
            await fill_chapter(page, chapter_num, title, content)
            if schedule:
                date_str, time_str = schedule
                await publish_scheduled(page, date_str, time_str, use_ai=use_ai)
                logger.info(f"  -> 定时发布 {date_str} {time_str}")
            else:
                await _navigate_to_publish_settings(page, use_ai=use_ai)
                await _submit_confirm_publish(page)
                logger.info("  -> 已发布")
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
                if err_tag:
                    try:
                        err_path = SCRIPT_DIR / f"error_{err_tag}.png"
                        await page.screenshot(path=str(err_path))
                        logger.error(f"截图: {err_path}")
                    except Exception:
                        pass
    return False, last_err


async def draft_one_chapter(page, new_chapter_url, chapter_num, title, content,
                            *, max_retries=2, skip_first_goto=False, err_tag=None):
    """存草稿单章。返回 (是否成功, 草稿ID或None, 最后错误)。

    **同一章要连存两次**: 番茄会把连续两次"新建章存草稿"并到同一个草稿槽
    （第 2 次覆盖第 1 次后该槽才提交），只存 1 次会被下一章覆盖丢失。对同一章
    再存一次同内容，让"被覆盖的那次"就是本章自己，本章占满并提交自己的槽，
    下一章自然拿到新槽——逐章独立、不再隔章丢章（实测有效）。

    返回的草稿ID 供调用方做"槽位复用"检测: 两章拿到同一个 draftId 说明先存的
    那章已被覆盖，必须移出成功、记入补传清单，绝不"报存成功却实际丢章"。
    """
    last_err = ""
    for attempt in range(1, max_retries + 2):
        try:
            if not (skip_first_goto and attempt == 1):
                await page.goto(new_chapter_url)
                await wait_for_editor_ready(page)
            await fill_chapter(page, chapter_num, title, content)
            await save_draft(page)
            # 第二次: 占满本章自己的槽（见上）
            await page.goto(new_chapter_url)
            await wait_for_editor_ready(page)
            await fill_chapter(page, chapter_num, title, content)
            await save_draft(page)
            draft_id = _extract_draft_id(page.url)
            logger.info("  -> 已存草稿")
            return True, draft_id, ""
        except DailyLimitReached:
            raise
        except Exception as e:
            last_err = str(e)
            if attempt <= max_retries:
                logger.warning(f"第{attempt}次失败: {e}，重试中...")
                await page.wait_for_timeout(2000)
            else:
                logger.error(f"失败: {e}")
                if err_tag:
                    try:
                        err_path = SCRIPT_DIR / f"error_{err_tag}.png"
                        await page.screenshot(path=str(err_path))
                        logger.error(f"截图: {err_path}")
                    except Exception:
                        pass
    return False, None, last_err


# ---------------------------------------------------------------------------
# 修改单章（CLI 和 GUI 共用）
# ---------------------------------------------------------------------------
async def edit_one_chapter(
    page, edit_url: str, ch_num: int, title: str, content: str,
    *, use_ai: bool = False, max_retries: int = 2, set_num=None,
) -> tuple[bool, str]:
    """编辑单个已有章节（含重试）。返回 (是否成功, 最后一次错误信息)。

    错误信息供上层写入失败清单（真实原因优于"见日志"），并用于识别
    "重复标题"类可二次尝试的失败。

    set_num: 同时改写编辑器里的「第 __ 章」章节号。默认 None=不动（按章节号
    匹配的"修改内容"模式，号是现成的）。重排工具要把某个 item 改成另一章，
    必须连号一起改——番茄的目录顺序按 item 在卷内的位置排，章号只是标题文本。
    DailyLimitReached 不在此处捕获（本章重试无意义，字数不会变），
    直接向上抛出，由上层中止整批并记录剩余章节。
    """
    last_err = ""
    for attempt in range(1, max_retries + 2):
        try:
            await page.goto(edit_url)
            # 打开时若有「上次遗留」的旧草稿 -> "放弃"，从已发布内容开始干净重填。
            await wait_for_editor_ready(page, draft_action="放弃")
            await dismiss_edit_hint(page)
            # 只清/填标题+正文，章节号默认不动（已发布章节的号是现成的）；
            # set_num 非空时连章节号一起改写。_prepare_body 已去 md+空行。
            await clear_editor(page)
            await fill_chapter(page, set_num, title, content)
            await page.wait_for_timeout(800)
            # 关键修复：填入新内容后，番茄会把它自动存成草稿；点"下一步"时会弹
            # 「有刚刚更新的章节，是否继续编辑？」。这里必须点"继续编辑"保留我们刚填的
            # 新标题+新正文；若点"放弃"会把这次编辑整个丢掉、最终发布的还是原章节
            # （这正是"标题/正文改不动"的根因，CDP 实测确认）。
            await _navigate_to_publish_settings(
                page, use_ai=use_ai, draft_action="继续编辑")
            await _submit_confirm_publish(page)
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


# 时钟图标在中间列，平台没给稳定 class，只能按候选链探。
# 这条链探测和点击都要用: 分开写过两份，平台 DOM 一改就得同步改两处，
# 漏一处的表现是「探测到了选择器但点不动」——错误还会被超时掩盖。
_CLOCK_ICON_PICK_JS = (
    "cell.querySelector('svg')"
    " || cell.querySelector('i[class]')"
    " || cell.querySelector('span[class*=\"icon\"]')"
    " || cell.querySelector('button')"
    " || cell.querySelector('[role=\"button\"]')"
    " || cell.querySelector('[role=\"img\"]')")

_DETECT_CLOCK_ICON_JS = r"""() => {
        for (const row of document.querySelectorAll('tr')) {
            const cells = row.querySelectorAll('td');
            if (cells.length < 3) continue;
            for (let i = 1; i < cells.length - 1; i++) {
                const cell = cells[i];
                const el = ICON_PICK;
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
    }""".replace("ICON_PICK", _CLOCK_ICON_PICK_JS)

_CLICK_CLOCK_ICON_JS = r"""(targetTitle) => {
                        for (const row of document.querySelectorAll('tr')) {
                            const cells = row.querySelectorAll('td');
                            if (cells.length < 3) continue;
                            if (cells[0].textContent.trim() !== targetTitle)
                                continue;
                            for (let i = 1; i < cells.length - 1; i++) {
                                const cell = cells[i];
                                const el = ICON_PICK;
                                if (el) { el.click(); return true; }
                            }
                            return false;
                        }
                        return false;
                    }""".replace("ICON_PICK", _CLOCK_ICON_PICK_JS)


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
    await settle_page(page)

    # 等待表格出现
    try:
        await page.wait_for_selector("tr td", timeout=_browser_timeout)
    except Exception:
        logger.error("章节管理页表格未加载")
        return 0, total

    # 单卷 = "只有一卷"的特例，走同一条循环。这两条路曾各写一份调用，
    # 给 _reschedule_current_volume 加参数时漏改一处，单卷和多卷行为就会不一样。
    if volume_texts:
        targets = list(volume_texts)
    else:
        if volume_text:
            await select_volume(page, volume_text)
        targets = [None]

    for vi, vt in enumerate(targets):
        if not remaining:
            break
        if cancel_check and cancel_check():
            break
        if vt is not None:
            logger.info(f"切换到分卷 ({vi+1}/{len(targets)}): {vt}")
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
        # 走 log_fail_list 而不是逐条 error：它会把同因条目折叠成一行，并在
        # 末尾给出可直接粘贴的章节号。原来 609 章的改期批次一中止就刷几百行
        # "未处理: xxx"，还得自己从标题里抠章号才知道从哪接着改。
        failed += len(remaining)
        log_fail_list([(t, "未处理") for t in remaining])

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
    icon_selector = await page.evaluate(_DETECT_CLOCK_ICON_JS)
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
                    clicked = await page.evaluate(_CLICK_CLOCK_ICON_JS, title)

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
                try:
                    await page.wait_for_timeout(int(delay * 1000))
                except Exception:
                    # 等待时页面已死（如用户关掉浏览器窗口）：立即结束本卷扫描，
                    # 保住已有计数；remaining 由调用方如实计入"未处理"
                    logger.warning("页面已失效，停止扫描，剩余章节计入未处理")
                    return success, failed

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
    if not require_login_cli():
        return

    got = load_local_chapters(directory, args)
    if got is None:
        return
    cfg, headless, delay, files, parsed = got
    # 与 upload 同一套筛选: --edit 挂在 upload 子命令下，两条路必须都筛，
    # 否则 `--edit --chapters 5-10` 会把平台上每一个匹配章的正文都覆盖掉。
    got = apply_cli_filters(files, parsed, args)
    if got is None:
        return
    files, parsed = got
    unique_titles = getattr(args, "unique_titles", False)
    use_ai = getattr(args, "use_ai", False)
    if unique_titles:
        parsed = deduplicate_titles(parsed)

    # 获取平台章节列表
    logger.info("正在获取平台章节列表...")
    chapter_manage_url = CHAPTER_MANAGE_URL_TPL.format(book_id=book_id)

    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()

        await page.goto(chapter_manage_url)
        await settle_page(page)

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

        # 批次循环（含批末二次尝试）在共用执行器里——CLI/GUI 同一份实现。
        success, failed, skipped, fail_list = await run_edit_batch(
            page, matched, use_ai=use_ai,
            max_retries=cfg.get("max_retries", 2), delay=delay)

        await save_auth(context)
        await close_browser_safely(browser)

        logger.info("")
        logger.info("=" * 40)
        skip_str = f"  跳过: {skipped}" if skipped else ""
        logger.info(f"  修改完成! 成功: {success}  失败: {failed}{skip_str}")
        log_fail_list(fail_list)
        logger.info("=" * 40)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _auto_continue_plan(book_id, parsed, files, per_day, headless,
                              args=None):
    """按平台队列自动决定"发哪几章、从哪天起"。返回 (parsed, files, 起始日期)。

    只取平台最大章号之后的章——中段缺口靠"发上去"补不回原位（新建只能追加到
    书尾），那是 remap 的活，这里只提醒不碰。
    """
    mod = _load_tool_module("keep_ahead")
    num2path = {}
    for (num, _t, _c) in parsed:
        if num is None:
            continue
        try:
            num2path[int(num)] = True
        except (TypeError, ValueError):
            pass
    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()
        try:
            items, _sig, _vols = await fetch_chapter_items(page, book_id)
        finally:
            await close_browser_safely(browser)
    days_ahead = getattr(args, "days_ahead", None)
    nums, start, _days, tail, gaps = mod.plan_refill(
        items, num2path, all_remaining=days_ahead is None,
        days_ahead=days_ahead or 0, per_day=max(1, per_day))
    mode = ("全部补齐" if days_ahead is None
            else f"只排到 {days_ahead} 天后")
    logger.info(f"自动接续（{mode}）: 平台队列排到 {tail}，从 {start} 起接着排")
    if gaps:
        logger.warning(
            f"⚠ 平台中段还缺 {len(gaps)} 章（如 第"
            + "、第".join(str(n) for n in gaps[:5])
            + "章…）——这些不能靠发布补回原位，请用 remap 子命令处理")
    keep = set(nums)
    kept = [(p_, f_) for p_, f_ in zip(parsed, files)
            if p_[0] is not None and str(p_[0]).isdigit() and int(p_[0]) in keep]
    if not kept:
        return [], [], None
    logger.info(f"自动接续: 本次发 {len(kept)} 章，第{nums[0]}~{nums[-1]}章")
    return [k[0] for k in kept], [k[1] for k in kept], start.strftime("%Y-%m-%d")


_TOOL_CACHE: dict = {}


def _load_tool_module(name):
    """按路径加载 tools/<name>/<name>.py，同名只加载一次。

    这些能力最初是救火脚本，后来转正；主 CLI 通过子命令统一暴露它们，
    实现仍留在各自模块里（一份实现，两个入口）。

    必须缓存: 每次 exec_module 都会重新跑一遍模块顶层，其中包含
    sys.path.insert(0, ROOT) —— GUI 每次「自动接续」上传都会加载一次
    keep_ahead，一场长会话下来 sys.path 里堆几十条重复项，拖慢之后所有 import。
    """
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    import importlib.util
    path = SCRIPT_DIR / "tools" / name / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _TOOL_CACHE[name] = mod
    return mod


def _run_tool_cmd(name, args, *, force_readonly=False):
    """把子命令的参数原样转交给对应工具模块，外壳走共用的 run_unattended。

    force_readonly: 只读子命令（audit）用——不让 --daily 把 run 置成 True。
    """
    mod = _load_tool_module(name)
    for field, default in (("run", False), ("only", None), ("range", None),
                           ("limit", None), ("safety_minutes", 60),
                           ("daily", False), ("headless", False),
                           ("show_browser", False), ("use_ai", False),
                           ("audit", False), ("force", False),
                           ("book_id", None), ("content_dir", None)):
        if not hasattr(args, field):
            setattr(args, field, default)
    run_unattended(mod.main_async, args, log_dir=mod.LOG_DIR, name=name,
                   readonly=force_readonly)


async def cmd_reschedule_cli(args):
    """批量改排期（CLI）。GUI 一直有这功能，CLI 之前是缺的。"""
    if not require_login_cli():
        return
    cfg = load_config()
    headless = args.headless or cfg.get("headless", False)
    validated = validate_times(args.time)
    if not validated:
        logger.error(f"发布时间格式错误: {args.time}")
        return
    try:
        datetime.strptime(args.schedule, "%Y-%m-%d")
    except ValueError:
        logger.error(f"日期格式错误: {args.schedule}（应为 YYYY-MM-DD）")
        return
    per_day = max(1, args.per_day)
    async with async_playwright() as p:
        browser, context = await create_context(p, headless=headless)
        page = await context.new_page()
        try:
            url = CHAPTER_MANAGE_URL_TPL.format(book_id=args.book_id)
            if not await goto_with_login_retry(page, url):
                logger.error("被重定向到登录页，登录状态可能已失效（请重新运行 login）")
                return
            await settle_page(page)

            # 多卷: --all-volumes 逐卷合并，否则只排当前卷
            vol_texts = None
            if args.all_volumes:
                vol_info = await detect_volumes(page)
                if vol_info.get("hasVolumes"):
                    vol_texts = [v["text"] if isinstance(v, dict) else v
                                 for v in vol_info.get("volumes", [])] or None

            all_chapters = []
            if vol_texts:
                for vt in vol_texts:
                    await select_volume(page, vt)
                    chs, _ = await extract_chapters_from_page(page, args.book_id)
                    all_chapters.extend(chs)
            else:
                all_chapters, _ = await extract_chapters_from_page(
                    page, args.book_id)

            # 只有「待发布」能改排期；顺序与 GUI 一致（列表是倒序展示的）
            pending = [ch for ch in reversed(all_chapters)
                       if "待发布" in ch.get("status", "")]
            if not pending:
                logger.warning("这部作品里没有「待发布」状态的章节，"
                               "已发布的章节不能再改排期。")
                return

            schedule = compute_schedule(
                len(pending), args.schedule, args.time, per_day)
            schedule_map = {}
            dups = []
            for i, ch in enumerate(pending):
                t = ch.get("title", "")
                if t in schedule_map:
                    dups.append(t)
                schedule_map[t] = schedule[i]
            if dups:
                # 排期按标题匹配行，同名会互相覆盖、排错章 —— 无人值守下直接中止
                logger.error(
                    "存在同名章节: " + "、".join(dict.fromkeys(dups))
                    + " —— 排期按标题匹配，同名会错配，已中止。"
                      "请先在平台修改章节标题后再试。")
                return

            logger.info(f"待发布章节 {len(pending)} 个，"
                        f"排期 {schedule[0][0]} ~ {schedule[-1][0]}")
            ok, bad = await reschedule_on_manage_page(
                page, args.book_id, schedule_map,
                max_retries=cfg.get("max_retries", 2),
                delay=cfg.get("delay_between_chapters", 3),
                volume_texts=vol_texts)
            logger.info("=" * 40)
            logger.info(f"  修改排期完成! 成功: {ok}  失败: {bad}")
            logger.info("=" * 40)
        finally:
            await save_auth(context)
            await close_browser_safely(browser)


# 子命令间重复出现的参数说明。写成常量而不是各处手打: 原来有 12 个参数干脆
# 没写 help（用户 -h 看到的是一片空白），写了的几处措辞还各不相同。
_H_BOOK_ID = "目标作品 ID（默认取上次 GUI 选的）"
_H_CONTENT_DIR = "本地章节目录（默认取 config.json 的 chapters_dir）"
_H_HEADLESS = "无头模式（不显示浏览器窗口）"
_H_SHOW_BROWSER = "强制显示浏览器窗口（覆盖 config.json 的 headless）"


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
  %(prog)s upload ./chapters --book-id 12345 --schedule 2026-09-01
      从 3/14 起每天 1 章, 默认 08:00 发布

  %(prog)s upload ./chapters --book-id 12345 --schedule 2026-09-01 --per-day 3
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
        help="定时发布起始日期, 格式 YYYY-MM-DD；排到过去的日期平台可能拒绝",
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
        "--modified-after", metavar="TIME",
        help="只操作在此时间之后修改过的文件，"
             "格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（与 GUI「按修改日期筛选」一致）",
    )
    up.add_argument(
        "--modified-before", metavar="TIME",
        help="只操作在此时间之前修改过的文件",
    )
    up.add_argument(
        "--chapters", metavar="SPEC",
        help="按章节号筛选。30=只第30章（GUI 那边数字总跟着 ≤/≥ 下拉框，"
             "要阈值请写 ≥30 / ≤30）；也支持 5-10 / 1,3,5-10 / <30 / >30。"
             "批量失败后清单给的章节号（如 79-114）可原样粘到这里补传",
    )
    up.add_argument(
        "--auto-continue", action="store_true",
        help="自动接续队列: 起始日期和章号范围都按平台队列自动算"
             "（只发平台最大章号之后的章，中段缺口交给 remap）",
    )
    up.add_argument(
        "--days-ahead", type=int, metavar="N",
        help="配合 --auto-continue: 只排到 N 天后（今天+N 天），"
             "而不是把本地剩下的全排上去。排得近，想改剧情时好改；也留着断更保护",
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

    # remap: 章节重排（把未公开段的错位内容整体前移，消掉中段缺口）
    rm = sub.add_parser("remap", help="章节重排：修未公开段的位置错位")
    rm.add_argument("--book-id", help=_H_BOOK_ID)
    rm.add_argument("--content-dir", help=_H_CONTENT_DIR)
    rm.add_argument("--run", action="store_true", help="真的写入（不加只预览）")
    rm.add_argument("--only", type=int, help="只改指定位置（全书位置）")
    rm.add_argument("--range", help="只改位置区间，如 709-800")
    rm.add_argument("--limit", type=int, help="本次最多改多少个")
    rm.add_argument("--safety-minutes", type=int, default=60,
                    help="跳过多少分钟内就要发布的章（默认 60）")
    rm.add_argument("--daily", action="store_true",
                    help="无人值守日常跑：等于 --run --headless，写日志、需人工时弹窗")
    rm.add_argument("--headless", action="store_true", help=_H_HEADLESS)
    rm.add_argument("--show-browser", action="store_true", help=_H_SHOW_BROWSER)
    rm.add_argument("--use-ai", action="store_true",
                    help="改写内容时申报「使用 AI 创作」")

    # audit: 缺口体检（remap 的只读模式，单独给个名字更好找）
    ad = sub.add_parser("audit", help="缺口体检：按「谁能修」分段报告，只读")
    ad.add_argument("--book-id", help=_H_BOOK_ID)
    ad.add_argument("--content-dir", help=_H_CONTENT_DIR)
    ad.add_argument("--headless", action="store_true", help=_H_HEADLESS)
    ad.add_argument("--show-browser", action="store_true", help=_H_SHOW_BROWSER)
    ad.add_argument("--daily", action="store_true",
                    help="无人值守日常体检：写日志，发现错位或余量告急时弹窗"
                         "（只读，不改任何东西——适合挂计划任务当烟雾报警器）")

    # clean-drafts: 清空草稿箱
    cd = sub.add_parser("clean-drafts", help="清空草稿箱（带本地源文件安全检查）")
    cd.add_argument("--book-id", help=_H_BOOK_ID)
    cd.add_argument("--content-dir", help=_H_CONTENT_DIR + "，用于安全检查")
    cd.add_argument("--run", action="store_true", help="真的删除（不加只预览）")
    cd.add_argument("--limit", type=int, help="本次最多删多少条")
    cd.add_argument("--force", action="store_true",
                    help="安全检查不通过也照删（会丢内容，慎用）")
    cd.add_argument("--headless", action="store_true", help=_H_HEADLESS)
    cd.add_argument("--show-browser", action="store_true", help=_H_SHOW_BROWSER)

    # reschedule: 批量改排期（GUI 一直有，CLI 之前缺）
    rs = sub.add_parser("reschedule", help="批量修改待发布章节的排期")
    rs.add_argument("--book-id", required=True, help="目标作品 ID")
    rs.add_argument("--schedule", metavar="DATE", required=True,
                    help="起始日期 YYYY-MM-DD")
    rs.add_argument("--time", default="08:00",
                    help="发布时间，多个逗号分隔，如 08:00,12:00,20:00")
    rs.add_argument("--per-day", type=int, default=1, help="每天章数")
    rs.add_argument("--headless", action="store_true", help=_H_HEADLESS)
    rs.add_argument("--all-volumes", action="store_true",
                    help="合并所有卷一起排（不加则只排当前卷）")

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
    elif args.command == "remap":
        _run_tool_cmd("remap", args)
    elif args.command == "audit":
        # audit 是只读的：--daily 只要日志+告警，绝不能像别的工具那样被置成
        # run=True（那会变成真改写）。所以这里显式钉死 run=False。
        args.audit = True
        args.run = False
        _run_tool_cmd("remap", args, force_readonly=True)
    elif args.command == "clean-drafts":
        _run_tool_cmd("clean_drafts", args)
    elif args.command == "reschedule":
        asyncio.run(cmd_reschedule_cli(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
