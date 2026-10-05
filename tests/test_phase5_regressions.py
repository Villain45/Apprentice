"""Phase 5 boundaries verified without launching a native GUI application."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from apprentice.adapters.application import ApplicationAdapterError, ApplicationBackend
from apprentice.executor.application import ApplicationExecutor
from apprentice.executor.runtime import Audit, ExecutionError
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.executor.terminal import TerminalExecutor
from apprentice.labeler.build_system_workflow import build
from apprentice.recorder.session import RecordingSession
from apprentice.store.system_workflow import SystemWorkflow, needs_confirmation


@pytest.fixture(autouse=True)
def isolated_replay_markers(monkeypatch, tmp_path):
    from apprentice.recorder import replay_guard
    monkeypatch.setattr(replay_guard, "MARKER_ROOT", tmp_path / ".replay-markers")


class FakeApplicationBackend:
    def __init__(self, *, available=True):
        self.available = available
        self.launched = []
        self.terminated = []
        self.released = []
        self.handle = SimpleNamespace(pid=12345, process=SimpleNamespace(poll=lambda: None))

    def supports_project(self, app):
        return app == "vscode"

    def resolve_executable(self, app, executable=None):
        if not self.available:
            raise ApplicationAdapterError("The application is unavailable.")
        return "fake.exe"

    def launch(self, *arguments):
        self.launched.append(arguments)
        return self.handle

    def running(self, handle):
        return True

    def project_open(self, handle, path):
        return True

    def terminate(self, handle):
        self.terminated.append(handle)

    def release(self, handle):
        self.released.append(handle)


class FakeTerminal:
    def preflight(self, workflow, values):
        pass

    async def step(self, step, values):
        raise ExecutionError("The command did not verify.")

    async def close(self):
        pass


def reviewed(*steps):
    workflow = SystemWorkflow.model_validate({
        "status": "draft", "name": "Review phase 5 boundaries", "description": "Check owned work and capability ordering.",
        "parameters": {"workspace": {"type": "directory"}}, "write_roots": ["{workspace}"],
        "steps": [{**step, "id": index} for index, step in enumerate(steps, 1)],
        "confirm_before": list(range(1, len(steps) + 1)),
    })
    data = workflow.model_dump()
    data.update(status="ready", confirm_before=[step.id for step in workflow.steps if needs_confirmation(step)])
    return SystemWorkflow.model_validate(data)


def application():
    return {"surface": "application", "action": "launch_application", "app": "example",
            "purpose": "Open the reviewed application.", "postcondition": {"kind": "application_running", "app": "example"}}


def terminal():
    return {"surface": "terminal", "action": "run_command", "executable": "unused",
            "cwd": "{workspace}", "purpose": "Verify the demonstrated command.",
            "postcondition": {"kind": "command_completed", "exit_code": 0}}


@pytest.mark.parametrize("outcome", ["failed", "declined"])
def test_later_failure_or_decline_does_not_handover_an_owned_application(tmp_path, outcome):
    backend = FakeApplicationBackend()
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    workflow = reviewed(application(), terminal())
    def confirm(request):
        return outcome != "declined" or request.step_id != 2
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", application=executor,
                                        terminal=FakeTerminal(), confirm=confirm).run(workflow, {"workspace": str(tmp_path)}))
    assert result.status == outcome and result.completed_steps == (1,)
    assert backend.terminated == [backend.handle]
    assert backend.released == []


def test_late_application_capability_failure_precedes_directory_creation_and_confirmation(tmp_path):
    backend = FakeApplicationBackend(available=False)
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    mkdir = {"surface": "filesystem", "action": "create_directory", "path": "{workspace}/new-project",
             "purpose": "Prepare the application workspace.",
             "postcondition": {"kind": "path_state", "path": "{workspace}/new-project", "state": "directory"}}
    approvals = []
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", application=executor,
                          confirm=lambda request: approvals.append(request) or True).run(
                              reviewed(mkdir, application()), {"workspace": str(tmp_path)}))
    assert result.status == "failed" and result.completed_steps == ()
    assert approvals == [] and not backend.launched and not (tmp_path / "new-project").exists()


def test_only_completed_workflow_hands_over_verified_application(tmp_path):
    backend = FakeApplicationBackend()
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", application=executor,
                                        confirm=lambda _: True).run(reviewed(application()), {"workspace": str(tmp_path)}))
    assert result.status == "completed"
    assert backend.released == [backend.handle] and backend.terminated == []


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows executable alias policy")
def test_application_preflight_rejects_windows_execution_aliases(tmp_path, monkeypatch):
    from pathlib import Path
    alias = tmp_path / "alias.exe"
    alias.write_bytes(b"alias placeholder")
    original = Path.lstat
    def attributes(path):
        if path == alias:
            return SimpleNamespace(st_file_attributes=0x400)
        return original(path)
    monkeypatch.setattr(Path, "lstat", attributes)
    with pytest.raises(ApplicationAdapterError, match="alias"):
        ApplicationBackend().resolve_executable("example", str(alias))


def test_service_ownership_must_match_address_not_just_port(monkeypatch):
    import socket
    import psutil
    parent = SimpleNamespace(pid=2468, children=lambda recursive: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: parent)
    listener = SimpleNamespace(pid=2468, status=psutil.CONN_LISTEN, family=socket.AF_INET6,
                               laddr=SimpleNamespace(ip="::1", port=8123))
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [listener])
    owned = SimpleNamespace(process=SimpleNamespace(pid=2468))
    # ::1 cannot serve a request made to 127.0.0.1 even when its port is identical.
    assert not TerminalExecutor.listener_owned(owned, "http://127.0.0.1:8123")


def test_repeated_cancel_during_process_creation_still_recovers_and_stops_the_process(tmp_path, monkeypatch):
    from apprentice.executor import terminal as terminal_module
    class Process:
        pid = 12345
        returncode = None
        def kill(self):
            self.returncode = -1
        async def wait(self):
            return self.returncode
    class Job:
        def close(self):
            pass
        def attach_and_resume(self, pid):
            pass
    process = Process()
    monkeypatch.setattr(terminal_module, "WindowsJob", Job)
    async def case():
        created, release = asyncio.Event(), asyncio.Event()
        async def delayed_creation(*args, **kwargs):
            # This models OS creation succeeding before asyncio returns its handle.
            created.set()
            await release.wait()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_creation)
        executor = TerminalExecutor()
        task = asyncio.create_task(executor._spawn("fake.exe", [], tmp_path, {}))
        await created.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None, "An OS process was lost before ownership was established."
    asyncio.run(case())


def recorded_command(session, cwd, *, arguments=(), exit_code=0):
    command_id = "command-" + str(len(session.snapshot()["events"]) + 1)
    session.emit("terminal", "command_started", {"command_id": command_id, "mode": "exec",
                 "cwd": str(cwd), "executable": sys.executable, "args": list(arguments)})
    session.emit("terminal", "command_completed", {"command_id": command_id, "exit_code": exit_code},
                 origin="observation")


def test_recorded_nonzero_command_completion_requires_explicit_review(tmp_path):
    session = RecordingSession("Review failed command", roots=[tmp_path])
    recorded_command(session, tmp_path, exit_code=2)
    recording = session.save(tmp_path / "session.json")
    draft = SystemWorkflow.model_validate(build(recording))
    assert draft.pending_review(), "A demonstrated command failure must not become accepted success automatically."
    with pytest.raises(ValueError):
        build(recording, ready=True)


@pytest.mark.parametrize("watched", [True, False])
def test_recorded_command_outside_watched_roots_declares_scope_or_remains_in_review(tmp_path, watched):
    workspace = tmp_path / "watched"
    workspace.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()
    session = RecordingSession("Command in another project", roots=[workspace] if watched else [])
    recorded_command(session, other)
    recording = session.save(tmp_path / "session.json")
    draft = SystemWorkflow.model_validate(build(recording))
    command = draft.steps[0]
    assert command.cwd in draft.write_roots or draft.pending_review(), (
        "A command outside watched roots needs declared execution scope or an explicit unresolved review."
    )
    if command.cwd not in draft.write_roots:
        with pytest.raises(ValueError):
            build(recording, ready=True)


def test_browser_download_output_does_not_collide_with_command_parameter_names(tmp_path):
    session = RecordingSession("Download and command", roots=[tmp_path])
    session.emit("browser", "navigate", {"url": "http://localhost/example"})
    session.emit("browser", "click", {"element": {"tag": "button", "text": "Export", "css": "#export"}})
    session.emit("browser", "download", {"filename": "report.csv"}, origin="observation")
    recorded_command(session, tmp_path, arguments=["--downloaded-file", "a separate argument"])
    draft = SystemWorkflow.model_validate(build(session.save(tmp_path / "session.json")))
    output = draft.steps[0].postcondition.save_as
    assert output == "downloaded_file"
    assert output not in draft.parameters
    assert draft.steps[1].args[1] != "{" + output + "}"
    assert draft.parameters[draft.steps[1].args[1].strip("{}")].example == "a separate argument"


def test_terminal_cleanup_failure_still_cleans_other_adapters_and_reports_a_final_outcome(tmp_path):
    class CleanupFailure(FakeTerminal):
        async def close(self):
            raise ExecutionError("Owned command cleanup did not verify.")
    backend = FakeApplicationBackend()
    application_executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    audit = Audit()
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", application=application_executor,
                         terminal=CleanupFailure(), confirm=lambda _: True, audit=audit).run(
                             reviewed(application(), terminal()), {"workspace": str(tmp_path)}))
    assert result.status == "failed" and result.completed_steps == (1,)
    assert backend.terminated == [backend.handle] and not backend.released
    assert audit.events[-1]["event"] == "run_finished" and audit.events[-1]["status"] == "failed"


def test_failed_gui_handover_terminates_the_still_owned_application(tmp_path):
    class HandoverFailure(FakeApplicationBackend):
        def release(self, handle):
            raise ExecutionError("The application could not be handed over.")
    backend = HandoverFailure()
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    async def case():
        await executor.step(reviewed(application()).steps[0], {"workspace": str(tmp_path)})
        with pytest.raises(ExecutionError):
            await executor.close()
        assert backend.terminated == [backend.handle]
        assert not executor._verified
    asyncio.run(case())


def test_outside_root_command_can_be_explicitly_reviewed_with_a_declared_root(tmp_path):
    import yaml
    project = tmp_path / "other-project"
    project.mkdir()
    session = RecordingSession("Review command scope")
    recorded_command(session, project)
    recording = session.save(tmp_path / "session.json")
    review = tmp_path / "review.yaml"
    review.write_text(yaml.safe_dump({
        "parameters": {"project": {"type": "directory", "example": str(project)}},
        "write_roots": ["{project}"],
        "events": {1: {"step": {
            "surface": "terminal", "action": "run_command", "executable": sys.executable,
            "args": ["-c", "pass"], "cwd": "{project}", "purpose": "Run the explicitly reviewed project command.",
            "postcondition": {"kind": "command_completed", "exit_code": 0},
        }}},
    }), encoding="utf-8")
    ready = SystemWorkflow.model_validate(build(recording, review_path=review, ready=True))
    assert ready.status == "ready" and ready.write_roots == ["{project}"]
    assert ready.steps[0].source_events == [1, 2] and ready.steps[0].cwd == "{project}"


def test_reviewed_artifact_output_names_are_reserved_before_command_arguments(tmp_path):
    import yaml
    session = RecordingSession("Review artifact names", roots=[tmp_path])
    recorded_command(session, tmp_path)
    recorded_command(session, tmp_path, arguments=["a separate argument"])
    recording = session.save(tmp_path / "session.json")
    review = tmp_path / "review.yaml"
    review.write_text(yaml.safe_dump({"events": {1: {"step": {
        "surface": "terminal", "action": "run_command", "executable": sys.executable,
        "args": ["-c", "pass"], "cwd": "{workspace}", "purpose": "Run a reviewed artifact-producing command.",
        "postcondition": {"kind": "command_completed", "exit_code": 0},
        "artifacts": [{"path": "{workspace}/result.txt", "save_as": "argument"}],
    }}}}), encoding="utf-8")
    draft = SystemWorkflow.model_validate(build(recording, review_path=review))
    assert "argument" not in draft.parameters
    assert draft.steps[0].artifacts[0].save_as == "argument"
    assert draft.steps[1].args == ["{argument_2}"]
    assert draft.parameters["argument_2"].example == "a separate argument"


def test_nonzero_exit_review_notes_are_added_to_existing_credential_review(tmp_path):
    session = RecordingSession("Review failed secret command", roots=[tmp_path])
    session.emit("terminal", "command_started", {"command_id": "secret-command", "mode": "exec",
                 "cwd": str(tmp_path), "executable": sys.executable, "args": ["<redacted>"],
                 "secret_bindings": [{"field": "args", "index": 0}], "requires_review": True})
    session.emit("terminal", "command_completed", {"command_id": "secret-command", "exit_code": 3}, origin="observation")
    draft = SystemWorkflow.model_validate(build(session.save(tmp_path / "session.json")))
    assert len(draft.steps[0].review_notes) == 2
    assert any("credential" in note for note in draft.steps[0].review_notes)
    assert any("nonzero" in note for note in draft.steps[0].review_notes)
