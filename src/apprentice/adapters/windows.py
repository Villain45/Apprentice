"""Windows activity metadata and verified Explorer navigation.

Shell folders are read through the documented Shell.Application Windows collection
and Folder.Self.Path. A single hidden PowerShell COM host owns the STA objects; no
new process is launched per poll. Native gestures are evidence for human review,
never coordinate-based replay instructions. Text and clipboard contents are not
recorded, including text entered outside a browser.

API references:
https://learn.microsoft.com/en-us/windows/win32/shell/ishelldispatch-windows
https://learn.microsoft.com/en-us/windows/win32/shell/folderitem-path
https://learn.microsoft.com/en-us/windows/win32/shell/ishelldispatch2-shellexecute
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
from typing import Callable, Iterable


class WindowsAdapterError(RuntimeError):
    """A static message; never relay COM errors containing user data."""


_SHELL_HOST = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try { $shell = New-Object -ComObject Shell.Application }
catch { [Console]::WriteLine('{"ok":false}'); exit 1 }
while ($null -ne ($line = [Console]::ReadLine())) {
    try {
        $request = $line | ConvertFrom-Json
        if ($request.op -eq 'folders') {
            $folders = @()
            foreach ($window in $shell.Windows()) {
                try {
                    if ([IO.Path]::GetFileName([string]$window.FullName) -ine 'explorer.exe') { continue }
                    $item = $window.Document.Folder.Self
                    if (-not $item.IsFileSystem) { continue }
                    $folder = [string]$item.Path
                    if (-not [IO.Path]::IsPathRooted($folder)) { continue }
                    if ($folder -match '^[a-z]+://') { continue }
                    $folders += @{ window_id = [long]$window.HWND; path = $folder }
                } catch { }
            }
            $answer = @{ ok = $true; folders = @($folders) }
        } elseif ($request.op -eq 'open') {
            $folder = [string]$request.path
            if (-not [IO.Directory]::Exists($folder)) { throw 'invalid directory' }
            $shell.ShellExecute($folder, '', '', 'open', 1)
            $answer = @{ ok = $true }
        } elseif ($request.op -eq 'ping') {
            $answer = @{ ok = $true }
        } else { throw 'unsupported operation' }
        [Console]::WriteLine(($answer | ConvertTo-Json -Compress -Depth 4))
    } catch { [Console]::WriteLine('{"ok":false}') }
}
"""


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


