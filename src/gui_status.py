"""环境就绪状态检测（Python 3.8+ 标准库，可被未初始化的 GUI 调用）。"""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import runtime_paths  # noqa: E402


REQUIRED_PACKAGES = ("av", "numpy", "pymupdf", "sherpa_onnx")
PROBE_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class StatusItem:
    key: str
    label: str
    ok: bool
    detail: str
    required: bool = True


def is_windows_x64() -> bool:
    return (
        platform.system() == "Windows"
        and platform.machine().lower() in ("amd64", "x86_64")
        and sys.maxsize > 2**32
    )


def find_bootstrap_python() -> Optional[Sequence[str]]:
    """Return a command that can run the installer (Python 3.8+), or None."""
    candidates = (
        ["py", "-3"],
        ["python"],
    )
    for command in candidates:
        if not _command_exists(command[0]):
            continue
        if _python_version_ok(command):
            return list(command)
    return None


def _command_exists(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _python_version_ok(command: Sequence[str]) -> bool:
    probe = (
        "import platform,sys;"
        "raise SystemExit(0 if sys.version_info >= (3, 8) "
        "and platform.system() == 'Windows' "
        "and platform.machine().lower() in ('amd64', 'x86_64') "
        "and sys.maxsize > 2**32 else 1)"
    )
    try:
        result = subprocess.run(
            list(command) + ["-c", probe],
            cwd=str(runtime_paths.REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def venv_python_path() -> Path:
    return runtime_paths.REPO_ROOT / ".venv" / "Scripts" / "python.exe"


def venv_pythonw_path() -> Path:
    return runtime_paths.REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _run_capture(command: Sequence[str], timeout: int = PROBE_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(command),
            cwd=str(runtime_paths.REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout or "", result.stderr or ""


def probe_venv_python(python: Path) -> Tuple[bool, str]:
    if not python.is_file():
        return False, "尚未创建 .venv"
    code, out, err = _run_capture(
        [
            str(python),
            "-c",
            (
                "import platform,sys;"
                "print('%d.%d.%d' % sys.version_info[:3])"
            ),
        ],
        timeout=20,
    )
    if code != 0:
        return False, "无法运行：{}".format((err or out).strip().splitlines()[-1] if (err or out).strip() else "未知错误")
    version = out.strip().splitlines()[-1] if out.strip() else ""
    if not version.startswith("3.12"):
        return False, "版本为 {}，需要 Python 3.12".format(version or "未知")
    if platform.machine().lower() not in ("amd64", "x86_64"):
        return False, "当前机器不是 Windows x64"
    return True, "Python {}".format(version)


def probe_packages(python: Path) -> Tuple[bool, List[str], str]:
    """Import-check runtime packages. Returns (ok, missing, detail)."""
    if not python.is_file():
        return False, list(REQUIRED_PACKAGES), "缺少 .venv，无法检查依赖"
    modules = list(REQUIRED_PACKAGES)
    code = (
        "import importlib,sys\n"
        "missing=[]\n"
        "for m in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "print(','.join(missing))\n"
    )
    rc, out, err = _run_capture([str(python), "-c", code] + modules)
    if rc != 0:
        detail = (err or out).strip().splitlines()[-1] if (err or out).strip() else "依赖检查失败"
        return False, modules, detail
    missing = [item for item in out.strip().split(",") if item]
    if missing:
        return False, missing, "缺少：{}".format(", ".join(missing))
    return True, [], "、".join(REQUIRED_PACKAGES)


def check_xberg_runtime() -> Tuple[bool, str]:
    try:
        assets = runtime_paths.install_assets(group="xberg")
    except (OSError, ValueError) as exc:
        return False, "清单读取失败：{}".format(exc)
    issues = []
    for asset in assets:
        path = runtime_paths.asset_path(asset)
        expected = int(asset.get("size_bytes", -1))
        if not path.is_file():
            issues.append("{} 缺失".format(asset["id"]))
        elif expected > 0 and path.stat().st_size != expected:
            issues.append("{} 大小不符".format(asset["id"]))
        if asset.get("kind") != "github_release_zip_tree":
            continue
        runtime_root = path.parent
        if not (runtime_root / "onnxruntime.dll").is_file():
            issues.append("onnxruntime.dll 缺失")
        if not (runtime_root / "models").is_dir():
            issues.append("models 目录缺失")
    if issues:
        return False, "；".join(issues)
    return True, "xberg.exe、ONNX Runtime 与内置模型完整"


def check_media_assets() -> Tuple[bool, str]:
    try:
        assets = runtime_paths.install_assets(group="media")
    except (OSError, ValueError) as exc:
        return False, "清单读取失败：{}".format(exc)
    issues = []
    for asset in assets:
        path = runtime_paths.asset_path(asset)
        expected = int(asset.get("size_bytes", -1))
        if not path.is_file():
            issues.append(asset["id"])
        elif expected > 0 and path.stat().st_size != expected:
            issues.append("{} 大小不符".format(asset["id"]))
    if issues:
        return False, "缺少或损坏：{}".format(", ".join(issues))
    return True, "SenseVoice / VAD 模型就绪"


def check_avx2_safe() -> Tuple[bool, str]:
    """Best-effort AVX2 probe; unavailable probe reports unknown-friendly detail."""
    if sys.platform != "win32":
        return False, "非 Windows，无法检测 AVX2"
    try:
        if __package__:
            from . import all2markdown_core as core
        else:
            import all2markdown_core as core  # type: ignore
        ok = bool(core.check_avx2())
        return ok, "支持 AVX2" if ok else "CPU 不支持 AVX2"
    except Exception:
        # Bootstrap interpreter may not load core; treat as unchecked soft status.
        return True, "未能检测（初始化后由转换预检确认）"


def collect_status(probe_python: Optional[bool] = None) -> List[StatusItem]:
    """Collect environment status items.

    ``probe_python`` forces package probing on/off; default: probe when .venv exists.
    """
    items: List[StatusItem] = []

    platform_ok = is_windows_x64()
    items.append(
        StatusItem(
            "platform",
            "系统平台",
            platform_ok,
            "Windows x64" if platform_ok else "需要 Windows x64（当前 {} {}）".format(
                platform.system(), platform.machine()
            ),
        )
    )

    bootstrap = find_bootstrap_python()
    items.append(
        StatusItem(
            "bootstrap_python",
            "初始化解释器",
            bootstrap is not None,
            " ".join(bootstrap) if bootstrap else "未找到 Python 3.8+（init 需要）",
            required=False,
        )
    )

    python = venv_python_path()
    venv_ok, venv_detail = probe_venv_python(python)
    items.append(StatusItem("venv", "项目环境", venv_ok, venv_detail))

    do_probe = (python.is_file() if probe_python is None else probe_python)
    if do_probe and venv_ok:
        packages_ok, _missing, packages_detail = probe_packages(python)
    elif not venv_ok:
        packages_ok, packages_detail = False, "待初始化后安装"
    else:
        packages_ok, packages_detail = True, "跳过探测"
    items.append(StatusItem("packages", "运行依赖", packages_ok, packages_detail))

    xberg_ok, xberg_detail = check_xberg_runtime()
    items.append(StatusItem("xberg", "Xberg 运行时", xberg_ok, xberg_detail))

    media_ok, media_detail = check_media_assets()
    items.append(StatusItem("media", "媒体模型", media_ok, media_detail))

    avx2_ok, avx2_detail = check_avx2_safe()
    items.append(
        StatusItem("avx2", "CPU（AVX2）", avx2_ok, avx2_detail, required=True)
    )

    return items


def is_ready(items: Sequence[StatusItem]) -> bool:
    return all(item.ok for item in items if item.required)


def missing_summary(items: Sequence[StatusItem]) -> List[str]:
    return [item.label for item in items if item.required and not item.ok]


def current_interpreter_is_venv() -> bool:
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return False
    managed = (runtime_paths.REPO_ROOT / ".venv" / "Scripts").resolve()
    return exe.parent == managed and exe.stem.lower() in ("python", "pythonw")


def init_command() -> Optional[List[str]]:
    """Command that runs the installer with streaming console output."""
    bootstrap = find_bootstrap_python()
    if bootstrap is None:
        return None
    script = runtime_paths.REPO_ROOT / "src" / "init_env.py"
    # -u keeps progress prints unbuffered for the GUI log pump.
    if bootstrap and bootstrap[0].endswith("py") and len(bootstrap) >= 2:
        return list(bootstrap[:2]) + ["-u", str(script)]
    return list(bootstrap) + ["-u", str(script)]
