import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from apprentice.labeler.build_workflow import build, main
from apprentice.store.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/monthly_report.json"
REVIEW = ROOT / "workflows/reviews/monthly_report.yaml"


def record(tmp_path, events, name="example"):
    path = tmp_path / "steps.json"
    path.write_text(json.dumps({"name": name, "start_url": "http://localhost:8000/", "steps": events}), encoding="utf-8")
    return path


def click(name="Open", **extra):
    return {"kind": "click", "element": {"tag": "button", "text": name, "css": "#button"}, **extra}


def test_original_recording_becomes_explicit_draft():
    data = build(FIXTURE)
    workflow = Workflow.model_validate(data)
    assert workflow.status == "draft"
    assert len(workflow.steps) == 7
    assert len(workflow.pending_review()) == 2
    assert data["parameters"]["total_amount"]["type"] == "number"
    assert data["steps"][5]["value"] == "{downloaded_file}"
    assert data["steps"][5]["source_steps"] == [8, 9]
    assert "test-secret-never-save" not in json.dumps(data)
    with pytest.raises(ValidationError, match="unresolved review"):
        build(FIXTURE, ready=True)


def test_review_adds_month_and_recomputes_confirmations():
    data = build(FIXTURE, review_path=REVIEW, ready=True)
    assert data["status"] == "ready"
    assert len(data["steps"]) == 8
    assert data["parameters"]["month"]["example"] == "September"
    assert data["steps"][4]["value"] == "{month}"
    assert data["steps"][4]["source_steps"] == []
    assert data["confirm_before"] == [7, 8]
    assert data["steps"][-1]["postcondition"]["text"] == "Submitted: {month}, {total_amount},"
    assert not Workflow.model_validate(data).pending_review()


def test_parameter_names_do_not_collide_or_overwrite_prior_values(tmp_path):
    events = [
        {"kind": "type", "element": {"tag": "input", "label": "A B", "css": "#first"}, "value": "first"},
        {"kind": "type", "element": {"tag": "input", "label": "A-B", "css": "#second"}, "value": "second"},
        {"kind": "type", "element": {"tag": "input", "label": "A B", "css": "#first"}, "value": "changed"},
        {"kind": "type", "element": {"tag": "input", "label": "A B", "css": "#first"}, "value": "first"},
    ]
    data = build(record(tmp_path, events), ready=True)
    assert list(data["parameters"]) == ["a_b", "a_b_2", "a_b_3"]
    assert [step["value"] for step in data["steps"]] == ["{a_b}", "{a_b_2}", "{a_b_3}", "{a_b}"]


@pytest.mark.parametrize("value,expected", [(".5", 0.5), ("0123", 123), ("1e3", 1000), ("10000000000000001", 10000000000000001)])
def test_number_examples_are_typed_without_losing_integer_precision(tmp_path, value, expected):
    event = {"kind": "type", "element": {"tag": "input", "type": "number", "label": "Amount"}, "value": value}
    data = build(record(tmp_path, [event]), ready=True)
    assert data["parameters"]["amount"]["example"] == expected
    assert data["steps"][0]["target"]["role"] == "spinbutton"


def test_select_choices_and_explicit_aria_role(tmp_path):
    event = {"kind": "select", "element": {"tag": "select", "label": "Wrong label", "aria_label": "Month",
             "role": "combobox", "options": ["January", "September"]}, "value": "September"}
    data = build(record(tmp_path, [event]), ready=True)
    assert data["parameters"]["month"]["choices"] == ["January", "September"]
    assert data["steps"][0]["target"]["name"] == "Month"


def test_checked_state_is_a_boolean_parameter(tmp_path):
    event = {"kind": "check", "element": {"tag": "input", "type": "checkbox", "label": "Include tax"}, "value": True}
    data = build(record(tmp_path, [event]), ready=True)
    assert data["parameters"]["include_tax"] == {"type": "boolean", "example": True, "ask_each_time": False}
    assert data["steps"][0]["value"] == "{include_tax}"


