#!/usr/bin/env python3
"""Transcribe MP4/M4A media directories to Markdown, fully offline on CPU.

Pipeline (fixed stack, see requirements.txt):
    MP4/M4A -> PyAV (demux/decode/resample to 16 kHz mono)
        -> Silero VAD (sherpa-onnx binding, speech segmentation)
        -> SenseVoice INT8 (sherpa-onnx offline recognizer, language=zh, use_itn)
        -> Markdown (timestamped lines, atomic write)

Only the following responsibilities live in Python:
  * preflight checks (Windows x64 / Python 3.12 / AVX2 / offline assets / deps)
  * recursive input scanning + "already transcribed" skipping
  * audio decoding, VAD segmentation, ASR scheduling
  * Markdown rendering, atomic write, per-file error isolation

PyAV, sherpa-onnx, SenseVoice, and Silero are installed and checksum-verified
by ``init.cmd`` in the managed environment and user model root. No network
access happens at conversion time.

This module is called internally by the unified entry point ``all2markdown.py``
when ``media_transcription_backend`` is ``"local"`` (the default); selecting
``"xberg"`` routes media to the Xberg phase instead. It can also run standalone
for debugging:

    python convert_mp4.py <input_dir> <output_dir>
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from . import runtime_paths
else:
    import runtime_paths  # type: ignore

REPO_ROOT = runtime_paths.REPO_ROOT


def sense_voice_dir() -> Path:
    return (
        runtime_paths.sherpa_root()
        / "models"
        / "sense_voice_zh_en_ja_ko_yue_2024_07_17"
    )


def vad_model_path() -> Path:
    return runtime_paths.sherpa_root() / "models" / "vad" / "silero_vad.onnx"


def asset_issues() -> list[str]:
    issues: list[str] = []
    for asset in runtime_paths.install_assets(group="media"):
        path = runtime_paths.asset_path(asset)
        expected_size = int(asset["size_bytes"])
        if not path.is_file():
            issues.append(f"{asset['id']}: {path}（缺失）")
        elif path.stat().st_size != expected_size:
            issues.append(
                f"{asset['id']}: {path}（大小 {path.stat().st_size}，预期 {expected_size}）"
            )
    return issues

# Fixed audio / VAD parameters (SenseVoice + Silero VAD both expect 16 kHz mono).
SAMPLE_RATE = 16000
VAD_WINDOW = 512               # Silero VAD window at 16 kHz (32 ms)
VAD_THRESHOLD = 0.25
VAD_MIN_SILENCE_DURATION = 0.5  # seconds of silence that ends a speech segment
VAD_MIN_SPEECH_DURATION = 0.5   # avoid fragmenting short pauses/noise
VAD_MAX_SPEECH_DURATION = 10.0  # long monologues are split here
VAD_BUFFER_SECONDS = 60         # internal VAD ring buffer (>= max segment length)

# Default input extensions. PyAV can demux far more, but this tool targets
# MP4-family media (.mp4 video / .m4a audio, both MP4 containers with AAC);
# extend at the command line with --ext.
DEFAULT_EXTENSIONS = (".mp4", ".m4a")

# SenseVoice language codes accepted by sherpa-onnx.
# This product is intentionally limited to Mandarin Chinese; no language
# detection or alternate SenseVoice language route is exposed.
SENSE_VOICE_LANGUAGES = ("zh",)
SENSE_VOICE_LANGUAGE = "zh"
SENSE_VOICE_USE_ITN = True

# Deterministic terminology cleanup for the chip/IC/EDA/DFT course domain.
# These are explicit substitutions only; unknown ASR text is preserved.
TERMINOLOGY_REPLACEMENTS = {
    "扫描链": "scan chain",
    "扫描使能": "scan enable",
    "扫描压缩": "scan compression",
    "扫描测试": "scan",
    "固定型故障": "stuck-at",
    "固定故障": "stuck-at",
    "转换故障": "transition fault",
    "故障覆盖率": "coverage",
    "静态时序分析": "STA",
    "标准延时格式": "SDF",
    "标准测试接口语言": "STIL",
    "威格尔": "WGL",
    "迪弗蒂": "DFT",
    "迪弗特": "DFT",
    "艾特皮吉": "ATPG",
    "A T P G": "ATPG",
    "M B I S T": "MBIST",
    "L B I S T": "LBIST",
    "B I S T": "BIST",
    "S T A": "STA",
    "S D F": "SDF",
    "S T I L": "STIL",
    "V e r i l o g": "Verilog",
    "系統维罗格": "Verilog",
    # Canonical spellings are included explicitly as the project vocabulary.
    "DFT": "DFT", "ATPG": "ATPG", "MBIST": "MBIST", "LBIST": "LBIST",
    "BIST": "BIST", "scan": "scan", "scan chain": "scan chain",
    "scan enable": "scan enable", "scan compression": "scan compression",
    "stuck-at": "stuck-at", "transition": "transition", "fault": "fault",
    "pattern": "pattern", "coverage": "coverage", "STA": "STA", "SDF": "SDF",
    "STIL": "STIL", "WGL": "WGL", "Verilog": "Verilog",
    "SystemVerilog": "SystemVerilog", "VCS": "VCS", "Verdi": "Verdi",
    "PrimeTime": "PrimeTime", "Tessent": "Tessent", "Innovus": "Innovus",
    "Genus": "Genus",
    "dft": "DFT", "atpg": "ATPG", "mbist": "MBIST", "lbist": "LBIST",
    "bist": "BIST", "verilog": "Verilog", "systemverilog": "SystemVerilog",
    "vcs": "VCS", "verdi": "Verdi", "primetime": "PrimeTime",
    "tessent": "Tessent", "innovus": "Innovus", "genus": "Genus",
}

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_UNEXPECTED = 11

# Third-party imports are optional at module level so that pure-Python helpers
# stay testable without them; preflight fails with an install hint otherwise.
try:
    import av
    import numpy as np
    import sherpa_onnx

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - depends on environment
    av = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    sherpa_onnx = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def _log() -> logging.Logger:
    return logging.getLogger("convert_mp4")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def check_platform() -> None:
    if sys.platform != "win32":
        print(f"仅支持 Windows；当前平台: {sys.platform}", file=sys.stderr)
        raise SystemExit(EXIT_PREFLIGHT)
    if platform.machine().lower() not in ("amd64", "x86_64") or sys.maxsize <= 2**32:
        print(f"仅支持 x86_64；当前架构: {platform.machine()}", file=sys.stderr)
        raise SystemExit(EXIT_PREFLIGHT)
    managed_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if (
        sys.version_info[:2] != (3, 12)
        or Path(sys.executable).resolve() != managed_python.resolve()
    ):
        print(
            "请通过 all2markdown.cmd 使用初始化生成的 Windows x64 Python 3.12 环境；"
            "如尚未初始化，请先运行 init.cmd。"
            f"\n当前解释器: {sys.executable}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_PREFLIGHT)


def check_avx2() -> bool:
    """Execute CPUID (leaf 7, sub-leaf 0) on x64 via ctypes and check the AVX2 bit."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # x64 machine code:
    #   mov eax, ecx            ; 89 C8
    #   mov ecx, edx            ; 89 D1
    #   cpuid                   ; 0F A2
    #   mov [r8], eax           ; 41 89 00
    #   mov [r8+4], ebx         ; 41 89 58 04
    #   mov [r8+8], ecx         ; 41 89 48 08
    #   mov [r8+12], edx        ; 41 89 50 0C
    #   ret                     ; C3
    code = bytes([
        0x89, 0xC8, 0x89, 0xD1, 0x0F, 0xA2,
        0x41, 0x89, 0x00, 0x41, 0x89, 0x58, 0x04,
        0x41, 0x89, 0x48, 0x08, 0x41, 0x89, 0x50, 0x0C,
        0xC3,
    ])
    MEM_COMMIT = 0x1000
    PAGE_EXECUTE_READWRITE = 0x40
    kernel32.VirtualAlloc.restype = ctypes.c_void_p
    kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
    kernel32.VirtualFree.restype = ctypes.c_int
    kernel32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    exec_mem = None
    try:
        exec_mem = kernel32.VirtualAlloc(
            None, len(code), MEM_COMMIT, PAGE_EXECUTE_READWRITE
        )
        if not exec_mem:
            return False
        ctypes.memmove(exec_mem, code, len(code))
        cpu_id = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)(exec_mem)
        max_leaf = (ctypes.c_int * 4)()
        cpu_id(0, 0, ctypes.cast(max_leaf, ctypes.c_void_p))
        if max_leaf[0] < 7:
            return False
        result = (ctypes.c_int * 4)()
        cpu_id(7, 0, ctypes.cast(result, ctypes.c_void_p))
        return bool(result[1] & (1 << 5))  # EBX bit 5 = AVX2
    except Exception:
        return False
    finally:
        if exec_mem:
            try:
                kernel32.VirtualFree(exec_mem, 0, 0x8000)  # MEM_RELEASE
            except Exception:
                pass


