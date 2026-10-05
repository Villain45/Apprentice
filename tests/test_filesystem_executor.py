"""Real sandbox I/O, verified outputs, confirmation and partial-operation boundaries."""

import asyncio
import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apprentice.executor import run_workflow
from apprentice.executor.filesystem import FilesystemExecutor, PathPolicy
from apprentice.executor.runtime import Audit, Control, ExecutionError, bind_parameters, dry_run
from apprentice.executor.surfaces import SurfaceExecutor
from apprentice.store.system_workflow import SystemWorkflow
from apprentice.store.workflow import Workflow, load_workflow, save_workflow


def fs_step(action, *, path=None, source=None, destination=None, **options):
    check = {"kind": "path_state", "path": path, "state": {
        "create_directory": "directory", "create_file": "file", "delete_path": "absent",
    }.get(action)}
    if action == "open_directory":
        check = {"kind": "directory_open", "path": path}
    elif action in ("copy_path", "move_path", "rename_path"):
        check = {"kind": "path_transferred", "source": source, "destination": destination,
                 "source_absent": action != "copy_path"}
    fields = {"surface": "filesystem", "action": action, "purpose": action.replace("_", " "),
              "postcondition": check, **options}
    fields.update({name: value for name, value in (("path", path), ("source", source),
                                                 ("destination", destination)) if value is not None})
    return fields


def workflow(*steps):
    numbered = [{**step, "id": index} for index, step in enumerate(steps, 1)]
    return SystemWorkflow.model_validate({
        "status": "ready", "name": "Sandbox files", "description": "Verify sandbox file operations.",
        "parameters": {"workspace": {"type": "directory"}}, "write_roots": ["{workspace}"],
        "steps": numbered, "confirm_before": [step["id"] for step in numbered if step["action"] == "delete_path"],
    })


def replay(tmp_path, task, *, confirm=lambda _: True, control=None, filesystem=None):
    audit = Audit()
    result = asyncio.run(SurfaceExecutor(tmp_path / "run", confirm=confirm, control=control,
                                         filesystem=filesystem, audit=audit).run(task, {"workspace": str(tmp_path)}))
    return result, audit


def test_create_copy_move_rename_and_delete_with_content_verification(tmp_path):
    requests = []
    task = workflow(
        fs_step("create_directory", path="{workspace}/Archive"),
        fs_step("create_file", path="{workspace}/source.txt", content="hello {{world}}\n", save_as="source_file"),
        fs_step("copy_path", source="{source_file}", destination="{workspace}/Archive/copy.txt"),
        fs_step("move_path", source="{source_file}", destination="{workspace}/Archive/moved.txt"),
        fs_step("rename_path", source="{workspace}/Archive/moved.txt", destination="{workspace}/Archive/final.txt"),
        fs_step("delete_path", path="{workspace}/Archive/copy.txt"),
    )
    result, audit = replay(tmp_path, task, confirm=lambda request: requests.append(request.step_id) or True)
    assert result.status == "completed" and result.completed_steps == (1, 2, 3, 4, 5, 6)
    assert (tmp_path / "Archive/final.txt").read_text() == "hello {world}\n"
    assert not (tmp_path / "source.txt").exists() and not (tmp_path / "Archive/copy.txt").exists()
    assert requests == [None, 6]
    assert "hello" not in json.dumps(audit.events) and str(tmp_path) not in json.dumps(audit.events)


def test_recursive_copy_preserves_empty_directories_and_file_contents(tmp_path):
    (tmp_path / "source/empty").mkdir(parents=True)
    (tmp_path / "source/nested").mkdir()
    (tmp_path / "source/nested/file.bin").write_bytes(bytes(range(256)) * 100)
    result, _ = replay(tmp_path, workflow(fs_step("copy_path", source="{workspace}/source",
                                               destination="{workspace}/copied", recursive=True)))
    assert result.status == "completed"
    assert (tmp_path / "copied/empty").is_dir()
    assert (tmp_path / "copied/nested/file.bin").read_bytes() == (tmp_path / "source/nested/file.bin").read_bytes()


def test_directory_copy_requires_explicit_recursion(tmp_path):
    (tmp_path / "source").mkdir()
    result, _ = replay(tmp_path, workflow(fs_step("copy_path", source="{workspace}/source", destination="{workspace}/copy")))
    assert result.status == "failed" and not (tmp_path / "copy").exists()


@pytest.mark.parametrize("directory", [False, True])
def test_cross_volume_move_verifies_copy_before_removing_source(tmp_path, monkeypatch, directory):
    from apprentice.executor import filesystem
    source = tmp_path / "source"
    if directory:
        (source / "empty").mkdir(parents=True)
        (source / "file.txt").write_text("preserve bytes")
    else:
        source.write_text("preserve bytes")

    def cross_volume(*args):
        raise OSError(errno.EXDEV, "synthetic cross-device boundary")
    monkeypatch.setattr(filesystem.os, "rename", cross_volume)
    result, _ = replay(tmp_path, workflow(fs_step("move_path", source="{workspace}/source", destination="{workspace}/destination")))
    assert result.status == "completed" and not source.exists()
    target = tmp_path / "destination/file.txt" if directory else tmp_path / "destination"
    assert target.read_text() == "preserve bytes"
    if directory:
        assert (tmp_path / "destination/empty").is_dir()


