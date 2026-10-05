"""Owned application launches and exact VS Code workspace evidence.

Project proof comes from a local verifier extension using the public
``vscode.workspace.workspaceFolders`` API, rather than window titles or CLI exit
codes. Its extension and user data live only in the recording/run directory.
https://code.visualstudio.com/api/references/vscode-api#workspace
https://code.visualstudio.com/docs/configure/command-line
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import psutil


class ApplicationAdapterError(RuntimeError):
    """Stable public error messages must never contain captured arguments."""


def path_key(value):
    return os.path.normcase(os.path.normpath(str(Path(value).resolve())))


@dataclass
class LaunchHandle:
    process: object
    executable: str
    created: float
    owned: dict[int, float] = field(default_factory=dict)
    marker: Path | None = None
    nonce: str | None = None
    project: str | None = None
    job: object | None = None

    @property
    def pid(self):
        return self.process.pid


class ApplicationBackend:
    """Application launches without a shell; only our process tree is owned."""

    def resolve_executable(self, app, executable=None):
        candidate = executable or ("code" if app == "vscode" else app)
        found = shutil.which(candidate)
        if not found and Path(candidate).is_absolute() and Path(candidate).is_file():
            found = candidate
        if app == "vscode":
            candidates = []
            if found:
                path = Path(found)
                if sys.platform == "win32":
                    candidates.extend([path.parent.parent / "Code.exe", path.parent / "Code.exe"])
                candidates.append(path)
            if sys.platform == "win32" and not executable:
                candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) /
                                  "Programs/Microsoft VS Code/Code.exe")
            found = next((str(path) for path in candidates if path.is_file() and
                          path.suffix.casefold() not in {".cmd", ".bat"}), None)
        if not found or (sys.platform == "win32" and Path(found).suffix.casefold() not in {".exe", ".com"}):
            raise ApplicationAdapterError("The application needs an installed executable; batch launchers require terminal review.")
        if sys.platform == "win32" and getattr(Path(found).lstat(), "st_file_attributes", 0) & 0x400:
            raise ApplicationAdapterError("Windows execution aliases need an installed executable before application replay.")
        return str(Path(found).resolve())

    def supports_project(self, app):
        return app == "vscode"

    @staticmethod
    def _verifier(directory, nonce):
        extensions = directory / "extensions"
        extension = extensions / "apprentice.workspace-proof-1.0.0"
        extension.mkdir(parents=True, exist_ok=False)
        marker = directory / "workspace-proof.json"
        manifest = {
            "name": "workspace-proof", "publisher": "apprentice", "version": "1.0.0",
            "engines": {"vscode": "^1.74.0"}, "main": "./extension.js",
            "extensionKind": ["ui"], "activationEvents": ["*"],
            "capabilities": {"untrustedWorkspaces": {"supported": True}},
        }
        (extension / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        # All user strings are JavaScript JSON literals, never shell fragments.
        script = (
            "const vscode = require('vscode');\nconst fs = require('fs');\n"
            f"const marker = {json.dumps(str(marker))};\nconst nonce = {json.dumps(nonce)};\n"
            "function publish() {\n"
            "  const folders = (vscode.workspace.workspaceFolders || []).map(f => ({scheme:f.uri.scheme,path:f.uri.fsPath}));\n"
            "  const data = JSON.stringify({nonce,process_id:process.pid,folders});\n"
            "  const temporary = marker + '.tmp';\n"
            "  fs.writeFileSync(temporary, data, {encoding:'utf8',mode:0o600});\n"
            "  fs.renameSync(temporary, marker);\n}\n"
            "exports.activate = function(context) { publish();\n"
            "  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(publish));\n};\n"
        )
        (extension / "extension.js").write_text(script, encoding="utf-8")
        profile = directory / "profile"
        settings = profile / "User"
        settings.mkdir(parents=True, exist_ok=False)
        (settings / "settings.json").write_text(json.dumps({
            "update.mode": "none", "extensions.autoUpdate": False,
            "extensions.autoCheckUpdates": False, "telemetry.telemetryLevel": "off",
            "workbench.startupEditor": "none", "window.restoreWindows": "none",
            "security.workspace.trust.startupPrompt": "never",
        }), encoding="utf-8")
        return extensions, profile, marker

    def launch(self, app, executable, args, cwd, project_path, evidence_dir):
        from apprentice.executor.filesystem import PathPolicy
        directory = Path(evidence_dir)
        PathPolicy.no_links(directory)
        directory.mkdir(parents=True, exist_ok=False)
        nonce, marker = None, None
        launch_args = list(args)
        if project_path is not None:
            if not self.supports_project(app):
                raise ApplicationAdapterError("This application's project state has no verified adapter yet.")
            if args:
                # Profile/extension/remote flags could bypass ownership and exact proof.
                raise ApplicationAdapterError("Verified VS Code projects use their reviewed path without extra launch arguments.")
            nonce = uuid4().hex
            extensions, profile, marker = self._verifier(directory, nonce)
            launch_args = ["--new-window", "--skip-add-to-recently-opened",
                           "--user-data-dir", str(profile), "--extensions-dir", str(extensions), str(project_path)]
        environment = dict(os.environ)
        if app == "vscode":
            environment.pop("ELECTRON_RUN_AS_NODE", None)
        job = None
        flags = 0
        if sys.platform == "win32":
            from apprentice.executor.terminal import WindowsJob
            job = WindowsJob()
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004  # CREATE_SUSPENDED
        process = None
        try:
            process = psutil.Popen([str(executable), *launch_args], cwd=cwd, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags,
                                   start_new_session=sys.platform != "win32")
            created = process.create_time()
            if job is not None:
                job.attach_and_resume(process.pid)
        except (OSError, psutil.Error):
            if process is not None:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            if job is not None:
                job.close()
            raise ApplicationAdapterError("The reviewed application could not launch.") from None
        except BaseException:
            if process is not None:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            if job is not None:
                job.close()
            raise
        return LaunchHandle(process, str(executable), created, {process.pid: created},
                            marker, nonce, str(project_path) if project_path is not None else None, job)

    @staticmethod
    def _same_process(pid, created):
        try:
            process = psutil.Process(pid)
            if process.create_time() != created or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            return process
        except psutil.Error:
            return None

    def owned_processes(self, handle):
        root = self._same_process(handle.pid, handle.created)
        if root is not None:
            try:
                for process in root.children(recursive=True):
                    handle.owned[process.pid] = process.create_time()
            except psutil.Error:
                pass
        return {pid: process for pid, created in tuple(handle.owned.items())
                if (process := self._same_process(pid, created)) is not None}

    def running(self, handle):
        root = self._same_process(handle.pid, handle.created)
        if root is None:
            return False
        self.owned_processes(handle)
        try:
            return path_key(root.exe()) == path_key(handle.executable)
        except psutil.Error:
            return False

    def project_open(self, handle, path):
        if handle.marker is None or not self.running(handle):
            return False
        try:
            if handle.marker.stat().st_size > 65536:
                return False
            proof = json.loads(handle.marker.read_text(encoding="utf-8"))
            if not isinstance(proof, dict) or proof.get("nonce") != handle.nonce:
                return False
            processes = self.owned_processes(handle)
            if type(proof.get("process_id")) is not int or proof["process_id"] not in processes:
                return False
            folders = proof.get("folders")
            if not isinstance(folders, list) or len(folders) != 1:
                return False
            folder = folders[0]
            return (isinstance(folder, dict) and folder.get("scheme") == "file" and
                    isinstance(folder.get("path"), str) and path_key(folder["path"]) == path_key(path))
        except (OSError, ValueError, TypeError):
            return False

    def terminate(self, handle):
        """Never terminate a preexisting process or a PID that has been reused."""
        if handle.job is not None:
            handle.job.close()
            handle.job = None
        owned = self.owned_processes(handle)
        ordered = [process for pid, process in owned.items() if pid != handle.pid]
        ordered.extend(process for pid, process in owned.items() if pid == handle.pid)
        for process in ordered:
            try:
                process.terminate()
            except psutil.Error:
                pass
        _, remaining = psutil.wait_procs(ordered, timeout=0.4)
        for process in remaining:
            try:
                if self._same_process(process.pid, handle.owned[process.pid]) is not None:
                    process.kill()
            except psutil.Error:
                pass
        try:
            handle.process.wait(timeout=0.4)
        except (psutil.Error, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def release(handle):
        """A verified GUI launch becomes a normal persistent user application."""
        if handle.job is not None:
            handle.job.release()
            handle.job = None