def check_dependencies() -> None:
    if _IMPORT_ERROR is not None:
        print(
            "缺少第三方依赖（av / numpy / sherpa_onnx）: "
            f"{_IMPORT_ERROR}\n\n请重新运行 init.cmd 以校验并修复安装。",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_PREFLIGHT)


def verify_assets() -> None:
    issues = asset_issues()
    if issues:
        print(
            "初始化媒体模型不完整或大小不符:\n  "
            + "\n  ".join(issues)
            + "\n\n请重新运行 init.cmd 以校验并修复安装。",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_PREFLIGHT)


# ---------------------------------------------------------------------------
# Pipeline pieces
# ---------------------------------------------------------------------------

@dataclass
class SpeechSegment:
    """One VAD speech segment: sample offset in the file + 16 kHz mono audio."""

    start_samples: int
    samples: "np.ndarray"


@dataclass
class TranscriptLine:
    start_seconds: float
    end_seconds: float
    text: str


def format_timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def normalize_chip_terms(text: str) -> str:
    """Apply only fixed, auditable chip-domain ASR corrections."""
    for source, target in sorted(TERMINOLOGY_REPLACEMENTS.items(), key=lambda item: -len(item[0])):
        text = text.replace(source, target)
    return text


def create_recognizer(language: str, use_itn: bool, num_threads: int) -> "sherpa_onnx.OfflineRecognizer":
    if language != SENSE_VOICE_LANGUAGE or use_itn != SENSE_VOICE_USE_ITN:
        raise ValueError("媒体转写固定为 language=zh、use_itn=true")
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str((sense_voice_dir() / "model.int8.onnx").resolve()),
        tokens=str((sense_voice_dir() / "tokens.txt").resolve()),
        num_threads=num_threads,
        provider="cpu",
        language=language,
        use_itn=use_itn,
    )


