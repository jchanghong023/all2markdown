# all2markdown 项目约束

## 项目概况

- 本仓库是可开源分发的 Windows 11 x64、纯 CPU、离线文档转换工具：把原始文件转换为 Markdown，不包含主题合并、私人文档或历史转换产物。
- 项目完全由 AI Agent 实现和维护。质量不得依赖用户手工阅读代码、人工比对输出或人工回归；功能是否达标以自动化测试结果为准。
- 目录职责：`input/`（默认输入）、`output/`（默认输出）、`.tmp/`（临时文件与运行日志）、`tests/`（测试与公开合成夹具）、`docs/`（说明文档）、`licenses/`（第三方许可文本）。

## 权威来源

- **需求权威文档：`REQUIREMENTS.md`**。产品目标、用户场景、功能需求与验收条件、跨功能约束、实现与验证状态、覆盖缺口和待确认问题都在那里；开工前必须先阅读与任务相关的章节。
- 本项目是自有项目（无上游 Fork），因此不维护 `FORK.md`，也不得创建与 `REQUIREMENTS.md` 平行的需求副本；其他说明性文档只能作为参考，不能成为需求权威。
- 职责划分：产品需求与用户可见行为以 `REQUIREMENTS.md` 为准；开发规则、目录职责、入口命令以本文件为准。发现两者冲突时按需求执行，并同步修正本文件的过时描述。
- 第三方组件版本、许可与归属见 `THIRD_PARTY_NOTICES.md` 与 `licenses/third_party/`。
- `docs/internal-corpus-structure.md` 是内部文档的聚合结构参考与公开测试矩阵来源。它只允许记录聚合结构特征：不得在文档、代码注释、测试夹具、提交记录或公开问题中写入原始文件名、目录、业务主题、正文、OCR 结果、截图或可反查源文件的哈希；内部原文件只能本地只读验证，不得复制进仓库。

## 主要入口与运行命令

关键代码入口：

- `all2markdown.py` → `src/all2markdown_core.py`：命令行转换实现，负责预检、递归扫描、跳过、Xberg 服务生命周期、大文档分流、结果组织、日志、错误隔离与原子落盘。
- `gui.py` → `src/gui.py`：图形界面；`src/gui_status.py` 负责环境状态检测与初始化命令构造。
- `init.cmd` → `src/init_env.py`：唯一安装入口，负责托管 Python、`.venv`、锁定依赖与固定资产。
- `src/runtime_paths.py`：安装与运行时路径、资产清单解析。
- `src/convert_mp4.py`：`.mp4` / `.m4a` 的本地转录实现模块，不是第二个产品入口。
- 配置：`src/config/xberg_offline.json`（管线配置）、`src/config/install_assets.json`（非 Python 固定资产唯一来源）。

本项目是纯 Python 脚本，没有编译构建步骤。命令（工作目录均为仓库根）：

| 用途 | 命令 | 运行条件 |
| --- | --- | --- |
| 安装/初始化 | `init.cmd` | 需联网；可用系统 Python 3.8+ |
| 图形界面 | `gui.cmd` | 已初始化时用项目 `.venv`，未初始化时用系统 Python 3.8+ 启动引导界面 |
| 命令行转换 | `all2markdown.cmd [输入目录] [输出目录] [--flat] [--exts .pdf,.docx] [--timeout 秒] [--xberg-config 文件]` | 需先运行 `init.cmd` |
| 语法/导入检查 | `.venv\Scripts\python.exe -m compileall -q all2markdown.py src tests` | 需 `.venv` |
| 全部 UT + 集成测试 | `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v` | 需 `.venv`；真实转换类用例需要已初始化资产 |
| 单个用例 | `.venv\Scripts\python.exe -m unittest tests.test_all2markdown.NormalizeMarkdownTest.test_numeric_entities_decoded -v` | 需 `.venv` |
| 端到端转换（E2E） | `all2markdown.cmd <输入目录> <输出目录>` 后检查生成的 Markdown 内容 | 需已初始化、资产完整 |
| 真实集成验收（手动） | `.venv\Scripts\python.exe fulltest.py --src <目录> --out <目录>` | 需已初始化；`--help` 查看全部选项 |
| CI | `.github/workflows/full-tests.yml` | 手动触发或推送 tag 时执行 `init.cmd` + `compileall` + `unittest discover`；推送 `v*` tag 且测试通过时另创建 GitHub Release |

测试依赖的真实资产缺失时会跳过真实 Xberg / 媒体模型用例；需要得到真实结论时必须先运行 `init.cmd`，不得把跳过当成通过。

## 平台、安装与离线约束

