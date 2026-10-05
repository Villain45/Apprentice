from copy import deepcopy

import pytest
import yaml
from pydantic import ValidationError

from apprentice.store.workflow import Workflow, load_workflow, save_workflow, validation_message


@pytest.fixture
def valid_data():
    return {
        "name": "A readable task", "description": "Open a report.", "status": "ready",
        "start_url": "http://localhost:8000/", "steps": [{
            "id": 1, "action": "click", "target": {"role": "button", "name": "Open report"},
            "purpose": "Open the report.", "postcondition": {"kind": "text_visible", "text": "Report ready"},
        }],
    }


def test_yaml_round_trip_and_no_accidental_overwrite(tmp_path, valid_data):
    valid_data["name"] = "Rapport café"
    workflow = Workflow.model_validate(valid_data)
    path = save_workflow(workflow, tmp_path / "task.yaml")
    assert load_workflow(path, require_ready=True) == workflow
    assert "Rapport café" in path.read_text(encoding="utf-8")
    assert "sensitive: false" not in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        save_workflow(workflow, path)
    assert load_workflow(path) == workflow


@pytest.mark.parametrize("field,value", [
    ("id", 2), ("postcondition", None), ("value", "unexpected"),
    ("surface", "desktop"), ("target", {"x": 422, "y": 731}),
])
def test_invalid_step_contracts(valid_data, field, value):
    valid_data["steps"][0][field] = value
    with pytest.raises(ValidationError):
        Workflow.model_validate(valid_data)


def test_unknown_fields_and_duplicate_ids_are_rejected(valid_data):
    valid_data["steps"].append(deepcopy(valid_data["steps"][0]))
    with pytest.raises(ValidationError, match="consecutive"):
        Workflow.model_validate(valid_data)
    valid_data["steps"].pop()
    valid_data["surprise"] = True
    with pytest.raises(ValidationError):
        Workflow.model_validate(valid_data)


@pytest.mark.parametrize("name", ["Send", "Delete", "Approve", "Pay", "Overwrite"])
def test_irreversible_actions_need_confirmation(valid_data, name):
    valid_data["steps"][0]["target"]["name"] = name
    with pytest.raises(ValidationError, match="requires confirmation"):
        Workflow.model_validate(valid_data)
    valid_data["confirm_before"] = [1]
    Workflow.model_validate(valid_data)
    valid_data["confirm_before"] = [2]
    with pytest.raises(ValidationError, match="missing step"):
        Workflow.model_validate(valid_data)


def test_review_items_block_ready_status(tmp_path, valid_data):
    valid_data["status"] = "draft"
    valid_data["steps"][0]["postcondition"] = {"kind": "review_required", "reason": "Choose a check."}
    draft = Workflow.model_validate(valid_data)
    path = save_workflow(draft, tmp_path / "draft.yaml")
    assert draft.pending_review() == ["Step 1: Choose a check."]
    with pytest.raises(ValueError, match="still a draft"):
        load_workflow(path, require_ready=True)
    valid_data["status"] = "ready"
    with pytest.raises(ValidationError, match="unresolved review"):
        Workflow.model_validate(valid_data)


@pytest.mark.parametrize("parameter", [
    {"type": "secret", "ask_each_time": True, "example": "private-do-not-print"},
    {"type": "secret", "ask_each_time": True, "default": "private-do-not-print"},
    {"type": "secret", "ask_each_time": False},
    {"type": "number", "example": "1500"}, {"type": "number", "example": float("inf")},
    {"type": "choice", "example": "March", "choices": ["January", "February"]},
    {"type": "month", "example": "2026-13"}, {"type": "date", "example": "2026-02-30"},
])
def test_invalid_parameters_and_secret_safe_errors(valid_data, parameter):
    valid_data["parameters"] = {"value": parameter}
    with pytest.raises(ValidationError) as error:
        Workflow.model_validate(valid_data)
    assert "private-do-not-print" not in validation_message(error.value)


def test_secret_target_cannot_store_a_literal(valid_data):
    valid_data["steps"][0].update({
        "action": "type", "target": {"name": "Password", "sensitive": True},
        "value": "private-do-not-print", "postcondition": {
            "kind": "input_value", "target": {"name": "Password"},
            "value": "private-do-not-print", "sensitive": True,
        },
    })
    with pytest.raises(ValidationError) as error:
        Workflow.model_validate(valid_data)
    assert "private-do-not-print" not in validation_message(error.value)


def test_secret_postcondition_cannot_store_a_literal(valid_data):
    valid_data["parameters"] = {"password": {"type": "secret", "ask_each_time": True}}
    valid_data["steps"][0].update({
        "action": "type", "target": {"name": "Password", "sensitive": True},
        "value": "{password}", "postcondition": {
            "kind": "input_value", "target": {"name": "Password", "sensitive": True},
            "value": "private-do-not-print", "sensitive": True,
        },
    })
    with pytest.raises(ValidationError) as error:
        Workflow.model_validate(valid_data)
    assert "private-do-not-print" not in validation_message(error.value)


@pytest.mark.parametrize("template", ["{unknown}", "{value.__class__}", "{value[0]}", "{unknown", "{value!r}"])
def test_unknown_or_executable_templates_are_rejected(valid_data, template):
    valid_data["steps"][0]["postcondition"]["text"] = template
    with pytest.raises(ValidationError):
        Workflow.model_validate(valid_data)


def test_escaped_literal_braces_do_not_create_references(valid_data):
    valid_data["steps"][0]["postcondition"]["text"] = "Ready {{literal}}"
    Workflow.model_validate(valid_data)


def test_download_binding_must_be_defined_once_and_before_use(valid_data):
    first = valid_data["steps"][0]
    first["postcondition"] = {"kind": "text_visible", "text": "{downloaded_file}"}
    second = deepcopy(first)
    second["id"] = 2
    second["postcondition"] = {"kind": "file_downloaded", "save_as": "downloaded_file", "pattern": "*.csv"}
    valid_data["steps"].append(second)
    with pytest.raises(ValidationError, match="unavailable value"):
        Workflow.model_validate(valid_data)
    first["postcondition"], second["postcondition"] = second["postcondition"], first["postcondition"]
    Workflow.model_validate(valid_data)
    second["postcondition"] = deepcopy(first["postcondition"])
    with pytest.raises(ValidationError, match="redefines value"):
        Workflow.model_validate(valid_data)


@pytest.mark.parametrize("content", ["name: first\nname: second\n", "!!python/object/apply:os.system ['echo unsafe']"])
def test_yaml_duplicate_keys_and_unsafe_tags(tmp_path, content):
    path = tmp_path / "invalid.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_workflow(path)