def create_vad() -> "sherpa_onnx.VoiceActivityDetector":
    return sherpa_onnx.VoiceActivityDetector(
        sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(vad_model_path().resolve()),
                threshold=VAD_THRESHOLD,
                min_silence_duration=VAD_MIN_SILENCE_DURATION,
                min_speech_duration=VAD_MIN_SPEECH_DURATION,
                window_size=VAD_WINDOW,
                max_speech_duration=VAD_MAX_SPEECH_DURATION,
            ),
            sample_rate=SAMPLE_RATE,
            num_threads=1,
            provider="cpu",
        ),
        buffer_size_in_seconds=VAD_BUFFER_SECONDS,
    )


def drain_vad(vad: "sherpa_onnx.VoiceActivityDetector", out: list[SpeechSegment]) -> None:
    # NOTE: sherpa-onnx 1.13.6 exposes ``front`` as a property whose ``samples``
    # is a lazy view into the detector's internal queue; ``pop()`` invalidates
    # it. Samples must be copied out before calling pop().
    while not vad.empty():
        seg = vad.front
        out.append(
            SpeechSegment(
                start_samples=int(seg.start),
                samples=np.asarray(seg.samples, dtype=np.float32).copy(),
            )
        )
        vad.pop()


def transcribe_file(
    path: Path,
    recognizer: "sherpa_onnx.OfflineRecognizer",
) -> tuple[float, list[TranscriptLine], bool]:
    """Decode one media file and transcribe it.

    Returns (duration_seconds, transcript_lines, has_audio_track). Audio is
    streamed through the VAD while decoding, so memory stays proportional to
    the amount of speech, not the file length.
    """
    container = av.open(str(path))
    try:
        audio_streams = container.streams.audio
        if not audio_streams:
            return 0.0, [], False
        stream = audio_streams[0]
        try:
            stream.thread_type = "AUTO"
        except Exception:  # noqa: BLE001 - not fatal if unsupported
            pass
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

        vad = create_vad()
        segments: list[SpeechSegment] = []
        pending = np.zeros(0, dtype=np.float32)
        total_samples = 0

        def feed(chunk_s16: "np.ndarray") -> None:
            nonlocal pending, total_samples
            chunk = chunk_s16.astype(np.float32) / 32768.0
            total_samples += len(chunk)
            pending = np.concatenate([pending, chunk])
            while len(pending) >= VAD_WINDOW:
                vad.accept_waveform(pending[:VAD_WINDOW])
                pending = pending[VAD_WINDOW:]
                drain_vad(vad, segments)

        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                feed(resampled.to_ndarray()[0])
        for resampled in resampler.resample(None):  # flush resampler tail
            feed(resampled.to_ndarray()[0])
        vad.flush()
        drain_vad(vad, segments)
        duration = total_samples / SAMPLE_RATE
    finally:
        container.close()

    lines: list[TranscriptLine] = []
    for seg in segments:
        if len(seg.samples) == 0:
            continue
        stream_out = recognizer.create_stream()
        stream_out.accept_waveform(SAMPLE_RATE, seg.samples)
        recognizer.decode_stream(stream_out)
        text = normalize_chip_terms(stream_out.result.text.strip())
        if not text:
            continue
        start = seg.start_samples / SAMPLE_RATE
        lines.append(TranscriptLine(start, start + len(seg.samples) / SAMPLE_RATE, text))
    return duration, lines, True


