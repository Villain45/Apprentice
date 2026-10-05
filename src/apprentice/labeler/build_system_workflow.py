"""Convert an ordered system session to reviewed, capability-aware version 2 YAML."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, TypeAdapter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apprentice.labeler.build_workflow import build as build_browser
from apprentice.store.system_workflow import SystemStep, SystemWorkflow, needs_confirmation
from apprentice.store.workflow import Contract, Text, load_yaml, save_workflow, validation_message


class CapturedEvent(Contract):
    id: Annotated[int, Field(gt=0)]
    timestamp_ns: Annotated[int, Field(ge=0)]
    surface: Text
    kind: Text
    origin: Literal["user", "observation"]
    payload: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    evidence: list[str | dict] = Field(default_factory=list)


class SessionRecording(Contract):
    version: Literal[2]
    name: Text
    roots: list[Text] = Field(default_factory=list)
    session_id: str | None = None
    recorded_at: str | None = None
    events: list[CapturedEvent]


class SessionReview(Contract):
    description: str | None = None
    parameters: dict[str, dict] = Field(default_factory=dict)
    write_roots: list[str] | None = None
    events: dict[int, dict] = Field(default_factory=dict)
    review_notes: list[str] | None = None


def literal(value):
    return str(value).replace("{", "{{").replace("}", "}}")


def pending(event, reason):
    surface = event["surface"]
    if surface not in ("application", "terminal", "desktop", "clipboard", "system"):
        surface = "system"
    return {"surface": surface, "action": event["kind"],
            "purpose": f"Review captured {event['surface']} operation: {event['kind']}.",
            "postcondition": {"kind": "review_required", "reason": reason},
            "source_events": [event["id"]]}


def output_names(step):
    """Reserve explicitly reviewed outputs before generating any input names."""
    candidates = [step.get("save_as")]
    check = step.get("postcondition")
    if isinstance(check, dict):
        candidates.append(check.get("save_as"))
    artifacts = step.get("artifacts", [])
    if isinstance(artifacts, list):
        candidates.extend(artifact.get("save_as") for artifact in artifacts if isinstance(artifact, dict))
    return {name for name in candidates if isinstance(name, str) and name}


def build(session_path, *, review_path=None, description=None, ready=False):
    session = SessionRecording.model_validate(json.loads(Path(session_path).read_text(encoding="utf-8"))).model_dump()
    events = session["events"]
    ids = [event.get("id") for event in events]
    if any(type(number) is not int or number <= 0 for number in ids) or ids != sorted(set(ids)):
        raise ValueError("session event IDs must be positive, unique and ordered")
    timestamps = [event["timestamp_ns"] for event in events]
    if timestamps != sorted(set(timestamps)):
        raise ValueError("session timestamps must be strictly ordered")
    if any(event.get("origin") not in ("user", "observation") for event in events):
        raise ValueError("replay events cannot be learned as user actions")
    review = SessionReview.model_validate(load_yaml(review_path)) if review_path else SessionReview()
    if set(review.events) - set(ids):
        raise ValueError("review refers to missing session events")
    parameters, roots, steps, notes = {}, [], [], []
    reserved_outputs = set()
    for instruction in review.events.values():
        if isinstance(instruction.get("step"), dict):
            reserved_outputs.update(output_names(instruction["step"]))
    root_paths = []
    for index, root in enumerate(session.get("roots", []), 1):
        name = "workspace" if index == 1 else f"workspace_{index}"
        base, suffix = name, 2
        while name in reserved_outputs or name in parameters:
            name, suffix = f"{base}_root_{suffix}", suffix + 1
        parameters[name] = {"type": "directory", "example": root}
        roots.append("{" + name + "}")
        root_paths.append((Path(root), name))

    def path_template(path):
        if not isinstance(path, str) or not path:
            raise ValueError("recorded filesystem operation has no path")
        for root, name in sorted(root_paths, key=lambda pair: len(str(pair[0])), reverse=True):
            try:
                relative = Path(path).relative_to(root)
            except ValueError:
                continue
            return "{" + name + "}" + ("/" + literal(relative.as_posix()) if str(relative) != "." else "")
        # Navigation outside watched roots remains parameterized, rather than hardcoding the machine.
        base = name = f"directory_{len(parameters) + 1}"
        suffix = 2
        while name in parameters or name in reserved_outputs:
            name, suffix = f"{base}_{suffix}", suffix + 1
        parameters[name] = {"type": "directory", "example": path}
        return "{" + name + "}"

    supported_browser = {"navigate", "download", "click", "type", "select", "upload", "check"}
    browser_events = [event for event in events if event["surface"] == "browser"
                      and event["kind"] in supported_browser and event["id"] not in review.events]
    start_url = next((event.get("payload", {}).get("url") for event in browser_events
                      if event["kind"] == "navigate" and event.get("payload", {}).get("url")), None)
    browser_consumed = set()
    if browser_events:
        raw = [{**event.get("payload", {}), "kind": event["kind"], "step": event["id"]}
               for event in browser_events]
        try:
            browser = build_browser(recording_data={"name": session["name"], "start_url": start_url or "about:blank", "steps": raw})
        except ValueError:
            # A browser recording requiring richer context still produces a reviewable system draft.
            browser = {"parameters": {}, "review_notes": [], "steps": []}
            for event in browser_events:
                steps.append((event["id"], pending(event, "Browser evidence cannot be converted unambiguously; review this event.")))
        for step in browser["steps"]:
            reserved_outputs.update(output_names(step))
        reserved = set(browser["parameters"]) | reserved_outputs
        for root_index, (root, name) in enumerate(root_paths):
            if name in reserved:
                replacement, suffix = name + "_root", 2
                while replacement in reserved or replacement in parameters:
                    replacement, suffix = f"{name}_root_{suffix}", suffix + 1
                parameters[replacement] = parameters.pop(name)
                root_paths[root_index] = (root, replacement)
                roots[root_index] = "{" + replacement + "}"
        parameters.update(browser["parameters"])
        notes.extend(browser["review_notes"])
        by_id = {event["id"]: event for event in browser_events}
        for step in browser["steps"]:
            sources = step["source_steps"]
            unsafe = any(by_id[number].get("payload", {}).get("source_frame", {}).get("is_main_frame") is False
                         or by_id[number].get("payload", {}).get("source_page", 0) not in (0, 1)
                         for number in sources)
            if unsafe:
                steps.append((min(sources), pending(by_id[sources[0]],
                              "This action uses another page or frame. Its replay context needs review.")))
            else:
                intervening = [event for event in events if min(sources) < event["id"] < max(sources)
                               and event["origin"] == "user" and event["surface"] != "browser"]
                if intervening:
                    step["review_notes"].append("This browser action spans other user interactions; review ordering.")
                steps.append((min(sources), step))
        browser_consumed.update(event["id"] for event in browser_events)

    def parameter(base, value=None, *, secret=False):
        base = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or "argument"
        if not base[0].isalpha():
            base = "argument_" + base
        name, suffix = base, 2
        while name in parameters or name in reserved_outputs:
            name, suffix = f"{base}_{suffix}", suffix + 1
        parameters[name] = {"type": "secret", "ask_each_time": True} if secret else {"type": "text", "example": value}
        return "{" + name + "}"

    command_pairs, command_completions = {}, set()
    for event in events:
        if event["surface"] != "terminal" or event["kind"] != "command_started":
            continue
        command_id = event["payload"].get("command_id")
        if not isinstance(command_id, str) or not command_id:
            continue
        completed = [candidate for candidate in events if candidate["surface"] == "terminal"
                     and candidate["kind"] == "command_completed"
                     and candidate["payload"].get("command_id") == command_id]
        started = [candidate for candidate in events if candidate["surface"] == "terminal"
                   and candidate["kind"] == "command_started"
                   and candidate["payload"].get("command_id") == command_id]
        if len(started) == 1 and len(completed) == 1 and completed[0]["id"] > event["id"]:
            command_pairs[event["id"]] = completed[0]

    for event in events:
        number, payload = event["id"], event.get("payload", {})
        if number in review.events:
            instruction = review.events[number]
            if instruction.get("ignore") is True:
                if set(instruction) != {"ignore", "reason"} or not isinstance(instruction.get("reason"), str) or not instruction["reason"].strip():
                    raise ValueError("ignored events need an explicit review reason")
                continue
            if set(instruction) != {"step"} or not isinstance(instruction["step"], dict):
                raise ValueError("event review needs either step or ignore/reason")
            step = dict(instruction["step"])
            if any(field in step for field in ("id", "source_events", "source_steps")):
                raise ValueError("review cannot forge step IDs or event provenance")
            if step.get("surface") == "browser":
                step["source_steps"] = [number]
            else:
                step["source_events"] = [number]
                if number in command_pairs:
                    completion = command_pairs[number]["id"]
                    step["source_events"].append(completion)
                    command_completions.add(completion)
            steps.append((number, step))
            continue
        if number in browser_consumed:
            continue
        if number in command_completions:
            continue
        if event["origin"] == "observation" and payload.get("correlated_to"):
            cause = payload["correlated_to"]
            cause_event = next((candidate for candidate in events if candidate["id"] == cause), None)
            if (type(cause) is not int or cause_event is None or cause >= number
                    or cause_event["origin"] != "user" or review.events.get(cause, {}).get("ignore") is True):
                raise ValueError("filesystem observation has an invalid originating event")
            continue  # An originating action already represents this effect.
        if event["surface"] == "terminal" and event["kind"] == "command_started":
            completion = command_pairs.get(number)
            if completion is None or type(completion["payload"].get("exit_code")) is not int:
                steps.append((number, pending(event, "Command completion is missing or ambiguous. Review completion/readiness before replay.")))
                continue
            command_completions.add(completion["id"])
            mode, cwd = payload.get("mode"), payload.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                steps.append((number, pending(event, "The command working directory is missing. Review its execution context.")))
                continue
            step = {"surface": "terminal", "purpose": "Run the demonstrated command and verify its exit code.",
                    "cwd": path_template(cwd), "source_events": [number, completion["id"]],
                    "postcondition": {"kind": "command_completed", "exit_code": completion["payload"]["exit_code"]}}
            if mode == "exec" and isinstance(payload.get("executable"), str) and isinstance(payload.get("args"), list):
                step.update(action="run_command", executable=literal(payload["executable"]), args=[])
                bindings = {binding.get("index"): binding for binding in payload.get("secret_bindings", [])
                            if isinstance(binding, dict) and binding.get("field") == "args"}
                for index, value in enumerate(payload["args"]):
                    if not isinstance(value, str):
                        raise ValueError("recorded command arguments must be strings")
                    previous = payload["args"][index - 1] if index else "argument"
                    name = previous.lstrip("-") if isinstance(previous, str) and previous.startswith("-") else "argument"
                    if index in bindings or "<redacted>" in value:
                        reference = parameter(name, secret=True)
                        step["args"].append(literal(value).replace("<redacted>", reference) if "<redacted>" in value else reference)
                    elif value.startswith("-"):
                        flag, separator, content = value.partition("=")
                        step["args"].append(literal(flag) + "=" + parameter(flag.lstrip("-"), content)
                                            if separator else literal(value))
                    elif Path(value).is_absolute() and any(Path(value).is_relative_to(root) for root, _ in root_paths):
                        step["args"].append(path_template(value))
                    else:
                        step["args"].append(parameter(name, value))
                if payload.get("requires_review"):
                    step["review_notes"] = ["Review the recorded command, arguments and credential bindings before execution."]
            elif mode == "powershell" and isinstance(payload.get("script"), str) and payload["script"] != "<redacted>":
                step.update(action="run_script", script=payload["script"],
                            review_notes=["Review the literal PowerShell script and its side effects before execution."])
            else:
                item = pending(event, "The shell script was redacted or cannot be represented as argv; review a script using runtime environment bindings.")
                item["source_events"].append(completion["id"])
                steps.append((number, item))
                continue
            if completion["payload"]["exit_code"] != 0:
                step.setdefault("review_notes", []).append(
                    "The demonstrated command returned a nonzero exit code. Review whether that was intended before replay."
                )
            if not any(Path(cwd).is_relative_to(root) for root, _ in root_paths):
                # Observing a command does not widen the user's reviewed execution scope.
                step["postcondition"] = {
                    "kind": "review_required",
                    "reason": "The command working directory is outside the recorded workspace roots. Review the command and declare its permitted write_roots.",
                }
            steps.append((number, step))
            continue
        if event["surface"] == "application" and event["kind"] in ("launch_application", "open_project"):
            app = payload.get("app")
            if isinstance(app, str) and app:
                if not isinstance(payload.get("args", []), list) or any(
                    not isinstance(value, str) or "<redacted>" in value for value in payload.get("args", [])
                ):
                    steps.append((number, pending(event, "Application arguments were redacted; review its explicit launch inputs.")))
                    continue
                action = event["kind"]
                step = {"surface": "application", "action": action, "app": literal(app),
                        "purpose": "Open the demonstrated project." if action == "open_project" else "Launch the demonstrated application.",
                        "source_events": [number], "args": []}
                if payload.get("executable"):
                    step["executable"] = literal(payload["executable"])
                for value in payload.get("args", []):
                    if not isinstance(value, str) or "<redacted>" in value:
                        step["review_notes"] = ["Application arguments need explicit sensitive-input review."]
                        break
                    step["args"].append(literal(value) if value.startswith("-") else parameter("app_argument", value))
                if payload.get("cwd"):
                    step["cwd"] = path_template(payload["cwd"])
                if action == "open_project" and payload.get("path"):
                    step["path"] = path_template(payload["path"])
                    step["postcondition"] = {"kind": "project_open", "app": step["app"], "path": step["path"]}
                elif action == "launch_application":
                    step["postcondition"] = {"kind": "application_running", "app": step["app"]}
                else:
                    steps.append((number, pending(event, "The project path was not captured. Review its workspace identity.")))
                    continue
                if payload.get("verified") is not True:
                    step.setdefault("review_notes", []).append("The recorded application/workspace did not verify. Review its identity before execution.")
                steps.append((number, step))
                continue
        if event["surface"] == "filesystem":
            action = payload.get("action", event["kind"])
            step = None
            if action in ("open_directory", "directory_open"):
                path = path_template(payload.get("path"))
                step = {"action": "open_directory", "path": path,
                        "postcondition": {"kind": "directory_open", "path": path}}
            elif action == "created" and (payload.get("is_directory") or payload.get("size") == 0):
                path = path_template(payload.get("path"))
                directory = bool(payload.get("is_directory"))
                step = {"action": "create_directory" if directory else "create_file", "path": path,
                        "postcondition": {"kind": "path_state", "path": path, "state": "directory" if directory else "file"}}
            elif action == "moved":
                source, destination = path_template(payload.get("path")), path_template(payload.get("destination"))
                step = {"action": "move_path", "source": source, "destination": destination,
                        "postcondition": {"kind": "path_transferred", "source": source,
                                          "destination": destination, "source_absent": True}}
            elif action == "deleted":
                path = path_template(payload.get("path"))
                step = {"action": "delete_path", "path": path,
                        "postcondition": {"kind": "path_state", "path": path, "state": "absent"}}
            if step is not None:
                step.update(surface="filesystem", purpose=f"{step['action'].replace('_', ' ').capitalize()}.",
                            source_events=[number])
                if event["origin"] == "observation":
                    step["review_notes"] = ["Filesystem change observed; confirm it was intended and not a background effect."]
                steps.append((number, step))
                continue
        steps.append((number, pending(event, "No verified replay adapter for this captured operation yet. Review it explicitly.")))
    if not steps:
        raise ValueError("the session has no replay steps after review")
    ordered = [step for _, step in sorted(steps, key=lambda pair: pair[0])]
    for index, step in enumerate(ordered, 1):
        step["id"] = index
    for name, fields in review.parameters.items():
        parameters[name] = {**parameters.get(name, {}), **fields}
    validated_steps = [TypeAdapter(SystemStep).validate_python(step) for step in ordered]
    workflow = SystemWorkflow.model_validate({
        "version": 2, "status": "draft", "name": session["name"],
        "description": review.description or description or "Replay the reviewed system demonstration.",
        "start_url": start_url, "parameters": parameters,
        "write_roots": review.write_roots if review.write_roots is not None else roots,
        "steps": ordered, "review_notes": review.review_notes if review.review_notes is not None else notes,
        "confirm_before": [step.id for step in validated_steps if needs_confirmation(step)],
    })
    # Recompute using the actual validated variants, including semantic browser confirmation policy.
    data = workflow.model_dump()
    data["confirm_before"] = [step.id for step in workflow.steps if needs_confirmation(step)]
    data["status"] = "ready" if ready else "draft"
    return SystemWorkflow.model_validate(data).model_dump(exclude_none=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--description")
    parser.add_argument("--ready", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = build(args.session, review_path=args.review, description=args.description, ready=args.ready)
        target = save_workflow(data, args.output, overwrite=args.overwrite)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.exit(2, f"Cannot build workflow: {validation_message(error)}\n")
    print(f"Saved {data['status']} system workflow: {target}")
    for note in SystemWorkflow.model_validate(data).pending_review():
        print(f"Review: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
