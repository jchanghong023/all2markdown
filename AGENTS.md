# all2markdown 项目约束

## 目标

本仓库是可开源分发的 Windows 11 x64 离线文档转换工具。安装入口是 `init.cmd`，转换入口是 `all2markdown.cmd`（调用根目录 `all2markdown.py`）。项目只负责把原始文件转换为 Markdown，不包含主题合并、私人文档或历史转换产物。

## 功能边界

- `src/all2markdown_core.py` 负责递归扫描、跳过已有输出、路由、Xberg 服务生命周期、结果组织、日志、错误隔离和原子落盘。
- 文档解析、OCR、Layout/Table、嵌入文档递归全部由固定版本 Xberg 完成；不得在本项目内实现 Office/PDF 解析器。
- `.mp4` / `.m4a` 路由到 `src/convert_mp4.py` 的本地 PyAV + Silero VAD + SenseVoice INT8 链路；该文件是内部实现模块，不是第二个产品入口。
- 每个顶层输入只生成一份 Markdown，嵌入内容合并到同一文件；不得生成图片文件、Base64 或 Markdown 图片引用。

## 平台、安装与离线要求

- 仅支持 Windows 11 x64、纯 CPU、AVX2。初始化可使用预装的 Windows x64 Python 3.8+；产品执行固定使用 uv 管理的 Python 3.12 和项目 `.venv`。
- `init.cmd` 是唯一安装入口；它可以联网下载托管 Python、锁定包和固定资产。转换阶段必须强制离线，不得联网补下载或读取无关的用户历史缓存。
- Xberg 固定 v1.0.14（commit `18be764`），ONNX Runtime 固定 1.24.2。运行时安装到 `%LOCALAPPDATA%\all2markdown\xberg\v1.0.14\runtime`（或 `ALL2MARKDOWN_DATA_DIR` 对应位置）。
- Xberg/OCR/Layout 模型安装到 `%USERPROFILE%\.models\all2markdown\xberg\v1.0.14`，媒体模型安装到同一模型根目录的 `sherpa_onnx\v1.13.6`（或 `ALL2MARKDOWN_MODEL_DIR` 对应位置）。
- `src/config/install_assets.json` 是非 Python 资产 URL、镜像路径、目标路径、大小和 SHA-256 的唯一来源；初始化必须校验 SHA-256，转换预检必须校验存在性和大小。
- 媒体链路固定 sherpa-onnx 1.13.6、PyAV 18.1.0、NumPy 2.5.2；不得改变 Mandarin、ITN、Silero VAD 或 CPU provider 行为。
- `requirements.txt` 只含运行时包，`requirements-dev.txt` 只增加测试夹具依赖；所有包版本必须使用 `==` 锁定。
- 不得使用 Git LFS，不得提交运行时、模型、wheel、Python 解释器、下载缓存或安装产物。仓库只保留安装清单和 `licenses/third_party/` 下的小型许可/归属文本。
- 运行时必须设置 Hugging Face 全部离线变量、固定本项目用户模型缓存并使用 CPU execution provider。

## 转换行为

- 递归扫描输入目录；`.mp4` / `.m4a` 由媒体模块处理，其余文件按 Xberg `/formats` 结果过滤。
- 输出保持输入目录层级，文件名保留原扩展名并转换为小写后缀（例如 `a.docx` → `a_docx.md`）。
- 对应 Markdown 已存在时直接跳过，不删除、不覆盖、不生成进度或状态文件。
- 单文件失败不得终止批次；失败不得留下不完整的最终 Markdown；使用临时文件加 `os.replace` 原子提交。
- 输入目录只读；项目临时文件统一写入 `.tmp/`。

## 代码与验证

- 只是打包/安装重构时不得改变转换功能。保留现有 Xberg 配置、Fast/Normal 大文档分流、嵌入递归、媒体转录、跳过、错误隔离和原子输出行为；优先最小改动。
- 安装代码必须兼容 Python 3.8 标准库；转换代码运行于托管 Python 3.12。
- 安装必须可重复执行、可从中断恢复；不得用失败下载覆盖已有最终文件，也不得在显式资产镜像失败后静默访问官方源。
- 测试放在 `tests/`，公开夹具放在 `tests/test_example/`，不得提交私人源文档或生成的业务 Markdown。
- 修改后至少运行静态导入/语法检查和可运行的单元测试。已执行 `init.cmd` 且用户资产完整时，真实 Xberg 与 79 秒 ASR 集成测试不得跳过；否则跳过原因必须明确要求运行 `init.cmd`。
- 不提交构建产物、`.venv`、缓存或临时文件。
