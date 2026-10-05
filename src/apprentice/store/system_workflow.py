"""Version 2 mixed-surface contract; version 1 remains browser-only."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from apprentice.store.workflow import (
    Contract, FileDownloaded, Name, Parameter, ReviewRequired, Step, Text, Workflow,
    needs_confirmation as browser_confirmation, references,
)


class SystemParameter(Parameter):
    type: Literal["text", "secret", "number", "choice", "file", "month", "date", "boolean",
                  "directory", "path"]

    @model_validator(mode="after")
    def path_value(self):
        if self.type in ("directory", "path"):
            for value in (self.example, self.default):
                if value is not None and (not value.strip() or "\x00" in value):
                    raise ValueError("path parameters need nonempty paths without null characters")
        return self


class PathState(Contract):
    kind: Literal["path_state"] = "path_state"
    path: Text
    state: Literal["file", "directory", "absent"]


class PathTransferred(Contract):
    kind: Literal["path_transferred"] = "path_transferred"
    source: Text
    destination: Text
    source_absent: bool = False


class DirectoryOpen(Contract):
    kind: Literal["directory_open"] = "directory_open"
    path: Text


FilesystemPostcondition = Annotated[
    PathState | PathTransferred | DirectoryOpen | ReviewRequired, Field(discriminator="kind")
]


class FilesystemStep(Contract):
    id: Annotated[int, Field(gt=0)]
    surface: Literal["filesystem"] = "filesystem"
    action: Literal["open_directory", "create_directory", "create_file", "copy_path",
                    "move_path", "rename_path", "delete_path"]
    path: Text | None = None
    source: Text | None = None
    destination: Text | None = None
    content: str | None = None
    recursive: bool = False
    save_as: Name | None = None
    purpose: Text
    postcondition: FilesystemPostcondition
    source_events: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    irreversible: bool = False
    review_notes: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def operation(self):
        transfer = self.action in ("copy_path", "move_path", "rename_path")
        if transfer:
            if not self.source or not self.destination or self.path is not None:
                raise ValueError("transfers need source/destination and no path")
            if self.source == self.destination:
                raise ValueError("transfer source and destination must differ")
        elif not self.path or self.source is not None or self.destination is not None:
            raise ValueError("this operation needs path and no source/destination")
        if self.content is not None and self.action != "create_file":
            raise ValueError("content is only valid for create_file")
        if self.recursive and self.action not in ("copy_path", "delete_path"):
            raise ValueError("recursive is only valid for copy_path/delete_path")
        if self.save_as and self.action == "delete_path":
            raise ValueError("deleted paths cannot be bound as outputs")
        check = self.postcondition
        if not isinstance(check, ReviewRequired):
            if transfer:
                if not isinstance(check, PathTransferred) or (
                    check.source != self.source or check.destination != self.destination
                    or check.source_absent != (self.action != "copy_path")
                ):
                    raise ValueError("transfer checks must verify the action's source/destination and source state")
            elif self.action == "open_directory":
                if not isinstance(check, DirectoryOpen) or check.path != self.path:
                    raise ValueError("open_directory must verify the opened directory")
            else:
                state = {"create_directory": "directory", "create_file": "file", "delete_path": "absent"}[self.action]
                if not isinstance(check, PathState) or check.path != self.path or check.state != state:
                    raise ValueError("path checks must verify the action's path and resulting state")
        return self


class CommandCompleted(Contract):
    kind: Literal["command_completed"] = "command_completed"
    exit_code: int = 0


class ServiceReady(Contract):
    kind: Literal["service_ready"] = "service_ready"
    url: Text
    expected_status: Annotated[int, Field(ge=100, le=599)] = 200


class CommandArtifact(Contract):
    path: Text
    save_as: Name


class TerminalStep(Contract):
    id: Annotated[int, Field(gt=0)]
    surface: Literal["terminal"] = "terminal"
    action: Text
    executable: Text | None = None
    args: list[str] = Field(default_factory=list)
    script: Text | None = None
    cwd: Text | None = None
    env: dict[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")], str] = Field(default_factory=dict)
    timeout_ms: Annotated[int, Field(gt=0)] | None = None
    artifacts: list[CommandArtifact] = Field(default_factory=list)
    purpose: Text
    postcondition: Annotated[CommandCompleted | ServiceReady | ReviewRequired, Field(discriminator="kind")]
    source_events: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    irreversible: bool = False
    review_notes: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def command_contract(self):
        if isinstance(self.postcondition, ReviewRequired):
            return self
        if not self.cwd:
            raise ValueError("terminal commands require an explicit working directory")
        if self.action == "run_command":
            if not self.executable or self.script is not None:
                raise ValueError("run_command needs executable/args and no script")
        elif self.action == "run_script":
            if not self.script or self.executable is not None or self.args:
                raise ValueError("run_script needs literal PowerShell text and no executable/args")
        else:
            raise ValueError("unsupported terminal operations need an explicit review postcondition")
        if len({name.casefold() for name in self.env}) != len(self.env):
            raise ValueError("environment names must be unique ignoring case")
        if any("\x00" in value for value in [self.cwd, self.executable or "", self.script or "", *self.args, *self.env.values()]):
            raise ValueError("commands cannot contain null characters")
        if isinstance(self.postcondition, ServiceReady) and self.artifacts:
            raise ValueError("bind command artifacts after completion, not during service startup")
        return self


class ApplicationRunning(Contract):
    kind: Literal["application_running"] = "application_running"
    app: Text


class ProjectOpen(Contract):
    kind: Literal["project_open"] = "project_open"
    app: Text
    path: Text


class ApplicationStep(Contract):
    id: Annotated[int, Field(gt=0)]
    surface: Literal["application"] = "application"
    action: Text
    app: Text | None = None
    executable: Text | None = None
    args: list[str] = Field(default_factory=list)
    path: Text | None = None
    cwd: Text | None = None
    timeout_ms: Annotated[int, Field(gt=0)] | None = None
    purpose: Text
    postcondition: Annotated[ApplicationRunning | ProjectOpen | ReviewRequired, Field(discriminator="kind")]
    source_events: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    irreversible: bool = False
    review_notes: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def application_contract(self):
        if isinstance(self.postcondition, ReviewRequired):
            return self
        if not self.app or self.postcondition.app != self.app:
            raise ValueError("applications need matching app identity checks")
        if any("\x00" in value for value in [self.app, self.executable or "", self.path or "", self.cwd or "", *self.args]):
            raise ValueError("application inputs cannot contain null characters")
        if self.action == "open_project":
            if not self.path or not isinstance(self.postcondition, ProjectOpen) or self.postcondition.path != self.path:
                raise ValueError("open_project needs a matching workspace path check")
        elif self.action == "launch_application":
            if self.path is not None or not isinstance(self.postcondition, ApplicationRunning):
                raise ValueError("launch_application requires a running-application check and no project path")
        else:
            raise ValueError("unsupported application operations need an explicit review postcondition")
        return self


class PendingStep(Contract):
    """Keep unsupported captured operations visible, without an arbitrary execution escape hatch."""

    id: Annotated[int, Field(gt=0)]
    surface: Literal["desktop", "clipboard", "system"]
    action: Text
    purpose: Text
    postcondition: ReviewRequired
    source_events: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    review_notes: list[Text] = Field(default_factory=list)


SystemStep = Annotated[Step | FilesystemStep | TerminalStep | ApplicationStep | PendingStep, Field(discriminator="surface")]


def needs_confirmation(step):
    if isinstance(step, Step):
        return browser_confirmation(step)
    if isinstance(step, (TerminalStep, ApplicationStep)):
        return not isinstance(step.postcondition, ReviewRequired)
    return isinstance(step, FilesystemStep) and (step.irreversible or step.action == "delete_path")


class SystemWorkflow(Contract):
    version: Literal[2] = 2
    status: Literal["draft", "ready"] = "draft"
    name: Text
    description: Text
    start_url: Text | None = None
    parameters: dict[Name, SystemParameter] = Field(default_factory=dict)
    write_roots: list[Text] = Field(default_factory=list)
    steps: Annotated[list[SystemStep], Field(min_length=1)]
    confirm_before: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    review_notes: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        ids = [step.id for step in self.steps]
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError("step IDs must be unique and consecutive, starting at 1")
        if len(set(self.confirm_before)) != len(self.confirm_before) or not set(self.confirm_before).issubset(ids):
            raise ValueError("confirmation step IDs must be unique and refer to existing steps")
        if any((isinstance(step, FilesystemStep) and step.action != "open_directory")
               or (isinstance(step, TerminalStep) and not isinstance(step.postcondition, ReviewRequired))
               for step in self.steps) and not self.write_roots:
            raise ValueError("filesystem mutations and terminal cwd need explicit write_roots")
        known = set(self.parameters)
        for value in [self.start_url, *self.write_roots]:
            if any(name not in known for name in references(value)):
                raise ValueError("start URL/write roots reference unavailable parameters")
        for root in self.write_roots:
            if any(self.parameters[name].type == "secret" for name in references(root)):
                raise ValueError("write roots cannot contain secret values")
        for step in self.steps:
            if needs_confirmation(step) and step.id not in self.confirm_before:
                raise ValueError(f"step {step.id} requires confirmation")
            if isinstance(step, Step):
                # Reuse every browser contract rule, including secret/check constraints.
                browser_params = {name: parameter.model_dump() for name, parameter in self.parameters.items()
                                  if parameter.type not in ("path", "directory")}
                for name, parameter in self.parameters.items():
                    if parameter.type in ("path", "directory"):
                        browser_params[name] = {**parameter.model_dump(), "type": "text"}
                for name in known - set(self.parameters):
                    browser_params[name] = {"type": "file"}
                isolated = step.model_dump()
                isolated["id"] = 1
                Workflow.model_validate({
                    "name": self.name, "description": self.description, "start_url": self.start_url or "about:blank",
                    "parameters": browser_params, "steps": [isolated],
                    "confirm_before": [1] if step.id in self.confirm_before else [],
                })
            else:
                fields = ("path", "source", "destination", "content", "cwd", "executable", "app", "args")
                values = [(step.model_dump().get(field), isinstance(step, TerminalStep) and field == "args") for field in fields]
                if isinstance(step, TerminalStep):
                    values.extend((value, True) for value in step.env.values())
                    values.extend((artifact.path, False) for artifact in step.artifacts)
                    if isinstance(step.postcondition, ServiceReady):
                        values.append((step.postcondition.url, False))
                for value, secret_allowed in values:
                    for name in references(value):
                        if name not in known:
                            raise ValueError(f"step {step.id} references an unavailable value")
                        if name in self.parameters and self.parameters[name].type == "secret":
                            if not secret_allowed:
                                raise ValueError("secret inputs are only allowed in browser fields and command arguments/environment")
                if isinstance(step, TerminalStep) and step.script:
                    # Shell braces are literal. Secret placeholders belong in environment bindings instead.
                    for name, parameter in self.parameters.items():
                        if parameter.type == "secret" and "{" + name + "}" in step.script:
                            raise ValueError("scripts must reference secrets through environment bindings")
                    if re.search(r"(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|authorization)\s*[=:]\s*['\"]?[^\s$'\"]", step.script):
                        raise ValueError("script credentials must be supplied through runtime environment bindings")
                if isinstance(step, TerminalStep):
                    credential = re.compile(r"password|passwd|passphrase|secret|token|api[_-]?key|authorization|credential", re.I)
                    def secret_reference(value):
                        return any(name in self.parameters and self.parameters[name].type == "secret"
                                   for name in references(value))
                    for key, value in step.env.items():
                        if value and credential.search(key) and not secret_reference(value):
                            raise ValueError("credential environment values need each-run secret parameters")
                    for index, value in enumerate(step.args):
                        flag, assignment, assigned = value.partition("=")
                        if value.startswith("-") and credential.search(flag):
                            candidate = assigned if assignment else (step.args[index + 1] if index + 1 < len(step.args) else "")
                            if candidate and not secret_reference(candidate):
                                raise ValueError("credential arguments need each-run secret parameters")
            output = step.postcondition.save_as if isinstance(step.postcondition, FileDownloaded) else getattr(step, "save_as", None)
            outputs = [output] if output else []
            if isinstance(step, TerminalStep):
                outputs.extend(artifact.save_as for artifact in step.artifacts)
            for output in outputs:
                if output in known:
                    raise ValueError(f"step {step.id} redefines an existing value")
                known.add(output)
        if self.status == "ready" and self.pending_review():
            raise ValueError("a ready workflow cannot contain unresolved review items")
        return self

    def pending_review(self):
        pending = list(self.review_notes)
        for step in self.steps:
            pending.extend(f"Step {step.id}: {note}" for note in step.review_notes)
            if isinstance(step.postcondition, ReviewRequired):
                pending.append(f"Step {step.id}: {step.postcondition.reason}")
        return pending
