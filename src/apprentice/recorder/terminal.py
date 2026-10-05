"""Consume explicit shell submissions, never history, scrollback, or raw keys.

``scripts/ApprenticeShell.ps1`` redacts data before it writes its private JSONL
spool. This adapter repeats that sanitation before recording memory and treats
the spool as untrusted input. Commands run outside the opted-in helper are not
claimed as replayable terminal actions.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import threading
from typing import Any, Callable

from apprentice.recorder.session import REDACTED, RecordingSession


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_CREDENTIAL_NAME = re.compile(
    r"(?:password|passwd|pwd|passphrase|secret|(?:access|refresh|auth|api)[_-]?token|"
    r"token|api[_-]?key|authorization|cookie|credential|client[_-]?secret|private[_-]?key)",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE = re.compile(
    r"(?:\bBearer\s+\S+|\bBasic\s+[A-Za-z0-9+/=]+|\b(?:sk|pk|ghp|gho|github_pat)[_-][A-Za-z0-9_-]{8,}|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"[a-z][a-z0-9+.-]*://[^/\s]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_OPAQUE_SCRIPT = re.compile(r"(?:encodedcommand|frombase64string|securestring|convertto-securestring)", re.IGNORECASE)
_MAX_LINE = 1_048_576


def _secret_argument(value: str) -> tuple[str, str | None]:
    """Return a redacted argument and an optional safe assignment prefix."""
    assignment = re.match(r"^([^=\s]+)=(.*)$", value, re.DOTALL)
    if assignment and _CREDENTIAL_NAME.search(assignment[1]):
        return assignment[1] + "=" + REDACTED, assignment[1] + "="
    if _CREDENTIAL_VALUE.search(value):
        return REDACTED, None
    # Header strings and environment assignments can contain several tokens.
    if _CREDENTIAL_NAME.search(value) and any(separator in value for separator in (":", "=", " ", "\n")):
        return REDACTED, None
    return value, None


def sanitize_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep argv structure while replacing credentials with fresh-input slots.

    The helper supports explicit sensitive argument indices for secrets without
    recognizable names. PowerShell script text is private by default; preserving
    a reviewed nonsecret script requires an explicit helper opt-in.
    """
    command_id = payload.get("command_id")
    mode = payload.get("mode")
    cwd = payload.get("cwd")
    if not isinstance(command_id, str) or not _IDENTIFIER.fullmatch(command_id):
        raise ValueError("Invalid shell command identifier.")
    if mode not in {"exec", "powershell"} or not isinstance(cwd, str) or not cwd:
        raise ValueError("Invalid submitted shell command.")
    clean: dict[str, Any] = {"command_id": command_id, "mode": mode, "cwd": cwd}
    if payload.get("interactive") is True:
        clean["interactive"] = True
    bindings: list[dict[str, Any]] = []
    existing_bindings = payload.get("secret_bindings", [])
    if not isinstance(existing_bindings, list):
        raise ValueError("Invalid shell redaction metadata.")
    sensitive_indices = set()
    for binding in existing_bindings:
        if not isinstance(binding, dict):
            raise ValueError("Invalid shell redaction metadata.")
        index = binding.get("index")
        if binding.get("field") == "args" and type(index) is int and index >= 0:
            sensitive_indices.add(index)
    explicitly_sensitive = payload.get("sensitive_arguments", [])
    if isinstance(explicitly_sensitive, list):
        sensitive_indices.update(index for index in explicitly_sensitive if type(index) is int and index >= 0)
    if mode == "exec":
        executable = payload.get("executable")
        args = payload.get("args")
        if not isinstance(executable, str) or not executable or not isinstance(args, list) or any(
            not isinstance(value, str) for value in args
        ):
            raise ValueError("A native submission requires executable and argument strings.")
        if _CREDENTIAL_VALUE.search(executable):
            executable = REDACTED
            bindings.append({"field": "executable"})
        clean["executable"] = executable
        clean_args = []
        next_sensitive = False
        all_sensitive = payload.get("sensitive") is True
        for index, argument in enumerate(args):
            redacted, prefix = _secret_argument(argument)
            if all_sensitive or next_sensitive or index in sensitive_indices or REDACTED in argument:
                redacted = REDACTED
                prefix = None
            if redacted != argument or REDACTED in redacted:
                binding: dict[str, Any] = {"field": "args", "index": index}
                if prefix:
                    binding["prefix"] = prefix
                elif argument.endswith("=" + REDACTED):
                    # A helper-redacted assignment retains only its safe key.
                    key = argument.partition("=")[0]
                    if _CREDENTIAL_NAME.search(key):
                        redacted = key + "=" + REDACTED
                        binding["prefix"] = key + "="
                bindings.append(binding)
            clean_args.append(redacted)
            next_sensitive = bool(_CREDENTIAL_NAME.search(argument) and argument.startswith("-") and "=" not in argument)
        clean["args"] = clean_args
    else:
        script = payload.get("script")
        if not isinstance(script, str) or not script:
            raise ValueError("A shell submission requires script text or a redaction slot.")
        reviewed = payload.get("script_text_reviewed") is True
        if not reviewed or payload.get("sensitive") is True or _CREDENTIAL_NAME.search(script) or (
            _CREDENTIAL_VALUE.search(script) or _OPAQUE_SCRIPT.search(script)
        ):
            script = REDACTED
        clean["script"] = script
        clean["script_text_reviewed"] = reviewed and script != REDACTED
        clean["requires_review"] = True
        if script == REDACTED:
            bindings.append({"field": "script"})
        executable = payload.get("executable")
        if isinstance(executable, str) and executable and not _CREDENTIAL_VALUE.search(executable):
            clean["executable"] = executable
    clean["secret_bindings"] = bindings
    clean["redacted"] = bool(bindings)
    clean["requires_review"] = clean.get("requires_review", False) or bool(bindings) or payload.get("requires_review") is True
    effect_paths = payload.get("effect_paths", [])
    if not isinstance(effect_paths, list) or any(not isinstance(path, str) or not path for path in effect_paths):
        raise ValueError("Declared command effects must be paths.")
    if effect_paths:
        clean["effect_paths"] = effect_paths
    return clean


