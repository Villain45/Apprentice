"""Preview or run the real Windows Phase 4 acceptance check.

Preview is the default and has no filesystem or GUI side effects. With --execute,
all files stay in a new runs/phase4-smoke-UUID directory. Explorer opens that test
directory; its windows are left open rather than risking closure of a user's tab.
No screenshots, keystrokes, clipboard contents, or user file contents are captured.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from apprentice.adapters.windows import WindowsAdapterError, WindowsCapture, WindowsExplorer
from apprentice.executor.runtime import Audit, Control, dry_run
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.recorder.filesystem import FilesystemCapture
from apprentice.recorder.session import RecordingSession
from apprentice.store.system_workflow import SystemWorkflow
from apprentice.store.workflow import save_workflow


PAYLOAD = "Phase 4 verified filesystem content.\n"


def workflow() -> SystemWorkflow:
    root = "{sandbox_root}"
    inbox = root + "/inbox"
    archive = root + "/archive"
    original = inbox + "/original.txt"
    copied = inbox + "/copied.txt"
    moved = archive + "/copied.txt"
    renamed = archive + "/verified.txt"
    steps = []

    def path_step(action, path, purpose, state):
        steps.append({"id": len(steps) + 1, "surface": "filesystem", "action": action,
                      "path": path, "purpose": purpose,
                      "postcondition": {"kind": "path_state", "path": path, "state": state}})

    def transfer(action, source, destination, purpose):
        steps.append({"id": len(steps) + 1, "surface": "filesystem", "action": action,
                      "source": source, "destination": destination, "purpose": purpose,
                      "postcondition": {"kind": "path_transferred", "source": source,
                                        "destination": destination, "source_absent": action != "copy_path"}})

    path_step("create_directory", inbox, "Create the test inbox", "directory")
    path_step("create_directory", archive, "Create the test archive", "directory")
    path_step("create_file", original, "Create known test content", "file")
    steps[-1]["content"] = "{payload}"
    transfer("copy_path", original, copied, "Copy and verify test content")
    transfer("move_path", copied, moved, "Move and verify test content")
    transfer("rename_path", moved, renamed, "Rename and verify test content")
    steps.append({"id": len(steps) + 1, "surface": "filesystem", "action": "open_directory",
                  "path": archive, "purpose": "Open the verified archive in Explorer",
                  "postcondition": {"kind": "directory_open", "path": archive}})
    return SystemWorkflow.model_validate({
        "version": 2, "status": "ready", "name": "Windows Phase 4 acceptance",
        "description": "Verify sandboxed filesystem operations and actual Explorer folder navigation.",
        "parameters": {"sandbox_root": {"type": "directory"}, "payload": {"type": "text"}},
        "write_roots": [root], "steps": steps,
    })


def wait_until(predicate, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError("The Windows acceptance observation did not arrive in time.")


def execute() -> int:
    # Resolve and verify the exact destination before making any test directories.
    runs_root = (REPO_ROOT / "runs").resolve()
    sandbox = (runs_root / f"phase4-smoke-{uuid4().hex}").resolve()
    if (runs_root != REPO_ROOT / "runs" or sandbox.parent != runs_root
            or not sandbox.is_relative_to(REPO_ROOT)):
        raise RuntimeError("The acceptance sandbox resolved outside this repository.")
    sandbox.mkdir(parents=True, exist_ok=False)
    teaching_root = sandbox / "teaching"
    replay_root = sandbox / "replay"
    artifacts = sandbox / "artifacts"
    for directory in (teaching_root, replay_root, artifacts):
        directory.mkdir()

    session = RecordingSession("Windows Phase 4 acceptance", roots=[teaching_root])
    windows = WindowsCapture(session.emit, interval=0.1, capture_input=False)
    filesystem = FilesystemCapture(session, [teaching_root], context_provider=windows.current_context)
    probe = WindowsExplorer()
    started = []
    report = {"status": "failed", "checks": {}, "sandbox": str(sandbox)}
    try:
        if not probe.check_available():
            raise WindowsAdapterError("Actual Explorer access is unavailable; no simulated result is accepted.")
        for adapter in (windows, filesystem):
            adapter.start()
            started.append(adapter)

        probe.open_directory(teaching_root)
        wait_until(lambda: probe.directory_is_open(teaching_root))
        report["checks"]["actual_shell_folder"] = True

        def captured_directory():
            return any(event["surface"] == "filesystem" and event["kind"] == "open_directory"
                       and Path(event["payload"].get("path", "")).resolve() == teaching_root
                       for event in session.snapshot()["events"])

        wait_until(captured_directory)
        report["checks"]["directory_navigation_capture"] = True

        # This intentionally generated background write is evidence only. The
        # watcher must preserve observation origin rather than invent a user step.
        observation_file = teaching_root / "background-observation.txt"
        observation_file.write_bytes(b"Acceptance background observation.\n")

        def captured_observation():
            return any(event["surface"] == "filesystem" and event["origin"] == "observation"
                       and Path(event["payload"].get("path", "")).resolve() == observation_file
                       for event in session.snapshot()["events"])

        wait_until(captured_observation)
        report["checks"]["watcher_observation_without_user_action"] = True

        # Recording remains paused until it closes: queued replay effects cannot
        # leak into teaching after execution or during watcher shutdown.
        session.pause()
        events_before_replay = len(session.snapshot()["events"])
        reviewed = workflow()
        save_workflow(reviewed, artifacts / "workflow.yaml")
        audit = Audit(artifacts / "audit.jsonl")
        requests = []

        async def confirm(request):
            # --execute and the surrounding tool approval authorize this concrete
            # isolated test. Production workflow confirmation is still exercised.
            requests.append(request.kind)
            return True

        engine = SurfaceExecutor(artifacts, confirm=confirm, control=Control(timeout_ms=15000), audit=audit)
        result = asyncio.run(engine.run(reviewed, {"sandbox_root": str(replay_root), "payload": PAYLOAD}))
        if result.status != "completed" or result.completed_steps != tuple(range(1, 8)):
            raise RuntimeError("The production surface runtime did not complete every verified step.")
        original = replay_root / "inbox/original.txt"
        final = replay_root / "archive/verified.txt"
        if original.read_bytes() != PAYLOAD.encode() or final.read_bytes() != PAYLOAD.encode():
            raise RuntimeError("Replayed file content did not verify.")
        if (replay_root / "inbox/copied.txt").exists() or (replay_root / "archive/copied.txt").exists():
            raise RuntimeError("A transferred source remains present.")
        if len([event for event in audit.events if event["event"] == "step_verified"]) != 7:
            raise RuntimeError("The audit did not verify all seven production steps.")
        if requests != ["workflow"] or PAYLOAD.strip() in (artifacts / "audit.jsonl").read_text(encoding="utf-8"):
            raise RuntimeError("Confirmation or value-free auditing did not verify.")
        if not probe.directory_is_open(replay_root / "archive"):
            raise RuntimeError("The replayed archive has no actual Shell folder evidence.")
        if len(session.snapshot()["events"]) != events_before_replay:
            raise RuntimeError("Generated replay events entered the teaching session.")
        report["checks"].update({"production_filesystem_replay": True, "file_bytes": True,
                                 "seven_verified_steps": True, "value_free_audit": True,
                                 "replayed_explorer_folder": True, "replay_excluded_from_teaching": True})
        report["status"] = "completed"
    except (OSError, RuntimeError, ValueError):
        # Evidence remains local for diagnosis, without relaying arbitrary desktop
        # errors or paths from unrelated applications into terminal output.
        report["reason"] = "The real Windows acceptance check failed; inspect the private sandbox artifacts."
    finally:
        session.pause()
        for adapter in reversed(started):
            try:
                adapter.stop()
            except Exception:
                report["status"] = "failed"
                report["reason"] = "A capture adapter did not stop cleanly."
        probe.close()
        session.close()
        session.save(artifacts / "session.json")
        (artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Private evidence: {artifacts}")
    print("Explorer test folders remain open; no user windows were closed.")
    return 0 if report["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run actual Explorer and sandbox filesystem checks.")
    args = parser.parse_args()
    if not args.execute:
        print("\n".join(dry_run(workflow())))
        print("Preview only. --execute creates a unique repository runs/ sandbox and opens its folders in Explorer.")
        return 0
    return execute()


if __name__ == "__main__":
    raise SystemExit(main())
