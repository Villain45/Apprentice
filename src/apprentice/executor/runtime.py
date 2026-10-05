"""Runtime values, cancellation, confirmations, and value-free audit records."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from apprentice.store.workflow import FORMATTER, Parameter, Workflow, references, validate_workflow


class ExecutionError(Exception):
    """A message safe to show without exposing browser or parameter contents."""


class ParameterError(ExecutionError):
    pass


class StopRequested(ExecutionError):
    pass


class ConfirmationDeclined(ExecutionError):
    pass


class Control:
    def __init__(self, stop: asyncio.Event | None = None, timeout_ms=10000):
        if timeout_ms <= 0:
            raise ValueError("timeout must be positive")
        self.stop = stop if stop is not None else asyncio.Event()
        self.timeout_ms = timeout_ms

    def check(self):
        if self.stop.is_set():
            raise StopRequested("Execution stopped.")

    async def wait(self, operation, *, timeout_ms=None):
        """Race every browser wait and confirmation against the stop event."""
        task = asyncio.ensure_future(operation)
        cancelled = asyncio.create_task(self.stop.wait())
        duration = self.timeout_ms if timeout_ms is None else timeout_ms
        try:
            done, _ = await asyncio.wait(
                (task, cancelled), return_when=asyncio.FIRST_COMPLETED,
                timeout=duration / 1000 if duration else None,
            )
            self.check()
            if task not in done:
                raise ExecutionError("Operation timed out.")
            return await task
        finally:
            for pending in (task, cancelled):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, cancelled, return_exceptions=True)


@dataclass(frozen=True)
class Confirmation:
    kind: str
    name: str
    step_id: int | None = None
    purpose: str | None = None


@dataclass(frozen=True)
class RunResult:
    status: str
    completed_steps: tuple[int, ...] = ()
    failed_step: int | None = None
    message: str = ""


@dataclass(frozen=True)
class DownloadArtifact:
    name: str
    path: Path


class Audit:
    """Write metadata only: never values, DOM text, or raw Playwright exceptions."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.events = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Each run gets its own file. Do not truncate an earlier audit.
            self.path.touch(exist_ok=False)

    def write(self, event, *, step=None, action=None, attempt=None, strategy=None, status=None):
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event}
        for key, value in (("step", step), ("action", action), ("attempt", attempt),
                           ("strategy", strategy), ("status", status)):
            if value is not None:
                record[key] = value
        self.events.append(record)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")


def parameter_value(name: str, parameter: Parameter, value):
    """Convert CLI text, then validate using the same typed Phase 2 contract."""
    if parameter.type == "boolean" and isinstance(value, str):
        if value.lower() not in ("true", "false"):
            raise ParameterError(f"Parameter {name!r} needs true or false.")
        value = value.lower() == "true"
    elif parameter.type == "number" and isinstance(value, str):
        try:
            decimal = Decimal(value)
            if not decimal.is_finite() or abs(decimal.adjusted()) > 308:
                raise InvalidOperation
            value = int(decimal) if decimal == decimal.to_integral_value() else float(decimal)
        except InvalidOperation:
            raise ParameterError(f"Parameter {name!r} needs a finite number.") from None
    if parameter.type == "secret":
        if not isinstance(value, str) or not value:
            raise ParameterError(f"Parameter {name!r} needs a value for this run.")
    else:
        data = parameter.model_dump(exclude_none=True)
        data["example"] = value
        try:
            type(parameter).model_validate(data)
        except ValueError:
            raise ParameterError(f"Parameter {name!r} does not match its type or choices.") from None
        if parameter.type in ("file", "directory", "path") and not value:
            raise ParameterError(f"Parameter {name!r} needs a file path.")
    return value


def bind_parameters(workflow: Workflow, supplied: dict) -> dict:
    if set(supplied) - set(workflow.parameters):
        raise ParameterError("Unknown runtime parameter names.")
    bound = {}
    for name, parameter in workflow.parameters.items():
        if name in supplied:
            value = supplied[name]
        elif parameter.default is not None and not parameter.ask_each_time:
            value = parameter.default
        else:
            raise ParameterError(f"Missing parameter {name!r}; recorded examples are not defaults.")
        bound[name] = parameter_value(name, parameter, value)
    return bound


def text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def render(value, values: dict):
    if isinstance(value, list):
        return [render(item, values) for item in value]
    if not isinstance(value, str):
        return value
    # Validate expressions even when called independently of a Workflow.
    names = list(references(value))
    if any(name not in values for name in names):
        raise ParameterError("A runtime value is unavailable.")
    match = re.fullmatch(r"\{([a-z][a-z0-9_]*)\}", value)
    if match:
        return values[match[1]]
    return FORMATTER.vformat(value, (), {name: text(item) for name, item in values.items()})


def browser_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https", "file") or (
        parsed.scheme in ("http", "https") and not parsed.netloc
    ):
        raise ParameterError("Browser URLs must use http, https, or file.")
    return value


def dry_run(workflow: Workflow) -> list[str]:
    workflow = validate_workflow(workflow)
    if workflow.status != "ready":
        raise ExecutionError("Review the workflow and mark it ready before replay.")
    lines = [f"Dry run: {workflow.name} ({len(workflow.steps)} steps)",
             "Parameters: " + ", ".join(f"{name} ({param.type})" for name, param in workflow.parameters.items())]
    for step in workflow.steps:
        confirmation = " [confirmation required]" if step.id in workflow.confirm_before else ""
        lines.append(f"{step.id}. {step.purpose} Verify {step.postcondition.kind}{confirmation}")
    return lines
