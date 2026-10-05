"""Coverage catalog: all operation families have a capture/review route, not a false replay claim."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    surface: str
    operations: tuple[str, ...]
    capture: str
    replay: str
    phase: int


CAPABILITIES = (
    Capability("browser", ("navigation", "controls", "forms", "selection", "upload", "download"),
               "semantic DOM events and masked evidence", "implemented", 3),
    Capability("filesystem", ("folder navigation", "create", "copy", "move", "rename", "delete"),
               "Explorer paths and watched effects; intent reviewed", "implemented", 4),
    Capability("application", ("launch", "close", "focus", "projects", "documents", "workspaces"),
               "foreground context plus explicit verified launch/project events",
               "launch implemented; VS Code project API implemented; close/focus/other project adapters pending", 5),
    Capability("terminal", ("commands", "pipelines", "scripts", "interactive input", "services"),
               "opt-in PowerShell submissions with redacted argv/script, cwd and completion; no history/output scraping",
               "commands, reviewed PowerShell scripts and loopback services implemented; interactive input requires review", 5),
    Capability("desktop", ("menus", "dialogs", "controls", "typing", "shortcuts", "drag", "scroll", "window management"),
               "mouse/shortcut/activity metadata; typed characters omitted", "pending", 6),
    Capability("clipboard", ("copy", "cut", "paste", "formats"),
               "shortcut metadata only; content not captured", "pending", 6),
    Capability("system", ("file editing", "archives", "search", "permissions", "settings", "devices",
                          "network resources", "custom apps", "unrecognized operations"),
               "native context/gestures retained for review; structured adapters can extend the envelope",
               "accessibility/vision/app adapters pending", 6),
)


def capture_route(surface):
    return next((item for item in CAPABILITIES if item.surface == surface), CAPABILITIES[-1])
