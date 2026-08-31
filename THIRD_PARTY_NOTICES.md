# THIRD-PARTY NOTICES

本仓库仅保存许可/归属文本、来源元数据和校验清单，不提交第三方运行时、模型、Python wheel 或 Python 解释器。`init.cmd` 从 `jchanghong023/xberg` 的 GitHub Latest Release 获取 Xberg，并按 `src/config/install_assets.json` 和锁定的 Python 依赖获取其他组件；转换阶段保持离线。

仓库内许可与模型卡位于 `licenses/third_party/`。Xberg 和 ONNX Runtime 压缩包中可用的上游许可/notice 还会在初始化时复制到 `%LOCALAPPDATA%\all2markdown\licenses`（或 `ALL2MARKDOWN_DATA_DIR\licenses`）；这些副本不是运行前提。

| 组件 | 版本 / 修订 | 许可证 | 许可、来源或归属 |
| --- | --- | --- | --- |
| uv | 0.12.7 | Apache-2.0 OR MIT | [astral-sh/uv](https://github.com/astral-sh/uv)；仅用于初始化、托管 Python 和同步包 |
| CPython | 3.12（由 uv 获取的 Windows x64 构建） | Python Software Foundation License | [python/cpython](https://github.com/python/cpython)、[PSF License](https://docs.python.org/3/license.html) |
| Xberg CLI | `jchanghong023/xberg` Latest Release（初始化时解析并记录具体标签） | MIT | `licenses/third_party/xberg/Xberg-LICENSE`、`Xberg-THIRD_PARTY_LICENSES.md` |
| ONNX Runtime（win-x64 CPU） | 1.24.2 | MIT | `licenses/third_party/xberg/onnxruntime-LICENSE.txt`、`onnxruntime-ThirdPartyNotices.txt` |
| RT-DETR v2（文档 Layout 检测） | 当前 Xberg CLI 内置模型清单指定的 layout-models 不可变修订 | Apache-2.0 | `licenses/third_party/xberg/layout-models-README.md` |
| TATR INT8（表格结构） | 当前 Xberg CLI 内置模型清单指定的 layout-models 不可变修订 | MIT（基于 Microsoft Table Transformer） | `licenses/third_party/xberg/layout-models-README.md` |
| PaddleOCR ONNX（PP-LCNet 文本行分类、PP-OCRv6 tiny det/rec/dict） | 当前 Xberg CLI 内置模型清单指定的 paddleocr-onnx-models 不可变修订 | Apache-2.0 | `licenses/third_party/xberg/paddleocr-models-README.md` |
| sherpa-onnx / sherpa-onnx-core | 1.13.6 | Apache-2.0 | `licenses/third_party/sherpa_onnx/LICENSE.sherpa-onnx` |
| PyAV（FFmpeg 解码） | 18.1.0 | BSD-3-Clause | `licenses/third_party/sherpa_onnx/LICENSE.pyav`；wheel 内 FFmpeg 组件遵循各自许可 |
| NumPy | 2.5.2 | BSD-3-Clause（含其分发组件许可） | `licenses/third_party/sherpa_onnx/LICENSE.numpy` |
| PyMuPDF | 1.28.2 | AGPL-3.0-only 或商业许可 | [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF)；用于 PDF 页数快速判定 |
| SenseVoice INT8 | `2365baeacb507f821a0c8120fcee3d484dba7a07`（zh-en-ja-ko-yue 2024-07-17） | MIT（模型权重源自 FunAudioLLM/SenseVoice） | `licenses/third_party/sherpa_onnx/LICENSE.sense-voice` |
| Silero VAD | sherpa-onnx `asr-models/silero_vad.onnx` | MIT | `licenses/third_party/sherpa_onnx/LICENSE.silero-vad` |

## 归属与来源

- **RT-DETR v2**：镜像自 [docling-project/docling-layout-heron-onnx](https://huggingface.co/docling-project/docling-layout-heron-onnx)（IBM Research / Docling），Apache-2.0。
- **TATR**：基于 [microsoft/table-transformer-structure-recognition](https://huggingface.co/microsoft/table-transformer-structure-recognition)（MIT），ONNX 转换自 Xenova，INT8 动态量化。
- **PaddleOCR 模型**：源自 [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)（Apache-2.0），由 xberg-io 托管 ONNX 转换版本。
- **ONNX Runtime**：Microsoft，MIT；本项目只使用 CPU execution provider，不获取 CUDA/DirectML 组件。
- **sherpa-onnx**：[k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)，Apache-2.0；用于 `src/convert_mp4.py` 的离线 Silero VAD + SenseVoice 推理。
- **SenseVoice**：源自 [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（MIT），使用 sherpa-onnx 官方 Hugging Face 仓库中的固定修订导出文件。
- **Silero VAD**：源自 [snakers4/silero-vad](https://github.com/snakers4/silero-vad)（MIT），文件取自 sherpa-onnx 固定发行资产。
- **PyAV / FFmpeg**：PyAV 为 BSD-3-Clause；其 wheel 包含的 FFmpeg 构建按各自许可分发，详见已安装 wheel 的许可目录。
- **测试夹具**：`tests/test_example/video-to-notes-intro-zh.mp4` 取自 [KIRVO-REPORTING/video-to-notes](https://github.com/KIRVO-REPORTING/video-to-notes) 的 `docs/assets/`（MIT），用于中文语音集成测试。

## 获取与再分发

- 固定非 Python 资产的 URL、修订、目标路径、大小和 SHA-256 记录在 `src/config/install_assets.json`。Latest Xberg 的仓库、API、资产名及所需模型选择器记录在该清单；初始化从 Release API 和当前 CLI 的 `cache manifest` 解析具体发布与模型修订，并把 URL、大小和校验值记录到本地 `release.json`。
- Python 运行时包只记录在 `requirements.txt`，开发/夹具依赖记录在 `requirements-dev.txt`；所有版本使用 `==` 固定。
- `init.cmd` 默认访问清单中的 GitHub、NuGet、Hugging Face 来源。显式设置 `ALL2MARKDOWN_ASSET_MIRROR_URL` 时固定资产只访问镜像；Latest Xberg 不会绕过镜像设置访问 GitHub，而是明确终止并要求取消该变量。
- 仓库本身不授予第三方组件许可证之外的再分发权。分发缓存、安装目录或下载产物时，分发者必须同时满足相应上游许可证和 notice 要求。
