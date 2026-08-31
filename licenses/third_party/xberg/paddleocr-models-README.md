---
license: apache-2.0
tags:
  - onnx
  - paddleocr
  - ocr
  - text-detection
  - text-recognition
pipeline_tag: image-to-text
library_name: onnxruntime
---

# PaddleOCR ONNX Models

Pre-converted PaddleOCR models in ONNX format for use with [Kreuzberg](https://github.com/Kreuzberg-dev/kreuzberg).

## Models

| File | Type | Version | Size | Description |
|------|------|---------|------|-------------|
| `ch_PP-OCRv4_det_infer.onnx` | Detection | PP-OCRv4 | 4.5 MB | Text region detection (language-agnostic) |
| `ch_ppocr_mobile_v2.0_cls_infer.onnx` | Classification | PPOCRv2 | 572 KB | Text angle classification (0° vs 180°) |
| `en_PP-OCRv4_rec_infer.onnx` | Recognition | PP-OCRv4 | 7.3 MB | English text recognition |

## Pipeline

PaddleOCR uses a three-stage pipeline:

1. **Detection** (`det`): Locates text regions in the image using a DB (Differentiable Binarization) network. The detection model is language-agnostic — it identifies bounding boxes around text regardless of script.
2. **Classification** (`cls`): Determines text orientation (upright vs upside-down) to correct rotated text before recognition.
3. **Recognition** (`rec`): Reads characters from each detected text region. This is the only language-specific model.

## Sources & Credits

These models are derived from [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), licensed under Apache 2.0.

- **Detection model** (`ch_PP-OCRv4_det_infer.onnx`): Pre-converted ONNX model sourced from [SWHL/RapidOCR](https://huggingface.co/SWHL/RapidOCR) on HuggingFace (PP-OCRv4 folder). Originally trained by PaddlePaddle as `ch_PP-OCRv4_det_infer`.
- **Classification model** (`ch_ppocr_mobile_v2.0_cls_infer.onnx`): Pre-converted ONNX model sourced from [SWHL/RapidOCR](https://huggingface.co/SWHL/RapidOCR) on HuggingFace (PP-OCRv1 folder). Originally trained by PaddlePaddle as `ch_ppocr_mobile_v2.0_cls_infer`.
- **Recognition model** (`en_PP-OCRv4_rec_infer.onnx`): Converted from PaddlePaddle format to ONNX using [paddle2onnx](https://github.com/PaddlePaddle/Paddle2ONNX) (opset 14). Original PaddlePaddle model downloaded from PaddlePaddle's official distribution (`en_PP-OCRv4_rec_infer`).

## Acknowledgments

- [PaddlePaddle](https://github.com/PaddlePaddle) for creating and training the PP-OCR model series
- [SWHL/RapidOCR](https://github.com/RapidAI/RapidOCR) for maintaining pre-converted ONNX models
- [Paddle2ONNX](https://github.com/PaddlePaddle/Paddle2ONNX) for the PaddlePaddle-to-ONNX conversion tool

## License

The original PaddleOCR models are licensed under [Apache License 2.0](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE).
