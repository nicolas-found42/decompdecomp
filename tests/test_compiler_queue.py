"""ROM and fork builds share the budget before they start staging or running."""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest

from decompnet import compiler
from decompnet.forks import project


@pytest.mark.parametrize(("command", "expected"), [
    (["ninja", "unit.o"], (["ninja", "unit.o", "-j4"], 4)),
    (["ninja", "-j1", "unit.o"], (["ninja", "-j1", "unit.o", "-j1"], 1)),
    (["make", "--jobs=32", "--", "-target"], (["make", "--jobs=32", "-j4", "--", "-target"], 4)),
    (["gmake", "-j", "2"], (["gmake", "-j", "2", "-j2"], 2)),
    (["ninja", "-n", "unit.o"], (["ninja", "-n", "unit.o"], 0)),
    (["./build.sh"], (["./build.sh"], 8)),
])
def test_builder_capacity_matches_the_command(monkeypatch, command, expected):
    monkeypatch.setattr(project, "FORK_BUILD_SLOTS", 4)
    monkeypatch.setattr(project, "COMPILER_SLOTS", 8)
    assert project._bounded_build(command) == expected


def test_a_fork_build_leaves_rom_and_toolchain_commands_waiting_before_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "_SLOTS", compiler.CompilerQueue(2))
    monkeypatch.setattr(project, "FORK_BUILD_SLOTS", 2)
    build_started, finish = threading.Event(), threading.Event()
    rom_waiting, export_waiting = threading.Event(), threading.Event()
    staged = []
    commands = []

    def build(self, cmd, log, timeout, failed, platform):
        assert cmd[-1] == "-j2"
        build_started.set()
        assert finish.wait(5)
        return 0, [], ""

    tempdir = compiler.tempfile.TemporaryDirectory

    def stage(*args, **kwargs):
        staged.append(True)
        return tempdir(*args, **kwargs)

    def execute(cfg, work, argv, timeout, **kwargs):
        commands.append(argv[0])
        return subprocess.CompletedProcess(argv, 1 if argv[0] == "compile" else 0, "", "")

    monkeypatch.setattr(project.ForkProject, "_execute_admitted", build)
    monkeypatch.setattr(compiler.tempfile, "TemporaryDirectory", stage)
    monkeypatch.setattr(compiler, "_exec", execute)
    cfg = compiler.CompilerConfig(["compile"], sandbox="none")

    def rom():
        with compiler.cancellable(rom_waiting.set):
            return compiler.compile_source(cfg, "", "int f(void) { return 1; }")

    def export():
        with compiler.cancellable(export_waiting.set):
            return compiler.run_in(cfg, tmp_path, ["export"])

    with ExitStack() as stack:
        pool = stack.enter_context(ThreadPoolExecutor(3))
        stack.callback(finish.set)
        fork = pool.submit(project.ForkProject(tmp_path).run, ["ninja"])
        assert build_started.wait(3)
        pending_rom, pending_export = pool.submit(rom), pool.submit(export)
        assert rom_waiting.wait(3) and export_waiting.wait(3)
        assert staged == [] and commands == []
        finish.set()
        assert fork.result(timeout=3)[0]
        assert not pending_rom.result(timeout=3).ok
        assert pending_export.result(timeout=3).returncode == 0
    assert staged == [True]
    assert sorted(commands) == ["compile", "export"]
