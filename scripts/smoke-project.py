"""Preview or execute the real Windows Phase 5 teaching/replay acceptance.

Execution uses two fresh projects inside runs/, opens their folders in Explorer
and isolated VS Code windows, and runs their local pytest commands. Only the
application processes created by this check are closed. Explorer tabs remain.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from apprentice.adapters.application import ApplicationBackend
from apprentice.adapters.windows import WindowsCapture, WindowsExplorer
from apprentice.executor.application import ApplicationExecutor
from apprentice.executor.runtime import Audit, Control, dry_run
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.labeler.build_system_workflow import build
from apprentice.recorder.application import ApplicationCapture
from apprentice.recorder.filesystem import FilesystemCapture
from apprentice.recorder.session import RecordingSession
from apprentice.recorder.terminal import TerminalCapture
from apprentice.store.system_workflow import SystemWorkflow
from apprentice.store.workflow import load_workflow, save_workflow


class AcceptanceApplications(ApplicationBackend):
    """Keep ownership until the harness closes its own verified GUI windows."""
    def __init__(self):
        super().__init__()
        self.handles = []

    def release(self, handle):
        self.handles.append(handle)

    def close(self):
        failed = False
        for handle in self.handles:
            try:
                self.terminate(handle)
            except Exception:
                failed = True
        self.handles.clear()
        if failed:
            raise RuntimeError("An owned acceptance application could not be closed.")


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError("The expected capture evidence did not arrive.")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def execute():
    runs = (REPO_ROOT / "runs").resolve()
    sandbox = (runs / f"phase5-project-smoke-{uuid4().hex}").resolve()
    if runs != REPO_ROOT / "runs" or sandbox.parent != runs or not sandbox.is_relative_to(REPO_ROOT):
        raise RuntimeError("The test sandbox must stay inside this repository.")
    sandbox.mkdir(parents=True, exist_ok=False)
    teaching, replay, artifacts = (sandbox / name for name in ("teaching project", "replay project", "artifacts"))
    for directory in (teaching, replay, artifacts):
        directory.mkdir()
    test_text = (
        "from pathlib import Path\n"
        "def test_project_context():\n"
        "    assert Path.cwd().resolve() == Path(__file__).resolve().parent\n"
        "    assert sum([2, 3, 5]) == 10\n"
    )
    for directory in (teaching, replay):
        (directory / "test_project.py").write_text(test_text, encoding="utf-8")
    session = RecordingSession("Phase 5 project acceptance", roots=[teaching])
    windows = WindowsCapture(session.emit, interval=0.1, capture_input=False)
    filesystem = FilesystemCapture(session, [teaching], context_provider=windows.current_context)
    spool = artifacts / "shell-events.jsonl"
    terminal = TerminalCapture(session, spool, context_provider=windows.current_context)
    applications, explorer = AcceptanceApplications(), WindowsExplorer()
    started = []
    report = {"status": "failed", "stage": "capture", "checks": {}, "sandbox": str(sandbox)}
    try:
        if not explorer.check_available():
            raise RuntimeError("Explorer integration is unavailable.")
        for adapter in (windows, filesystem, terminal):
            adapter.start()
            started.append(adapter)
        explorer.open_directory(teaching)

        def folder_event():
            return next((event for event in session.snapshot()["events"]
                         if event["surface"] == "filesystem" and event["kind"] == "open_directory"
                         and Path(event["payload"].get("path", "")).resolve() == teaching), None)

        wait_until(lambda: explorer.directory_is_open(teaching) and folder_event())
        folder_id = folder_event()["id"]
        report["checks"]["captured_actual_explorer_folder"] = True
        report["stage"] = "teaching_project"
        application_event = ApplicationCapture(session, backend=applications,
                                               evidence_dir=artifacts / "teaching-application", timeout=30).record_application(
            "vscode", path=teaching, cwd=teaching)
        if not application_event or application_event["payload"].get("verified") is not True:
            raise RuntimeError("The teaching workspace did not verify.")
        report["checks"]["captured_vscode_workspace_api"] = True
        report["stage"] = "teaching_command"
        script = artifacts / "teach.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f". {ps_literal(REPO_ROOT / 'scripts/ApprenticeShell.ps1')}\n"
            f"Enable-ApprenticeRecording -Path {ps_literal(spool)}\n"
            f"Set-Location -LiteralPath {ps_literal(teaching)}\n"
            f"Invoke-ApprenticeCommand -Executable {ps_literal(sys.executable)} "
            "-ArgumentList @('-m','pytest','-q','--junitxml','report.xml') "
            f"-EffectPaths @({ps_literal(teaching / 'report.xml')})\n"
            "$commandExit = $LASTEXITCODE\nDisable-ApprenticeRecording\nexit $commandExit\n", encoding="utf-8-sig")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        completed = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                                    "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                   cwd=teaching, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if completed.returncode != 0:
            raise RuntimeError("The taught command failed.")
        wait_until(lambda: any(event["kind"] == "command_completed" for event in session.snapshot()["events"]))
        captured = session.snapshot()["events"]
        command_event = next(event for event in captured if event["kind"] == "command_started")
        command_done = next(event for event in captured if event["kind"] == "command_completed")
        if (command_event["payload"]["args"] != ["-m", "pytest", "-q", "--junitxml", "report.xml"]
                or Path(command_event["payload"]["cwd"]).resolve() != teaching
                or command_done["payload"]["exit_code"] != 0):
            raise RuntimeError("The actual argv/cwd/completion capture differs from the command.")
        report["checks"]["captured_exact_argv_cwd_exit"] = True
        session.pause()
        for adapter in reversed(started):
            adapter.stop()
        started.clear()
        session_path = session.save(artifacts / "session.json")
        event_count = len(session.snapshot()["events"])
        report["stage"] = "review"
        draft = build(session_path)
        save_workflow(draft, artifacts / "draft.yaml")
        keep = {folder_id, application_event["id"], command_event["id"], command_done["id"]}
        review = {"description": "Open a different project and verify its local test report.",
                  "parameters": {**draft["parameters"], "python_executable": {"type": "file", "example": sys.executable}},
                  "review_notes": [], "events": {}}
        for event in captured:
            if event["id"] not in keep:
                review["events"][event["id"]] = {"ignore": True, "reason": "Incidental context/background activity outside this explicit acceptance task."}
        for step in draft["steps"]:
            primary = step.get("source_events", [None])[0]
            if primary not in {folder_id, application_event["id"], command_event["id"]}:
                continue
            replacement = {key: value for key, value in step.items() if key not in ("id", "source_events", "source_steps")}
            replacement["review_notes"] = []
            if primary == application_event["id"]:
                replacement["timeout_ms"] = 30000
            elif primary == command_event["id"]:
                replacement.update(executable="{python_executable}", timeout_ms=120000,
                                   artifacts=[{"path": "{workspace}/report.xml", "save_as": "test_results"}])
            review["events"][primary] = {"step": replacement}
        review_path = artifacts / "review.yaml"
        review_path.write_text(yaml.safe_dump(review, sort_keys=False), encoding="utf-8")
        reviewed = SystemWorkflow.model_validate(build(session_path, review_path=review_path, ready=True))
        if [step.surface for step in reviewed.steps] != ["filesystem", "application", "terminal"]:
            raise RuntimeError("The reviewed task did not preserve the three demonstrated actions.")
        save_workflow(reviewed, artifacts / "workflow.yaml")
        supplied = {name: parameter.example for name, parameter in reviewed.parameters.items()}
        supplied.update(workspace=str(replay), python_executable=sys.executable)
        report["checks"]["validated_reviewed_parameterized_workflow"] = True
        report["stage"] = "replay"
        audit = Audit(artifacts / "audit.jsonl")
        approvals = []

        async def confirm(request):
            # --execute and tool approval authorize these known sandbox effects.
            # Exercise the production approval points as part of acceptance.
            approvals.append((request.kind, request.step_id))
            return True

        control = Control(timeout_ms=30000)
        engine = SurfaceExecutor(artifacts / "replay", confirm=confirm, control=control, audit=audit,
                                 application=ApplicationExecutor(artifacts / "replay", backend=applications, control=control))
        result = asyncio.run(engine.run(reviewed, supplied))
        if result.status != "completed" or result.completed_steps != (1, 2, 3):
            raise RuntimeError("Production replay did not verify all three actions.")
        document = ET.parse(replay / "report.xml")
        suites = document.findall(".//testsuite")
        if (sum(int(suite.get("tests", "0")) for suite in suites) != 1
                or any(int(suite.get("failures", "0")) or int(suite.get("errors", "0"))
                       or int(suite.get("skipped", "0")) for suite in suites)):
            raise RuntimeError("The fresh replay report did not prove passing tests.")
        if approvals != [("workflow", None), ("step", 2), ("step", 3)]:
            raise RuntimeError("Expected workflow/launch/command confirmations were not exercised.")
        if not explorer.directory_is_open(replay):
            raise RuntimeError("The replay directory did not open in Explorer.")
        if len(session.snapshot()["events"]) != event_count:
            raise RuntimeError("Replay entered the teaching session.")
        audit_text = (artifacts / "audit.jsonl").read_text(encoding="utf-8")
        if str(teaching) in audit_text or str(replay) in audit_text or "python_executable" in audit_text:
            raise RuntimeError("Audit exposed parameter values.")
        report["checks"].update(replayed_different_project=True, three_verified_steps=True,
                                 fresh_test_report=True, required_confirmations=True,
                                 replay_excluded_from_teaching=True, value_free_audit=True)
        report.update(status="completed", stage="completed")
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["reason"] = "Acceptance failed; inspect this sandbox's private evidence."
    finally:
        session.pause()
        for adapter in reversed(started):
            try:
                adapter.stop()
            except Exception:
                report.update(status="failed", reason="An acceptance capture adapter did not close cleanly.")
        for cleanup in (applications.close, explorer.close):
            try:
                cleanup()
            except Exception:
                report.update(status="failed", reason="Acceptance resource cleanup did not verify.")
        session.close()
        if not (artifacts / "session.json").exists():
            session.save(artifacts / "session.json")
        (artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Only owned VS Code windows were closed. Explorer sandbox tabs remain open.")
    return 0 if report["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Perform actual Windows capture and replay in fresh sandbox projects.")
    args = parser.parse_args()
    if args.execute:
        return execute()
    print("\n".join(dry_run(load_workflow(REPO_ROOT / "workflows/project_tests.yaml"))))
    print("Preview only. --execute creates two runs/ projects, opens Explorer/VS Code, and runs local tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
