"""Watch filesystem effects without mistaking background writes for user intent."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Callable, Iterable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from apprentice.recorder.session import RecordingSession


IGNORED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "recordings", "runs", ".active-replays"}


class FilesystemCapture(FileSystemEventHandler):
    def __init__(self, session: RecordingSession, roots: Iterable[str | Path], *,
                 context_provider: Callable[[], dict] | None = None,
                 excluded_paths: Iterable[str | Path] = (), observer=None,
                 debounce: float = 0.2):
        self.session = session
        self.roots = list(dict.fromkeys(Path(root).resolve() for root in roots))
        self.context_provider = context_provider or (lambda: {})
        self.excluded_paths = [Path(path).resolve() for path in excluded_paths]
        self.observer = observer if observer is not None else Observer()
        self.debounce = debounce
        self._recent: dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._started = False

    def _ignored(self, path: str) -> bool:
        candidate = Path(path).resolve()
        matches = [root for root in self.roots if candidate == root or root in candidate.parents]
        if not matches:
            return True
        # An explicitly selected sandbox under runs/ is still a valid teaching
        # root. Ignore generated names inside that root, not unrelated ancestors.
        root = max(matches, key=lambda item: len(item.parts))
        names = {part.casefold() for part in candidate.relative_to(root).parts}
        if names & IGNORED_PARTS or any(name == ".env" or name.startswith(".env.") for name in names):
            return True
        return any(candidate == excluded or excluded in candidate.parents for excluded in self.excluded_paths)

    def on_any_event(self, event) -> None:
        kind = getattr(event, "event_type", "")
        if kind not in {"created", "modified", "moved", "deleted"}:
            return
        if kind == "modified" and event.is_directory:
            return  # Directory metadata changes add no independently replayable intent.
        path = os.fsdecode(event.src_path)
        destination = os.fsdecode(getattr(event, "dest_path", ""))
        if self._ignored(path) and (not destination or self._ignored(destination)):
            return
        key = (kind, path, destination)
        now = time.monotonic()
        with self._lock:
            previous = self._recent.get(key)
            if previous is not None and now - previous < self.debounce:
                return
            self._recent[key] = now
            if len(self._recent) > 1000:
                self._recent = {item: timestamp for item, timestamp in self._recent.items()
                                if now - timestamp < max(self.debounce, 1.0)}
        payload = {"action": kind, "path": path, "is_directory": bool(event.is_directory)}
        if destination:
            payload["destination"] = destination
        inspect_path = Path(destination or path)
        if not event.is_directory and kind != "deleted":
            try:
                payload["size"] = inspect_path.stat().st_size
            except OSError:
                payload["metadata_unavailable"] = True
        try:
            context = self.context_provider()
            self.session.emit("filesystem", kind, payload, origin="observation", context=context)
        except Exception:
            self.session.failure("filesystem", "A filesystem observation could not be captured.")

    def start(self) -> None:
        if self._started:
            return
        if not self.roots or any(not root.is_dir() for root in self.roots):
            raise ValueError("Filesystem capture needs existing directory roots.")
        try:
            for root in self.roots:
                self.observer.schedule(self, str(root), recursive=True)
            self.observer.start()
            self._started = True
        except Exception:
            self.session.failure("filesystem", "Filesystem monitoring could not start.")
            try:
                self.observer.stop()
                if self.observer.is_alive():
                    self.observer.join(timeout=2)
            except (RuntimeError, AttributeError):
                pass
            raise

    def stop(self) -> None:
        if self._started:
            self.observer.stop()
            self.observer.join(timeout=3)
            if self.observer.is_alive():
                self.session.failure("filesystem", "Filesystem monitoring did not stop promptly.")
            self._started = False
