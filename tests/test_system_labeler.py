"""Teach, explicitly review, and replay system work in a different sandbox."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from watchdog.events import DirCreatedEvent, FileModifiedEvent, FileMovedEvent

from apprentice.executor.filesystem import FilesystemExecutor
from apprentice.executor.runtime import Audit
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.labeler.build_system_workflow import build
from apprentice.recorder.filesystem import FilesystemCapture
from apprentice.recorder.session import RecordingSession
from apprentice.store.system_workflow import SystemWorkflow
from apprentice.store.workflow import load_workflow, save_workflow


class FakeExplorer:
    available = True

    def __init__(self):
        self.opened = []
        self.closed = False

    def open_directory(self, path):
        assert Path(path).is_dir()
        self.opened.append(Path(path))

    def directory_is_open(self, path):
        return Path(path) in self.opened

    def close(self):
        self.closed = True


def write_review(tmp_path, data):
    path = tmp_path / "review.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_demonstration_requires_review_then_replays_into_another_root(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    original = recorded / "original.txt"
    original.write_text("important sample contents", encoding="utf-8")
    session = RecordingSession("Organize files", roots=[recorded])
    session.emit("filesystem", "open_directory", {"path": str(recorded)},
                 evidence={"verification": "shell_folder"})
    watcher = FilesystemCapture(session, [recorded])
    folder = recorded / "organized"
    folder.mkdir()
    watcher.on_any_event(DirCreatedEvent(str(folder)))
    renamed = folder / "renamed.txt"
    original.rename(renamed)
    watcher.on_any_event(FileMovedEvent(str(original), str(renamed)))
    watcher.on_any_event(FileModifiedEvent(str(renamed)))  # A background effect, not another edit action.
    session.emit("desktop", "shortcut", {"keys": ["ctrl", "x"]})
    session.emit("application", "focus", {}, origin="observation", context={"app": "explorer.exe"})
    recording = session.save(tmp_path / "session.json")

    draft = build(recording)
    assert draft["status"] == "draft"
    assert draft["steps"][0]["action"] == "open_directory"
    assert draft["steps"][1]["review_notes"]
    assert draft["steps"][2]["review_notes"]
    with pytest.raises(ValueError):
        build(recording, ready=True)

    review = write_review(tmp_path, {"events": {
        2: {"step": {"surface": "filesystem", "action": "create_directory",
                     "path": "{workspace}/organized", "purpose": "Create the destination folder.",
                     "postcondition": {"kind": "path_state", "path": "{workspace}/organized", "state": "directory"}}},
        3: {"step": {"surface": "filesystem", "action": "move_path",
                     "source": "{workspace}/original.txt", "destination": "{workspace}/organized/renamed.txt",
                     "purpose": "Move and rename the demonstrated file.",
                     "postcondition": {"kind": "path_transferred", "source": "{workspace}/original.txt",
                                       "destination": "{workspace}/organized/renamed.txt", "source_absent": True}}},
        4: {"ignore": True, "reason": "The editor's background metadata update is not a separate user action."},
        5: {"ignore": True, "reason": "The observed file move already represents this cut shortcut."},
        6: {"ignore": True, "reason": "Foreground context only; no separate action to replay."},
    }})
    ready = build(recording, review_path=review, ready=True)
    yaml_path = save_workflow(ready, tmp_path / "workflow.yaml")
    workflow = load_workflow(yaml_path, require_ready=True)
    assert [step.source_events for step in workflow.steps] == [[1], [2], [3]]
    replayed = tmp_path / "replayed"
    replayed.mkdir()
    (replayed / "original.txt").write_text("important sample contents", encoding="utf-8")
    explorer = FakeExplorer()
    filesystem = FilesystemExecutor([replayed], explorer=explorer)
    audit = Audit()
    executor = SurfaceExecutor(tmp_path / "run", filesystem=filesystem,
                               confirm=lambda request: True, audit=audit)
    result = asyncio.run(executor.run(workflow, {"workspace": str(replayed)}))
    assert result.status == "completed", result.message
    assert result.completed_steps == (1, 2, 3)
    assert explorer.opened == [replayed]
    assert explorer.closed
    assert not (replayed / "original.txt").exists()
    assert (replayed / "organized" / "renamed.txt").read_text(encoding="utf-8") == "important sample contents"
    assert renamed.read_text(encoding="utf-8") == "important sample contents"
    assert str(replayed) not in json.dumps(audit.events)


def test_known_effects_do_not_become_duplicate_filesystem_actions(tmp_path):
    session = RecordingSession("Open folder", roots=[tmp_path])
    action = session.emit("filesystem", "open_directory", {"path": str(tmp_path)})
    session.emit("filesystem", "modified", {"path": str(tmp_path / "metadata.db")}, origin="observation")
    session.register_effect(tmp_path / "metadata.db", action["id"])
    recording = session.save(tmp_path / "session.json")
    data = build(recording, ready=True)
    assert len(data["steps"]) == 1
    assert data["steps"][0]["source_events"] == [1]


@pytest.mark.parametrize("surface,kind", [("future_device", "capture_operation"), ("terminal", "command_submitted"),
                                           ("clipboard", "changed"), ("desktop", "drag")])
def test_unknown_capabilities_remain_explicit_review_items(tmp_path, surface, kind):
    session = RecordingSession("General system task")
    session.emit(surface, kind, {"supported": False})
    path = session.save(tmp_path / "session.json")
    draft = build(path)
    assert draft["steps"][0]["action"] == kind
    assert draft["steps"][0]["postcondition"]["kind"] == "review_required"
    assert draft["steps"][0]["source_events"] == [1]
    with pytest.raises(ValueError):
        build(path, ready=True)


@pytest.mark.parametrize("context", [{"source_page": 2, "source_frame": {"is_main_frame": True}},
                                     {"source_page": 1, "source_frame": {"is_main_frame": False}}])
def test_other_browser_pages_and_frames_cannot_become_main_page_replay(tmp_path, context):
    session = RecordingSession("Browser task")
    session.emit("browser", "navigate", {"url": "https://example.test", "source_page": 1,
                                          "source_frame": {"is_main_frame": True}})
    session.emit("browser", "type", {"url": "https://example.test", "element": {
        "tag": "input", "type": "text", "label": "Name", "css": "#name"}, "value": "Alice", **context})
    path = session.save(tmp_path / "session.json")
    draft = build(path)
    assert draft["steps"][0]["postcondition"]["kind"] == "review_required"
    assert draft["steps"][0]["surface"] == "system"
    with pytest.raises(ValueError):
        build(path, ready=True)


def test_main_page_browser_recording_stays_parameterized_and_ready(tmp_path):
    session = RecordingSession("Fill browser form")
    session.emit("browser", "navigate", {"url": "https://example.test", "source_page": 1})
    session.emit("browser", "type", {"url": "https://example.test", "source_page": 1,
        "source_frame": {"is_main_frame": True}, "element": {
            "tag": "input", "type": "text", "label": "Name", "css": "#name"}, "value": "Alice"})
    recording = session.save(tmp_path / "session.json")
    ready = build(recording, ready=True)
    workflow = SystemWorkflow.model_validate(ready)
    assert workflow.start_url == "https://example.test"
    assert workflow.steps[0].surface == "browser"
    assert workflow.steps[0].value == "{name}"
    assert workflow.steps[0].source_steps == [2]
    assert workflow.parameters["name"].example == "Alice"


def test_browser_download_and_upload_keep_cross_surface_provenance(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "monthly_report.json").read_text(encoding="utf-8"))
    session = RecordingSession("Monthly report", roots=[tmp_path])
    for event in fixture["steps"]:
        session.emit("browser", event["kind"], {**event, "source_page": 1,
                     "source_frame": {"is_main_frame": True}})
    recording = session.save(tmp_path / "session.json")
    draft = build(recording)
    download_steps = [step for step in draft["steps"] if step["postcondition"]["kind"] == "file_downloaded"]
    assert len(download_steps) == 1
    upload_steps = [step for step in draft["steps"] if step["action"] == "upload"]
    assert len(upload_steps) == 1
    assert upload_steps[0]["value"] == "{" + download_steps[0]["postcondition"]["save_as"] + "}"
    assert upload_steps[0]["id"] in draft["confirm_before"]
    assert draft["parameters"]["password"]["ask_each_time"] is True
    assert "example" not in draft["parameters"]["password"]
