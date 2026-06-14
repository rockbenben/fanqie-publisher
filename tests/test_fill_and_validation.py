# -*- coding: utf-8 -*-
"""章节号回读自愈 / 校验失明 / 存草稿误判成功 测试（零依赖）。

回归锁定（2026-07-02 线上实证）：
1. fill_chapter 只校验正文字数、从不回读章节号——第一次进编辑器章节号输入框
   未就绪时写入静默失效，章节号留空，发布时平台弹"章节序号只支持阿拉伯数字"，
   整章超时重试才偶然成功。修复后 fill 内回读+重写自愈。
2. _navigate_to_publish_settings 不识别字段校验 toast → 空转 14 轮 + 15s 误报
   "发布设置超时"（实测每章 ~43s），真实原因丢失。修复后立即抛真实原因。
3. save_draft 用 wait_for_selector("text=已保存") 会命中常驻自动保存指示器，
   把"校验被拒、实际没存成"误判为成功（草稿计数虚高）。修复后先扫错误 toast。

运行:  python tests/test_fill_and_validation.py
"""
import asyncio
import sys
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


def run(coro):
    try:
        asyncio.run(coro)
        return None
    except Exception as e:
        return e


# ---------------- fill_chapter 章节号自愈 ----------------
class FillPage:
    """模拟编辑器：章节号输入框前 `ready_after` 次写入不就绪（写入丢失）。"""

    def __init__(self, ready_after=0, wc_after_paste=2570):
        self.num_field = None        # 章节号字段当前值；None=尚不可见
        self.ready_after = ready_after
        self.write_attempts = 0
        self.wc = 0
        self.wc_after_paste = wc_after_paste
        self.clock = 0.0

    async def evaluate(self, js, *args):
        self.clock += 0.02
        # 主 fill JS：含三元素数组参数
        if args and isinstance(args[0], list) and len(args[0]) == 3:
            self.wc = self.wc_after_paste
            self._try_write(args[0][0])
            return None
        if "return inp.value" in js:  # _READ_CHAPTER_NUM_JS
            return self.num_field
        if js.strip().startswith("(num)"):  # _WRITE_CHAPTER_NUM_JS
            self._try_write(args[0])
            return True
        return None

    def _try_write(self, num):
        if not num:
            return
        self.write_attempts += 1
        if self.write_attempts > self.ready_after:
            self.num_field = str(num)
        # 否则：输入框尚未就绪，写入静默丢失（num_field 保持 None）

    def locator(self, selector, has_text=None):
        page = self

        class L:
            async def count(self):
                if selector == "text=正文字数":
                    return 1
                return 0

            async def text_content(self):
                return f"正文字数 {page.wc}"

            @property
            def first(self):
                return self
        return L()

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000


def test_fill_self_heals_chapter_num():
    # 第一次写入丢失，第二次就绪 → fill 内自愈，不抛错
    p = FillPage(ready_after=1)
    err = run(fu.fill_chapter(p, "356", "标题", "正" * 2570))
    check("章节号自愈: 不抛错", err is None, f"err={err!r}")
    check("章节号自愈: 最终写入 356", p.num_field == "356", f"got={p.num_field!r}")


def test_fill_raises_if_never_ready():
    # 章节号输入框始终不就绪 → fill 抛清晰错误（而非把空号拖到发布阶段）
    p = FillPage(ready_after=99)
    err = run(fu.fill_chapter(p, "356", "标题", "正" * 2570))
    check("章节号始终失败: 抛错", isinstance(err, RuntimeError),
          f"err={err!r}")
    check("章节号始终失败: 错误点名章节号",
          err and "章节号" in str(err), f"err={err!r}")


def test_fill_edit_mode_skips_num_check():
    # 修改模式 chapter_num=None：不做章节号校验（号是平台现成的）
    p = FillPage(ready_after=99)
    err = run(fu.fill_chapter(p, None, "标题", "正" * 2570))
    check("修改模式: 不因章节号失败", err is None, f"err={err!r}")


# ---------------- _check_editor_validation / 状态机 ----------------
class TextPage:
    """按可见文本集合驱动 .count()；toasts 经 _visible_toast_texts。"""

    def __init__(self, present=(), messages=()):
        self.present = set(present)
        self.messages = list(messages)
        self.clock = 0.0
        self.next_clicks = 0

    async def evaluate(self, js, *args):
        self.clock += 0.02
        if "arco-message" in js or "messages:" in js:
            return {"messages": self.messages, "notifications": []}
        return None

    def locator(self, selector, has_text=None):
        page = self

        class L:
            async def count(self):
                key = selector.replace("text=", "")
                if has_text:
                    return 1 if has_text in page.present else 0
                return 1 if key in page.present else 0

            async def is_visible(self):
                return await self.count() > 0

            async def click(self, **kw):
                page.next_clicks += 1

            @property
            def first(self):
                return self
        return L()

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000

    async def wait_for_selector(self, selector, timeout=None):
        self.clock += (timeout or 15000) / 1000
        raise fu.PWTimeout("wait_for_selector timeout")


