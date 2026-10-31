# -*- coding: utf-8 -*-
"""按钮可发现性的回归测试（零依赖：不建 Tk 窗口）。

守的是 2026-10-31 改的界面约定。改坏了不会崩，但会退回「新用户找不到按钮 /
搞不清用哪个」的状态，所以钉在这里：

1) 引导栏那颗「下一步」按钮永远跟着界面上那颗真按钮走。
   文字取自目标按钮、能不能点也取自目标按钮——引导栏不另判一套。
   两套判据对不上时用户看到的是「引导栏说能点、点了却弹『未登录』」。

2) 提示语跟着运行状态走。任务跑着的时候不能还在说「点开始上传」——
   那会让人把自己正在跑的活给停了。

3) 四个维护按钮的**名字是这套工具自己的词汇**（缺口 / 排期 / 重排 / 草稿箱），
   不另起一套说法；而「什么时候用我」写在每颗按钮的悬停提示第一句里——
   名字只说得出动的是什么，说不出什么时候该用它，新手正是卡在这一步。

4) 主按钮的状态改动只能走 _set_upload_btn 一处。直接 configure 就会
   绕过同步，引导栏那颗立刻开始说假话。

运行:  python tests/test_button_ux.py
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fanqie_gui import (  # noqa: E402
    FanqieGUI, GUIDE_CTA, STEP_HINTS, BTN_AUDIT, BTN_FIX_SCHED,
    BTN_REMAP, BTN_CLEAN, TIP_AUDIT, TIP_FIX_SCHED, TIP_REMAP, TIP_CLEAN,
)

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL [{name}] {detail}"
    PASS += 1
    print(f"  PASS {name}")


class Btn:
    """ttk.Button 的最小替身：只存文字和状态。"""

    def __init__(self, text="", state="normal"):
        self.kw = {"text": text, "state": state}

    def cget(self, k):
        return self.kw.get(k, "")

    def configure(self, **kw):
        self.kw.update(kw)


class Stub:
    """FanqieGUI 的最小替身：只提供引导栏同步用到的那几个字段。"""

    # 静态方法要包一层 staticmethod，否则赋到类属性上会变成实例方法，
    # 调用时把 self 当成第一个参数传进去。
    _compute_onboarding_step = staticmethod(FanqieGUI._compute_onboarding_step)
    _sync_guide_cta = FanqieGUI._sync_guide_cta
    _guide_hint = FanqieGUI._guide_hint
    _upload_btn_disabled = FanqieGUI._upload_btn_disabled

    def __init__(self, state=(True, True, True)):
        self.state = state
        self._guide_ctas = [Btn(), Btn()]
        self.btn_upload = Btn("开始上传")
        self.btn_login = Btn("登录 / 新建")
        self.btn_books = Btn("↻")           # 图标按钮，自己没有名字
        self.btn_browse_dir = Btn("选择文件夹")
        self.uploading = False

    def _onboarding_state(self):
        return self.state


def cta(s, i=0):
    return s._guide_ctas[i]


# ---- ① 「下一步」按钮的文字跟着目标按钮走 ----
print("[① 引导栏按钮 = 目标按钮的镜像]")
s = Stub(state=(True, True, True))          # 第 ④ 步
s._sync_guide_cta()
check("第④步 → 「开始上传」", cta(s).cget("text") == "开始上传", cta(s).cget("text"))
check("两处入口都同步了（展开栏 + 收起条）",
      cta(s, 0).cget("text") == cta(s, 1).cget("text"))

s.btn_upload.configure(text="开始修改")
s._sync_guide_cta()
check("修改模式 → 「开始修改」", cta(s).cget("text") == "开始修改", cta(s).cget("text"))

s.btn_upload.configure(text="停止")
s._sync_guide_cta()
check("运行中 → 「停止」", cta(s).cget("text") == "停止", cta(s).cget("text"))

print("[② 目标按钮灰了，引导栏那颗也灰]")
s.btn_upload.configure(state="disabled")
s._sync_guide_cta()
check("目标禁用 → 引导栏禁用", cta(s).cget("state") == "disabled",
      cta(s).cget("state"))
s.btn_upload.configure(state="normal")
s._sync_guide_cta()
check("目标恢复 → 引导栏恢复", cta(s).cget("state") == "normal")

print("[③ 前三步各自指向对应的按钮]")
s1 = Stub(state=(False, False, False))       # 第 ① 步
s1._sync_guide_cta()
check("第①步 → 「登录 / 新建」", cta(s1).cget("text") == "登录 / 新建",
      cta(s1).cget("text"))

s2 = Stub(state=(True, False, False))        # 第 ② 步
s2._sync_guide_cta()
check("第②步 → 「刷新作品列表」（目标是个 ↻ 图标按钮，没名字）",
      cta(s2).cget("text") == "刷新作品列表", cta(s2).cget("text"))

s3 = Stub(state=(True, True, False))         # 第 ③ 步
s3._sync_guide_cta()
check("第③步 → 「选择文件夹」", cta(s3).cget("text") == "选择文件夹",
      cta(s3).cget("text"))

print("[④ GUIDE_CTA 每个步号都有映射，且目标按钮名不重不漏]")
check("步号 1..4 都有映射", sorted(GUIDE_CTA) == [1, 2, 3, 4], str(sorted(GUIDE_CTA)))
targets = [v[0] for v in GUIDE_CTA.values()]
check("四个目标互不相同", len(set(targets)) == 4, str(targets))
check("第④步指向主按钮", GUIDE_CTA[4][0] == "btn_upload")

print("[⑤ 提示语跟着运行状态走]")
s = Stub()
hint = s._guide_hint(4)
check("空闲时说「下一步」", hint.startswith("下一步"), hint)
check("空闲提示不点名具体按钮（按钮就摆在右边）",
      "开始上传" not in hint and "点「" not in hint, hint)
s.uploading = True
hint = s._guide_hint(4)
check("运行中提示改成「进行中」", hint.startswith("进行中"), hint)
check("运行中提示不再让人点「开始上传」", "开始上传" not in hint, hint)
check("运行中提示告诉人怎么中断", "停止" in hint, hint)

print("[⑥ 主按钮灰着时，提示语要说清在等什么]")
s = Stub()
s.btn_upload.configure(state="disabled")
hint = s._guide_hint(4)
check("灰按钮 → 提示说明在取章节列表", "章节列表" in hint, hint)
check("灰按钮 → 换成「在等」的说明，不是平常那句",
      hint != STEP_HINTS[4], hint)

print("[⑦ 四个维护按钮：名字是这套工具自己的词汇，且不超 6 个字]")
# 名字跟着工具的词汇表走（缺口 / 排期 / 重排 / 草稿箱）——README、审计报告、
# 命令行子命令说的是同一套话，界面上另起一套说法只会让人多记一份。
check("检查按钮叫「检查缺口」", BTN_AUDIT == "检查缺口", BTN_AUDIT)
check("修复排期按钮", BTN_FIX_SCHED == "修复排期…", BTN_FIX_SCHED)
check("重排按钮", BTN_REMAP == "章节重排…", BTN_REMAP)
check("清草稿按钮", BTN_CLEAN == "清空草稿箱…", BTN_CLEAN)
for label in (BTN_AUDIT, BTN_FIX_SCHED, BTN_REMAP, BTN_CLEAN):
    check(f"「{label}」不超过 6 个字（宽度预算）", len(label) <= 6,
          f"{len(label)} 字")

print("[⑧ 「用哪个」的答案写在提示语第一句，不在名字里]")
for name, tip in (("检查缺口", TIP_AUDIT), ("修复排期", TIP_FIX_SCHED),
                   ("章节重排", TIP_REMAP), ("清空草稿箱", TIP_CLEAN)):
    check(f"「{name}」的提示第一句是「什么时候用：」",
          tip.startswith("什么时候用："), tip[:24])
check("「检查缺口」的提示说明了流程：先查后修",
      "先点它查一遍" in TIP_AUDIT, TIP_AUDIT[:40])
check("「修复排期」说清了只改时间", "发布时间" in TIP_FIX_SCHED)
check("「章节重排」说清了什么时候才用它（改时间救不回来）",
      "救不回来" in TIP_REMAP)

print("[⑧ 每个步骤都有一句提示]")
for step in (1, 2, 3, 4):
    check(f"第 {step} 步有提示语", bool(STEP_HINTS.get(step)), str(step))

# ---- 源码级不变量：不靠 Tk 就能守住的那几条 ----
print("[⑨ 源码级不变量]")
SRC = (ROOT / "fanqie_gui.py").read_text(encoding="utf-8")

check("主按钮没有绕过 _set_upload_btn 直接 configure",
      "self.btn_upload.configure(" not in SRC,
      "直接 configure 会绕过同步，引导栏那颗立刻开始说假话")
check("有 _set_upload_btn 这个唯一入口",
      "def _set_upload_btn(self" in SRC)
check("改主按钮后会同步引导栏",
      "self._sync_guide_cta()" in SRC)

for attr in targets:
    check(f"{attr} 在源码里确实被创建",
          f"self.{attr} = ttk.Button(" in SRC, attr)

check("底栏没有多出来的分组文字标签（宽度预算）",
      'text="排错"' not in SRC and 'text="出问题时用"' not in SRC,
      "底栏是抢宽度的地方，标签一长进度条就被挤没")
check("维护按钮用的是次级样式 Tool.TButton",
      SRC.count('style="Tool.TButton"') == 4,
      str(SRC.count('style="Tool.TButton"')))
check("引导栏按钮用的是 Guide.TButton（描边红，不与主按钮抢红实心）",
      'style="Guide.TButton"' in SRC)
check("四颗维护按钮的提示用的是 TIP_* 常量（不是就地写字符串）",
      all(f"TIP_{k}" in SRC for k in ("AUDIT", "FIX_SCHED", "REMAP", "CLEAN")))
check("审计报告的「修：」指向 BTN_REMAP（报告就是那个路由器）",
      SRC.count('f"   修：点「{BTN_REMAP}」') == 2,
      str(SRC.count('f"   修：点「{BTN_REMAP}」')))

print("[⑩ README 里的按钮名与代码一致]")
README = (ROOT / "README.md").read_text(encoding="utf-8")
for label in (BTN_AUDIT, BTN_FIX_SCHED, BTN_REMAP, BTN_CLEAN):
    check(f"README 提到「{label}」", label in README, label)
check("README 提到引导栏的「下一步」按钮", "「下一步」" in README)

print(f"\nALL PASSED ({PASS} 断言)")