def build_markdown(
    name: str,
    duration_seconds: float,
    lines: list[TranscriptLine],
    has_audio_track: bool,
) -> str:
    parts: list[str] = [f"# {name}", ""]
    if has_audio_track:
        parts.append(f"- 音频时长: {format_timestamp(duration_seconds)}")
    else:
        parts.append("- 音频时长: 无音频轨道")
    parts.append(f"- 语音片段: {len(lines)}")
    parts += ["", "## 转录", ""]
    if not has_audio_track:
        parts += ["（无音频轨道）", ""]
    elif not lines:
        parts += ["（未检测到语音）", ""]
    else:
        for line in lines:
            parts.append(
                f"[{format_timestamp(line.start_seconds)} --> "
                f"{format_timestamp(line.end_seconds)}] {normalize_chip_terms(line.text)}"
            )
        parts.append("")
    return "\n".join(parts)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def markdown_relative_path(rel: Path) -> Path:
    """Map an input relative path to its Markdown output relative path.

    The output name embeds the original extension so the source format stays
    visible and conversions of same-stem inputs with different formats do not
    collide: ``sub/abc.docx`` -> ``sub/abc_docx.md``.  Extension is normalized
    to lowercase; inputs without an extension keep ``name.md``.
    """
    ext = rel.suffix.lstrip(".").lower()
    name = f"{rel.stem}_{ext}.md" if ext else f"{rel.stem}.md"
    return rel.with_name(name)


def media_markdown_relative_path(rel: Path) -> Path:
    """Map media with the same collision-free naming rule as document outputs.

    The original extension remains visible: ``abc.mp4`` -> ``abc_mp4.md``.
    """
    return markdown_relative_path(rel)


def parse_extensions(raw: str) -> set[str]:
    exts: set[str] = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        exts.add(item)
    return exts