class TerminalCapture:
    """Join a shell's explicitly enrolled JSONL stream to a recording session."""

    def __init__(self, session: RecordingSession, spool_path: str | Path, *,
                 context_provider: Callable[[], dict] | None = None, poll_interval: float = 0.1):
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Shell polling requires a positive finite interval.")
        self.session = session
        self.spool_path = Path(spool_path).resolve()
        self.context_provider = context_provider or (lambda: {})
        self.poll_interval = poll_interval
        self._offset = 0
        self._identity = None
        self._active: dict[str, dict] = {}
        self._seen: set[str] = set()
        self._epoch = session.capture_epoch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._failure_kinds: set[str] = set()

    def _failure(self, key: str, reason: str) -> None:
        if key not in self._failure_kinds:
            self._failure_kinds.add(key)
            self.session.failure("terminal", reason)

    def _context(self, supplied: Any) -> dict:
        context = self.context_provider()
        context = context if isinstance(context, dict) else {}
        if isinstance(supplied, dict):
            for name in ("app", "pid", "process_id", "executable", "window_id"):
                value = supplied.get(name)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    # Do not accept arbitrary window titles or command lines.
                    if isinstance(value, str) and _CREDENTIAL_VALUE.search(value):
                        continue
                    context[name] = value
        return context

    def _event(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or envelope.get("version") != 1:
            raise ValueError("Unknown shell spool envelope.")
        surface = envelope.get("surface", "terminal")
        kind = envelope.get("kind")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Unknown shell spool payload.")
        context = self._context(envelope.get("context"))
        if surface == "application":
            self._application(kind, payload, context, envelope.get("evidence"))
            return
        if surface != "terminal":
            raise ValueError("Unknown opted-in shell surface.")
        if kind == "review_required":
            self.session.emit("terminal", kind, {"requires_review": True,
                              "reason": "A submitted PowerShell command needs manual review."}, context=context)
            return
        if kind == "command_started":
            clean = sanitize_command(payload)
            command_id = clean["command_id"]
            if command_id in self._seen:
                raise ValueError("A submitted command identifier was reused.")
            self._seen.add(command_id)
            event = self.session.emit("terminal", kind, clean, context=context)
            if event:
                self._active[command_id] = event
                # Effect paths are explicitly declared by the user, not inferred
                # from coincident writes, process focus, or a command's cwd.
                for path in clean.get("effect_paths", []):
                    self.session.register_effect(path, event["id"], duration_seconds=3600)
        elif kind in {"command_completed", "command_ongoing", "command_failed"}:
            command_id = payload.get("command_id")
            if not isinstance(command_id, str) or not _IDENTIFIER.fullmatch(command_id):
                raise ValueError("Invalid shell completion identifier.")
            started = self._active.get(command_id)
            if not started:
                return  # A submission during pause/replay was deliberately excluded.
            clean = {"command_id": command_id}
            if kind == "command_completed":
                exit_code = payload.get("exit_code")
                if type(exit_code) is not int:
                    raise ValueError("A completed command requires an integer exit code.")
                clean["exit_code"] = exit_code
            else:
                clean["requires_review"] = True
                clean["reason"] = ("The command is still running; review its readiness condition." if kind == "command_ongoing"
                                   else "The submitted command could not start or complete; review is required.")
            process_id = payload.get("process_id")
            if type(process_id) is int and process_id > 0:
                clean["process_id"] = process_id
            event = self.session.emit("terminal", kind, clean, context=context)
            if kind != "command_ongoing":
                self._active.pop(command_id, None)
                if event:
                    duration = max(1e-9, (event["timestamp_ns"] - started["timestamp_ns"]) / 1_000_000_000)
                    for path in started["payload"].get("effect_paths", []):
                        self.session.register_effect(path, started["id"], duration_seconds=duration)
        else:
            raise ValueError("Unknown submitted shell event.")

    def _application(self, kind: Any, payload: dict, context: dict, evidence: Any) -> None:
        if kind not in {"open_project", "launch_application", "review_required"}:
            raise ValueError("Unknown submitted application event.")
        clean = {}
        for name in ("app", "executable", "path", "cwd", "reason", "review_reason", "verification"):
            value = payload.get(name)
            if isinstance(value, str) and value:
                clean[name] = REDACTED if _CREDENTIAL_VALUE.search(value) else value
        args = payload.get("args", [])
        if args == REDACTED:
            # RecordingSession's privacy sanitizer replaces a sensitive args
            # collection with one redaction marker. Retain its application
            # action as review evidence rather than dropping the whole event.
            # Its original length and values are deliberately unrecoverable.
            args = [REDACTED]
            clean["requires_review"] = True
            clean["review_reason"] = "Application arguments were redacted; review explicit launch inputs."
        if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
            raise ValueError("Invalid application arguments.")
        if args:
            temporary = sanitize_command({"command_id": "application", "mode": "exec",
                                          "cwd": clean.get("cwd", "."), "executable": clean.get("executable", "application"),
                                          "args": args, "secret_bindings": payload.get("secret_bindings", []),
                                          "sensitive": payload.get("sensitive") is True})
            clean.update({name: temporary[name] for name in ("args", "secret_bindings", "redacted", "requires_review")})
        for name in ("process_id", "event_id"):
            if type(payload.get(name)) is int:
                clean[name] = payload[name]
        if payload.get("requires_review") is True:
            clean["requires_review"] = True
        if type(payload.get("verified")) is bool:
            clean["verified"] = payload["verified"]
        safe_evidence = []
        references = [evidence] if isinstance(evidence, dict) else evidence or []
        if not isinstance(references, list):
            raise ValueError("Invalid application evidence.")
        for reference in references:
            if isinstance(reference, dict):
                item = {name: reference[name] for name in ("verification", "kind", "process_id", "workspace_path", "path")
                        if name in reference and isinstance(reference[name], (str, int))}
                if item:
                    safe_evidence.append(item)
        self.session.emit("application", kind, clean, context=context, evidence=safe_evidence)

    def poll_once(self) -> int:
        """Consume complete lines; a partial final write waits for the next poll."""
        with self._lock:
            epoch = self.session.capture_epoch
            if epoch != self._epoch:
                self._active.clear()
                self._epoch = epoch
            try:
                stat = self.spool_path.stat()
            except FileNotFoundError:
                return 0  # The shell may opt in after the system session starts.
            identity = (stat.st_dev, stat.st_ino)
            if self._identity is not None and (identity != self._identity or stat.st_size < self._offset):
                self._offset = 0
                self._active.clear()
                self._failure("rotation", "The shell event stream changed; review incomplete commands.")
            self._identity = identity
            processed = 0
            with self.spool_path.open("rb") as stream:
                stream.seek(self._offset)
                for _ in range(1000):
                    position = stream.tell()
                    line = stream.readline(_MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > _MAX_LINE:
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(_MAX_LINE + 1)
                        self._offset = stream.tell()
                        self._failure("oversized", "An oversized shell event was excluded; review is required.")
                        continue
                    if not line.endswith(b"\n"):
                        stream.seek(position)
                        break
                    self._offset = stream.tell()
                    processed += 1
                    if not self.session.capturing or not line.strip():
                        continue
                    try:
                        self._event(json.loads(line.decode("utf-8-sig")))
                    except (ValueError, TypeError, UnicodeError):
                        self._failure("invalid", "A submitted shell event could not be captured safely; review is required.")
            return processed

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.poll_once()
            except OSError:
                self._failure("unavailable", "The opted-in shell event stream could not be read.")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.poll_once()
        self._thread = threading.Thread(target=self._run, name="apprentice-terminal-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.poll_once()
        for command_id in tuple(self._active):
            self.session.emit("terminal", "command_incomplete", {"command_id": command_id, "requires_review": True,
                              "reason": "The command did not complete before recording ended."})
        self._active.clear()
