#!/usr/bin/env python3
"""Convert an original-document directory to Markdown using the initialized Xberg runtime.

Only the following responsibilities live in Python:
  * preflight checks (Windows x64 / AVX2 / offline assets)
  * recursive input scanning + "already converted" skipping
  * Xberg server lifecycle and fixed configuration
  * result collection, embedded-children merge, atomic write
  * logging and per-file error isolation

All document parsing, OCR, layout and embedded-object recursion is done by the
latest Xberg Windows runtime installed and verified by ``init.cmd``.

Routing: ``.mp4`` and ``.m4a`` inputs use the local PyAV -> Silero VAD ->
sherpa-onnx SenseVoice INT8 path by default. ``media_transcription_backend``
in the pipeline config can be set to ``"xberg"`` to send those files through
the pinned Xberg API instead. Every other supported format is handled by the
same installed runtime. Conversion remains fully offline after initialization.

Large documents: PDF/DOCX/PPTX inputs get a quick metadata-only page count
(PyMuPDF / docProps app.xml / ppt/slides parts); when ``large_document`` is
enabled and the count exceeds ``page_threshold`` the per-file /extract request
uses a derived fast config (``layout: null`` -> RT-DETR + TATR off, no image
extraction/OCR). Unavailable counts, XLSX and non-PDF families stay on the
normal config. The ``large_document`` policy block never reaches Xberg.

Usage:
    python all2markdown.py <input_dir> <output_dir>
"""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import hashlib
import json
import logging
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Local module; its third-party imports (av/numpy/sherpa_onnx) are guarded,
# so importing it never breaks the stdlib-only document pipeline.
from src import convert_mp4, runtime_paths

# PyMuPDF is an optional, pinned, offline-distributed dependency used only for
# the quick PDF page count behind the large_document fast-mode routing. The
# document pipeline keeps working without it: PDFs then never obtain a page
# count and always use the normal extraction config.
try:
    import pymupdf  # type: ignore
    PYMUPDF_IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001
    pymupdf = None
    PYMUPDF_IMPORT_ERROR = str(exc)


APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent
CONFIG_PATH = APP_ROOT / "config" / "xberg_offline.json"
LOG_DIR = REPO_ROOT / ".tmp" / "logs"
TMP_DIR = REPO_ROOT / ".tmp"

# Defaults are intentionally generic so the open-source package contains no
# private source documents.  Supplying both directories explicitly is
# recommended for automation; the defaults are convenient for local use.
DEFAULT_INPUT_DIR = REPO_ROOT / "input"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"

# Fixed internal tuning values (concurrency comes from config/xberg_offline.json).
#
# CONCURRENT_REQUESTS must stay 1 on this pure-CPU pipeline: every /extract
# request carries exactly one file, so Xberg's batch plan gives that request the
# ENTIRE thread budget (input_count=1 -> 1 worker -> full concurrency.max_threads
# for ONNX Runtime intra-op + Rayon). N concurrent requests therefore multiply
# compute threads N-fold (4 x 12 = 48 threads on 8 physical cores), thrashing
# the ORT sessions and making large PDFs far slower -- and past the timeout --
# than a sequential run where each file gets the full budget alone.
CONCURRENT_REQUESTS = 1   # client-side pool of single-file /extract requests
REQUEST_TIMEOUT = 21600   # default per-file timeout in seconds (--timeout overrides)
XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES = 100 * 1024 * 1024

# Per-file heartbeat keeps long-running files observable even when the Xberg
# endpoint emits no per-page events. Every interval the worker logs elapsed
# time plus the server process's aggregate CPU and RSS, if psutil is available.
# CPU-time delta (ctypes, stdlib-only) so the console shows the file is alive
# and how hard it is computing. Overridable via XBERG_HEARTBEAT_SECONDS (tests).
HEARTBEAT_INTERVAL = float(os.environ.get("XBERG_HEARTBEAT_SECONDS", "30"))


