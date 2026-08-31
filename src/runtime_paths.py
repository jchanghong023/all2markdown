"""Shared installed-runtime and model path contract.

Resolvers read environment variables at call time so tests and enterprise
installations can relocate assets without import-order dependencies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_ASSETS_PATH = REPO_ROOT / "src" / "config" / "install_assets.json"


def _resolved_override(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def model_root() -> Path:
    """Return the root for downloaded models."""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return _resolved_override(
        "ALL2MARKDOWN_MODEL_DIR", home / ".models" / "all2markdown"
    )


def data_root() -> Path:
    """Return the root for runtime, caches, managed Python, and notices."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    default = (
        Path(local_app_data) / "all2markdown"
        if local_app_data
        else Path.home() / "AppData" / "Local" / "all2markdown"
    )
    return _resolved_override("ALL2MARKDOWN_DATA_DIR", default)


def runtime_dir() -> Path:
    return data_root() / "xberg" / "v1.0.14" / "runtime"


def xberg_cache_dir() -> Path:
    return data_root() / "xberg" / "v1.0.14" / "cache"


def hf_cache_dir() -> Path:
    return model_root() / "xberg" / "v1.0.14" / "hf"


def sherpa_root() -> Path:
    return model_root() / "sherpa_onnx" / "v1.13.6"


def load_install_manifest(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and minimally validate the authoritative install manifest."""
    manifest_path = path or INSTALL_ASSETS_PATH
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported install asset manifest schema")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise ValueError("install asset manifest has no asset list")
    seen = set()
    required = {
        "id",
        "group",
        "kind",
        "url",
        "mirror_path",
        "root",
        "relative_path",
        "sha256",
        "size_bytes",
    }
    for asset in assets:
        if not isinstance(asset, dict) or not required.issubset(asset):
            raise ValueError("install asset manifest contains an invalid entry")
        asset_id = asset["id"]
        if asset_id in seen:
            raise ValueError("duplicate install asset id: {}".format(asset_id))
        seen.add(asset_id)
        if asset["kind"] not in ("file", "zip_member"):
            raise ValueError("invalid asset kind for {}".format(asset_id))
        if asset["root"] not in ("model", "runtime"):
            raise ValueError("invalid destination root for {}".format(asset_id))
        if asset["kind"] == "zip_member" and not (
            asset.get("member") or asset.get("member_basename")
        ):
            raise ValueError("zip member selector missing for {}".format(asset_id))
    return manifest


def install_assets(
    group: Optional[str] = None, path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    assets = load_install_manifest(path)["assets"]
    if group is None:
        return assets
    return [asset for asset in assets if asset["group"] == group]


def install_assets_by_id(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    return {asset["id"]: asset for asset in install_assets(path=path)}


def _relative_parts(value: str) -> List[str]:
    relative = PureWindowsPath(value.replace("/", "\\"))
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("unsafe asset destination: {}".format(value))
    parts = [part for part in relative.parts if part not in ("", ".")]
    if not parts:
        raise ValueError("empty asset destination")
    return parts


def asset_path(asset: Mapping[str, Any]) -> Path:
    """Resolve a manifest entry to its final installed path."""
    root_name = asset["root"]
    if root_name == "runtime":
        root = runtime_dir()
    elif root_name == "model":
        root = model_root()
    else:
        raise ValueError("unknown asset destination root: {}".format(root_name))
    return root.joinpath(*_relative_parts(str(asset["relative_path"])))
