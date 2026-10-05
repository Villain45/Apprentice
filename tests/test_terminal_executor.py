"""Local executable tests exercise real argv, owned services and cancellation."""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from apprentice.executor.filesystem import PathPolicy
from apprentice.executor.runtime import Control, ExecutionError, StopRequested
from apprentice.executor.terminal import TerminalExecutor
from apprentice.store.system_workflow import TerminalStep


def literal(value):
    return value.replace("{", "{{").replace("}", "}}")


def command(tmp_path, code="pass", *, args=(), postcondition=None, **extra):
    return TerminalStep.model_validate({
        "id": 1, "surface": "terminal", "action": "run_command", "executable": sys.executable,
        "args": ["-c", literal(code), *args], "cwd": str(tmp_path), "purpose": "Run a local test command",
        "postcondition": postcondition or {"kind": "command_completed", "exit_code": 0}, **extra,
    })


def executor(tmp_path, **kwargs):
    return TerminalExecutor(policy=PathPolicy([tmp_path]), run_dir=tmp_path / "run", **kwargs)


def test_direct_argv_preserves_empty_quotes_spaces_and_metacharacters(tmp_path):
    arguments = ["", 'two words', 'a"b', "C:\\folder with spaces\\", "& echo should-not-run", "$(nothing)"]
    step = command(tmp_path, "import json,sys;from pathlib import Path;Path('argv.json').write_text(json.dumps(sys.argv[1:]))",
                   args=arguments, artifacts=[{"path": str(tmp_path / "argv.json"), "save_as": "result"}])
    values = {}
    asyncio.run(executor(tmp_path).step(step, values))
    assert json.loads((tmp_path / "argv.json").read_text()) == arguments
    assert values["result"] == str(tmp_path / "argv.json")


def test_cwd_environment_and_secret_argv_are_transient(tmp_path, capsys):
    secret = "synthetic secret with spaces"
    code = ("import hashlib,os,sys;from pathlib import Path;"
            "print(sys.argv[1]);print(os.environ['TEST_TOKEN']);"
            "Path('digest.txt').write_text(hashlib.sha256((sys.argv[1]+os.environ['TEST_TOKEN']).encode()).hexdigest())")
    step = command(tmp_path, code, args=["{token}"], env={"TEST_TOKEN": "{token}"},
                   artifacts=[{"path": str(tmp_path / "digest.txt"), "save_as": "digest"}])
    values = {"token": secret}
    asyncio.run(executor(tmp_path).step(step, values))
    assert (tmp_path / "digest.txt").read_text() == hashlib.sha256((secret * 2).encode()).hexdigest()
    assert secret not in capsys.readouterr().out
    assert not (tmp_path / "run").exists()


def test_unexpected_exit_is_not_retried_and_raw_error_is_not_exposed(tmp_path):
    code = "from pathlib import Path;p=Path('count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');raise RuntimeError('private error detail')"
    with pytest.raises(ExecutionError, match="exit code") as failure:
        asyncio.run(executor(tmp_path).step(command(tmp_path, code), {}))
    assert (tmp_path / "count").read_text() == "1"
    assert "private error detail" not in str(failure.value)


def test_reviewed_nonzero_exit_can_verify(tmp_path):
    step = command(tmp_path, "raise SystemExit(3)", postcondition={"kind": "command_completed", "exit_code": 3})
    asyncio.run(executor(tmp_path).step(step, {}))


def test_stale_artifact_does_not_bind_any_output(tmp_path):
    (tmp_path / "stale.txt").write_text("old")
    step = command(tmp_path, "from pathlib import Path;Path('new.txt').write_text('new')", artifacts=[
        {"path": str(tmp_path / "new.txt"), "save_as": "new_file"},
        {"path": str(tmp_path / "stale.txt"), "save_as": "stale_file"},
    ])
    values = {"existing": "keep"}
    with pytest.raises(ExecutionError, match="stale"):
        asyncio.run(executor(tmp_path).step(step, values))
    assert values == {"existing": "keep"}


