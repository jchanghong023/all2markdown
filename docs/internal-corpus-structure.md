# 内部文档语料结构参考

## 使用边界

本文仅记录一批公司内部文档的**聚合结构特征**，用于指导转换代码设计、诊断和公开回归夹具建设。

禁止在本文、代码注释、测试夹具、提交记录或公开问题中写入、复制或推导以下信息：

- 原始文件名、目录、业务主题、项目名或人员信息；
- 文档标题、正文、表格数据、批注、备注、页眉页脚文字或 OCR 结果；
- 截图、原图、嵌入附件、文档属性、作者信息或可反查源文件的哈希；
- 从内部文档生成的 Markdown、日志全文或中间 JSON。

原始文件只可用于本地只读验证，不得复制到仓库。公开测试必须使用从零构造、内容无业务含义的合成夹具。

## 语料规模

本次结构扫描覆盖 110 个顶层文件：

| 容器类型 | 数量 |
| --- | ---: |
| PPTX | 40 |
| DOCX | 33 |
| XLSX | 33 |
| PDF | 4 |

其中 OOXML 文件共 106 个。93 个 OOXML 文件通过正文、幻灯片或工作表 drawing 直接引用图片；13 个没有此类直接图片引用。

这些数字用于确定测试矩阵的相对重要性，不应成为实现中的硬编码限制。

## 覆盖结论与缺口

本参考覆盖了本批内部语料中实际观察到的全部顶层容器格式和全部 OOXML/PDF 图片格式，但**不等于覆盖产品或 Xberg 支持的全部格式**。内部语料只能提供结构风险样本，不能代替完整的合成格式矩阵。

### 顶层输入格式覆盖

| 格式族 | 本批观察 | 覆盖结论 |
| --- | --- | --- |
| PDF | 有 | 已覆盖本批结构；仍需合成扫描页、特殊图片编码和损坏输入 |
| DOCX | 有 | 已覆盖普通 OOXML 文档容器 |
| PPTX | 有 | 已覆盖普通 OOXML 演示文稿容器 |
| XLSX | 有 | 已覆盖普通 OOXML 工作簿容器 |
| DOCM、DOTX、DOTM | 无 | 与 DOCX 同族但含宏或模板差异，必须另建合成夹具 |
| PPTM、PPSX、POTX、POTM | 无 | 与 PPTX 同族但入口、宏和模板关系不同，必须另建合成夹具 |
| XLTX、XLSM、XLSB、XLAM | 无 | 模板、宏、二进制工作簿和加载项未被本批语料覆盖 |
| 旧版二进制 Office：DOC、PPT、XLS | 无 | 解析器和嵌入对象行为不同，必须使用公开合成或公开许可夹具 |
| OpenDocument：ODT、ODP、ODS | 无 | ZIP 结构与 OOXML 不同，必须独立测试 |
| RTF、HTML、TXT、Markdown、XML、CSV | 无 | 由运行时格式清单决定，不能从 OOXML 结果推断 |
| 邮件、电子书、归档及其他复合格式 | 无 | 递归、资源限制和子文档合并必须按运行时支持项测试 |
| 独立图片文件 | 无 | 必须按下方完整图片格式矩阵测试 |
| MP4、M4A | 无 | 属于独立媒体转录链，不由本文的文档图片统计覆盖 |
| Xberg `/formats` 返回的其他格式 | 无覆盖证明 | 支持集合由固定运行时决定；应以运行时格式清单和公开合成烟测验证 |

测试报告必须区分“内部语料观察到”“代码声明支持”“已经由合成测试验证”三种状态，禁止由其中一种推导另外两种。

### 图片格式覆盖

| 图片格式族 | 本批观察 | 必须覆盖的实现路径 |
| --- | --- | --- |
| PNG | 有 | 标准解码、透明背景、损坏输入、超大尺寸 |
| JPEG/JPG | 有 | 标准解码、EXIF 方向、CMYK/灰度、损坏输入 |
| GIF | 有 | 静态图和动画首帧/多帧策略 |
| TIFF/TIF | 仅在 OOXML 包媒体中观察到 | 单页、多页、不同压缩方式和方向 |
| BMP | 无 | 标准解码和尺寸限制 |
| WebP | 无 | 有损、无损、透明通道 |
| PNM/PBM/PGM/PPM | 无 | 各子格式及 ASCII/二进制编码 |
| JP2/J2K/J2C/JPX/JPM/MJ2 | 无 | JPEG 2000 特殊解码、颜色空间和尺寸限制 |
| JBIG2/JB2 | 无 | PDF/独立图片特殊解码和尺寸限制 |
| EMF | 有 | 光栅化、目标 DPI、背景合成、资源释放 |
| WMF | 有 | placeable 与 standard header、光栅化和尺寸限制 |
| SVG | 无 | 安全解析、外部资源禁用、字体策略和光栅化 |
| PDF 内部图像对象 | 有 | 对象重用、软蒙版、页面光栅与内嵌图像去重 |

