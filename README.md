离线、基于 Rust 的快速文档转 Markdown 工具。支持 Office、PDF、图片、MP4/M4A 等 100 多种格式。

# all2markdown

## 功能

将目录中的文档转换为 Markdown，并保持原始目录层级。已有对应 Markdown 时自动跳过，不删除、不覆盖。每个顶层输入只生成一份 Markdown；嵌入文档合并到同一文件，不生成图片文件、Base64 或 Markdown 图片引用。

图片和扫描件的 OCR 文本会放在水平分隔线之间的 `text` 代码块中，避免识别出的 `#`、`---` 等字符改变 Markdown 章节结构。嵌入 Office/Visio 包的内部 XML 部件不会作为独立文档输出。

## 系统要求

- Windows 11 x64
- 支持 AVX2 的 CPU；仅使用 CPU 推理
- 初始化前已安装任意 Windows x64 Python 3.8 或更高版本
- 首次初始化需要网络；初始化完成后的转换强制离线

预装 Python 只用于启动初始化程序，不运行转换。产品固定使用 `uv` 管理的 Python 3.12 和项目 `.venv`。

## 安装

在项目根目录双击 `init.cmd`，或运行：

```bat
$env:UV_SYSTEM_CERTS = "true"
init.cmd
```

初始化会完成以下操作：

1. 在 `.tmp\init-bootstrap` 创建临时引导环境并安装固定版本 `uv`。
2. 下载并管理 Python 3.12。
3. 创建项目 `.venv`，同步 `requirements.txt` 中锁定的运行时包。
4. 从 `jchanghong023/xberg` 的 GitHub Latest Release 解析并下载最新 Windows CLI，再从该 CLI 的内置 `cache manifest` 解析与当前版本匹配的 OCR/Layout 模型；ONNX Runtime、SenseVoice 和 Silero VAD 使用安装清单中的固定版本。
5. 对 Xberg 发布包、解压出的 CLI 和每个模型/运行时资产校验大小及 SHA-256，并执行包导入及 `xberg.exe --version` 检查。

非 Python 资产约 573 MB；托管 Python、运行时包和下载缓存还会占用额外空间，建议至少预留 2 GB。初始化可安全重复执行：每次都会查询 Xberg Latest Release，并读取该版本内置的模型清单；当前版本及有效文件直接复用，发布更新、模型修订变化、缺失或损坏文件自动修复。解析出的 Xberg 发布身份、模型修订和校验值记录在本地 `release.json`；下载中断后会尽量从临时分片续传。

校验失败时，`init.cmd` 返回非零状态，不接受未验证文件，也不会用失败下载覆盖已有最终文件。修复网络或镜像内容后重新运行即可。

### 默认安装位置

| 内容 | 默认位置 |
| --- | --- |
| 项目运行环境 | `<项目目录>\.venv` |
| 模型根目录 | `%USERPROFILE%\.models\all2markdown` |
| Xberg 模型缓存 | `%USERPROFILE%\.models\all2markdown\xberg\latest\hf` |
| 媒体模型 | `%USERPROFILE%\.models\all2markdown\sherpa_onnx\v1.13.6` |
| Xberg 运行时 | `%LOCALAPPDATA%\all2markdown\xberg\latest\runtime` |
| Xberg 提取缓存 | `%LOCALAPPDATA%\all2markdown\xberg\latest\cache` |
| 托管 Python | `%LOCALAPPDATA%\all2markdown\python` |
| uv 下载缓存 | `%LOCALAPPDATA%\all2markdown\uv-cache` |
| 上游安装时许可文件 | `%LOCALAPPDATA%\all2markdown\licenses` |

### 初始化环境变量

在运行 `init.cmd` 前设置：