def test_validation_toast_fails_fast():
    # 编辑器停着，可见"章节序号只支持阿拉伯数字"toast → 立即抛错，不空转 43s
    p = TextPage(present={"button.auto-editor-next"},
                 messages=["章节序号只支持阿拉伯数字"])
    err = run(fu._navigate_to_publish_settings(p, draft_action="放弃"))
    check("校验toast: 抛 RuntimeError", isinstance(err, RuntimeError),
          f"err={err!r}")
    check("校验toast: 带平台原文",
          err and "章节序号只支持阿拉伯数字" in str(err), f"err={err!r}")
    check("校验toast: 快速失败(虚拟<5s, 线上43s)", p.clock < 5,
          f"clock={p.clock:.1f}s")


def test_body_validation_toast():
    p = TextPage(present={"button.auto-editor-next"},
                 messages=["正文至少输入1000字"])
    err = run(fu._navigate_to_publish_settings(p, draft_action="放弃"))
    check("正文字数不足: 抛错带原文",
          isinstance(err, RuntimeError) and "正文至少" in str(err),
          f"err={err!r}")


def test_reach_settings_not_blocked_by_check():
    # 已到发布设置：即使有残留 toast 也应正常完成（发布设置分支优先）
    p = TextPage(present={"发布设置"}, messages=["章节序号只支持阿拉伯数字"])
    err = run(fu._navigate_to_publish_settings(p, draft_action="放弃"))
    check("已到发布设置: 不被残留toast误杀", err is None, f"err={err!r}")


# ---------------- save_draft 误判成功 ----------------
class DraftPage:
    def __init__(self, messages=(), saved_indicator=True):
        self.messages = list(messages)
        self.saved_indicator = saved_indicator  # 常驻"已保存"自动保存指示器
        self.clock = 0.0

    async def evaluate(self, js, *args):
        self.clock += 0.02
        if "arco-message" in js or "messages:" in js:
            return {"messages": self.messages, "notifications": []}
        return None

    def locator(self, selector, has_text=None):
        page = self

        class L:
            async def count(self):
                if has_text == "存草稿":
                    return 1
                if selector == "text=已保存":
                    return 1 if page.saved_indicator else 0
                return 0

            async def click(self, **kw):
                pass

            @property
            def first(self):
                return self
        return L()

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000


def test_save_draft_rejects_on_error_toast():
    # 校验失败 toast 在场 + 常驻"已保存"指示器也在 → 必须抛错，不能误判成功
    p = DraftPage(messages=["章节序号只支持阿拉伯数字"], saved_indicator=True)
    err = run(fu.save_draft(p))
    check("存草稿: 错误toast在场即抛错(不被'已保存'指示器掩盖)",
          isinstance(err, RuntimeError) and "存草稿失败" in str(err),
          f"err={err!r}")


def test_save_draft_success():
    p = DraftPage(messages=[], saved_indicator=True)
    err = run(fu.save_draft(p))
    check("存草稿: 正常保存不抛错", err is None, f"err={err!r}")


# ---------------- 填完内容后弹"是否继续编辑"：放弃会清空 ----------------
class DraftWipePage:
    """复现 2026-07-02 截图：填好内容(wc=2570)→点下一步→平台弹"是否继续编辑"
    (刚填的内容被自动存草稿)→
      - 点"放弃": 内容被丢弃、表单变空、卡在编辑器(空表单校验失败) → 永远到不了发布设置
      - 点"继续编辑": 保留内容 → 下一步 → 发布设置
    """

    def __init__(self):
        self.stage = "editor"     # editor -> settings
        self.wc = 2570
        self.next_clicks = 0
        self.draft_popup = False  # 首次点下一步后出现
        self.clock = 0.0

    async def evaluate(self, js, *args):
        self.clock += 0.02
        if "arco-message" in js or "messages:" in js:
            return {"messages": [], "notifications": []}
        if "正文字数" in js:
            return None
        return None

    def locator(self, selector, has_text=None):
        page = self

        class L:
            async def count(self):
                if selector == "text=发布设置":
                    return 1 if page.stage == "settings" else 0
                if selector == "text=是否继续编辑":
                    return 1 if page.draft_popup else 0
                if selector == "button.auto-editor-next":
                    return 1 if page.stage == "editor" else 0
                if selector == "button" and has_text == "继续编辑":
                    return 1 if page.draft_popup else 0
                if selector == "button" and has_text == "放弃":
                    return 1 if page.draft_popup else 0
                if selector == "text=正文字数":
                    return 1
                return 0

            async def is_visible(self):
                return await self.count() > 0

            async def text_content(self):
                return f"正文字数 {page.wc}"

            async def click(self, **kw):
                if has_text == "继续编辑":
                    page.draft_popup = False        # 保留内容
                elif has_text == "放弃":
                    page.draft_popup = False
                    page.wc = 0                      # 内容被丢弃 → 表单空
                # 编辑器"下一步"
                if selector == "button.auto-editor-next":
                    page.next_clicks += 1
                    if not page.draft_popup and page.next_clicks == 1:
                        page.draft_popup = True       # 首次下一步弹草稿恢复
                    elif page.wc > 0 and not page.draft_popup:
                        page.stage = "settings"       # 有内容才放行

            @property
            def first(self):
                return self
        return L()

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000

    async def wait_for_selector(self, selector, timeout=None):
        if selector == "text=发布设置" and self.stage == "settings":
            return
        self.clock += (timeout or 15000) / 1000
        raise fu.PWTimeout("wait_for_selector timeout")


