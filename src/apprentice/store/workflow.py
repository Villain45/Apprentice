"""Version 1 browser contract, version dispatch, and safe YAML persistence."""

from __future__ import annotations

import argparse
import math
import os
import re
import string
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

IDENTIFIER = r"^[a-z][a-z0-9_]*$"
Name = Annotated[str, Field(pattern=IDENTIFIER)]
Text = Annotated[str, Field(min_length=1)]
Scalar = str | int | float | bool
IRREVERSIBLE = re.compile(
    r"\b(submit|send|pay|purchase|buy|delete|remove|overwrite|approve|confirm)\b"
    r"|place\s+order|complete\s+checkout", re.I
)
FORMATTER = string.Formatter()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(Contract):
    role: Text | None = None
    name: Text | None = None
    css: Text | None = None
    sensitive: bool = False

    @model_validator(mode="after")
    def locator_exists(self):
        if not self.name and not self.css:
            raise ValueError("a target needs a semantic name or CSS fallback")
        return self


class Parameter(Contract):
    type: Literal["text", "secret", "number", "choice", "file", "month", "date", "boolean"]
    example: Scalar | None = None
    default: Scalar | None = None
    choices: list[Text] | None = None
    ask_each_time: bool = False

    @model_validator(mode="after")
    def safe_and_typed(self):
        if self.type == "secret":
            if self.example is not None or self.default is not None:
                raise ValueError("secret parameters must never contain examples or defaults")
            if not self.ask_each_time:
                raise ValueError("secret parameters must be requested each run")
        if self.choices is not None:
            if self.type not in ("choice", "month") or not self.choices:
                raise ValueError("nonempty choices are only valid for choice or month parameters")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("parameter choices must be unique")
        for value in (self.example, self.default):
            if value is None:
                continue
            if self.type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("number parameters need numeric examples/defaults")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("number parameters must be finite")
            elif self.type == "boolean":
                if not isinstance(value, bool):
                    raise ValueError("boolean parameters need boolean examples/defaults")
            elif not isinstance(value, str):
                raise ValueError("this parameter needs text examples/defaults")
            if self.choices is not None and value not in self.choices:
                raise ValueError("example/default must belong to the available choices")
            if self.type == "month" and self.choices is None:
                if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
                    raise ValueError("month inputs use YYYY-MM")
            if self.type == "date":
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError
                    date.fromisoformat(value)
                except ValueError as error:
                    raise ValueError("date inputs use a valid YYYY-MM-DD date") from error
        return self


class InputValue(Contract):
    kind: Literal["input_value"] = "input_value"
    target: Target
    value: Text
    sensitive: bool = False


class Selection(Contract):
    kind: Literal["selection"] = "selection"
    target: Target
    value: Text
    by: Literal["label", "value"] = "label"


class FilesSelected(Contract):
    kind: Literal["files_selected"] = "files_selected"
    target: Target
    files: Annotated[list[Text], Field(min_length=1)]


class FileDownloaded(Contract):
    kind: Literal["file_downloaded"] = "file_downloaded"
    pattern: Text
    save_as: Name


class ElementVisible(Contract):
    kind: Literal["element_visible"] = "element_visible"
    target: Target


class TextVisible(Contract):
    kind: Literal["text_visible"] = "text_visible"
    text: Text
    target: Target | None = None
    exact: bool = False


class UrlMatches(Contract):
    kind: Literal["url"] = "url"
    url: Text


class Checked(Contract):
    kind: Literal["checked"] = "checked"
    target: Target
    checked: bool | Annotated[str, Field(pattern=r"^\{[a-z][a-z0-9_]*\}$")]


class ReviewRequired(Contract):
    kind: Literal["review_required"] = "review_required"
    reason: Text


Postcondition = Annotated[
    InputValue | Selection | FilesSelected | FileDownloaded | ElementVisible
    | TextVisible | UrlMatches | Checked | ReviewRequired,
    Field(discriminator="kind"),
]


