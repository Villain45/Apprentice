"""Bounded filesystem operations with content verification and no implicit overwrites."""

from __future__ import annotations

import asyncio
import hashlib
import errno
import os
import re
import stat
from pathlib import Path, PureWindowsPath

from apprentice.executor.runtime import Control, ExecutionError, render


def reparse(path):
    attributes = path.lstat()
    return stat.S_ISLNK(attributes.st_mode) or bool(getattr(attributes, "st_file_attributes", 0) & 0x400)


class PathPolicy:
    """Write only inside reviewed roots; reject links, junctions and root mutations."""

    def __init__(self, roots, artifact_root=None):
        self.roots = tuple(self.absolute(item) for item in roots)
        self.artifact_root = self.absolute(artifact_root) if artifact_root is not None else None
        if any(root == Path(root.anchor) for root in self.roots):
            raise ExecutionError("Choose a workspace directory rather than an entire drive as a write root.")
        for root in self.roots:
            self.no_links(root)

    @staticmethod
    def absolute(value):
        if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
            raise ExecutionError("Filesystem operations need valid absolute paths.")
        path = Path(value)
        if not path.is_absolute():
            raise ExecutionError("Filesystem operations need absolute paths.")
        if os.name == "nt":
            if str(path).startswith(("\\\\?\\", "\\\\.\\")):
                raise ExecutionError("Filesystem replay does not use Windows device namespaces.")
            for part in path.parts[1:]:
                if part in (".", ".."):
                    continue
                if (part.endswith((".", " ")) or re.search(r'[<>:"|?*\x00-\x1f]', part)
                        or PureWindowsPath(part).is_reserved()):
                    raise ExecutionError("Filesystem paths cannot contain Windows devices, streams or invalid names.")
        # Normalize .. without following symlinks; then inspect each actual ancestor.
        return Path(os.path.abspath(path))

    @staticmethod
    def no_links(path):
        for ancestor in [*reversed(path.parents), path]:
            if ancestor.exists() or ancestor.is_symlink():
                if reparse(ancestor):
                    raise ExecutionError("Filesystem replay does not traverse symbolic links or junctions.")

    def path(self, value, *, write=False, artifact=False):
        path = self.absolute(value)
        self.no_links(path)
        roots = self.roots + ((self.artifact_root,) if artifact and self.artifact_root else ())
        if not any(path.is_relative_to(root) for root in roots):
            raise ExecutionError("A filesystem path is outside the reviewed workspace roots.")
        if write and path in self.roots:
            raise ExecutionError("A workflow cannot mutate its workspace root.")
        return path


