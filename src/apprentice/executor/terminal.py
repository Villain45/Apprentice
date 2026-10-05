"""Reviewed local commands with owned process trees and verified, fresh outputs.

Arguments are passed directly to the executable. Output is deliberately discarded:
commands can print passwords or tokens, and neither output nor environment values
belong in the value-free execution audit.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import ipaddress
import os
import shutil
import signal
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from apprentice.executor.filesystem import PathPolicy
from apprentice.executor.runtime import Control, ExecutionError, render, text
from apprentice.store.workflow import references


class WindowsJob:
    """Assign a suspended process before it can launch an unowned child."""

    def __init__(self):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", Counters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]

        class Accounting(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
                        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
                        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD)]

        self.ThreadEntry = ThreadEntry
        self.ExtendedLimits = ExtendedLimits
        self.Accounting = Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ExecutionError("An owned Windows process group could not be created.")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ExecutionError("Windows process cleanup could not be configured.")

    def attach_and_resume(self, pid):
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        try:
            if not handle or not self.api.AssignProcessToJobObject(self.handle, handle):
                raise ExecutionError("The command could not be assigned to its owned process group.")
        finally:
            if handle:
                self.api.CloseHandle(handle)
        snapshot = self.api.CreateToolhelp32Snapshot(0x0004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ExecutionError("The command could not be resumed safely.")
        try:
            entry = self.ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process_id == pid:
                    thread = self.api.OpenThread(0x0002, False, entry.thread_id)
                    try:
                        if thread and self.api.ResumeThread(thread) != 0xFFFFFFFF:
                            return
                    finally:
                        if thread:
                            self.api.CloseHandle(thread)
                    break
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        raise ExecutionError("The command could not be resumed safely.")

    def close(self):
        handle, self.handle = getattr(self, "handle", None), None
        if handle:
            self.api.CloseHandle(handle)

    def release(self):
        """Hand a successfully opened GUI application back to the user."""
        if not self.handle:
            return
        limits = self.ExtendedLimits()
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ExecutionError("The opened application could not be handed back safely.")
        self.close()

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise ExecutionError("The owned process group could not be stopped.")

    def active_processes(self):
        if not self.handle:
            return 0
        information = self.Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(information),
                                                  ctypes.sizeof(information), None):
            raise ExecutionError("Owned process cleanup could not be verified.")
        return information.active_processes


@dataclass(eq=False)
class _OwnedProcess:
    process: asyncio.subprocess.Process
    job: WindowsJob | None = None


class TerminalExecutor:
    def __init__(self, *, control=None, policy=None, run_dir=None):
        self.control = control if control is not None else Control()
        self.policy = policy if policy is not None else PathPolicy(())
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self._owned: set[_OwnedProcess] = set()
        self._closing: dict[_OwnedProcess, asyncio.Task] = {}
        self.script_cleanup_pending = False

    def environment(self, step, values, *, known_only=False):
        # Normalize Windows keys so an override cannot leave ambiguous PATH/Path entries.
        environment = {key.upper() if os.name == "nt" else key: value for key, value in os.environ.items()}
        for key, template in getattr(step, "env", {}).items():
            if known_only and any(name not in values for name in references(template)):
                continue
            value = text(render(template, values))
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ExecutionError("Command environment entries are invalid.")
            environment[key.upper() if os.name == "nt" else key] = value
        return environment

    @staticmethod
    def executable(value, environment):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ExecutionError("A command needs an available executable.")
        resolved = shutil.which(value, path=environment.get("PATH"))
        if resolved is None or not Path(resolved).is_file():
            raise ExecutionError("The command executable is unavailable.")
        if os.name == "nt" and getattr(Path(resolved).lstat(), "st_file_attributes", 0) & 0x400:
            raise ExecutionError("Windows executable aliases are unavailable for verified command replay.")
        if Path(resolved).suffix.lower() in (".cmd", ".bat"):
            raise ExecutionError("Batch files need a reviewed PowerShell script; implicit shell execution is refused.")
        return str(Path(resolved).absolute())

    def resolve(self, step, values):
        cwd = self.policy.path(render(step.cwd, values))
        if not cwd.is_dir():
            raise ExecutionError("The command working directory is missing.")
        environment = self.environment(step, values)
        executable = getattr(step, "executable", None)
        if step.action == "run_script":
            executable = executable or self.powershell()
            if executable is None or Path(executable).stem.lower() not in ("pwsh", "powershell"):
                raise ExecutionError("Reviewed scripts require an available PowerShell executable.")
        resolved = self.executable(text(render(executable, values)), environment)
        arguments = [text(render(value, values)) for value in getattr(step, "args", [])]
        if any("\x00" in value for value in arguments):
            raise ExecutionError("Command arguments cannot contain null characters.")
        return cwd, environment, resolved, arguments

    @staticmethod
    def powershell():
        # WindowsApps aliases can be discoverable by PATH while CreateProcess
        # cannot execute them (WinError 1920). Choose an actual installed host.
        for name in ("pwsh", "powershell"):
            candidate = shutil.which(name)
            if candidate is None:
                continue
            if os.name == "nt" and getattr(Path(candidate).lstat(), "st_file_attributes", 0) & 0x400:
                continue
            return candidate
        return None

    @staticmethod
    def readiness_address(value):
        try:
            parsed = urlsplit(value)
            port = parsed.port or 80
        except (TypeError, ValueError):
            raise ExecutionError("Service readiness needs a valid loopback HTTP URL.") from None
        if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or not 1 <= port <= 65535):
            raise ExecutionError("Service readiness needs a valid loopback HTTP URL.")
        return parsed.hostname, port

    @classmethod
    def unused_port(cls, value):
        host, port = cls.readiness_address(value)
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            for family, kind, protocol, _, address in addresses:
                with socket.socket(family, kind, protocol) as probe:
                    probe.settimeout(0.15)
                    if probe.connect_ex(address) == 0:
                        raise ExecutionError("A service is already listening on the requested readiness port.")
        except OSError:
            raise ExecutionError("The local service readiness address could not be checked.") from None

    def preflight(self, workflow, values):
        for step in workflow.steps:
            if step.surface != "terminal" or step.action not in ("run_command", "run_script"):
                continue
            known = lambda value: all(name in values for name in references(value))
            environment = self.environment(step, values, known_only=True)
            executable = getattr(step, "executable", None)
            if step.action == "run_script":
                executable = self.powershell()
                if executable is None:
                    raise ExecutionError("Reviewed scripts require an available PowerShell executable.")
            if executable is not None and known(executable):
                candidate = text(render(executable, values))
                deferred_path = any(key.casefold() == "path" and not known(value)
                                    for key, value in getattr(step, "env", {}).items())
                if deferred_path and not Path(candidate).is_absolute():
                    raise ExecutionError("Executable lookup needs an unavailable future PATH; review the command host.")
                self.executable(candidate, environment)
            if known(step.cwd):
                cwd = self.policy.path(render(step.cwd, values))
                if not cwd.is_dir():
                    created_earlier = any(
                        previous.id < step.id and previous.surface == "filesystem"
                        and previous.action == "create_directory" and known(previous.path)
                        and self.policy.absolute(render(previous.path, values)) == cwd
                        for previous in workflow.steps
                    )
                    if not created_earlier:
                        raise ExecutionError("The command working directory is missing.")
            for argument in getattr(step, "args", []):
                if known(argument) and "\x00" in text(render(argument, values)):
                    raise ExecutionError("Command arguments cannot contain null characters.")
            for artifact in getattr(step, "artifacts", []):
                if all(name in values for name in references(artifact.path)):
                    self.policy.path(render(artifact.path, values))
            if step.postcondition.kind == "service_ready":
                url = step.postcondition.url
                if all(name in values for name in references(url)):
                    self.unused_port(text(render(url, values)))

    async def _spawn(self, executable, arguments, cwd, environment):
        job = WindowsJob() if os.name == "nt" else None
        options = {"creationflags": 0x00000004 | subprocess.CREATE_NO_WINDOW} if job else {"start_new_session": True}
        process = None
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
            executable, *arguments, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options,
        ))
        try:
            # Shield process creation so cancellation cannot orphan a just-created process.
            try:
                process = await asyncio.shield(creation)
            except asyncio.CancelledError as cancellation:
                # Further Stop requests must not cancel the task delivering the
                # handle for the already-created OS process.
                while True:
                    try:
                        process = await asyncio.shield(creation)
                        break
                    except asyncio.CancelledError:
                        if creation.cancelled():
                            raise
                raise cancellation
            if job:
                job.attach_and_resume(process.pid)
            owned = _OwnedProcess(process, job)
            self._owned.add(owned)
            return owned
        except BaseException:
            if job:
                job.close()
            if process is not None:
                if not job:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                cleanup = asyncio.create_task(asyncio.wait_for(process.wait(), 2))
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.cancelled():
                            raise
            raise

    async def _close_owned(self, owned):
        if owned not in self._closing:
            cleanup = asyncio.create_task(self._stop_owned(owned))
            self._closing[owned] = cleanup
            def finished(task):
                self._owned.discard(owned)
                self._closing.pop(owned, None)
                if not task.cancelled():
                    task.exception()  # Retrieve failures even if the caller was cancelled twice.
            cleanup.add_done_callback(finished)
        await asyncio.shield(self._closing[owned])

    async def _stop_owned(self, owned):
        if owned.job:
            try:
                owned.job.terminate()
                deadline = asyncio.get_running_loop().time() + 2
                while owned.job.active_processes():
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ExecutionError("Owned process cleanup did not finish in time.")
                    await asyncio.sleep(0.01)
            finally:
                owned.job.close()  # Kill-on-close remains a fallback even on cancellation.
        else:
            try:
                os.killpg(owned.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(owned.process.wait(), 0.5)
        except asyncio.TimeoutError:
            if owned.process.returncode is None:
                try:
                    owned.process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(owned.process.wait(), 2)
        finally:
            if not owned.job:
                # The parent may have exited while descendants ignore SIGTERM.
                try:
                    os.killpg(owned.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def close(self):
        if self._owned:
            await asyncio.gather(*(self._close_owned(item) for item in tuple(self._owned)))

    async def snapshot(self, path):
        self.policy.no_links(path)
        if not path.exists():
            return None
        if not path.is_file():
            raise ExecutionError("Command artifacts must be regular files.")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while data := stream.read(1024 * 1024):
                self.control.check()
                digest.update(data)
                await asyncio.sleep(0)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ExecutionError("A command artifact changed during verification.")
        return after.st_size, after.st_mtime_ns, after.st_ctime_ns, digest.hexdigest()

    @staticmethod
    def listener_owned(owned, url):
        host, port = TerminalExecutor.readiness_address(url)
        try:
            targets = {ipaddress.ip_address(address[0]) for _, _, _, _, address
                       in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
            parent = psutil.Process(owned.process.pid)
            pids = {parent.pid, *(child.pid for child in parent.children(recursive=True))}
            for connection in psutil.net_connections(kind="tcp"):
                if connection.pid not in pids or connection.status != psutil.CONN_LISTEN or connection.laddr.port != port:
                    continue
                listener = ipaddress.ip_address(connection.laddr.ip)
                if any(listener == target or (listener.is_unspecified and listener.version == target.version)
                       for target in targets):
                    return True
            return False
        except (psutil.Error, OSError):
            raise ExecutionError("The local service listener's ownership could not be verified.") from None

    async def ready(self, owned, url, expected_status):
        self.readiness_address(url)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=0.25) as client:
            while True:
                self.control.check()
                if owned.process.returncode is not None:
                    raise ExecutionError("The service exited before readiness verified.")
                try:
                    response = await client.get(url)
                except httpx.HTTPError:
                    pass
                else:
                    if response.status_code == expected_status:
                        if await asyncio.to_thread(self.listener_owned, owned, url):
                            if owned.process.returncode is None:
                                return
                        else:
                            raise ExecutionError("The readiness response belongs to a different process.")
                await asyncio.sleep(0.05)

    async def step(self, step, values):
        """Execute once, verify, and publish outputs atomically; never retry a command."""
        self.control.check()
        owned = None
        keep = False
        script_path = None
        previously_owned = set(self._owned)
        try:
            cwd, environment, executable, arguments = self.resolve(step, values)
            artifacts = {}
            for artifact in getattr(step, "artifacts", []):
                path = self.policy.path(render(artifact.path, values))
                artifacts[artifact.save_as] = (path, await self.snapshot(path))
            if step.postcondition.kind == "service_ready":
                url = text(render(step.postcondition.url, values))
                await asyncio.to_thread(self.unused_port, url)
            if step.action == "run_script":
                if self.run_dir is None:
                    raise ExecutionError("Reviewed scripts require a private run directory.")
                self.run_dir.mkdir(parents=True, exist_ok=True)
                descriptor, filename = tempfile.mkstemp(prefix="command-", suffix=".ps1", dir=self.run_dir)
                script_path = Path(filename)
                with os.fdopen(descriptor, "w", encoding="utf-8-sig") as stream:
                    stream.write("$ErrorActionPreference = 'Stop'\n$global:LASTEXITCODE = 0\n")
                    stream.write(step.script)
                    stream.write("\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n")
                arguments = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(script_path), *arguments]
            timeout = getattr(step, "timeout_ms", None) or self.control.timeout_ms
            owned = await self.control.wait(self._spawn(executable, arguments, cwd, environment), timeout_ms=timeout)
            if step.postcondition.kind == "command_completed":
                exit_code = await self.control.wait(owned.process.wait(), timeout_ms=timeout)
                if exit_code != step.postcondition.exit_code:
                    raise ExecutionError("The command exit code did not match its reviewed postcondition.")
                await asyncio.shield(self._close_owned(owned))
                owned = None
            elif step.postcondition.kind == "service_ready":
                await self.control.wait(self.ready(owned, url, step.postcondition.expected_status), timeout_ms=timeout)
            else:
                raise ExecutionError("The command's postcondition is unsupported.")
            outputs = {}
            for name, (path, before) in artifacts.items():
                after = await self.snapshot(path)
                if after is None or after == before:
                    raise ExecutionError("A command artifact is missing or unchanged; stale outputs were not bound.")
                outputs[name] = str(path)
            self.control.check()
            values.update(outputs)
            keep = owned is not None  # Keep only a verified service until this workflow ends.
        except (OSError, ValueError, psutil.Error):
            raise ExecutionError("The command failed. Review the program and current workspace before another run.") from None
        finally:
            if not keep:
                # A stop can win Control.wait just as spawn returns, before the
                # caller receives its handle. Reclaim every process from this step.
                await asyncio.shield(asyncio.gather(*(
                    self._close_owned(item) for item in self._owned - previously_owned
                )))
            if script_path is not None:
                try:
                    script_path.unlink(missing_ok=True)
                except OSError:
                    # A scanner/editor may retain a file lock. Preserve the primary
                    # outcome (especially cancellation); scripts contain no rendered
                    # parameter values. Expose metadata for the router's audit.
                    self.script_cleanup_pending = True