def test_cross_volume_copy_failure_keeps_source_and_stops_later_operations(tmp_path, monkeypatch):
    from apprentice.executor import filesystem
    source = tmp_path / "source.txt"
    source.write_text("original")
    def cross_volume(*args):
        raise OSError(errno.EXDEV, "synthetic cross-device boundary")
    monkeypatch.setattr(filesystem.os, "rename", cross_volume)
    executor = FilesystemExecutor([tmp_path])
    async def bad_copy(source, destination):
        destination.write_text("incorrect")
    monkeypatch.setattr(executor, "copy_file", bad_copy)
    result, _ = replay(tmp_path, workflow(
        fs_step("move_path", source="{workspace}/source.txt", destination="{workspace}/destination.txt"),
        fs_step("delete_path", path="{workspace}/source.txt")), filesystem=executor)
    assert result.status == "failed" and result.completed_steps == ()
    assert source.read_text() == "original"


def test_cross_volume_removal_never_deletes_newly_added_unverified_files(tmp_path, monkeypatch):
    from apprentice.executor import filesystem
    source = tmp_path / "source"
    source.mkdir()
    (source / "copied.txt").write_text("original")
    def cross_volume(*args):
        raise OSError(errno.EXDEV, "synthetic cross-device boundary")
    monkeypatch.setattr(filesystem.os, "rename", cross_volume)
    executor = FilesystemExecutor([tmp_path])
    original_manifest = executor.manifest
    source_scans = 0

    async def changing_manifest(path):
        nonlocal source_scans
        manifest = await original_manifest(path)
        if path == source:
            source_scans += 1
            if source_scans == 3:
                # Arrives after the removal precheck; it was never part of the verified copy.
                (source / "new.txt").write_text("retain me")
        return manifest

    monkeypatch.setattr(executor, "manifest", changing_manifest)
    result, _ = replay(tmp_path, workflow(fs_step("move_path", source="{workspace}/source", destination="{workspace}/destination")),
                       filesystem=executor)
    assert result.status == "failed" and result.completed_steps == ()
    assert (source / "new.txt").read_text() == "retain me"
    assert (tmp_path / "destination/copied.txt").read_text() == "original"


def test_recursive_delete_is_confirmed_and_cannot_delete_workspace_root(tmp_path):
    (tmp_path / "disposable/empty").mkdir(parents=True)
    (tmp_path / "disposable/item.txt").write_text("test")
    result, _ = replay(tmp_path, workflow(fs_step("delete_path", path="{workspace}/disposable", recursive=True)))
    assert result.status == "completed" and not (tmp_path / "disposable").exists()
    result, _ = replay(tmp_path, workflow(fs_step("delete_path", path="{workspace}", recursive=True)))
    assert result.status == "failed" and result.completed_steps == () and tmp_path.is_dir()


def test_declined_delete_preserves_files(tmp_path):
    path = tmp_path / "preserve.txt"
    path.write_text("keep")
    result, _ = replay(tmp_path, workflow(fs_step("delete_path", path="{workspace}/preserve.txt")),
                       confirm=lambda request: request.kind == "workflow")
    assert result.status == "declined" and path.read_text() == "keep"


def test_creating_existing_file_never_overwrites_or_retries(tmp_path):
    (tmp_path / "existing.txt").write_text("original")
    result, audit = replay(tmp_path, workflow(fs_step("create_file", path="{workspace}/existing.txt", content="replacement")))
    assert result.status == "failed" and result.completed_steps == ()
    assert (tmp_path / "existing.txt").read_text() == "original"
    assert not any(event["event"] == "step_retry" for event in audit.events)


def test_late_path_escape_is_rejected_before_first_mutation(tmp_path):
    task = workflow(fs_step("create_file", path="{workspace}/first.txt"),
                    fs_step("create_file", path="{workspace}/../escape.txt"))
    result, _ = replay(tmp_path, task)
    assert result.status == "failed" and result.completed_steps == ()
    assert not (tmp_path / "first.txt").exists()


def test_links_and_junctions_are_rejected_without_following_them(tmp_path, monkeypatch):
    (tmp_path / "junction").mkdir()
    from apprentice.executor import filesystem
    monkeypatch.setattr(filesystem, "reparse", lambda path: path.name == "junction")
    with pytest.raises(ExecutionError, match="links or junctions"):
        PathPolicy([tmp_path]).path(tmp_path / "junction/escape.txt", write=True)