HEIF/HEIC、AVIF 等未列入当前独立图片输入扩展名集合；若未来运行时格式清单或产品范围加入这些格式，必须同步更新本文、安装资产和合成测试，不能依赖内部语料自然出现。

## OOXML 图片结构

OOXML ZIP 包的所有 `*/media/*` 部件共 2861 个，包含：

| Magic 检测格式 | 包内媒体数 |
| --- | ---: |
| PNG | 2408 |
| JPEG | 147 |
| GIF | 3 |
| TIFF | 30 |
| EMF | 250 |
| WMF | 23 |

包内媒体统计包含母版、布局、兼容性表示和其他不一定进入正文转换的部件，不能直接视为 OCR 调用次数。未发现 BMP、WebP、SVG、JPEG 2000、JBIG2 或 PNM 家族媒体。

按不同媒体部件计数，同一媒体被多个 shape 或 relationship 重用时只计一次。正文、幻灯片和工作表 drawing 直接引用的图片共 2602 个：

| 检测格式 | 数量 | 当前解码风险 |
| --- | ---: | --- |
| PNG | 2276 | 标准 `image` 解码路径支持 |
| JPEG | 57 | 标准 `image` 解码路径支持 |
| GIF | 2 | 标准 `image` 解码路径支持；动画只应按明确策略处理 |
| EMF | 244 | 高风险；不能直接交给 `image::load_from_memory()` |
| WMF | 23 | 高风险；不能直接交给 `image::load_from_memory()` |

未发现 SVG 被正文、幻灯片或工作表 drawing 直接引用，但实现和测试仍应覆盖 SVG，因为 OOXML 合法且实际 Office 文档常见。

### WMF 头部差异

23 个 WMF 中：

- 19 个使用 Aldus placeable header，开头为 `D7 CD C6 9A`；
- 4 个使用标准 WMF header，开头为 `01 00 09 00`。

固定版本 Xberg 的格式检测只识别前一种。后一种会被标记为 `unknown`，但它仍是合法 WMF。格式识别测试必须覆盖两种头部，不能只依赖扩展名，也不能只覆盖 placeable WMF。

### 风险分布

51 个 OOXML 文件直接引用 EMF/WMF：

| 容器类型 | 文件数 |
| --- | ---: |
| PPTX | 26 |
| DOCX | 23 |
| XLSX | 2 |

直接引用的 EMF/WMF 共 267 个，约占直接引用图片的 10%。因此矢量图不是偶发边缘输入，必须作为常规路径设计和测试。

## OLE 与预览图

22 个 OOXML 文件包含传统 OLE Compound File，共 107 个 CFB 对象，magic 为 `D0 CF 11 E0`。

需要区分两个独立对象：

1. OLE 本体：可能是复合文档，固定版本当前会产生“不支持格式识别”的 warning；
2. 幻灯片或文档中的可见 preview：常以 EMF/WMF 图片存在，应作为普通可见图片光栅化并 OCR。

OLE 数量与矢量图片数量不能建立一一对应关系。一个 OLE 可能没有 preview、共享 preview，或具有多个兼容性表示；也可能存在与 OLE 无关的 EMF/WMF。

产品行为应保持：

- OLE 本体不支持时明确 warning，不静默丢失；
- preview 可读取时尽量保留其可见信息；
- preview OCR 成功不能被误报为 OLE 本体解析成功；
- 单个 OLE 或 preview 失败不终止顶层文档。

## PPTX 结构考虑点

### 图片身份与顺序

图片的稳定身份是：

```text
slide part + relationship id + relationship target + shape
```

不得使用以下方式关联 placeholder、媒体和 OCR 结果：

- `HashMap` 迭代顺序；
- ZIP member 顺序；
- 媒体文件名的数字顺序；
- 独立构造的两个数组的相同下标。