class Step(Contract):
    id: Annotated[int, Field(gt=0)]
    surface: Literal["browser"] = "browser"
    action: Literal["click", "type", "select", "upload", "check", "navigate"]
    target: Target | None = None
    value: str | list[Text] | bool | None = None
    purpose: Text
    postcondition: Postcondition
    source_steps: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    irreversible: bool = False
    review_notes: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def action_contract(self):
        if self.action == "navigate":
            if self.target is not None or not isinstance(self.value, str) or not self.value:
                raise ValueError("navigation needs a URL value and no element target")
        elif self.target is None:
            raise ValueError("browser actions need an element target")
        if self.action in ("type", "select") and not isinstance(self.value, str):
            raise ValueError("type/select actions need a text value or parameter reference")
        if self.action == "upload":
            if not (isinstance(self.value, str) and self.value) and not (
                isinstance(self.value, list) and self.value
            ):
                raise ValueError("uploads need one or more file values")
        if self.action == "check" and not isinstance(self.value, bool) and not (
            isinstance(self.value, str) and re.fullmatch(r"\{([a-z][a-z0-9_]*)\}", self.value)
        ):
            raise ValueError("check actions need a boolean value or boolean parameter reference")
        if self.action == "click" and self.value is not None:
            raise ValueError("click actions have no value")
        return self


def references(value):
    """Find named templates without evaluating Python or format expressions."""
    if isinstance(value, str):
        try:
            parts = list(FORMATTER.parse(value))
        except ValueError as error:
            raise ValueError("invalid template braces; escape literal braces as {{ and }}") from error
        for _, name, format_spec, conversion in parts:
            if name is None:
                continue
            if not re.fullmatch(IDENTIFIER, name) or format_spec or conversion:
                raise ValueError("templates must use simple names such as {month}")
            yield name
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def needs_confirmation(step: Step) -> bool:
    return step.irreversible or step.action == "upload" or (
        step.action == "click" and bool(IRREVERSIBLE.search(step.target.name or ""))
    )


class Workflow(Contract):
    version: Literal[1] = 1
    status: Literal["draft", "ready"] = "draft"
    name: Text
    description: Text
    start_url: Text
    parameters: dict[Name, Parameter] = Field(default_factory=dict)
    steps: Annotated[list[Step], Field(min_length=1)]
    confirm_before: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    review_notes: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent_workflow(self):
        ids = [step.id for step in self.steps]
        if ids != list(range(1, len(self.steps) + 1)):
            raise ValueError("step IDs must be unique and consecutive, starting at 1")
        if len(set(self.confirm_before)) != len(self.confirm_before):
            raise ValueError("confirmation step IDs must be unique")
        if not set(self.confirm_before).issubset(ids):
            raise ValueError("confirmation refers to a missing step")
        known = set(self.parameters)
        for name in references(self.start_url):
            if name not in known:
                raise ValueError(f"start URL references unknown parameter {name!r}")
        for step in self.steps:
            if needs_confirmation(step) and step.id not in self.confirm_before:
                raise ValueError(f"step {step.id} requires confirmation")
            action_refs = list(references(step.value))
            uses_secret = any(name in self.parameters and self.parameters[name].type == "secret" for name in action_refs)
            if uses_secret and step.target and not step.target.sensitive:
                raise ValueError(f"step {step.id} must mark its secret target as sensitive")
            if step.target and step.target.sensitive and step.action in ("type", "select", "upload"):
                secret_values = list(step.value) if isinstance(step.value, list) else [step.value]
                if isinstance(step.postcondition, (InputValue, Selection)):
                    secret_values.append(step.postcondition.value)
                elif isinstance(step.postcondition, FilesSelected):
                    secret_values.extend(step.postcondition.files)
                for value in secret_values:
                    match = re.fullmatch(r"\{([a-z][a-z0-9_]*)\}", value or "")
                    if not match or match[1] not in self.parameters or self.parameters[match[1]].type != "secret":
                        raise ValueError(f"step {step.id} must use secret references for its value and check")
                if isinstance(step.postcondition, InputValue) and not step.postcondition.sensitive:
                    raise ValueError(f"step {step.id} must mark its value check as sensitive")
            if step.action == "check" and isinstance(step.value, str):
                if len(action_refs) != 1 or action_refs[0] not in self.parameters or self.parameters[action_refs[0]].type != "boolean":
                    raise ValueError(f"step {step.id} requires a boolean parameter")
                parameter = self.parameters[action_refs[0]]
                if step.target.sensitive and (not parameter.ask_each_time or parameter.example is not None
                                              or parameter.default is not None):
                    raise ValueError(f"step {step.id} must request its sensitive checked state without saved values")
            elif step.action == "check" and step.target.sensitive:
                raise ValueError(f"step {step.id} must parameterize its sensitive checked state")
            values = [step.value]
            for field in ("value", "files", "text", "url", "pattern", "checked"):
                values.append(getattr(step.postcondition, field, None))
            for value in values:
                for name in references(value):
                    if name not in known:
                        raise ValueError(f"step {step.id} references unavailable value {name!r}")
            if isinstance(step.postcondition, FileDownloaded):
                output = step.postcondition.save_as
                if output in known:
                    raise ValueError(f"step {step.id} redefines value {output!r}")
                known.add(output)
        if self.status == "ready" and self.pending_review():
            raise ValueError("a ready workflow cannot contain unresolved review items")
        return self

    def pending_review(self) -> list[str]:
        pending = list(self.review_notes)
        for step in self.steps:
            pending.extend(f"Step {step.id}: {note}" for note in step.review_notes)
            if isinstance(step.postcondition, ReviewRequired):
                pending.append(f"Step {step.id}: {step.postcondition.reason}")
        return pending


