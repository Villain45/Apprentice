"""Real file/browser integration and causal recording boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from apprentice.recorder.browser_session import BrowserCapture
from apprentice.recorder.filesystem import FilesystemCapture
from apprentice.recorder.session import RecordingSession
from apprentice.recorder.replay_guard import ReplayGuard, replay_active


@pytest.fixture(autouse=True)
def isolated_replay_markers(tmp_path, monkeypatch):
    # Public executors and default session probes share this test's marker root,
    # while concurrent development runs cannot suspend a synthetic recording.
    from apprentice.recorder import replay_guard
    monkeypatch.setattr(replay_guard, "MARKER_ROOT", tmp_path / ".active-replays")


def test_ordered_concurrent_session_and_secrets(tmp_path):
    session = RecordingSession("General task", roots=[tmp_path], clock=lambda: 5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: session.emit("native", "shortcut", {"count": index}), range(40)))
    session.emit("browser", "type", {"element": {"sensitive": True}, "value": "private-password"})
    session.emit("native", "input_activity", {"characters": "private-native", "clipboard": "copied-secret"})
    snapshot = session.snapshot()
    assert [event["id"] for event in snapshot["events"]] == list(range(1, 43))
    timestamps = [event["timestamp_ns"] for event in snapshot["events"]]
    assert len(set(timestamps)) == len(timestamps)
    assert timestamps == sorted(timestamps)
    destination = session.save(tmp_path / "recording.json")
    saved = destination.read_text(encoding="utf-8")
    assert "private-password" not in saved
    assert "private-native" not in saved
    assert "copied-secret" not in saved
    with pytest.raises(FileExistsError):
        session.save(destination)
    assert json.loads(saved)["version"] == 2


def test_pause_excluded_apps_replay_and_enrichment():
    session = RecordingSession("Task", excluded_apps=["password-manager.exe"])
    assert session.emit("native", "click", context={"app": "Password-Manager.EXE"}) is None
    assert session.emit("browser", "click", origin="replay") is None
    session.pause()
    assert session.emit("native", "click") is None
    session.resume()
    event = session.emit("native", "unknown", {"activity": "unsupported operation"})
    session.annotate(event["id"], payload={"after_state": {"visible": True}}, evidence=["evidence.png"])
    enriched = session.snapshot()["events"][0]
    assert enriched["timestamp_ns"] == event["timestamp_ns"]
    assert enriched["kind"] == "unknown"  # Unsupported actions remain present for review.
    assert enriched["evidence"] == ["evidence.png"]
    structured = session.emit("filesystem", "open_directory", {"path": "example"},
                             evidence={"verification": "shell_folder", "window_id": 1})
    assert structured["evidence"] == [{"verification": "shell_folder", "window_id": 1}]
    session.failure("test", "Capture unavailable.")
    assert session.snapshot()["events"][-1]["kind"] == "review_required"
    session.close()
    assert session.emit("native", "click") is None
    with pytest.raises(ValueError):
        session.resume()


def test_effect_correlation_is_evidence_not_an_extra_action(tmp_path):
    counter = iter([0, 1, 7_000_000_000])
    session = RecordingSession("Task", clock=lambda: next(counter))
    cause = session.emit("browser", "download", {"filename": "report.csv"})
    session.emit("filesystem", "created", {"path": str(tmp_path / "report.csv")}, origin="observation")
    session.register_effect(tmp_path / "report.csv", cause["id"])
    later = session.emit("filesystem", "modified", {"path": str(tmp_path / "report.csv")}, origin="observation")
    events = session.snapshot()["events"]
    assert events[1]["payload"]["correlated_to"] == cause["id"]
    assert events[1]["origin"] == "observation"
    assert "correlated_to" not in later["payload"]  # Unrelated later writes remain reviewable.
    with pytest.raises(ValueError):
        session.register_effect(tmp_path / "other", events[1]["id"])


def test_watch_events_do_not_invent_intent_or_record_file_contents(tmp_path):
    session = RecordingSession("Task", roots=[tmp_path])
    watcher = FilesystemCapture(session, [tmp_path], context_provider=lambda: {"app": "editor"})
    source = tmp_path / "document.txt"
    source.write_text("private file contents", encoding="utf-8")
    watcher.on_any_event(FileCreatedEvent(str(source)))
    watcher.on_any_event(FileModifiedEvent(str(source)))
    destination = tmp_path / "renamed.txt"
    source.rename(destination)
    watcher.on_any_event(FileMovedEvent(str(source), str(destination)))
    watcher.on_any_event(FileCreatedEvent(str(tmp_path / ".env")))
    watcher.on_any_event(FileCreatedEvent(str(tmp_path / "recordings" / "session.json")))
    events = session.snapshot()["events"]
    assert [event["kind"] for event in events] == ["created", "modified", "moved"]
    assert all(event["origin"] == "observation" for event in events)
    assert events[0]["payload"]["size"] == len("private file contents")
    assert events[2]["payload"]["destination"] == str(destination)
    assert "private file contents" not in json.dumps(events)


def test_real_watcher_records_create_and_rename_and_stops(tmp_path):
    session = RecordingSession("Task", roots=[tmp_path])
    watcher = FilesystemCapture(session, [tmp_path])
    watcher.start()
    try:
        original = tmp_path / "original.txt"
        original.write_text("sample", encoding="utf-8")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
                event["kind"] == "created" for event in session.snapshot()["events"]):
            time.sleep(0.02)
        renamed = tmp_path / "renamed.txt"
        original.rename(renamed)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
                event["kind"] == "moved" for event in session.snapshot()["events"]):
            time.sleep(0.02)
        kinds = {event["kind"] for event in session.snapshot()["events"]}
        assert {"created", "moved"} <= kinds
    finally:
        watcher.stop()
    assert not watcher.observer.is_alive()


def test_semantic_browser_bridge_keeps_sensitive_inputs_and_frames_safe(tmp_path):
    child = tmp_path / "child.html"
    child.write_text('<label>Password <input id="password" type="password"></label>', encoding="utf-8")
    main = tmp_path / "main.html"
    main.write_text(f'<label>Name <input id="name"></label><button id="done">Done</button>'
                    f'<iframe src="{child.as_uri()}"></iframe>', encoding="utf-8")
    session = RecordingSession("Browser task")
    browser = BrowserCapture(session, tmp_path / "evidence", main.as_uri(), headless=True, settle_ms=0)
    browser.start()
    try:
        browser.page.locator("#name").fill("Alice")
        browser.page.locator("#done").click()
        password = browser.page.frame_locator("iframe").locator("#password")
        password.fill("private-browser-password")
        password.press("Tab")
        browser.pump(50)
        browser.flush(force=True)
        events = session.snapshot()["events"]
        name_event = next(event for event in events if event["kind"] == "type"
                          and event["payload"].get("value") == "Alice")
        assert name_event["payload"]["source_frame"]["is_main_frame"] is True
        secret_event = next(event for event in events if event["kind"] == "type"
                            and event["payload"].get("sensitive"))
        assert secret_event["payload"]["source_frame"]["is_main_frame"] is False
        assert secret_event["payload"]["value"] == "<redacted>"
        assert "private-browser-password" not in json.dumps(events)
        assert name_event["evidence"]
        browser.page.locator("#done").click()
        session.pause()
        shots_before = len(list((tmp_path / "evidence").glob("*.png")))
        browser.flush(force=True)
        assert len(list((tmp_path / "evidence").glob("*.png"))) == shots_before
        session.resume()
        popup = browser.context.new_page()
        popup.goto(main.as_uri())
        popup.locator("#done").click()
        browser.pump(50)
        assert any(event["payload"].get("source_page") == 2 for event in session.snapshot()["events"])
    finally:
        browser.stop()


def test_cli_adapter_failure_is_explicit_and_other_adapters_stop(tmp_path, monkeypatch):
    from apprentice.adapters import windows
    from apprentice.recorder import record_system

    stopped = []

    class UnavailableWindows:
        def __init__(self, emit, **options):
            self.emit = emit

        def start(self):
            self.emit("system", "review_required", {"adapter": "windows", "reason": "Foreground unavailable."})
            raise RuntimeError("private raw native error")

        def stop(self):
            stopped.append("windows")

        def current_context(self):
            return {}

    class WorkingFilesystem:
        def __init__(self, session, roots, **options):
            self.session = session

        def start(self):
            self.session.emit("filesystem", "created", {"path": str(tmp_path / "folder"), "is_directory": True},
                              origin="observation")

        def stop(self):
            stopped.append("filesystem")

    monkeypatch.setattr(windows, "WindowsCapture", UnavailableWindows)
    monkeypatch.setattr(record_system, "FilesystemCapture", WorkingFilesystem)
    monkeypatch.setattr(record_system, "_console_controls", lambda session, stop: None)
    args = SimpleNamespace(output=tmp_path / "output", name="task", root=[str(tmp_path)], exclude_app=[],
                           url=None, headless=True, no_input=True, duration=0.01)
    destination = record_system.record(args)
    data = json.loads(destination.read_text(encoding="utf-8"))
    assert [event["kind"] for event in data["events"]] == ["review_required", "created"]
    assert stopped == ["windows", "filesystem"]
    assert "private raw native error" not in destination.read_text(encoding="utf-8")


def test_whole_session_suspends_for_live_replays_then_resumes(tmp_path):
    marker_root = tmp_path / "replay-markers"
    session = RecordingSession("Task", replay_probe=lambda: replay_active(marker_root))
    first = session.emit("filesystem", "open_directory", {"path": str(tmp_path)})
    assert first is not None
    before = session.capture_epoch
    with ReplayGuard(marker_root) as guard:
        assert guard.path.exists()
        metadata = json.loads(guard.path.read_text(encoding="utf-8"))
        assert set(metadata) == {"pid", "create_time"}
        assert replay_active(marker_root)
        assert not session.capturing
        assert session.capture_epoch > before
        assert session.emit("filesystem", "created", {"path": str(tmp_path / "created")}, origin="observation") is None
        assert session.emit("desktop", "click") is None
    assert not replay_active(marker_root)
    assert list(marker_root.iterdir()) == []
    assert session.capturing
    assert session.emit("filesystem", "open_directory", {"path": str(tmp_path)})["id"] == 2


def test_stale_and_pid_reused_replay_markers_do_not_suspend_capture(tmp_path, monkeypatch):
    from apprentice.recorder import replay_guard

    marker_root = tmp_path / "markers"
    marker_root.mkdir()
    marker = marker_root / "12345-0123456789abcdef0123456789abcdef.json"
    marker.write_text(json.dumps({"pid": 12345, "create_time": 10.0}), encoding="utf-8")

    class Process:
        def __init__(self, pid):
            assert pid == 12345

        def create_time(self):
            return 20.0  # A new process reused the old PID.

        def is_running(self):
            return True

        def status(self):
            return replay_guard.psutil.STATUS_RUNNING

    monkeypatch.setattr(replay_guard.psutil, "Process", Process)
    assert not replay_active(marker_root)
    marker.write_text(json.dumps({"pid": 12345, "create_time": 20.0}), encoding="utf-8")
    assert replay_active(marker_root)
    monkeypatch.setattr(Process, "is_running", lambda self: False)
    assert not replay_active(marker_root)
    # Invalid or partial files from a crash are never accepted as a live marker.
    marker.write_text("{", encoding="utf-8")
    assert not replay_active(marker_root)


def test_browser_takes_no_snapshots_during_replay(tmp_path):
    active = False
    session = RecordingSession("Task", replay_probe=lambda: active)
    browser = BrowserCapture(session, tmp_path, "https://example.test", headless=True)

    class Frame:
        def evaluate(self, script):
            raise AssertionError("Replay must suspend DOM snapshots.")

    class Page:
        def screenshot(self, **options):
            raise AssertionError("Replay must suspend screenshots.")

    active = True
    assert browser._state(Frame()) is None
    assert browser._shot(Page()) is None
    browser._pending = [(1, Page(), Frame(), "click")]
    browser.flush(force=True)
    assert browser._pending == []


def test_explicit_watch_root_under_runs_is_not_ignored(tmp_path):
    root = tmp_path / "runs" / "smoke"
    root.mkdir(parents=True)
    session = RecordingSession("Task", roots=[root], replay_probe=lambda: False)
    watcher = FilesystemCapture(session, [root])
    folder = root / "created"
    watcher.on_any_event(FileCreatedEvent(str(folder)))
    watcher.on_any_event(FileCreatedEvent(str(root / "recordings" / "private.json")))
    assert [event["payload"]["path"] for event in session.snapshot()["events"]] == [str(folder)]


def test_public_executor_suspends_recording_of_its_own_effects(tmp_path, monkeypatch):
    from apprentice.executor.filesystem import FilesystemExecutor
    from apprentice.executor.surfaces import SurfaceExecutor
    from apprentice.recorder import replay_guard
    from apprentice.store.system_workflow import SystemWorkflow

    monkeypatch.setattr(replay_guard, "MARKER_ROOT", tmp_path / "active-replays")
    root = tmp_path / "workspace"
    root.mkdir()
    session = RecordingSession("Teach task")

    class ObservedFilesystem(FilesystemExecutor):
        async def step(self, step, values):
            assert replay_guard.replay_active()
            assert session.emit("filesystem", "created", {"path": str(root / "created")}, origin="observation") is None
            await super().step(step, values)

    workflow = SystemWorkflow.model_validate({
        "version": 2, "status": "ready", "name": "Create folder", "description": "A reviewed creation.",
        "parameters": {"workspace": {"type": "directory"}}, "write_roots": ["{workspace}"],
        "steps": [{"id": 1, "surface": "filesystem", "action": "create_directory", "path": "{workspace}/created",
                   "purpose": "Create a folder.", "postcondition": {
                       "kind": "path_state", "path": "{workspace}/created", "state": "directory"}}]})
    executor = SurfaceExecutor(tmp_path / "run", filesystem=ObservedFilesystem([root]), confirm=lambda request: True)
    result = asyncio.run(executor.run(workflow, {"workspace": str(root)}))
    assert result.status == "completed", result.message
    assert (root / "created").is_dir()
    assert session.snapshot()["events"] == []
    assert not replay_guard.replay_active()
    assert session.emit("desktop", "click") is not None
