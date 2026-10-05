"""Exercise the Phase 2 contract against local HTML; no model or remote server."""

import fnmatch
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from apprentice.recorder.record import INJECT, take_screenshot
from apprentice.store.workflow import load_workflow

ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "src/apprentice/portal/index.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def test_recorder_observes_dom_changes_without_secret_values(browser):
    events = []
    with browser.new_context() as context:
        context.expose_binding("__apprenticeEmit", lambda source, payload: events.append(json.loads(payload)))
        context.add_init_script(INJECT)
        page = context.new_page()
        page.goto(PORTAL.as_uri())
        before = page.evaluate("window.__apprenticeState()")
        assert not any(element["id"] == "exportBtn" for element in before["elements"])
        page.locator("#user").fill("demo")
        page.locator("#pass").fill("browser-test-secret")
        page.locator("#loginBtn").click()
        after = page.evaluate("window.__apprenticeState()")
        assert any(element["id"] == "exportBtn" for element in after["elements"])
        month = next(element for element in after["elements"] if element["id"] == "month")
        assert month["options"][8] == "September"
        password = next(event for event in events if event["element"]["id"] == "pass")
        assert password["value"] == "<redacted>" and password["sensitive"]
        assert "browser-test-secret" not in json.dumps(events + [before, after])


def test_marked_plain_text_secrets_are_redacted_and_masked(browser, tmp_path):
    events = []
    html = '<label for="token">API token</label><input id="token" data-sensitive><button>Continue</button>'
    with browser.new_context() as context:
        context.expose_binding("__apprenticeEmit", lambda source, payload: events.append(json.loads(payload)))
        context.add_init_script(INJECT)
        page = context.new_page()
        page.goto("data:text/html," + quote(html))
        page.locator("#token").fill("plain-text-test-secret")
        page.get_by_role("button", name="Continue").click()
        token = next(event for event in events if event["kind"] == "type")
        assert token["value"] == "<redacted>" and token["element"]["sensitive"]
        assert "plain-text-test-secret" not in json.dumps(events)
        box = page.locator("#token").bounding_box()
        screenshot = tmp_path / "masked.png"
        take_screenshot(page, screenshot)
        with Image.open(screenshot) as image:
            assert image.getpixel((int(box["x"] + box["width"] / 2),
                                   int(box["y"] + box["height"] / 2)))[:3] == (0, 0, 0)


def test_sensitive_textarea_default_is_not_in_dom_evidence(browser, tmp_path):
    html = '<label>Token<textarea data-sensitive>initial-hidden-secret</textarea></label>'
    source = tmp_path / "secret-page.html"
    source.write_text(html, encoding="utf-8")
    with browser.new_context() as context:
        context.expose_binding("__apprenticeEmit", lambda source, payload: None)
        context.add_init_script(INJECT)
        page = context.new_page()
        page.goto(source.as_uri())
        state = page.evaluate("window.__apprenticeState()")
        assert "initial-hidden-secret" not in json.dumps(state)
        assert state["elements"][0]["label"] == "Token"


def test_reviewed_workflow_actions_and_every_postcondition_match_portal(browser, tmp_path):
    workflow = load_workflow(ROOT / "workflows/monthly_report.yaml", require_ready=True)
    values = {"username": "demo", "password": "test-password", "total_amount": 1500, "month": "September"}
    with browser.new_context(accept_downloads=True) as context:
        page = context.new_page()
        page.goto(PORTAL.as_uri())
        for step in workflow.steps:
            target = page.locator(step.target.css)
            check = step.postcondition
            if step.action == "type":
                target.fill(step.value.format(**values))
            elif step.action == "select":
                target.select_option(label=step.value.format(**values))
            elif step.action == "upload":
                assert step.id in workflow.confirm_before
                target.set_input_files(step.value.format(**values))
            elif check.kind == "file_downloaded":
                with page.expect_download() as pending:
                    target.click()
                download = pending.value
                assert fnmatch.fnmatch(download.suggested_filename, check.pattern)
                saved = tmp_path / download.suggested_filename
                download.save_as(saved)
                assert saved.is_file() and saved.read_text().startswith("item,total")
                values[check.save_as] = str(saved)
            else:
                if step.irreversible:
                    assert step.id in workflow.confirm_before
                target.click()
            if check.kind == "input_value":
                assert page.locator(check.target.css).input_value() == check.value.format(**values)
            elif check.kind == "selection":
                assert page.locator(check.target.css).locator("option:checked").inner_text() == check.value.format(**values)
            elif check.kind == "element_visible":
                assert page.locator(check.target.css).is_visible()
            elif check.kind == "files_selected":
                selected = page.locator(check.target.css).evaluate("el => [...el.files].map(f => f.name)")
                assert selected == [Path(value.format(**values)).name for value in check.files]
            elif check.kind == "text_visible":
                assert check.text.format(**values) in page.locator(check.target.css).inner_text()
            else:
                assert check.kind == "file_downloaded"
        assert page.evaluate("window.__submissions") == [
            {"month": "September", "amount": "1500", "file": "report-2026-09.csv"}
        ]