def _process_cpu_seconds(pid: int) -> float | None:
    """User+kernel CPU seconds consumed by a Windows process (stdlib ctypes)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    k32.GetProcessTimes.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        ok = k32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return (kernel.value + user.value) / 10_000_000.0
    finally:
        k32.CloseHandle(handle)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def tail_server_log(
    log_path: Path, stop: threading.Event, interval: float = 1.0
) -> None:
    """Forward new lines from the Xberg server log to the console logger.

    Xberg's tracing events remain useful while a file runs for tens of minutes:
    model loads, cache hits, lifecycle/config warnings, and xref warnings.
    The server output uses ANSI colors; they are stripped for the console.
    """
    log = _log()
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            buf = ""
            while not stop.wait(interval):
                chunk = fh.read()
                if not chunk:
                    continue
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = _ANSI_ESCAPE.sub("", line).strip()
                    if line:
                        log.info("[XBERG] %s", line)
            if buf.strip():
                line = _ANSI_ESCAPE.sub("", buf).strip()
                if line:
                    log.info("[XBERG] %s", line)
    except Exception as exc:  # noqa: BLE001
        log.warning("[XBERG] 服务端日志转发停止: %s", exc)

# Explicit MIME hints for core formats avoid relying solely on magic-byte
# sniffing. OOXML package markers may occur beyond a small prefix and otherwise
# look like plain application/zip; a validated MIME type avoids that ambiguity.
EXT_TO_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docm": "application/vnd.ms-word.document.macroEnabled.12",
    ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    ".dotm": "application/vnd.ms-word.template.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    ".ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
    ".potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    ".potm": "application/vnd.ms-powerpoint.template.macroEnabled.12",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    ".xlam": "application/vnd.ms-excel.addin.macroEnabled.12",
}

# PPTX-family inputs are sent as base64 bytes so the content extraction route
# consistently performs embedded-document scanning.
BYTES_INPUT_EXTENSIONS = {".pptx", ".pptm", ".ppsx", ".potx", ".potm"}

# Inputs handled by Xberg's raster ImageExtractor. Only these get the
# whole-document OCR duplicate collapse (see collapse_whole_document_duplicate).
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
    ".jp2", ".j2k", ".j2c", ".jpx", ".jpm", ".mj2", ".jbig2", ".jb2",
    ".pnm", ".pbm", ".pgm", ".ppm",
}

# Large-document routing (single on-disk config, no second file). The policy
# block below is split off in split_pipeline_config(); Xberg's ExtractionConfig
# denies unknown fields so it must never reach the server config or /extract.
LARGE_DOC_KEY = "large_document"
MEDIA_BACKEND_KEY = "media_transcription_backend"
MEDIA_BACKENDS = {"local", "xberg"}
# WordprocessingML / presentation families that get a quick page/slide count.
WORD_FAMILY = {".docx", ".docm", ".dotx", ".dotm"}
PPTX_FAMILY = {".pptx", ".pptm", ".ppsx", ".potx", ".potm"}
_PPTX_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")

SUPPORTED_OCR_MODEL_TIERS = {"tiny"}

# OPC package roots whose XML parts are conversion internals, not standalone
# embedded documents. VSDX uses ``visio`` beside the OOXML roots.
OOXML_INTERNAL_ROOTS = {
    "_rels", "docProps", "customXml", "word", "ppt", "xl", "glossary", "visio"
}
OOXML_SKIP_EXTENSIONS = {"xml", "rels", "vml", "bin", "dll", "ttf", "odttf", "css"}
OOXML_SKIP_SUBDIRS = {"media", "diagrams", "theme", "_rels"}

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_SERVER = 4
EXIT_UNEXPECTED = 11


def _log() -> logging.Logger:
    return logging.getLogger("all2markdown")


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
        try:
            kernel32.VirtualFree(exec_mem, 0, 0x8000)  # MEM_RELEASE
        except Exception:
            pass


def verify_assets() -> None:
    issues: list[str] = []
    for asset in runtime_paths.install_assets(group="xberg"):
        path = runtime_paths.asset_path(asset)
        expected_size = int(asset["size_bytes"])
        if not path.is_file():
            issues.append(f"{asset['id']}: {path}（缺失）")
        elif path.stat().st_size != expected_size:
            issues.append(
                f"{asset['id']}: {path}（大小 {path.stat().st_size}，预期 {expected_size}）"
            )
    if issues:
        print(
            "初始化资产不完整或大小不符:\n  "
            + "\n  ".join(issues)
            + "\n\n请重新运行 init.cmd 以校验并修复安装。",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_PREFLIGHT)


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Xberg server lifecycle
# ---------------------------------------------------------------------------

def _xberg_request_body_limit(
    input_dir: Path, configs: tuple[dict[str, Any], ...]
) -> int:
    """Size the local API body limit for Base64 presentation requests."""
    limit = XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES
    for path in input_dir.rglob("*"):
        if path.suffix.lower() not in BYTES_INPUT_EXTENSIONS:
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        mime_type = EXT_TO_MIME[path.suffix.lower()]
        encoded_size = 4 * ((size + 2) // 3)
        for config in configs:
            empty_body = {
                "inputs": [
                    {"data": "", "mime_type": mime_type, "filename": path.name}
                ],
                "config": config,
            }
            empty_size = len(
                json.dumps(empty_body, ensure_ascii=False).encode("utf-8")
            )
            limit = max(limit, empty_size + encoded_size)
    return limit


def build_env(
    port: int,
    max_request_body_bytes: int = XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES,
) -> dict[str, str]:
    env = os.environ.copy()
    env["HF_HUB_CACHE"] = str(runtime_paths.hf_cache_dir())
    env["XBERG_CACHE_DIR"] = str(runtime_paths.xberg_cache_dir())
    env["HF_HUB_OFFLINE"] = "1"
    env["HUGGINGFACE_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_DATASETS_OFFLINE"] = "1"
    env["XBERG_API_ALLOW_LOCAL_URI_INPUTS"] = "1"
    env["XBERG_ORT_EP"] = "cpu"
    env["ORT_DYLIB_PATH"] = str(runtime_paths.runtime_dir() / "onnxruntime.dll")
    env["XBERG_MAX_CONCURRENT_REQUESTS"] = "0"
    if max_request_body_bytes > XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES:
        try:
            max_request_body_bytes = max(
                max_request_body_bytes,
                int(env.get("XBERG_MAX_REQUEST_BODY_BYTES", "0")),
            )
        except ValueError:
            pass
        env["XBERG_MAX_REQUEST_BODY_BYTES"] = str(max_request_body_bytes)
    env["XBERG_CORS_ORIGINS"] = f"http://127.0.0.1:{port}"
    return env


def start_server(
    port: int,
    config_path: Path = CONFIG_PATH,
    *,
    max_request_body_bytes: int = XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES,
) -> tuple[subprocess.Popen[Any], Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"xberg-server-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
    cmd = [
        str(runtime_paths.runtime_dir() / "xberg.exe"),
        "serve",
        "-c",
        str(config_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warn",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=build_env(port, max_request_body_bytes),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc._log_handle = log_handle  # type: ignore[attr-defined]
    return proc, log_path


def wait_healthy(port: int, timeout: float = 180.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "healthy":
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(1.0)
    print(f"Xberg API 未在 {timeout:.0f}s 内就绪: {last_error}", file=sys.stderr)
    raise SystemExit(EXIT_SERVER)


def http_post_json(port: int, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    import urllib.request

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/extract",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_json(port: int, path: str) -> Any:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Embedded children merge
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join(text.split())


def _content_digest(text: str) -> str:
    """Short stable digest of normalized content for duplicate detection.

    Storing digests instead of full normalized strings keeps the seen-set
    tiny for documents with hundreds of embedded children.
    """
    norm = _normalize(text)
    if not norm:
        return ""
    return hashlib.blake2b(norm.encode("utf-8"), digest_size=16).hexdigest()


def _is_ooxml_internal_part(path: str) -> bool:
    p = path.replace("\\", "/")
    parts = [x for x in p.split("/") if x]
    if not parts:
        return True
    if "[Content_Types].xml" in parts:
        return True

    # Xberg prefixes recursively extracted package parts with the embedded
    # document path, for example:
    # ppt/embeddings/Drawing.vsdx/visio/pages/page1.xml
    # Classify from the innermost OPC root; classifying only parts[0] mistakes
    # every VSDX XML part for a user document because the outer path contains
    # ``embeddings``.
    roots = [index for index, part in enumerate(parts) if part in OOXML_INTERNAL_ROOTS]
    if not roots:
        return False
    package_parts = parts[roots[-1] :]
    root = package_parts[0]
    if "embeddings" in package_parts or "attachments" in package_parts:
        return False
    if root == "word" and len(package_parts) >= 2:
        if package_parts[1] in OOXML_SKIP_SUBDIRS:
            return True
        if package_parts[1] in (
            "document.xml", "styles.xml", "settings.xml", "webSettings.xml",
            "fontTable.xml", "numbering.xml", "footnotes.xml", "endnotes.xml",
            "comments.xml", "header1.xml", "header2.xml", "header3.xml",
            "footer1.xml", "footer2.xml", "footer3.xml",
        ):
            return True
    if root in ("ppt", "xl", "visio") and len(package_parts) >= 2:
        if package_parts[1] in OOXML_SKIP_SUBDIRS:
            return True
    ext = package_parts[-1].rsplit(".", 1)[-1].lower() if "." in package_parts[-1] else ""
    return ext in OOXML_SKIP_EXTENSIONS


def _collect_children(
    children: Any,
    out: list[tuple[str, str]],
    seen: set[str],
    prefix: str,
    depth: int,
) -> list[tuple[str, str]]:
    """Flatten Xberg children into (display_path, content) document sections."""
    if depth > 5 or not isinstance(children, list):
        return out
    for child in children:
        if not isinstance(child, dict):
            continue
        rel_path = str(child.get("path", ""))
        result = child.get("result")
        if not isinstance(result, dict):
            continue
        display = f"{prefix}/{rel_path}" if prefix else rel_path
        content = _render_document_root(
            result,
            image_input=Path(rel_path).suffix.lower() in IMAGE_EXTENSIONS,
        )
        if _is_ooxml_internal_part(display):
            _collect_children(result.get("children"), out, seen, display, depth + 1)
            continue
        digest = _content_digest(content)
        if digest and digest not in seen:
            seen.add(digest)
            out.append((display, content))
        _collect_children(result.get("children"), out, seen, display, depth + 1)
    return out


# Standalone image references (whole line is one or more ![alt](src) tokens;
# Xberg's escape_markdown may escape the leading bang as \!).
_IMAGE_PLACEHOLDER_LINE = re.compile(r"^\s*(?:\\?!\[[^\]]*\]\([^)]*\)\s*)+$")
# Code fence delimiter (up to 3 leading spaces per CommonMark).
_FENCE_DELIMITER = re.compile(r"^\s{0,3}(```|~~~)")
_BACKTICK_RUN = re.compile(r"`+")
# Numeric character references emitted by Xberg's comrak serializer for
# non-standard whitespace (&#9; tab, &#32; space, &#160; nbsp, ...).
_NUMERIC_ENTITY = re.compile(r"&#(?:[xX]([0-9A-Fa-f]+)|(\d+));")
# Obsidian-style highlight markers Xberg emits for Word text highlights.
_HIGHLIGHT_MARKERS = re.compile(r"==([^=\n]+)==")
# A paragraph that is exactly one ASCII letter: a Word dropcap rendered as
# its own paragraph (w:framePr w:dropCap is not interpreted by Xberg).
_DROPCAP_PARAGRAPH = re.compile(r"^[A-Za-z]$")


def strip_image_placeholders(text: str) -> str:
    """Drop standalone image-reference lines, honoring code fences.

    Xberg renders an ``![name](image_N.ext)`` placeholder (possibly
    bang-escaped as ``\\!`` by escape_markdown) for images it can neither
    parse nor OCR (empty/undecodable data, EMF, ...). Project rule forbids
    image references in output, so placeholder-only lines are removed;
    inline references inside real text and anything inside code fences stay.
    """
    out_lines: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        delimiter = _FENCE_DELIMITER.match(line)
        if delimiter:
            marker = delimiter.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            out_lines.append(line)
            continue
        if fence is None and _IMAGE_PLACEHOLDER_LINE.match(line):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _decode_numeric_entity(match: re.Match[str]) -> str:
    hex_part, dec_part = match.group(1), match.group(2)
    try:
        code = int(hex_part, 16) if hex_part is not None else int(dec_part)
    except ValueError:
        return match.group(0)
    if code == 160:          # nbsp -> plain space
        return " "
    if code == 9:            # tab
        return "\t"
    if code < 32 or code == 127:  # other C0/C1 controls -> space
        return " "
    if code <= 0x10FFFF:
        return chr(code)
    return match.group(0)


def _normalize_plain_inline(text: str) -> str:
    text = text.replace("&nbsp;", " ")
    text = _NUMERIC_ENTITY.sub(_decode_numeric_entity, text)
    return _HIGHLIGHT_MARKERS.sub(r"\1", text)


def _normalize_inline(line: str) -> str:
    """Normalize prose while preserving same-line Markdown code spans."""
    output: list[str] = []
    cursor = 0
    search_from = 0
    while opener := _BACKTICK_RUN.search(line, search_from):
        delimiter_length = len(opener.group(0))
        closer = _BACKTICK_RUN.search(line, opener.end())
        while closer is not None and len(closer.group(0)) != delimiter_length:
            closer = _BACKTICK_RUN.search(line, closer.end())
        if closer is None:
            break
        output.append(_normalize_plain_inline(line[cursor : opener.start()]))
        output.append(line[opener.start() : closer.end()])
        cursor = closer.end()
        search_from = cursor
    output.append(_normalize_plain_inline(line[cursor:]))
    return "".join(output)


def normalize_markdown(text: str) -> str:
    """Normalize Xberg rendering artifacts without touching code fences.

    * numeric HTML entities (&#9;, &#32;, &#160;, ...) -> their characters,
      extending the &#10;/&#2; normalization Xberg itself performs;
    * ``==highlight==`` markers -> plain text (highlight info has no
      standard Markdown representation);
    * Word dropcap paragraphs (a lone ASCII letter) re-joined with the
      following lowercase-starting paragraph ("D" + "rop caps..." ->
      "Drop caps...").
    """
    lines = text.split("\n")
    fence: str | None = None
    for idx, line in enumerate(lines):
        delimiter = _FENCE_DELIMITER.match(line)
        if delimiter:
            marker = delimiter.group(1)
            fence = None if fence == marker else marker
            continue
        if fence is None:
            lines[idx] = _normalize_inline(line)

    out: list[str] = []
    fence = None
    i, n = 0, len(lines)

    def paragraph_end(start: int) -> int:
        j = start
        while j < n and lines[j].strip() and not _FENCE_DELIMITER.match(lines[j]):
            j += 1
        return j

    while i < n:
        line = lines[i]
        delimiter = _FENCE_DELIMITER.match(line)
        if delimiter:
            marker = delimiter.group(1)
            fence = None if fence == marker else marker
            out.append(line)
            i += 1
            continue
        if fence is not None or not line.strip():
            out.append(line)
            i += 1
            continue
        end = paragraph_end(i)
        paragraph = lines[i:end]
        k = end
        while k < n and not lines[k].strip():
            k += 1
        merged = False
        if (
            k < n
            and not _FENCE_DELIMITER.match(lines[k])
            and len(paragraph) == 1
            and _DROPCAP_PARAGRAPH.match(paragraph[0])
            and lines[k][:1].isascii()
            and lines[k][:1].islower()
        ):
            next_end = paragraph_end(k)
            out.append(paragraph[0] + lines[k])
            out.extend(lines[k + 1 : next_end])
            i = next_end
            merged = True
        if not merged:
            out.extend(paragraph)
            i = end
    return "\n".join(out)


def _wrap_ocr_literal(text: str) -> str:
    """Keep OCR punctuation from becoming Markdown headings or separators."""
    body = text.rstrip("\n")
    if not body:
        return ""
    longest_backtick_run = max(
        (len(run) for run in re.findall(r"`+", body)),
        default=0,
    )
    fence = "`" * max(3, longest_backtick_run + 1)
    return f"---\n{fence}text\n{body}\n{fence}\n---"


def collapse_whole_document_duplicate(text: str) -> str:
    """Drop the repeated second half of a whole-image OCR document.

    Some Xberg image extraction paths can emit OCR text both as paragraphs and
    as the trailing Image element's OCR text. When the blank-line-separated
    block sequence is exactly duplicated, keep only the first half.
    """
    stripped = text.strip("\n")
    blocks = stripped.split("\n\n")
    count = len(blocks)
    if count >= 2 and count % 2 == 0 and blocks[: count // 2] == blocks[count // 2 :]:
        return "\n\n".join(blocks[: count // 2]) + "\n"
    return text


# OCR geometry is returned by Xberg as either a rectangle or a quadrilateral.
# Keep this adapter deliberately tolerant so output remains useful if a future
# Xberg patch adds a plain ``bbox``/``polygon`` shape to the REST response.
def _ocr_aabb(element: dict[str, Any]) -> tuple[float, float, float, float] | None:
    if "bbox" in element and isinstance(element["bbox"], (list, tuple)):
        raw_bbox = element["bbox"]
        if len(raw_bbox) == 4 and all(isinstance(value, (int, float)) for value in raw_bbox):
            x1, y1, x2, y2 = (float(value) for value in raw_bbox)
            return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)
    geometry = element.get("geometry") or element.get("bbox") or element.get("polygon")
    if isinstance(geometry, dict):
        kind = str(geometry.get("type", "")).lower()
        if (
            kind in {"rectangle", "rect", "bbox"}
            or {"left", "top"} <= set(geometry)
            or {"x", "y"} <= set(geometry)
        ):
            try:
                left = float(geometry.get("left", geometry.get("x", 0)))
                top = float(geometry.get("top", geometry.get("y", 0)))
                width = float(geometry.get("width", geometry.get("w", 0)))
                height = float(geometry.get("height", geometry.get("h", 0)))
                return left, top, max(0.0, width), max(0.0, height)
            except (TypeError, ValueError):
                return None
        points = geometry.get("points")
        if points is not None:
            geometry = points
    if isinstance(geometry, (list, tuple)) and geometry:
        if len(geometry) == 4 and all(isinstance(value, (int, float)) for value in geometry):
            left, top, width, height = (float(value) for value in geometry)
            return left, top, max(0.0, width), max(0.0, height)
        if len(geometry) >= 8 and all(isinstance(value, (int, float)) for value in geometry):
            geometry = list(zip(geometry[::2], geometry[1::2]))
        points: list[tuple[float, float]] = []
        for point in geometry:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    points.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError):
                    pass
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            left, top = min(xs), min(ys)
            return left, top, max(xs) - left, max(ys) - top
    return None


def _ocr_line_text(elements: list[dict[str, Any]]) -> str:
    """Rebuild OCR text from text + bbox without character-count wrapping.

    Lines are clustered by vertical center, then sorted by x. Inter-box gaps
    become literal spaces based on the median OCR character width. Vertical
    gaps become blank lines. No whitespace collapsing or ``textwrap`` is used.
    """
    items: list[tuple[str, float, float, float, float]] = []
    for element in elements:
        text = element.get("text")
        box = _ocr_aabb(element)
        if not isinstance(text, str) or not text or box is None:
            continue
        left, top, width, height = box
        if width <= 0 or height <= 0:
            continue
        items.append((text, left, top, width, height))
    if not items:
        return ""

    heights = [item[4] for item in items]
    char_widths = [item[3] / max(1, len(item[0])) for item in items]
    median_height = max(1.0, statistics.median(heights))
    unit = max(1.0, statistics.median(char_widths))
    # A line's center may move by roughly half a glyph height due to OCR box
    # jitter. Use a bounded tolerance so adjacent rows do not merge.
    y_tolerance = max(2.0, median_height * 0.55)
    rows: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: (value[2], value[1])):
        center = item[2] + item[4] / 2.0
        row = next((candidate for candidate in reversed(rows)
                    if abs(center - candidate["center"]) <= y_tolerance), None)
        if row is None:
            rows.append({"center": center, "items": [item], "top": item[2], "bottom": item[2] + item[4]})
        else:
            row["items"].append(item)
            row["center"] = statistics.mean([x[2] + x[4] / 2.0 for x in row["items"]])
            row["top"] = min(row["top"], item[2])
            row["bottom"] = max(row["bottom"], item[2] + item[4])
    rows.sort(key=lambda row: row["top"])

    output: list[str] = []
    previous_bottom: float | None = None
    for row in rows:
        if previous_bottom is not None:
            vertical_gap = row["top"] - previous_bottom
            # Preserve visibly separated rows as an empty line, but do not
            # manufacture dozens of lines from large page-coordinate gaps.
            if vertical_gap > median_height * 1.8:
                output.append("")
        row_items = sorted(row["items"], key=lambda value: value[1])
        line: list[str] = []
        cursor = row_items[0][1]
        indent = max(0, int(round((cursor - min(item[1] for item in items)) / unit)))
        if indent:
            line.append(" " * indent)
        line.append(row_items[0][0])
        cursor = row_items[0][1] + row_items[0][3]
        for text, left, _top, width, _height in row_items[1:]:
            gap = left - cursor
            spaces = max(0, int(round(gap / unit)))
            if gap > unit * 0.35 and spaces == 0:
                spaces = 1
            if spaces:
                line.append(" " * spaces)
            line.append(text)
            cursor = left + width
        output.append("".join(line))
        previous_bottom = row["bottom"]
    return "\n".join(output)




def spatial_ocr_markdown(elements: Any) -> str:
    """Render Xberg OCR elements as one or more spatially faithful blocks."""
    if not isinstance(elements, list):
        return ""
    by_page: dict[int, list[dict[str, Any]]] = {}
    for element in elements:
        if isinstance(element, dict):
            try:
                page = int(element.get("page_number", 1))
            except (TypeError, ValueError):
                page = 1
            by_page.setdefault(page, []).append(element)
    blocks: list[str] = []
    for page in sorted(by_page):
        page_elements = by_page[page]
        # Xberg may expose a line plus its word children in the same response.
        # Rendering both duplicates every word (e.g. ``Hello WorldHello World``).
        # Prefer the highest available spatial level; fall back to words only
        # when the backend did not emit line/block elements.
        level_groups = {
            "line": [item for item in page_elements if item.get("level") == "line"],
            "block": [item for item in page_elements if item.get("level") == "block"],
            "word": [item for item in page_elements if item.get("level") == "word"],
        }
        selected = next(
            (group for name in ("line", "block", "word") if (group := level_groups[name])),
            page_elements,
        )
        text = _ocr_line_text(selected)
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def _document_ocr_elements(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect top-level and page-local OCR metadata from REST responses."""
    result = [item for item in (doc.get("ocr_elements") or []) if isinstance(item, dict)]
    for page_number, page in enumerate(doc.get("pages") or [], 1):
        if isinstance(page, dict):
            for item in page.get("ocr_elements") or []:
                if isinstance(item, dict):
                    if "page_number" not in item:
                        item = dict(item)
                        item["page_number"] = page_number
                    result.append(item)
    return result


def _render_document_root(
    doc: dict[str, Any], *, image_input: bool = False
) -> str:
    root = str(doc.get("content") or "")
    spatial = spatial_ocr_markdown(_document_ocr_elements(doc))
    # Image/scanned OCR is the only source for these elements. Prefer the
    # geometry-preserving rendering when present; native document text keeps
    # Xberg's normal Markdown semantics and is left untouched.
    extraction_method = str(doc.get("extraction_method") or "").lower()
    ocr_only = "ocr" in extraction_method and "native" not in extraction_method
    use_spatial = bool(spatial) and (image_input or not root.strip() or ocr_only)
    if use_spatial:
        root = spatial
    if image_input and not use_spatial:
        root = collapse_whole_document_duplicate(root)
    if image_input or ocr_only or use_spatial:
        root = _wrap_ocr_literal(root)
    return strip_image_placeholders(normalize_markdown(root))


def build_final_markdown(doc: dict[str, Any], *, image_input: bool = False) -> str:
    parts: list[str] = [_render_document_root(doc, image_input=image_input)]
    seen: set[str] = set()
    root_digest = _content_digest(parts[0])
    if root_digest:
        seen.add(root_digest)
    children = _collect_children(doc.get("children"), [], seen, "", 0)
    for display, content in children:
        parts.append("")
        parts.append(f"## Embedded document: {display}")
        parts.append("")
        parts.append(content.rstrip())
    # Only trim record separators added by the assembler. Do not call
    # ``strip()`` here: a leading space can be an intentional OCR x-position.
    text = "\n".join(parts).rstrip("\n") + "\n"
    return strip_image_placeholders(text)


# ---------------------------------------------------------------------------
# Large-document routing (quick page count -> normal / fast config)
# ---------------------------------------------------------------------------

def _pdf_page_count(path: Path) -> int | None:
    """PDF page count via PyMuPDF metadata (no rendering, no OCR)."""
    if pymupdf is None:
        return None
    with pymupdf.open(str(path)) as doc:
        return doc.page_count


def _docx_page_count(path: Path) -> int | None:
    """DOCX page count from docProps/app.xml ``<Pages>`` (metadata only)."""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.lower() == "docprops/app.xml":
                root = ET.fromstring(zf.read(name))
                for el in root.iter():
                    if el.tag.rsplit("}", 1)[-1] == "Pages":
                        text = (el.text or "").strip()
                        return int(text) if text.isdigit() else None
    return None


def _pptx_slide_count(path: Path) -> int | None:
    """PPTX slide count by counting ppt/slides/slideN.xml package parts."""
    with zipfile.ZipFile(path) as zf:
        count = sum(1 for name in zf.namelist() if _PPTX_SLIDE_RE.match(name.lower()))
    return count or None


def page_count(path: Path) -> int | None:
    """Quick page count from file structure/metadata; None when unavailable.

    Reads no body content, renders nothing and runs no OCR/models. PDF uses
    PyMuPDF; DOCX uses docProps/app.xml <Pages>; PPTX counts slide parts;
    everything else (including XLSX) is None. Any failure returns None so the
    caller falls back to the normal extraction config (never skip/fail).
    """
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _pdf_page_count(path)
        if ext in WORD_FAMILY:
            return _docx_page_count(path)
        if ext in PPTX_FAMILY:
            return _pptx_slide_count(path)
    except Exception as exc:  # noqa: BLE001
        _log().warning("页数判定失败 %s: %s（按 normal 模式处理）", path.name, exc)
    return None


def split_pipeline_config(
    config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the single on-disk config into (xberg_config, large_document policy).

    Xberg's ExtractionConfig denies unknown fields, so the ``large_document``
    policy and media backend selector are pipeline-level directives consumed
    here only; they must never reach the server ``--config`` file or the per-
    request /extract body.
    """
    policy = dict(config.get(LARGE_DOC_KEY) or {})
    xberg = {
        k: v
        for k, v in config.items()
        if k not in {LARGE_DOC_KEY, MEDIA_BACKEND_KEY}
    }
    return xberg, policy


def media_transcription_backend(config: dict[str, Any]) -> str:
    """Return the configured MP4/M4A backend, defaulting to the local path."""
    raw = config.get(MEDIA_BACKEND_KEY, "local")
    backend = str(raw).strip().lower()
    if backend not in MEDIA_BACKENDS:
        allowed = ", ".join(sorted(MEDIA_BACKENDS))
        raise ValueError(f"{MEDIA_BACKEND_KEY} 必须是: {allowed}")
    return backend


def fast_mode_config(base: dict[str, Any]) -> dict[str, Any]:
    """Fast-mode config for large documents (page count > threshold).

    Derived from the normal config; the expensive visual steps are turned off.
    The keys are part of the Xberg CLI configuration used by the latest release:

    * ``layout: null`` disables RT-DETR + TATR entirely -- the only legal way
      to switch them off (LayoutStrategy only has always/auto; every pipeline
      gates on ``layout.is_some()``, cf. extract_impl.rs:355-364).
    * ``pdf_options.extract_images`` / ``images.extract_images`` /
      ``images.run_ocr_on_images`` stop image extraction and image OCR.

    Fast mode intentionally disables layout and image OCR. PDFs that depend on
    document-level OCR fallback can therefore be empty because
    ``run_ocr_on_images`` also gates that fallback. Native-text pages are
    unaffected, and the trade-off keeps the "图片提取/OCR OFF" requirement at
    the cost of that edge case.

    Native text, native tables (``pdf_options.extract_tables``), heading
    hierarchy and ``ocr_strategy: auto`` keep their base values.
    """
    cfg = copy.deepcopy(base)
    cfg["layout"] = None
    cfg["use_layout_for_markdown"] = False
    pdf_options = dict(cfg.get("pdf_options") or {})
    pdf_options["extract_images"] = False
    cfg["pdf_options"] = pdf_options
    images = dict(cfg.get("images") or {})
    images["extract_images"] = False
    images["run_ocr_on_images"] = False
    cfg["images"] = images
    return cfg


def resolve_mode(path: Path, policy: dict[str, Any]) -> tuple[str, int | None]:
    """Return ("fast"|"normal", page_count) for a document input.

    Normal when the feature is disabled, the count is unavailable, the format
    has no count (XLSX etc.), or count <= threshold; fast only when a count
    exists and is strictly above the threshold.
    """
    if not policy.get("enabled", False):
        return "normal", None
    try:
        threshold = int(policy.get("page_threshold", 200))
    except (TypeError, ValueError):
        threshold = 200
    count = page_count(path)
    if count is None or count <= threshold:
        return "normal", count
    return "fast", count


def _write_sanitized_config(xberg_config: dict[str, Any]) -> Path:
    """Persist the pipeline config (policy stripped) for ``xberg serve``.

    The on-disk JSON must stay the single source of truth, but Xberg's strict
    parser rejects the ``large_document`` key at server startup, so the server
    is launched with this sanitized copy inside the repo temp directory.
    """
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / f"xberg-server-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(xberg_config, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Conversion driver
# ---------------------------------------------------------------------------

def scan_inputs(
    input_dir: Path, output_dir: Path, supported: set[str]
) -> tuple[list[Path], int]:
    """Collect files to convert; return (files, skipped_count)."""
    files: list[Path] = []
    skipped = 0
    try:
        output_subtree = output_dir.resolve().relative_to(input_dir.resolve())
    except ValueError:
        output_subtree = None
    if output_subtree == Path("."):
        output_subtree = None
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(input_dir)
        if output_subtree is not None and (
            rel == output_subtree or output_subtree in rel.parents
        ):
            continue
        if path.suffix.lower() not in supported:
            continue
        out_rel = (
            convert_mp4.media_markdown_relative_path(rel)
            if path.suffix.lower() in set(convert_mp4.DEFAULT_EXTENSIONS)
            else convert_mp4.markdown_relative_path(rel)
        )
        out_md = output_dir / out_rel
        if out_md.exists():
            _log().info("[跳过] %s (已有 %s)", rel, out_md.relative_to(output_dir))
            skipped += 1
            continue
        files.append(path)
    return files, skipped


def mime_hint(path: Path) -> str | None:
    """Return an explicit MIME hint for core formats, or None to auto-detect.

    Only hints when the file's magic bytes agree with the extension, so a
    misnamed file still falls back to Xberg content detection.
    """
    mime = EXT_TO_MIME.get(path.suffix.lower())
    if mime is None:
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return None
    expected = b"%PDF" if mime == "application/pdf" else b"PK\x03\x04"
    return mime if head == expected else None


def extract_single(
    port: int, path: Path, config: dict[str, Any], timeout: int
) -> dict[str, Any]:
    """POST a one-file /extract request and return its result document.

    One request per file gives true per-file progress, error isolation and a
    correct per-file timeout; Xberg still parallelizes internally. PPTX-family
    files travel as base64 bytes so embedded objects are not dropped
    (see BYTES_INPUT_EXTENSIONS).
    """
    hint = mime_hint(path)
    if hint is not None and path.suffix.lower() in BYTES_INPUT_EXTENSIONS:
        file_input: dict[str, Any] = {
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "mime_type": hint,
            "filename": path.name,
        }
    else:
        file_input = {"uri": str(path)}
        if hint is not None:
            file_input["mime_type"] = hint
    body = {"inputs": [file_input], "config": config}
    response = http_post_json(port, body, timeout)
    for error in response.get("errors") or []:
        raise RuntimeError(f"{error.get('error_type')}: {error.get('message')}")
    results = response.get("results") or []
    if not results:
        raise RuntimeError("Xberg 未返回结果")
    return results[0]


def convert_one_result(
    result: dict[str, Any],
    input_path: Path,
    input_dir: Path,
    output_dir: Path,
) -> bool:
    rel = input_path.relative_to(input_dir)
    output_rel = (
        convert_mp4.media_markdown_relative_path(rel)
        if input_path.suffix.lower() in set(convert_mp4.DEFAULT_EXTENSIONS)
        else convert_mp4.markdown_relative_path(rel)
    )
    output_path = output_dir / output_rel
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        markdown = build_final_markdown(
            result, image_input=input_path.suffix.lower() in IMAGE_EXTENSIONS
        )
        tmp_path.write_text(markdown, encoding="utf-8", newline="\n")
        os.replace(tmp_path, output_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="all2markdown",
        description="Windows 11 x64 / 纯 CPU / 完全离线：用仓库内置 Xberg 将原始文档目录转换为 Markdown",
    )
    parser.add_argument(
        "input_dir", nargs="?", default=None, help="原始文档目录（只读；默认仓库内 input）"
    )
    parser.add_argument(
        "output_dir", nargs="?", default=None, help="Markdown 输出目录（默认仓库内 output）"
    )
    parser.add_argument(
        "--xberg-config",
        type=Path,
        default=CONFIG_PATH,
        help="Xberg 配置文件路径（默认 config/xberg_offline.json）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help=(
            "单文件超时秒数，同时作用于客户端 HTTP 与服务端 extraction_timeout_secs"
            f"（默认 {REQUEST_TIMEOUT}；传入不同值会使提取缓存键重建一次）"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
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
        verify_assets()
        log.info("预检通过（平台 / AVX2 / 离线资产），耗时 %.1fs", time.monotonic() - preflight_t0)

        input_dir = (
            Path(args.input_dir) if args.input_dir else DEFAULT_INPUT_DIR
        ).resolve()
        output_dir = (
            Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
        ).resolve()
        if args.input_dir is None:
            log.info("未指定输入目录，使用默认: %s", input_dir)
        if args.output_dir is None:
            log.info("未指定输出目录，使用默认: %s", output_dir)
        if not input_dir.is_dir():
            print(f"输入目录不存在: {input_dir}", file=sys.stderr)
            return EXIT_USAGE

        config_path = Path(args.xberg_config).resolve()
        if not config_path.is_file():
            print(f"Xberg 配置文件不存在: {config_path}", file=sys.stderr)
            return EXIT_USAGE
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Xberg 配置文件读取失败: {config_path}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        # The on-disk JSON is the single source of truth for Xberg settings and
        # pipeline-level routing policies. Xberg's ExtractionConfig denies
        # unknown fields, so those policy keys are split off here and never
        # reach the server config or the /extract bodies.
        xberg_config, large_policy = split_pipeline_config(config)
        try:
            media_backend = media_transcription_backend(config)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        log.info("媒体转录后端: %s", media_backend)
        ocr_config = dict((xberg_config.get("ocr") or {}).get("paddle_ocr_config") or {})
        ocr_tier = str(ocr_config.get("model_tier", "tiny")).lower()
        if ocr_tier not in SUPPORTED_OCR_MODEL_TIERS:
            print(
                "ocr.paddle_ocr_config.model_tier 必须是: "
                + ", ".join(sorted(SUPPORTED_OCR_MODEL_TIERS)),
                file=sys.stderr,
            )
            return EXIT_USAGE
        log.info("OCR 模型: PP-OCRv6 %s", ocr_tier)
        if large_policy.get("enabled", False) and PYMUPDF_IMPORT_ERROR is not None:
            log.warning(
                "PyMuPDF 不可用（%s）：PDF 页数判定关闭，PDF 全部按 normal 模式处理",
                PYMUPDF_IMPORT_ERROR,
            )
        if args.timeout <= 0:
            print(f"--timeout 必须为正数: {args.timeout}", file=sys.stderr)
            return EXIT_USAGE
        timeout = args.timeout
        # Keep the server-side per-extraction timeout aligned with the client
        # HTTP timeout so the server returns a clean Timeout error first instead
        # of the client dropping the socket while the server keeps burning CPU.
        # extraction_timeout_secs is part of Xberg's extraction cache key, so a
        # non-default --timeout rebuilds the cache once (per content hash).
        config_timeout = xberg_config.get("extraction_timeout_secs")
        if config_timeout != timeout:
            log.info(
                "覆盖服务端 extraction_timeout_secs: %s -> %d（缓存键重建一次）",
                config_timeout,
                timeout,
            )
            xberg_config = {**xberg_config, "extraction_timeout_secs": timeout}
        fast_config = (
            fast_mode_config(xberg_config)
            if large_policy.get("enabled", False)
            else xberg_config
        )

        output_dir.mkdir(parents=True, exist_ok=True)

        # Media are pre-scanned so a local-media-only input tree never starts
        # the Xberg server. In ``xberg`` mode the same files are deliberately
        # left for the Xberg phase below.
        media_extensions = set(convert_mp4.DEFAULT_EXTENSIONS)
        media_files, media_skipped = (
            scan_inputs(input_dir, output_dir, media_extensions)
            if media_backend == "local"
            else ([], 0)
        )
        has_doc_candidate = any(
            p.is_file()
            and (media_backend == "xberg" or p.suffix.lower() not in media_extensions)
            for p in input_dir.rglob("*")
        )

        total = 0
        tag_counter = {"value": 0}
        progress = {
            "done": 0,
            "ok": 0,
            "fail": 0,
            "warn": 0,
            "t0": time.monotonic(),
            "lock": threading.Lock(),
        }

        def next_tag() -> str:
            tag_counter["value"] += 1
            return f"{tag_counter['value']}/{total}"

        def report_progress() -> None:
            log.info(
                "进度 %d/%d（成功 %d，失败 %d，总耗时 %.0fs）",
                progress["done"],
                total,
                progress["ok"],
                progress["fail"],
                time.monotonic() - progress["t0"],
            )

        proc: subprocess.Popen[Any] | None = None
        server_log: Path | None = None
        stop_tail = threading.Event()
        try:
            doc_files: list[Path] = []
            doc_skipped = 0
            if has_doc_candidate:
                request_body_limit = _xberg_request_body_limit(
                    input_dir, (xberg_config, fast_config)
                )
                if request_body_limit > XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES:
                    log.info(
                        "Xberg 请求正文上限提升至 %.1f MB（适配 Base64 演示文稿）",
                        request_body_limit / 1048576,
                    )
                port = pick_free_port()
                server_t0 = time.monotonic()
                sanitized_config = _write_sanitized_config(xberg_config)
                log.info("Xberg server 使用剥离后的配置文件: %s", sanitized_config)
                proc, server_log = start_server(
                    port,
                    sanitized_config,
                    max_request_body_bytes=request_body_limit,
                )
                wait_healthy(port)
                log.info(
                    "Xberg server 就绪，启动耗时 %.1fs（日志: %s）",
                    time.monotonic() - server_t0,
                    server_log,
                )
                threading.Thread(
                    target=tail_server_log,
                    args=(server_log, stop_tail),
                    name="xberg-log-tail",
                    daemon=True,
                ).start()
                formats = http_get_json(port, "/formats")
                # The runtime MIME registry lists audio/video extensions (incl.
                # .m4a as audio/mp4). Keep them only when explicitly selected;
                # the default local backend must never send media to Xberg.
                supported = {
                    ("." + str(f.get("extension", "")).lower())
                    for f in formats
                    if f.get("extension")
                    and (
                        media_backend == "xberg"
                        or not str(f.get("mime_type", "")).startswith(("audio/", "video/"))
                    )
                }
                supported.update({".pdf", ".docx", ".pptx", ".xlsx"})
                if media_backend == "xberg":
                    supported.update(media_extensions)
                doc_files, doc_skipped = scan_inputs(input_dir, output_dir, supported)

            doc_files.sort(key=lambda p: p.stat().st_size, reverse=True)
            # Large-document routing: quick page count decides the per-file
            # config (normal vs fast) before any extraction starts. Page-count
            # failures degrade to normal; routing never skips or fails a file.
            modes: dict[Path, tuple[str, int | None]] = {}
            for p in doc_files:
                mode, count = resolve_mode(p, large_policy)
                modes[p] = (mode, count)
                log.info(
                    "[分流] %s: %s 模式（页数 %s）",
                    p.relative_to(input_dir),
                    mode,
                    count if count is not None else "未知",
                )
            total = len(media_files) + len(doc_files)
            if total == 0:
                log.info("没有需要转换的文件（全部已有对应 Markdown，或目录为空）")
                return EXIT_OK
            skipped = media_skipped + doc_skipped
            xberg_media_count = (
                sum(p.suffix.lower() in media_extensions for p in doc_files)
                if media_backend == "xberg"
                else 0
            )
            log.info(
                "共 %d 个待转换文件（文档 %d，媒体 %d，另跳过 %d 个），"
                "文档客户端并发 %d，单文件超时 %ds",
                total,
                len(doc_files) - xberg_media_count,
                len(media_files) + xberg_media_count,
                skipped,
                CONCURRENT_REQUESTS,
                timeout,
            )

            # Phase 1: 媒体 -> 本地 ASR 转录（顺序执行，模型整批复用）。
            if media_files:
                media_error: str | None = None
                recognizer = None
                if convert_mp4._IMPORT_ERROR is not None:
                    media_error = (
                        f"缺少媒体转录依赖: {convert_mp4._IMPORT_ERROR}；"
                        "请重新运行 init.cmd 以校验并修复安装"
                    )
                else:
                    model_issues = convert_mp4.asset_issues()
                    if model_issues:
                        media_error = (
                            "初始化媒体模型不完整或大小不符: "
                            + "; ".join(model_issues)
                            + "；请重新运行 init.cmd 以校验并修复安装"
                        )
                    else:
                        model_t0 = time.monotonic()
                        recognizer = convert_mp4.create_recognizer(
                            language="zh",
                            use_itn=True,
                            num_threads=min(os.cpu_count() or 4, 12),
                        )
                        log.info(
                            "SenseVoice INT8 就绪（language=zh, use_itn=true），加载耗时 %.1fs",
                            time.monotonic() - model_t0,
                        )
                for path in media_files:
                    rel = path.relative_to(input_dir)
                    tag = next_tag()
                    log.info(
                        "[开始 %s] %s（%.1f MB）[媒体转录]",
                        tag,
                        rel,
                        path.stat().st_size / 1048576,
                    )
                    file_t0 = time.monotonic()
                    try:
                        if media_error is not None:
                            raise RuntimeError(media_error)
                        duration, lines, has_audio = convert_mp4.transcribe_file(
                            path, recognizer
                        )
                        markdown = convert_mp4.build_markdown(
                            str(rel.as_posix()), duration, lines, has_audio
                        )
                        convert_mp4.write_atomic(
                            output_dir / convert_mp4.media_markdown_relative_path(rel),
                            markdown,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.error("[失败 %s] %s: %s", tag, rel, exc)
                        with progress["lock"]:
                            progress["done"] += 1
                            progress["fail"] += 1
                        report_progress()
                        continue
                    with progress["lock"]:
                        progress["done"] += 1
                        progress["ok"] += 1
                    log.info(
                        "[完成 %s] %s 耗时 %.1fs（音频 %.1fs，%d 个语音片段）",
                        tag,
                        rel,
                        time.monotonic() - file_t0,
                        duration,
                        len(lines),
                    )
                    report_progress()

            # Phase 2: 文档 -> Xberg（客户端并发池，单文件单请求）。
            if doc_files:

                def worker(index: int, path: Path) -> None:
                    rel = path.relative_to(input_dir)
                    tag = f"{index}/{total}"
                    media_tag = (
                        " [媒体转录/Xberg]"
                        if media_backend == "xberg" and path.suffix.lower() in media_extensions
                        else ""
                    )
                    log.info(
                        "[开始 %s] %s（%.1f MB）%s",
                        tag,
                        rel,
                        path.stat().st_size / 1048576,
                        media_tag,
                    )
                    file_t0 = time.monotonic()
                    stop_beat = threading.Event()

                    def heartbeat() -> None:
                        last_cpu: float | None = None
                        last_t = time.monotonic()
                        if proc is not None:
                            last_cpu = _process_cpu_seconds(proc.pid)
                        while not stop_beat.wait(HEARTBEAT_INTERVAL):
                            now = time.monotonic()
                            cpu = (
                                _process_cpu_seconds(proc.pid) if proc is not None else None
                            )
                            util = 0.0
                            text = (
                                f"[进行中 {tag}] {rel} 已耗时 {now - file_t0:.0f}s"
                            )
                            if cpu is not None:
                                if last_cpu is not None:
                                    delta = max(0.0, cpu - last_cpu)
                                    util = min(
                                        999.0, delta / max(0.001, now - last_t) * 100.0
                                    )
                                    text += f"，CPU 活跃≈{util:.0f}%（单核百分为 100，多线程可超 100）"
                                else:
                                    text += "（CPU 计时不可用）"
                            else:
                                text += "（无法读取后台进程 CPU）"
                            if util < 5.0 and now - file_t0 > 120:
                                log.warning(text + " — CPU 几乎空闲，可能阻塞在 I/O")
                            else:
                                log.info(text)
                            last_t = now
                            last_cpu = cpu

                    beat = threading.Thread(target=heartbeat, name=f"hb-{index}", daemon=True)
                    beat.start()
                    try:
                        try:
                            mode, _ = modes.get(path, ("normal", None))
                            file_config = fast_config if mode == "fast" else xberg_config
                            result = extract_single(port, path, file_config, timeout)
                        except Exception as exc:  # noqa: BLE001
                            log.error("[失败 %s] %s: %s", tag, rel, exc)
                            with progress["lock"]:
                                progress["done"] += 1
                                progress["fail"] += 1
                            report_progress()
                            return
                        warnings = result.get("processing_warnings") or []
                        try:
                            convert_one_result(result, path, input_dir, output_dir)
                        except Exception as exc:  # noqa: BLE001
                            log.error("[失败 %s] %s 写盘失败: %s", tag, rel, exc)
                            with progress["lock"]:
                                progress["done"] += 1
                                progress["fail"] += 1
                            report_progress()
                            return
                        with progress["lock"]:
                            progress["done"] += 1
                            progress["ok"] += 1
                            progress["warn"] += len(warnings)
                        for w in warnings:
                            log.warning("[WARN] %s -> %s: %s", rel, w.get("source"), w.get("message"))
                        meta = result.get("metadata") or {}
                        meta_parts: list[str] = []
                        dur = meta.get("extraction_duration_ms")
                        if isinstance(dur, int) and dur > 0:
                            meta_parts.append(f"Xberg耗时 {dur / 1000.0:.1f}s")
                        if meta.get("ocr_used"):
                            meta_parts.append("使用OCR")
                        pages = meta.get("pages")
                        if isinstance(pages, dict):
                            n = pages.get("total_count")
                            if isinstance(n, int) and n > 0:
                                unit = {"page": "页", "slide": "幻灯片", "sheet": "工作表"}.get(
                                    str(pages.get("unit_type")), "页"
                                )
                                meta_parts.append(f"共{n}{unit}")
                        meta_suffix = f"；{'; '.join(meta_parts)}" if meta_parts else ""
                        log.info(
                            "[完成 %s] %s 耗时 %.1fs（%d 内嵌对象, %d warnings）%s",
                            tag,
                            rel,
                            time.monotonic() - file_t0,
                            len(result.get("children") or []),
                            len(warnings),
                            meta_suffix,
                        )
                        report_progress()
                    finally:
                        stop_beat.set()

                with ThreadPoolExecutor(
                    max_workers=CONCURRENT_REQUESTS, thread_name_prefix="convert"
                ) as pool:
                    futures = [
                        pool.submit(worker, index, p)
                        for index, p in enumerate(doc_files, len(media_files) + 1)
                    ]
                    for future in futures:
                        future.result()

            log.info(
                "完成: %d ok, %d skip, %d fail, %d warnings，总耗时 %.1fs",
                progress["ok"],
                skipped,
                progress["fail"],
                progress["warn"],
                time.monotonic() - progress["t0"],
            )
            return EXIT_OK if progress["fail"] == 0 else EXIT_PARTIAL
        finally:
            stop_tail.set()
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                if hasattr(proc, "_log_handle"):
                    proc._log_handle.close()
                log.info("Xberg server 已停止（日志: %s）", server_log)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_UNEXPECTED
        return int(exc.code)
    except Exception as exc:  # noqa: BLE001
        log.exception("未预期错误")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
