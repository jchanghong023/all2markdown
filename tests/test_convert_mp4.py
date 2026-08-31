"""Unit contracts plus heavyweight ASR integration for initialized user models."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import convert_mp4, runtime_paths  # noqa: E402

# All test temp files/directories live under .tmp/tests (gitignored).
TEST_TEMP_ROOT = REPO_ROOT / ".tmp" / "tests"

TIMESTAMP_LINE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}\] \S", re.MULTILINE
)


MEDIA_READY = convert_mp4._IMPORT_ERROR is None and not convert_mp4.asset_issues()


@unittest.skipUnless(MEDIA_READY, "run init.cmd before media integration tests")
class ConvertMp4IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="convert_mp4_test_", dir=str(TEST_TEMP_ROOT))
        )
        cls.input_dir = cls.tmp / "input"
        cls.output_dir = cls.tmp / "output"
        cls.input_dir.mkdir()
        examples = REPO_ROOT / "tests" / "test_example"
        # Real Chinese speech (video-to-notes intro, MIT), 79s, h264 + AAC.
        fixture = examples / "video-to-notes-intro-zh.mp4"
        shutil.copy(fixture, cls.input_dir / "intro.mp4")
        # .m4a (audio-only MP4 container) must route through the same ASR
        # pipeline; PyAV probes content, so the shared fixture also covers
        # the m4a extension path.
        shutil.copy(fixture, cls.input_dir / "intro.m4a")
        # Directory hierarchy must be preserved.
        (cls.input_dir / "sub").mkdir()
        shutil.copy(fixture, cls.input_dir / "sub" / "lecture.mp4")
        # Corrupt input must fail in isolation without killing the batch.
        (cls.input_dir / "bad.mp4").write_bytes(b"this is not a real mp4 file")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_transcription_and_skip(self) -> None:
        with self.assertLogs(level="ERROR") as captured:
            rc = convert_mp4.main([str(self.input_dir), str(self.output_dir)])
        self.assertTrue(any("bad.mp4" in message for message in captured.output))
        # bad.mp4 must fail in isolation; the rest must succeed.
        self.assertEqual(rc, convert_mp4.EXIT_PARTIAL)

        intro_md = self.output_dir / "intro_mp4.md"
        self.assertTrue(intro_md.is_file(), "missing intro_mp4.md")
        text = intro_md.read_text(encoding="utf-8")
        # SenseVoice INT8, language=zh, use_itn=true on the fixture audio.
        self.assertIn("视频自带字幕", text, "transcribed Chinese text missing")
        self.assertIn("课程", text)
        self.assertIn("播客", text)
        self.assertIn("## 转录", text)
        self.assertIn("- 音频时长: 00:01:19", text)
        segment_match = re.search(r"- 语音片段: (\d+)", text)
        self.assertIsNotNone(segment_match)
        self.assertGreater(int(segment_match.group(1)), 0)
        self.assertRegex(text, TIMESTAMP_LINE, "timestamped transcript line missing")
        self.assertNotIn("（未检测到语音）", text)

        lecture_md = self.output_dir / "sub" / "lecture_mp4.md"
        self.assertTrue(lecture_md.is_file(), "hierarchy not preserved")
        self.assertIn("视频自带字幕", lecture_md.read_text(encoding="utf-8"))

        # .m4a inputs transcribe to <name>_m4a.md with the same content.
        intro_m4a_md = self.output_dir / "intro_m4a.md"
        self.assertTrue(intro_m4a_md.is_file(), "missing intro_m4a.md")
        m4a_text = intro_m4a_md.read_text(encoding="utf-8")
        self.assertIn("视频自带字幕", m4a_text)
        m4a_segment_match = re.search(r"- 语音片段: (\d+)", m4a_text)
        self.assertIsNotNone(m4a_segment_match)
        self.assertGreater(int(m4a_segment_match.group(1)), 0)
        self.assertRegex(m4a_text, TIMESTAMP_LINE, "timestamped transcript line missing")

        self.assertFalse((self.output_dir / "bad_mp4.md").exists())

        # Second run: everything already transcribed -> nothing to do, exit 0.
        (self.input_dir / "bad.mp4").unlink()  # 移除坏文件，模拟已修复输入
        mtime_before = {p: p.stat().st_mtime_ns for p in self.output_dir.rglob("*.md")}
        rc2 = convert_mp4.main([str(self.input_dir), str(self.output_dir)])
        self.assertEqual(rc2, convert_mp4.EXIT_OK)
        mtime_after = {p: p.stat().st_mtime_ns for p in self.output_dir.rglob("*.md")}
        self.assertEqual(mtime_before, mtime_after, "existing markdown must not be rewritten")

    def test_invalid_input_dir_rejected(self) -> None:
        out = self.tmp / "cfg_out"
        rc = convert_mp4.main([str(self.tmp / "missing_input"), str(out)])
        self.assertEqual(rc, convert_mp4.EXIT_USAGE)
        self.assertFalse(out.exists(), "invalid input must not create output dir")


class InstalledMediaContractTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="media_contract_", dir=str(TEST_TEMP_ROOT))
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {"ALL2MARKDOWN_MODEL_DIR": str(self.tmp / "models")},
            clear=False,
        )
        self.env_patch.start()
        self.asset = {
            "id": "sensevoice-test",
            "group": "media",
            "kind": "file",
            "url": "https://example.invalid/model",
            "mirror_path": "model",
            "root": "model",
            "relative_path": "sherpa_onnx/v1.13.6/models/test.onnx",
            "sha256": "0" * 64,
            "size_bytes": 4,
        }

    def tearDown(self) -> None:
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_model_root_resolution(self) -> None:
        expected_root = (self.tmp / "models" / "sherpa_onnx" / "v1.13.6").resolve()
        self.assertEqual(runtime_paths.sherpa_root(), expected_root)
        self.assertEqual(
            convert_mp4.sense_voice_dir(),
            expected_root / "models" / "sense_voice_zh_en_ja_ko_yue_2024_07_17",
        )
        self.assertEqual(
            convert_mp4.vad_model_path(),
            expected_root / "models" / "vad" / "silero_vad.onnx",
        )

    def test_missing_and_wrong_size_messages_request_init_repair(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            runtime_paths, "install_assets", return_value=[self.asset]
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            convert_mp4.verify_assets()
        message = stderr.getvalue()
        self.assertIn("sensevoice-test", message)
        self.assertIn(str(runtime_paths.asset_path(self.asset)), message)
        self.assertIn("init.cmd", message)

        model = runtime_paths.asset_path(self.asset)
        model.parent.mkdir(parents=True)
        model.write_bytes(b"bad")
        stderr = io.StringIO()
        with mock.patch.object(
            runtime_paths, "install_assets", return_value=[self.asset]
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            convert_mp4.verify_assets()
        self.assertIn("大小 3，预期 4", stderr.getvalue())
        self.assertIn("init.cmd", stderr.getvalue())

    def test_recognizer_and_vad_receive_absolute_user_paths(self) -> None:
        captured: dict[str, object] = {}

        class OfflineRecognizer:
            @staticmethod
            def from_sense_voice(**kwargs: object) -> object:
                captured["recognizer"] = kwargs
                return object()

        def silero_config(**kwargs: object) -> object:
            captured["silero"] = kwargs
            return kwargs

        def vad_config(**kwargs: object) -> object:
            captured["vad_config"] = kwargs
            return kwargs

        def detector(config: object, **kwargs: object) -> object:
            captured["detector"] = (config, kwargs)
            return object()

        fake_sherpa = types.SimpleNamespace(
            OfflineRecognizer=OfflineRecognizer,
            SileroVadModelConfig=silero_config,
            VadModelConfig=vad_config,
            VoiceActivityDetector=detector,
        )
        with mock.patch.object(convert_mp4, "sherpa_onnx", fake_sherpa):
            convert_mp4.create_recognizer(language="zh", use_itn=True, num_threads=6)
            convert_mp4.create_vad()

        recognizer = captured["recognizer"]
        model = pathlib.Path(recognizer["model"])
        tokens = pathlib.Path(recognizer["tokens"])
        self.assertTrue(model.is_absolute())
        self.assertTrue(tokens.is_absolute())
        self.assertEqual(model, convert_mp4.sense_voice_dir() / "model.int8.onnx")
        self.assertEqual(tokens, convert_mp4.sense_voice_dir() / "tokens.txt")
        self.assertEqual(recognizer["provider"], "cpu")
        silero = captured["silero"]
        vad_model = pathlib.Path(silero["model"])
        self.assertTrue(vad_model.is_absolute())
        self.assertEqual(vad_model, convert_mp4.vad_model_path())
        self.assertEqual(captured["vad_config"]["provider"], "cpu")


class FormatTimestampTest(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(convert_mp4.format_timestamp(0.0), "00:00:00.000")

    def test_sub_second(self) -> None:
        self.assertEqual(convert_mp4.format_timestamp(0.742), "00:00:00.742")

    def test_hours(self) -> None:
        self.assertEqual(convert_mp4.format_timestamp(3661.5), "01:01:01.500")

    def test_negative_clamped(self) -> None:
        self.assertEqual(convert_mp4.format_timestamp(-1.0), "00:00:00.000")


class BuildMarkdownTest(unittest.TestCase):
    def test_normal(self) -> None:
        lines = [convert_mp4.TranscriptLine(0.742, 5.6, "开饭时间早上9点至下午5点。")]
        md = convert_mp4.build_markdown("speech.mp4", 5.632, lines, has_audio_track=True)
        self.assertIn("# speech.mp4", md)
        self.assertIn("- 音频时长: 00:00:05.632", md)
        self.assertIn("[00:00:00.742 --> 00:00:05.600] 开饭时间早上9点至下午5点。", md)

    def test_no_speech(self) -> None:
        md = convert_mp4.build_markdown("silent.mp4", 9.77, [], has_audio_track=True)
        self.assertIn("- 语音片段: 0", md)
        self.assertIn("（未检测到语音）", md)

    def test_no_audio_track(self) -> None:
        md = convert_mp4.build_markdown("video_only.mp4", 0.0, [], has_audio_track=False)
        self.assertIn("无音频轨道", md)


class ChipTerminologyTest(unittest.TestCase):
    def test_fixed_replacements_only(self) -> None:
        text = convert_mp4.normalize_chip_terms("扫描链和静态时序分析，A T P G 与未知词")
        self.assertEqual(text, "scan chain和STA，ATPG 与未知词")


class MediaOutputPathTest(unittest.TestCase):
    def test_supported_inputs_have_distinct_extension_preserving_names(self) -> None:
        self.assertEqual(
            convert_mp4.media_markdown_relative_path(pathlib.Path("lecture.mp4")),
            pathlib.Path("lecture_mp4.md"),
        )
        self.assertEqual(
            convert_mp4.media_markdown_relative_path(pathlib.Path("lecture.m4a")),
            pathlib.Path("lecture_m4a.md"),
        )
        self.assertEqual(
            convert_mp4.media_markdown_relative_path(pathlib.Path("lecture_m4a.mp4")),
            pathlib.Path("lecture_m4a_mp4.md"),
        )


class ParseExtensionsTest(unittest.TestCase):
    def test_default(self) -> None:
        defaults = set(convert_mp4.DEFAULT_EXTENSIONS)
        self.assertEqual(defaults, {".mp4", ".m4a"})
        self.assertEqual(
            convert_mp4.parse_extensions(",".join(convert_mp4.DEFAULT_EXTENSIONS)),
            defaults,
        )

    def test_mixed_case_and_missing_dot(self) -> None:
        self.assertEqual(
            convert_mp4.parse_extensions(".MP4, mkv, mov"), {".mp4", ".mkv", ".mov"}
        )

    def test_empty_items_dropped(self) -> None:
        self.assertEqual(convert_mp4.parse_extensions(".mp4,, "), {".mp4"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
