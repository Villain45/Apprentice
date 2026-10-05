"""Run the production executor against local HTML, including failure boundaries."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from apprentice.executor.browser import BrowserExecutor
from apprentice.executor.run_workflow import command_parameters, main
from apprentice.executor import run_workflow
from apprentice.executor.runtime import Audit, Control, ExecutionError, ParameterError, bind_parameters, render
from apprentice.store.workflow import Workflow, load_workflow

ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "src/apprentice/portal/index.html"
VALUES = {"username": "demo", "password": "executor-test-secret", "month": "September", "total_amount": 1500}


@pytest.fixture(scope="module")
def engine():
    runner = asyncio.Runner()
    playwright = runner.run(async_playwright().start())
    browser = runner.run(playwright.chromium.launch(headless=True))
    yield runner, browser
    runner.run(browser.close())
    runner.run(playwright.stop())
    runner.close()


def simple_workflow(url, steps, *, parameters=None, confirm_before=None):
    return Workflow.model_validate({
        "name": "Test task", "description": "A local executor check.", "status": "ready",
        "start_url": url, "parameters": parameters or {}, "steps": steps,
        "confirm_before": confirm_before or [],
    })


def click_step(name="Open", *, step_id=1, postcondition=None, css="#open"):
    return {"id": step_id, "action": "click", "target": {"role": "button", "name": name, "css": css},
            "purpose": f"Use {name}.", "postcondition": postcondition or {"kind": "text_visible", "text": "Done"}}


def html_file(tmp_path, html, name="page.html"):
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return path.as_uri()


@pytest.mark.parametrize("variant", ["baseline", "moved", "renamed", "banner", "dark", "zoom"])
def test_real_executor_completes_each_portal_variant(engine, tmp_path, variant):
    runner, browser = engine

    async def case():
        workflow = load_workflow(ROOT / "workflows/monthly_report.yaml", require_ready=True)
        requests = []
        audit = Audit(tmp_path / "audit.jsonl")

        def approve(request):
            requests.append((request.kind, request.step_id))
            return True

        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            executor = BrowserExecutor(page, tmp_path, confirm=approve, audit=audit)
            result = await executor.run(workflow, VALUES, start_url=PORTAL.as_uri() + "?v=" + variant)
            assert result.status == "completed", result
            assert result.completed_steps == tuple(range(1, 9))
            assert requests == [("workflow", None), ("step", 7), ("step", 8)]
            assert await page.evaluate("window.__submissions") == [
                {"month": "September", "amount": "1500", "file": "report-2026-09.csv"}
            ]
            assert (tmp_path / "downloads/step-4-attempt-1/report-2026-09.csv").read_text() == "item,total\nlicenses,1200\nsupport,300\n"
        assert "executor-test-secret" not in audit.path.read_text(encoding="utf-8")
        verified = [entry["step"] for entry in audit.events if entry["event"] == "step_verified"]
        assert verified == list(range(1, 9))
        strategies = [entry["strategy"] for entry in audit.events if entry["event"] == "target_resolved"]
        assert ("css" in strategies) == (variant == "renamed")

    runner.run(case())


def test_semantic_targets_work_when_recorded_css_is_stale(engine, tmp_path):
    runner, browser = engine

    async def case():
        workflow = load_workflow(ROOT / "workflows/monthly_report.yaml", require_ready=True)
        for step in workflow.steps:
            if step.target and step.target.name:
                step.target.css = "#obsolete"
            check_target = getattr(step.postcondition, "target", None)
            if check_target and check_target.name:
                check_target.css = "#obsolete"
        audit = Audit()
        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True, audit=audit).run(
                workflow, VALUES, start_url=PORTAL.as_uri())
            assert result.status == "completed"
        assert not any(item.get("strategy") == "css" for item in audit.events)

    runner.run(case())


def test_ambiguous_semantics_stop_without_using_css_to_choose_a_button(engine, tmp_path):
    runner, browser = engine
    url = html_file(tmp_path, '<script>window.calls=0</script><button id="open" onclick="calls++">Open</button><button>Open</button>')

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            workflow = simple_workflow(url, [click_step()])
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True).run(workflow, {})
            assert result.status == "failed" and result.completed_steps == ()
            assert await page.evaluate("window.calls") == 0

    runner.run(case())


def test_failed_verification_retries_safe_fill_once_and_never_advances(engine, tmp_path):
    runner, browser = engine
    url = html_file(tmp_path, '<script>window.fills=0;window.sends=0</script><label for="value">Value</label>'
                    '<input id="value" oninput="fills++"><button onclick="sends++">Send</button>')
    steps = [{"id": 1, "action": "type", "target": {"role": "textbox", "name": "Value"},
              "value": "{value}", "purpose": "Enter a value.", "postcondition": {
                  "kind": "input_value", "target": {"role": "textbox", "name": "Value"}, "value": "never"}},
             click_step("Send", step_id=2)]

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            audit = Audit()
            workflow = simple_workflow(url, steps, parameters={"value": {"type": "text"}}, confirm_before=[2])
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True, audit=audit,
                                           control=Control(timeout_ms=150)).run(workflow, {"value": "actual"})
            assert result.status == "failed" and result.failed_step == 1
            assert result.completed_steps == ()
            assert await page.evaluate("window.fills") == 2
            assert await page.evaluate("window.sends") == 0
            assert len([item for item in audit.events if item["event"] == "step_retry"]) == 1

    runner.run(case())


def test_uncertain_submission_is_never_repeated(engine, tmp_path):
    runner, browser = engine
    url = html_file(tmp_path, '<script>window.submits=0</script><button id="open" onclick="submits++">Submit</button>')

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            audit = Audit()
            workflow = simple_workflow(url, [click_step("Submit")], confirm_before=[1])
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True, audit=audit,
                                           control=Control(timeout_ms=150)).run(workflow, {})
            assert result.status == "failed" and "may have completed" in result.message
            assert await page.evaluate("window.submits") == 1
            assert not any(item["event"] == "step_retry" for item in audit.events)

    runner.run(case())


@pytest.mark.parametrize("confirm", [None, lambda _: False])
def test_run_requires_approval_before_navigation(engine, tmp_path, confirm):
    runner, browser = engine

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            workflow = load_workflow(ROOT / "workflows/monthly_report.yaml")
            result = await BrowserExecutor(page, tmp_path, confirm=confirm).run(workflow, VALUES)
            assert result.status == "declined" and result.completed_steps == ()
            assert page.url == "about:blank"
            assert not (tmp_path / "downloads").exists()

    runner.run(case())


def test_declined_upload_keeps_file_unselected_and_does_not_submit(engine, tmp_path):
    runner, browser = engine

    async def case():
        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            workflow = load_workflow(ROOT / "workflows/monthly_report.yaml")
            result = await BrowserExecutor(page, tmp_path, confirm=lambda request: request.step_id != 7).run(
                workflow, VALUES, start_url=PORTAL.as_uri())
            assert result.status == "declined" and result.failed_step == 7
            assert result.completed_steps == tuple(range(1, 7))
            assert await page.locator("#file").evaluate("el => el.files.length") == 0
            assert await page.evaluate("window.__submissions") == []

    runner.run(case())


def test_stop_interrupts_a_blocked_browser_action(engine, tmp_path):
    runner, browser = engine
    url = html_file(tmp_path, '<button id="open" disabled>Open</button>')

    async def case():
        context = await browser.new_context()
        try:
            page = await context.new_page()
            control = Control(timeout_ms=10000)
            audit = Audit()

            async def stop():
                while not any(item["event"] == "target_resolved" for item in audit.events):
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)  # Let Playwright begin waiting for the disabled button.
                control.stop.set()

            stopper = asyncio.create_task(stop())
            started = time.monotonic()
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True, control=control, audit=audit).run(
                simple_workflow(url, [click_step()]), {})
            await stopper
            assert result.status == "stopped" and result.completed_steps == ()
            assert page.is_closed()
            assert time.monotonic() - started < 2
        finally:
            await context.close()

    runner.run(case())


def test_stop_interrupts_a_pending_confirmation(engine, tmp_path):
    runner, browser = engine

    async def case():
        context = await browser.new_context()
        try:
            page = await context.new_page()
            control = Control()
            reached = asyncio.Event()

            async def approve(_):
                reached.set()
                await asyncio.Event().wait()

            async def stop():
                await reached.wait()
                control.stop.set()

            stopper = asyncio.create_task(stop())
            result = await BrowserExecutor(page, tmp_path, confirm=approve, control=control).run(
                load_workflow(ROOT / "workflows/monthly_report.yaml"), VALUES)
            await stopper
            assert result.status == "stopped" and page.is_closed()
        finally:
            await context.close()

    runner.run(case())


def test_task_cancellation_closes_the_context_and_records_a_stop(engine, tmp_path):
    runner, browser = engine
    url = html_file(tmp_path, '<button id="open" disabled>Open</button>')

    async def case():
        context = await browser.new_context()
        try:
            page = await context.new_page()
            audit = Audit()
            executor = BrowserExecutor(page, tmp_path, confirm=lambda _: True, audit=audit)
            task = asyncio.create_task(executor.run(simple_workflow(url, [click_step()]), {}))
            while not any(item["event"] == "target_resolved" for item in audit.events):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert page.is_closed()
            assert audit.events[-1]["event"] == "run_finished"
            assert audit.events[-1]["status"] == "stopped"
        finally:
            await context.close()

    runner.run(case())


def test_bad_download_pattern_stops_before_later_form_actions(engine, tmp_path):
    runner, browser = engine

    async def case():
        workflow = load_workflow(ROOT / "workflows/monthly_report.yaml")
        workflow.steps[3].postcondition.pattern = "*.pdf"
        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True).run(
                workflow, VALUES, start_url=PORTAL.as_uri())
            assert result.status == "failed" and result.failed_step == 4
            assert result.completed_steps == (1, 2, 3)
            assert await page.locator("#amount").input_value() == ""
            assert await page.evaluate("window.__submissions") == []
            assert not (tmp_path / "downloads").exists()

    runner.run(case())


def test_navigation_selection_by_value_boolean_and_delayed_text_checks(engine, tmp_path):
    runner, browser = engine
    url = html_file(tmp_path, '<label for="month">Month</label><select id="month"><option value="j">January</option>'
                    '<option value="s">September</option></select><label><input type="checkbox" id="tax">Include tax</label>'
                    '<button id="open" onclick="setTimeout(()=>document.getElementById(\'result\').textContent=\'Done\',30)">Open</button>'
                    '<p id="result"></p>')
    start = html_file(tmp_path, "<p>Start</p>", "start.html")
    steps = [
        {"id": 1, "action": "navigate", "value": url, "purpose": "Open the form.", "postcondition": {"kind": "url", "url": url}},
        {"id": 2, "action": "select", "target": {"role": "combobox", "name": "Month"}, "value": "{month}",
         "purpose": "Select month.", "postcondition": {"kind": "selection", "target": {"role": "combobox", "name": "Month"}, "value": "{month}", "by": "value"}},
        {"id": 3, "action": "check", "target": {"role": "checkbox", "name": "Include tax"}, "value": "{tax}",
         "purpose": "Include tax.", "postcondition": {"kind": "checked", "target": {"role": "checkbox", "name": "Include tax"}, "checked": "{tax}"}},
        click_step(step_id=4),
    ]

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            workflow = simple_workflow(start, steps, parameters={"month": {"type": "choice", "choices": ["j", "s"]}, "tax": {"type": "boolean"}})
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True).run(workflow, {"month": "s", "tax": "true"})
            assert result.status == "completed" and result.completed_steps == (1, 2, 3, 4)
            assert await page.locator("#tax").is_checked()

    runner.run(case())


def test_multiple_files_can_be_set_on_a_hidden_file_input(engine, tmp_path):
    runner, browser = engine
    one, two = tmp_path / "one.csv", tmp_path / "two.csv"
    one.write_text("one")
    two.write_text("two")
    url = html_file(tmp_path, '<label for="files">Reports</label><input id="files" type="file" multiple style="display:none">')
    step = {"id": 1, "action": "upload", "target": {"role": "file_input", "name": "Reports"},
            "value": ["{one}", "{two}"], "purpose": "Select both reports.", "postcondition": {
                "kind": "files_selected", "target": {"role": "file_input", "name": "Reports"}, "files": ["{one}", "{two}"]}}

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            workflow = simple_workflow(url, [step], parameters={"one": {"type": "file"}, "two": {"type": "file"}}, confirm_before=[1])
            result = await BrowserExecutor(page, tmp_path, confirm=lambda _: True).run(workflow, {"one": str(one), "two": str(two)})
            assert result.status == "completed"

    runner.run(case())


def test_runtime_inputs_are_required_and_validated_without_secret_echo():
    workflow = load_workflow(ROOT / "workflows/monthly_report.yaml")
    with pytest.raises(ParameterError, match="Missing parameter"):
        bind_parameters(workflow, {})
    for changed in ({"month": "Unknown"}, {"total_amount": "NaN"}, {"password": 12}):
        with pytest.raises(ParameterError) as error:
            bind_parameters(workflow, {**VALUES, **changed})
        assert "executor-test-secret" not in str(error.value)
    assert bind_parameters(workflow, {**VALUES, "total_amount": "0123"})["total_amount"] == 123
    with pytest.raises(ParameterError):
        command_parameters(workflow, ["password=executor-test-secret"])
    assert render("{enabled}", {"enabled": False}) is False
    assert render("{{literal}} {amount}", {"amount": 1500.0}) == "{literal} 1500"


def test_cli_dry_run_creates_no_browser_or_run_artifacts(tmp_path, capsys):
    root = tmp_path / "runs"
    assert main([str(ROOT / "workflows/monthly_report.yaml"), "--run-root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "Dry run:" in output and "confirmation required" in output
    assert "executor-test-secret" not in output
    assert not root.exists()


def test_cli_rejects_secrets_in_arguments_without_echoing_them(capsys):
    with pytest.raises(SystemExit) as error:
        main([str(ROOT / "workflows/monthly_report.yaml"), "--param", "password=executor-test-secret"])
    assert error.value.code == 2
    assert "executor-test-secret" not in capsys.readouterr().err


def test_cli_execute_prompts_for_secret_and_runs_the_reviewed_workflow(tmp_path, monkeypatch, capsys):
    prompts = []

    async def answer(message, *, secret=False):
        prompts.append((message, secret))
        return "cli-only-test-secret" if secret else "yes"

    monkeypatch.setattr(run_workflow, "prompt", answer)
    root = tmp_path / "runs"
    assert main([str(ROOT / "workflows/monthly_report.yaml"), "--execute", "--headless", "--no-hotkey",
                 "--url", PORTAL.as_uri(), "--run-root", str(root), "--param", "username=demo",
                 "--param", "month=September", "--param", "total_amount=1500"]) == 0
    output = capsys.readouterr().out
    assert "Completed: 8/8 steps verified." in output
    assert "cli-only-test-secret" not in output
    assert len([item for item in prompts if item[1]]) == 1
    assert len([item for item in prompts if not item[1]]) == 3
    logs = list(root.glob("*/audit.jsonl"))
    assert len(logs) == 1 and "cli-only-test-secret" not in logs[0].read_text()


def test_cli_stop_hotkey_binding_cancels_before_browser_start(tmp_path, monkeypatch, capsys):
    import pynput.keyboard

    calls = []

    class FakeHotkeys:
        def __init__(self, bindings):
            self.callback = bindings["<ctrl>+<alt>+<esc>"]

        def start(self):
            calls.append("start")

        def wait(self):
            self.callback()

        def stop(self):
            calls.append("stop")

    async def pending_input(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(pynput.keyboard, "GlobalHotKeys", FakeHotkeys)
    monkeypatch.setattr(run_workflow, "prompt", pending_input)
    root = tmp_path / "runs"
    assert main([str(ROOT / "workflows/monthly_report.yaml"), "--execute", "--headless", "--run-root", str(root)]) == 130
    assert calls == ["start", "stop"]
    assert not root.exists()
    assert "Stopped before execution." in capsys.readouterr().out


def test_audit_does_not_truncate_an_existing_run(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = Audit(path)
    audit.write("run_finished", status="completed")
    with pytest.raises(FileExistsError):
        Audit(path)
    assert json.loads(path.read_text())["status"] == "completed"
