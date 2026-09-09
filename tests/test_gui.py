"""GUI 逻辑测试：进度解析、日志队列、返回码文案（无显示可跑）。
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
        self.assertEqual(self.app._status_var.get(), "就绪")
        self.assertEqual(len(self.app._type_vars), len(all2markdown.FORMAT_GROUPS))
        self.assertTrue(
            all(var.get() for var in self.app._type_vars.values()),
            "类型多选默认全勾选",
        )
        self.assertTrue(self.app._bar is not None)
        self.assertTrue(self.app._log is not None)


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
        self.assertIsNone(gui.selected_extensions(states))

    def test_partial_selection_unions_groups(self) -> None:
        states = {name: False for name in all2markdown.FORMAT_GROUPS}
        states["PDF 文档"] = True
        selected = gui.selected_extensions(states)
        self.assertEqual(selected, {".pdf"})

    def test_nothing_checked_gives_empty_set(self) -> None:
        states = {name: False for name in all2markdown.FORMAT_GROUPS}
        self.assertEqual(gui.selected_extensions(states), set())

if __name__ == "__main__":
    unittest.main(verbosity=2)
