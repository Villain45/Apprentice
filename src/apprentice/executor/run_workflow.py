"""Preview or execute a reviewed browser workflow. Preview is the default."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import threading
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apprentice.executor.browser import BrowserExecutor
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.executor.runtime import Audit, Control, ExecutionError, ParameterError, StopRequested, dry_run, parameter_value
from apprentice.store.workflow import load_workflow, validation_message


async def prompt(message, *, secret=False):
    """A cancellable console prompt; its daemon reader cannot hold up shutdown."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(value, error):
        if not future.done():
            if error:
                future.set_exception(ExecutionError("Console input was closed."))
            else:
                future.set_result(value)

    def read():
        try:
            value = getpass.getpass(message) if secret else input(message)
            error = False
        except (EOFError, KeyboardInterrupt):
            value, error = None, True
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            pass

    threading.Thread(target=read, daemon=True).start()
    return await future


async def listener_ready(listener):
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def deliver():
        if not ready.done():
            ready.set_result(None)

    def wait():
        listener.wait()
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass

    threading.Thread(target=wait, daemon=True).start()
    await ready


def command_parameters(workflow, entries):
    values = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if not separator or name not in workflow.parameters or name in values:
            raise ParameterError("Use unique --param name=value entries declared in the workflow.")
        parameter = workflow.parameters[name]
        if parameter.type == "secret" or parameter.ask_each_time:
            raise ParameterError("Sensitive inputs must be requested interactively, outside command-line history.")
        values[name] = parameter_value(name, parameter, value)
    return values


async def execute(workflow, args, supplied):
    from playwright.async_api import Error as PlaywrightError, async_playwright

    control = Control(timeout_ms=args.timeout)
    hotkeys = None
    values = dict(supplied)
    try:
        if not args.no_hotkey:
            from pynput.keyboard import GlobalHotKeys
            loop = asyncio.get_running_loop()
            try:
                hotkeys = GlobalHotKeys({"<ctrl>+<alt>+<esc>": lambda: loop.call_soon_threadsafe(control.stop.set)})
                hotkeys.start()
                await control.wait(listener_ready(hotkeys))
            except StopRequested:
                raise
            except Exception:
                raise ExecutionError("Could not enable the stop hotkey. Use --no-hotkey with Ctrl+C if needed.") from None
        for name, parameter in workflow.parameters.items():
            if name in values:
                continue
            if parameter.default is not None and not parameter.ask_each_time:
                values[name] = parameter.default
                continue
            answer = await control.wait(prompt(f"{name} ({parameter.type}): ",
                                              secret=parameter.type == "secret" or parameter.ask_each_time), timeout_ms=0)
            values[name] = parameter_value(name, parameter, answer)

        async def confirm(request):
            message = (f"Run {request.name}?" if request.kind == "workflow" else
                       f"Step {request.step_id}: {request.purpose} Proceed?")
            answer = await prompt(message + " [y/N] ")
            return answer.strip().lower() in ("y", "yes")

        folder = args.run_root / uuid.uuid4().hex
        audit = Audit(folder / "audit.jsonl")
        if workflow.version == 1:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=args.headless)
                try:
                    context = await browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 800})
                    page = await context.new_page()
                    executor = BrowserExecutor(page, folder, confirm=confirm, control=control, audit=audit)
                    result = await executor.run(workflow, values, start_url=args.url)
                    await context.close()
                finally:
                    await browser.close()
        else:
            # Filesystem-only replay starts no browser/Playwright process. Mixed replay opens it lazily.
            async with AsyncExitStack() as stack:
                async def browser_factory():
                    playwright = await stack.enter_async_context(async_playwright())
                    browser = await playwright.chromium.launch(headless=args.headless)
                    stack.push_async_callback(browser.close)
                    context = await browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 800})
                    stack.push_async_callback(context.close)
                    return await context.new_page()

                result = await SurfaceExecutor(folder, browser_factory=browser_factory, confirm=confirm,
                                               control=control, audit=audit).run(workflow, values, start_url=args.url)
        print(f"{result.status.capitalize()}: {len(result.completed_steps)}/{len(workflow.steps)} steps verified.")
        if result.message:
            print(result.message)
        print(f"Audit: {audit.path}")
        return {"completed": 0, "failed": 1, "declined": 2, "stopped": 130}[result.status]
    except StopRequested:
        print("Stopped before execution.")
        return 130
    except PlaywrightError:
        raise ExecutionError("Browser startup failed. Check the Playwright Chromium installation.") from None
    finally:
        values.clear()
        if hotkeys is not None:
            hotkeys.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="perform actions after confirmations")
    mode.add_argument("--dry-run", action="store_true", help="show steps without opening a browser (default)")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="ordinary inputs; secrets are prompted")
    parser.add_argument("--url", help="override the start URL, e.g. a local portal variant")
    parser.add_argument("--headless", action="store_true", help="run Chromium without a visible window")
    parser.add_argument("--timeout", type=int, default=10000, metavar="MS", help="action/check timeout (default: 10000 ms)")
    parser.add_argument("--run-root", type=Path, default=Path("runs"), help="private audit/download folder")
    parser.add_argument("--no-hotkey", action="store_true", help="disable Ctrl+Alt+Esc; Ctrl+C still stops the CLI")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        workflow = load_workflow(args.workflow, require_ready=True)
        values = command_parameters(workflow, args.param)
        for line in dry_run(workflow):
            print(line)
        if not args.execute:
            return 0
        print("Stop: Ctrl+C" + (" or Ctrl+Alt+Esc" if not args.no_hotkey else ""))
        return asyncio.run(execute(workflow, args, values))
    except KeyboardInterrupt:
        print("Stopped. Review the application state before another run.")
        return 130
    except (OSError, ValueError, yaml.YAMLError, ExecutionError) as error:
        parser.exit(2, f"Cannot replay workflow: {validation_message(error)}\n")


if __name__ == "__main__":
    raise SystemExit(main())