class _ShellHost:
    """Bounded, serialized IPC to a hidden Windows PowerShell COM apartment."""

    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._answers: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = threading.Event()

    @staticmethod
    def executable() -> str | None:
        if sys.platform != "win32":
            return None
        candidate = shutil.which("powershell.exe")
        if candidate:
            return candidate
        fallback = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return str(fallback) if fallback.is_file() else None

    def _start(self):
        if self._closed.is_set():
            raise WindowsAdapterError("The Windows adapter has stopped.")
        executable = self.executable()
        if not executable:
            raise WindowsAdapterError("Windows Explorer integration is unavailable on this system.")
        encoded = base64.b64encode(_SHELL_HOST.encode("utf-16-le")).decode("ascii")
        self._process = subprocess.Popen(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        process = self._process
        if self._closed.is_set():
            self.close()
            raise WindowsAdapterError("The Windows adapter has stopped.")

        def read():
            try:
                for line in process.stdout:
                    try:
                        self._answers.put(json.loads(line))
                    except (ValueError, TypeError):
                        self._answers.put(None)
            finally:
                self._answers.put(None)

        threading.Thread(target=read, name="apprentice-shell-ipc", daemon=True).start()

    def request(self, op: str, **payload) -> dict:
        if not self._lock.acquire(timeout=self.timeout):
            raise WindowsAdapterError("Windows Explorer did not respond in time.")
        try:
            if self._closed.is_set():
                raise WindowsAdapterError("The Windows adapter has stopped.")
            if self._process is None:
                self._start()
            if self._closed.is_set():
                raise WindowsAdapterError("The Windows adapter has stopped.")
            self._process.stdin.write(json.dumps({"op": op, **payload}, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            answer = self._answers.get(timeout=self.timeout)
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                raise WindowsAdapterError("Windows Explorer integration did not complete the request.")
            return answer
        except (OSError, ValueError, queue.Empty) as exc:
            self.close()
            raise WindowsAdapterError("Windows Explorer did not respond in time.") from None
        finally:
            self._lock.release()

    def close(self):
        # No acquisition of the exchange lock: stopping must interrupt a blocked read.
        self._closed.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
            except OSError:
                pass
        self._answers.put(None)


class WindowsBackend:
    """Read foreground identity and Explorer folders; injected alternatives need no GUI."""

    def __init__(self):
        self._shell = _ShellHost()
        self._user32 = None

    @property
    def available(self) -> bool:
        return _ShellHost.executable() is not None

    def foreground(self) -> dict:
        if sys.platform != "win32":
            return {}
        if self._user32 is None:
            from ctypes import wintypes
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.GetForegroundWindow.argtypes = []
            self._user32.GetForegroundWindow.restype = wintypes.HWND
            self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        from ctypes import wintypes
        import psutil
        handle = self._user32.GetForegroundWindow()
        if not handle:
            return {}
        process_id = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        try:
            app = psutil.Process(process_id.value).name()
        except (psutil.Error, OSError):
            app = "unknown"
        # Window titles can contain entered secrets or document contents; omit them.
        return {"app": app, "process_id": process_id.value, "window_id": int(handle)}

    def folders(self) -> list[dict]:
        folders = self._shell.request("folders").get("folders", [])
        return [folder for folder in folders if isinstance(folder, dict)
                and isinstance(folder.get("window_id"), int) and isinstance(folder.get("path"), str)]

    def open_directory(self, path: Path):
        self._shell.request("open", path=str(path))

    def close(self):
        self._shell.close()


class WindowsExplorer:
    """Launching and verifying a folder are deliberately separate operations."""

    def __init__(self, backend=None):
        self.backend = backend if backend is not None else WindowsBackend()
        self._closed = False

    @property
    def available(self) -> bool:
        return not self._closed and bool(self.backend.available)

    def check_available(self) -> bool:
        """Read-only preflight checks COM access to the interactive Shell desktop."""
        if not self.available:
            return False
        try:
            self.backend.folders()
        except (WindowsAdapterError, OSError):
            self.close()
            return False
        return True

    def open_directory(self, path: str | Path):
        if not self.available:
            raise WindowsAdapterError("Windows Explorer integration is unavailable on this system.")
        directory = Path(path).resolve()
        if not directory.is_dir():
            raise WindowsAdapterError("The requested directory does not exist.")
        self.backend.open_directory(directory)

    def directory_is_open(self, path: str | Path) -> bool:
        if not self.available:
            return False
        expected = _path_key(path)
        return any(_path_key(folder["path"]) == expected for folder in self.backend.folders())

    def close(self):
        self._closed = True
        self.backend.close()


_MODIFIERS = {"ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", "alt_gr",
              "shift", "shift_l", "shift_r", "cmd", "cmd_l", "cmd_r"}


def _modifier(name: str) -> str:
    return name.split("_")[0] if name != "alt_gr" else "alt_gr"


class WindowsCapture:
    """Capture all foreground apps and non-text gestures for explicit review.

    Callbacks emit through the coordinator, which owns pause, exclusion, replay
    suppression, and persistence. Browser PIDs can be a live provider so only a
    browser enrolled in semantic capture suppresses duplicate native gestures.
    """

    def __init__(self, emit: Callable, *, backend=None, interval: float = 0.25,
                 managed_browser_pids: Iterable[int] | Callable = (),
                 managed_browser_apps: Iterable[str] = (), capture_input: bool = True):
        if interval <= 0:
            raise ValueError("Capture interval must be positive.")
        self.emit = emit
        self.backend = backend if backend is not None else WindowsBackend()
        self.interval = interval
        self.managed_browser_pids = managed_browser_pids
        self.managed_browser_apps = {app.casefold() for app in managed_browser_apps}
        self.capture_input = capture_input
        self._stop = threading.Event()
        self._thread = None
        self._listeners = []
        self._context = {}
        self._last_focus = None
        self._last_directory = None
        self._modifiers = set()
        self._typed = 0
        self._typing_context = {}
        self._pressed_mouse = {}
        self._state_lock = threading.Lock()
        self._reported_failures = set()

    def current_context(self) -> dict:
        # Query at gesture time rather than reusing a potentially stale foreground.
        return dict(self.backend.foreground())

    def _managed_browser(self, context: dict) -> bool:
        pids = self.managed_browser_pids() if callable(self.managed_browser_pids) else self.managed_browser_pids
        return (context.get("process_id") in pids
                or context.get("app", "").casefold() in self.managed_browser_apps)

    def _gesture(self, kind: str, payload: dict, context: dict, evidence: dict | None = None):
        if self._stop.is_set() or self._managed_browser(context):
            return
        if not context:
            self._unavailable("The interactive foreground window is unavailable; activity needs review.")
            return
        self.emit("desktop", kind, payload, origin="user", context=context, evidence=evidence)

    def _unavailable(self, reason: str):
        if reason not in self._reported_failures and not self._stop.is_set():
            self._reported_failures.add(reason)
            self.emit("system", "review_required", {"adapter": "windows", "reason": reason}, origin="user")

    def _flush_typing(self):
        with self._state_lock:
            count, context = self._typed, self._typing_context
            self._typed = 0
            self._typing_context = {}
        if count:
            self._gesture("input_activity", {"count": count, "value": "<redacted>"}, context)

    def poll_once(self):
        if self._stop.is_set():
            return
        context = self.current_context()
        if not context:
            self._unavailable("The interactive foreground window is unavailable; activity needs review.")
            return
        identity = (context.get("window_id"), context.get("process_id"))
        if identity != self._last_focus:
            self._flush_typing()
            self._last_focus = identity
            self._context = context
            self.emit("application", "focus", {}, origin="observation", context=context)
        if context.get("app", "").casefold() == "explorer.exe":
            try:
                folders = [folder for folder in self.backend.folders()
                           if folder.get("window_id") == context.get("window_id")]
            except (WindowsAdapterError, OSError):
                self._unavailable("Explorer folder navigation is unavailable; navigation needs review.")
                # One unavailable semantic source must not stop other app capture.
                self._flush_typing()
                return
            unique = {_path_key(folder["path"]): folder["path"] for folder in folders}
            if len(unique) == 1:
                path = next(iter(unique.values()))
                directory_key = (context.get("window_id"), _path_key(path))
                if directory_key != self._last_directory:
                    self._last_directory = directory_key
                    self.emit("filesystem", "open_directory", {"path": path}, origin="user", context=context,
                              evidence={"verification": "shell_folder", "window_id": context.get("window_id")})
            elif len(unique) > 1:
                directory_key = (context.get("window_id"), tuple(sorted(unique)))
                if directory_key != self._last_directory:
                    self._last_directory = directory_key
                    self._gesture("explorer_navigation_uncertain", {"paths": list(unique.values())}, context)
        self._flush_typing()

    def on_click(self, x, y, button, pressed):
        name = getattr(button, "name", str(button).replace("Button.", ""))
        context = self.current_context()
        if pressed:
            self._pressed_mouse[name] = (int(x), int(y), context)
            return
        start = self._pressed_mouse.pop(name, (int(x), int(y), context))
        delta = abs(int(x) - start[0]) + abs(int(y) - start[1])
        self._gesture("drag" if delta > 5 else "click", {"button": name}, start[2],
                      {"cursor_start": [start[0], start[1]], "cursor_end": [int(x), int(y)],
                       "target_resolution": "review_required"})

    def on_scroll(self, x, y, dx, dy):
        self._gesture("scroll", {"horizontal": int(dx), "vertical": int(dy)}, self.current_context(),
                      {"cursor": [int(x), int(y)], "target_resolution": "review_required"})

    def on_press(self, key):
        name = getattr(key, "name", None)
        if name in _MODIFIERS:
            self._modifiers.add(name)
            return
        context = self.current_context()
        modifiers = sorted({_modifier(item) for item in self._modifiers})
        character = getattr(key, "char", None)
        # Ctrl+Alt may be AltGr producing ordinary text. Do not persist those chars.
        safe_command = ("ctrl" in modifiers or "cmd" in modifiers) and "alt" not in modifiers and "alt_gr" not in modifiers
        if name or (safe_command and character and character.isascii() and character.isalpha()):
            command = name or character.lower()
            self._gesture("shortcut", {"keys": [*modifiers, command]}, context)
        else:
            # Every ordinary character is reduced to activity; no string is built.
            with self._state_lock:
                if self._typed and self._typing_context != context:
                    previous_count, previous_context = self._typed, self._typing_context
                    self._typed = 0
                else:
                    previous_count, previous_context = 0, {}
                self._typed += 1
                self._typing_context = context
            if previous_count:
                self._gesture("input_activity", {"count": previous_count, "value": "<redacted>"}, previous_context)

    def on_release(self, key):
        name = getattr(key, "name", None)
        self._modifiers.discard(name)

    def start(self):
        if self._thread is not None:
            raise WindowsAdapterError("The Windows recorder has already started.")
        try:
            if not self.backend.available:
                raise WindowsAdapterError("Windows activity capture is unavailable on this system.")
            if not self.current_context():
                self._unavailable("The interactive foreground window is unavailable; activity needs review.")
                raise WindowsAdapterError("Windows activity capture requires access to the interactive desktop.")
            self.poll_once()
            if self.capture_input:
                from pynput import keyboard, mouse
                self._listeners = [keyboard.Listener(on_press=self.on_press, on_release=self.on_release,
                                                     win32_event_filter=self.keyboard_event_filter),
                                   mouse.Listener(on_click=self.on_click, on_scroll=self.on_scroll,
                                                  win32_event_filter=self.mouse_event_filter)]
                for listener in self._listeners:
                    listener.start()
        except BaseException:
            self.stop()
            raise

        def poll():
            while not self._stop.wait(self.interval):
                try:
                    self.poll_once()
                except (WindowsAdapterError, OSError):
                    self._unavailable("Windows activity capture failed; remaining activity needs review.")
                    self._stop.set()

        self._thread = threading.Thread(target=poll, name="apprentice-windows-capture", daemon=True)
        self._thread.start()

    @staticmethod
    def keyboard_event_filter(msg, data):
        # False hides the event from our listener callbacks; user input is untouched.
        # KBDLLHOOKSTRUCT: LLKHF_INJECTED (0x10), LOWER_IL_INJECTED (0x02).
        return not bool(data.flags & 0x12)

    @staticmethod
    def mouse_event_filter(msg, data):
        # MSLLHOOKSTRUCT: LLMHF_INJECTED (0x01), LOWER_IL_INJECTED (0x02).
        return not bool(data.flags & 0x03)

    def stop(self):
        self._flush_typing()
        self._stop.set()
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception:
                # A listener whose start failed may not have initialized its hook.
                # Other hooks and the COM helper must still be stopped.
                pass
        self.backend.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
