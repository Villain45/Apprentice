"""Exact project evidence, owned-process cleanup, and structured teaching."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import time

import pytest

from apprentice.adapters.application import ApplicationAdapterError, ApplicationBackend, LaunchHandle
from apprentice.executor.application import ApplicationExecutor
from apprentice.executor.runtime import Control, ExecutionError, StopRequested
from apprentice.recorder.application import ApplicationCapture, _Spool
from apprentice.recorder.session import RecordingSession
from apprentice.store.system_workflow import ApplicationStep


def step(project="{workspace}", **overrides):
    data = {"id": 1, "surface": "application", "action": "open_project", "app": "vscode",
            "path": project, "purpose": "Open reviewed workspace",
            "postcondition": {"kind": "project_open", "app": "vscode", "path": project}}
    if project is None:
        data.update(action="launch_application", app="example", postcondition={"kind": "application_running", "app": "example"})
        data.pop("path")
    return ApplicationStep.model_validate({**data, **overrides})


class FakeBackend:
    def __init__(self, proof=True, exit_code=None):
        self.proof = proof
        self.exit_code = exit_code
        self.launches = []
        self.terminated = []
        self.released = []
        self.handle = SimpleNamespace(pid=76543, process=SimpleNamespace(poll=lambda: self.exit_code))

    def resolve_executable(self, app, executable=None):
        return executable or "fake.exe"

    def supports_project(self, app):
        return app == "vscode"

    def launch(self, *arguments):
        self.launches.append(arguments)
        return self.handle

    def project_open(self, handle, path):
        assert handle is self.handle
        return self.proof

    def running(self, handle):
        return self.proof

    def terminate(self, handle):
        self.terminated.append(handle)

    def release(self, handle):
        self.released.append(handle)


def test_project_replay_requires_exact_backend_proof_and_hands_over_success(tmp_path):
    backend = FakeBackend()
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    async def run():
        await executor.step(step(), {"workspace": str(tmp_path)})
        await executor.close()
    asyncio.run(run())
    assert backend.launches[0][4] == str(tmp_path)
    assert not backend.terminated and backend.released == [backend.handle]


def test_successful_launcher_exit_does_not_verify_project(tmp_path):
    backend = FakeBackend(proof=False, exit_code=0)
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    with pytest.raises(ExecutionError, match="without verified"):
        asyncio.run(executor.step(step(), {"workspace": str(tmp_path)}))
    assert backend.terminated == [backend.handle]
    assert not backend.released


def test_no_project_proof_times_out_and_cleans_owned_launch_once(tmp_path):
    backend = FakeBackend(proof=False)
    executor = ApplicationExecutor(tmp_path / "run", backend=backend, control=Control(timeout_ms=30))
    with pytest.raises(ExecutionError, match="timed out"):
        asyncio.run(executor.step(step(), {"workspace": str(tmp_path)}))
    assert backend.terminated == [backend.handle] and len(backend.launches) == 1


def test_stop_and_task_cancellation_clean_pending_launch(tmp_path):
    async def run(cancel):
        backend = FakeBackend(proof=False)
        control = Control(timeout_ms=1000)
        executor = ApplicationExecutor(tmp_path / "run", backend=backend, control=control)
        operation = asyncio.create_task(executor.step(step(), {"workspace": str(tmp_path)}))
        await asyncio.sleep(0.01)
        if cancel:
            operation.cancel()
        else:
            control.stop.set()
        with pytest.raises(asyncio.CancelledError if cancel else StopRequested):
            await operation
        assert backend.terminated == [backend.handle]
    asyncio.run(run(False))
    asyncio.run(run(True))


def test_stop_closes_previously_verified_owned_application(tmp_path):
    backend = FakeBackend()
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    async def run():
        await executor.step(step(), {"workspace": str(tmp_path)})
        await executor.close(stopped=True)
    asyncio.run(run())
    assert backend.terminated == [backend.handle] and not backend.released


def test_application_preflight_rejects_missing_project_and_unsupported_project_adapter(tmp_path):
    backend = FakeBackend()
    executor = ApplicationExecutor(tmp_path / "run", backend=backend)
    for task, value in [(step(), str(tmp_path / "missing")),
                        (step(app="other", postcondition={"kind": "project_open", "app": "other", "path": "{workspace}"}), str(tmp_path))]:
        with pytest.raises(ExecutionError):
            executor.preflight(SimpleNamespace(steps=[task]), {"workspace": value})
    assert not backend.launches


def test_project_launch_arguments_cannot_override_owned_profile(tmp_path):
    executor = ApplicationExecutor(tmp_path, backend=FakeBackend())
    task = step(args=["--user-data-dir", "outside"])
    with pytest.raises(ExecutionError, match="additional"):
        executor.preflight(SimpleNamespace(steps=[task]), {"workspace": str(tmp_path)})


def test_application_name_is_parameterized_before_dispatch(tmp_path):
    backend = FakeBackend()
    executor = ApplicationExecutor(tmp_path, backend=backend)
    task = step(app="{app}", postcondition={"kind": "project_open", "app": "{app}", "path": "{workspace}"})
    executor.preflight(SimpleNamespace(steps=[task]), {"workspace": str(tmp_path), "app": "vscode"})
    asyncio.run(executor.step(task, {"workspace": str(tmp_path), "app": "vscode"}))
    assert backend.launches[0][0] == "vscode"


def test_preflight_allows_directory_created_by_a_prior_verified_filesystem_step(tmp_path):
    backend = FakeBackend()
    executor = ApplicationExecutor(tmp_path, backend=backend)
    create = SimpleNamespace(surface="filesystem", action="create_directory", path="{workspace}/new-project")
    project = step(project="{workspace}/new-project")
    executor.preflight(SimpleNamespace(steps=[create, project]), {"workspace": str(tmp_path)})
    assert not (tmp_path / "new-project").exists() and not backend.launches
    with pytest.raises(ExecutionError):
        executor.preflight(SimpleNamespace(steps=[project, create]), {"workspace": str(tmp_path)})


def test_preflight_defers_argument_outputs_until_their_producer_completes(tmp_path):
    backend = FakeBackend()
    executor = ApplicationExecutor(tmp_path, backend=backend)
    task = step(project=None, args=["{generated_report}"])
    executor.preflight(SimpleNamespace(steps=[task]), {})
    asyncio.run(executor.step(task, {"generated_report": str(tmp_path / "report.txt")}))
    assert backend.launches[0][2] == [str(tmp_path / "report.txt")]


def test_structured_capture_proves_application_and_preserves_exact_project(tmp_path):
    backend = FakeBackend()
    session = RecordingSession("project")
    event = ApplicationCapture(session, backend=backend, evidence_dir=tmp_path / "evidence").record_application(
        "vscode", path=str(tmp_path), cwd=str(tmp_path))
    assert event["kind"] == "open_project"
    assert event["payload"]["verified"] is True
    assert event["payload"]["path"] == str(tmp_path)
    assert event["evidence"][0]["verification"] == "vscode_workspace_api"
    assert backend.released == [backend.handle]


def test_uncertain_capture_retains_action_for_review_and_kills_owned_launch(tmp_path):
    backend = FakeBackend(proof=False, exit_code=0)
    session = RecordingSession("project")
    event = ApplicationCapture(session, backend=backend, evidence_dir=tmp_path).record_application("vscode", path=str(tmp_path))
    assert event["payload"]["verified"] is False
    assert event["payload"]["review_reason"] and event["evidence"] == []
    assert backend.terminated == [backend.handle]


def test_native_application_argument_secrets_are_redacted(tmp_path):
    session = RecordingSession("app")
    ApplicationCapture(session, backend=FakeBackend(), evidence_dir=tmp_path).record_application(
        "example", args=["--api-key", "never-save-this-token"])
    recording = json.dumps(session.snapshot())
    assert "never-save-this-token" not in recording and "<redacted>" in recording


def test_application_spool_is_utf8_one_json_envelope_without_raw_console_output(tmp_path):
    spool = _Spool(tmp_path / "events.jsonl")
    event = spool.emit("application", "open_project", {"app": "vscode", "path": "C:/project", "verified": True},
                       evidence=[{"verification": "vscode_workspace_api", "process_id": 123}])
    assert json.loads(spool.path.read_text(encoding="utf-8")) == event
    assert not spool.path.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("proof", [
    {"nonce": "different", "process_id": 567, "folders": [{"scheme": "file", "path": "EXPECTED"}]},
    {"nonce": "correct", "process_id": 999, "folders": [{"scheme": "file", "path": "EXPECTED"}]},
    {"nonce": "correct", "process_id": 567, "folders": [{"scheme": "file", "path": "WRONG"}]},
    {"nonce": "correct", "process_id": 567, "folders": [{"scheme": "remote", "path": "EXPECTED"}]},
    {"nonce": "correct", "process_id": 567, "folders": []},
])
def test_workspace_proof_rejects_stale_unowned_wrong_and_remote_evidence(tmp_path, monkeypatch, proof):
    expected = tmp_path / "expected"
    expected.mkdir()
    folder = proof["folders"][0] if proof["folders"] else {}
    if folder.get("path") == "EXPECTED":
        folder["path"] = str(expected)
    marker = tmp_path / "proof.json"
    marker.write_text(json.dumps(proof))
    handle = LaunchHandle(SimpleNamespace(pid=123), "fake.exe", 100, marker=marker, nonce="correct")
    backend = ApplicationBackend()
    monkeypatch.setattr(backend, "running", lambda handle: True)
    monkeypatch.setattr(backend, "owned_processes", lambda handle: {567: object()})
    assert not backend.project_open(handle, expected)


def test_workspace_proof_accepts_exact_owned_live_api_evidence(tmp_path, monkeypatch):
    marker = tmp_path / "proof.json"
    marker.write_text(json.dumps({"nonce": "correct", "process_id": 567,
                                  "folders": [{"scheme": "file", "path": str(tmp_path)}]}))
    handle = LaunchHandle(SimpleNamespace(pid=123), "fake.exe", 100, marker=marker, nonce="correct")
    backend = ApplicationBackend()
    monkeypatch.setattr(backend, "running", lambda handle: True)
    monkeypatch.setattr(backend, "owned_processes", lambda handle: {567: object()})
    assert backend.project_open(handle, tmp_path)


def test_verifier_extension_uses_workspace_api_and_local_isolated_profile(tmp_path):
    extensions, profile, marker = ApplicationBackend._verifier(tmp_path, "nonce")
    extension = next(extensions.iterdir())
    manifest = json.loads((extension / "package.json").read_text())
    script = (extension / "extension.js").read_text()
    assert "vscode.workspace.workspaceFolders" in script
    assert manifest["capabilities"]["untrustedWorkspaces"]["supported"] is True
    assert "child_process" not in script and "getConfiguration" not in script
    assert profile.parent == tmp_path and marker.parent == tmp_path


def test_real_owned_application_failure_cleans_our_process_without_scanning_arguments(tmp_path):
    backend = ApplicationBackend()
    # Python is a headless generic application for this ownership check.
    handle = backend.launch("python", sys.executable, ["-c", "import time; time.sleep(20)"],
                            str(tmp_path), None, tmp_path / "evidence")
    assert backend.running(handle)
    backend.terminate(handle)
    assert not backend.running(handle)


def test_real_generic_application_handover_leaves_successful_process_running(tmp_path):
    backend = ApplicationBackend()
    handle = backend.launch("python", sys.executable, ["-c", "import time; time.sleep(20)"],
                            str(tmp_path), None, tmp_path / "evidence")
    try:
        assert backend.running(handle)
        backend.release(handle)
        assert backend.running(handle)
    finally:
        backend.terminate(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable launch policy")
def test_windows_batch_application_launcher_is_not_implicitly_executed(tmp_path):
    batch = tmp_path / "app.cmd"
    batch.write_text("@echo off\nexit /b 0\n")
    with pytest.raises(ApplicationAdapterError, match="batch launchers"):
        ApplicationBackend().resolve_executable("example", str(batch))


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("APPRENTICE_TEST_GUI") != "1",
                    reason="Explicit opt-in desktop acceptance; normal tests do not launch GUI apps")
def test_real_vscode_project_api_and_owned_gui_cleanup(tmp_path):
    backend = ApplicationBackend()
    executable = backend.resolve_executable("vscode")
    project = tmp_path / "reviewed-project"
    project.mkdir()
    handle = backend.launch("vscode", executable, [], str(project), str(project), tmp_path / "evidence")
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not backend.project_open(handle, project):
            time.sleep(0.1)
        assert backend.project_open(handle, project), "VS Code never published exact workspace API evidence"
    finally:
        backend.terminate(handle)
    assert not backend.running(handle)
