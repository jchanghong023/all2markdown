"""all2markdown 图形界面（标准库 tkinter，离线可用）。

克隆后双击 gui.cmd 即可：自动检测环境 → 未就绪可一键初始化 → 就绪后选择目录转换。
未初始化时用系统 Python 启动；就绪后由 gui.cmd 优先使用项目 .venv。

本模块 import 时不创建任何 Tk 对象，可在无显示环境下导入做逻辑测试。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

_SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SRC_DIR.parent
for _path in (str(REPO_ROOT), str(_SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import gui_status  # noqa: E402

# 与控制台一致的日志格式，界面日志区逐行镜像。
LOG_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# 进度解析：核心日志原文（见 all2markdown_core.main）。
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*个待转换文件")
_DONE_RE = re.compile(r"(?:\[完成|进度)\s*(\d+)/(\d+)")
_START_FILE_RE = re.compile(r"\[开始\s*([^\]]+)\]\s*(.+)$")
_FAIL_RE = re.compile(r"\[失败\s*([^\]]+)\]\s*(.+)$")
_DL_RE = re.compile(r"下载进度.*：([0-9.]+[KMGT]?B)/([0-9.]+[KMGT]?B)（(\d+)%）")

# 日志区上限（行），防止长批次无界增长。
MAX_LOG_LINES = 5000
# 每次轮询最多回灌行数，保证界面不卡顿。
DRAIN_PER_TICK = 200

STATUS_OK = "#1b7f3b"
STATUS_BAD = "#b42318"
STATUS_WARN = "#9a6700"
STATUS_MUTED = "#57606a"
ACCENT = "#0969da"
BG = "#f6f8fa"
CARD_BG = "#ffffff"


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


def parse_current_file(line: str) -> str | None:
    """从 ``[开始 tag] path`` 解析当前文件名。"""
    match = _START_FILE_RE.search(line)
    if not match:
        return None
    return match.group(2).strip()


def parse_failed_file(line: str) -> str | None:
    """从 ``[失败 tag] path`` 解析失败文件。"""
    match = _FAIL_RE.search(line)
    if not match:
        return None
    return match.group(2).split(":", 1)[0].strip()


def parse_download_progress(line: str) -> tuple[str, str, int] | None:
    """解析初始化下载进度行 → (已下, 总量, 百分比)。"""
    match = _DL_RE.search(line)
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


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
    d = REPO_ROOT / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def gui_run_log_path() -> Path:
    """本次转换/初始化的完整日志文件路径（纯函数，可单测文件名规则）。"""
    return gui_log_dir() / time.strftime("gui-%Y%m%d-%H%M%S.log")


def describe_returncode(rc: int) -> str:
    """把转换返回码翻译为面向用户的状态文案（含已知码中文解释）。"""
    try:
        import all2markdown
    except Exception:
        all2markdown = None  # type: ignore

    if all2markdown is not None:
        if rc == all2markdown.EXIT_OK:
            return "转换完成：全部成功"
        if rc == all2markdown.EXIT_PARTIAL:
            return "转换结束：部分文件失败，详见日志"
        if rc == all2markdown.EXIT_CANCELLED:
            return "已手动停止：保留已完成的结果"
        hints = {
            all2markdown.EXIT_USAGE: "用法错误（输入目录/配置/参数）",
            all2markdown.EXIT_PREFLIGHT: "预检失败（运行环境或离线资产）",
            all2markdown.EXIT_SERVER: "Xberg 服务启动失败",
            all2markdown.EXIT_UNEXPECTED: "未预期错误",
        }
        hint = hints.get(rc, f"返回码 {rc}")
        return f"转换失败（{hint}），详见日志"
    if rc == 0:
        return "转换完成：全部成功"
    return f"转换失败（返回码 {rc}），详见日志"


def resolve_initial_dir(argv: list[str] | None) -> str:
    """取启动参数中的初始输入目录（纯函数，无显示可测）。"""
    for arg in argv or []:
        if not arg.startswith("-") and arg.strip():
            return arg
    return ""


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


def selected_extensions(
    states: dict[str, bool], groups: dict[str, list[str]]
) -> set[str] | None:
    """把各分组勾选状态折成扩展名集合；全选/全不选之外全勾返回 None（不限）。"""
    if all(states.get(name, False) for name in groups):
        return None
    picked: set[str] = set()
    for name, exts in groups.items():
        if states.get(name, False):
            picked.update(exts)
    return picked


def _load_all2markdown():
    import all2markdown

    return all2markdown


class App(tk.Tk):
    """主窗口：环境状态 + 一键初始化 + 目录选择 + 转换。"""

    def __init__(self, input_dir: str = "") -> None:
        super().__init__()
        self._initial_dir = input_dir
        self.title("all2markdown")
        self.minsize(860, 640)
        self.geometry("920x720")
        self.configure(bg=BG)
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._total = 0
        self._status_items: list[gui_status.StatusItem] = []
        self._ready = False
        self._busy = False
        self._pending_relaunch = False
        self._init_started_at: float | None = None
        self._last_log_text = ""
        self._init_process: subprocess.Popen | None = None
        self._current_file = ""
        self._download_progress = ""
        self._failed_files: list[str] = []
        self._type_vars: dict[str, tk.BooleanVar] = {}
        self._type_boxes: list[ttk.Checkbutton] = []
        self._format_groups: dict[str, list[str]] = {}
        self._setup_style()
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_status_async(initial=True)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure(
            "Card.TLabel", background=CARD_BG, font=("Segoe UI", 10)
        )
        style.configure(
            "CardTitle.TLabel",
            background=CARD_BG,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure("Muted.TLabel", foreground=STATUS_MUTED, background=BG)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.map(
            "Accent.TButton",
            foreground=[("!disabled", "#ffffff"), ("disabled", STATUS_MUTED)],
            background=[("!disabled", ACCENT), ("disabled", "#d0d7de")],
        )

    # -- 布局 ----------------------------------------------------------
    def _build_widgets(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # 环境状态卡片
        status_card = tk.Frame(outer, bg=CARD_BG, highlightbackground="#d0d7de",
                               highlightthickness=1, padx=14, pady=12)
        status_card.grid(row=0, column=0, sticky=tk.EW, pady=(0, 10))
        status_card.columnconfigure(0, weight=1)

        header = tk.Frame(status_card, bg=CARD_BG)
        header.grid(row=0, column=0, sticky=tk.EW)
        header.columnconfigure(0, weight=1)
        tk.Label(
            header, text="环境状态", font=("Segoe UI", 12, "bold"),
            bg=CARD_BG, fg="#24292f",
        ).grid(row=0, column=0, sticky=tk.W)
        self._recheck_btn = ttk.Button(
            header, text="重新检测", command=self._refresh_status_async, width=10
        )
        self._recheck_btn.grid(row=0, column=1, sticky=tk.E)

        self._status_body = tk.Frame(status_card, bg=CARD_BG)
        self._status_body.grid(row=1, column=0, sticky=tk.EW, pady=(8, 0))
        self._status_body.columnconfigure(1, weight=1)

        self._ready_banner = tk.Label(
            status_card, text="正在检测环境…", anchor="w", justify=tk.LEFT,
            bg=CARD_BG, fg=STATUS_MUTED, font=("Segoe UI", 10),
        )
        self._ready_banner.grid(row=2, column=0, sticky=tk.EW, pady=(10, 0))

        action_row = tk.Frame(status_card, bg=CARD_BG)
        action_row.grid(row=3, column=0, sticky=tk.EW, pady=(10, 0))
        self._init_btn = tk.Button(
            action_row, text="一键初始化", command=self._start_init,
            bg=ACCENT, fg="#ffffff", activebackground="#0550ae",
            activeforeground="#ffffff", relief=tk.FLAT, padx=12, pady=4,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        self._init_btn.pack(side=tk.LEFT)
        self._relaunch_btn = ttk.Button(
            action_row, text="用项目环境重新打开",
            command=self._relaunch_with_venv,
        )
        self._relaunch_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._init_hint = tk.Label(
            action_row, text="初始化需要联网下载运行时与模型",
            bg=CARD_BG, fg=STATUS_MUTED, font=("Segoe UI", 9),
        )
        self._init_hint.pack(side=tk.LEFT, padx=(12, 0))

        # 转换区域
        convert_card = tk.Frame(outer, bg=CARD_BG, highlightbackground="#d0d7de",
                                highlightthickness=1, padx=14, pady=12)
        convert_card.grid(row=1, column=0, sticky=tk.EW, pady=(0, 10))
        convert_card.columnconfigure(1, weight=1)
        self._convert_card = convert_card

        tk.Label(
            convert_card, text="转换", font=("Segoe UI", 12, "bold"),
            bg=CARD_BG, fg="#24292f",
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))

        tk.Label(convert_card, text="输入目录", bg=CARD_BG).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 8)
        )
        self._input_var = tk.StringVar(value=self._initial_dir)
        self._input_entry = ttk.Entry(convert_card, textvariable=self._input_var)
        self._input_entry.grid(row=1, column=1, sticky=tk.EW, padx=(0, 8))
        self._browse_btn = ttk.Button(
            convert_card, text="浏览…", command=self._browse, width=10
        )
        self._browse_btn.grid(row=1, column=2)

        tk.Label(
            convert_card,
            text="输出目录：output（项目默认）。递归处理子目录；已有 Markdown 自动跳过。",
            bg=CARD_BG, fg=STATUS_MUTED, font=("Segoe UI", 9),
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(4, 8))

        self._type_frame = tk.Frame(convert_card, bg=CARD_BG)
        self._type_frame.grid(row=3, column=0, columnspan=3, sticky=tk.EW)
        self._type_boxes_frame = tk.Frame(self._type_frame, bg=CARD_BG)
        self._type_boxes_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sel_row = tk.Frame(self._type_frame, bg=CARD_BG)
        sel_row.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(sel_row, text="全选", command=lambda: self._set_all_types(True)).pack(
            side=tk.LEFT
        )
        ttk.Button(
            sel_row, text="全不选", command=lambda: self._set_all_types(False)
        ).pack(side=tk.LEFT, padx=(6, 0))

        btn_row = tk.Frame(convert_card, bg=CARD_BG)
        btn_row.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(12, 4))
        self._start_btn = tk.Button(
            btn_row, text="开始转换", command=self._start,
            bg=ACCENT, fg="#ffffff", activebackground="#0550ae",
            activeforeground="#ffffff", relief=tk.FLAT, padx=16, pady=4,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(
            btn_row, text="停止", command=self._stop, state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._open_out_btn = ttk.Button(
            btn_row, text="打开输出目录", command=self._open_output_dir
        )
        self._open_out_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._status_var = tk.StringVar(value="就绪")
        tk.Label(
            btn_row, textvariable=self._status_var, bg=CARD_BG, fg=STATUS_MUTED
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._bar = ttk.Progressbar(convert_card, mode="determinate", maximum=100)
        self._bar.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(4, 0))

        # 日志
        log_card = tk.Frame(outer, bg=CARD_BG, highlightbackground="#d0d7de",
                            highlightthickness=1, padx=8, pady=8)
        log_card.grid(row=3, column=0, sticky=tk.NSEW)
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        tk.Label(
            log_card, text="日志", font=("Segoe UI", 11, "bold"),
            bg=CARD_BG, fg="#24292f",
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 4))
        self._log = scrolledtext.ScrolledText(
            log_card, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 9),
            background="#fbfcfd",
        )
        self._log.grid(row=1, column=0, sticky=tk.NSEW)

        self._worker = None
        self.after(100, self._pump)

    def _build_type_checkboxes(self) -> None:
        for child in self._type_boxes_frame.winfo_children():
            child.destroy()
        self._type_vars.clear()
        self._type_boxes.clear()
        groups = self._format_groups
        if not groups:
            ttk.Label(
                self._type_boxes_frame,
                text="环境就绪后显示可选文件类型",
            ).pack(anchor=tk.W)
            return
        row = tk.Frame(self._type_boxes_frame, bg=CARD_BG)
        row.pack(anchor=tk.W)
        for name, exts in groups.items():
            var = tk.BooleanVar(value=True)
            self._type_vars[name] = var
            label = f"{name}（{len(exts)}种）"
            box = ttk.Checkbutton(row, text=label, variable=var)
            box.pack(side=tk.LEFT, padx=(0, 10))
            self._type_boxes.append(box)

    def _set_all_types(self, value: bool) -> None:
        for var in self._type_vars.values():
            var.set(value)

    # -- 状态 ----------------------------------------------------------
    def _refresh_status_async(self, initial: bool = False) -> None:
        if self._busy:
            return
        self._recheck_btn.configure(state=tk.DISABLED)
        if not initial:
            self._append_log("重新检测环境…")

        def worker() -> None:
            try:
                items = gui_status.collect_status()
                ready = gui_status.is_ready(items)
                missing = gui_status.missing_summary(items)
                on_venv = gui_status.current_interpreter_is_venv()
                groups: dict[str, list[str]] = {}
                if ready and on_venv:
                    try:
                        core = _load_all2markdown()
                        groups = dict(core.FORMAT_GROUPS)
                    except Exception as exc:  # noqa: BLE001
                        ready = False
                        missing = missing or ["运行时加载失败"]
                        items = list(items) + [
                            gui_status.StatusItem(
                                "runtime_import", "运行时加载", False, str(exc)
                            )
                        ]
                payload = {
                    "items": [
                        {
                            "key": item.key,
                            "label": item.label,
                            "ok": item.ok,
                            "detail": item.detail,
                            "required": item.required,
                        }
                        for item in items
                    ],
                    "ready": ready,
                    "missing": missing,
                    "on_venv": on_venv,
                    "groups": groups,
                }
                self._log_queue.put("\x01STATUS" + json.dumps(payload, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                self._log_queue.put(f"环境检测失败：{exc!r}")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, payload: dict) -> None:
        self._status_items = [
            gui_status.StatusItem(
                key=item["key"],
                label=item["label"],
                ok=bool(item["ok"]),
                detail=item["detail"],
                required=bool(item.get("required", True)),
            )
            for item in payload["items"]
        ]
        self._ready = bool(payload["ready"])
        self._format_groups = payload.get("groups") or {}
        for child in self._status_body.winfo_children():
            child.destroy()
        for i, item in enumerate(self._status_items):
            mark = "✓" if item.ok else "✗"
            color = STATUS_OK if item.ok else STATUS_BAD
            if not item.required and not item.ok:
                color = STATUS_WARN
                mark = "!"
            tk.Label(
                self._status_body, text=mark, width=2, anchor="w",
                bg=CARD_BG, fg=color, font=("Segoe UI", 10, "bold"),
            ).grid(row=i, column=0, sticky=tk.W)
            tk.Label(
                self._status_body, text=item.label, width=14, anchor="w",
                bg=CARD_BG, fg="#24292f", font=("Segoe UI", 10, "bold"),
            ).grid(row=i, column=1, sticky=tk.W, padx=(2, 8))
            tk.Label(
                self._status_body, text=item.detail, anchor="w", justify=tk.LEFT,
                bg=CARD_BG, fg=STATUS_MUTED, font=("Segoe UI", 9),
            ).grid(row=i, column=2, sticky=tk.W)

        missing = payload["missing"]
        on_venv = payload["on_venv"]
        self._build_type_checkboxes()
        if self._ready and on_venv:
            self._ready_banner.configure(
                text="环境已就绪，可以开始转换。", fg=STATUS_OK
            )
            self._init_btn.pack_forget()
            self._relaunch_btn.pack_forget()
            self._init_hint.pack_forget()
            self._set_convert_enabled(True)
        elif self._ready and not on_venv:
            self._ready_banner.configure(
                text="环境已就绪，但当前不是项目 Python。请点「用项目环境重新打开」。",
                fg=STATUS_WARN,
            )
            self._init_btn.pack_forget()
            self._relaunch_btn.pack(side=tk.LEFT)
            self._init_hint.configure(text="转换必须在 .venv 的 Python 3.12 中运行")
            self._init_hint.pack(side=tk.LEFT, padx=(12, 0))
            self._set_convert_enabled(False)
        else:
            if missing:
                text = "尚未就绪，缺少：{}。请点击「一键初始化」。".format("、".join(missing))
            else:
                text = "尚未就绪，请点击「一键初始化」。"
            self._ready_banner.configure(text=text, fg=STATUS_BAD)
            self._relaunch_btn.pack_forget()
            self._init_btn.pack(side=tk.LEFT)
            self._init_hint.configure(text="初始化需要联网下载运行时与模型（可重复执行）")
            self._init_hint.pack(side=tk.LEFT, padx=(12, 0))
            self._set_convert_enabled(False)
        self._recheck_btn.configure(state=tk.NORMAL)
        if not self._ready:
            self._set_status("请先完成初始化")
        elif self._pending_relaunch and not gui_status.current_interpreter_is_venv():
            self._pending_relaunch = False
            self._append_log("初始化完成，正在用项目环境重新打开界面…")
            self.after(200, self._relaunch_with_venv)

    def _set_accent_enabled(self, button: tk.Button, enabled: bool, text: str) -> None:
        if enabled:
            button.configure(
                state=tk.NORMAL, text=text, bg=ACCENT, fg="#ffffff",
                activebackground="#0550ae", activeforeground="#ffffff",
                disabledforeground="#8c959f",
            )
        else:
            button.configure(
                state=tk.DISABLED, text=text, bg="#d0d7de", fg="#8c959f",
                activebackground="#d0d7de", activeforeground="#8c959f",
                disabledforeground="#8c959f",
            )

    def _set_convert_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self._input_entry.configure(state=state)
        self._browse_btn.configure(state=state)
        self._set_accent_enabled(self._start_btn, enabled, "开始转换")
        for box in self._type_boxes:
            box.configure(state=state)
        if not self._busy:
            self._stop_btn.configure(state=tk.DISABLED, text="停止")

    # -- 交互 ----------------------------------------------------------
    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择输入目录")
        if chosen:
            self._input_var.set(chosen)
            self._set_status(f"已选择：{chosen}")

    def _set_status(self, text: str) -> None:
        self._status_var.set(text)

    def _set_running(self, running: bool) -> None:
        self._busy = running
        if running:
            self._input_entry.configure(state=tk.DISABLED)
            self._browse_btn.configure(state=tk.DISABLED)
            self._set_accent_enabled(self._start_btn, False, "转换中…")
            self._set_accent_enabled(self._init_btn, False, "一键初始化")
            self._recheck_btn.configure(state=tk.DISABLED)
            for box in self._type_boxes:
                box.configure(state=tk.DISABLED)
            self._stop_btn.configure(state=tk.NORMAL, text="停止")
        else:
            self._set_accent_enabled(self._init_btn, True, "一键初始化")
            self._recheck_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED, text="停止")
            self._set_convert_enabled(self._ready and gui_status.current_interpreter_is_venv())

    def _stop(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            return
        if self._init_process is not None:
            self._cancel_init()
            return
        try:
            core = _load_all2markdown()
            core.request_cancel()
        except Exception:
            pass
        self._set_status("正在停止（当前文件完成后停）…")
        self._stop_btn.configure(state=tk.DISABLED, text="停止中…")

    def _cancel_init(self) -> None:
        process = self._init_process
        if process is None:
            return
        self._append_log("正在取消初始化（已下载的校验通过资产会保留）…")
        self._set_status("正在取消初始化…")
        self._stop_btn.configure(state=tk.DISABLED, text="取消中…")
        try:
            process.terminate()
        except Exception:
            pass

        def force_kill() -> None:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass

        threading.Thread(target=force_kill, daemon=True).start()

    def _open_output_dir(self) -> None:
        out = REPO_ROOT / "output"
        try:
            out.mkdir(parents=True, exist_ok=True)
            os.startfile(str(out))  # noqa: S606 - open Explorer for user
        except OSError as exc:
            messagebox.showwarning("无法打开输出目录", str(exc))

    def _start_init(self) -> None:
        if self._busy:
            return
        command = gui_status.init_command()
        if command is None:
            messagebox.showerror(
                "无法初始化",
                "未找到可用于初始化的 Python 3.8+。请先安装 Python 后重试。",
            )
            return
        if not messagebox.askyesno(
            "开始初始化",
            "将下载并安装运行时、Python 依赖与离线模型。\n"
            "需要网络，可能需要较长时间。是否继续？",
        ):
            return
        log_path = gui_run_log_path()
        try:
            fh = open(log_path, "a", encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("日志文件打不开", f"无法写入 {log_path}：{exc}")
            return
        header = f"开始初始化：{' '.join(command)}"
        self._append_log(header)
        try:
            fh.write(header + "\n")
            fh.flush()
        except Exception:
            pass
        self._bar.configure(mode="indeterminate")
        self._bar.start(12)
        self._init_started_at = time.monotonic()
        self._download_progress = ""
        self._current_file = ""
        self._set_status("初始化中…")
        self._busy = True
        self._set_accent_enabled(self._init_btn, False, "初始化中…")
        self._recheck_btn.configure(state=tk.DISABLED)
        self._set_accent_enabled(self._start_btn, False, "开始转换")
        self._stop_btn.configure(state=tk.NORMAL, text="取消初始化")
        self._worker = threading.Thread(
            target=self._run_init,
            args=(command, fh),
            daemon=True,
        )
        self._worker.start()

    def _run_init(self, command: list[str], fh) -> None:
        rc = 1
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Unbuffered so download progress reaches the GUI in real time.
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
            )
            self._init_process = process
            assert process.stdout is not None
            for line in process.stdout:
                text = line.rstrip("\n")
                if text.strip():
                    self._log_queue.put(text)
                    try:
                        fh.write(text + "\n")
                        fh.flush()
                    except Exception:
                        pass
            rc = process.wait()
        except Exception as exc:  # noqa: BLE001
            self._log_queue.put(f"初始化进程异常：{exc!r}")
            rc = 1
        finally:
            self._init_process = None
            try:
                fh.close()
            except Exception:
                pass
        if process is not None and process.returncode in (1, 15) and self._init_started_at is not None:
            # terminated via cancel — keep rc as-is; UI maps non-zero to failed/cancelled
            pass
        self._log_queue.put(f"\x00INIT_RC={rc}")

    def _finish_init(self, rc: int) -> None:
        self._bar.stop()
        self._bar.configure(mode="determinate", value=0)
        self._busy = False
        self._worker = None
        self._init_process = None
        self._init_started_at = None
        self._download_progress = ""
        self._set_accent_enabled(self._init_btn, True, "一键初始化")
        self._stop_btn.configure(state=tk.DISABLED, text="停止")
        if rc == 0:
            self._append_log("初始化完成，正在刷新环境状态…")
            self._set_status("初始化完成")
            self._pending_relaunch = True
            self._refresh_status_async()
        else:
            self._pending_relaunch = False
            self._append_log(
                f"初始化未完成（返回码 {rc}）。已下载且校验通过的资产会保留，可稍后重试。"
            )
            self._set_status("初始化未完成")
            self._refresh_status_async()

    def _relaunch_with_venv(self) -> None:
        pythonw = gui_status.venv_pythonw_path()
        python = gui_status.venv_python_path()
        target = pythonw if pythonw.is_file() else python
        if not target.is_file():
            messagebox.showwarning("环境不可用", "未找到 .venv，请先完成初始化。")
            return
        try:
            subprocess.Popen([str(target), str(REPO_ROOT / "gui.py")], cwd=str(REPO_ROOT))
        except OSError as exc:
            messagebox.showerror("无法重新打开", str(exc))
            return
        self.destroy()

    def _start(self) -> None:
        if not self._ready:
            messagebox.showwarning("尚未就绪", "请先完成初始化。")
            return
        if not gui_status.current_interpreter_is_venv():
            self._relaunch_with_venv()
            return
        core = _load_all2markdown()
        input_dir = self._input_var.get().strip().strip('"')
        if not input_dir or not Path(input_dir).is_dir():
            messagebox.showwarning("输入目录无效", "请先选择一个存在的输入目录。")
            return
        states = {name: var.get() for name, var in self._type_vars.items()}
        selected = selected_extensions(states, self._format_groups)
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
            except Exception:
                pass
        try:
            fh.flush()
        except Exception:
            pass
        self._total = 0
        self._current_file = ""
        self._failed_files = []
        self._download_progress = ""
        self._bar.configure(mode="determinate", value=0)
        self._set_status("转换中…")
        self._set_running(True)
        self._worker = threading.Thread(
            target=self._run,
            args=(argv, fh, log_path),
            daemon=True,
        )
        self._worker.start()

    def _run(self, argv: list[str], fh: object, log_path: Path) -> None:
        core = _load_all2markdown()
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        tee = TeeStderr(self._log_queue.put, sys.__stderr__, fh)
        try:
            with contextlib.redirect_stderr(tee):
                rc = core.main(argv)
        except SystemExit as exc:
            code = exc.code
            rc = code if isinstance(code, int) else core.EXIT_UNEXPECTED
            self._log_queue.put(f"预检/启动失败（返回码 {rc}），原因见上方日志")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger().exception("转换异常：%s", exc)
            rc = core.EXIT_UNEXPECTED
        finally:
            for handler in list(root.handlers):
                if handler not in handlers_before:
                    try:
                        root.removeHandler(handler)
                        handler.close()
                    except Exception:
                        pass
            try:
                fh.close()  # type: ignore[union-attr]
            except Exception:
                pass
        self._log_queue.put("\x00RC={}".format(rc))

    # -- 日志回灌与进度 ------------------------------------------------
    def _pump(self) -> None:
        try:
            self._drain_once()
            self._heartbeat()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"GUI 日志泵异常：{exc!r}（业务本身不受影响）")
        self.after(100, self._pump)

    def _heartbeat(self) -> None:
        """Keep status fresh during long silent downloads (no UI freeze)."""
        if not self._busy:
            return
        if self._init_started_at is not None:
            elapsed = int(time.monotonic() - self._init_started_at)
            parts = [f"初始化中… 已用 {elapsed}s"]
            if self._download_progress:
                parts.append(self._download_progress)
            tail = self._last_log_text.strip()
            if len(tail) > 40:
                tail = tail[:38] + "…"
            if tail and not self._download_progress:
                parts.append(tail)
            self._set_status("｜".join(parts))
            return
        # conversion heartbeat
        if self._current_file:
            name = self._current_file
            if len(name) > 36:
                name = "…" + name[-35:]
            self._set_status(f"正在处理：{name}")

    def _drain_once(self) -> None:
        for _ in range(DRAIN_PER_TICK):
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                return
            if line.startswith("\x01STATUS"):
                payload = json.loads(line[7:])
                self._apply_status(payload)
                continue
            if line.startswith("\x00INIT_RC="):
                try:
                    rc = int(line[len("\x00INIT_RC=") :])
                except ValueError:
                    rc = 1
                    self._append_log(f"初始化完成信号解析失败：{line!r}")
                self._finish_init(rc)
                continue
            if line.startswith("\x00RC="):
                try:
                    rc = int(line[len("\x00RC=") :])
                except ValueError:
                    rc = 11
                    self._append_log(f"转换完成信号解析失败：{line!r}")
                self._finish(rc)
                return
            self._append_log(line)
            self._last_log_text = line
            dl = parse_download_progress(line)
            if dl is not None:
                done_s, total_s, pct = dl
                self._download_progress = f"下载 {done_s}/{total_s}（{pct}%）"
                self._bar.stop()
                self._bar.configure(mode="determinate", maximum=100, value=pct)
            current = parse_current_file(line)
            if current is not None:
                self._current_file = current
            failed = parse_failed_file(line)
            if failed is not None:
                self._failed_files.append(failed)
            event = parse_progress(line)
            if event is not None:
                kind, first, second = event
                if kind == "total":
                    self._total = first
                else:
                    self._total = second or self._total
                    if self._total > 0:
                        self._bar.configure(mode="determinate", maximum=100)
                        self._bar["value"] = first / self._total * 100
                        self._set_status(f"转换中 {first}/{self._total}")

    def _finish(self, rc: int) -> None:
        try:
            core = _load_all2markdown()
            ok_code = core.EXIT_OK
        except Exception:
            ok_code = 0
        if rc == ok_code:
            self._bar["value"] = 100
        result = describe_returncode(rc)
        self._set_status(result)
        self._append_log(result)
        self._current_file = ""
        if self._failed_files:
            self._append_log(f"失败 {len(self._failed_files)} 个文件：")
            for name in self._failed_files[:20]:
                self._append_log(f"  · {name}")
            if len(self._failed_files) > 20:
                self._append_log(f"  … 另有 {len(self._failed_files) - 20} 个，详见日志")
            self._set_status(f"{result}｜失败 {len(self._failed_files)} 个")
        self._set_running(False)
        self._worker = None

    def _append_log(self, line: str) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, line.rstrip("\n") + "\n")
        try:
            extra = int(self._log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
            if extra > 0:
                self._log.delete("1.0", "{}.0".format(extra + 1))
        except (ValueError, tk.TclError):
            pass
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno("确认退出", "仍有任务在进行，确定退出吗？"):
                return
            try:
                core = _load_all2markdown()
                core.request_cancel()
            except Exception:
                pass
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    """启动图形界面；可带一个初始输入目录参数。"""
    if argv is None:
        argv = sys.argv[1:]
    App(resolve_initial_dir(argv)).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
