"""Portable tests for Windows capture semantics, privacy, and verification."""
import json
from types import SimpleNamespace

import pytest

from apprentice.adapters.windows import WindowsAdapterError, WindowsCapture, WindowsExplorer


class FakeBackend:
    available = True

    def __init__(self):
        self.context = {"app": "explorer.exe", "process_id": 42, "window_id": 99}
        self.visible_folders = []
        self.launched = []
        self.closed = False

    def foreground(self):
        return dict(self.context)

    def folders(self):
        return list(self.visible_folders)

    def open_directory(self, path):
        self.launched.append(path)

    def close(self):
        self.closed = True


def collector():
    events = []

    def emit(surface, kind, payload=None, **metadata):
        events.append({"surface": surface, "kind": kind, "payload": payload, **metadata})

    return events, emit


def test_directory_open_requires_shell_folder_evidence(tmp_path):
    backend = FakeBackend()
    explorer = WindowsExplorer(backend)
    explorer.open_directory(tmp_path)
    assert backend.launched == [tmp_path.resolve()]
    assert not explorer.directory_is_open(tmp_path)
    backend.visible_folders = [{"window_id": 99, "path": str(tmp_path)}]
    assert explorer.directory_is_open(tmp_path)
    explorer.close()
    assert backend.closed
    assert not explorer.available
    with pytest.raises(WindowsAdapterError):
        explorer.open_directory(tmp_path)


def test_directory_launch_rejects_files_and_unavailable_platform(tmp_path):
    backend = FakeBackend()
    backend.available = False
    with pytest.raises(WindowsAdapterError):
        WindowsExplorer(backend).open_directory(tmp_path)
    backend.available = True
    file = tmp_path / "report.csv"
    file.write_text("data")
    with pytest.raises(WindowsAdapterError):
        WindowsExplorer(backend).open_directory(file)
    assert not backend.launched


def test_folder_navigation_recorded_without_filesystem_mutation(tmp_path):
    backend = FakeBackend()
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    backend.visible_folders = [{"window_id": 99, "path": str(first)},
                               {"window_id": 101, "path": str(second)}]
    capture.poll_once()
    capture.poll_once()
    assert [event["kind"] for event in events] == ["focus", "open_directory"]
    assert events[1]["payload"]["path"] == str(first)
    backend.visible_folders[0]["path"] = str(second)
    capture.poll_once()
    assert [event["payload"]["path"] for event in events if event["kind"] == "open_directory"] == [str(first), str(second)]
    assert list(first.iterdir()) == list(second.iterdir()) == []
    assert events[1]["evidence"]["verification"] == "shell_folder"


def test_ambiguous_explorer_tabs_are_review_evidence(tmp_path):
    backend = FakeBackend()
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    backend.visible_folders = [{"window_id": 99, "path": str(tmp_path / "a")},
                               {"window_id": 99, "path": str(tmp_path / "b")}]
    capture.poll_once()
    capture.poll_once()
    assert [event["kind"] for event in events] == ["focus", "explorer_navigation_uncertain"]
    assert events[-1]["surface"] == "desktop"


def test_any_application_focus_is_retained_without_titles():
    backend = FakeBackend()
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    backend.context = {"app": "unknown-editor.exe", "process_id": 500, "window_id": 200}
    capture.poll_once()
    assert events[0]["surface"] == "application"
    assert events[0]["context"] == backend.context
    assert "title" not in json.dumps(events)


def test_password_and_ordinary_typed_chars_are_never_saved():
    backend = FakeBackend()
    backend.context["app"] = "editor.exe"
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    password = "Sensitive!724Secret"
    for char in password:
        capture.on_press(SimpleNamespace(char=char))
    capture.poll_once()
    typing = [event for event in events if event["kind"] == "input_activity"]
    assert typing[0]["payload"] == {"count": len(password), "value": "<redacted>"}
    assert password not in json.dumps(events)
    assert "char" not in json.dumps(events)


def test_shortcuts_are_capture_only_and_altgr_does_not_leak_text():
    backend = FakeBackend()
    backend.context["app"] = "editor.exe"
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    ctrl, alt = SimpleNamespace(name="ctrl_l"), SimpleNamespace(name="alt_r")
    capture.on_press(ctrl)
    capture.on_press(SimpleNamespace(char="v"))
    assert events[-1]["payload"] == {"keys": ["ctrl", "v"]}
    capture.on_press(alt)
    capture.on_press(SimpleNamespace(char="q"))
    capture.on_release(ctrl)
    capture.on_release(alt)
    capture.poll_once()
    assert len([event for event in events if event["kind"] == "shortcut"]) == 1
    assert "q" not in [key for event in events if event["kind"] == "shortcut" for key in event["payload"]["keys"]]