def test_keep_draft_preserves_content():
    # 新默认 draft_action="继续编辑"：内容保留、到达发布设置
    p = DraftWipePage()
    err = run(fu._navigate_to_publish_settings(p))  # 用新默认值
    check("继续编辑: 到达发布设置", err is None, f"err={err!r}")
    check("继续编辑: 内容未被清空", p.wc == 2570, f"wc={p.wc}")


def test_discard_draft_wipes_content_old_bug():
    # 旧行为 draft_action="放弃"：复现内容被清空 + 到不了发布设置（锁住回归方向）
    p = DraftWipePage()
    err = run(fu._navigate_to_publish_settings(p, draft_action="放弃"))
    check("放弃(旧bug): 内容被清空", p.wc == 0, f"wc={p.wc}")
    check("放弃(旧bug): 到不了发布设置", err is not None, f"err={err!r}")


# ---------------- 错别字确认对话框：必须点"提交"不点"取消" ----------------
class TypoConfirmPage:
    """复现"检测到你还有错别字未修改，是否确定提交？"对话框（取消/提交）。

    `submit_visible_at`: 含"提交"的按钮集合中，哪个下标是当前对话框里可见的那个
    （模拟背景另有一个不可见的"提交"排在前面 → .first 会点错）。
    """

    def __init__(self, dialog_text="检测到你还有错别字未修改，是否确定提交？",
                 submit_buttons_visibility=(True,)):
        self.dialog_text = dialog_text
        self.submit_vis = list(submit_buttons_visibility)
        self.stage = "typo"       # typo -> settings
        self.cancel_clicks = 0
        self.submit_clicked_idx = None
        self.clock = 0.0

    async def evaluate(self, js, *args):
        self.clock += 0.02
        if "arco-message" in js or "messages:" in js:
            return {"messages": [], "notifications": []}
        return None

    def locator(self, selector, has_text=None):
        page = self

        class L:
            def __init__(self, kind, idx=None):
                self.kind = kind
                self.idx = idx

            async def count(self):
                if self.kind == "text":
                    key = selector.replace("text=", "")
                    return 1 if (page.stage == "typo" and key in page.dialog_text) else 0
                if self.kind == "submit":
                    return len(page.submit_vis) if page.stage == "typo" else 0
                if self.kind == "cancel":
                    return 1 if page.stage == "typo" else 0
                if self.kind == "settings":
                    return 1 if page.stage == "settings" else 0
                if self.kind == "next":
                    return 0
                return 0

            def nth(self, i):
                return L("submit", idx=i)

            @property
            def first(self):
                return L(self.kind, idx=0)

            async def is_visible(self):
                if self.kind == "submit":
                    return page.submit_vis[self.idx or 0]
                return await self.count() > 0

            async def click(self, **kw):
                if self.kind == "submit":
                    page.submit_clicked_idx = self.idx or 0
                    page.stage = "settings"     # 提交后进入发布设置
                elif self.kind == "cancel":
                    page.cancel_clicks += 1

        if selector == "text=发布设置":
            return L("settings")
        if selector == "button.auto-editor-next":
            return L("next")
        if selector.startswith("text="):
            return L("text")
        if selector == "button" and has_text == "提交":
            return L("submit")
        if selector == "button" and has_text == "取消":
            return L("cancel")
        return L("none")

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000

    async def wait_for_selector(self, selector, timeout=None):
        if selector == "text=发布设置" and self.stage == "settings":
            return
        self.clock += (timeout or 15000) / 1000
        raise fu.PWTimeout("timeout")


