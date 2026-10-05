"""Explicitly start and stop a system-wide teaching session.

Run with ``python -m apprentice.recorder.record_system --name task --root PATH``.
Browser recording can join the same timeline through ``--url``. Native captures
contain metadata and activity only; unsupported actions become review items.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
import threading
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import Error as PlaywrightError

from apprentice.recorder.browser_session import BrowserCapture
from apprentice.recorder.filesystem import FilesystemCapture
from apprentice.recorder.session import RecordingSession
from apprentice.recorder.terminal import TerminalCapture


def _console_controls(session: RecordingSession, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            command = input().strip().casefold()
        except (EOFError, OSError):
            return
        if command in {"", "s", "stop"}:
            stop.set()
        elif command in {"p", "pause"}:
            session.pause()
            print("Recording paused.")
        elif command in {"r", "resume"}:
            session.resume()
            print("Recording resumed.")


def record(args) -> Path:
    from apprentice.adapters.windows import WindowsCapture

    output = Path(args.output) / args.name
    if output.exists():
        raise FileExistsError("That recording folder already exists; choose another name.")
    roots = [Path(root).resolve() for root in args.root] if args.root else [Path.cwd().resolve()]
    if any(not root.is_dir() for root in roots):
        raise ValueError("Every --root must be an existing directory.")
    output.mkdir(parents=True)
    session = RecordingSession(args.name, roots=roots, excluded_apps=args.exclude_app)
    browser = BrowserCapture(session, output, args.url, headless=args.headless) if args.url else None
    windows = WindowsCapture(session.emit, capture_input=not args.no_input,
                             managed_browser_pids=browser.managed_process_ids if browser else ())
    shell_events = getattr(args, "shell_events", None)
    excluded_paths = [output]
    if shell_events:
        excluded_paths.append(Path(shell_events).absolute())
    filesystem = FilesystemCapture(session, roots, context_provider=windows.current_context,
                                    excluded_paths=excluded_paths)
    terminal = TerminalCapture(session, shell_events, context_provider=windows.current_context) if shell_events else None
    adapters = []
    stop = threading.Event()
    try:
        if browser:
            adapters.append(browser)
            browser.start()
        captures = [(windows, "windows"), (filesystem, "filesystem")]
        if terminal is not None:
            captures.append((terminal, "terminal"))
        for adapter, label in captures:
            previous_count = len(session.snapshot()["events"])
            try:
                adapter.start()
                adapters.append(adapter)
            except Exception:
                try:
                    adapter.stop()
                except Exception:
                    pass
                recent = session.snapshot()["events"][previous_count:]
                if not any(event["kind"] == "review_required" and event["payload"].get("adapter") == label
                           for event in recent):
                    session.failure(label, "A system capture adapter could not start; review is required.")
        print("Recording. Enter stops; p pauses, r resumes; Ctrl+C also stops.")
        print("Native typing records activity only. Browser secrets are redacted; uncertain actions require review.")
        if terminal is not None:
            print("Opt-in PowerShell/application events are enrolled. Raw shell history and output are omitted.")
        threading.Thread(target=_console_controls, args=(session, stop), daemon=True).start()
        deadline = time.monotonic() + args.duration if args.duration else None
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            if browser and browser.active:
                browser.pump()
            else:
                stop.wait(0.05)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        for adapter in reversed(adapters):
            try:
                adapter.stop()
            except Exception:
                session.failure(type(adapter).__name__, "A capture adapter could not stop cleanly.")
        session.close()
        destination = session.save(output / "session.json")
    print(f"Saved {len(session.snapshot()['events'])} events to {destination}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--root", action="append", default=[], help="Directory to monitor; repeat for multiple roots.")
    parser.add_argument("--url", help="Enroll a semantic Chromium recorder in the session.")
    parser.add_argument("--headless", action="store_true", help="Use a headless enrolled browser.")
    parser.add_argument("--exclude-app", action="append", default=[])
    parser.add_argument("--no-input", action="store_true", help="Capture app and folder context without native gestures.")
    parser.add_argument("--shell-events", type=Path, help="Opt-in PowerShell/application JSONL event file.")
    parser.add_argument("--output", default="recordings")
    parser.add_argument("--duration", type=float, help="Optional recording duration in seconds.")
    args = parser.parse_args()
    if not args.name.strip() or args.name in {".", ".."} or any(char in args.name for char in '<>:"/\\|?*'):
        parser.error("--name must be a single safe folder name.")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be a positive finite number.")
    try:
        record(args)
    except (OSError, ValueError, PlaywrightError):
        parser.exit(2, "Recording could not start; check the name, roots, and installed adapters.\n")


if __name__ == "__main__":
    main()