def test_overwritten_artifact_is_fresh(tmp_path):
    output = tmp_path / "output.txt"
    output.write_text("before")
    step = command(tmp_path, "from pathlib import Path;Path('output.txt').write_text('after')",
                   artifacts=[{"path": str(output), "save_as": "output"}])
    values = {}
    asyncio.run(executor(tmp_path).step(step, values))
    assert values == {"output": str(output)}
    assert output.read_text() == "after"


def test_preflight_rejects_missing_executable_before_writes(tmp_path):
    step = command(tmp_path, executable="apprentice-does-not-exist-584810")
    with pytest.raises(ExecutionError, match="unavailable"):
        executor(tmp_path).preflight(SimpleNamespace(steps=[step]), {})
    assert not list(tmp_path.iterdir())


def test_future_artifact_argument_does_not_hide_unavailable_executable(tmp_path):
    step = command(tmp_path, executable="apprentice-does-not-exist-584810", args=["{future_file}"])
    with pytest.raises(ExecutionError, match="unavailable"):
        executor(tmp_path).preflight(SimpleNamespace(steps=[step]), {})


def test_unresolved_future_search_path_requires_explicit_command_host(tmp_path):
    step = command(tmp_path, executable="python", env={"PATH": "{future_directory}"})
    with pytest.raises(ExecutionError, match="future PATH"):
        executor(tmp_path).preflight(SimpleNamespace(steps=[step]), {})


def test_preflight_allows_explicit_directory_creation_before_command(tmp_path):
    from apprentice.store.system_workflow import FilesystemStep
    child = tmp_path / "new-folder"
    mkdir = FilesystemStep.model_validate({
        "id": 1, "surface": "filesystem", "action": "create_directory", "path": str(child),
        "purpose": "Create command working directory", "postcondition": {
            "kind": "path_state", "path": str(child), "state": "directory"},
    })
    step = command(tmp_path, id=2, cwd=str(child))
    executor(tmp_path).preflight(SimpleNamespace(steps=[mkdir, step]), {})
    assert not child.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppExecLink behavior")
def test_preflight_rejects_windows_execution_alias_before_mutations(tmp_path, monkeypatch):
    original = Path.lstat
    def alias_attributes(path):
        if path == Path(sys.executable):
            return SimpleNamespace(st_file_attributes=0x400)
        return original(path)
    monkeypatch.setattr(Path, "lstat", alias_attributes)
    with pytest.raises(ExecutionError, match="aliases"):
        executor(tmp_path).preflight(SimpleNamespace(steps=[command(tmp_path)]), {})


def test_cwd_and_artifacts_are_bounded_by_reviewed_roots(tmp_path):
    runner = executor(tmp_path)
    for step in (command(tmp_path, cwd=str(tmp_path.parent)), command(tmp_path, artifacts=[{
            "path": str(tmp_path.parent / "outside.txt"), "save_as": "output"}])):
        with pytest.raises(ExecutionError, match="outside"):
            runner.preflight(SimpleNamespace(steps=[step]), {})


def test_batch_executables_are_not_implicitly_shell_launched(tmp_path):
    batch = tmp_path / "sample.cmd"
    batch.write_text("@echo off\n")
    batch.chmod(0o755)
    with pytest.raises(ExecutionError, match="Batch files"):
        executor(tmp_path).preflight(SimpleNamespace(steps=[command(tmp_path, executable=str(batch))]), {})


