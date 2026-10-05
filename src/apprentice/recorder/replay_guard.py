"""Suspend teaching while an Apprentice replay controls the same desktop.

OS folder and filesystem notifications cannot reliably distinguish user actions
from executor effects. A live replay therefore suspends the whole recording
session, including browser snapshots, until that replay ends. Markers contain
only process identity; crashed processes and reused PIDs do not leave recording
permanently paused.
"""
from __future__ import annotations

import json
from functools import wraps
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

import psutil


MARKER_ROOT = Path(__file__).resolve().parents[3] / "runs" / ".active-replays"
_MARKER_NAME = re.compile(r"^([1-9][0-9]*)-[0-9a-f]{32}\.json$")


def _linked(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _root(marker_root=None) -> Path:
    candidate = Path(marker_root) if marker_root is not None else MARKER_ROOT
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate == Path(candidate.anchor):
        raise ValueError("Replay markers need a dedicated directory.")
    for ancestor in [*reversed(candidate.parents), candidate]:
        if _linked(ancestor):
            raise ValueError("Replay markers cannot use links or junctions.")
    return candidate


def replay_active(marker_root=None) -> bool:
    """Return true only for markers matching a live process and its birth time."""
    try:
        directory = _root(marker_root)
        markers = list(directory.iterdir())
    except (OSError, ValueError):
        return False
    for marker in markers:
        match = _MARKER_NAME.fullmatch(marker.name)
        if not match:
            continue
        try:
            if _linked(marker) or not marker.is_file() or marker.stat().st_size > 4096:
                continue
            data = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"pid", "create_time"}:
                continue
            pid, created = data["pid"], data["create_time"]
            if type(pid) is not int or pid != int(match[1]):
                continue
            if type(created) not in (float, int) or not math.isfinite(created) or created <= 0:
                continue
            process = psutil.Process(pid)
            # Windows exposes the same recorded birth time on each query. A small
            # tolerance accounts for timestamp representation across platforms.
            if abs(process.create_time() - created) <= 0.001 and process.is_running():
                if process.status() != psutil.STATUS_ZOMBIE:
                    return True
        except (OSError, ValueError, UnicodeError, psutil.Error):
            continue
    return False


class ReplayGuard:
    def __init__(self, marker_root=None):
        self.directory = _root(marker_root)
        self.path: Path | None = None

    def __enter__(self):
        if self.path is not None:
            raise RuntimeError("This replay guard is already active.")
        self.directory.mkdir(parents=True, exist_ok=True)
        # Recheck after creation before any write, including existing ancestors.
        self.directory = _root(self.directory)
        pid = os.getpid()
        birth = psutil.Process(pid).create_time()
        marker = self.directory / f"{pid}-{uuid4().hex}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".marker-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"pid": pid, "create_time": birth}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            self.path = marker
            return self
        finally:
            temporary_path = Path(temporary)
            if temporary_path.parent == self.directory and temporary_path.exists():
                temporary_path.unlink()

    def __exit__(self, exc_type, exc_value, traceback):
        marker, self.path = self.path, None
        if marker is not None:
            # Only unlink our exact marker, never recursively remove the directory.
            if marker.parent == self.directory and _MARKER_NAME.fullmatch(marker.name):
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    pass  # A stale marker is ignored once this process has exited.
        return False


def replay_guarded(function):
    """Suspend recording around public asynchronous executor entry points."""
    @wraps(function)
    async def guarded(*args, **kwargs):
        with ReplayGuard():
            return await function(*args, **kwargs)
    return guarded
