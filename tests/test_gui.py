"""GUI 逻辑测试：进度解析、日志队列、返回码文案（无显示可跑）。

App 本体冒烟仅在有显示时执行；--flat 端到端在 test_all2markdown 中。
"""

from __future__ import annotations

import logging
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


class QueueHandlerTest(unittest.TestCase):
    def test_formats_and_queues(self) -> None:
        target: queue.Queue[str] = queue.Queue()
        handler = gui.QueueHandler(target)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="你好 %s", args=("世界",), exc_info=None,
        )
        handler.emit(record)
        line = target.get_nowait()
        self.assertIn("你好 世界", line)
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} INFO")


class DescribeReturncodeTest(unittest.TestCase):
    def test_codes(self) -> None:
        self.assertIn("全部成功", gui.describe_returncode(all2markdown.EXIT_OK))
        self.assertIn("部分", gui.describe_returncode(all2markdown.EXIT_PARTIAL))
        self.assertIn("42", gui.describe_returncode(42))



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
