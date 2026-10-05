"""Application/project steps with owned launches and verified resulting state."""
from __future__ import annotations

import asyncio
from pathlib import Path

from apprentice.adapters.application import ApplicationAdapterError, ApplicationBackend, path_key
from apprentice.executor.filesystem import PathPolicy
from apprentice.executor.runtime import Control, ExecutionError, render, text
from apprentice.store.workflow import references


class ApplicationExecutor:
    def __init__(self, run_dir, *, backend=None, control=None):
        self.run_dir = Path(run_dir)
        self.backend = backend if backend is not None else ApplicationBackend()
        self.control = control if control is not None else Control()
        self._pending = []
        self._verified = []

    @staticmethod
    def directory(value):
        path = PathPolicy.absolute(value)
        PathPolicy.no_links(path)
        if not path.is_dir():
            raise ExecutionError("Application working/project directories must exist.")
        return str(path)

    def preflight(self, workflow, values):
        planned_directories = set()
        try:
            for step in workflow.steps:
                if step.surface == "filesystem" and step.action == "create_directory":
                    if all(name in values for name in references(step.path)):
                        planned_directories.add(path_key(text(render(step.path, values))))
                if step.surface != "application":
                    continue
                if step.action not in {"launch_application", "open_project"}:
                    raise ExecutionError("The application action needs a supported reviewed adapter.")
                app = text(render(step.app, values))
                if any("\x00" in text(render(argument, values)) for argument in step.args
                       if all(name in values for name in references(argument))):
                    raise ExecutionError("Application arguments cannot contain null characters.")
                if step.action == "open_project" and not self.backend.supports_project(app):
                    raise ExecutionError("This application's project state has no verified adapter yet.")
                if step.action == "open_project" and step.args:
                    raise ExecutionError("Verified projects do not accept additional launch arguments.")
                executable = text(render(step.executable, values)) if step.executable else None
                self.backend.resolve_executable(app, executable)
                for field in ("path", "cwd"):
                    value = getattr(step, field, None)
                    if value is not None and all(name in values for name in references(value)):
                        rendered = text(render(value, values))
                        if path_key(rendered) in planned_directories and not Path(rendered).exists():
                            PathPolicy.no_links(PathPolicy.absolute(rendered))
                        else:
                            self.directory(rendered)
        except ApplicationAdapterError:
            raise ExecutionError("The reviewed application or project adapter is unavailable.") from None

    async def step(self, step, values):
        self.control.check()
        handle = None
        verified = False
        try:
            app = text(render(step.app, values))
            executable = self.backend.resolve_executable(
                app, text(render(step.executable, values)) if step.executable else None)
            args = [text(render(value, values)) for value in step.args]
            cwd = self.directory(text(render(step.cwd, values))) if step.cwd else None
            path = self.directory(text(render(step.path, values))) if step.path else None
            self.control.check()
            # Synchronous spawn establishes ownership before any cancellable await.
            handle = self.backend.launch(app, executable, args, cwd, path,
                                         self.run_dir / "applications" / f"step-{step.id}")
            self._pending.append(handle)
            async def verify():
                while True:
                    self.control.check()
                    if step.action == "open_project":
                        complete = self.backend.project_open(handle, path)
                    else:
                        complete = self.backend.running(handle)
                    if complete:
                        return
                    # Successful launcher exit is deliberately insufficient proof.
                    if hasattr(handle, "process") and handle.process.poll() is not None:
                        raise ExecutionError("The application launcher exited without verified application state.")
                    await asyncio.sleep(0.05)
            await self.control.wait(verify(), timeout_ms=getattr(step, "timeout_ms", None) or self.control.timeout_ms)
            self.control.check()
            self._pending.remove(handle)
            self._verified.append(handle)
            verified = True
        except (ApplicationAdapterError, OSError, ValueError):
            raise ExecutionError("Application state could not be verified. Review the current step.") from None
        finally:
            if handle is not None and not verified:
                self.backend.terminate(handle)
                if handle in self._pending:
                    self._pending.remove(handle)

    async def close(self, *, stopped=False):
        failed, remaining = False, []
        for handle in [*self._pending, *self._verified]:
            if stopped or handle in self._pending or not hasattr(self.backend, "release"):
                try:
                    await asyncio.to_thread(self.backend.terminate, handle)
                except Exception:
                    failed = True
                    remaining.append(handle)
            else:
                try:
                    await asyncio.to_thread(self.backend.release, handle)
                except Exception:
                    failed = True
                    # A failed handover still belongs to this run. Close it and
                    # continue cleanup of every other application handle.
                    try:
                        await asyncio.to_thread(self.backend.terminate, handle)
                    except Exception:
                        remaining.append(handle)
        self._pending = remaining
        self._verified.clear()
        if failed:
            raise ExecutionError("Application cleanup or handover did not verify. Review the system state.")
