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
XBERG_CHANNEL = "latest"
XBERG_RELEASE_STATE_FILENAME = "release.json"


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


def xberg_root() -> Path:
    return data_root() / "xberg" / XBERG_CHANNEL


def runtime_dir() -> Path:
    return xberg_root() / "runtime"


def xberg_cache_dir() -> Path:
    return xberg_root() / "cache"


def hf_cache_dir() -> Path:
    return model_root() / "xberg" / XBERG_CHANNEL / "hf"


def xberg_release_state_path() -> Path:
    return xberg_root() / XBERG_RELEASE_STATE_FILENAME


def sherpa_root() -> Path:
    return model_root() / "sherpa_onnx" / "v1.13.6"


def load_install_manifest(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and minimally validate the authoritative install manifest."""
    manifest_path = path or INSTALL_ASSETS_PATH
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported install asset manifest schema")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise ValueError("install asset manifest has no asset list")
    seen = set()
    common_required = {"id", "group", "kind", "root", "relative_path"}
    static_required = {"url", "mirror_path", "sha256", "size_bytes"}
    release_required = {"api_url", "repository", "asset_name", "member_basename"}
    model_required = {"repository", "model_path"}
    valid_kinds = {
        "file",
        "zip_member",
        "github_release_zip_member",
        "xberg_manifest_model",
    }
    for asset in assets:
        if not isinstance(asset, dict) or not common_required.issubset(asset):
            raise ValueError("install asset manifest contains an invalid entry")
        asset_id = asset["id"]
        if asset_id in seen:
            raise ValueError("duplicate install asset id: {}".format(asset_id))
        seen.add(asset_id)
        kind = asset["kind"]
        if kind not in valid_kinds:
            raise ValueError("invalid asset kind for {}".format(asset_id))
        if asset["root"] not in ("model", "runtime"):
            raise ValueError("invalid destination root for {}".format(asset_id))
        if kind in ("file", "zip_member") and not static_required.issubset(asset):
            raise ValueError("static asset metadata missing for {}".format(asset_id))
        if kind == "zip_member" and not (
            asset.get("member") or asset.get("member_basename")
        ):
            raise ValueError("zip member selector missing for {}".format(asset_id))
        if kind == "github_release_zip_member" and not release_required.issubset(asset):
            raise ValueError("GitHub release metadata missing for {}".format(asset_id))
        if kind == "xberg_manifest_model" and not model_required.issubset(asset):
            raise ValueError("Xberg model selector missing for {}".format(asset_id))
    return manifest


def load_xberg_release_state() -> Optional[Dict[str, Any]]:
    """Load the installed release identity without network access."""
    try:
        with xberg_release_state_path().open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    required = {
        "schema_version",
        "repository",
        "tag_name",
        "asset_name",
        "browser_download_url",
        "archive_sha256",
        "archive_size_bytes",
        "member_sha256",
        "member_size_bytes",
    }
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or not required.issubset(state)
    ):
        return None
    return state


def _resolve_installed_release_asset(asset: Mapping[str, Any]) -> Dict[str, Any]:
    resolved = dict(asset)
    state = load_xberg_release_state()
    if (
        state
        and state["repository"] == asset["repository"]
        and state["asset_name"] == asset["asset_name"]
    ):
        resolved.update(
            {
                "url": state["browser_download_url"],
                "sha256": state["member_sha256"],
                "size_bytes": state["member_size_bytes"],
                "release_tag": state["tag_name"],
            }
        )
    else:
        resolved.update({"url": "", "sha256": "", "size_bytes": -1})
    return resolved


def _resolve_installed_xberg_model_asset(asset: Mapping[str, Any]) -> Dict[str, Any]:
    resolved = dict(asset)
    state = load_xberg_release_state()
    models = state.get("models") if state else None
    model = models.get(asset["id"]) if isinstance(models, dict) else None
    if (
        isinstance(model, dict)
        and model.get("repository") == asset["repository"]
        and model.get("model_path") == asset["model_path"]
    ):
        resolved.update(model)
        resolved["kind"] = "file"
    else:
        resolved.update(
            {
                "url": "",
                "mirror_path": "",
                "sha256": "",
                "size_bytes": -1,
            }
        )
    return resolved


def install_assets(
    group: Optional[str] = None, path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    assets = load_install_manifest(path)["assets"]
    resolved_assets = []
    for asset in assets:
        if asset["kind"] == "github_release_zip_member":
            resolved_assets.append(_resolve_installed_release_asset(asset))
        elif asset["kind"] == "xberg_manifest_model":
            resolved_assets.append(_resolve_installed_xberg_model_asset(asset))
        else:
            resolved_assets.append(asset)
    if group is None:
        return resolved_assets
    return [asset for asset in resolved_assets if asset["group"] == group]


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