def test_only_enrolled_browser_pids_suppress_duplicate_native_gestures():
    backend = FakeBackend()
    backend.context = {"app": "chrome.exe", "process_id": 50, "window_id": 99}
    events, emit = collector()
    managed = {50}
    capture = WindowsCapture(emit, backend=backend, managed_browser_pids=lambda: managed, capture_input=False)
    left = SimpleNamespace(name="left")
    capture.on_click(10, 20, left, True)
    capture.on_click(10, 20, left, False)
    assert not events
    managed.clear()
    capture.on_click(10, 20, left, True)
    capture.on_click(10, 20, left, False)
    assert events[-1]["kind"] == "click"
    assert events[-1]["evidence"]["target_resolution"] == "review_required"


def test_drag_scroll_and_foreground_at_gesture_time_are_preserved():
    backend = FakeBackend()
    backend.context = {"app": "design.exe", "process_id": 600, "window_id": 100}
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    left = SimpleNamespace(name="left")
    capture.on_click(10, 20, left, True)
    capture.on_click(100, 200, left, False)
    capture.on_scroll(100, 200, 0, -3)
    assert [event["kind"] for event in events] == ["drag", "scroll"]
    assert events[0]["context"]["app"] == "design.exe"
    assert events[0]["evidence"]["cursor_start"] == [10, 20]


def test_stop_closes_backend_and_suppresses_subsequent_input():
    backend = FakeBackend()
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    capture.stop()
    capture.on_scroll(0, 0, 0, 1)
    capture.poll_once()
    assert backend.closed and not events


def test_capture_rejects_invalid_poll_interval():
    with pytest.raises(ValueError):
        WindowsCapture(lambda *args, **kwargs: None, interval=0)


def test_capability_preflight_checks_shell_access_and_closes_failed_helper():
    backend = FakeBackend()
    explorer = WindowsExplorer(backend)
    assert explorer.check_available()

    def fail():
        raise WindowsAdapterError("Windows Explorer integration did not complete the request.")

    backend.folders = fail
    assert not explorer.check_available()
    assert backend.closed
    assert not explorer.available


def test_explorer_probe_failure_keeps_other_app_capture_available():
    backend = FakeBackend()
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)

    def fail():
        raise WindowsAdapterError("Windows Explorer integration did not complete the request.")

    backend.folders = fail
    capture.poll_once()
    assert events[-1]["kind"] == "review_required"
    backend.context = {"app": "other.exe", "process_id": 9, "window_id": 10}
    capture.poll_once()
    capture.on_scroll(0, 0, 0, 1)
    assert events[-1]["surface"] == "desktop"


def test_missing_interactive_desktop_becomes_review_not_anonymous_activity():
    backend = FakeBackend()
    backend.context = {}
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend, capture_input=False)
    capture.poll_once()
    capture.on_scroll(0, 0, 0, 1)
    assert len(events) == 1
    assert events[0]["surface"] == "system"
    assert events[0]["kind"] == "review_required"
    assert events[0]["origin"] == "user"
    with pytest.raises(WindowsAdapterError):
        capture.start()
    assert backend.closed


def test_partial_hook_startup_cleans_up_all_resources(monkeypatch):
    import sys

    backend = FakeBackend()
    backend.context["app"] = "editor.exe"
    listeners = []

    class Listener:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            listeners.append(self)

        def start(self):
            if len(listeners) == 2 and self is listeners[1]:
                raise RuntimeError("Hook could not start.")
            self.started = True

        def stop(self):
            self.stopped = True
            if not self.started:
                raise AttributeError("Hook is not initialized.")

    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace(
        keyboard=SimpleNamespace(Listener=Listener), mouse=SimpleNamespace(Listener=Listener)))
    events, emit = collector()
    capture = WindowsCapture(emit, backend=backend)
    with pytest.raises(RuntimeError):
        capture.start()
    assert backend.closed
    assert all(listener.stopped for listener in listeners)
    assert "win32_event_filter" in listeners[0].kwargs


@pytest.mark.parametrize("flags", [0x02, 0x10, 0x12])
def test_injected_keyboard_events_are_not_recorded(flags):
    assert not WindowsCapture.keyboard_event_filter(0, SimpleNamespace(flags=flags))
    assert WindowsCapture.keyboard_event_filter(0, SimpleNamespace(flags=0x01))


@pytest.mark.parametrize("flags", [0x01, 0x02, 0x03])
def test_injected_mouse_events_are_not_recorded(flags):
    assert not WindowsCapture.mouse_event_filter(0, SimpleNamespace(flags=flags))
    assert WindowsCapture.mouse_event_filter(0, SimpleNamespace(flags=0))
