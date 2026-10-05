"""One ordered, privacy-aware event stream for all recording adapters.

An observation is evidence, not permission to replay a filesystem change. New
surfaces can use this envelope without pretending that their actions are already
supported by an executor.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
from uuid import uuid4

from apprentice.recorder.replay_guard import replay_active


REDACTED = "<redacted>"
_SECRET_KEYS = {"password", "passwd", "secret", "token", "access_token", "api_key",
                "authorization", "cookie", "cookies", "clipboard", "clipboard_text"}
_RAW_INPUT_KEYS = {"keystrokes", "raw_keys", "typed_keys", "character", "characters"}
_SENSITIVE_VALUES = {"value", "text", "filename", "files", "content", "contents",
                     "command", "arguments", "args", "input"}


def sanitize(value: Any, *, sensitive: bool = False) -> Any:
    """Sanitize recursively before data enters memory or a saved session.

    Browser adapters deliberately supply complete nonsecret field values. Native
    adapters supply activity metadata only; raw key streams and clipboard data
    are always discarded even when an adapter forgets to mark them sensitive.
    """
    if isinstance(value, dict):
        sensitive = sensitive or value.get("sensitive") is True or (
            isinstance(value.get("element"), dict) and value["element"].get("sensitive") is True)
        clean = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Recording metadata keys must be strings.")
            normalized = key.casefold().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized in _RAW_INPUT_KEYS:
                clean[key] = REDACTED
            elif sensitive and normalized in _SENSITIVE_VALUES:
                clean[key] = REDACTED
            else:
                clean[key] = sanitize(item, sensitive=sensitive)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize(item, sensitive=sensitive) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not (-float("inf") < value < float("inf")):
            raise ValueError("Recording metadata must contain finite numbers.")
        return value
    if isinstance(value, Path):
        return str(value)
    raise ValueError("Recording metadata must be JSON-compatible.")


def _app_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _evidence(value) -> list:
    references = [value] if isinstance(value, dict) else list(value or ())
    if any(not isinstance(item, (str, dict)) for item in references):
        raise ValueError("Evidence must be file references or structured metadata.")
    return sanitize(references)


class RecordingSession:
    def __init__(self, name: str, *, roots: Iterable[str | Path] = (),
                 excluded_apps: Iterable[str] = (),
                 clock: Callable[[], int] = time.monotonic_ns,
                 replay_probe: Callable[[], bool] = replay_active):
        if not name.strip():
            raise ValueError("A recording needs a name.")
        self.name = name
        self.roots = [str(Path(root).resolve()) for root in roots]
        self.session_id = str(uuid4())
        self.recorded_at = datetime.now(timezone.utc).isoformat()
        self._clock = clock
        self._replay_probe = replay_probe
        self._excluded_apps = {_app_name(app) for app in excluded_apps}
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._effects: dict[str, tuple[int, int]] = {}
        self._last_time = -1
        self._paused = False
        self._closed = False
        self._capture_epoch = 0
        self._replay_suspended = False

    @property
    def capturing(self) -> bool:
        """Whole-session suspension prevents teaching Apprentice's own replay effects."""
        replay = bool(self._replay_probe())
        with self._lock:
            if replay != self._replay_suspended:
                self._replay_suspended = replay
                self._capture_epoch += 1
            return not (self._closed or self._paused or replay)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def capture_epoch(self) -> int:
        """Adapters invalidate cached evidence when recording pauses or resumes."""
        self.capturing
        with self._lock:
            return self._capture_epoch

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._capture_epoch += 1

    def resume(self) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("The recording has ended.")
            self._paused = False
            self._capture_epoch += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def emit(self, surface: str, kind: str, payload: dict | None = None, *,
             origin: str = "user", context: dict | None = None,
             evidence: Iterable[str | dict] | dict | None = None) -> dict | None:
        if origin not in {"user", "observation", "replay"}:
            raise ValueError("Unknown recording event origin.")
        if not surface or not kind:
            raise ValueError("Events need a surface and a kind.")
        if not self.capturing:
            return None
        context = sanitize(context or {})
        payload = sanitize(payload or {})
        evidence = _evidence(evidence)
        with self._lock:
            app = context.get("app") or context.get("executable") or context.get("process_name")
            if self._closed or self._paused or origin == "replay":
                return None
            if isinstance(app, str) and _app_name(app) in self._excluded_apps:
                return None
            timestamp = max(int(self._clock()), self._last_time + 1)
            self._last_time = timestamp
            event = {"id": len(self._events) + 1, "timestamp_ns": timestamp,
                     "surface": surface, "kind": kind, "origin": origin,
                     "payload": payload, "context": context, "evidence": evidence}
            if surface == "filesystem" and origin == "observation":
                self._correlate(event)
            self._events.append(event)
            return deepcopy(event)

    def annotate(self, event_id: int, *, payload: dict | None = None,
                 evidence: Iterable[str | dict] | dict | None = None) -> bool:
        """Enrich settled evidence without changing the event's original order."""
        if not self.capturing:
            return False
        clean = sanitize(payload or {})
        references = _evidence(evidence)
        with self._lock:
            if not 1 <= event_id <= len(self._events):
                return False
            event = self._events[event_id - 1]
            event["payload"].update(clean)
            for reference in references:
                if reference not in event["evidence"]:
                    event["evidence"].append(reference)
            return True

    def register_effect(self, path: str | Path, cause_event_id: int, *, duration_seconds: float = 5) -> None:
        """Link known outcomes to their semantic cause instead of replaying twice."""
        with self._lock:
            if not 1 <= cause_event_id <= len(self._events):
                raise ValueError("The effect needs an existing cause event.")
            if self._events[cause_event_id - 1]["origin"] != "user":
                raise ValueError("An observed effect needs a user action as its cause.")
            if not 0 < duration_seconds < float("inf"):
                raise ValueError("Effect correlation needs a positive finite duration.")
            timestamp = self._events[cause_event_id - 1]["timestamp_ns"]
            self._effects[_path_key(path)] = (cause_event_id, timestamp + int(duration_seconds * 1_000_000_000))
            for event in self._events:
                if event["surface"] == "filesystem" and event["origin"] == "observation":
                    self._correlate(event)

    def _correlate(self, event: dict) -> None:
        payload = event["payload"]
        for field in ("path", "destination"):
            path = payload.get(field)
            if isinstance(path, str) and path:
                effect = self._effects.get(_path_key(path))
                if effect is not None:
                    cause, deadline = effect
                    cause_time = self._events[cause - 1]["timestamp_ns"]
                    if not cause_time <= event["timestamp_ns"] <= deadline:
                        continue
                    payload["correlated_to"] = cause
                    return

    def failure(self, adapter: str, reason: str = "Capture adapter unavailable.") -> dict | None:
        # Callers supply a stable message, never exception text or captured values.
        return self.emit("system", "review_required", {"adapter": adapter, "reason": reason})

    def snapshot(self) -> dict:
        with self._lock:
            return {"version": 2, "session_id": self.session_id, "name": self.name,
                    "recorded_at": self.recorded_at, "roots": list(self.roots),
                    "events": deepcopy(self._events)}

    def save(self, path: str | Path, *, overwrite: bool = False) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            raise FileExistsError("A recording already exists at this path.")
        data = json.dumps(self.snapshot(), ensure_ascii=False, indent=2, allow_nan=False)
        descriptor, temporary = tempfile.mkstemp(prefix=".session-", suffix=".tmp",
                                                dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination
