"""Offline contract tests for init.cmd provisioning helpers."""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
import warnings
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import init_env, runtime_paths  # noqa: E402

TEST_TEMP_ROOT = REPO_ROOT / ".tmp"



class AssetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        self.server.requests.append((path, self.headers.get("Range")))
        payload = self.server.payloads.get(path)
        if payload is None:
            self.send_error(404)
            return
        range_header = self.headers.get("Range")
        if range_header and path not in self.server.ignore_range:
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            if offset >= len(payload):
                self.send_response(416)
                self.end_headers()
                return
            body = payload[offset:]
            self.send_response(206)
            self.send_header(
                "Content-Range", "bytes {}-{}/{}".format(offset, len(payload) - 1, len(payload))
            )
        else:
            body = payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class InitEnvAssetTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="init_env_test_", dir=str(TEST_TEMP_ROOT))
        )
        self.model_root = self.tmp / "models"
        self.data_root = self.tmp / "data"
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "ALL2MARKDOWN_MODEL_DIR": str(self.model_root),
                "ALL2MARKDOWN_DATA_DIR": str(self.data_root),
                "ALL2MARKDOWN_ASSET_MIRROR_URL": "",
                "ALL2MARKDOWN_PYPI_INDEX_URL": "",
            },
            clear=False,
        )
        self.env_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
        self.server.payloads = {}
        self.server.ignore_range = set()
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def asset(
        self,
        asset_id: str,
        payload: bytes,
        path: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
    ) -> dict[str, object]:
        return {
            "id": asset_id,
            "group": "media",
            "kind": "file",
            "url": self.base_url + path,
            "mirror_path": asset_id + ".bin",
            "root": "model",
            "relative_path": "tests/{}.bin".format(asset_id),
            "sha256": sha256 or hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload) if size is None else size,
        }

    def test_official_and_mirror_urls_never_fall_back(self) -> None:
        payload = b"mirror payload"
        asset = self.asset("source", payload, "/official/source.bin")
        mirror_base = self.base_url + "/mirror"
        self.server.payloads["/official/source.bin"] = payload
        self.server.payloads["/mirror/source.bin"] = b"wrong payload!"

        self.assertEqual(init_env.asset_url(asset), self.base_url + "/official/source.bin")
        self.assertEqual(
            init_env.asset_url(asset, mirror_base), self.base_url + "/mirror/source.bin"
        )
        with self.assertRaises(init_env.InstallError):
            init_env.install_asset(asset, mirror_url=mirror_base, sleep=lambda _delay: None)
        requested_paths = [path for path, _range in self.server.requests]
        self.assertEqual(requested_paths, ["/mirror/source.bin"] * 3)
        self.assertNotIn("/official/source.bin", requested_paths)

    def test_range_resume_and_ignored_range_restart(self) -> None:
        payload = b"0123456789abcdef"
        resume = self.asset("resume", payload, "/resume.bin")
        self.server.payloads["/resume.bin"] = payload
        resume_partial = runtime_paths.asset_path(resume).with_name("resume.bin.part")
        resume_partial.parent.mkdir(parents=True)
        resume_partial.write_bytes(payload[:5])

        init_env.install_asset(resume, sleep=lambda _delay: None)
        self.assertEqual(runtime_paths.asset_path(resume).read_bytes(), payload)
        self.assertIn(("/resume.bin", "bytes=5-"), self.server.requests)

        ignored = self.asset("ignored", payload, "/ignored.bin")
        self.server.payloads["/ignored.bin"] = payload
        self.server.ignore_range.add("/ignored.bin")
        ignored_partial = runtime_paths.asset_path(ignored).with_name("ignored.bin.part")
        ignored_partial.write_bytes(payload[:4])

        init_env.install_asset(ignored, sleep=lambda _delay: None)
        self.assertEqual(runtime_paths.asset_path(ignored).read_bytes(), payload)
        ignored_requests = [item for item in self.server.requests if item[0] == "/ignored.bin"]
        self.assertEqual(ignored_requests, [("/ignored.bin", "bytes=4-"), ("/ignored.bin", None)])

    def test_existing_asset_reuse_and_corrupt_asset_atomic_repair(self) -> None:
        payload = b"verified"
        asset = self.asset("repair", payload, "/repair.bin")
        self.server.payloads["/repair.bin"] = payload
        destination = runtime_paths.asset_path(asset)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"corrupt!")

        self.assertFalse(init_env.install_asset(asset, sleep=lambda _delay: None))
        self.assertEqual(destination.read_bytes(), payload)
        request_count = len(self.server.requests)
        self.assertTrue(init_env.install_asset(asset, sleep=lambda _delay: None))
        self.assertEqual(len(self.server.requests), request_count)

    def test_checksum_failure_preserves_prior_final_file(self) -> None:
        expected = b"expected"
        downloaded = b"bad-data"
        asset = self.asset(
            "checksum",
            downloaded,
            "/checksum.bin",
            sha256=hashlib.sha256(expected).hexdigest(),
        )
        self.server.payloads["/checksum.bin"] = downloaded
        destination = runtime_paths.asset_path(asset)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"prior-ok")

        with self.assertRaisesRegex(init_env.InstallError, "checksum"):
            init_env.install_asset(asset, sleep=lambda _delay: None)
        self.assertEqual(destination.read_bytes(), b"prior-ok")
        self.assertFalse(destination.with_name("checksum.bin.part").exists())

    def test_archive_member_selection_and_traversal_rejection(self) -> None:
        member = b"selected member"
        valid_zip = self.tmp / "valid.zip"
        with zipfile.ZipFile(valid_zip, "w") as archive:
            archive.writestr("nested/tool.exe", member)
        valid_asset = {
            "id": "archive",
            "kind": "zip_member",
            "member_basename": "tool.exe",
            "sha256": hashlib.sha256(member).hexdigest(),
            "size_bytes": len(member),
        }
        output = self.tmp / "selected.part"
        init_env._extract_archive_asset(valid_zip, output, valid_asset)
        self.assertEqual(output.read_bytes(), member)

        duplicate_zip = self.tmp / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate_zip, "w") as archive:
                archive.writestr("a/tool.exe", member)
                archive.writestr("b/tool.exe", member)
        with self.assertRaises(init_env.InstallError):
            init_env._extract_archive_asset(duplicate_zip, output, valid_asset)

        traversal_zip = self.tmp / "traversal.zip"
        with zipfile.ZipFile(traversal_zip, "w") as archive:
            archive.writestr("../escape.txt", b"escape")
            archive.writestr("tool.exe", member)
        with self.assertRaisesRegex(init_env.InstallError, "不安全路径"):
            init_env._extract_archive_asset(traversal_zip, output, valid_asset)

        missing_zip = self.tmp / "missing.zip"
        with zipfile.ZipFile(missing_zip, "w") as archive:
            archive.writestr("other.exe", member)
        with self.assertRaises(init_env.InstallError):
            init_env._extract_archive_asset(missing_zip, output, valid_asset)


class InitEnvBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="init_bootstrap_test_", dir=str(TEST_TEMP_ROOT))
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "ALL2MARKDOWN_MODEL_DIR": str(self.tmp / "models"),
                "ALL2MARKDOWN_DATA_DIR": str(self.tmp / "data"),
                "ALL2MARKDOWN_PYPI_INDEX_URL": "https://user:secret@example.invalid/simple",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bootstrap_and_uv_command_construction(self) -> None:
        python = pathlib.Path("bootstrap-python.exe")
        uv = pathlib.Path("uv.exe")
        product_python = pathlib.Path("product-python.exe")
        self.assertEqual(init_env.bootstrap_pip_command(python)[-3:], [
            "uv==0.12.7",
            "--index-url",
            "https://user:secret@example.invalid/simple",
        ])
        self.assertEqual(
            init_env.build_uv_python_install_command(uv), ["uv.exe", "python", "install", "3.12"]
        )
        self.assertEqual(
            init_env.build_uv_venv_command(uv)[-4:],
            ["--python", "3.12", "--python-preference", "only-managed"],
        )
        self.assertEqual(
            init_env.build_uv_sync_command(uv, product_python)[:5],
            ["uv.exe", "pip", "sync", "--python", "product-python.exe"],
        )
        env = init_env.uv_environment()
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(runtime_paths.data_root() / "python"))
        self.assertEqual(env["UV_CACHE_DIR"], str(runtime_paths.data_root() / "uv-cache"))
        self.assertEqual(env["UV_DEFAULT_INDEX"], os.environ["ALL2MARKDOWN_PYPI_INDEX_URL"])
        self.assertNotIn("secret", init_env.redact_url(os.environ["ALL2MARKDOWN_PYPI_INDEX_URL"]))

    def test_valid_and_conflicting_project_venv(self) -> None:
        project_venv = self.tmp / ".venv"
        python = project_venv / "Scripts" / "python.exe"
        project_venv.mkdir()
        with mock.patch.object(init_env, "PROJECT_VENV", project_venv), mock.patch.object(
            init_env, "is_windows_x64_python312", return_value=True
        ), mock.patch.object(init_env, "run_command") as run:
            self.assertEqual(init_env.prepare_product_venv(pathlib.Path("uv.exe"), {}), python)
            run.assert_not_called()

        marker = project_venv / "keep.txt"
        marker.write_text("user environment", encoding="utf-8")
        with mock.patch.object(init_env, "PROJECT_VENV", project_venv), mock.patch.object(
            init_env, "is_windows_x64_python312", return_value=False
        ), mock.patch.object(init_env, "run_command") as run:
            with self.assertRaisesRegex(init_env.InstallError, "移动或删除"):
                init_env.prepare_product_venv(pathlib.Path("uv.exe"), {})
            run.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "user environment")

    def test_explicit_model_and_data_root_overrides(self) -> None:
        self.assertEqual(runtime_paths.model_root(), (self.tmp / "models").resolve())
        self.assertEqual(runtime_paths.data_root(), (self.tmp / "data").resolve())
        self.assertEqual(
            runtime_paths.hf_cache_dir(),
            (self.tmp / "models" / "xberg" / "v1.0.14" / "hf").resolve(),
        )
        self.assertEqual(
            runtime_paths.runtime_dir(),
            (self.tmp / "data" / "xberg" / "v1.0.14" / "runtime").resolve(),
        )
        with mock.patch.dict(
            os.environ,
            {
                "ALL2MARKDOWN_MODEL_DIR": ".tmp/relative-model",
                "ALL2MARKDOWN_DATA_DIR": ".tmp/relative-data",
            },
        ):
            self.assertEqual(
                runtime_paths.model_root(), (REPO_ROOT / ".tmp" / "relative-model").resolve()
            )
            self.assertEqual(
                runtime_paths.data_root(), (REPO_ROOT / ".tmp" / "relative-data").resolve()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
