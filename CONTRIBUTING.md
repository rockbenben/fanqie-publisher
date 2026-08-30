# 参与开发

## 项目结构

```text
fanqie/
├── fanqie_gui.py       # GUI 界面（主入口）
├── fanqie_upload.py    # 核心上传逻辑（也支持命令行）
├── run.bat             # Windows 一键启动（自动装依赖）
├── run.sh              # macOS / Linux 启动脚本
├── requirements.txt    # Python 依赖
├── tests/              # 回归测试
├── docs/images/        # README 首屏截图（改过界面记得重截）
├── tools/remap/        # 章节重排（把未公开的待发布章按位置重装内容，消掉中段缺口）
├── tools/keep_ahead/   # 续排（把本地还没发的章接在队列末尾按天排期）
├── tools/clean_drafts/ # 草稿箱清理（带本地源文件安全检查）
├── config.json         # 配置文件（自动生成，不纳入版本控制）
├── .gui_state.json     # GUI 内部状态（自动生成，勿手动编辑）
├── .auth_state.json    # 当前活跃登录状态（自动生成）
├── .auth_*.json        # 各命名账号的登录状态（自动生成）
├── fanqie_error.log    # 运行日志（自动生成，2MB 自动轮转）
└── chapters/           # 默认章节文件夹（启动时自动创建，支持子文件夹）
```

GUI 和命令行共用 `fanqie_upload.py` 里的同一套核心函数——**同一个能力不要写两遍**。`tests/test_no_duplicate_logic.py` 和 `tests/test_entrypoint_parity.py` 就是守这条的：前者查重复实现，后者查「GUI 有的能力命令行也得有」。

## 测试

`tests/` 下的回归测试绝大多数零依赖（不需要浏览器或番茄账号），直接运行即可：

```bash
python tests/test_chapter_filter.py     # 单个套件
for f in tests/test_*.py; do python "$f" || break; done   # 整目录
```

例外是 `test_js_blocks_browser.py`、`test_js_blocks_browser2.py`：它们需要 Playwright 浏览器内核来跑 DOM 夹具，缺内核时自动 SKIP，不影响其余套件。

覆盖的几类：

| 类别 | 守的是什么 |
| ---- | ---------- |
| 发布结果判定 | 「按钮消失」不等于提交成功——必须拿到接口 `code=0` 或页面跳转 |
| 批次对账 | 日志记成功的章，平台上必须真的存在 |
| 不可逆模式 | 新建类逐章确认 + 失败即停；改写类批末对账 |
| 解析纯函数 | 章节号识别、排期计算（含同日保序）、Markdown 清洗、HTML 标签 |
| 入口对等 | 每个能力和参数 CLI/GUI 都要有，且共用同一个解析器 |
| 重复实现回归 | 一个功能只能有一份实现 |

## CI

`.github/workflows/ci.yml` 在推送和 PR 时跑下面这些（文档只改 `.md` / `docs/` 不触发）：

| 检查 | 范围 |
| ---- | ---- |
| 回归测试 | 全部套件 × （Ubuntu / Windows）×（Python 3.10 / 3.14）= 4 个 job |
| 工具自检 | remap / keep_ahead / clean_drafts 的 `--self-check` |
| 子命令冒烟 | 各子命令的 `-h`（argparse 挂了不会有测试报错，但用户第一条命令就跑不通） |
| pyflakes | 主程序 + tools + tests |

**为什么矩阵里有 3.10：** README、支持范围表、`run.bat` 的版本检查三处都声称下限是 3.10，
这个 job 就是那三句话的唯一证据。**为什么有 Windows：** 主要用户双击 `run.bat` 启动，
路径与编码差异只有它能暴露。

需要浏览器内核的 DOM 夹具测试不进每次推送（省掉每次约 150MB 下载），
要跑在 Actions 页面手动触发 `browser-tests`。

**改动核心逻辑后整目录跑一遍确认全绿。** 尤其是发布路径——这个项目历史上出过「日志说成功、平台实际漏 151 章」的静默故障，那两道防线全靠测试守着。