def test_sensitive_upload_has_no_filename_example(tmp_path):
    event = {"kind": "upload", "element": {"tag": "input", "type": "file", "label": "Credential file", "sensitive": True},
             "value": ["private-secret-file.csv"]}
    data = build(record(tmp_path, [event]), ready=True)
    assert data["parameters"]["credential_file"] == {"type": "secret", "ask_each_time": True}
    assert "private-secret-file.csv" not in json.dumps(data)


def test_sensitive_checked_state_has_no_saved_example(tmp_path):
    event = {"kind": "check", "element": {"tag": "input", "type": "checkbox", "label": "Private option", "sensitive": True},
             "value": "<redacted>", "sensitive": True}
    data = build(record(tmp_path, [event]), ready=True)
    assert data["parameters"]["private_option"] == {"type": "boolean", "ask_each_time": True}


def test_multiple_downloads_and_multiple_uploaded_files(tmp_path):
    events = [click("Export one"), {"kind": "download", "filename": "one.csv"},
              click("Export two"), {"kind": "download", "filename": "two.csv"},
              {"kind": "upload", "element": {"tag": "input", "type": "file", "label": "Files"}, "value": ["one.csv", "two.csv"]}]
    data = build(record(tmp_path, events), ready=True)
    assert data["steps"][-1]["value"] == ["{downloaded_file}", "{downloaded_file_2}"]
    assert data["confirm_before"] == [3]


def test_ambiguous_download_filename_requires_review(tmp_path):
    events = [click("Export one"), {"kind": "download", "filename": "report.csv"},
              click("Export two"), {"kind": "download", "filename": "report.csv"},
              {"kind": "upload", "element": {"tag": "input", "type": "file", "label": "File"}, "value": ["report.csv"]}]
    data = build(record(tmp_path, events))
    assert data["steps"][-1]["value"] == "{file}"
    assert "Multiple downloads" in data["steps"][-1]["review_notes"][0]


def test_intermediate_navigation_is_verified(tmp_path):
    events = [click("Continue"), {"kind": "navigate", "url": "http://localhost:8000/reports"},
              click("Export"), {"kind": "download", "filename": "report.csv"}]
    data = build(record(tmp_path, events), ready=True)
    assert len(data["steps"]) == 2
    assert data["steps"][0]["postcondition"] == {"kind": "url", "url": "http://localhost:8000/reports"}


def test_click_checks_use_observed_changes(tmp_path):
    export = {"tag": "button", "text": "Export", "css": "#export"}
    events = [click("Sign in", before_state={"elements": [], "messages": []},
                    after_state={"elements": [export], "messages": []}),
              {"kind": "click", "element": export}, {"kind": "download", "filename": "report.csv"}]
    data = build(record(tmp_path, events), ready=True)
    assert data["steps"][0]["postcondition"]["kind"] == "element_visible"
    events[0]["before_state"]["elements"] = [export]
    data = build(record(tmp_path, events))
    assert data["steps"][0]["postcondition"]["kind"] == "review_required"


@pytest.mark.parametrize("text,kind", [("Saved successfully", "text_visible"), ("Missing file or amount", "review_required")])
def test_observed_feedback_does_not_bless_an_error(tmp_path, text, kind):
    event = click("Save", before_state={"messages": []},
                  after_state={"messages": [{"css": "#result", "text": text}]})
    assert build(record(tmp_path, [event]))["steps"][0]["postcondition"]["kind"] == kind


def test_unknown_empty_and_navigation_only_recordings_fail_clearly(tmp_path):
    for events in ([], [{"kind": "mystery"}], [{"kind": "navigate", "url": "http://localhost:8000/"}]):
        with pytest.raises(ValueError):
            build(record(tmp_path, events))


def test_review_cannot_claim_recorded_provenance(tmp_path):
    review = tmp_path / "review.yaml"
    review.write_text(yaml.safe_dump({"steps": {1: {"source_steps": [999]}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        build(FIXTURE, review_path=review)


def test_cli_builds_readable_yaml_and_preserves_an_existing_workflow(tmp_path, capsys):
    path = tmp_path / "workflow.yaml"
    args = [str(FIXTURE), "--review", str(REVIEW), "--ready", "--output", str(path)]
    assert main(args) == 0
    assert "Saved ready workflow" in capsys.readouterr().out
    original = path.read_bytes()
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert path.read_bytes() == original