def test_typo_confirm_clicks_submit():
    p = TypoConfirmPage()
    err = run(fu._navigate_to_publish_settings(p))
    check("错别字确认: 到达发布设置", err is None, f"err={err!r}")
    check("错别字确认: 点了提交", p.submit_clicked_idx is not None)
    check("错别字确认: 没点取消", p.cancel_clicks == 0,
          f"cancel_clicks={p.cancel_clicks}")


def test_typo_confirm_skips_invisible_submit():
    # 背景里有个不可见的"提交"排在前面，对话框可见的在后 → 必须点可见那个，不点取消
    p = TypoConfirmPage(submit_buttons_visibility=(False, True))
    err = run(fu._navigate_to_publish_settings(p))
    check("错别字确认(多提交按钮): 到达发布设置", err is None, f"err={err!r}")
    check("错别字确认(多提交按钮): 点了可见的那个(idx=1)",
          p.submit_clicked_idx == 1, f"idx={p.submit_clicked_idx}")
    check("错别字确认(多提交按钮): 没点取消", p.cancel_clicks == 0)


def test_typo_confirm_wording_variant():
    # 平台文案微调为"是否确认提交"，仍靠"错别字未修改"锚点命中
    p = TypoConfirmPage(dialog_text="检测到你还有错别字未修改，是否确认提交？")
    err = run(fu._navigate_to_publish_settings(p))
    check("错别字确认(文案变体): 仍点提交到达发布设置",
          err is None and p.submit_clicked_idx is not None, f"err={err!r}")


# ---------------- 内容检测方式："请选择内容检测方式" -> 仅基础检测 ----------------
class DetectMethodPage:
    """复现"请选择内容检测方式"弹窗（仅基础检测 / 全面检测）。

    needs_confirm: True 时选完还需点"确定"才推进（Arco 弹窗页脚）。
    """

    def __init__(self, needs_confirm=False):
        self.stage = "detect"     # detect -> (selected) -> settings
        self.needs_confirm = needs_confirm
        self.picked = None
        self.confirm_clicks = 0
        self.clock = 0.0

    async def evaluate(self, js, *args):
        self.clock += 0.02
        if "arco-message" in js or "messages:" in js:
            return {"messages": [], "notifications": []}
        # _CLICK_BY_TEXT_JS：参数是要点的文本
        if args and args[0] in ("仅基础检测", "全面检测"):
            self.picked = args[0]
            if not self.needs_confirm:
                self.stage = "settings"
            return True
        return None

    def locator(self, selector, has_text=None):
        page = self

        class L:
            async def count(self):
                if selector == "text=发布设置":
                    return 1 if page.stage == "settings" else 0
                if selector == "text=内容检测方式":
                    return 1 if page.stage == "detect" else 0
                if selector == "button.auto-editor-next":
                    return 0
                if selector == "button" and has_text in ("确定", "确认"):
                    return 1 if (page.stage == "detect"
                                 and page.needs_confirm) else 0
                return 0

            async def is_visible(self):
                return await self.count() > 0

            async def click(self, **kw):
                if has_text in ("确定", "确认"):
                    page.confirm_clicks += 1
                    page.stage = "settings"

            @property
            def first(self):
                return self
        return L()

    async def wait_for_timeout(self, ms):
        self.clock += ms / 1000

    async def wait_for_selector(self, selector, timeout=None):
        if selector == "text=发布设置" and self.stage == "settings":
            return
        self.clock += (timeout or 15000) / 1000
        raise fu.PWTimeout("timeout")


def test_detect_method_picks_basic():
    p = DetectMethodPage(needs_confirm=False)
    err = run(fu._navigate_to_publish_settings(p))
    check("内容检测方式: 到达发布设置", err is None, f"err={err!r}")
    check("内容检测方式: 选了仅基础检测(非全面检测)",
          p.picked == "仅基础检测", f"picked={p.picked!r}")


def test_detect_method_with_confirm():
    p = DetectMethodPage(needs_confirm=True)
    err = run(fu._navigate_to_publish_settings(p))
    check("内容检测方式(需确认): 到达发布设置", err is None, f"err={err!r}")
    check("内容检测方式(需确认): 选基础+点确定",
          p.picked == "仅基础检测" and p.confirm_clicks == 1,
          f"picked={p.picked!r} confirm={p.confirm_clicks}")


def main():
    test_fill_self_heals_chapter_num()
    test_fill_raises_if_never_ready()
    test_fill_edit_mode_skips_num_check()
    test_validation_toast_fails_fast()
    test_body_validation_toast()
    test_reach_settings_not_blocked_by_check()
    test_save_draft_rejects_on_error_toast()
    test_save_draft_success()
    test_keep_draft_preserves_content()
    test_discard_draft_wipes_content_old_bug()
    test_typo_confirm_clicks_submit()
    test_typo_confirm_skips_invisible_submit()
    test_typo_confirm_wording_variant()
    test_detect_method_picks_basic()
    test_detect_method_with_confirm()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
