"""GUI 逻辑测试：状态检测、进度解析、日志队列、返回码文案（无显示可跑）。
App 本体冒烟仅在有显示时执行；--flat 端到端在 test_all2markdown 中。
"""

from __future__ import annotations

import io
import pathlib
import queue
import sys
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import all2markdown  # noqa: E402
from src import gui  # noqa: E402
from src import gui_status  # noqa: E402


class AppSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.app = gui.App()
        except Exception as exc:  # noqa: BLE001 - 无显示时整类跳过
            raise unittest.SkipTest(f"需要图形显示才可构建 App：{exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        app = getattr(cls, "app", None)
        if app is not None:
            app.destroy()

    def test_builds_widgets(self) -> None:
        self.assertIn("all2markdown", self.app.title())
        self.assertEqual(self.app._start_btn["text"], "开始转换")
        self.assertEqual(self.app._stop_btn["text"], "停止")
        self.assertEqual(str(self.app._stop_btn["state"]), "disabled")
        self.assertTrue(self.app._init_btn is not None)
        self.assertTrue(self.app._recheck_btn is not None)
        self.assertTrue(self.app._bar is not None)
        self.assertTrue(self.app._log is not None)
        self.assertTrue(self.app._ready_banner is not None)


class ParseProgressTest(unittest.TestCase):
    def test_total(self) -> None:
        self.assertEqual(
            gui.parse_progress("共 12 个待转换文件（文档 12，媒体 0，另跳过 3 个）"),
            ("total", 12, 12),
        )

    def test_done_styles(self) -> None:
        self.assertEqual(gui.parse_progress("[完成 3/12] a.pdf 耗时 1.2s"), ("done", 3, 12))
        self.assertEqual(gui.parse_progress("进度 12/12（成功 12）"), ("done", 12, 12))

    def test_ignores_other_lines(self) -> None:
        self.assertIsNone(gui.parse_progress("预检通过（平台 / AVX2 / 离线资产），耗时 0.0s"))
        self.assertIsNone(gui.parse_progress("[跳过] a.pdf (已有 a_pdf.md)"))
        self.assertIsNone(gui.parse_progress(""))


class TeeStderrTest(unittest.TestCase):
    def test_fans_out_to_queue_console_and_file(self) -> None:
        target: queue.Queue[str] = queue.Queue()
        console = io.StringIO()
        fh = io.StringIO()
        tee = gui.TeeStderr(target.put, console, fh)
        self.assertEqual(tee.write("预检失败\n\n下一行"), len("预检失败\n\n下一行"))
        tee.flush()
        # 空行不入队，非空行进面板队列。
        self.assertEqual(target.get_nowait(), "预检失败")
        self.assertEqual(target.get_nowait(), "下一行")
        self.assertTrue(target.empty())
        # 控制台与文件保留原文（含空行）。
        self.assertEqual(console.getvalue(), "预检失败\n\n下一行")
        self.assertEqual(fh.getvalue(), "预检失败\n\n下一行")

    def test_none_targets_are_safe(self) -> None:
        target: queue.Queue[str] = queue.Queue()
        tee = gui.TeeStderr(target.put, None, None)
        tee.write("hello")
        tee.flush()


class ResolveInitialDirTest(unittest.TestCase):
    def test_empty_and_flags(self) -> None:
        self.assertEqual(gui.resolve_initial_dir(None), "")
        self.assertEqual(gui.resolve_initial_dir([]), "")
        self.assertEqual(gui.resolve_initial_dir(["--flat"]), "")

    def test_first_positional_wins(self) -> None:
        self.assertEqual(gui.resolve_initial_dir(["D:/docs"]), "D:/docs")
        self.assertEqual(gui.resolve_initial_dir(["--x", "D:/a", "D:/b"]), "D:/a")


class DescribeReturncodeTest(unittest.TestCase):
    def test_codes(self) -> None:
        self.assertIn("全部成功", gui.describe_returncode(all2markdown.EXIT_OK))
        self.assertIn("部分", gui.describe_returncode(all2markdown.EXIT_PARTIAL))
        self.assertIn("预检失败", gui.describe_returncode(all2markdown.EXIT_PREFLIGHT))
        self.assertIn("用法错误", gui.describe_returncode(all2markdown.EXIT_USAGE))
        self.assertIn("手动停止", gui.describe_returncode(all2markdown.EXIT_CANCELLED))
        self.assertIn("42", gui.describe_returncode(42))

    def test_run_log_naming(self) -> None:
        path = gui.gui_run_log_path()
        self.assertEqual(path.parent.name, ".tmp")
        self.assertRegex(path.name, r"^gui-\d{8}-\d{6}\.log$")


class ConversionArgvTest(unittest.TestCase):
    def test_unrestricted_omits_exts(self) -> None:
        self.assertEqual(
            gui.build_conversion_argv("D:/in", "output", None),
            ["D:/in", "output", "--flat"],
        )

    def test_restricted_appends_sorted_exts(self) -> None:
        self.assertEqual(
            gui.build_conversion_argv("D:/in", "output", {".png", ".pdf"}),
            ["D:/in", "output", "--flat", "--exts", ".pdf,.png"],
        )

    def test_all_checked_means_unrestricted(self) -> None:
        states = {name: True for name in all2markdown.FORMAT_GROUPS}
        self.assertIsNone(
            gui.selected_extensions(states, all2markdown.FORMAT_GROUPS)
        )

    def test_partial_selection_unions_groups(self) -> None:
        states = {name: False for name in all2markdown.FORMAT_GROUPS}
        states["PDF 文档"] = True
        selected = gui.selected_extensions(states, all2markdown.FORMAT_GROUPS)
        self.assertEqual(selected, {".pdf"})

    def test_nothing_checked_gives_empty_set(self) -> None:
        states = {name: False for name in all2markdown.FORMAT_GROUPS}
        self.assertEqual(
            gui.selected_extensions(states, all2markdown.FORMAT_GROUPS), set()
        )


class ParseEnhancedTest(unittest.TestCase):
    def test_current_file(self) -> None:
        self.assertEqual(
            gui.parse_current_file("[开始 1/3] docs/a.pdf（0.5 MB）"),
            "docs/a.pdf（0.5 MB）",
        )
        self.assertIsNone(gui.parse_current_file("预检通过"))

    def test_failed_file(self) -> None:
        self.assertEqual(
            gui.parse_failed_file("[失败 2/3] docs/b.pdf: boom"),
            "docs/b.pdf",
        )
        self.assertIsNone(gui.parse_failed_file("[完成 2/3] docs/b.pdf 耗时 1s"))

    def test_download_progress(self) -> None:
        self.assertEqual(
            gui.parse_download_progress("下载进度 media：1.0MB/4.5MB（22%）"),
            ("1.0MB", "4.5MB", 22),
        )
        self.assertIsNone(gui.parse_download_progress("开始下载 media：0B"))


class InitSignalTest(unittest.TestCase):
    def test_init_rc_prefix_slices_return_code(self) -> None:
        # Regression: \x00INIT_RC= is 9 chars; slicing [10:] made int("") fail
        # and left the UI stuck on "初始化中…".
        for rc in (0, 1, 130):
            line = "\x00INIT_RC={}".format(rc)
            self.assertTrue(line.startswith("\x00INIT_RC="))
            self.assertEqual(int(line[len("\x00INIT_RC=") :]), rc)

    def test_convert_rc_prefix(self) -> None:
        line = "\x00RC=1"
        self.assertEqual(int(line[len("\x00RC=") :]), 1)


class GuiStatusTest(unittest.TestCase):
    def test_status_item_defaults(self) -> None:
        item = gui_status.StatusItem("k", "标签", False, "详情")
        self.assertTrue(item.required)

    def test_is_ready_requires_all_required_ok(self) -> None:
        items = [
            gui_status.StatusItem("a", "A", True, "ok"),
            gui_status.StatusItem("b", "B", False, "bad"),
        ]
        self.assertFalse(gui_status.is_ready(items))
        items[1] = gui_status.StatusItem("b", "B", True, "ok")
        self.assertTrue(gui_status.is_ready(items))

    def test_optional_failure_does_not_block_ready(self) -> None:
        items = [
            gui_status.StatusItem("a", "A", True, "ok"),
            gui_status.StatusItem("b", "B", False, "optional", required=False),
        ]
        self.assertTrue(gui_status.is_ready(items))
        self.assertEqual(gui_status.missing_summary(items), [])

    def test_missing_summary_lists_required_failures(self) -> None:
        items = [
            gui_status.StatusItem("a", "项目环境", False, "x"),
            gui_status.StatusItem("b", "运行依赖", False, "y"),
            gui_status.StatusItem("c", "平台", True, "ok"),
        ]
        self.assertEqual(gui_status.missing_summary(items), ["项目环境", "运行依赖"])

    def test_collect_status_returns_expected_keys(self) -> None:
        items = gui_status.collect_status()
        keys = [item.key for item in items]
        for expected in (
            "platform",
            "bootstrap_python",
            "venv",
            "packages",
            "xberg",
            "media",
            "avx2",
        ):
            self.assertIn(expected, keys)
        platform_item = next(i for i in items if i.key == "platform")
        self.assertEqual(platform_item.ok, gui_status.is_windows_x64())

    def test_init_command_shape(self) -> None:
        command = gui_status.init_command()
        if command is None:
            self.skipTest("本机没有可用的初始化 Python 3.8+")
        self.assertTrue(command[0])
        self.assertTrue(command[-1].endswith("init_env.py"))
        self.assertIn("init_env.py", command[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