class WorkflowLoader(yaml.SafeLoader):
    """Reject duplicate keys so a later YAML entry cannot conceal an earlier one."""


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise ValueError("duplicate YAML mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


WorkflowLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_yaml(path: str | Path):
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=WorkflowLoader)


def validate_workflow(data):
    if isinstance(data, BaseModel):
        data = data.model_dump()
    if isinstance(data, dict) and data.get("version") == 2:
        from apprentice.store.system_workflow import SystemWorkflow
        return SystemWorkflow.model_validate(data)
    return Workflow.model_validate(data)


def load_workflow(path: str | Path, *, require_ready=False):
    workflow = validate_workflow(load_yaml(path))
    if require_ready and workflow.status != "ready":
        raise ValueError("workflow is still a draft; review it and set status: ready")
    return workflow


def save_workflow(workflow: Workflow | dict, path: str | Path, *, overwrite=False) -> Path:
    # Revalidate even model instances: callers may have modified them since creation.
    validated = validate_workflow(workflow)
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError("output exists; use --overwrite to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(compact(validated.model_dump(exclude_none=True)), sort_keys=False,
                             allow_unicode=True, width=100)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target


def compact(value):
    """Omit empty review lists and false flags; retain union tags and provenance."""
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items()
                if not (key in ("ask_each_time", "sensitive", "irreversible") and item is False)
                and not (key == "review_notes" and item == [])}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


def validation_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        # Never include offending inputs: an invalid hand-edited YAML may hold a secret.
        return "\n".join(f"{'.'.join(map(str, item['loc'])) or 'workflow'}: {item['msg']}"
                         for item in error.errors(include_input=False, include_url=False))
    if isinstance(error, yaml.YAMLError):
        return "invalid YAML syntax"
    return str(error)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a version 1 or 2 workflow YAML.")
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--ready", action="store_true", help="also require a reviewed, ready workflow")
    args = parser.parse_args(argv)
    try:
        workflow = load_workflow(args.workflow, require_ready=args.ready)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.exit(2, f"Workflow invalid: {validation_message(error)}\n")
    print(f"Valid {workflow.status} workflow: {workflow.name} ({len(workflow.steps)} steps)")
    for item in workflow.pending_review():
        print(f"Review: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
