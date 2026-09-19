"""三级测试门驱动：fastcheck / fulltest / slowtest（语义与权限见 AGENTS.md）。

只复用项目既有质量门（compileall + unittest discover，与 CI 本地步骤一致），
不定义新的语言级检查标准。

入口：
  fastcheck.cmd [ --budget 秒 ]        AI 可自主执行，墙钟硬上限 60 秒
  fulltest.cmd  --authorized           当前平台全部本地测试，需人类本次明确授权
  slowtest.cmd  --authorized           fulltest 语义 + 远程 CI，需人类本次明确授权

--authorized 只能来自人类针对本次运行的明确指令原文；AI Agent 不得自行添加
（软约束，依赖 AI 规则与评审把关，见 AGENTS.md「三级测试门」）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

FASTCHECK_BUDGET_SECONDS = 60.0  # 硬上限；--budget 只允许向下调整
FULLTEST_ENV = {
    "PYTHONUTF8": "1",
    "ALL2MARKDOWN_REQUIRE_REAL_CONVERSION": "1",
}
REMOTE_WAIT_SECONDS = 6600  # CI 自身 timeout 90 分钟，等待上限 110 分钟
REMOTE_POLL_SECONDS = 30

# 真实集成用例：启动 Xberg 服务、真实转换、真实 ASR 等，只属于 fulltest/slowtest。
# fastcheck 通过动态发现 + 按 "模块.类名" 排除获得全部轻量用例；新增轻量类自动纳入。
HEAVYWEIGHT_CLASSES = {
    "test_all2markdown.RequiredRealConversionAssetsTest",
    "test_all2markdown.ConvertDocsIntegrationTest",
    "test_all2markdown.LargeDocFastModeIntegrationTest",
    "test_all2markdown.FlatOutputIntegrationTest",
    "test_all2markdown.ExtFilterIntegrationTest",
    "test_all2markdown.CancelIntegrationTest",
    "test_convert_mp4.ConvertMp4IntegrationTest",
}

CI_WORKFLOW = "full-tests.yml"


def _die(code: int, message: str) -> "int":
    print(message)
    return code


def _ensure_venv() -> int | None:
    if not VENV_PY.is_file():
        return _die(3, "尚未初始化（缺少 .venv），请先运行 init.cmd")
    if pathlib.Path(sys.executable).resolve() != VENV_PY.resolve():
        return _die(
            3,
            "测试门必须通过 fastcheck.cmd / fulltest.cmd / slowtest.cmd 用项目 .venv 运行",
        )
    return None


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:  # 防御性兜底；本项目仅支持 Windows
        try:
            import signal

            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _run_step(name: str, cmd: list[str], env: dict[str, str], timeout: float | None) -> tuple[str, int | None, float]:
    """返回 (状态, 退出码或 None, 耗时秒)。状态为 PASS / FAIL / TIMEOUT。"""
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        proc.wait()
        return "TIMEOUT", None, time.monotonic() - started
    return ("PASS" if rc == 0 else "FAIL"), rc, time.monotonic() - started


def _compileall_cmd() -> list[str]:
    return [
        str(VENV_PY),
        "-m",
        "compileall",
        "-q",
        "all2markdown.py",
        "src",
        "tests",
    ]


def _fulltest_stage_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(FULLTEST_ENV)
    return env


def _flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_flatten(item))
        else:
            tests.append(item)
    return tests


def _fast_units_inner() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir="tests", pattern="test_*.py", top_level_dir="tests")
    tests = _flatten(suite)

    def class_key(test: unittest.TestCase) -> str:
        return ".".join(test.id().split(".")[-3:-1])

    kept = [test for test in tests if class_key(test) not in HEAVYWEIGHT_CLASSES]
    excluded = sorted(
        {class_key(test) for test in tests if class_key(test) in HEAVYWEIGHT_CLASSES}
    )
    print(
        f"fastcheck 单元子集：运行 {len(kept)} / {len(tests)} 条用例；"
        f"排除真实集成类 {len(excluded)} 个（由 fulltest 覆盖）：{'、'.join(excluded)}"
    )
    result = unittest.TextTestRunner(verbosity=1).run(
        unittest.TestSuite(kept)
    )
    return 0 if result.wasSuccessful() else 1


def _check_budget(value: float) -> float:
    if value <= 0 or value > FASTCHECK_BUDGET_SECONDS:
        raise SystemExit(
            f"--budget 只允许向下调整且为正数（硬上限 {FASTCHECK_BUDGET_SECONDS:g} 秒），收到 {value:g}"
        )
    return value


def cmd_fastcheck(args: argparse.Namespace) -> int:
    _check_budget(args.budget)
    if (rc := _ensure_venv()) is not None:
        return rc
    budget = args.budget
    start = time.monotonic()
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    steps: list[tuple[str, list[str]]] = [
        ("compileall", _compileall_cmd()),
        (
            "fast-units",
            [str(VENV_PY), str(pathlib.Path(__file__).resolve()), "_fast-units"],
        ),
    ]
    for name, cmd in steps:
        remaining = budget - (time.monotonic() - start)
        if remaining <= 0:
            print(f"fastcheck TIMEOUT（{name} 未启动）：预算 {budget:g}s 已耗尽")
            print(f"fastcheck 总耗时 {time.monotonic() - start:.1f}s")
            return 2
        status, rc, _ = _run_step(name, cmd, env, remaining)
        if status == "TIMEOUT":
            print(f"fastcheck TIMEOUT：{name} 超过剩余预算，进程树已终止")
            print(f"fastcheck 总耗时 {time.monotonic() - start:.1f}s")
            return 2
        if status == "FAIL":
            print(f"fastcheck FAIL：{name} 退出码 {rc}")
            print(f"fastcheck 总耗时 {time.monotonic() - start:.1f}s")
            return 1
    elapsed = time.monotonic() - start
    print(f"fastcheck PASS（预算 {budget:g}s）")
    print(f"fastcheck 总耗时 {elapsed:.1f}s")
    return 0


def _fulltest_stages() -> list[tuple[str, str, int]]:
    """当前平台完整本地验证：compileall + 全量 unittest discover（与 CI 本地步骤一致）。"""
    env = _fulltest_stage_env()
    results: list[tuple[str, str, int]] = []
    for name, cmd in (
        ("compileall", _compileall_cmd()),
        (
            "unittest-discover",
            [
                str(VENV_PY),
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
                "-v",
            ],
        ),
    ):
        status, rc, duration = _run_step(name, cmd, env, None)
        print(f"[fulltest] {name}: {status}，耗时 {duration:.1f}s")
        assert rc is not None  # 无超时，_run_step 必返回退出码
        results.append((name, status, rc))
    return results


def cmd_fulltest(args: argparse.Namespace) -> int:
    if not args.authorized:
        return _die(
            3,
            "fulltest 需要人类针对本次运行明确授权（由人类指令原文附带 --authorized 运行；"
            "AI 不得自行添加）。",
        )
    if (rc := _ensure_venv()) is not None:
        return rc
    start = time.monotonic()
    stages = _fulltest_stages()
    elapsed = time.monotonic() - start
    failed = [name for name, status, _ in stages if status != "PASS"]
    print(f"fulltest 总耗时 {elapsed:.1f}s")
    if failed:
        print(f"fulltest FAIL：{ '、'.join(failed) } 未通过")
        return 1
    print("fulltest PASS")
    return 0


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def _gh_json(args: list[str]) -> list[dict]:
    proc = subprocess.run(
        ["gh", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失败：{proc.stderr.strip()}")
    return json.loads(proc.stdout or "[]")


def _remote_ci_stage(report: list[tuple[str, str, str]]) -> None:
    """触发 workflow_dispatch 的 CI 并等待最终结论。

    CI（full-tests.yml）只执行 init/compileall/unittest 本地步骤，不会回调
    slowtest，无远程递归。tag push 才会发布 release；workflow_dispatch 只跑
    测试 job，无发布副作用。
    """
    if subprocess.run(["gh", "--version"], capture_output=True).returncode != 0:
        report.append(("remote-ci", "UNVERIFIED_MISSING_ENV", "未安装 GitHub CLI（gh）"))
        return
    try:
        branch = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "HEAD"])
    except RuntimeError as exc:
        report.append(("remote-ci", "UNVERIFIED", str(exc)))
        return
    if branch == "HEAD":
        report.append(("remote-ci", "UNVERIFIED", "当前处于 detached HEAD，无法确定触发分支"))
        return
    try:
        _git(["fetch", "origin", branch])
        local_head = _git(["rev-parse", "HEAD"])
        remote_head = _git(["rev-parse", f"origin/{branch}"])
    except RuntimeError as exc:
        report.append(("remote-ci", "UNVERIFIED", str(exc)))
        return
    if local_head != remote_head:
        report.append(
            (
                "remote-ci",
                "UNVERIFIED",
                f"本地 HEAD {local_head[:12]} 未推送到 origin/{branch}（远端为 "
                f"{remote_head[:12]}）。先 git push 后重跑 slowtest；slowtest 不会自动推送。",
            ),
        )
        return
    before = {
        run["databaseId"]
        for run in _gh_json(
            [
                "run",
                "list",
                "--workflow",
                CI_WORKFLOW,
                "--branch",
                branch,
                "--limit",
                "50",
                "--json",
                "databaseId",
            ]
        )
    }
    trigger = subprocess.run(
        ["gh", "workflow", "run", CI_WORKFLOW, "--ref", branch],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if trigger.returncode != 0:
        report.append(("remote-ci", "FAIL", f"触发失败：{trigger.stderr.strip()}"))
        return
    print(f"[slowtest] remote-ci: 已触发 workflow_dispatch（{branch}），等待最终结论…")
    deadline = time.monotonic() + REMOTE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(REMOTE_POLL_SECONDS)
        runs = _gh_json(
            [
                "run",
                "list",
                "--workflow",
                CI_WORKFLOW,
                "--branch",
                branch,
                "--limit",
                "50",
                "--json",
                "databaseId,status,conclusion,headSha,event",
            ]
        )
        fresh = [
            run
            for run in runs
            if run["databaseId"] not in before
            and run.get("event") == "workflow_dispatch"
            and run.get("headSha", "").startswith(local_head)
        ]
        if not fresh:
            continue
        run = fresh[0]
        if run["status"] == "completed":
            conclusion = run.get("conclusion")
            report.append(
                (
                    "remote-ci",
                    "PASS" if conclusion == "success" else "FAIL",
                    f"run {run['databaseId']} 结论 {conclusion}",
                )
            )
            return
        print(f"[slowtest] remote-ci: run {run['databaseId']} 仍在 {run['status']}…")
    report.append(
        (
            "remote-ci",
            "UNVERIFIED_REMOTE_STATUS",
            f"等待超过 {REMOTE_WAIT_SECONDS // 60} 分钟仍未完成；续查："
            f"gh run watch <runId> --repo jchanghong023/all2markdown",
        )
    )


def cmd_slowtest(args: argparse.Namespace) -> int:
    if not args.authorized:
        return _die(
            3,
            "slowtest 需要人类针对本次运行明确授权（由人类指令原文附带 --authorized 运行；"
            "AI 不得自行添加）。",
        )
    if (rc := _ensure_venv()) is not None:
        return rc
    start = time.monotonic()
    stages = _fulltest_stages()
    failed = [name for name, status, _ in stages if status != "PASS"]
    if failed:
        print(f"slowtest 终止：当前平台验证未通过（{ '、'.join(failed) }），跳过远程阶段")
        print(f"slowtest 总耗时 {time.monotonic() - start:.1f}s")
        return 1
    print("[slowtest] wsl-cross-platform: SKIPPED_NOT_APPLICABLE（产品仅支持 Windows 11 x64）")
    report: list[tuple[str, str, str]] = []
    _remote_ci_stage(report)
    for name, status, detail in report:
        print(f"[slowtest] {name}: {status}（{detail}）")
    elapsed = time.monotonic() - start
    print(f"slowtest 总耗时 {elapsed:.1f}s")
    if any(status != "PASS" for _, status, _ in report):
        print("slowtest: NOT FULLY VERIFIED")
        return 1
    print("slowtest PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(description="all2markdown 三级测试门")
    sub = parser.add_subparsers(dest="gate", required=True)

    p_fast = sub.add_parser("fastcheck", help="快速反馈，<=60 秒硬限制")
    p_fast.add_argument(
        "--budget",
        type=float,
        default=FASTCHECK_BUDGET_SECONDS,
        help="时间预算（秒），只允许向下调整",
    )
    p_fast.set_defaults(func=cmd_fastcheck)

    p_full = sub.add_parser("fulltest", help="当前平台全部本地测试（需人工授权）")
    p_full.add_argument("--authorized", action="store_true", help="人类本次明确授权的标志")
    p_full.set_defaults(func=cmd_fulltest)

    p_slow = sub.add_parser("slowtest", help="fulltest + 远程 CI（需人工授权）")
    p_slow.add_argument("--authorized", action="store_true", help="人类本次明确授权的标志")
    p_slow.set_defaults(func=cmd_slowtest)

    p_inner = sub.add_parser("_fast-units", help="内部：fastcheck 的单元子集运行器")
    p_inner.set_defaults(func=lambda _args: _fast_units_inner())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