| 变量 | 作用 |
| --- | --- |
| `ALL2MARKDOWN_MODEL_DIR` | 替换模型根目录。相对路径按项目根目录解析。 |
| `ALL2MARKDOWN_DATA_DIR` | 替换运行时、缓存、托管 Python 和安装许可文件根目录。相对路径按项目根目录解析。 |
| `ALL2MARKDOWN_PYPI_INDEX_URL` | 替换 Python 包索引，同时用于引导 pip 和 uv。 |
| `ALL2MARKDOWN_ASSET_MIRROR_URL` | 替换清单中的固定非 Python 资产来源；按 `mirror_path` 拼接。Latest Xberg 必须直接查询 GitHub，设置此变量时初始化会明确失败而不会回退官方源。 |
| `UV_PYTHON_INSTALL_MIRROR` | 指定 uv 托管 Python 下载镜像。 |

标准 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 等代理变量会由 Python、pip 和 uv 正常继承。索引、代理或镜像包含凭据时，初始化日志会隐藏凭据。固定资产按 `src/config/install_assets.json` 校验；Latest Xberg 同时校验 GitHub Release API 返回的压缩包 SHA-256，并以该 CLI 的内置模型清单校验 OCR/Layout 模型，最终把发布、解压成员及模型校验值记录到本地状态。

## 使用

1. 将需要转换的文件放入项目根目录的 `input` 文件夹。
2. 双击 `all2markdown.cmd`，或运行：

   ```bat
   all2markdown.cmd
   ```

3. 转换后的 Markdown 位于 `output` 文件夹中。

也可指定输入、输出和参数：

```bat
all2markdown.cmd input output --timeout 7200
all2markdown.cmd input output --xberg-config my_xberg_config.json
```

`all2markdown.cmd` 只使用项目 `.venv\Scripts\python.exe`。如果尚未初始化，会提示先运行 `init.cmd`，不会回退到 PATH 或系统 Python。

## 常用配置

默认配置文件为 `src/config/xberg_offline.json`。修改前建议先复制一份，保持 JSON 语法有效（布尔值使用 `true`/`false`，末项后不能有逗号）。表中的点号表示逐层嵌套，例如 `concurrency.max_threads` 对应 `"concurrency": {"max_threads": 12}`。

| 配置项 | 默认值 | 修改说明 |
| --- | --- | --- |
| `use_cache` | `true` | 是否使用 Xberg 提取缓存。排查缓存结果时可临时改为 `false`。 |
| `disable_ocr` | `false` | 改为 `true` 可完全关闭 OCR；扫描件可能因此无文字输出。 |
| `force_ocr` | `false` | 改为 `true` 可强制 OCR；不要与 `disable_ocr` 同时开启。 |
| `ocr.paddle_ocr_config.model_tier` | `"tiny"` | 安装清单只提供 `tiny`，不要改为其他档位。 |
| `ocr.paddle_ocr_config.det_limit_side_len` | `1280` | OCR 检测缩放边长。提高可能改善小字识别，但会增加内存和处理时间。 |
| `ocr.paddle_ocr_config.drop_score` | `0.35` | OCR 文本置信度下限。降低会保留更多文字，也可能增加误识别。 |
| `images.run_ocr_on_images` | `true` | 是否对文档中的图片执行 OCR。 |
| `pdf_options.extract_tables` | `true` | 是否提取 PDF 表格。 |
| `concurrency.max_threads` | `12` | Xberg 单文件处理使用的 CPU 线程数；程序固定逐个提交文档。 |
| `large_document.enabled` | `true` | 大文档快速模式会关闭布局分析、图片提取和图片 OCR。 |
| `large_document.page_threshold` | `200` | 页数严格大于该值时进入快速模式；无法识别页数时使用普通模式。 |
| `max_embedded_file_bytes` | `52428800` | 单个嵌入文件的最大字节数，默认约 50 MiB。 |
| `max_archive_depth` | `3` | 压缩包或嵌入内容的最大递归深度。 |

无论如何修改配置，`output` 中已存在的对应 Markdown 都会直接跳过。需要让新配置作用于某个文件时，请先自行移走该文件已有的输出 Markdown。

转换阶段设置 Hugging Face 离线变量、固定本地模型缓存并强制 CPU provider；不会联网补下载模型，也不会读取其他历史 Hugging Face 缓存。若预检报告资产缺失或大小不符，请重新运行 `init.cmd`。

确认 Markdown 无误后，可以手动删除 `input` 中的原始文档；这不会影响已生成的 Markdown。