幻灯片 Markdown 通常按版面位置排序，而 relationship 和 XML shape 顺序不保证与版面顺序一致。提取图片、生成 placeholder、记录 bounding box 和回填 OCR 必须共享同一稳定身份；排序只能改变展示顺序，不能改变身份关联。

同一 relationship 被多个 shape 引用时，不得因按 relationship 去重而丢失位置。相同媒体可共享 OCR 计算结果，但每个 shape 必须保留自己的页码、位置和输出锚点。

### 字段节点

PowerPoint 段落不只有普通 `a:r`。页码、日期等可见内容可能存放在 `a:fld` 中，渲染值位于字段内部的 `a:t`。

解析段落必须按子节点文档顺序处理至少：

- `a:r`：普通 run；
- `a:fld`：字段及其缓存文本；
- `a:br`：显式换行；
- 兼容性包装中的 fallback/choice，按既定安全策略选择。

忽略 `a:fld` 会留下字段前后的普通文字，却丢失字段值，形成看似正常但语义不完整的输出。不得用删除某个常见单词的方式掩盖此类问题。

### Alt/description

图片 description、name 和 title 是辅助元数据，不等价于图片可见文字，也不等价于 OCR 结果。

当产品要求“仅保留图片 OCR 文本”时：

- OCR 成功：输出 OCR 内容；
- OCR 无文字：不凭空输出 description；
- OCR 解码失败：保留诊断，不把 description 当正文；
- 最终不得留下 Markdown 图片引用、Base64 或图片文件。

## DOCX 结构考虑点

DOCX 图片可能由正文、页眉、页脚、脚注、尾注或批注相关 part 引用。relationship 的解析必须相对于来源 part 解析 target，不能假定所有图片都从 `word/document.xml` 引用。

需要覆盖：

- PNG/JPEG 与 EMF/WMF 混排；
- 相同媒体多次引用；
- 图片与 OLE preview 并存；
- 页眉页脚图片是否进入输出的明确产品策略；
- 图片 OCR 没识别到文字与图片字节无法解码的不同 warning；
- 嵌入文档递归与顶层图片处理互不重复。

## XLSX 结构考虑点

大部分工作簿没有图片，但语料中存在通过 drawing relationship 引用 EMF 的工作簿，因此 XLSX 图片不能被假定为只含 PNG/JPEG。

需要覆盖：

- worksheet → drawing → image relationship 链；
- 一个 drawing 中多个图片 shape；
- shape 顺序与 relationship 顺序不同；
- EMF 光栅化失败不影响单元格、公式和其他工作表；
- 同一媒体被多个工作表或 drawing 重用。

## PDF 结构考虑点

4 个 PDF 的结构扫描范围为 1 至 2106 页。发现 290 个不同内嵌图片对象，共 354 次页面引用：

| 提取后格式 | 不同图片数 |
| --- | ---: |
| PNG | 215 |
| JPEG | 75 |

本批 PDF 未发现 EMF/WMF 类内嵌图片风险。PDF 仍需独立覆盖：

- 超大页数快速分流；
- 原生文字页与扫描页；
- 同一图片对象跨页重用；
- 页面光栅 OCR 与内嵌图片 OCR 的去重；
- JP2/J2K、JBIG2 等 PDF 常见特殊编码，即使本批扫描未观察到也必须保留测试。

## 解码与 OCR 错误分类

诊断必须区分以下阶段：

1. relationship/ZIP member 不存在或不可读；
2. 格式检测为 unknown；
3. 已识别格式但无法光栅化或解码；
4. 像素解码成功，但 OCR 后端执行失败；
5. OCR 正常执行，但没有识别到文字；
6. OCR 成功，但结果无法关联回原 shape。

“没有识别到文字”不能被记录成“图片格式无法解码”。更换 OCR 模型或阈值不能解决发生在像素解码之前的失败。

建议图片 warning 至少包含非正文诊断字段：

```text
container type
page/slide/sheet number
source part
relationship id
relationship target
shape-local stable id
detected format
byte length
bounded magic prefix
failure stage
error reason
```

运行日志可显示用户当前输入文件名，但测试、文档和公开问题不得记录内部文件名。

## 实现不变量

转换链必须维持以下不变量：

