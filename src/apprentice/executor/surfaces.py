"""One runtime for mixed workflows, retaining the established browser executor."""

from __future__ import annotations

import asyncio
import inspect

from playwright.async_api import Error as PlaywrightError

from apprentice.executor.browser import BrowserExecutor
from apprentice.executor.filesystem import FilesystemExecutor
from apprentice.executor.runtime import (
    Audit, Confirmation, ConfirmationDeclined, Control, ExecutionError, RunResult,
    StopRequested, bind_parameters, browser_url, render, text,
)
from apprentice.store.system_workflow import ApplicationStep, FilesystemStep, TerminalStep, needs_confirmation
from apprentice.store.workflow import validate_workflow
from apprentice.recorder.replay_guard import replay_guarded


class SurfaceExecutor:
    def __init__(self, run_dir, *, page=None, browser_factory=None, filesystem=None,
                 terminal=None, application=None, confirm=None, control=None, audit=None):
        self.run_dir = run_dir
        self.page = page
        self.browser_factory = browser_factory
        self.filesystem = filesystem
        self._injected_filesystem = filesystem
        self.terminal = self._injected_terminal = terminal
        self.application = self._injected_application = application
        self.confirm = confirm
        self.control = control if control is not None else Control()
        self.audit = audit if audit is not None else Audit()
        self.browser = None

    async def approve(self, request):
        # Same approval implementation as the MVP, including async cancellation and strict True.
        return await BrowserExecutor.approve(self, request)

    async def get_browser(self, workflow):
        self.control.check()
        if self.page is None:
            if self.browser_factory is None:
                raise ExecutionError("A browser adapter is required for this workflow.")
            self.page = await self.control.wait(self.browser_factory())
        self.browser = BrowserExecutor(self.page, self.run_dir, confirm=self.confirm,
                                       control=self.control, audit=self.audit)
        self.browser.workflow = workflow
        return self.browser

    @replay_guarded
    async def run(self, workflow, supplied, *, start_url=None):
        workflow = validate_workflow(workflow)
        if workflow.status != "ready":
            raise ExecutionError("Review the workflow and mark it ready before replay.")
        if workflow.version == 1:
            browser = await self.get_browser(workflow)
            return await browser.run(workflow, supplied, start_url=start_url)
        self.browser = None
        self.filesystem = self._injected_filesystem
        self.terminal = self._injected_terminal
        self.application = self._injected_application
        values = bind_parameters(workflow, supplied)
        completed, current, succeeded = [], None, False
        try:
            # Validate every capability and statically bound path before any navigation or mutation.
            if any(step.surface not in ("browser", "filesystem", "terminal", "application") for step in workflow.steps):
                raise ExecutionError("This workflow contains an unavailable replay surface. Resolve it before running.")
            has_browser = any(step.surface == "browser" for step in workflow.steps)
            if has_browser and self.page is None and self.browser_factory is None:
                raise ExecutionError("A browser adapter is required for this workflow.")
            url = start_url if start_url is not None else workflow.start_url
            if has_browser and url is not None:
                url = browser_url(text(render(url, values)))
            if start_url is not None and not has_browser:
                raise ExecutionError("A start URL override needs browser steps.")
            if self.filesystem is None:
                self.filesystem = FilesystemExecutor(
                    [render(root, values) for root in workflow.write_roots],
                    control=self.control, artifact_root=self.run_dir,
                )
            # Injected adapters use the same cancellation event too.
            self.filesystem.control = self.control
            self.control.check()
            await self.control.wait(asyncio.to_thread(self.filesystem.preflight, workflow, values))
            if any(isinstance(step, TerminalStep) for step in workflow.steps):
                if self.terminal is None:
                    from apprentice.executor.terminal import TerminalExecutor
                    self.terminal = TerminalExecutor(policy=self.filesystem.policy, run_dir=self.run_dir, control=self.control)
                self.terminal.control = self.control
                await self.control.wait(asyncio.to_thread(self.terminal.preflight, workflow, values))
            if any(isinstance(step, ApplicationStep) for step in workflow.steps):
                if self.application is None:
                    from apprentice.executor.application import ApplicationExecutor
                    self.application = ApplicationExecutor(run_dir=self.run_dir, control=self.control)
                self.application.control = self.control
                await self.control.wait(asyncio.to_thread(self.application.preflight, workflow, values))
            self.audit.write("run_started")
            await self.approve(Confirmation("workflow", workflow.name))
            for step in workflow.steps:
                current = step.id
                self.control.check()
                if step.surface == "browser":
                    if self.browser is None:
                        browser = await self.get_browser(workflow)
                        if url is not None:
                            await self.control.wait(browser.page.goto(
                                url, wait_until="domcontentloaded", timeout=self.control.timeout_ms))
                    await self.browser.step(step, values)
                else:
                    if needs_confirmation(step) or step.id in workflow.confirm_before:
                        await self.approve(Confirmation("step", workflow.name, step.id, step.purpose))
                    self.audit.write("step_started", step=step.id, action=step.action, attempt=1)
                    adapter = {"filesystem": self.filesystem, "terminal": self.terminal, "application": self.application}[step.surface]
                    # TerminalExecutor owns command timeout/readiness and process-tree cleanup.
                    if isinstance(step, (TerminalStep, ApplicationStep)):
                        await adapter.step(step, values)
                    else:
                        await self.control.wait(adapter.step(step, values))
                    self.audit.write("step_verified", step=step.id, action=step.action, attempt=1)
                completed.append(step.id)
            result = RunResult("completed", tuple(completed), message="Every step was verified.")
            succeeded = True
        except ConfirmationDeclined:
            result = RunResult("declined", tuple(completed), current, "Execution was declined.")
        except StopRequested:
            await self.close_stopped_context()
            result = RunResult("stopped", tuple(completed), current, "Stopped. Review the system state before another run.")
        except asyncio.CancelledError:
            await self.close_stopped_context()
            self.audit.write("run_finished", step=current, status="stopped")
            raise
        except (ExecutionError, PlaywrightError, OSError) as error:
            if current is not None:
                self.audit.write("step_failed", step=current, status="review_required")
            message = str(error) if isinstance(error, ExecutionError) else "Execution failed. Review the current step."
            result = RunResult("failed", tuple(completed), current, message)
        finally:
            values.clear()
            cleaned = await self.close_processes(stopped=not succeeded)
            if self.terminal is not None and getattr(self.terminal, "script_cleanup_pending", False):
                self.audit.write("cleanup_pending", status="private_script")
            if self.filesystem is not None:
                try:
                    self.filesystem.close()
                except Exception:
                    cleaned = False
                    self.audit.write("cleanup_pending", status="filesystem")
            if succeeded and not cleaned:
                result = RunResult("failed", tuple(completed), current,
                                   "Cleanup did not verify. Review the system state before another run.")
        self.audit.write("run_finished", step=result.failed_step, status=result.status)
        return result

    async def close_stopped_context(self):
        await self.close_processes(stopped=True)
        if self.filesystem is not None:
            self.filesystem.close()
        if self.browser is not None:
            await self.browser.close_stopped_context()
        elif self.page is not None:
            try:
                await asyncio.wait_for(self.page.context.close(), timeout=2)
            except (PlaywrightError, TimeoutError):
                pass

    async def close_processes(self, *, stopped=False):
        # Cleanup is independent of the already-set stop event.
        failed = False
        for adapter in (self.terminal, self.application):
            if adapter is None:
                continue
            try:
                operation = adapter.close(stopped=stopped or failed) if adapter is self.application else adapter.close()
                if inspect.isawaitable(operation):
                    cleanup = asyncio.ensure_future(operation)
                    while True:
                        try:
                            await asyncio.shield(cleanup)
                            break
                        except asyncio.CancelledError:
                            if cleanup.cancelled():
                                raise
            except Exception:
                failed = True
                self.audit.write("cleanup_pending", status="application" if adapter is self.application else "terminal")
        return not failed
