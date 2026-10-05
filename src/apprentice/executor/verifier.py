"""Wait for the actual postcondition before permitting the next step."""

from __future__ import annotations

import asyncio
import fnmatch
import time
from pathlib import PureWindowsPath

from playwright.async_api import Error as PlaywrightError

from apprentice.executor.runtime import Control, DownloadArtifact, ExecutionError, render, text
from apprentice.executor.targets import Targets
from apprentice.store.workflow import (
    Checked, ElementVisible, FileDownloaded, FilesSelected, InputValue, ReviewRequired,
    Selection, TextVisible, UrlMatches,
)


class VerificationFailed(ExecutionError):
    pass


class Verifier:
    def __init__(self, page, control: Control, targets: Targets):
        self.page, self.control, self.targets = page, control, targets

    async def matches(self, check, values, artifact: DownloadArtifact | None = None):
        self.control.check()
        if isinstance(check, ReviewRequired):
            raise ExecutionError("A postcondition still needs review.")
        if isinstance(check, FileDownloaded):
            return artifact is not None and artifact.path.is_file() and fnmatch.fnmatch(
                artifact.name, text(render(check.pattern, values))
            )
        if isinstance(check, UrlMatches):
            return self.page.url == text(render(check.url, values))
        if isinstance(check, TextVisible) and check.target is None:
            locator = self.page.get_by_text(text(render(check.text, values)), exact=check.exact).filter(visible=True)
            return await self.control.wait(locator.count()) > 0
        resolved = await self.targets.probe(check.target)
        if resolved is None:
            return False
        locator = resolved.locator
        if isinstance(check, ElementVisible):
            return await self.control.wait(locator.is_visible())
        if isinstance(check, InputValue):
            return await self.control.wait(locator.input_value(timeout=self.control.timeout_ms)) == text(render(check.value, values))
        if isinstance(check, Selection):
            selected = await self.control.wait(locator.evaluate(
                "(el, by) => el.selectedOptions ? [...el.selectedOptions].map(o => by === 'label' ? o.label : o.value) : null",
                check.by,
            ))
            return selected == [text(render(check.value, values))]
        if isinstance(check, FilesSelected):
            selected = await self.control.wait(locator.evaluate("el => el.files ? [...el.files].map(f => f.name) : null"))
            expected = [PureWindowsPath(text(render(value, values))).name for value in check.files]
            return selected == expected
        if isinstance(check, Checked):
            expected = render(check.checked, values)
            if not isinstance(expected, bool):
                raise ExecutionError("A checked-state postcondition needs a boolean value.")
            return await self.control.wait(locator.is_checked(timeout=self.control.timeout_ms)) == expected
        if isinstance(check, TextVisible):
            actual = await self.control.wait(locator.inner_text(timeout=self.control.timeout_ms))
            expected = " ".join(text(render(check.text, values)).split())
            actual = " ".join(actual.split())
            return actual == expected if check.exact else expected in actual
        raise ExecutionError("Unsupported postcondition.")

    async def verify(self, check, values, artifact=None):
        deadline = time.monotonic() + self.control.timeout_ms / 1000
        while True:
            try:
                if await self.matches(check, values, artifact):
                    return
            except PlaywrightError:
                # A render may replace the control between lookup and reading it.
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VerificationFailed("Postcondition was not met; review this step before continuing.")
            await self.control.wait(asyncio.sleep(min(0.05, remaining)))