- 输入目录只读；
- 每个顶层输入只生成一个 Markdown；
- 图片 OCR 文本出现在对应 shape 的位置附近；
- 不依赖哈希表或 ZIP 的未定义迭代顺序；
- 不生成图片文件、Base64 或 Markdown 图片引用；
- 单图片失败不终止顶层文档；
- 失败不留下不完整最终文件；
- OLE 本体失败与 preview OCR 分别报告；
- description/alt text 不冒充 OCR 正文；
- 普通文本、字段、表格和嵌入文档不因图片修复发生行为变化。

## 公开回归夹具策略

不得直接裁剪、匿名化或重打包内部文档作为测试夹具。即使删除正文，原文件仍可能在 metadata、relationship、嵌入对象、缩略图或二进制流中保留敏感信息。

所有夹具必须从零生成，并只使用无业务含义的标记文本，例如：

```text
IMG_A_001
IMG_B_002
FIELD_003
```

最低测试矩阵：

1. PPTX：两张 PNG，XML 顺序、版面顺序和 relationship 顺序不同，验证 OCR 不互换；
2. PPTX：5 至 10 张唯一图片，多次运行结果顺序稳定；
3. PPTX：同一媒体由两个 shape 重用，OCR 可复用但位置保留两次；
4. PPTX：placeable WMF；
5. PPTX：standard-header WMF；
6. PPTX：EMF；
7. PPTX：SVG；
8. PPTX：普通 run 与 `a:fld` 混排，验证字段值不丢失；
9. PPTX：description 有文本但图片 OCR 为空，最终不输出 description；
10. PPTX：不可解码图片，产生结构化 warning 且无残留 placeholder；
11. PPTX：OLE 本体不支持但 preview 可 OCR，分别验证 warning 和可见文本；
12. DOCX：正文、页眉、页脚、脚注、尾注分别引用图片；
13. DOCX：EMF、WMF 与普通栅格图片混排，单图失败隔离；
14. XLSX：drawing 中两张图片，relationship 顺序与位置顺序不同；
15. PDF：原生文字页、扫描页、重复图片对象、软蒙版和大页数分流；
16. 独立栅格图片：PNG、JPEG/JPG、GIF、TIFF/TIF、BMP、WebP；
17. PNM 家族：PNM、PBM、PGM、PPM 的 ASCII 与二进制表示；
18. JPEG 2000 家族：JP2、J2K、J2C、JPX、JPM、MJ2；
19. JBIG2 家族：JBIG2、JB2，以及 PDF 内嵌 JBIG2；
20. 图片边界：空文件、错误扩展名、截断数据、超大尺寸、异常色彩空间和透明背景；
21. Word 家族：DOCX、DOCM、DOTX、DOTM 的入口和嵌入对象行为；
22. PowerPoint 家族：PPTX、PPTM、PPSX、POTX、POTM 的入口、页数和嵌入对象行为；
23. Excel 家族：XLSX、XLTX、XLSM、XLSB、XLAM 的入口、工作表和 drawing 行为；
24. 媒体链：MP4、M4A 的离线本地转录与错误隔离；
25. 运行时其他格式：对固定 Xberg `/formats` 清单做公开无敏感内容的最小烟测；
26. 所有格式：最终 Markdown 不含图片链接、Base64 或生成图片路径。

涉及真实 OCR 的夹具应使用清晰、高对比度、固定字体和唯一短字符串；顺序测试应优先注入确定性的假 OCR 后端或固定 OCR 结果，避免把模型精度波动误判为关联逻辑错误。真实后端集成测试另行验证像素解码和 OCR 端到端行为。

## 验证层次

建议按以下层次定位失败：

1. 单元测试：magic 检测、relationship 解析、稳定身份、字段解析；
2. 光栅化测试：输入矢量 bytes，验证尺寸、像素上限和 RGB 输出；
3. 关联测试：固定 OCR 结果回填到正确 shape；
4. Xberg 集成测试：合成 OOXML 经完整 `/extract` 返回；
5. all2markdown 集成测试：最终 Markdown 清理、单文件隔离和原子落盘；
6. 本地内部语料烟测：只记录聚合成功数和按失败阶段统计，不保存正文或原始响应。

内部语料烟测报告只允许包含聚合指标，例如：

```text
按容器类型的文件数
按格式和阶段的失败数
单文件最大 warning 数
转换成功/失败/跳过数
```

不得包含文档正文、OCR 文本、文件清单、截图或可识别业务信息。
