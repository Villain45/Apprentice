"""DOM-first browser replay with per-step verification and bounded safe retries."""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import re
from pathlib import Path, PureWindowsPath

from playwright.async_api import Error as PlaywrightError

from apprentice.executor.runtime import (
    Audit, Confirmation, ConfirmationDeclined, Control, DownloadArtifact, ExecutionError,
    RunResult, StopRequested, bind_parameters, browser_url, render, text,
)
from apprentice.executor.targets import AmbiguousTarget, TargetMissing, Targets
from apprentice.executor.verifier import VerificationFailed, Verifier
from apprentice.recorder.replay_guard import replay_guarded
from apprentice.store.workflow import FileDownloaded, Selection, Workflow, needs_confirmation


def download_name(suggested: str) -> str:
    name = PureWindowsPath(suggested).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
    if not name or name in (".", ".."):
        return "download"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I):
        name = "download-" + name
    return name


class BrowserExecutor:
    """Use a dedicated browser context; stopping closes that context immediately."""

    def __init__(self, page, run_dir, *, confirm=None, control=None, audit=None, base_dir=None):
        self.page = page
        self.run_dir = Path(run_dir)
        self.confirm = confirm
        self.control = control if control is not None else Control()
        self.audit = audit if audit is not None else Audit()
        self.base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
        self.targets = Targets(page, self.control)
        self.verifier = Verifier(page, self.control, self.targets)

    async def approve(self, request):
        self.control.check()
        if self.confirm is None:
            raise ConfirmationDeclined("Confirmation is required before execution.")
        try:
            answer = self.confirm(request)
            if inspect.isawaitable(answer):
                answer = await self.control.wait(answer, timeout_ms=0)
        except StopRequested:
            raise
        except Exception:
            raise ExecutionError("Confirmation could not be obtained.") from None
        self.control.check()
        if answer is not True:
            raise ConfirmationDeclined("Execution was declined.")
        self.audit.write("confirmed", step=request.step_id)

    def files(self, value):
        paths = value if isinstance(value, list) else [value]
        resolved = []
        for item in paths:
            if not isinstance(item, str) or not item:
                raise ExecutionError("Upload needs valid file paths.")
            path = Path(item)
            if not path.is_absolute():
                path = self.base_dir / path
            try:
                if not path.is_file():
                    raise ExecutionError("An upload file is missing. Review the supplied path.")
                resolved.append(str(path.resolve()))
            except (OSError, ValueError):
                raise ExecutionError("An upload file could not be accessed.") from None
        return resolved

    async def action(self, step, values, resolved):
        self.control.check()
        value = render(step.value, values)
        locator = resolved.locator if resolved is not None else None
        timeout = self.control.timeout_ms
        if step.action == "navigate":
            await self.control.wait(self.page.goto(browser_url(text(value)), wait_until="domcontentloaded", timeout=timeout))
        elif step.action == "click":
            await self.control.wait(locator.click(timeout=timeout))
        elif step.action == "type":
            await self.control.wait(locator.fill(text(value), timeout=timeout))
        elif step.action == "select":
            by = step.postcondition.by if isinstance(step.postcondition, Selection) else "label"
            await self.control.wait(locator.select_option(**{by: text(value)}, timeout=timeout))
        elif step.action == "upload":
            await self.control.wait(locator.set_input_files(self.files(value), timeout=timeout))
        elif step.action == "check":
            if not isinstance(value, bool):
                raise ExecutionError("Checked state must be boolean.")
            await self.control.wait(locator.set_checked(value, timeout=timeout))
        else:
            raise ExecutionError("Unsupported browser action.")

    async def download(self, step, values, resolved, attempt):
        async with self.page.expect_download(timeout=self.control.timeout_ms) as pending:
            await self.action(step, values, resolved)
        download = await pending.value
        if await self.control.wait(download.failure()) is not None:
            raise ExecutionError("The browser reported a failed download.")
        name = download_name(download.suggested_filename)
        if not fnmatch.fnmatch(name, text(render(step.postcondition.pattern, values))):
            raise VerificationFailed("The new download does not match the expected pattern.")
        folder = self.run_dir / "downloads" / f"step-{step.id}-attempt-{attempt}"
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except OSError:
            raise ExecutionError("Download destination exists or cannot be created.") from None
        path = folder / name
        await self.control.wait(download.save_as(path))
        return DownloadArtifact(name, path)

    async def step(self, step, values):
        if needs_confirmation(step) or step.id in self.workflow.confirm_before:
            await self.approve(Confirmation("step", self.workflow.name, step.id, step.purpose))
        for attempt in (1, 2):
            self.audit.write("step_started", step=step.id, action=step.action, attempt=attempt)
            dispatched = False
            try:
                self.control.check()
                resolved = await self.targets.resolve(step.target) if step.target is not None else None
                if resolved:
                    self.audit.write("target_resolved", step=step.id, strategy=resolved.strategy)
                dispatched = True
                artifact = None
                if isinstance(step.postcondition, FileDownloaded):
                    artifact = await self.control.wait(self.download(step, values, resolved, attempt))
                else:
                    await self.action(step, values, resolved)
                await self.verifier.verify(step.postcondition, values, artifact)
                if artifact is not None:
                    values[step.postcondition.save_as] = str(artifact.path.resolve())
                self.audit.write("step_verified", step=step.id, action=step.action, attempt=attempt)
                return
            except (ExecutionError, PlaywrightError, OSError) as error:
                if isinstance(error, (StopRequested, ConfirmationDeclined)):
                    raise
                can_repeat = (not needs_confirmation(step) and step.id not in self.workflow.confirm_before
                              and step.action in ("type", "select", "check", "navigate"))
                can_reground = not dispatched and isinstance(error, TargetMissing)
                if attempt == 1 and not isinstance(error, AmbiguousTarget) and (can_repeat or can_reground):
                    self.audit.write("step_retry", step=step.id, action=step.action)
                    continue
                self.audit.write("step_failed", step=step.id, action=step.action,
                                 status="verification_failed" if isinstance(error, VerificationFailed) else "action_failed")
                if dispatched and (needs_confirmation(step) or step.action == "click"):
                    raise ExecutionError("The action may have completed. Review the application before running it again.") from None
                raise ExecutionError("Step failed. Review its target and postcondition before another run.") from None

    @replay_guarded
    async def run(self, workflow: Workflow, supplied: dict, *, start_url=None) -> RunResult:
        # Revalidate mutable model instances and bind inputs before changing the page.
        self.workflow = Workflow.model_validate(workflow.model_dump())
        if self.workflow.status != "ready":
            raise ExecutionError("Review the workflow and mark it ready before replay.")
        values = bind_parameters(self.workflow, supplied)
        url = browser_url(text(render(start_url if start_url is not None else self.workflow.start_url, values)))
        completed, current = [], None
        self.audit.write("run_started")
        try:
            await self.approve(Confirmation("workflow", self.workflow.name))
            await self.control.wait(self.page.goto(url, wait_until="domcontentloaded", timeout=self.control.timeout_ms))
            for step in self.workflow.steps:
                current = step.id
                await self.step(step, values)
                completed.append(step.id)
            result = RunResult("completed", tuple(completed), message="Every step was verified.")
        except ConfirmationDeclined:
            result = RunResult("declined", tuple(completed), current, "Execution was declined.")
        except StopRequested:
            await self.close_stopped_context()
            result = RunResult("stopped", tuple(completed), current, "Stopped. Review the application state before another run.")
        except asyncio.CancelledError:
            await self.close_stopped_context()
            self.audit.write("run_finished", step=current, status="stopped")
            raise
        except (ExecutionError, PlaywrightError, OSError) as error:
            message = str(error) if isinstance(error, ExecutionError) else "Browser execution failed. Review the current step."
            result = RunResult("failed", tuple(completed), current, message)
        finally:
            values.clear()
        self.audit.write("run_finished", step=result.failed_step, status=result.status)
        return result

    async def close_stopped_context(self):
        try:
            await asyncio.wait_for(self.page.context.close(), timeout=2)
        except (PlaywrightError, TimeoutError):
            pass
