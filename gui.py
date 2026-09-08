#!/usr/bin/env python3
"""all2markdown 图形界面入口（固定使用项目 .venv Python 3.12）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_CORE_PATH = Path(__file__).resolve().parent / "src" / "gui.py"
_SPEC = importlib.util.spec_from_file_location("all2markdown_gui", _CORE_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - installation failure
    raise RuntimeError(f"无法加载图形界面实现: {_CORE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["all2markdown_gui"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
