"""Cross-surface contract, teaching, replay and browser preservation checks."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from apprentice.executor import run_workflow
from apprentice.executor.runtime import Audit, Control, ExecutionError
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.labeler.build_system_workflow import build
from apprentice.recorder.session import RecordingSession
from apprentice.store.system_workflow import SystemWorkflow
from apprentice.store.workflow import load_workflow


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_marker_directory(monkeypatch, tmp_path):
    from apprentice.recorder import replay_guard
    monkeypatch.setattr(replay_guard, "MARKER_ROOT", tmp_path / ".markers")


def command(*, code="pass", postcondition=None, artifacts=None, **fields):
    return {"surface": "terminal", "action": "run_command", "executable": sys.executable,
            "args": ["-c", code], "cwd": "{workspace}", "purpose": "Run a reviewed sandbox command.",
            "postcondition": postcondition or {"kind": "command_completed", "exit_code": 0},
            "artifacts": artifacts or [], **fields}


def workflow(*steps, parameters=None):
    numbered = [{**step, "id": index} for index, step in enumerate(steps, 1)]
    return SystemWorkflow.model_validate({"status": "ready", "name": "Phase 5 task", "description": "A reviewed mixed task.",
        "parameters": {"workspace": {"type": "directory"}, **(parameters or {})}, "write_roots": ["{workspace}"],
        "steps": numbered, "confirm_before": [step["id"] for step in numbered if step["surface"] in ("terminal", "application")
                                                  or step["action"] == "upload" or step.get("irreversible")]})


def test_project_example_is_ready_and_commands_are_confirmed():
    task = load_workflow(ROOT / "workflows/project_tests.yaml", require_ready=True)
    assert task.confirm_before == [2, 3]
    assert task.steps[-1].timeout_ms == 120000
    assert task.steps[-1].args == ["-m", "pytest", "-q"]


@pytest.mark.parametrize("issue", ["confirmation", "unknown_ref", "secret_cwd", "literal_credential", "secret_script", "wrong_project", "duplicate_output"])
def test_phase5_contract_rejects_incoherent_or_persisted_credentials(issue):
    step = command()
    if issue == "wrong_project":
        step = {"surface": "application", "action": "open_project", "app": "vscode", "path": "{workspace}",
                "purpose": "Open project", "postcondition": {"kind": "project_open", "app": "vscode", "path": "different"}}
    data = {"status": "ready", "name": "Validate", "description": "Validate phase 5.",
            "parameters": {"workspace": {"type": "directory"}, "password": {"type": "secret", "ask_each_time": True}},
            "write_roots": ["{workspace}"], "steps": [{"id": 1, **step}], "confirm_before": [1]}
    if issue == "confirmation":
        data["confirm_before"] = []
    elif issue == "unknown_ref":
        data["steps"][0]["args"] = ["{missing}"]
    elif issue == "secret_cwd":
        data["steps"][0].update(cwd="{password}", args=["{password}"])
    elif issue == "literal_credential":
        data["steps"][0]["args"] = ["--password", "literal-secret"]
    elif issue == "secret_script":
        data["steps"][0].update(action="run_script", script="Write-Output '{password}'", executable=None, args=[])
    elif issue == "duplicate_output":
        data["steps"][0]["artifacts"] = [{"path": "{workspace}/a.txt", "save_as": "workspace"}]
    with pytest.raises(ValueError):
        SystemWorkflow.model_validate(data)


def test_literal_shell_braces_and_secret_environment_are_supported():
    task = workflow(command(action="run_script", executable=None, args=[],
                    script="if ($env:TEST_TOKEN) { Write-Output 'ok' }", env={"TEST_TOKEN": "{token}"}),
                    parameters={"token": {"type": "secret", "ask_each_time": True}})
    assert "{ Write-Output" in task.steps[0].script


def test_terminal_teaching_pairs_completion_parameterizes_values_and_redacts_secrets(tmp_path):
    session = RecordingSession("Run command", roots=[tmp_path])
    start = session.emit("terminal", "command_started", {"command_id": "cmd1", "mode": "exec", "cwd": str(tmp_path),
        "executable": sys.executable, "args": ["-m", "pytest", "--token=<redacted>", "", "name with spaces"],
        "secret_bindings": [{"field": "args", "index": 2}], "requires_review": True})
    done = session.emit("terminal", "command_completed", {"command_id": "cmd1", "exit_code": 0}, origin="observation")
    recording = session.save(tmp_path / "session.json")
    draft = build(recording)
    assert len(draft["steps"]) == 1
    assert draft["steps"][0]["source_events"] == [start["id"], done["id"]]
    assert draft["steps"][0]["args"][0] == "-m"
    assert draft["steps"][0]["cwd"] == "{workspace}" and draft["confirm_before"] == [1]
    secrets = [parameter for parameter in draft["parameters"].values() if parameter["type"] == "secret"]
    assert len(secrets) == 1 and secrets[0]["ask_each_time"] and "example" not in secrets[0]
    with pytest.raises(ValueError):
        build(recording, ready=True)


def test_incomplete_command_and_redacted_application_stay_reviewable(tmp_path):
    session = RecordingSession("Incomplete", roots=[tmp_path])
    session.emit("terminal", "command_started", {"command_id": "unfinished", "mode": "exec", "cwd": str(tmp_path),
                                                   "executable": sys.executable, "args": []})
    session.emit("application", "launch_application", {"app": "custom", "args": "<redacted>", "verified": True})
    draft = build(session.save(tmp_path / "session.json"))
    assert len(draft["steps"]) == 2
    assert all(step["postcondition"]["kind"] == "review_required" for step in draft["steps"])


def test_command_failure_stops_later_browser_submission(tmp_path):
    page_file = tmp_path / "page.html"
    page_file.write_text('<script>window.calls=0</script><button onclick="calls++">Submit</button>')
    task = workflow(command(code="raise SystemExit(2)"),
        {"surface": "browser", "action": "click", "target": {"role": "button", "name": "Submit"},
         "purpose": "Submit after command success.", "irreversible": True,
         "postcondition": {"kind": "text_visible", "text": "Done"}})
    task.start_url = page_file.as_uri()

    async def case():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                result = await SurfaceExecutor(tmp_path / "run", page=page, confirm=lambda _: True).run(task, {"workspace": str(tmp_path)})
                assert result.status == "failed" and result.failed_step == 1
                assert page.url == "about:blank"
            finally:
                await browser.close()
    asyncio.run(case())


def test_command_artifact_becomes_browser_upload_and_audit_contains_no_values(tmp_path):
    page_file = tmp_path / "upload.html"
    page_file.write_text('<label>Report<input type="file"></label>')
    task = workflow(command(code="from pathlib import Path;Path('result.csv').write_text('fresh result')",
                            artifacts=[{"path": "{workspace}/result.csv", "save_as": "report"}]),
        {"surface": "browser", "action": "upload", "target": {"role": "file_input", "name": "Report"},
         "value": "{report}", "purpose": "Upload the freshly verified command output.",
         "postcondition": {"kind": "files_selected", "target": {"role": "file_input", "name": "Report"}, "files": ["{report}"]}})
    task.start_url = page_file.as_uri()
    audit, approvals = Audit(), []

    async def case():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                result = await SurfaceExecutor(tmp_path / "run", page=page, audit=audit,
                    confirm=lambda request: approvals.append(request.step_id) or True).run(task, {"workspace": str(tmp_path)})
                assert result.status == "completed" and result.completed_steps == (1, 2)
                assert await page.locator('input').evaluate("e=>e.files[0].name") == "result.csv"
            finally:
                await browser.close()
    asyncio.run(case())
    assert approvals == [None, 1, 2]
    assert str(tmp_path) not in json.dumps(audit.events) and "fresh result" not in json.dumps(audit.events)


def test_unavailable_late_command_host_fails_before_browser_factory(tmp_path):
    calls = []
    task = workflow({"surface": "browser", "action": "navigate", "value": "https://example.test", "purpose": "Navigate.",
                     "postcondition": {"kind": "url", "url": "https://example.test"}},
                    command(executable="not-an-installed-program-apprentice.exe"))
    async def browser_factory():
        calls.append("browser")
        raise AssertionError
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", browser_factory=browser_factory,
                                         confirm=lambda _: calls.append("approval") or True).run(task, {"workspace": str(tmp_path)}))
    assert result.status == "failed" and result.completed_steps == () and calls == []


def test_declined_command_never_starts_process(tmp_path):
    task = workflow(command(code="from pathlib import Path;Path('should-not-exist').touch()"))
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", confirm=lambda request: request.kind == "workflow").run(
        task, {"workspace": str(tmp_path)}))
    assert result.status == "declined" and not (tmp_path / "should-not-exist").exists()
