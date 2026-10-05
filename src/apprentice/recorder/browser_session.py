"""Enroll the proven semantic browser recorder in a general system session."""
from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import time

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from apprentice.recorder.record import INJECT, SENSITIVE_SELECTOR
from apprentice.recorder.session import RecordingSession


class BrowserCapture:
    def __init__(self, session: RecordingSession, output_dir: str | Path, url: str, *,
                 headless: bool = False, settle_ms: int = 400):
        self.session = session
        self.output_dir = Path(output_dir)
        self.url = url
        self.headless = headless
        self.settle_ms = settle_ms
        self.playwright = self.browser = self.context = self.page = None
        self._pages: dict[object, int] = {}
        self._before: dict[tuple, tuple] = {}
        self._pending: list[tuple] = []
        self._shot_number = 0
        self._last_received = 0.0
        self._process_ids: set[int] = set()
        self._last_process_refresh = 0.0
        self._capture_epoch = session.capture_epoch

    def managed_process_ids(self) -> set[int]:
        # This provider also runs on Windows hook threads; return cached metadata only.
        return set(self._process_ids)

    def _refresh_process_ids(self) -> None:
        # Limit native gesture suppression to this recorder's Chromium descendants.
        if time.monotonic() - self._last_process_refresh < 0.5:
            return
        self._last_process_refresh = time.monotonic()
        try:
            import psutil
        except ImportError:
            return
        try:
            observed = set()
            for child in psutil.Process(os.getpid()).children(recursive=True):
                if child.name().casefold() in {"chrome.exe", "chromium.exe", "chrome", "chromium",
                                              "headless_shell.exe", "headless_shell"}:
                    observed.add(child.pid)
            self._process_ids = observed
        except (psutil.Error, OSError):
            pass

    def _state(self, frame):
        if not self.session.capturing:
            return None
        try:
            return frame.evaluate("() => window.__apprenticeState ? window.__apprenticeState() : null")
        except PlaywrightError:
            return None

    def _shot(self, page) -> str | None:
        if not self.session.capturing:
            return None
        filename = f"browser_{self._shot_number:05d}.png"
        self._shot_number += 1
        try:
            # Mask every enrolled frame, including embedded password fields.
            page.screenshot(path=str(self.output_dir / filename),
                            mask=[frame.locator(SENSITIVE_SELECTOR) for frame in page.frames],
                            mask_color="#000000", timeout=2000)
            return filename
        except PlaywrightError:
            return None

    def _receive(self, page, frame, payload: dict) -> None:
        if not self.session.capturing:
            return
        if self._capture_epoch != self.session.capture_epoch:
            self._before.clear()
            self._capture_epoch = self.session.capture_epoch
        if page not in self._pages:
            self._attach(page)
        page_id = self._pages[page]
        frame_key = (page_id, frame)
        before_shot, before_state = self._before.get(frame_key, (None, None))
        payload = dict(payload)
        payload.update(source_page=page_id,
                       source_frame={"url": frame.url, "is_main_frame": frame == page.main_frame},
                       before_shot=before_shot, before_state=before_state,
                       after_shot=None, after_state=None)
        event = self.session.emit("browser", payload["kind"], payload,
                                  context={"app": "chromium", "source_page": page_id},
                                  evidence=[before_shot] if before_shot else [])
        if event is not None:
            self._pending.append((event["id"], page, frame, payload["kind"]))
            self._last_received = time.monotonic()

    def _binding(self, source: dict, payload: str) -> None:
        try:
            self._receive(source["page"], source["frame"], json.loads(payload))
        except (ValueError, KeyError, TypeError):
            self.session.failure("browser", "A browser event could not be decoded.")

    def _attach(self, page) -> None:
        if page in self._pages:
            return
        self._pages[page] = len(self._pages) + 1
        page.on("download", lambda download: self._receive(page, page.main_frame,
                {"kind": "download", "filename": download.suggested_filename, "url": page.url}))
        page.on("framenavigated", lambda frame: self._receive(page, frame,
                {"kind": "navigate", "url": frame.url}))

    def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=self.headless)
            self.context = self.browser.new_context(accept_downloads=True,
                    viewport={"width": 1280, "height": 800})
            self.context.expose_binding("__apprenticeEmit", self._binding)
            self.context.add_init_script(INJECT)
            self.context.on("page", self._attach)
            self.page = self.context.new_page()
            self.page.goto(self.url, wait_until="load", timeout=15000)
            self.flush(force=True)
            self._refresh_process_ids()
        except Exception:
            self.session.failure("browser", "The semantic browser recorder could not start.")
            self.stop()
            raise

    @property
    def active(self) -> bool:
        return self.browser is not None and self.browser.is_connected() and any(
            not page.is_closed() for page in self._pages)

    def pump(self, milliseconds: int = 50) -> None:
        """Dispatch Playwright callbacks on its owner thread while other adapters run."""
        self._refresh_process_ids()
        page = next((page for page in self._pages if not page.is_closed()), None)
        if page is not None:
            try:
                page.wait_for_timeout(milliseconds)
            except PlaywrightError:
                if self.active:
                    raise
        self.flush()

    def flush(self, *, force: bool = False) -> None:
        if not self.session.capturing:
            # Do not take after screenshots of work performed while paused.
            self._pending.clear()
            self._before.clear()
            return
        if not self._pending or (not force and (time.monotonic() - self._last_received) * 1000 < self.settle_ms):
            return
        batch, self._pending = self._pending, []
        grouped = defaultdict(list)
        for event_id, page, frame, kind in batch:
            grouped[(page, frame)].append((event_id, kind))
        screenshots = {}
        for (page, frame), events in grouped.items():
            if page not in screenshots:
                screenshots[page] = self._shot(page)
            after_shot, after_state = screenshots[page], self._state(frame)
            shared = sum(kind == "click" for _, kind in events) > 1
            for event_id, _ in events:
                self.session.annotate(event_id,
                        payload={"after_shot": after_shot, "after_state": after_state,
                                 "observations_shared": shared},
                        evidence=[after_shot] if after_shot else [])
            self._before[(self._pages[page], frame)] = (after_shot, after_state)

    def stop(self) -> None:
        try:
            self.flush(force=True)
        finally:
            if self.context is not None:
                try:
                    self.context.close()
                except PlaywrightError:
                    pass
                self.context = None
            if self.browser is not None:
                try:
                    self.browser.close()
                except PlaywrightError:
                    pass
                self.browser = None
            if self.playwright is not None:
                self.playwright.stop()
                self.playwright = None
