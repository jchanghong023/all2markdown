#!/usr/bin/env python3
"""Offline Rust-based fast document-to-Markdown toolbox entry point."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_CORE_PATH = Path(__file__).resolve().parent / "src" / "all2markdown_core.py"
_SPEC = importlib.util.spec_from_file_location("all2markdown", _CORE_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - installation failure
    raise RuntimeError(f"无法加载 all2markdown 实现: {_CORE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[__name__] = _MODULE
_SPEC.loader.exec_module(_MODULE)

if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