def scan_inputs(
    input_dir: Path, output_dir: Path, extensions: set[str]
) -> tuple[list[Path], int]:
    todo: list[Path] = []
    skipped = 0
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        rel_md = media_markdown_relative_path(path.relative_to(input_dir))
        if (output_dir / rel_md).is_file():
            skipped += 1
            continue
        todo.append(path)
    # Large files first, so they do not become the trailing batch.
    todo.sort(key=lambda p: p.stat().st_size, reverse=True)
    return todo, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="convert_mp4",
        description=(
            "Windows 11 x64 / 纯 CPU / 完全离线：用 PyAV + Silero VAD + "
            "sherpa-onnx SenseVoice INT8 将 MP4/M4A 媒体目录转录为 Markdown"
        ),
    )
    parser.add_argument("input_dir", help="媒体目录（只读）")
    parser.add_argument("output_dir", help="Markdown 输出目录")
    parser.add_argument(
        "--ext",
        default=",".join(DEFAULT_EXTENSIONS),
        help="输入扩展名，逗号分隔（默认 .mp4,.m4a）",
    )
    parser.add_argument(
        "--num-threads",
        type=int,
        default=min(os.cpu_count() or 4, 12),
        help="ASR 推理线程数（默认 min(CPU 核数, 12)）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.StreamHandler()],
    )
    log = _log()

    try:
        preflight_t0 = time.monotonic()
        check_platform()
        if not check_avx2():
            print("CPU 不支持 AVX2（本项目最低硬件要求），终止运行", file=sys.stderr)
            return EXIT_PREFLIGHT
        check_dependencies()
        verify_assets()
        log.info(
            "预检通过（平台 / Python 3.12 / AVX2 / 依赖 / 离线模型），耗时 %.1fs",
            time.monotonic() - preflight_t0,
        )

        input_dir = Path(args.input_dir).resolve()
        output_dir = Path(args.output_dir).resolve()
        if not input_dir.is_dir():
            print(f"输入目录不存在: {input_dir}", file=sys.stderr)
            return EXIT_USAGE
        extensions = parse_extensions(args.ext)
        if not extensions:
            print("--ext 未给出任何有效扩展名", file=sys.stderr)
            return EXIT_USAGE

        output_dir.mkdir(parents=True, exist_ok=True)

        model_t0 = time.monotonic()
        recognizer = create_recognizer(
            language=SENSE_VOICE_LANGUAGE,
            use_itn=SENSE_VOICE_USE_ITN,
            num_threads=max(1, args.num_threads),
        )
        log.info(
            "SenseVoice INT8 就绪（language=%s, use_itn=%s, threads=%d），加载耗时 %.1fs",
            SENSE_VOICE_LANGUAGE,
            str(SENSE_VOICE_USE_ITN).lower(),
            max(1, args.num_threads),
            time.monotonic() - model_t0,
        )

        files, skipped = scan_inputs(input_dir, output_dir, extensions)
        if not files:
            log.info("没有需要转录的文件（全部已有对应 Markdown，或目录为空）")
            return EXIT_OK
        total = len(files)
        log.info("共 %d 个待转录文件（另跳过 %d 个）", total, skipped)

        done = ok = fail = 0
        batch_t0 = time.monotonic()
        for index, path in enumerate(files, 1):
            rel = path.relative_to(input_dir)
            tag = f"{index}/{total}"
            log.info(
                "[开始 %s] %s（%.1f MB）", tag, rel, path.stat().st_size / 1048576
            )
            file_t0 = time.monotonic()
            try:
                duration, lines, has_audio = transcribe_file(path, recognizer)
                markdown = build_markdown(str(rel.as_posix()), duration, lines, has_audio)
                rel_md = media_markdown_relative_path(rel)
                write_atomic(output_dir / rel_md, markdown)
            except Exception as exc:  # noqa: BLE001
                log.error("[失败 %s] %s: %s", tag, rel, exc)
                done += 1
                fail += 1
                continue
            done += 1
            ok += 1
            elapsed = time.monotonic() - file_t0
            speech_seconds = sum(line.end_seconds - line.start_seconds for line in lines)
            log.info(
                "[完成 %s] %s 耗时 %.1fs（音频 %.1fs，语音 %.1fs，%d 个片段）",
                tag,
                rel,
                elapsed,
                duration,
                speech_seconds,
                len(lines),
            )
            log.info(
                "进度 %d/%d（成功 %d，失败 %d，总耗时 %.0fs）",
                done,
                total,
                ok,
                fail,
                time.monotonic() - batch_t0,
            )

        log.info(
            "完成: %d ok, %d skip, %d fail，总耗时 %.1fs",
            ok,
            skipped,
            fail,
            time.monotonic() - batch_t0,
        )
        return EXIT_OK if fail == 0 else EXIT_PARTIAL
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_UNEXPECTED
        return int(exc.code)
    except Exception:  # noqa: BLE001
        log.exception("未预期错误")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
