---
license: apache-2.0
tags:
  - document-layout-analysis
  - table-structure-recognition
  - onnx
  - kreuzberg
---

# Kreuzberg Layout Models

ONNX models used by [Kreuzberg](https://kreuzberg.dev) for document layout detection and table structure recognition.

## Models

### RT-DETR (Document Layout Detection)

| Property | Value |
|----------|-------|
| **Path** | `rtdetr/model.onnx` |
| **Size** | 169 MB |
| **Precision** | FP32 |
| **Architecture** | RT-DETR v2 (Real-Time Detection Transformer) |
| **Input** | `images`: `[batch, 3, 640, 640]` f32 (ImageNet-normalized, letterboxed) |
| **Input** | `orig_target_sizes`: `[batch, 2]` i64 (original `[height, width]`) |
| **Outputs** | `labels` i64, `boxes` f32 `[batch, N, 4]`, `scores` f32 |
| **Classes** | 17 document layout classes |
| **SHA256** | `3bf2fb0ee6df87435b7ae47f0f3930ec3dc97ec56fd824acc6d57bc7a6b89ef2` |

**Layout Classes:** Caption, Footnote, Formula, ListItem, PageFooter, PageHeader, Picture, SectionHeader, Table, Text, Title, DocumentIndex, Code, CheckboxSelected, CheckboxUnselected, Form, KeyValueRegion

### TATR (Table Structure Recognition)

| Property | Value |
|----------|-------|
| **Path** | `tatr/model.onnx` |
| **Size** | 29 MB |
| **Precision** | INT8 quantized |
| **Architecture** | DETR (DEtection TRansformer) — non-autoregressive object detection |
| **Input** | `pixel_values`: `[batch, 3, H, W]` f32 (variable size, typically 800×800) |
| **Outputs** | `logits` f32 `[batch, 125, 7]` (class probabilities), `pred_boxes` f32 `[batch, 125, 4]` (normalized cx/cy/w/h) |
| **Classes** | 7 classes (see below) |
| **SHA256** | see release commit |

**Table Structure Classes:**
0. `table` — entire table region
1. `table column` — column span
2. `table row` — row span
3. `table column header` — header row cells
4. `table projected row header` — projected row header
5. `table spanning cell` — cells spanning multiple rows/columns
6. `no object` — background

## Attribution & Provenance

### RT-DETR

This model is mirrored from [docling-project/docling-layout-heron-onnx](https://huggingface.co/docling-project/docling-layout-heron-onnx), created by the [Docling](https://github.com/docling-project/docling) team at IBM Research.

- **Original repository:** [docling-project/docling-layout-heron-onnx](https://huggingface.co/docling-project/docling-layout-heron-onnx)
- **License:** Apache-2.0
- **Architecture paper:** Zhao et al., "DETRs Beat YOLOs on Real-time Object Detection" ([arXiv:2304.08069](https://arxiv.org/abs/2304.08069))
- **Training data:** DocLayNet and internal IBM document datasets

### TATR (Table Transformer)

This model is based on [microsoft/table-transformer-structure-recognition](https://huggingface.co/microsoft/table-transformer-structure-recognition) by Microsoft Research. The ONNX conversion was produced by [Xenova/table-transformer-structure-recognition](https://huggingface.co/Xenova/table-transformer-structure-recognition) using HuggingFace Optimum. Quantized to INT8 for inference efficiency.

- **Original repository:** [microsoft/table-transformer-structure-recognition](https://huggingface.co/microsoft/table-transformer-structure-recognition)
- **ONNX source:** [Xenova/table-transformer-structure-recognition](https://huggingface.co/Xenova/table-transformer-structure-recognition)
- **License:** MIT
- **Architecture paper:** Smock et al., "PubTables-1M: Towards comprehensive table extraction from unstructured documents" ([arXiv:2110.00061](https://arxiv.org/abs/2110.00061))
- **Training data:** PubTables-1M dataset
- **Quantization:** INT8 (dynamic quantization via ONNX Runtime)

## Usage

These models are automatically downloaded and cached by the [Kreuzberg](https://kreuzberg.dev) document extraction library. See the [layout extraction documentation](https://kreuzberg.dev) for details.

## License

- RT-DETR: [Apache-2.0 License](https://www.apache.org/licenses/LICENSE-2.0)
- TATR: [MIT License](https://opensource.org/licenses/MIT)
