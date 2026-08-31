"""Unit contracts plus heavyweight integration for the initialized runtime."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import all2markdown  # noqa: E402
from src import all2markdown_core, convert_mp4, runtime_paths  # noqa: E402

# All test temp files/directories live under .tmp/tests (gitignored).
TEST_TEMP_ROOT = REPO_ROOT / ".tmp" / "tests"

def _assets_ready(group: str) -> bool:
    return all(
        runtime_paths.asset_path(asset).is_file()
        and runtime_paths.asset_path(asset).stat().st_size == int(asset["size_bytes"])
        for asset in runtime_paths.install_assets(group=group)
    )


XBERG_ASSETS_OK = _assets_ready("xberg")
MP4_DEPS_OK = convert_mp4._IMPORT_ERROR is None and _assets_ready("media")
REQUIRE_REAL_CONVERSION = (
    os.environ.get("ALL2MARKDOWN_REQUIRE_REAL_CONVERSION") == "1"
)


class RequiredRealConversionAssetsTest(unittest.TestCase):
    @unittest.skipUnless(
        REQUIRE_REAL_CONVERSION,
        "set ALL2MARKDOWN_REQUIRE_REAL_CONVERSION=1 to enforce initialized assets",
    )
    def test_initialized_assets_are_available(self) -> None:
        self.assertTrue(XBERG_ASSETS_OK, "Xberg assets unavailable; run init.cmd")
        self.assertIsNone(
            convert_mp4._IMPORT_ERROR,
            f"media dependencies unavailable: {convert_mp4._IMPORT_ERROR}",
        )
        self.assertEqual(
            convert_mp4.asset_issues(),
            [],
            "media assets unavailable; run init.cmd",
        )


@unittest.skipUnless(XBERG_ASSETS_OK, "run init.cmd before Xberg integration tests")
class ConvertDocsIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="all2markdown_test_", dir=str(TEST_TEMP_ROOT))
        )
        cls.input_dir = cls.tmp / "input"
        cls.output_dir = cls.tmp / "output"
        cls.input_dir.mkdir()
        cls.output_dir.mkdir()
        examples = REPO_ROOT / "tests" / "test_example"
        shutil.copy(examples / "test_hello_world.png", cls.input_dir / "hello.png")
        shutil.copy(examples / "merged_table.pptx", cls.input_dir / "table.pptx")
        shutil.copy(examples / "single_paper.pdf", cls.input_dir / "native.pdf")
        shutil.copy(examples / "scanned_hello.pdf", cls.input_dir / "scanned.pdf")
        shutil.copy(
            examples / "docx_with_embedded_office.docx",
            cls.input_dir / "embed_office_doc.docx",
        )
        shutil.copy(
            examples / "pptx_with_embedded_office.pptx",
            cls.input_dir / "embed_office_ppt.pptx",
        )
        (cls.input_dir / "sub").mkdir()
        shutil.copy(examples / "merged_header.xlsx", cls.input_dir / "sub" / "sheet.xlsx")
        # MP4/M4A inputs are routed to the local ASR pipeline (convert_mp4).
        if MP4_DEPS_OK:
            shutil.copy(
                examples / "video-to-notes-intro-zh.mp4", cls.input_dir / "intro.mp4"
            )
            shutil.copy(
                examples / "video-to-notes-intro-zh.mp4", cls.input_dir / "intro.m4a"
            )
        (cls.input_dir / "bad.docx").write_bytes(b"this is not a real docx file")
        with zipfile.ZipFile(cls.input_dir / "bundle.zip", "w") as zf:
            zf.write(examples / "single_paper.pdf", "docs/native.pdf")
            zf.writestr("data/table.txt", "alpha,beta\n1,2\n")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_conversion_and_skip(self) -> None:
        with self.assertLogs(level="ERROR") as captured:
            rc = all2markdown.main(
                [
                    str(self.input_dir),
                    str(self.output_dir),
                    "--xberg-config",
                    str(all2markdown.CONFIG_PATH),
                ]
            )
        self.assertTrue(any("bad.docx" in message for message in captured.output))
        # bad.docx must fail in isolation; the rest must succeed.
        self.assertEqual(rc, all2markdown.EXIT_PARTIAL)

        expected = [
            "hello_png.md",
            "table_pptx.md",
            "native_pdf.md",
            "scanned_pdf.md",
            "embed_office_doc_docx.md",
            "embed_office_ppt_pptx.md",
            "sub/sheet_xlsx.md",
            "bundle_zip.md",
        ]
        if MP4_DEPS_OK:
            expected.append("intro_mp4.md")
            expected.append("intro_m4a.md")
        for rel in expected:
            md = self.output_dir / rel
            self.assertTrue(md.is_file(), f"missing output {rel}")
            text = md.read_text(encoding="utf-8")
            self.assertNotIn("![", text, f"image reference leaked into {rel}")
            self.assertNotIn("images/", text, f"image dir leaked into {rel}")

        self.assertFalse((self.output_dir / "bad_docx.md").exists())

        hello_md = (self.output_dir / "hello_png.md").read_text(encoding="utf-8")
        self.assertIn("Hello World", hello_md, "image OCR text missing from markdown")
        self.assertEqual(
            hello_md,
            "---\n```text\nHello World\n```\n---\n",
            "image OCR must be one literal block without line/word duplication",
        )

        # DOCX embeddings are scanned on the URI path; both OOXML children recurse.
        doc_embed_md = (self.output_dir / "embed_office_doc_docx.md").read_text(encoding="utf-8")
        self.assertIn("## Embedded document: merged_header.xlsx", doc_embed_md)
        self.assertIn("## Embedded document: merged_table.pptx", doc_embed_md)

        # PPTX embeddings are only scanned on the bytes path (v1.0.14
        # extract_path bug); all2markdown must send PPTX as base64 bytes.
        ppt_embed_md = (self.output_dir / "embed_office_ppt_pptx.md").read_text(encoding="utf-8")
        self.assertIn("## Embedded document: merged_header.xlsx", ppt_embed_md)

        bundle_md = (self.output_dir / "bundle_zip.md").read_text(encoding="utf-8")
        self.assertIn("## Embedded document: docs/native.pdf", bundle_md)
        self.assertIn("## Embedded document: data/table.txt", bundle_md)
        self.assertIn("alpha,beta", bundle_md)

        scanned_md = (self.output_dir / "scanned_pdf.md").read_text(encoding="utf-8")
        self.assertIn("invoice", scanned_md.lower(), "scanned pdf OCR text missing")

        # MP4/M4A routing: transcribed locally, never sent to Xberg.
        if MP4_DEPS_OK:
            intro_md = (self.output_dir / "intro_mp4.md").read_text(encoding="utf-8")
            self.assertIn("视频自带字幕", intro_md, "MP4 transcription text missing")
            self.assertRegex(intro_md, r"- 语音片段: [1-9]\d*")
            self.assertIn("## 转录", intro_md)
            m4a_md = (self.output_dir / "intro_m4a.md").read_text(encoding="utf-8")
            self.assertIn("视频自带字幕", m4a_md, "M4A transcription text missing")
            self.assertRegex(m4a_md, r"- 语音片段: [1-9]\d*")
            self.assertIn("## 转录", m4a_md)
        else:
            self.assertFalse((self.output_dir / "intro_mp4.md").exists())
            self.assertFalse((self.output_dir / "intro_m4a.md").exists())

        # Second run: everything already converted -> nothing to do, exit 0.
        (self.input_dir / "bad.docx").unlink()  # 移除坏文件，模拟已修复输入
        mtime_before = {p: p.stat().st_mtime_ns for p in self.output_dir.rglob("*.md")}
        rc2 = all2markdown.main([str(self.input_dir), str(self.output_dir)])
        self.assertEqual(rc2, all2markdown.EXIT_OK)
        mtime_after = {p: p.stat().st_mtime_ns for p in self.output_dir.rglob("*.md")}
        self.assertEqual(mtime_before, mtime_after, "existing markdown must not be rewritten")

    def test_invalid_xberg_config_rejected(self) -> None:
        out = self.tmp / "cfg_out"
        rc = all2markdown.main(
            [
                str(self.input_dir),
                str(out),
                "--xberg-config",
                str(self.tmp / "missing.json"),
            ]
        )
        self.assertEqual(rc, all2markdown.EXIT_USAGE)
        self.assertFalse(out.exists(), "invalid config must not create output dir")


class StripImagePlaceholdersTest(unittest.TestCase):
    def test_standalone_reference_removed(self) -> None:
        text = "before\n![img](image_1.png)\nafter\n"
        self.assertEqual(all2markdown.strip_image_placeholders(text), "before\nafter\n")

    def test_escaped_reference_removed(self) -> None:
        text = "before\n\\![](../media/image17.png)\nafter\n"
        self.assertEqual(all2markdown.strip_image_placeholders(text), "before\nafter\n")

    def test_multiple_references_on_one_line_removed(self) -> None:
        text = "![a](1.png) ![b](2.emf)\nkept\n"
        self.assertEqual(all2markdown.strip_image_placeholders(text), "kept\n")

    def test_inline_reference_in_text_kept(self) -> None:
        text = "see ![alt](x.png) in this sentence\n"
        self.assertEqual(all2markdown.strip_image_placeholders(text), text)

    def test_reference_inside_backtick_fence_kept(self) -> None:
        text = "```md\n![alt](x.png)\n```\n"
        self.assertEqual(all2markdown.strip_image_placeholders(text), text)

    def test_reference_inside_tilde_fence_kept(self) -> None:
        text = "~~~\n![alt](x.png)\n~~~\n![gone](y.png)\n"
        self.assertEqual(all2markdown.strip_image_placeholders(text), "~~~\n![alt](x.png)\n~~~\n")


class NormalizeMarkdownTest(unittest.TestCase):
    def test_numeric_entities_decoded(self) -> None:
        self.assertEqual(
            all2markdown.normalize_markdown("title&#9;1 and&#32;nbsp&#160;x"),
            "title\t1 and nbsp x",
        )

    def test_entities_inside_fence_untouched(self) -> None:
        text = "```\na&#9;b\n```"
        self.assertEqual(all2markdown.normalize_markdown(text), text)

    def test_inline_code_untouched(self) -> None:
        text = "Use `a == b && c == d&#32;` and ==yellow highlight==."
        self.assertEqual(
            all2markdown.normalize_markdown(text),
            "Use `a == b && c == d&#32;` and yellow highlight.",
        )


    def test_highlight_markers_removed(self) -> None:
        self.assertEqual(
            all2markdown.normalize_markdown("Some ==yellow highlight== here."),
            "Some yellow highlight here.",
        )

    def test_dropcap_paragraph_rejoined(self) -> None:
        self.assertEqual(
            all2markdown.normalize_markdown("D\n\nrop caps are used to emphasize."),
            "Drop caps are used to emphasize.",
        )

    def test_single_letter_before_uppercase_not_merged(self) -> None:
        text = "A\n\nNew paragraph follows."
        self.assertEqual(all2markdown.normalize_markdown(text), text)

    def test_dropcap_inside_fence_untouched(self) -> None:
        text = "```\nD\n\nrop\n```"
        self.assertEqual(all2markdown.normalize_markdown(text), text)


class EmbeddedPackageFilteringTest(unittest.TestCase):
    def test_nested_visio_xml_is_not_emitted_as_semantic_sections(self) -> None:
        doc = {
            "content": "# Training",
            "children": [
                {
                    "path": "ppt/embeddings/Drawing.vsdx",
                    "result": {
                        "content": "# Diagram title",
                        "children": [
                            {
                                "path": "visio/pages/page1.xml",
                                "result": {
                                    "content": (
                                        "## VisioDocument\n\n## DocumentSheet\n\n"
                                        "### Cell\n\n### Cell\n\n## DataTransferInfo"
                                    )
                                },
                            }
                        ],
                    },
                }
            ],
        }

        text = all2markdown.build_final_markdown(doc)

        self.assertIn(
            "## Embedded document: ppt/embeddings/Drawing.vsdx",
            text,
        )
        self.assertIn("# Diagram title", text)
        self.assertNotIn("page1.xml", text)
        self.assertNotIn("VisioDocument", text)
        self.assertNotIn("DocumentSheet", text)
        self.assertNotIn("### Cell", text)
        self.assertNotIn("DataTransferInfo", text)

    def test_embedded_image_ocr_uses_the_same_assembler(self) -> None:
        doc = {
            "content": "# Bundle",
            "children": [
                {
                    "path": "scans/page.jp2",
                    "result": {"content": "## Cell\n\n## Cell\n"},
                }
            ],
        }

        text = all2markdown.build_final_markdown(doc)

        self.assertIn("## Embedded document: scans/page.jp2", text)
        self.assertIn("```text\n## Cell\n```", text)
        self.assertEqual(text.count("## Cell"), 1)


class SpatialOcrLayoutTest(unittest.TestCase):
    def _box(self, text: str, left: int, top: int, right: int, bottom: int, level: str = "line") -> dict[str, object]:
        return {
            "text": text,
            "level": level,
            "geometry": {
                "type": "quadrilateral",
                "points": [[left, top], [right, top], [right, bottom], [left, bottom]],
            },
            "confidence": {"recognition": 0.95},
        }

    def test_parent_line_preferred_over_word_children(self) -> None:
        elements = [
            self._box("Hello World", 10, 10, 110, 25, "line"),
            self._box("Hello", 12, 10, 55, 25, "word"),
            self._box("World", 60, 10, 108, 25, "word"),
        ]
        self.assertEqual(all2markdown.spatial_ocr_markdown(elements), "Hello World")

    def test_backend_accepted_low_confidence_text_is_preserved(self) -> None:
        element = self._box("faint but valid", 10, 10, 140, 25)
        element["confidence"] = {"recognition": 0.2}

        self.assertEqual(
            all2markdown.spatial_ocr_markdown([element]),
            "faint but valid",
        )

    def test_markdown_punctuation_from_image_ocr_is_literal(self) -> None:
        text = all2markdown.build_final_markdown(
            {"content": "## Cell\n\n---"},
            image_input=True,
        )
        self.assertEqual(text, "---\n```text\n## Cell\n\n---\n```\n---\n")

    def test_repeated_spatial_ocr_blocks_are_preserved(self) -> None:
        elements = [
            self._box("COPY", 10, 10, 80, 30),
            self._box("COPY", 10, 100, 80, 120),
        ]
        text = all2markdown.build_final_markdown(
            {
                "content": "COPY\n\nCOPY\n\nCOPY\n\nCOPY",
                "ocr_elements": elements,
            },
            image_input=True,
        )
        self.assertIn("COPY\n\nCOPY", text)
        self.assertEqual(text.count("COPY"), 2)

    def test_code_columns_and_long_line_are_not_wrapped(self) -> None:
        elements = [
            self._box("Signal", 10, 10, 70, 20),
            self._box("Value", 150, 10, 200, 20),
            self._box("Status", 260, 10, 320, 20),
            self._box("CLK", 10, 30, 40, 40),
            self._box("100M", 150, 30, 190, 40),
            self._box("PASS", 260, 30, 300, 40),
        ]
        text = all2markdown.spatial_ocr_markdown(elements)
        self.assertIn("Signal        Value      Status", text)
        self.assertIn("CLK           100M       PASS", text)
        self.assertFalse(text.startswith("```"))

        markdown = all2markdown.build_final_markdown(
            {"content": "", "extraction_method": "ocr", "ocr_elements": elements}
        )
        self.assertTrue(markdown.startswith("---\n```text\n"))
        self.assertTrue(markdown.endswith("\n```\n---\n"))
        self.assertIn("Signal        Value      Status", markdown)

        long_line = "LOG " + ("x" * 200)
        long_text = all2markdown.spatial_ocr_markdown([self._box(long_line, 10, 60, 2110, 70)])
        self.assertEqual(long_text, long_line)


class CollapseWholeDocumentDuplicateTest(unittest.TestCase):
    def test_duplicated_halves_collapsed(self) -> None:
        self.assertEqual(
            all2markdown.collapse_whole_document_duplicate("Hello World\n\nHello World\n"),
            "Hello World\n",
        )

    def test_multiblock_duplicate_collapsed(self) -> None:
        text = "one\n\ntwo\n\none\n\ntwo\n"
        self.assertEqual(all2markdown.collapse_whole_document_duplicate(text), "one\n\ntwo\n")

    def test_asymmetric_text_untouched(self) -> None:
        text = "one\n\ntwo\n\nthree\n"
        self.assertEqual(all2markdown.collapse_whole_document_duplicate(text), text)


class FormatRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.tmp = pathlib.Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ooxml_zip_variants_get_registered_mime_hints(self) -> None:
        expected = {
            ".docm": "application/vnd.ms-word.document.macroEnabled.12",
            ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
            ".dotm": "application/vnd.ms-word.template.macroEnabled.12",
            ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
            ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
            ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
            ".xlam": "application/vnd.ms-excel.addin.macroEnabled.12",
        }
        for extension, mime_type in expected.items():
            with self.subTest(extension=extension):
                package = self.tmp / f"book{extension}"
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr("[Content_Types].xml", b"package")
                self.assertEqual(all2markdown.mime_hint(package), mime_type)

    def test_large_presentation_expands_server_request_limit(self) -> None:
        input_dir = self.tmp / "input"
        input_dir.mkdir()
        presentation = input_dir / "large.pptx"
        size = 75 * 1024 * 1024
        with presentation.open("wb") as handle:
            handle.write(b"PK\x03\x04")
            handle.truncate(size)
        config = {"output_format": "markdown"}

        limit = all2markdown_core._xberg_request_body_limit(
            input_dir, (config,)
        )
        empty_body = {
            "inputs": [
                {
                    "data": "",
                    "mime_type": all2markdown.EXT_TO_MIME[".pptx"],
                    "filename": presentation.name,
                }
            ],
            "config": config,
        }
        expected = len(
            json.dumps(empty_body, ensure_ascii=False).encode("utf-8")
        ) + 4 * ((size + 2) // 3)
        self.assertEqual(limit, expected)
        self.assertGreater(
            limit, all2markdown_core.XBERG_DEFAULT_MAX_REQUEST_BODY_BYTES
        )

    def test_advanced_image_output_collapses_duplicate_ocr(self) -> None:
        input_dir = self.tmp / "input"
        output_dir = self.tmp / "output"
        input_dir.mkdir()

        for extension in (".jp2", ".j2k", ".j2c"):
            with self.subTest(extension=extension):
                source = input_dir / f"scan{extension}"
                all2markdown.convert_one_result(
                    {"content": "Hello World\n\nHello World\n"},
                    source,
                    input_dir,
                    output_dir,
                )

                text = (output_dir / f"scan_{extension[1:]}.md").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(text.count("Hello World"), 1)

    def test_nested_output_tree_is_not_rescanned(self) -> None:
        input_dir = self.tmp / "input"
        output_dir = input_dir / "markdown"
        output_dir.mkdir(parents=True)
        source = input_dir / "source.docx"
        source.write_bytes(b"source")
        (output_dir / "generated.md").write_text("generated", encoding="utf-8")

        files, skipped = all2markdown.scan_inputs(
            input_dir,
            output_dir,
            {".docx", ".md"},
        )

        self.assertEqual(files, [source])
        self.assertEqual(skipped, 0)


class DefaultDirectoriesTest(unittest.TestCase):
    def test_default_constants_point_into_repo(self) -> None:
        self.assertEqual(all2markdown.DEFAULT_INPUT_DIR, all2markdown.REPO_ROOT / "input")
        self.assertEqual(all2markdown.DEFAULT_OUTPUT_DIR, all2markdown.REPO_ROOT / "output")

    def test_positional_args_optional(self) -> None:
        args = all2markdown.build_parser().parse_args([])
        self.assertIsNone(args.input_dir)
        self.assertIsNone(args.output_dir)
        self.assertEqual(args.xberg_config, all2markdown.CONFIG_PATH)

    def test_positional_args_accepted(self) -> None:
        args = all2markdown.build_parser().parse_args(["in_dir", "out_dir"])
        self.assertEqual(args.input_dir, "in_dir")
        self.assertEqual(args.output_dir, "out_dir")


class InstalledRuntimeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="runtime_contract_", dir=str(TEST_TEMP_ROOT))
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "ALL2MARKDOWN_MODEL_DIR": str(self.tmp / "models"),
                "ALL2MARKDOWN_DATA_DIR": str(self.tmp / "data"),
            },
            clear=False,
        )
        self.env_patch.start()
        self.assets = [
            {
                "id": "runtime-test",
                "group": "xberg",
                "kind": "file",
                "url": "https://example.invalid/runtime",
                "mirror_path": "runtime",
                "root": "runtime",
                "relative_path": "runtime-test.bin",
                "sha256": "0" * 64,
                "size_bytes": 2,
            },
            {
                "id": "model-test",
                "group": "xberg",
                "kind": "file",
                "url": "https://example.invalid/model",
                "mirror_path": "model",
                "root": "model",
                "relative_path": "xberg/model-test.bin",
                "sha256": "1" * 64,
                "size_bytes": 3,
            },
        ]

    def tearDown(self) -> None:
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preflight_reports_manifest_id_and_user_path_when_missing(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            runtime_paths, "install_assets", return_value=self.assets
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            all2markdown_core.verify_assets()
        self.assertEqual(raised.exception.code, all2markdown_core.EXIT_PREFLIGHT)
        message = stderr.getvalue()
        self.assertIn("runtime-test", message)
        self.assertIn(str(runtime_paths.runtime_dir() / "runtime-test.bin"), message)
        self.assertIn("init.cmd", message)

    def test_preflight_reports_wrong_size_without_hashing(self) -> None:
        runtime_file = runtime_paths.asset_path(self.assets[0])
        model_file = runtime_paths.asset_path(self.assets[1])
        runtime_file.parent.mkdir(parents=True)
        model_file.parent.mkdir(parents=True)
        runtime_file.write_bytes(b"x")
        model_file.write_bytes(b"abc")
        stderr = io.StringIO()
        with mock.patch.object(
            runtime_paths, "install_assets", return_value=self.assets
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            all2markdown_core.verify_assets()
        message = stderr.getvalue()
        self.assertIn("runtime-test", message)
        self.assertIn("大小 1，预期 2", message)
        self.assertNotIn("model-test", message)

    def test_build_env_uses_user_roots_and_forces_offline_cpu(self) -> None:
        env = all2markdown_core.build_env(
            43123, max_request_body_bytes=123_456_789
        )
        self.assertEqual(env["HF_HUB_CACHE"], str(runtime_paths.hf_cache_dir()))
        self.assertEqual(env["XBERG_CACHE_DIR"], str(runtime_paths.xberg_cache_dir()))
        self.assertEqual(
            env["ORT_DYLIB_PATH"], str(runtime_paths.runtime_dir() / "onnxruntime.dll")
        )
        for name in (
            "HF_HUB_OFFLINE",
            "HUGGINGFACE_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "HF_DATASETS_OFFLINE",
        ):
            self.assertEqual(env[name], "1")
        self.assertEqual(env["XBERG_ORT_EP"], "cpu")
        self.assertEqual(env["XBERG_API_ALLOW_LOCAL_URI_INPUTS"], "1")
        self.assertEqual(env["XBERG_MAX_CONCURRENT_REQUESTS"], "0")
        self.assertEqual(env["XBERG_MAX_REQUEST_BODY_BYTES"], "123456789")
        self.assertEqual(env["XBERG_CORS_ORIGINS"], "http://127.0.0.1:43123")

    @unittest.skipUnless(sys.platform == "win32", "Windows CMD contract")
    def test_launcher_refuses_path_python_when_venv_absent(self) -> None:
        launcher = self.tmp / "all2markdown.cmd"
        shutil.copyfile(REPO_ROOT / "all2markdown.cmd", launcher)
        result = subprocess.run(
            ["cmd", "/d", "/c", str(launcher)],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("尚未初始化，请先运行 init.cmd", result.stdout)


class MediaBackendConfigTest(unittest.TestCase):
    def test_default_is_local(self) -> None:
        self.assertEqual(all2markdown.media_transcription_backend({}), "local")

    def test_xberg_backend(self) -> None:
        self.assertEqual(
            all2markdown.media_transcription_backend(
                {all2markdown.MEDIA_BACKEND_KEY: "XBERG"}
            ),
            "xberg",
        )

    def test_invalid_backend(self) -> None:
        with self.assertRaises(ValueError):
            all2markdown.media_transcription_backend(
                {all2markdown.MEDIA_BACKEND_KEY: "whisper"}
            )

    def test_split_removes_backend_selector(self) -> None:
        xberg, policy = all2markdown.split_pipeline_config(
            {
                "output_format": "markdown",
                all2markdown.MEDIA_BACKEND_KEY: "xberg",
                "large_document": {"enabled": False},
            }
        )
        self.assertEqual(xberg, {"output_format": "markdown"})
        self.assertEqual(policy, {"enabled": False})


class PageCountTest(unittest.TestCase):
    """page_count() reads metadata/structure only; failures degrade to None."""

    @classmethod
    def setUpClass(cls) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="page_count_test_", dir=str(TEST_TEMP_ROOT))
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _docx(self, pages: str | None) -> pathlib.Path:
        p = self.tmp / f"doc_{pages or 'empty'}.docx"
        body = (
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            f"<Pages>{pages}</Pages></Properties>"
            if pages is not None
            else '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"/>'
        )
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("docProps/app.xml", body)
        return p

    def test_docx_pages_from_app_xml(self) -> None:
        self.assertEqual(all2markdown.page_count(self._docx("321")), 321)

    def test_docx_missing_pages_returns_none(self) -> None:
        self.assertIsNone(all2markdown.page_count(self._docx(None)))

    def test_docx_non_numeric_pages_returns_none(self) -> None:
        self.assertIsNone(all2markdown.page_count(self._docx("many")))

    def test_docx_corrupt_zip_returns_none(self) -> None:
        p = self.tmp / "bad.docx"
        p.write_bytes(b"not a zip archive")
        self.assertIsNone(all2markdown.page_count(p))

    def _pptx(self, count: int, slides_only_masters: bool = False) -> pathlib.Path:
        p = self.tmp / f"deck_{count}.pptx"
        with zipfile.ZipFile(p, "w") as zf:
            if not slides_only_masters:
                for i in range(1, count + 1):
                    zf.writestr(f"ppt/slides/slide{i}.xml", "<p:sld/>")
            zf.writestr("ppt/slideMasters/slideMaster1.xml", "<x/>")
            zf.writestr("ppt/slideLayouts/slideLayout1.xml", "<x/>")
        return p

    def test_pptx_slide_count(self) -> None:
        self.assertEqual(all2markdown.page_count(self._pptx(250)), 250)

    def test_pptx_masters_are_not_counted(self) -> None:
        self.assertIsNone(all2markdown.page_count(self._pptx(0, slides_only_masters=True)))

    def test_xlsx_not_counted(self) -> None:
        p = self.tmp / "book.xlsx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("docProps/app.xml", "<x/>")
        self.assertIsNone(all2markdown.page_count(p))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(all2markdown.page_count(self.tmp / "nope.pdf"))

    @unittest.skipUnless(all2markdown.PYMUPDF_IMPORT_ERROR is None, "PyMuPDF 未安装")
    def test_pdf_page_count(self) -> None:
        fixture = all2markdown.REPO_ROOT / "tests" / "test_example" / "large_210_pages.pdf"
        self.assertEqual(all2markdown.page_count(fixture), 210)

    @unittest.skipUnless(all2markdown.PYMUPDF_IMPORT_ERROR is None, "PyMuPDF 未安装")
    def test_pdf_corrupt_returns_none(self) -> None:
        p = self.tmp / "broken.pdf"
        p.write_bytes(b"%PDF-1.4\nthis is not a real pdf\n%%EOF\n")
        self.assertIsNone(all2markdown.page_count(p))


class LargeDocRoutingTest(unittest.TestCase):
    """resolve_mode(): fast only when a count exists and is strictly > threshold."""

    @classmethod
    def setUpClass(cls) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="routing_test_", dir=str(TEST_TEMP_ROOT))
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _ppt(self, count: int) -> pathlib.Path:
        p = self.tmp / f"r_{count}.pptx"
        with zipfile.ZipFile(p, "w") as zf:
            for i in range(1, count + 1):
                zf.writestr(f"ppt/slides/slide{i}.xml", "<p:sld/>")
        return p

    def test_threshold_boundary(self) -> None:
        policy = {"enabled": True, "page_threshold": 200}
        self.assertEqual(all2markdown.resolve_mode(self._ppt(200), policy), ("normal", 200))
        self.assertEqual(all2markdown.resolve_mode(self._ppt(201), policy), ("fast", 201))

    def test_disabled_feature_always_normal(self) -> None:
        policy = {"enabled": False, "page_threshold": 200}
        self.assertEqual(all2markdown.resolve_mode(self._ppt(500), policy), ("normal", None))

    def test_unknown_count_normal(self) -> None:
        policy = {"enabled": True, "page_threshold": 200}
        p = self.tmp / "x.xlsx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("docProps/app.xml", "<x/>")
        self.assertEqual(all2markdown.resolve_mode(p, policy), ("normal", None))

    def test_failed_count_normal(self) -> None:
        policy = {"enabled": True, "page_threshold": 200}
        p = self.tmp / "broken.docx"
        p.write_bytes(b"not a zip archive")
        self.assertEqual(all2markdown.resolve_mode(p, policy), ("normal", None))

    def test_bad_threshold_falls_back_200(self) -> None:
        policy = {"enabled": True, "page_threshold": "lots"}
        self.assertEqual(all2markdown.resolve_mode(self._ppt(201), policy), ("fast", 201))


class PipelineConfigSplitTest(unittest.TestCase):
    """Policy block is pipeline-only; fast config toggles only the expensive steps."""

    def test_split_removes_policy_block(self) -> None:
        raw = {
            "use_cache": True,
            "layout": {"strategy": "auto"},
            "large_document": {"enabled": True, "page_threshold": 200},
        }
        xberg, policy = all2markdown.split_pipeline_config(raw)
        self.assertEqual(policy, {"enabled": True, "page_threshold": 200})
        self.assertNotIn("large_document", xberg)
        self.assertEqual(xberg, {"use_cache": True, "layout": {"strategy": "auto"}})

    def test_split_no_policy_block(self) -> None:
        xberg, policy = all2markdown.split_pipeline_config({"use_cache": True})
        self.assertEqual(policy, {})
        self.assertEqual(xberg, {"use_cache": True})

    def test_real_config_contains_policy(self) -> None:
        raw = json.loads(all2markdown.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(raw["large_document"]["enabled"])
        xberg, policy = all2markdown.split_pipeline_config(raw)
        self.assertNotIn("large_document", xberg)
        self.assertEqual(policy["page_threshold"], 200)

    def test_fast_mode_only_toggles_expensive_steps(self) -> None:
        raw = json.loads(all2markdown.CONFIG_PATH.read_text(encoding="utf-8"))
        xberg, _ = all2markdown.split_pipeline_config(raw)
        fast = all2markdown.fast_mode_config(xberg)
        self.assertIsNone(fast["layout"])
        self.assertFalse(fast["use_layout_for_markdown"])
        # image extraction off (image OCR off implicitly: nothing extracted)
        self.assertFalse(fast["pdf_options"]["extract_images"])
        self.assertFalse(fast["images"]["extract_images"])
        # 图片/OCR 全关（fast 模式按需求关闭；见 §12 已知缺陷说明）
        self.assertFalse(fast["images"]["run_ocr_on_images"])
        # native processing preserved
        self.assertTrue(fast["pdf_options"]["extract_tables"])
        self.assertEqual(fast["ocr_strategy"]["mode"], "auto")
        self.assertFalse(fast["disable_ocr"])
        for key in ("use_cache", "output_format", "concurrency", "max_embedded_file_bytes", "security_limits"):
            self.assertEqual(fast[key], xberg[key], f"fast config must keep {key}")


@unittest.skipUnless(XBERG_ASSETS_OK, "run init.cmd before Xberg integration tests")
class LargeDocFastModeIntegrationTest(unittest.TestCase):
    """End-to-end: a >200-page PDF must enter fast mode against the real Xberg server.

    The 210-page ReportLab fixture is routed with the fast config (layout/RT-DETR/
    TATR off, no image extraction or image OCR) while native text/table/heading/OCR
    auto stay on; exactly one Markdown is produced, and a second run skips it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="large_doc_fast_test_", dir=str(TEST_TEMP_ROOT))
        )
        cls.input_dir = cls.tmp / "input"
        cls.output_dir = cls.tmp / "output"
        cls.input_dir.mkdir()
        examples = all2markdown.REPO_ROOT / "tests" / "test_example"
        shutil.copy(
            examples / "large_210_pages.pdf", cls.input_dir / "large_210_pages.pdf"
        )
        shutil.copy(examples / "merged_table.pptx", cls.input_dir / "small.pptx")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @unittest.skipUnless(all2markdown.PYMUPDF_IMPORT_ERROR is None, "PyMuPDF 未安装")
    def test_fast_mode_routing_end_to_end(self) -> None:
        captured: dict[str, dict[str, object]] = {}
        original = all2markdown.extract_single

        def spy(port: int, path: pathlib.Path, config: dict[str, object], timeout: int) -> dict[str, object]:
            captured[path.name] = config
            return original(port, path, config, timeout)

        all2markdown.extract_single = spy
        try:
            rc = all2markdown.main([str(self.input_dir), str(self.output_dir)])
        finally:
            all2markdown.extract_single = original
        self.assertEqual(rc, all2markdown.EXIT_OK)

        cfg = captured.get("large_210_pages.pdf")
        self.assertIsNotNone(cfg, "the 210-page pdf must be extracted")
        # fast mode: layout (RT-DETR + TATR) off, no image extraction / image OCR
        self.assertIsNone(cfg.get("layout"))
        self.assertFalse(cfg.get("use_layout_for_markdown", False))
        self.assertFalse(cfg.get("pdf_options", {}).get("extract_images", False))
        self.assertFalse(cfg.get("images", {}).get("extract_images", False))
        # 图片/OCR 全关（fast 模式按需求开启；见 AGENTS.md §12 已知缺陷说明）
        self.assertFalse(cfg.get("images", {}).get("run_ocr_on_images", False))
        # native processing preserved
        self.assertTrue(cfg.get("pdf_options", {}).get("extract_tables", True))
        self.assertEqual(cfg.get("ocr_strategy", {}).get("mode"), "auto")
        self.assertFalse(cfg.get("disable_ocr", False))

        small = captured.get("small.pptx")
        self.assertIsNotNone(small)
        self.assertIsInstance(small.get("layout"), dict, "small pptx must keep normal layout config")

        md = self.output_dir / "large_210_pages_pdf.md"
        self.assertTrue(md.is_file(), "missing single markdown for the large pdf")
        text = md.read_text(encoding="utf-8")
        # 2 lines x 120 "1" per page x 210 pages = 50400; native text must be kept
        # even with the all-visual-steps-off fast config (dense text avoids the
        # sparse-page OCR-fallback defect, see AGENTS.md §12).
        self.assertGreaterEqual(text.count("1"), 50000, "native text of every page should be preserved")
        self.assertNotIn("![", text, "no image references expected")

        # Second run: already converted -> skip, exit 0, markdown untouched.
        mtime_before = md.stat().st_mtime_ns
        captured2: dict[str, object] = {}

        def spy2(port: int, path: pathlib.Path, config: dict[str, object], timeout: int) -> dict[str, object]:
            captured2[path.name] = config
            return original(port, path, config, timeout)

        all2markdown.extract_single = spy2
        try:
            rc2 = all2markdown.main([str(self.input_dir), str(self.output_dir)])
        finally:
            all2markdown.extract_single = original
        self.assertEqual(rc2, all2markdown.EXIT_OK)
        self.assertEqual(md.stat().st_mtime_ns, mtime_before, "existing markdown must not be rewritten")
        self.assertEqual(captured2, {}, "skipped run must not call /extract")


if __name__ == "__main__":
    unittest.main(verbosity=2)
