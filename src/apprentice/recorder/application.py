"""Explicit structured application teaching; no global command-line collection.

PowerShell's Invoke-ApprenticeProject calls this helper. The shared JSONL spool
lets a verified application launch join the same browser/files/terminal session.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apprentice.adapters.application import ApplicationAdapterError, ApplicationBackend
from apprentice.executor.application import ApplicationExecutor
from apprentice.executor.runtime import ExecutionError
from apprentice.recorder.session import sanitize


_SECRET_ARGUMENT = re.compile(r"(?:password|passwd|secret|token|api[-_]?key|authorization)", re.I)


class ApplicationCapture:
    def __init__(self, session, *, backend=None, evidence_dir=None, timeout=30):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Application capture needs a positive finite timeout.")
        self.session = session
        self.backend = backend if backend is not None else ApplicationBackend()
        self.evidence_dir = Path(evidence_dir or "recordings/application-evidence")
        self.timeout = timeout

    def record_application(self, app, *, executable=None, args=(), path=None, cwd=None,
                           sensitive=False):
        """Launch an explicit operation and record whether the outcome was proved."""
        if (not isinstance(app, str) or not app.strip() or
                not all(isinstance(argument, str) and "\x00" not in argument for argument in args)):
            raise ValueError("Application teaching needs a name and string arguments.")
        action = "open_project" if path is not None else "launch_application"
        args = list(args)
        sensitive = sensitive or any(_SECRET_ARGUMENT.search(argument) for argument in args)
        payload = {"app": app, "executable": executable, "args": args,
                   "path": str(path) if path is not None else None,
                   "cwd": str(cwd) if cwd is not None else None, "verified": False}
        if sensitive:
            payload["sensitive"] = True
        handle, verified = None, False
        evidence = []
        try:
            resolved = self.backend.resolve_executable(app, executable)
            payload["executable"] = resolved
            project = ApplicationExecutor.directory(str(path)) if path is not None else None
            directory = ApplicationExecutor.directory(str(cwd)) if cwd is not None else None
            payload["path"], payload["cwd"] = project, directory
            if project is not None and not self.backend.supports_project(app):
                raise ApplicationAdapterError("This application has no verified project adapter.")
            handle = self.backend.launch(app, resolved, args, directory, project,
                                         self.evidence_dir / uuid4().hex)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                verified = (self.backend.project_open(handle, project) if project is not None
                            else self.backend.running(handle))
                if verified:
                    break
                if hasattr(handle, "process") and handle.process.poll() is not None:
                    break
                time.sleep(0.05)
            payload["verified"] = bool(verified)
            if verified:
                proof = {"verification": "vscode_workspace_api" if project is not None else "owned_process",
                         "kind": "project_open" if project is not None else "application_running",
                         "process_id": handle.pid}
                if project is not None:
                    proof["path"] = project
                evidence.append(proof)
            else:
                payload["review_reason"] = "The launched application's state could not be verified."
        except (ApplicationAdapterError, ExecutionError, OSError, ValueError):
            payload["review_reason"] = "The application or its project proof is unavailable; review this action."
        finally:
            if handle is not None:
                if not verified:
                    self.backend.terminate(handle)
                elif hasattr(self.backend, "release"):
                    try:
                        self.backend.release(handle)
                    except (ApplicationAdapterError, ExecutionError, OSError):
                        self.backend.terminate(handle)
                        payload["verified"] = False
                        payload["review_reason"] = "Application handover failed; review this action."
                        evidence.clear()
        return self.session.emit("application", action, payload, origin="user", evidence=evidence,
                                 context={"app": app, "process_id": handle.pid} if handle is not None else {"app": app})


def record_application(session, app, **kwargs):
    """Convenience API for an already-running RecordingSession."""
    configuration = {key: kwargs.pop(key) for key in ("backend", "evidence_dir", "timeout") if key in kwargs}
    return ApplicationCapture(session, **configuration).record_application(app, **kwargs)


class _Spool:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def emit(self, surface, kind, payload, *, origin="user", context=None, evidence=None):
        event = sanitize({"version": 1, "surface": surface, "kind": kind, "payload": payload,
                          "context": context or {}, "evidence": evidence or []})
        data = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        # Append a complete bounded envelope; no output or credentials are printed.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-file", required=True)
    parser.add_argument("--app", default="vscode")
    parser.add_argument("--executable")
    parser.add_argument("--path", help="Existing absolute project directory; omit for a generic app launch.")
    parser.add_argument("--cwd")
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--sensitive", action="store_true", help="Discard captured application arguments.")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    spool = Path(args.event_file).absolute()
    if not spool.parent.is_dir():
        parser.error("The event file needs an existing recording directory.")
    try:
        event = record_application(_Spool(spool), args.app, executable=args.executable, args=args.arg,
                                   path=args.path, cwd=args.cwd, sensitive=args.sensitive,
                                   timeout=args.timeout, evidence_dir=spool.parent / "applications")
    except (OSError, ValueError, ApplicationAdapterError, ExecutionError):
        parser.exit(2, "Application teaching could not start; check the explicit inputs and recording directory.\n")
    if not event["payload"].get("verified"):
        parser.exit(2, "Application state could not be proved; the recorded action requires review.\n")
    print("Application state verified and recorded.")


if __name__ == "__main__":
    main()