class FilesystemExecutor:
    def __init__(self, roots=(), *, explorer=None, control=None, artifact_root=None):
        self.control = control if control is not None else Control()
        self.policy = PathPolicy(roots, artifact_root)
        self.explorer = explorer

    def preflight(self, workflow, values):
        for step in workflow.steps:
            if step.surface != "filesystem":
                continue
            if step.action == "open_directory":
                if self.explorer is None:
                    from apprentice.adapters.windows import WindowsExplorer
                    self.explorer = WindowsExplorer()
                if not self.explorer.available:
                    raise ExecutionError("Verified Explorer navigation is unavailable on this system.")
                if hasattr(self.explorer, "check_available") and not self.explorer.check_available():
                    raise ExecutionError("Verified Explorer navigation is unavailable on this system.")
            # Paths containing outputs from earlier steps are checked when those outputs are bound.
            for field in ("path", "source", "destination"):
                value = getattr(step, field)
                if value is None:
                    continue
                from apprentice.store.workflow import references
                if any(name not in values for name in references(value)):
                    continue
                if step.action == "open_directory":
                    self.policy.no_links(self.policy.absolute(render(value, values)))
                    continue
                self.policy.path(render(value, values), write=field == "destination" or (
                    field == "path" and step.action != "open_directory"
                ), artifact=field == "source")

    def resolve(self, value, values, *, write=False, artifact=False):
        return self.policy.path(render(value, values), write=write, artifact=artifact)

    async def digest(self, path):
        self.policy.no_links(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while data := stream.read(1024 * 1024):
                self.control.check()
                digest.update(data)
                await asyncio.sleep(0)
        self.control.check()
        return digest.hexdigest()

    async def manifest(self, path):
        """Include empty directories and every file's content, excluding links/special files."""
        self.control.check()
        self.policy.no_links(path)
        if path.is_file():
            return {".": ("file", await self.digest(path))}
        if not path.is_dir():
            raise ExecutionError("A source path is missing or is not a regular file/directory.")
        result = {".": ("directory", None)}
        pending = [path]
        while pending:
            directory = pending.pop()
            self.policy.no_links(directory)
            for item in sorted(directory.iterdir()):
                self.control.check()
                self.policy.no_links(item)
                relative = str(item.relative_to(path))
                if item.is_dir():
                    result[relative] = ("directory", None)
                    pending.append(item)
                elif item.is_file():
                    result[relative] = ("file", await self.digest(item))
                else:
                    raise ExecutionError("Filesystem replay only supports regular files and directories.")
                await asyncio.sleep(0)
        return result

    async def copy_file(self, source, destination):
        self.control.check()
        self.policy.no_links(source)
        self.policy.no_links(destination)
        # Exclusive creation prevents silently replacing a file that appeared since preflight.
        with source.open("rb") as original, destination.open("xb") as copied:
            while data := original.read(1024 * 1024):
                self.control.check()
                copied.write(data)
                await asyncio.sleep(0)
        self.control.check()

    async def copy(self, source, destination, before, recursive):
        if source.is_file():
            await self.copy_file(source, destination)
            return
        if not recursive:
            raise ExecutionError("Copying a directory needs an explicitly reviewed recursive operation.")
        destination.mkdir(exist_ok=False)
        for relative, (kind, _) in sorted(before.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
            if relative == ".":
                continue
            self.control.check()
            target = destination / relative
            self.policy.no_links(target)
            if kind == "directory":
                target.mkdir(exist_ok=False)
            else:
                await self.copy_file(source / relative, target)
            await asyncio.sleep(0)

    async def delete(self, path, recursive, *, expected=None, artifact=False):
        if expected is not None and await self.manifest(path) != expected:
            raise ExecutionError("The source changed before removal. Review both paths.")
        if path.is_file():
            self.control.check()
            self.policy.no_links(path)
            if expected is not None and await self.digest(path) != expected["."][1]:
                raise ExecutionError("The source changed before removal. Review both paths.")
            path.unlink()
        elif path.is_dir():
            if not recursive:
                path.rmdir()  # Refuse a nonempty directory rather than inferring recursive intent.
                return
            before = expected if expected is not None else await self.manifest(path)
            for relative in sorted(before, key=lambda item: (len(Path(item).parts), item), reverse=True):
                self.control.check()
                target = path if relative == "." else path / relative
                self.policy.path(target, write=True, artifact=artifact)
                if before[relative][0] == "directory":
                    target.rmdir()
                else:
                    if expected is not None and await self.digest(target) != before[relative][1]:
                        raise ExecutionError("A source file changed before removal. Review both paths.")
                    target.unlink()
                await asyncio.sleep(0)
        else:
            raise ExecutionError("The path to delete is missing.")

    async def step(self, step, values):
        """Dispatch once. Uncertain/partial operations stop for review, never retry."""
        self.control.check()
        path = None
        if step.path:
            if step.action == "open_directory":
                path = self.policy.absolute(render(step.path, values))
                self.policy.no_links(path)
            else:
                path = self.resolve(step.path, values, write=True)
        try:
            if step.action == "open_directory":
                if not path.is_dir():
                    raise ExecutionError("The directory to open is missing.")
                await self.control.wait(asyncio.to_thread(self.explorer.open_directory, path))
                async def opened():
                    while True:
                        self.control.check()
                        if await asyncio.to_thread(self.explorer.directory_is_open, path):
                            return
                        await asyncio.sleep(0.05)
                await self.control.wait(opened())
            elif step.action == "create_directory":
                path.mkdir(exist_ok=False)
                if not path.is_dir():
                    raise ExecutionError("Directory creation did not verify.")
            elif step.action == "create_file":
                from apprentice.executor.runtime import text
                content = text(render(step.content or "", values)).encode("utf-8")
                with path.open("xb") as stream:
                    for offset in range(0, len(content), 1024 * 1024):
                        self.control.check()
                        stream.write(content[offset:offset + 1024 * 1024])
                        await asyncio.sleep(0)
                if not path.is_file() or await self.digest(path) != hashlib.sha256(content).hexdigest():
                    raise ExecutionError("The new file's content did not verify.")
            elif step.action in ("copy_path", "move_path", "rename_path"):
                source = self.resolve(step.source, values, artifact=True)
                destination = self.resolve(step.destination, values, write=True)
                if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
                    raise ExecutionError("Transfer paths must be separate and cannot contain one another.")
                if source in (*self.policy.roots, self.policy.artifact_root) and step.action != "copy_path":
                    raise ExecutionError("A workflow cannot move or rename its workspace root.")
                if destination.exists() or destination.is_symlink():
                    raise ExecutionError("Destination already exists; implicit overwrites are refused.")
                if not destination.parent.is_dir():
                    raise ExecutionError("The transfer destination's parent directory is missing.")
                before = await self.manifest(source)
                self.control.check()
                if step.action == "copy_path":
                    await self.copy(source, destination, before, step.recursive)
                else:
                    if step.action == "rename_path" and source.parent != destination.parent:
                        raise ExecutionError("Rename stays in one directory; use move_path for a different directory.")
                    # Prefer atomic same-volume rename. Cross-volume move verifies both copies before removing source.
                    try:
                        os.rename(source, destination)
                    except OSError as error:
                        if step.action != "move_path" or (error.errno != errno.EXDEV and getattr(error, "winerror", None) != 17):
                            raise
                        await self.copy(source, destination, before, recursive=True)
                        if await self.manifest(destination) != before or await self.manifest(source) != before:
                            raise ExecutionError("Cross-volume copy did not verify. The source was retained; review both paths.")
                        self.control.check()
                        await self.delete(source, recursive=source.is_dir(), expected=before, artifact=True)
                if await self.manifest(destination) != before:
                    raise ExecutionError("Transferred contents did not verify. Review both paths.")
                if step.action == "copy_path":
                    if await self.manifest(source) != before:
                        raise ExecutionError("The source changed during copying. Review both paths.")
                elif source.exists():
                    raise ExecutionError("The transferred source is still present. Review both paths.")
                path = destination
            elif step.action == "delete_path":
                await self.delete(path, step.recursive)
                if path.exists() or path.is_symlink():
                    raise ExecutionError("Deleted path is still present. Review its current state.")
            else:
                raise ExecutionError("Unsupported filesystem action.")
            self.control.check()
            if step.save_as:
                values[step.save_as] = str(path)
        except (OSError, ValueError):
            raise ExecutionError("Filesystem action failed. Review the paths and current state before another run.") from None
        except RuntimeError:
            raise ExecutionError("The filesystem integration failed. Review the current system state before another run.") from None

    def close(self):
        if self.explorer is not None and hasattr(self.explorer, "close"):
            self.explorer.close()