def test_source_changes_during_copy_fail_without_binding_output(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("before")
    executor = FilesystemExecutor([tmp_path])
    original = executor.copy_file

    async def changed(source_path, destination):
        await original(source_path, destination)
        source_path.write_text("after")

    monkeypatch.setattr(executor, "copy_file", changed)
    result, _ = replay(tmp_path, workflow(fs_step("copy_path", source="{workspace}/source.txt",
                                                destination="{workspace}/copy.txt", save_as="new_file")), filesystem=executor)
    assert result.status == "failed" and result.completed_steps == ()
    assert (tmp_path / "copy.txt").read_text() == "before" and source.read_text() == "after"


def test_stop_after_transfer_prevents_later_steps(tmp_path):
    (tmp_path / "source.bin").write_bytes(b"x" * 3_000_000)
    control = Control()
    executor = FilesystemExecutor([tmp_path], control=control)
    original = executor.copy_file

    async def stopped(source, destination):
        await original(source, destination)
        control.stop.set()

    executor.copy_file = stopped
    result, _ = replay(tmp_path, workflow(
        fs_step("copy_path", source="{workspace}/source.bin", destination="{workspace}/copy.bin"),
        fs_step("create_file", path="{workspace}/later.txt")), control=control, filesystem=executor)
    assert result.status == "stopped" and result.completed_steps == ()
    assert not (tmp_path / "later.txt").exists()


def test_open_directory_verifies_reported_shell_folder(tmp_path):
    class Explorer:
        available = True
        def open_directory(self, path):
            self.opened = path
        def directory_is_open(self, path):
            return self.opened == path
        def close(self):
            pass

    result, _ = replay(tmp_path, workflow(fs_step("open_directory", path="{workspace}")),
                       filesystem=FilesystemExecutor([tmp_path], explorer=Explorer()))
    assert result.status == "completed"


def test_successful_launch_without_shell_proof_does_not_verify(tmp_path):
    class Explorer:
        available = True
        def open_directory(self, path):
            pass
        def directory_is_open(self, path):
            return False
        def close(self):
            pass

    result, _ = replay(tmp_path, workflow(fs_step("open_directory", path="{workspace}")), control=Control(timeout_ms=80),
                       filesystem=FilesystemExecutor([tmp_path], explorer=Explorer()))
    assert result.status == "failed" and result.completed_steps == ()


def test_v2_yaml_roundtrip_and_directory_parameters(tmp_path):
    task = workflow(fs_step("create_directory", path="{workspace}/output"))
    path = save_workflow(task, tmp_path / "workflow.yaml")
    loaded = load_workflow(path, require_ready=True)
    assert isinstance(loaded, SystemWorkflow) and loaded.model_dump() == task.model_dump()
    assert bind_parameters(loaded, {"workspace": str(tmp_path)}) == {"workspace": str(tmp_path)}
    assert "create directory" in "\n".join(dry_run(loaded))
    assert not (tmp_path / "output").exists()


def test_direct_validation_script_supports_version_two_without_pythonpath(tmp_path):
    task = workflow(fs_step("create_directory", path="{workspace}/output"))
    path = save_workflow(task, tmp_path / "workflow.yaml")
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, str(root / "src/apprentice/store/workflow.py"), str(path), "--ready"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "Valid ready workflow" in result.stdout


def test_filesystem_cli_never_starts_playwright(tmp_path, monkeypatch):
    import playwright.async_api
    def forbidden():
        pytest.fail("Filesystem-only replay must not start Playwright.")
    monkeypatch.setattr(playwright.async_api, "async_playwright", forbidden)

    async def approve(*args, **kwargs):
        return "yes"
    monkeypatch.setattr(run_workflow, "prompt", approve)
    args = SimpleNamespace(timeout=10000, no_hotkey=True, run_root=tmp_path / "run", url=None, headless=True)
    task = workflow(fs_step("create_file", path="{workspace}/new.txt", content="created"))
    assert asyncio.run(run_workflow.execute(task, args, {"workspace": str(tmp_path)})) == 0
    assert (tmp_path / "new.txt").read_text() == "created"


@pytest.mark.parametrize("change", ["missing_confirmation", "wrong_check", "redefined_output", "unknown_reference", "no_roots"])
def test_v2_contract_rejects_unsafe_or_incoherent_workflows(change):
    data = workflow(fs_step("create_file", path="{workspace}/file.txt", save_as="created"),
                    fs_step("delete_path", path="{created}")).model_dump()
    if change == "missing_confirmation":
        data["confirm_before"] = []
    elif change == "wrong_check":
        data["steps"][0]["postcondition"]["path"] = "{workspace}/other.txt"
    elif change == "redefined_output":
        data["steps"][0]["save_as"] = "workspace"
    elif change == "unknown_reference":
        data["steps"][0]["path"] = "{unknown}/file.txt"
    else:
        data["write_roots"] = []
    with pytest.raises(ValueError):
        SystemWorkflow.model_validate(data)


def test_version_one_remains_browser_only():
    with pytest.raises(ValueError):
        Workflow.model_validate({"name": "v1", "description": "No filesystem in v1", "start_url": "file:///page.html",
                                 "steps": [{"id": 1, **fs_step("create_file", path="C:/sandbox/file.txt")}]})