def test_artifact_directory_is_refused(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    step = command(tmp_path, artifacts=[{"path": str(folder), "save_as": "output"}])
    with pytest.raises(ExecutionError, match="regular files"):
        asyncio.run(executor(tmp_path).step(step, {}))


def test_service_readiness_only_accepts_loopback_http(tmp_path):
    runner = executor(tmp_path)
    for url in ("https://localhost:9000", "http://example.com", "http://127.0.0.1.evil.test",
                "http://user:password@localhost:9000", "http://localhost:99999"):
        with pytest.raises(ExecutionError, match="loopback"):
            runner.readiness_address(url)


def test_existing_listener_never_counts_as_a_new_ready_service(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        step = command(tmp_path, postcondition={"kind": "service_ready", "url": url})
        with pytest.raises(ExecutionError, match="already listening"):
            executor(tmp_path).preflight(SimpleNamespace(steps=[step]), {})


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_real_service_is_owned_and_closed_when_workflow_finishes(tmp_path):
    async def scenario():
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        step = command(tmp_path, "from http.server import HTTPServer,SimpleHTTPRequestHandler;"
                       f"HTTPServer(('127.0.0.1',{port}),SimpleHTTPRequestHandler).serve_forever()",
                       postcondition={"kind": "service_ready", "url": url}, timeout_ms=10000)
        runner = executor(tmp_path)
        try:
            await runner.step(step, {})
            assert len(runner._owned) == 1
            process = next(iter(runner._owned)).process
            assert process.returncode is None
        finally:
            await runner.close()
        assert process.returncode is not None
        assert not runner._owned
        TerminalExecutor.unused_port(url)
    asyncio.run(scenario())


def test_step_timeout_terminates_process_and_has_no_outputs(tmp_path):
    async def scenario():
        runner = executor(tmp_path)
        step = command(tmp_path, "import time;time.sleep(30)", timeout_ms=150)
        with pytest.raises(ExecutionError, match="timed out"):
            await runner.step(step, {})
        assert not runner._owned
    asyncio.run(scenario())


def test_stop_winning_spawn_race_still_cleans_new_process(tmp_path):
    async def scenario():
        runner = executor(tmp_path, control=Control(timeout_ms=10000))
        original = runner._spawn
        async def stopped_spawn(*args):
            result = await original(*args)
            runner.control.stop.set()
            return result
        runner._spawn = stopped_spawn
        with pytest.raises(StopRequested):
            await runner.step(command(tmp_path, "import time;time.sleep(30)"), {})
        assert not runner._owned
    asyncio.run(scenario())


async def wait_for_file(path):
    for _ in range(200):
        if path.exists():
            try:
                return int(path.read_text())
            except ValueError:
                pass
        await asyncio.sleep(0.025)
    raise AssertionError("The local child process did not start.")


def alive(pid):
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_stop_kills_owned_child_tree(tmp_path):
    async def scenario():
        runner = executor(tmp_path, control=Control(timeout_ms=10000))
        code = ("import subprocess,sys,time;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                "Path('child.pid').write_text(str(child.pid));time.sleep(30)")
        task = asyncio.create_task(runner.step(command(tmp_path, code), {}))
        pid = await wait_for_file(tmp_path / "child.pid")
        assert alive(pid)
        runner.control.stop.set()
        with pytest.raises(StopRequested):
            await task
        assert not alive(pid)
        assert not runner._owned
    asyncio.run(scenario())


def test_task_cancellation_kills_owned_child_tree(tmp_path):
    async def scenario():
        runner = executor(tmp_path, control=Control(timeout_ms=10000))
        code = ("import subprocess,sys,time;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                "Path('child.pid').write_text(str(child.pid));time.sleep(30)")
        task = asyncio.create_task(runner.step(command(tmp_path, code), {}))
        pid = await wait_for_file(tmp_path / "child.pid")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not alive(pid)
        assert not runner._owned
    asyncio.run(scenario())


@pytest.mark.skipif(not (shutil.which("pwsh") or shutil.which("powershell")), reason="PowerShell is not installed")
def test_reviewed_powershell_preserves_braces_and_reads_secret_environment(tmp_path):
    step = TerminalStep.model_validate({
        "id": 1, "surface": "terminal", "action": "run_script", "cwd": str(tmp_path),
        "script": "if ($true) { [System.IO.File]::WriteAllText((Join-Path $PWD 'output.txt'), $env:TEST_TOKEN) }",
        "env": {"TEST_TOKEN": "{token}"}, "purpose": "Run reviewed PowerShell",
        "postcondition": {"kind": "command_completed", "exit_code": 0},
        "artifacts": [{"path": str(tmp_path / "output.txt"), "save_as": "output"}], "timeout_ms": 10000,
    })
    asyncio.run(executor(tmp_path).step(step, {"token": "synthetic Unicode value: café"}))
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "synthetic Unicode value: café"
    assert not list((tmp_path / "run").glob("*.ps1"))
