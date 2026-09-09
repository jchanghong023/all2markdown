"""all2markdown 图形界面（标准库 tkinter，离线可用）。

功能：
* 选择任意输入目录（任意盘符/文件夹），输出固定为项目默认 ``output``。
* 输出一律平铺（``--flat``）：忽略输入子目录层级，同名文件只转换一次，
  已有对应 Markdown 直接跳过，无需状态文件。
* 后台线程执行转换，主线程用队列回灌全部控制台日志并解析进度条。

本模块 import 时不创建任何 Tk 对象，可在无显示环境下导入做逻辑测试。
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import all2markdown

# 与控制台一致的日志格式，界面日志区逐行镜像。
LOG_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# 进度解析：核心日志原文（见 all2markdown_core.main）。
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*个待转换文件")
_DONE_RE = re.compile(r"(?:\[完成|进度)\s*(\d+)/(\d+)")

# 日志区上限（行），防止长批次无界增长。
MAX_LOG_LINES = 5000
# 每次轮询最多回灌行数，保证界面不卡顿。
DRAIN_PER_TICK = 200


def parse_progress(line: str) -> tuple[str, int, int] | None:
    """从一行核心日志解析进度事件。

    返回 ``("total", N, N)``（得知总数）或 ``("done", 已完成, 总数)``，
    无关行返回 None。
    """
    match = _TOTAL_RE.search(line)
    if match:
        total = int(match.group(1))
        return ("total", total, total)
    match = _DONE_RE.search(line)
    if match:
        return ("done", int(match.group(1)), int(match.group(2)))
    return None


class TeeStderr:
    """stderr 分流：界面队列 + 控制台原样 + run 日志文件。

    核心的预检/用法错误走 ``print(..., file=sys.stderr)``，不经过 logging；
    没有这一层，它们既进不了界面日志区，在 pythonw 下还直接不可见。
    空行不入队，避免日志区出现无意义空行。
    """

    def __init__(
        self,
        put: "callable[[str], None]",
        console: "object | None",
        fh: "object | None",
    ) -> None:
        self._put = put
        self._console = console
        self._fh = fh

    def write(self, s: str) -> int:
        for target in (self._console, self._fh):
            if target is not None:
                try:
                    target.write(s)
                except Exception:  # noqa: BLE001 - 控制台/文件坏了不影响界面
                    pass
        try:
            for line in s.splitlines():
                if line.strip():
                    self._put(line)
        except Exception:  # noqa: BLE001 - 队列满了就丢，转换本身不受影响
            pass
        return len(s)

    def flush(self) -> None:
        for target in (self._console, self._fh):
            if target is not None:
                try:
                    target.flush()
                except Exception:  # noqa: BLE001
                    pass


def gui_log_dir() -> Path:
    """界面 run 日志与 Xberg 服务日志所在的 repo 临时目录（gitignored）。"""
    d = all2markdown.REPO_ROOT / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def gui_run_log_path() -> Path:
    """本次转换的完整日志文件路径（纯函数，可单测文件名规则）。"""
    return gui_log_dir() / time.strftime("gui-%Y%m%d-%H%M%S.log")


_RC_HINTS = {
    all2markdown.EXIT_USAGE: "用法错误（输入目录/配置/参数）",
    all2markdown.EXIT_PREFLIGHT: "预检失败（运行环境或离线资产）",
    all2markdown.EXIT_SERVER: "Xberg 服务启动失败",
    all2markdown.EXIT_UNEXPECTED: "未预期错误",
}


def describe_returncode(rc: int) -> str:
    """把转换返回码翻译为面向用户的状态文案（含已知码中文解释）。"""
    if rc == all2markdown.EXIT_OK:
        return "转换完成：全部成功"
    if rc == all2markdown.EXIT_PARTIAL:
        return "转换结束：部分文件失败，详见日志"
    hint = _RC_HINTS.get(rc, f"返回码 {rc}")
    return f"转换失败（{hint}），详见日志"


def build_conversion_argv(
    input_dir: str, output_dir: str, selected: set[str] | None
) -> list[str]:
    """拼装一次转换的 argv（纯函数，可单测）。

    输出恒平铺；``selected`` 为 None 表示不限类型（全选），否则只处理
    选中的扩展名集合。
    """
    argv = [input_dir, output_dir, "--flat"]
    if selected is not None:
        argv += ["--exts", ",".join(sorted(selected))]
    return argv


def selected_extensions(states: dict[str, bool]) -> set[str] | None:
    """把各分组勾选状态折成扩展名集合；全选/全不选之外全勾返回 None（不限）。"""
    groups = all2markdown.FORMAT_GROUPS
    if all(states.get(name, False) for name in groups):
        return None
    picked: set[str] = set()
    for name, exts in groups.items():
        if states.get(name, False):
            picked.update(exts)
    return picked


class App(tk.Tk):
    """主窗口：目录选择 + 开始按钮 + 进度条 + 日志区。"""


    def __init__(self) -> None:
        super().__init__()
        self.title("all2markdown 图形界面")
        self.minsize(760, 540)
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._total = 0
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # -- 布局 ----------------------------------------------------------
    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="输入目录：").grid(row=0, column=0, sticky=tk.W)
        self._input_var = tk.StringVar()
        self._input_entry = ttk.Entry(root, textvariable=self._input_var)
        self._input_entry.grid(row=0, column=1, sticky=tk.EW, padx=(0, 8))
        self._browse_btn = ttk.Button(root, text="浏览…", command=self._browse)
        self._browse_btn.grid(row=0, column=2)

        hint = "任意盘符/文件夹均可；输出自动平铺，同名文件只转换一次"
        ttk.Label(root, text=hint, foreground="gray").grid(
            row=1, column=1, columnspan=2, sticky=tk.W, pady=(2, 6)
        )

        ttk.Label(root, text="输出目录：").grid(row=2, column=0, sticky=tk.W)
        ttk.Label(root, text="output（项目默认，不可更改）").grid(
            row=2, column=1, sticky=tk.W
        )

        type_frame = ttk.LabelFrame(root, text="本次处理的文件类型", padding=8)
        type_frame.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        self._type_vars: dict[str, tk.BooleanVar] = {}
        self._type_boxes: list[ttk.Checkbutton] = []
        for col, (name, exts) in enumerate(all2markdown.FORMAT_GROUPS.items()):
            var = tk.BooleanVar(value=True)
            self._type_vars[name] = var
            label = f"{name}（{len(exts)}种：{', '.join(exts[:4])}…)"
            box = ttk.Checkbutton(type_frame, text=label, variable=var)
            box.grid(row=0, column=col, sticky=tk.W, padx=(0, 12))
            self._type_boxes.append(box)
        sel_row = ttk.Frame(type_frame)
        sel_row.grid(row=1, column=0, columnspan=5, sticky=tk.W, pady=(4, 0))
        ttk.Button(sel_row, text="全选", command=lambda: self._set_all_types(True)).pack(
            side=tk.LEFT
        )
        ttk.Button(
            sel_row, text="全不选", command=lambda: self._set_all_types(False)
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(sel_row, text="只递归处理勾选类型；output 中已有 Markdown 不重复处理",
                  foreground="gray").pack(side=tk.LEFT, padx=(12, 0))

        btn_row = ttk.Frame(root)
        btn_row.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(10, 6))
        self._start_btn = ttk.Button(
            btn_row, text="开始转换", command=self._start, style="Accent.TButton"
        )
        self._start_btn.pack(side=tk.LEFT)
        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(btn_row, textvariable=self._status_var).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        self._bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self._bar.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self._log = scrolledtext.ScrolledText(
            root, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 9),
            background="white",
        )
        self._log.grid(row=7, column=0, columnspan=3, sticky=tk.NSEW)
        root.rowconfigure(7, weight=1)

    def _set_all_types(self, value: bool) -> None:
        for var in self._type_vars.values():
            var.set(value)

    # -- 交互 ----------------------------------------------------------
    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择输入目录")
        if chosen:
            self._input_var.set(chosen)
            self._set_status(f"已选择：{chosen}")

    def _set_status(self, text: str) -> None:
        self._status_var.set(text)

    def _set_running(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        self._input_entry.configure(state=state)
        self._browse_btn.configure(state=state)
        self._start_btn.configure(state=state)
        for box in self._type_boxes:
            box.configure(state=state)
        self._start_btn.configure(text="转换中…" if running else "开始转换")

    def _start(self) -> None:
        input_dir = self._input_var.get().strip().strip('"')
        if not input_dir or not Path(input_dir).is_dir():
            messagebox.showwarning("输入目录无效", "请先选择一个存在的输入目录。")
            return
        states = {name: var.get() for name, var in self._type_vars.items()}
        selected = selected_extensions(states)
        if selected is not None and not selected:
            messagebox.showwarning("未选择类型", "请至少勾选一种要处理的文件类型。")
            return
        argv = build_conversion_argv(input_dir, "output", selected)
        log_path = gui_run_log_path()
        try:
            fh = open(log_path, "a", encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("日志文件打不开", f"无法写入 {log_path}：{exc}")
            return
        header = [f"输入目录：{input_dir}"]
        if selected is None:
            header.append("文件类型：全部（输出 output，平铺去重）")
        else:
            header.append(
                "文件类型：{}（输出 output，平铺去重）".format(", ".join(sorted(selected)))
            )
        for line in header:
            self._append_log(line)
            try:
                fh.write(line + "\n")
            except Exception:  # noqa: BLE001
                pass
        try:
            fh.flush()
        except Exception:  # noqa: BLE001
            pass
        self._total = 0
        self._bar.configure(mode="determinate", value=0)
        self._set_status("转换中…")
        self._set_running(True)
        self._worker = threading.Thread(
            target=self._run,
            args=(argv, fh, log_path),
            daemon=True,
        )
        self._worker.start()
        self.after(100, self._drain)

    def _run(self, argv: list[str], fh: object, log_path: Path) -> None:
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        # pythonw 下 sys.stderr 为 None；__stderr__ 同理，Tee 内部各自判空。
        tee = TeeStderr(self._log_queue.put, sys.__stderr__, fh)
        try:
            with contextlib.redirect_stderr(tee):
                rc = all2markdown.main(argv)
        except SystemExit as exc:
            # 预检失败等直接 raise SystemExit：必须转成返回码进面板，
            # 否则哨兵丢失、界面永远卡在“转换中…”。
            code = exc.code
            rc = code if isinstance(code, int) else all2markdown.EXIT_UNEXPECTED
            self._log_queue.put(f"预检/启动失败（返回码 {rc}），原因见上方日志")
        except Exception as exc:  # noqa: BLE001 - 异常也要落盘到界面
            logging.getLogger().exception("转换异常：%s", exc)
            rc = all2markdown.EXIT_UNEXPECTED
        finally:
            # main() 内的 basicConfig 会往 root 加 handler；清掉本次新增的，
            # 下次转换行为与本次完全一致（配置/级别都不残留）。
            for handler in list(root.handlers):
                if handler not in handlers_before:
                    try:
                        root.removeHandler(handler)
                        handler.close()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                fh.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        self._log_queue.put("\u0000RC={}".format(rc))

    # -- 日志回灌与进度 ------------------------------------------------
    def _drain(self) -> None:
        try:
            self._drain_once()
        except Exception as exc:  # noqa: BLE001 - 泵永不断链，异常直接进面板
            self._append_log(f"GUI 日志泵异常：{exc!r}（转换本身不受影响）")
        if self._worker is not None:
            self.after(100, self._drain)

    def _drain_once(self) -> None:
        for _ in range(DRAIN_PER_TICK):
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                return
            if line.startswith("\u0000RC="):
                self._finish(int(line[4:]))
                return
            self._append_log(line)
            event = parse_progress(line)
            if event is not None:
                kind, first, second = event
                if kind == "total":
                    self._total = first
                else:
                    self._total = second or self._total
                    if self._total > 0:
                        self._bar["value"] = first / self._total * 100
                        self._set_status(f"转换中 {first}/{self._total}")

    def _finish(self, rc: int) -> None:
        if rc == all2markdown.EXIT_OK:
            self._bar["value"] = 100
        result = describe_returncode(rc)
        self._set_status(result)
        self._append_log(result)
        self._set_running(False)
        self._worker = None

    def _append_log(self, line: str) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, line.rstrip("\n") + "\n")
        extra = int(self._log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self._log.delete("1.0", "{}.0".format(extra + 1))
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)


def main() -> int:
    """启动图形界面。"""
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
