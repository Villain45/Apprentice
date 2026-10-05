"""Keep browser replay reliable while routing steps through other surfaces."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from apprentice.executor.filesystem import FilesystemExecutor
from apprentice.executor.runtime import Audit, Control
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.store.system_workflow import SystemWorkflow
from apprentice.store.workflow import load_workflow


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "src/apprentice/portal/index.html"
VALUES = {
    "username": "demo", "password": "routed-browser-private-password",
    "month": "September", "total_amount": 1500,
}


@pytest.fixture(scope="module")
def engine():
    runner = asyncio.Runner()
    playwright = runner.run(async_playwright().start())
    browser = runner.run(playwright.chromium.launch(headless=True))
    yield runner, browser
    runner.run(browser.close())
    runner.run(playwright.stop())
    runner.close()


def mixed_report_workflow():
    data = load_workflow(ROOT / "workflows/monthly_report.yaml").model_dump()
    data["version"] = 2
    data["parameters"]["workspace"] = {"type": "directory"}
    data["write_roots"] = ["{workspace}"]
    data["steps"][6]["value"] = "{moved_report}"
    data["steps"][6]["postcondition"]["files"] = ["{moved_report}"]
    folder = "{workspace}/processed"
    destination = folder + "/renamed-report.csv"
    added = [
        {
            "id": 5, "surface": "filesystem", "action": "create_directory",
            "path": folder, "purpose": "Create a folder for the downloaded report.",
            "postcondition": {"kind": "path_state", "path": folder, "state": "directory"},
        },
        {
            "id": 6, "surface": "filesystem", "action": "move_path",
            "source": "{downloaded_file}", "destination": destination,
            "save_as": "moved_report", "purpose": "Move and rename the new report.",
            "postcondition": {
                "kind": "path_transferred", "source": "{downloaded_file}",
                "destination": destination, "source_absent": True,
            },
        },
    ]
    data["steps"] = data["steps"][:4] + added + data["steps"][4:]
    for identifier, step in enumerate(data["steps"], 1):
        step["id"] = identifier
    data["confirm_before"] = [9, 10]
    return SystemWorkflow.model_validate(data)


@pytest.mark.parametrize("variant", ["baseline", "moved", "renamed", "banner", "dark", "zoom"])
def test_browser_download_file_move_and_upload_across_portal_variants(engine, tmp_path, variant):
    runner, browser = engine
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    audit = Audit(tmp_path / "audit.jsonl")
    requests = []

    def approve(request):
        requests.append((request.kind, request.step_id))
        return True

    async def case():
        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            executor = SurfaceExecutor(tmp_path / "run", page=page, confirm=approve, audit=audit)
            result = await executor.run(
                mixed_report_workflow(), {**VALUES, "workspace": str(workspace)},
                start_url=PORTAL.as_uri() + "?v=" + variant,
            )
            assert result.status == "completed", result
            assert result.completed_steps == tuple(range(1, 11))
            assert requests == [("workflow", None), ("step", 9), ("step", 10)]
            assert await page.evaluate("window.__submissions") == [
                {"month": "September", "amount": "1500", "file": "renamed-report.csv"}
            ]
        destination = workspace / "processed/renamed-report.csv"
        assert destination.read_text() == "item,total\nlicenses,1200\nsupport,300\n"
        assert not (tmp_path / "run/downloads/step-4-attempt-1/report-2026-09.csv").exists()
        verified = [event["step"] for event in audit.events if event["event"] == "step_verified"]
        assert verified == list(range(1, 11))
        started = [event["step"] for event in audit.events if event["event"] == "step_started"]
        assert started.count(4) == started.count(10) == 1
        logged = audit.path.read_text(encoding="utf-8")
        assert VALUES["password"] not in logged
        assert str(workspace) not in logged and json.dumps(str(workspace))[1:-1] not in logged
        assert "renamed-report.csv" not in logged
        assert all(set(event) <= {"time", "event", "step", "action", "attempt", "strategy", "status"}
                   for event in audit.events)

    runner.run(case())


def test_surface_router_preserves_the_existing_version_one_workflow(engine, tmp_path):
    runner, browser = engine

    async def case():
        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            result = await SurfaceExecutor(tmp_path, page=page, confirm=lambda _: True).run(
                load_workflow(ROOT / "workflows/monthly_report.yaml"), VALUES,
                start_url=PORTAL.as_uri(),
            )
            assert result.status == "completed" and result.completed_steps == tuple(range(1, 9))
            assert await page.evaluate("window.__submissions") == [
                {"month": "September", "amount": "1500", "file": "report-2026-09.csv"}
            ]

    runner.run(case())


def test_filesystem_failure_stops_the_remaining_browser_form_steps(engine, tmp_path):
    runner, browser = engine
    workspace = tmp_path / "workspace"
    destination = workspace / "processed"
    destination.mkdir(parents=True)
    original = destination / "renamed-report.csv"
    original.write_text("keep existing file", encoding="utf-8")

    async def case():
        async with await browser.new_context(accept_downloads=True) as context:
            page = await context.new_page()
            result = await SurfaceExecutor(tmp_path / "run", page=page, confirm=lambda _: True).run(
                mixed_report_workflow(), {**VALUES, "workspace": str(workspace)},
                start_url=PORTAL.as_uri(),
            )
            assert result.status == "failed" and result.failed_step in (5, 6)
            assert result.completed_steps in ((1, 2, 3, 4), (1, 2, 3, 4, 5))
            assert await page.locator("#amount").input_value() == ""
            assert await page.locator("#file").evaluate("el => el.files.length") == 0
            assert await page.evaluate("window.__submissions") == []
        assert original.read_text(encoding="utf-8") == "keep existing file"

    runner.run(case())


def test_mixed_workflow_declined_before_any_browser_or_filesystem_action(engine, tmp_path):
    runner, browser = engine
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    async def factory():
        calls.append("browser")
        return await browser.new_page()

    async def case():
        result = await SurfaceExecutor(
            tmp_path / "run", browser_factory=factory, confirm=lambda _: False,
        ).run(mixed_report_workflow(), {**VALUES, "workspace": str(workspace)}, start_url=PORTAL.as_uri())
        assert result.status == "declined" and result.completed_steps == ()
        assert calls == []
        assert list(workspace.iterdir()) == []

    runner.run(case())


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
def test_mutated_unsupported_step_is_rejected_before_creating_a_browser(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workflow = mixed_report_workflow()
    workflow.steps[-1].surface = "terminal"
    calls = []

    async def factory():
        calls.append("browser")
        raise AssertionError("Preflight must reject unsupported work before creating a browser.")

    async def case():
        executor = SurfaceExecutor(tmp_path / "run", browser_factory=factory, confirm=lambda _: True)
        try:
            result = await executor.run(workflow, {**VALUES, "workspace": str(workspace)})
        except ValueError:
            pass
        else:
            assert result.status == "failed" and result.completed_steps == ()
        assert calls == [] and list(workspace.iterdir()) == []

    asyncio.run(case())


def test_routed_stop_closes_the_browser_and_does_not_run_later_file_actions(engine, tmp_path):
    runner, browser = engine
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page_file = tmp_path / "blocked.html"
    page_file.write_text('<button id="open" disabled>Open</button>', encoding="utf-8")
    workflow = SystemWorkflow.model_validate({
        "status": "ready", "name": "Stop routed task", "description": "Stop before a later file operation.",
        "start_url": page_file.as_uri(), "parameters": {"workspace": {"type": "directory"}},
        "write_roots": ["{workspace}"], "steps": [
            {"id": 1, "surface": "browser", "action": "click", "target": {"role": "button", "name": "Open"},
             "purpose": "Open a blocked control.", "postcondition": {"kind": "text_visible", "text": "Done"}},
            {"id": 2, "surface": "filesystem", "action": "create_directory", "path": "{workspace}/later",
             "purpose": "Create a folder only after the browser step succeeds.",
             "postcondition": {"kind": "path_state", "path": "{workspace}/later", "state": "directory"}},
        ],
    })

    async def case():
        context = await browser.new_context()
        stopper = None
        try:
            page = await context.new_page()
            control, audit = Control(), Audit()

            async def stop():
                while not any(event["event"] == "target_resolved" for event in audit.events):
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                control.stop.set()

            stopper = asyncio.create_task(stop())
            started = time.monotonic()
            result = await SurfaceExecutor(tmp_path / "run", page=page, control=control, audit=audit,
                                           confirm=lambda _: True).run(workflow, {"workspace": str(workspace)})
            await asyncio.wait_for(stopper, timeout=2)
            assert result.status == "stopped" and result.completed_steps == ()
            assert page.is_closed() and time.monotonic() - started < 2
            assert not (workspace / "later").exists()
            assert json.dumps(audit.events).find(str(workspace)) == -1
        finally:
            if stopper is not None and not stopper.done():
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
            await context.close()

    runner.run(case())


def test_reused_router_rebinds_workspace_start_url_and_explicit_confirmations(engine, tmp_path):
    runner, browser = engine
    first_root, second_root = tmp_path / "first workspace", tmp_path / "second workspace"
    first_root.mkdir()
    second_root.mkdir()
    requests = []

    def workflow(label, confirmation):
        page_file = tmp_path / (label + ".html")
        page_file.write_text(
            '<script>window.calls=0</script><button onclick="calls++;'
            'document.getElementById(\'result\').textContent=\'' + label + " done'\">Open</button>"
            '<p id="result"></p>', encoding="utf-8",
        )
        return SystemWorkflow.model_validate({
            "status": "ready", "name": label, "description": "Run with an independent workspace and page.",
            "start_url": page_file.as_uri(), "parameters": {"workspace": {"type": "directory"}},
            "write_roots": ["{workspace}"], "confirm_before": [2] if confirmation else [],
            "steps": [
                {"id": 1, "surface": "filesystem", "action": "create_directory", "path": "{workspace}/made",
                 "purpose": "Create a directory in this run's workspace.",
                 "postcondition": {"kind": "path_state", "path": "{workspace}/made", "state": "directory"}},
                {"id": 2, "surface": "browser", "action": "click", "target": {"role": "button", "name": "Open"},
                 "purpose": "Complete this page's action.", "postcondition": {"kind": "text_visible", "text": label + " done"}},
            ],
        })

    first, second = workflow("First run", False), workflow("Second run", True)

    def approve(request):
        requests.append((request.name, request.kind, request.step_id))
        return True

    async def case():
        async with await browser.new_context() as context:
            page = await context.new_page()
            executor = SurfaceExecutor(tmp_path / "run", page=page, confirm=approve)
            result = await executor.run(first, {"workspace": str(first_root)})
            assert result.status == "completed", result
            result = await executor.run(second, {"workspace": str(second_root)})
            assert result.status == "completed", result
            assert (first_root / "made").is_dir() and (second_root / "made").is_dir()
            assert page.url == second.start_url and await page.evaluate("window.calls") == 1
            assert requests == [("First run", "workflow", None), ("Second run", "workflow", None),
                                ("Second run", "step", 2)]

    runner.run(case())


def test_stop_before_run_prevents_synchronous_filesystem_mutation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workflow = SystemWorkflow.model_validate({
        "status": "ready", "name": "Already stopped", "description": "Respect stop before creating a file.",
        "parameters": {"workspace": {"type": "directory"}}, "write_roots": ["{workspace}"],
        "steps": [{"id": 1, "surface": "filesystem", "action": "create_file", "path": "{workspace}/must-not-exist.txt",
                   "content": "private contents", "purpose": "Create a file only while the run remains active.",
                   "postcondition": {"kind": "path_state", "path": "{workspace}/must-not-exist.txt", "state": "file"}}],
    })

    async def case():
        control = Control()
        control.stop.set()
        requests = []
        executor = SurfaceExecutor(tmp_path / "run", control=control,
                                   confirm=lambda request: requests.append(request) or True)
        result = await executor.run(workflow, {"workspace": str(workspace)})
        assert result.status == "stopped" and result.completed_steps == ()
        assert requests == [] and list(workspace.iterdir()) == []

    asyncio.run(case())


def test_unavailable_late_explorer_step_is_rejected_before_browser_start(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class UnavailableExplorer:
        available = False

        def open_directory(self, path):
            raise AssertionError("An unavailable adapter must not be invoked.")

        def close(self):
            pass

    workflow = SystemWorkflow.model_validate({
        "status": "ready", "name": "Check all capabilities", "description": "Reject unavailable late steps before earlier side effects.",
        "start_url": PORTAL.as_uri(), "parameters": {"workspace": {"type": "directory"}},
        "write_roots": ["{workspace}"], "steps": [
            {"id": 1, "surface": "browser", "action": "navigate", "value": PORTAL.as_uri(),
             "purpose": "Open the portal.", "postcondition": {"kind": "url", "url": PORTAL.as_uri()}},
            {"id": 2, "surface": "filesystem", "action": "open_directory", "path": "{workspace}",
             "purpose": "Open the workspace in Explorer.", "postcondition": {"kind": "directory_open", "path": "{workspace}"}},
        ],
    })
    calls = []

    async def factory():
        calls.append("browser")
        raise AssertionError("A late capability gap must fail before browser startup.")

    async def case():
        filesystem = FilesystemExecutor([workspace], explorer=UnavailableExplorer())
        executor = SurfaceExecutor(tmp_path / "run", filesystem=filesystem, browser_factory=factory,
                                   confirm=lambda request: calls.append("approval") or True)
        result = await executor.run(workflow, {"workspace": str(workspace)})
        assert result.status == "failed" and result.completed_steps == ()
        assert calls == []

    asyncio.run(case())


def test_stop_during_mixed_workflow_approval_closes_the_provided_browser_context(engine, tmp_path):
    runner, browser = engine
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def case():
        context = await browser.new_context()
        stopper = None
        try:
            page = await context.new_page()
            control, reached = Control(), asyncio.Event()

            async def confirm(_):
                reached.set()
                await asyncio.Event().wait()

            async def stop():
                await reached.wait()
                control.stop.set()

            stopper = asyncio.create_task(stop())
            result = await SurfaceExecutor(tmp_path / "run", page=page, control=control, confirm=confirm).run(
                mixed_report_workflow(), {**VALUES, "workspace": str(workspace)}, start_url=PORTAL.as_uri(),
            )
            await asyncio.wait_for(stopper, timeout=2)
            assert result.status == "stopped" and page.is_closed()
            assert result.completed_steps == () and list(workspace.iterdir()) == []
        finally:
            if stopper is not None and not stopper.done():
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
            await context.close()

    runner.run(case())
