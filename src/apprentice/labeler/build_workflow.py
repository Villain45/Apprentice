"""Build a model-free workflow draft; apply explicit human review from YAML."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Preserve the original `python src/apprentice/labeler/build_workflow.py ...` command.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apprentice.store.workflow import (
    IRREVERSIBLE, Step, Workflow, load_yaml, needs_confirmation, save_workflow,
    validation_message,
)


class Event(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    kind: str
    element: dict = Field(default_factory=dict)
    value: str | list[str] | bool | None = None
    sensitive: bool = False
    filename: str | None = None
    url: str | None = None
    step: int | None = None
    before_state: dict | None = None
    after_state: dict | None = None
    observations_shared: bool = False


class Recording(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    steps: list[Event] = Field(min_length=1)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    description: str | None = None
    parameters: dict[str, dict] = Field(default_factory=dict)
    steps: dict[int, dict] = Field(default_factory=dict)
    insert_before: list[dict] = Field(default_factory=list)
    review_notes: list[str] | None = None


def slug(value):
    name = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "value"
    return name if name[0].isalpha() else f"value_{name}"


def name_of(element):
    return next((str(element[key]).strip() for key in
                 ("aria_label", "label", "text", "placeholder", "name", "id")
                 if element.get(key) and str(element[key]).strip()), "")


def role_of(element):
    if element.get("role"):
        return element["role"]
    tag, kind = element.get("tag"), element.get("type")
    if tag == "input":
        return {"file": "file_input", "button": "button", "submit": "button",
                "reset": "button", "number": "spinbutton", "checkbox": "checkbox",
                "radio": "radio", "range": "slider"}.get(kind, "textbox")
    return {"select": "combobox", "textarea": "textbox", "a": "link",
            "button": "button"}.get(tag, tag or "element")


def sensitive(event):
    return event.sensitive or event.element.get("type") == "password" or bool(
        event.element.get("sensitive")
    ) or event.value == "<redacted>"


def target_of(event):
    element = event.element
    name, css = name_of(element), element.get("css")
    if not name and not css:
        raise ValueError("an event has no semantic target or CSS fallback; record it again")
    target = {"role": role_of(element)}
    if name:
        target["name"] = name
    if css:
        target["css"] = css
    if sensitive(event):
        target["sensitive"] = True
    return target


def same_element(left, right):
    if left.get("css") and right.get("css"):
        return left["css"] == right["css"]
    if left.get("id") and right.get("id"):
        return left["id"] == right["id"]
    return bool(name_of(left)) and (role_of(left), name_of(left)) == (
        role_of(right), name_of(right)
    )


def observed_click_check(event, following):
    """Use recorded DOM changes; the next control may already have been visible."""
    before, after = event.before_state, event.after_state
    if event.observations_shared:
        return {"kind": "review_required", "reason": "Several clicks share one DOM snapshot; review their results."}
    if not before or not after:
        return {"kind": "review_required",
                "reason": "No DOM change was recorded for this click. Add an observed success check."}
    if after.get("url") and before.get("url") != after["url"]:
        return {"kind": "url", "url": after["url"]}
    if following and following.element:
        def present(state):
            return any(same_element(item, following.element)
                       for item in state.get("elements", []))
        if not present(before) and present(after):
            return {"kind": "element_visible", "target": target_of(following)}
    old_messages = {(item.get("css"), item.get("text")) for item in before.get("messages", [])}
    changed = [item for item in after.get("messages", [])
               if item.get("text") and (item.get("css"), item["text"]) not in old_messages]
    if len(changed) == 1 and changed[0].get("css"):
        return {"kind": "text_visible", "target": {"css": changed[0]["css"]},
                "text": changed[0]["text"].replace("{", "{{").replace("}", "}}"), "exact": True}
    return {"kind": "review_required",
            "reason": "The click has no unambiguous observed result. Add a postcondition."}


def apply_review(data, review_path):
    """Overrides use draft step IDs, before inserted steps are renumbered."""
    review = Review.model_validate(load_yaml(review_path))
    if review.description is not None:
        data["description"] = review.description
    for name, changes in review.parameters.items():
        data["parameters"][name] = {**data["parameters"].get(name, {}), **changes}
    ids = {step["id"] for step in data["steps"]}
    if not set(review.steps).issubset(ids):
        raise ValueError("review overrides refer to a missing draft step")
    for step in data["steps"]:
        changes = review.steps.get(step["id"], {})
        if {"id", "source_steps"} & changes.keys():
            raise ValueError("review overrides cannot change draft IDs or recording provenance")
        step.update(changes)
    if review.review_notes is not None:
        data["review_notes"] = review.review_notes
    insertions = {}
    for insertion in review.insert_before:
        item = dict(insertion)
        anchor = item.pop("step", None)
        if not isinstance(anchor, int) or isinstance(anchor, bool) or anchor not in ids:
            raise ValueError("insert_before needs an existing draft step number")
        if "id" in item or item.get("source_steps"):
            raise ValueError("inserted review steps cannot claim recording provenance")
        item["source_steps"] = []
        insertions.setdefault(anchor, []).append(item)
    updated = []
    for step in data["steps"]:
        updated.extend(insertions.get(step["id"], []))
        updated.append(step)
    for index, step in enumerate(updated, 1):
        step["id"] = index
    data["steps"] = updated
    return data


def build(rec_path=None, *, review_path=None, description=None, ready=False, recording_data=None):
    recording = Recording.model_validate(recording_data if recording_data is not None else
                                         json.loads(Path(rec_path).read_text(encoding="utf-8")))
    raw = recording.steps
    allowed = {"navigate", "download", "click", "type", "select", "upload", "check"}
    if any(event.kind not in allowed for event in raw):
        raise ValueError("recording contains an unsupported event kind")
    source_ids = [event.step if event.step is not None else index + 1 for index, event in enumerate(raw)]
    if any(number < 1 for number in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("recorded step numbers must be positive and unique")
    parameters, steps, notes = {}, [], []
    used_names, memo, downloads = set(), {}, []
    associated, navigation, skip, picker_steps = {}, {}, set(), {}

    def unique(base):
        name, number = base, 2
        while name in used_names:
            name = f"{base}_{number}"
            number += 1
        used_names.add(name)
        return name

    previous = None
    for index, event in enumerate(raw):
        if event.kind == "download":
            if not event.filename:
                raise ValueError("download events need a filename")
            if previous is not None and raw[previous].kind == "click":
                if previous in associated:
                    raise ValueError("one click produced multiple downloads; review/split this recording")
                associated[previous] = index
            else:
                notes.append(f"Download event {source_ids[index]} has no unambiguous triggering click.")
            skip.add(index)
        elif event.kind == "navigate":
            if not event.url:
                raise ValueError("navigation events need a URL")
            if previous is None and event.url == recording.start_url:
                skip.add(index)
            elif previous is not None and raw[previous].kind == "click":
                navigation[previous] = index
                skip.add(index)
        else:
            previous = index

    for index, event in enumerate(raw):
        if index in skip:
            continue
        following = next((candidate for candidate in raw[index + 1:]
                          if candidate.kind not in ("navigate", "download")), None)
        if event.kind == "click" and event.element.get("type") == "file" and following \
                and following.kind == "upload" and same_element(event.element, following.element):
            following_index = next(i for i in range(index + 1, len(raw)) if raw[i] is following)
            picker_steps.setdefault(following_index, []).append(source_ids[index])
            continue
        kind = event.kind
        target = target_of(event) if kind != "navigate" else None
        step = {"id": len(steps) + 1, "surface": "browser", "action": kind,
                "purpose": "", "source_steps": [source_ids[index]], "review_notes": []}
        if target:
            step["target"] = target
        if index in picker_steps:
            step["source_steps"] = picker_steps[index] + step["source_steps"]
        label = target.get("name", target.get("css", "control")) if target else "page"
        if target and not target.get("name"):
            step["review_notes"].append("Only a CSS target was captured; verify its meaning and stability.")

        if kind in ("type", "select"):
            if not isinstance(event.value, str):
                raise ValueError("type/select events need a recorded text value")
            is_secret = sensitive(event)
            param_type = "secret" if is_secret else (
                "choice" if kind == "select" else
                {"number": "number", "month": "month", "date": "date"}.get(
                    event.element.get("type"), "text")
            )
            identity = (event.element.get("css") or event.element.get("id")
                        or (role_of(event.element), name_of(event.element)))
            key = (identity, param_type, None if is_secret else event.value)
            if key not in memo:
                param = {"type": param_type}
                if is_secret:
                    param["ask_each_time"] = True
                elif param_type == "number":
                    try:
                        decimal = Decimal(event.value)
                        if not decimal.is_finite() or abs(decimal.adjusted()) > 308:
                            raise InvalidOperation
                        number = int(decimal) if decimal == decimal.to_integral_value() else float(decimal)
                    except InvalidOperation:
                        number = None
                    if number is not None and (not isinstance(number, float) or math.isfinite(number)):
                        param["example"] = number
                    else:
                        step["review_notes"].append("The recorded number is empty or invalid; choose a numeric value.")
                else:
                    param["example"] = event.value
                    if kind == "select" and event.element.get("options"):
                        param["choices"] = list(dict.fromkeys(event.element["options"]))
                memo[key] = unique(slug(label))
                parameters[memo[key]] = param
            value = "{" + memo[key] + "}"
            step["value"] = value
            step["purpose"] = f"{'Select' if kind == 'select' else 'Enter'} {label}."
            step["postcondition"] = {"kind": "selection" if kind == "select" else "input_value",
                                     "target": target, "value": value}
            if kind == "type" and is_secret:
                step["postcondition"]["sensitive"] = True
        elif kind == "upload":
            if not isinstance(event.value, list) or not event.value:
                raise ValueError("upload events need one or more recorded filenames")
            values = []
            for filename in event.value:
                is_secret = sensitive(event)
                matches = [] if is_secret else [output for name, output in downloads
                                                if name == PureWindowsPath(filename).name]
                if len(matches) == 1:
                    parameter = matches[0]
                else:
                    parameter = unique(slug(label))
                    parameters[parameter] = ({"type": "secret", "ask_each_time": True} if is_secret
                                             else {"type": "file", "example": filename})
                    if not is_secret and len(matches) > 1:
                        step["review_notes"].append("Multiple downloads share this filename; choose the source file.")
                    elif not is_secret:
                        step["review_notes"].append("Only the upload filename was captured; supply its full path when running.")
                values.append("{" + parameter + "}")
            step["value"] = values[0] if len(values) == 1 else values
            step["purpose"] = f"Choose files for {label}."
            step["postcondition"] = {"kind": "files_selected", "target": target, "files": values}
            step["irreversible"] = True
        elif kind == "check":
            is_secret = sensitive(event)
            if not isinstance(event.value, bool) and not (is_secret and event.value == "<redacted>"):
                raise ValueError("check events need a recorded boolean value")
            parameter = unique(slug(label))
            parameters[parameter] = ({"type": "boolean", "ask_each_time": True} if is_secret
                                     else {"type": "boolean", "example": event.value})
            step["value"] = "{" + parameter + "}"
            step["purpose"] = f"Set the checked state of {label}."
            step["postcondition"] = {"kind": "checked", "target": target, "checked": step["value"]}
        elif kind == "navigate":
            step["value"] = event.url
            step["purpose"] = "Open the recorded page."
            step["postcondition"] = {"kind": "url", "url": event.url}
        else:
            step["purpose"] = f"Activate {label}."
            step["irreversible"] = bool(IRREVERSIBLE.search(label))
            if index in associated:
                downloaded_index = associated[index]
                filename = PureWindowsPath(raw[downloaded_index].filename).name
                output = unique("downloaded_file")
                downloads.append((filename, output))
                extension = PureWindowsPath(filename).suffix
                step["postcondition"] = {"kind": "file_downloaded", "save_as": output,
                                         "pattern": f"*{extension}" if extension else filename}
                step["source_steps"].append(source_ids[downloaded_index])
            elif index in navigation:
                navigated = navigation[index]
                step["postcondition"] = {"kind": "url", "url": raw[navigated].url}
                step["source_steps"].append(source_ids[navigated])
            else:
                step["postcondition"] = observed_click_check(event, following)
                check = step["postcondition"]
                if check["kind"] == "text_visible":
                    feedback = check["text"]
                    if re.search(r"\b(missing|invalid|failed|error|denied|incorrect|unable)\b", feedback, re.I):
                        step["postcondition"] = {"kind": "review_required",
                            "reason": "The observed feedback may be an error. Verify the intended result."}
                    elif any(filename in feedback for filename, _ in downloads) or any(
                        str(param.get("example", "")) and str(param.get("example", "")) in feedback
                        for param in parameters.values()
                    ):
                        step["postcondition"] = {"kind": "review_required",
                            "reason": "Feedback includes recorded values. Add a stable text check or parameter template."}
        steps.append(step)

    if not steps:
        raise ValueError("recording contains no supported user actions")
    data = {"version": 1, "status": "draft", "name": recording.name,
            "description": description or f"Recorded browser task: {recording.name.replace('_', ' ')}.",
            "start_url": recording.start_url, "parameters": parameters, "steps": steps,
            "confirm_before": [], "review_notes": notes}
    if review_path:
        apply_review(data, review_path)
    # Recompute after review/insertion so no irreversible action loses its barrier.
    data["confirm_before"] = [item["id"] for item in data["steps"]
                              if needs_confirmation(Step.model_validate(item))]
    data["status"] = "ready" if ready else "draft"
    return Workflow.model_validate(data).model_dump(exclude_none=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path, help="path to steps.json")
    parser.add_argument("--output", type=Path, help="default: workflows/<safe name>.yaml")
    parser.add_argument("--description")
    parser.add_argument("--review", type=Path, help="explicit parameter/step overrides and insertions")
    parser.add_argument("--ready", action="store_true", help="require all review items to be resolved")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output YAML")
    args = parser.parse_args(argv)
    try:
        workflow = Workflow.model_validate(build(args.recording, review_path=args.review,
                                                 description=args.description, ready=args.ready))
        output = args.output or Path("workflows") / f"{slug(workflow.name)}.yaml"
        saved = save_workflow(workflow, output, overwrite=args.overwrite)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.exit(2, f"Cannot build workflow: {validation_message(error)}\n")
    print(f"Saved {workflow.status} workflow to {saved}\n")
    for step in workflow.steps:
        target = (step.target.name or step.target.css) if step.target else "page"
        confirmation = " [confirmation required]" if step.id in workflow.confirm_before else ""
        print(f"{step.id}. {step.purpose} ({step.action}: {target}){confirmation}")
    for item in workflow.pending_review():
        print(f"Review: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
