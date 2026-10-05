"""Resolve a unique semantic target; use recorded CSS only as a fallback."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError, Locator, Page

from apprentice.executor.runtime import Control, ExecutionError
from apprentice.store.workflow import Target

FORM_ROLES = {"textbox", "searchbox", "spinbutton", "combobox", "checkbox", "radio", "slider", "switch", "file_input"}
ARIA_ROLES = FORM_ROLES - {"file_input"} | {
    "button", "link", "heading", "option", "menuitem", "tab", "treeitem", "listbox",
    "dialog", "alert", "status", "row", "cell", "listitem", "navigation", "img",
}


class TargetMissing(ExecutionError):
    pass


class AmbiguousTarget(ExecutionError):
    pass


@dataclass(frozen=True)
class ResolvedTarget:
    locator: Locator
    strategy: str


class Targets:
    def __init__(self, page: Page, control: Control):
        self.page, self.control = page, control

    def candidates(self, target: Target):
        page, name, role = self.page, target.name, target.role
        if name:
            if role in FORM_ROLES or not role:
                yield "label", page.get_by_label(name, exact=True)
            if role in ARIA_ROLES:
                yield "role", page.get_by_role(role, name=name, exact=True)
            if role in ("textbox", "searchbox", "spinbutton") or not role:
                yield "placeholder", page.get_by_placeholder(name, exact=True)
            if role not in ARIA_ROLES and role != "file_input":
                yield "text", page.get_by_text(name, exact=True)
        if target.css:
            locator = page.locator(target.css)
            if role == "file_input":
                locator = locator.and_(page.locator('input[type="file"]'))
            elif role in ("textbox", "searchbox"):
                locator = locator.and_(page.locator('input, textarea, [contenteditable="true"]'))
            elif role in ARIA_ROLES:
                locator = locator.and_(page.get_by_role(role))
            yield "css", locator

    async def probe(self, target: Target):
        self.control.check()
        try:
            for strategy, locator in self.candidates(target):
                if target.role != "file_input":
                    locator = locator.filter(visible=True)
                count = await self.control.wait(locator.count())
                if count > 1:
                    raise AmbiguousTarget("Several elements match the target; review its descriptor.")
                if count == 1:
                    return ResolvedTarget(locator, strategy)
        except PlaywrightError:
            raise ExecutionError("The target descriptor could not be resolved.") from None
        return None

    async def resolve(self, target: Target, *, timeout_ms=None):
        duration = timeout_ms if timeout_ms is not None else self.control.timeout_ms
        deadline = time.monotonic() + duration / 1000
        while True:
            found = await self.probe(target)
            if found:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TargetMissing("Target not found; review its descriptor or the current page.")
            await self.control.wait(asyncio.sleep(min(0.05, remaining)))
