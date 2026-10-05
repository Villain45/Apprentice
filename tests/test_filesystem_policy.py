"""Reject Windows device paths before performing filesystem I/O."""

import os
from pathlib import Path

import pytest

from apprentice.executor import filesystem
from apprentice.executor.filesystem import PathPolicy
from apprentice.executor.runtime import ExecutionError


@pytest.mark.skipif(os.name != "nt", reason="Windows device-name semantics")
@pytest.mark.parametrize("component", [
    "NUL", "nul.csv", "CON", "con.tar.gz", "CONIN$", "CONOUT$", "CONIN$.txt",
    "PRN", "AUX", "COM1", "com9.txt",
    "LPT1", "lpt9.csv", "COM\u00b9", "COM\u00b2.txt", "LPT\u00b3.csv",
    "NUL .txt", "COM1 .csv", "LPT9   .log",
    "report.txt:secret", "report.txt:", "trailing-dot.", "trailing-space ",
    "has*wildcard", "has?wildcard", "has<angle", 'has"quote', "has|pipe", "has\x01control",
])
def test_windows_special_components_are_rejected_without_opening_them(tmp_path, component):
    # Test both final names and intermediate directories: devices are reserved everywhere.
    for candidate in (tmp_path / component, tmp_path / component / "ordinary-file.txt"):
        with pytest.raises(ExecutionError):
            PathPolicy.absolute(candidate)


@pytest.mark.skipif(os.name != "nt", reason="Windows device-namespace semantics")
@pytest.mark.parametrize("candidate", [
    r"\\.\PhysicalDrive0", r"\\.\COM56", r"\\?\GLOBALROOT\Device\HarddiskVolume1\Windows",
    r"\\?\D:\workspace\ordinary-file.txt", r"\\?\UNC\server\share\file.txt",
])
def test_windows_device_namespaces_are_rejected_without_inspection(candidate):
    with pytest.raises(ExecutionError):
        PathPolicy.absolute(candidate)


@pytest.mark.parametrize("name", ["workspace with spaces", ".settings", "report.con.csv", "COM10", "NULL.txt"])
def test_ordinary_names_remain_supported(tmp_path, name):
    assert PathPolicy.absolute(tmp_path / name) == tmp_path / name


def test_parent_components_are_normalized_before_checking_workspace_boundaries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    policy = PathPolicy([root])
    assert policy.path(root / "nested" / ".." / "report.csv", write=True) == root / "report.csv"
    with pytest.raises(ExecutionError):
        policy.path(root / ".." / "outside.csv", write=True)


def test_workspace_root_and_entire_drive_are_not_mutation_targets(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    policy = PathPolicy([root])
    assert policy.path(root) == root
    with pytest.raises(ExecutionError):
        policy.path(root, write=True)
    with pytest.raises(ExecutionError):
        PathPolicy([Path(tmp_path.anchor)])


def test_reparse_ancestor_is_rejected_without_creating_a_windows_link(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    policy = PathPolicy([root])
    monkeypatch.setattr(filesystem, "reparse", lambda path: path == root)
    with pytest.raises(ExecutionError):
        policy.path(root / "child" / "file.txt", write=True)


def test_artifact_reads_are_explicit_and_do_not_grant_destination_write_access(tmp_path):
    root, artifacts = tmp_path / "workspace", tmp_path / "run"
    root.mkdir()
    artifacts.mkdir()
    policy = PathPolicy([root], artifact_root=artifacts)
    downloaded = artifacts / "downloads" / "report.csv"
    assert policy.path(downloaded, artifact=True) == downloaded
    with pytest.raises(ExecutionError):
        policy.path(downloaded)
    with pytest.raises(ExecutionError):
        policy.path(downloaded, write=True)
