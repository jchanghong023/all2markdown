#!/usr/bin/env python3
"""Provision the managed all2markdown runtime and verified assets.

This installer intentionally uses only Python 3.8-compatible standard-library
APIs. The bootstrap interpreter installs uv; it never runs the product.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

if __package__:
    from . import runtime_paths
else:
    import runtime_paths  # type: ignore


UV_VERSION = "0.12.7"
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAYS = (5, 15)
MAX_RELEASE_METADATA_BYTES = 1024 * 1024
BOOTSTRAP_DIR = runtime_paths.REPO_ROOT / ".tmp" / "init-bootstrap"
PROJECT_VENV = runtime_paths.REPO_ROOT / ".venv"
REQUIREMENTS_PATH = runtime_paths.REPO_ROOT / "requirements.txt"


class InstallError(RuntimeError):
    """An actionable initialization failure."""


def redact_url(value: str) -> str:
    """Remove credentials from a URL before displaying it."""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc or parsed.username is None:
        return value
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = "{}:{}".format(host, parsed.port)
    return urllib.parse.urlunsplit(
        (parsed.scheme, "***@{}".format(host), parsed.path, parsed.query, parsed.fragment)
    )


def asset_url(asset: Mapping[str, Any], mirror_url: Optional[str] = None) -> str:
    mirror = (mirror_url or "").strip()
    if not mirror:
        return str(asset["url"])
    return "{}/{}".format(mirror.rstrip("/"), str(asset["mirror_path"]).lstrip("/"))


def _display_command(command: Sequence[str]) -> str:
    displayed = []
    for value in command:
        text = str(value)
        if "://" in text:
            text = redact_url(text)
        displayed.append('"{}"'.format(text) if " " in text else text)
    return " ".join(displayed)


def run_command(
    command: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess:
    print("运行：{}".format(_display_command(command)))
    try:
        return subprocess.run(
            [str(value) for value in command],
            cwd=str(runtime_paths.REPO_ROOT),
            env=dict(env) if env is not None else None,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError("命令执行失败：{} ({})".format(_display_command(command), exc)) from exc


def ensure_directory(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise InstallError("安装目录被同名文件占用：{}".format(path))
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError("无法创建安装目录 {}：{}".format(path, exc)) from exc


def ensure_install_roots() -> None:
    for path in (
        runtime_paths.model_root(),
        runtime_paths.data_root(),
        runtime_paths.runtime_dir(),
        runtime_paths.xberg_cache_dir(),
        runtime_paths.hf_cache_dir(),
        runtime_paths.sherpa_root(),
        runtime_paths.data_root() / "licenses",
    ):
        ensure_directory(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_asset_file(path: Path, asset: Mapping[str, Any]) -> Tuple[bool, int, Optional[str]]:
    if not path.is_file():
        return False, 0, None
    size = path.stat().st_size
    if size != int(asset["size_bytes"]):
        return False, size, None
    digest = sha256_file(path)
    return digest.lower() == str(asset["sha256"]).lower(), size, digest


def _request(url: str, offset: int = 0) -> urllib.request.Request:
    headers = {"User-Agent": "all2markdown-init/1"}
    if offset:
        headers["Range"] = "bytes={}-".format(offset)
    return urllib.request.Request(url, headers=headers)


def _open_download(url: str, partial: Path):
    offset = partial.stat().st_size if partial.is_file() else 0
    if not offset:
        return urllib.request.urlopen(_request(url), timeout=DOWNLOAD_TIMEOUT), "wb"

    try:
        response = urllib.request.urlopen(_request(url, offset), timeout=DOWNLOAD_TIMEOUT)
    except urllib.error.HTTPError:
        partial.unlink(missing_ok=True)
        return urllib.request.urlopen(_request(url), timeout=DOWNLOAD_TIMEOUT), "wb"

    status = response.getcode()
    content_range = response.headers.get("Content-Range", "")
    if status == 206 and content_range.lower().startswith("bytes {}-".format(offset)):
        return response, "ab"

    response.close()
    partial.unlink(missing_ok=True)
    return urllib.request.urlopen(_request(url), timeout=DOWNLOAD_TIMEOUT), "wb"


def _human_bytes(num: float) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "{:.0f}{}".format(value, unit)
            return "{:.1f}{}".format(value, unit)
        value /= 1024.0
    return "{:.1f}GB".format(value)


def download_to_partial(url: str, partial: Path, label: str = "") -> None:
    """Download with periodic stdout progress (unbuffered-friendly)."""
    ensure_directory(partial.parent)
    tag = " {}".format(label) if label else ""
    response, mode = _open_download(url, partial)
    with response:
        header_total = response.headers.get("Content-Length") or ""
        try:
            remaining = int(header_total) if header_total.strip().isdigit() else 0
        except ValueError:
            remaining = 0
        start = partial.stat().st_size if mode == "ab" and partial.is_file() else 0
        downloaded = start
        expected = start + remaining if remaining > 0 else 0
        last_print = 0.0
        print(
            "开始下载{}：{}".format(tag, _human_bytes(start) if start else "0B"),
            flush=True,
        )
        with partial.open(mode) as handle:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_print >= 1.0:
                    last_print = now
                    if expected > 0:
                        pct = min(100.0, downloaded * 100.0 / expected)
                        print(
                            "下载进度{}：{}/{}（{:.0f}%）".format(
                                tag, _human_bytes(downloaded), _human_bytes(expected), pct
                            ),
                            flush=True,
                        )
                    else:
                        print(
                            "下载进度{}：{}".format(tag, _human_bytes(downloaded)),
                            flush=True,
                        )
        if expected > 0:
            print(
                "下载完成{}：{}/{}（100%）".format(
                    tag, _human_bytes(downloaded), _human_bytes(expected)
                ),
                flush=True,
            )
        else:
            print("下载完成{}：{}".format(tag, _human_bytes(downloaded)), flush=True)

def _load_release_metadata(url: str) -> Mapping[str, Any]:
    try:
        with urllib.request.urlopen(_request(url), timeout=DOWNLOAD_TIMEOUT) as response:
            payload = response.read(MAX_RELEASE_METADATA_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError(
            "无法读取 Xberg 最新发布信息 {}：{}".format(redact_url(url), exc)
        ) from exc
    if len(payload) > MAX_RELEASE_METADATA_BYTES:
        raise InstallError("Xberg 最新发布信息超过大小限制：{}".format(redact_url(url)))
    try:
        metadata = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("Xberg 最新发布信息不是有效 JSON") from exc
    if not isinstance(metadata, dict):
        raise InstallError("Xberg 最新发布信息格式无效")
    return metadata


def resolve_latest_github_release(asset: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve and validate the exact archive published by the latest release."""
    metadata = _load_release_metadata(str(asset["api_url"]))
    tag_name = str(metadata.get("tag_name") or "").strip()
    release_assets = metadata.get("assets")
    if not tag_name or metadata.get("draft") or not isinstance(release_assets, list):
        raise InstallError("Xberg 最新发布信息缺少有效标签或资产列表")
    matches = [
        candidate
        for candidate in release_assets
        if isinstance(candidate, dict) and candidate.get("name") == asset["asset_name"]
    ]
    if len(matches) != 1:
        raise InstallError(
            "Xberg 最新发布 {} 中资产 {} 的匹配数为 {}，预期为 1".format(
                tag_name, asset["asset_name"], len(matches)
            )
        )
    selected = matches[0]
    digest = str(selected.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
        raise InstallError(
            "Xberg 最新发布 {} 的资产缺少 SHA-256 摘要".format(tag_name)
        )
    archive_sha256 = digest.split(":", 1)[1].lower()
    try:
        archive_size = int(selected["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("Xberg 最新发布资产缺少有效大小") from exc
    download_url = str(selected.get("browser_download_url") or "").strip()
    if archive_size <= 0 or urllib.parse.urlsplit(download_url).scheme not in ("http", "https"):
        raise InstallError("Xberg 最新发布资产的大小或下载地址无效")
    return {
        "schema_version": 1,
        "repository": str(asset["repository"]),
        "tag_name": tag_name,
        "asset_name": str(asset["asset_name"]),
        "browser_download_url": download_url,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive_size,
    }


def _safe_archive_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or windows.drive
        or posix.is_absolute()
        or ".." in posix.parts
    ):
        raise InstallError("压缩包包含不安全路径：{}".format(name))
    return normalized


def _selected_member(archive: zipfile.ZipFile, asset: Mapping[str, Any]) -> zipfile.ZipInfo:
    safe_members = []
    for info in archive.infolist():
        normalized = _safe_archive_name(info.filename)
        safe_members.append((info, normalized))

    if asset.get("member"):
        expected = str(asset["member"]).replace("\\", "/")
        matches = [(info, name) for info, name in safe_members if name == expected]
    else:
        expected_basename = str(asset["member_basename"]).casefold()
        matches = [
            (info, name)
            for info, name in safe_members
            if PurePosixPath(name).name.casefold() == expected_basename
        ]

    if len(matches) != 1:
        raise InstallError(
            "资产 {} 的压缩包成员匹配数为 {}，预期为 1".format(asset["id"], len(matches))
        )
    info = matches[0][0]
    unix_mode = (info.external_attr >> 16) & 0o170000
    if info.is_dir() or unix_mode == 0o120000:
        raise InstallError("资产 {} 的目标成员不是普通文件".format(asset["id"]))
    return info


def _copy_archive_notices(
    archive: zipfile.ZipFile, asset: Mapping[str, Any], selected: zipfile.ZipInfo
) -> None:
    notice_names = {
        "license",
        "license.txt",
        "third_party_licenses.md",
        "thirdpartynotices.txt",
        "third-party-notices.txt",
    }
    license_dir = runtime_paths.data_root() / "licenses"
    try:
        ensure_directory(license_dir)
        for info in archive.infolist():
            if info is selected or info.is_dir() or info.file_size > 10 * 1024 * 1024:
                continue
            basename = PurePosixPath(info.filename.replace("\\", "/")).name
            if basename.casefold() not in notice_names:
                continue
            target = license_dir / "{}-{}".format(asset["id"], basename)
            partial = target.with_name(target.name + ".part")
            with archive.open(info) as source, partial.open("wb") as output:
                shutil.copyfileobj(source, output, CHUNK_SIZE)
            os.replace(str(partial), str(target))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print("警告：无法保存 {} 的上游许可文件：{}".format(asset["id"], exc))


def _extract_archive_asset(
    archive_path: Path, destination_partial: Path, asset: Mapping[str, Any]
) -> None:
    with zipfile.ZipFile(str(archive_path), "r") as archive:
        selected = _selected_member(archive, asset)
        with archive.open(selected, "r") as source, destination_partial.open("wb") as output:
            shutil.copyfileobj(source, output, CHUNK_SIZE)
        _copy_archive_notices(archive, asset, selected)


def _extract_archive_tree(archive_path: Path, dest_dir: Path) -> List[Dict[str, Any]]:
    """Extract every archive member into ``dest_dir``.

    A single shared root directory prefix (as produced by the Windows CLI
    packaging script) is stripped so ``xberg.exe`` lands directly under
    ``dest_dir``. Returns the installed file inventory.
    """
    if dest_dir.exists():
        shutil.rmtree(str(dest_dir))
    ensure_directory(dest_dir)

    members: List[Tuple[zipfile.ZipInfo, str]] = []
    with zipfile.ZipFile(str(archive_path), "r") as archive:
        for info in archive.infolist():
            normalized = _safe_archive_name(info.filename)
            if info.is_dir():
                continue
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise InstallError("压缩包包含符号链接：{}".format(info.filename))
            members.append((info, normalized))

        if not members:
            raise InstallError("Xberg 发布压缩包为空")

        prefixes = {PurePosixPath(name).parts[0] for _, name in members if name}
        strip_root: Optional[str] = None
        if len(prefixes) == 1:
            candidate = next(iter(prefixes))
            if all(
                name == candidate or name.startswith(candidate + "/")
                for _, name in members
            ):
                strip_root = candidate

        inventory: List[Dict[str, Any]] = []
        for info, name in members:
            rel = name
            if strip_root is not None:
                rel = name[len(strip_root) :].lstrip("/")
            if not rel or rel.endswith("/"):
                continue
            target = dest_dir.joinpath(*PurePosixPath(rel).parts)
            ensure_directory(target.parent)
            partial = target.with_name(target.name + ".part")
            with archive.open(info, "r") as source, partial.open("wb") as output:
                shutil.copyfileobj(source, output, CHUNK_SIZE)
            os.replace(str(partial), str(target))
            inventory.append(
                {
                    "path": rel.replace("\\", "/"),
                    "size_bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            )
        _copy_tree_notices(dest_dir)
    return inventory


def _copy_tree_notices(dest_dir: Path) -> None:
    license_dir = runtime_paths.data_root() / "licenses"
    try:
        ensure_directory(license_dir)
        for name in ("LICENSE", "THIRD_PARTY_LICENSES.md"):
            source = dest_dir / name
            if not source.is_file():
                continue
            target = license_dir / "xberg-runtime-{}".format(name)
            partial = target.with_name(target.name + ".part")
            with source.open("rb") as src, partial.open("wb") as out:
                shutil.copyfileobj(src, out, CHUNK_SIZE)
            os.replace(str(partial), str(target))
    except OSError as exc:
        print("警告：无法保存 Xberg 上游许可文件：{}".format(exc))


def _release_is_installed(
    asset: Mapping[str, Any], release: Mapping[str, Any], destination: Path
) -> bool:
    state = runtime_paths.load_xberg_release_state()
    if not state:
        return False
    identity_keys = (
        "repository",
        "tag_name",
        "asset_name",
        "browser_download_url",
        "archive_sha256",
        "archive_size_bytes",
    )
    if any(state.get(key) != release.get(key) for key in identity_keys):
        return False
    valid, _, _ = validate_asset_file(
        destination,
        {
            "size_bytes": state["member_size_bytes"],
            "sha256": state["member_sha256"],
        },
    )
    if not valid:
        return False
    runtime_root = destination.parent
    if not (runtime_root / "onnxruntime.dll").is_file():
        return False
    if not (runtime_root / "models").is_dir():
        return False
    return True


def _write_release_state_partial(state: Mapping[str, Any]) -> Path:
    state_path = runtime_paths.xberg_release_state_path()
    ensure_directory(state_path.parent)
    partial = state_path.with_name(state_path.name + ".part")
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(state), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return partial


def _local_xberg_zip_override() -> Optional[Path]:
    raw = os.environ.get("ALL2MARKDOWN_XBERG_ZIP_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = runtime_paths.REPO_ROOT / path
    path = path.resolve()
    if not path.is_file():
        raise InstallError(
            "ALL2MARKDOWN_XBERG_ZIP_PATH 指向的本地 Xberg 压缩包不存在：{}".format(path)
        )
    return path


def _swap_runtime_tree(staging: Path, runtime_root: Path) -> None:
    backup = runtime_root.with_name(runtime_root.name + ".old")
    if backup.exists():
        shutil.rmtree(str(backup))
    if runtime_root.exists():
        runtime_root.rename(backup)
    try:
        staging.rename(runtime_root)
    except OSError:
        if backup.exists() and not runtime_root.exists():
            backup.rename(runtime_root)
        raise
    if backup.exists():
        shutil.rmtree(str(backup), ignore_errors=True)


def _install_latest_github_release_asset(asset: Mapping[str, Any]) -> bool:
    local_zip = _local_xberg_zip_override()
    if local_zip is not None:
        release = {
            "schema_version": 1,
            "kind": "github_release_zip_tree",
            "repository": str(asset["repository"]),
            "tag_name": "local-zip",
            "asset_name": str(asset["asset_name"]),
            "browser_download_url": local_zip.as_uri(),
            "archive_sha256": sha256_file(local_zip),
            "archive_size_bytes": local_zip.stat().st_size,
        }
        print("使用本地 Xberg 压缩包：{}".format(local_zip))
    else:
        release = resolve_latest_github_release(asset)
        release = dict(release)
        release["kind"] = "github_release_zip_tree"

    destination = runtime_paths.asset_path(asset)
    runtime_root = destination.parent
    ensure_directory(runtime_root.parent)
    if _release_is_installed(asset, release, destination):
        print(
            "复用已校验 Xberg 最新发布 {}：{}".format(
                release["tag_name"], runtime_root
            )
        )
        return True

    archive_source: Optional[Path] = None
    archive_owned = False
    if local_zip is not None:
        archive_source = local_zip
    else:
        archive_expected = {
            "id": "{}-archive".format(asset["id"]),
            "size_bytes": release["archive_size_bytes"],
            "sha256": release["archive_sha256"],
        }
        archive_partial = runtime_root.parent / "{}.{}.archive.part".format(
            asset["id"], str(release["archive_sha256"])[:16]
        )
        for obsolete in runtime_root.parent.glob(
            "{}.*.archive.part".format(asset["id"])
        ):
            if obsolete != archive_partial:
                obsolete.unlink(missing_ok=True)
        archive_valid, _, _ = validate_asset_file(archive_partial, archive_expected)
        if not archive_valid:
            if (
                archive_partial.is_file()
                and archive_partial.stat().st_size
                >= int(release["archive_size_bytes"])
            ):
                archive_partial.unlink()
            download_to_partial(
                str(release["browser_download_url"]),
                archive_partial,
                label=str(asset["id"]),
            )
            archive_valid, actual_size, actual_digest = validate_asset_file(
                archive_partial, archive_expected
            )
            if not archive_valid:
                archive_partial.unlink(missing_ok=True)
                raise _validation_error(
                    archive_expected,
                    str(release["browser_download_url"]),
                    actual_size,
                    actual_digest,
                )
        archive_source = archive_partial
        archive_owned = True

    staging = runtime_root.with_name(runtime_root.name + ".staging")
    state_partial: Optional[Path] = None
    try:
        tree_files = _extract_archive_tree(archive_source, staging)
        exe = staging / "xberg.exe"
        ort = staging / "onnxruntime.dll"
        models = staging / "models"
        if not exe.is_file():
            raise InstallError("Xberg 发布包缺少 xberg.exe")
        if not ort.is_file():
            raise InstallError("Xberg 发布包缺少 onnxruntime.dll")
        if not models.is_dir():
            raise InstallError("Xberg 发布包缺少 models 目录")

        state = dict(release)
        state.update(
            {
                "member_sha256": sha256_file(exe),
                "member_size_bytes": exe.stat().st_size,
                "tree_files": tree_files,
            }
        )
        state_partial = _write_release_state_partial(state)
        _swap_runtime_tree(staging, runtime_root)
        os.replace(str(state_partial), str(runtime_paths.xberg_release_state_path()))
        state_partial = None
        if archive_owned and archive_source is not None:
            archive_source.unlink(missing_ok=True)
    except (InstallError, OSError, RuntimeError, zipfile.BadZipFile):
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
        if state_partial is not None:
            state_partial.unlink(missing_ok=True)
        raise
    print(
        "已安装 Xberg 最新发布 {}：{}".format(release["tag_name"], runtime_root)
    )
    return False


def _validation_error(
    asset: Mapping[str, Any], source_url: str, actual_size: int, actual_digest: Optional[str]
) -> InstallError:
    return InstallError(
        "资产 {asset_id} 校验失败；来源 {source}；预期 size={expected_size}, sha256={expected_hash}；"
        "实际 size={actual_size}, sha256={actual_hash}".format(
            asset_id=asset["id"],
            source=redact_url(source_url),
            expected_size=asset["size_bytes"],
            expected_hash=asset["sha256"],
            actual_size=actual_size,
            actual_hash=actual_digest or "<未计算>",
        )
    )


def _prepare_direct_partial(partial: Path, asset: Mapping[str, Any]) -> None:
    if not partial.is_file():
        return
    expected_size = int(asset["size_bytes"])
    size = partial.stat().st_size
    if size > expected_size:
        partial.unlink()
        return
    if size == expected_size:
        valid, _, _ = validate_asset_file(partial, asset)
        if not valid:
            partial.unlink()


def _install_direct_asset(
    asset: Mapping[str, Any], source_url: str, destination: Path
) -> None:
    partial = destination.with_name(destination.name + ".part")
    _prepare_direct_partial(partial, asset)
    download_to_partial(source_url, partial, label=str(asset["id"]))
    valid, actual_size, actual_digest = validate_asset_file(partial, asset)
    if not valid:
        if partial.exists() and actual_size >= int(asset["size_bytes"]):
            partial.unlink()
        raise _validation_error(asset, source_url, actual_size, actual_digest)
    os.replace(str(partial), str(destination))


def _install_archive_asset(
    asset: Mapping[str, Any], source_url: str, destination: Path
) -> None:
    archive_partial = destination.with_name(destination.name + ".archive.part")
    output_partial = destination.with_name(destination.name + ".part")
    output_partial.unlink(missing_ok=True)
    download_to_partial(source_url, archive_partial, label=str(asset["id"]))
    try:
        _extract_archive_asset(archive_partial, output_partial, asset)
        valid, actual_size, actual_digest = validate_asset_file(output_partial, asset)
        if not valid:
            raise _validation_error(asset, source_url, actual_size, actual_digest)
        os.replace(str(output_partial), str(destination))
        archive_partial.unlink(missing_ok=True)
    except (InstallError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        output_partial.unlink(missing_ok=True)
        archive_partial.unlink(missing_ok=True)
        if isinstance(exc, InstallError):
            raise
        raise InstallError(
            "资产 {} 的压缩包处理失败；来源 {}：{}".format(
                asset["id"], redact_url(source_url), exc
            )
        ) from exc


def install_asset(
    asset: Mapping[str, Any],
    mirror_url: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Install one manifest asset; return True when an existing file was reused."""
    if asset["kind"] == "github_release_zip_tree":
        if mirror_url:
            raise InstallError(
                "Xberg 最新发布解析不访问显式资产镜像；"
                "请取消 ALL2MARKDOWN_ASSET_MIRROR_URL 后重试"
            )
        last_error: Optional[BaseException] = None
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                return _install_latest_github_release_asset(asset)
            except (InstallError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt + 1 < DOWNLOAD_ATTEMPTS:
                    delay = RETRY_DELAYS[attempt]
                    print(
                        "Xberg 最新发布第 {} 次安装失败，{} 秒后重试：{}".format(
                            attempt + 1, delay, exc
                        )
                    )
                    sleep(delay)
        raise InstallError("Xberg 最新发布安装失败：{}".format(last_error))
    destination = runtime_paths.asset_path(asset)
    ensure_directory(destination.parent)
    valid, _, _ = validate_asset_file(destination, asset)
    if valid:
        print("复用已校验资产 {}：{}".format(asset["id"], destination))
        return True

    source_url = asset_url(asset, mirror_url)
    print("获取资产 {}：{}".format(asset["id"], redact_url(source_url)))
    last_error: Optional[BaseException] = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            if asset["kind"] == "file":
                _install_direct_asset(asset, source_url, destination)
            elif asset["kind"] == "zip_member":
                _install_archive_asset(asset, source_url, destination)
            else:
                raise InstallError("未知资产类型：{}".format(asset["kind"]))
            print("已安装资产 {}：{}".format(asset["id"], destination))
            return False
        except (InstallError, OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 < DOWNLOAD_ATTEMPTS:
                delay = RETRY_DELAYS[attempt]
                print(
                    "资产 {} 第 {} 次尝试失败，{} 秒后重试：{}".format(
                        asset["id"], attempt + 1, delay, exc
                    )
                )
                sleep(delay)
    raise InstallError(
        "资产 {} 安装失败；尝试来源 {}：{}".format(
            asset["id"], redact_url(source_url), last_error
        )
    )


def install_assets(
    assets: Iterable[Mapping[str, Any]], mirror_url: Optional[str] = None
) -> None:
    for asset in assets:
        install_asset(asset, mirror_url=mirror_url)


def bootstrap_python_path() -> Path:
    return BOOTSTRAP_DIR / "Scripts" / "python.exe"


def uv_executable_path() -> Path:
    return BOOTSTRAP_DIR / "Scripts" / "uv.exe"


def product_python_path() -> Path:
    return PROJECT_VENV / "Scripts" / "python.exe"


def _bootstrap_is_usable() -> bool:
    python = bootstrap_python_path()
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-m", "pip", "--version"],
            cwd=str(runtime_paths.REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _remove_generated_bootstrap() -> None:
    if BOOTSTRAP_DIR.is_dir():
        shutil.rmtree(str(BOOTSTRAP_DIR))
    elif BOOTSTRAP_DIR.exists():
        BOOTSTRAP_DIR.unlink()


def bootstrap_pip_command(python: Path) -> List[str]:
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "uv=={}".format(UV_VERSION),
    ]
    index_url = os.environ.get("ALL2MARKDOWN_PYPI_INDEX_URL", "").strip()
    if index_url:
        command.extend(["--index-url", index_url])
    return command


def prepare_bootstrap() -> Path:
    if BOOTSTRAP_DIR.exists() and not _bootstrap_is_usable():
        print("重建不完整的初始化环境：{}".format(BOOTSTRAP_DIR))
        _remove_generated_bootstrap()
    if not BOOTSTRAP_DIR.exists():
        ensure_directory(BOOTSTRAP_DIR.parent)
        run_command([sys.executable, "-m", "venv", str(BOOTSTRAP_DIR)])
    python = bootstrap_python_path()
    if not _bootstrap_is_usable():
        raise InstallError("初始化环境缺少可用的 Python/pip：{}".format(BOOTSTRAP_DIR))
    run_command(bootstrap_pip_command(python))
    uv = uv_executable_path()
    if not uv.is_file():
        raise InstallError("uv 安装后未找到：{}".format(uv))
    return uv


def uv_environment() -> Dict[str, str]:
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(runtime_paths.data_root() / "python")
    env["UV_CACHE_DIR"] = str(runtime_paths.data_root() / "uv-cache")
    index_url = env.get("ALL2MARKDOWN_PYPI_INDEX_URL", "").strip()
    if index_url:
        env["UV_DEFAULT_INDEX"] = index_url
    return env


def build_uv_python_install_command(uv: Path) -> List[str]:
    return [str(uv), "python", "install", "3.12"]


def build_uv_venv_command(uv: Path) -> List[str]:
    return [
        str(uv),
        "venv",
        str(PROJECT_VENV),
        "--python",
        "3.12",
        "--python-preference",
        "only-managed",
    ]


def build_uv_sync_command(uv: Path, python: Path) -> List[str]:
    return [
        str(uv),
        "pip",
        "sync",
        "--python",
        str(python),
        str(REQUIREMENTS_PATH),
    ]


def is_windows_x64_python312(python: Path) -> bool:
    if not python.is_file():
        return False
    probe = (
        "import platform,sys;"
        "raise SystemExit(0 if sys.version_info[:2] == (3, 12) "
        "and platform.system() == 'Windows' "
        "and platform.machine().lower() in ('amd64','x86_64') "
        "and sys.maxsize > 2**32 else 1)"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", probe],
            cwd=str(runtime_paths.REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def prepare_product_venv(uv: Path, env: Mapping[str, str]) -> Path:
    python = product_python_path()
    if PROJECT_VENV.exists():
        if not is_windows_x64_python312(python):
            raise InstallError(
                "现有 .venv 不是可用的 Windows x64 Python 3.12 环境；"
                "请先移动或删除 {}，再重新运行 init.cmd".format(PROJECT_VENV)
            )
        print("复用项目环境：{}".format(PROJECT_VENV))
        return python
    run_command(build_uv_venv_command(uv), env=env)
    if not is_windows_x64_python312(python):
        raise InstallError("uv 未创建可用的 Windows x64 Python 3.12 环境：{}".format(PROJECT_VENV))
    return python


def smoke_check(python: Path) -> None:
    run_command([str(python), "-c", "import av, numpy, pymupdf, sherpa_onnx"])
    xberg = runtime_paths.runtime_dir() / "xberg.exe"
    run_command([str(xberg), "--version"])


def initialize() -> None:
    if (
        platform.system() != "Windows"
        or platform.machine().lower() not in ("amd64", "x86_64")
        or sys.maxsize <= 2**32
    ):
        raise InstallError("init.cmd 仅支持 Windows x64")
    ensure_install_roots()
    uv = prepare_bootstrap()
    env = uv_environment()
    index_url = env.get("UV_DEFAULT_INDEX")
    if index_url:
        print("Python 包索引：{}".format(redact_url(index_url)))
    run_command(build_uv_python_install_command(uv), env=env)
    product_python = prepare_product_venv(uv, env)
    run_command(build_uv_sync_command(uv, product_python), env=env)
    mirror_url = os.environ.get("ALL2MARKDOWN_ASSET_MIRROR_URL", "").strip() or None
    install_assets(runtime_paths.load_install_manifest()["assets"], mirror_url=mirror_url)
    smoke_check(product_python)
    print("初始化完成。请使用 all2markdown.cmd。")


def main() -> int:
    try:
        initialize()
        return 0
    except KeyboardInterrupt:
        print("初始化已取消。", file=sys.stderr)
        return 130
    except (InstallError, OSError, ValueError) as exc:
        print("初始化失败：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