- 仅支持 Windows 11 x64、纯 CPU、AVX2。初始化可使用预装的 Windows x64 Python 3.8+；产品执行固定使用 uv 管理的 Python 3.12 和项目 `.venv`。
- `init.cmd` 是唯一安装入口；它可以联网下载托管 Python、锁定包和初始化资产。转换阶段必须强制离线，不得联网补下载或读取无关的用户历史缓存。
- Xberg 每次初始化都从 `jchanghong023/xberg` 的 GitHub Latest Release 下载 `xberg-cli-x86_64-pc-windows-msvc.zip`，并**整包解压**到 `%LOCALAPPDATA%\all2markdown\xberg\latest\runtime`（或 `ALL2MARKDOWN_DATA_DIR` 对应位置）。包内已含 `xberg.exe`、ONNX Runtime DLL、MSVC CRT，以及 `models/` 下的 PaddleOCR tiny / layout（RT-DETR、TATR）/ Whisper tiny 模型。`ALL2MARKDOWN_XBERG_ZIP_PATH` 可指向本地 zip 做安装验证；生产安装不得静默改走其它源。
- 转换时 `HF_HUB_CACHE` 指向包内 `runtime/models`，不再从 `cache manifest` 单独下载 Xberg/OCR/Layout 模型。媒体模型（SenseVoice / Silero VAD）仍安装到 `%USERPROFILE%\.models\all2markdown` 下的 `sherpa_onnx\v1.13.6`（或 `ALL2MARKDOWN_MODEL_DIR` 对应位置）。
- `src/config/install_assets.json` 是非 Python 固定资产的唯一来源；Xberg 运行时资产类型为 `github_release_zip_tree`。动态发布标签、URL、压缩包摘要、成员清单原子记录到本地 `release.json`。初始化必须校验 SHA-256，转换预检必须离线校验存在性和大小。
- 媒体链路固定 sherpa-onnx 1.13.6、PyAV 18.1.0、NumPy 2.5.2；不得改变 Mandarin、ITN、Silero VAD 或 CPU provider 行为。
- `requirements.txt` 只含运行时包，`requirements-dev.txt` 只增加测试夹具依赖；所有包版本必须使用 `==` 锁定。
- 不得使用 Git LFS，不得提交运行时、模型、wheel、Python 解释器、下载缓存或安装产物。仓库只保留安装清单和 `licenses/third_party/` 下的小型许可/归属文本。
- 运行时必须设置 Hugging Face 全部离线变量、`HF_HUB_CACHE` 指向包内模型目录，并使用 CPU execution provider。
- 安装必须可重复执行、可从中断恢复；不得用失败下载覆盖已有最终文件，也不得在显式资产镜像失败后静默访问官方源。
- 安装代码必须兼容 Python 3.8 标准库；转换代码运行于托管 Python 3.12。

## 代码与实现约束

- 只是打包/安装重构时不得改变转换功能。保留现有 Xberg 配置、Fast/Normal 大文档分流、嵌入递归、媒体转录、跳过、错误隔离和原子输出行为；优先最小改动。
- 文档解析、OCR、Layout/Table、嵌入文档递归全部由固定版本 Xberg 完成；不得在本项目内实现 Office/PDF 解析器。
- 输入目录只读；项目临时文件统一写入 `.tmp/`；失败不得留下不完整的最终 Markdown（临时文件 + `os.replace` 原子提交）。
- 单文件失败不得终止批次；批次级问题（预检、服务启动、用法错误）通过退出码与单文件失败区分。
- 客户端并发请求数固定为 1：单文件请求独占全部线程预算，多请求并发会成倍放大 ORT 线程并导致更慢甚至超时。吞吐只能通过大文档分流等既有手段改善。
- 不提交构建产物、`.venv`、缓存或临时文件。
- 测试放在 `tests/`，公开夹具放在 `tests/test_example/`，不得提交私人源文档或生成的业务 Markdown。
- 界面（GUI）的呈现细节（窗口尺寸、字体、配色、控件布局、日志行数上限等）不属于需求，可在保证功能可见、可操作、可读的前提下自行优化。
- 产品行为的完整定义（输出命名、跳过、图片引用保留、媒体转录、退出码等）见 `REQUIREMENTS.md` 第 4 节，本文件不再重复；已确认但尚未实施的代码变更见 `REQUIREMENTS.md` 第 8 节。

## 测试与验证要求

- 功能开发和功能性修改必须有自动化验证：UT 验证局部逻辑，E2E 验证从真实公开入口（`init.cmd` / `gui.cmd` / `all2markdown.cmd` 或它们调用的实现入口）到可观察结果的完整链路；跨模块交互按需要增加集成测试。
- UT、编译检查、静态检查和局部模拟都不能替代 E2E。已有有效测试覆盖可以复用，不要求为每处修改机械新增测试。
- 测试应对应 `REQUIREMENTS.md` 中的需求及验收条件，覆盖核心成功路径和相关关键失败路径；不得只复述实现或仅验证程序没有崩溃。
- 桩和模拟可以补充测试，但未经真实边界的验证必须说明，不能把局部模拟冒充端到端验证。
- 必须区分「已实现」「验证通过」「验证失败」「未验证」；环境、依赖或权限不足时说明未验证范围，不得声称功能已验收。
- 当前缺少 UT 或 E2E 的地方如实写明缺口与后续要求（已知缺口见 `REQUIREMENTS.md` 第 7 节），不得编造命令或降低标准。
- 任何代码修改后至少运行静态导入/语法检查和可运行的单元测试；纯文档等非功能性变更按实际影响验证，不强制运行无关的完整测试。
- 已执行 `init.cmd` 且用户资产完整时，真实 Xberg 与 79 秒 ASR 集成测试不得跳过；否则跳过原因必须明确要求运行 `init.cmd`。
- CI 通过 `ALL2MARKDOWN_REQUIRE_REAL_CONVERSION=1` 强制真实转换测试不得被跳过；本地需要同等结论时也应设置该变量。

## 文档联动

- 需求或预期用户可见行为发生变化时，必须检查并同步更新 `REQUIREMENTS.md`：需求变更直接改当前规格，不保留聊天记录或提交历史式台账。
- 仅实现方式变化且需求不变时，不制造需求变更，也不得通过改写需求来合理化实现缺陷；需求未实现不能被删除，只能如实标注实现或验证缺口。
- 入口、命令、目录职责或开发规则变化时，同步更新本文件（`AGENTS.md`）。
