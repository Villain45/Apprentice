"""Explicit shell enrollment, exact argv, completion and recording privacy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from apprentice.recorder.session import REDACTED, RecordingSession
from apprentice.recorder.terminal import TerminalCapture, sanitize_command


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "ApprenticeShell.ps1"
SHELLS = [path for name in ("powershell", "pwsh") if (path := shutil.which(name))]


@pytest.fixture(autouse=True)
def isolated_replay_markers(tmp_path, monkeypatch):
    from apprentice.recorder import replay_guard
    monkeypatch.setattr(replay_guard, "MARKER_ROOT", tmp_path / ".active-replays")


def command(**overrides):
    return {"command_id": "command_one", "mode": "exec", "executable": "python.exe",
            "args": ["-m", "pytest", "-q"], "cwd": str(ROOT), **overrides}


def write_event(path, kind, payload, *, surface="terminal", **metadata):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"version": 1, "surface": surface, "kind": kind,
                                "payload": payload, **metadata}) + "\n")


def events(session):
    return session.snapshot()["events"]


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_shell(tmp_path, shell, body):
    runner = tmp_path / "run capture.ps1"
    runner.write_text("$ErrorActionPreference = 'Stop'\n. " + ps_literal(HOOK) + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result


def test_credential_args_redacted_preserving_binding_structure():
    raw = command(args=["--password", "secret_password", "--token=secret_token", "AUTH_TOKEN=secret_env",
                        "Authorization: Bearer secret_header", "https://user:secret_uri@example.test/path",
                        "normal", "opaque_private"], sensitive_arguments=[7])
    clean = sanitize_command(raw)
    encoded = json.dumps(clean)
    for secret in ("secret_password", "secret_token", "secret_env", "secret_header", "secret_uri", "opaque_private"):
        assert secret not in encoded
    assert clean["args"] == ["--password", REDACTED, "--token=" + REDACTED, "AUTH_TOKEN=" + REDACTED,
                            REDACTED, REDACTED, "normal", REDACTED]
    assert clean["secret_bindings"][1] == {"field": "args", "index": 2, "prefix": "--token="}
    assert clean["requires_review"] and clean["redacted"]


def test_script_text_private_by_default_and_nonsecret_exact_opt_in():
    script = "Get-ChildItem | Sort-Object Name; Write-Output 'two words'"
    private = sanitize_command(command(mode="powershell", script=script))
    assert private["script"] == REDACTED
    assert private["secret_bindings"] == [{"field": "script"}]
    reviewed = sanitize_command(command(mode="powershell", script=script, script_text_reviewed=True))
    assert reviewed["script"] == script
    assert reviewed["requires_review"]
    secret = sanitize_command(command(mode="powershell", script="$env:API_KEY='unknown_private'", script_text_reviewed=True))
    assert "unknown_private" not in json.dumps(secret)
    assert secret["script"] == REDACTED


def test_complete_line_polling_order_context_and_no_stdout(tmp_path):
    spool = tmp_path / "shell.jsonl"
    session = RecordingSession("Shell", roots=[tmp_path])
    capture = TerminalCapture(session, spool)
    assert capture.poll_once() == 0
    write_event(spool, "command_started", command(), context={"app": "pwsh", "pid": 22, "command_line": "private"})
    with spool.open("ab") as stream:
        stream.write(b'{"version":1,"surface":"terminal","kind":"command_completed","payload":{"command_id":"command_one",')
    assert capture.poll_once() == 1
    assert len(events(session)) == 1
    with spool.open("ab") as stream:
        stream.write(b'"exit_code":3,"stdout":"private-output","stderr":"private-error"}}\n')
    assert capture.poll_once() == 1
    assert capture.poll_once() == 0
    assert [event["kind"] for event in events(session)] == ["command_started", "command_completed"]
    assert events(session)[1]["payload"] == {"command_id": "command_one", "exit_code": 3}
    assert "private" not in json.dumps(session.snapshot())


def test_pause_and_replay_exclusion_cannot_create_orphan_completion(tmp_path):
    spool = tmp_path / "shell.jsonl"
    session = RecordingSession("Shell")
    capture = TerminalCapture(session, spool)
    session.pause()
    write_event(spool, "command_started", command())
    capture.poll_once()
    session.resume()
    write_event(spool, "command_completed", {"command_id": "command_one", "exit_code": 0})
    capture.poll_once()
    assert events(session) == []
    write_event(spool, "command_started", command(command_id="two"))
    capture.poll_once()
    session.pause()
    session.resume()
    write_event(spool, "command_completed", {"command_id": "two", "exit_code": 0})
    capture.poll_once()
    assert len(events(session)) == 1


def test_only_declared_effect_paths_correlate(tmp_path):
    spool = tmp_path / "shell.jsonl"
    session = RecordingSession("Shell")
    capture = TerminalCapture(session, spool)
    known = tmp_path / "report.csv"
    write_event(spool, "command_started", command(effect_paths=[str(known)]))
    capture.poll_once()
    expected = session.emit("filesystem", "created", {"path": str(known)}, origin="observation")
    unrelated = session.emit("filesystem", "created", {"path": str(tmp_path / "background.txt")}, origin="observation")
    assert expected["payload"]["correlated_to"] == 1
    assert "correlated_to" not in unrelated["payload"]
    write_event(spool, "command_completed", {"command_id": "command_one", "exit_code": 0})
    capture.poll_once()
    later = session.emit("filesystem", "modified", {"path": str(known)}, origin="observation")
    assert "correlated_to" not in later["payload"]


def test_invalid_spool_data_static_review_without_plaintext(tmp_path):
    spool = tmp_path / "shell.jsonl"
    spool.write_text("not-json-private\n", encoding="utf-8")
    write_event(spool, "command_started", command(args=["--token", "unsafe_private"]))
    write_event(spool, "command_started", command(args=["duplicate_private"]))
    write_event(spool, "command_completed", {"command_id": "command_one", "exit_code": "invalid_private"})
    session = RecordingSession("Shell")
    capture = TerminalCapture(session, spool)
    capture.poll_once()
    capture.stop()
    encoded = json.dumps(session.snapshot())
    assert "private" not in encoded
    assert sum(event["kind"] == "review_required" for event in events(session)) == 1
    assert events(session)[-1]["kind"] == "command_incomplete"


def test_verified_application_spool_forwarding_and_untrusted_fields(tmp_path):
    spool = tmp_path / "shell.jsonl"
    write_event(spool, "open_project", {"app": "vscode", "executable": "Code.exe", "args": [],
                "path": str(tmp_path), "cwd": str(tmp_path), "verified": True, "clipboard": "private-clipboard"},
                surface="application", evidence=[{"verification": "vscode_workspace_api", "kind": "project_open",
                        "process_id": 5, "path": str(tmp_path), "raw_keys": "private-keys"}])
    session = RecordingSession("Project")
    TerminalCapture(session, spool).poll_once()
    event = events(session)[0]
    assert event["surface"] == "application" and event["payload"]["verified"]
    assert event["evidence"][0]["path"] == str(tmp_path)
    assert "private" not in json.dumps(event)


def test_canonical_sensitive_application_spool_retains_review_action(tmp_path):
    spool = tmp_path / "shell.jsonl"
    write_event(spool, "launch_application", {"app": "private-client", "executable": "client.exe",
                "args": REDACTED, "sensitive": True, "verified": True}, surface="application")
    session = RecordingSession("Application")
    TerminalCapture(session, spool).poll_once()
    event = events(session)[0]
    assert event["surface"] == "application" and event["kind"] == "launch_application"
    assert event["payload"]["args"] == [REDACTED]
    assert event["payload"]["requires_review"]
    assert event["payload"]["secret_bindings"] == [{"field": "args", "index": 0}]


def test_application_inline_credentials_redacted_before_recording_memory(tmp_path):
    spool = tmp_path / "shell.jsonl"
    write_event(spool, "open_project", {"app": "vscode", "args": ["--token", "private_value"],
                "path": str(tmp_path), "verified": True}, surface="application")
    session = RecordingSession("Project")
    TerminalCapture(session, spool).poll_once()
    event = events(session)[0]
    assert event["payload"]["args"] == ["--token", REDACTED]
    assert "private_value" not in json.dumps(session.snapshot())


@pytest.mark.skipif(os.name != "nt", reason="The shell helper targets Windows.")
@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_preserves_exact_native_argv_and_exit_codes(tmp_path, shell):
    spool = tmp_path / "shell.jsonl"
    received = tmp_path / "argv.json"
    helper = tmp_path / "argument probe.py"
    helper.write_text("import json,sys,pathlib\npathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n", encoding="utf-8")
    arguments = [str(helper), str(received), "two words", 'embedded"quote', "", "trailing\\", "\\\\server\\space dir\\", "x'y", "plain"]
    array = "@(" + ",".join(ps_literal(argument) for argument in arguments) + ")"
    run_shell(tmp_path, shell, "Enable-ApprenticeRecording -Path " + ps_literal(spool) + "\n"
              "Invoke-ApprenticeCommand -Executable " + ps_literal(sys.executable) + " -ArgumentList " + array + "\n"
              "Invoke-ApprenticeCommand -Executable " + ps_literal(sys.executable) + " -ArgumentList @('-c','import sys;sys.exit(7)')\n"
              "Disable-ApprenticeRecording\n")
    assert json.loads(received.read_text()) == arguments[2:]
    envelopes = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines()]
    assert [envelope["kind"] for envelope in envelopes] == ["command_started", "command_completed"] * 2
    assert envelopes[0]["payload"]["args"] == arguments
    assert envelopes[0]["payload"]["cwd"] == str(tmp_path)
    assert [envelopes[index]["payload"]["exit_code"] for index in (1, 3)] == [0, 7]


@pytest.mark.skipif(os.name != "nt", reason="The shell helper targets Windows.")
@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_relative_paths_follow_actual_location(tmp_path, shell):
    project = tmp_path / "project with spaces"
    project.mkdir()
    received = project / "cwd.json"
    helper = tmp_path / "cwd probe.py"
    helper.write_text("import json,os,pathlib\npathlib.Path('cwd.json').write_text(json.dumps(os.getcwd()))\n",
                      encoding="utf-8")
    run_shell(tmp_path, shell, "Set-Location -LiteralPath " + ps_literal(project) + "\n"
              "Enable-ApprenticeRecording -Path '.\\shell.jsonl'\n"
              "Invoke-ApprenticeCommand -Executable " + ps_literal(sys.executable) +
              " -ArgumentList @(" + ps_literal(helper) + ") -EffectPaths @('.\\cwd.json')\n"
              "Invoke-ApprenticeScript -Script \"Write-Output 'public' | Out-Null\" -RecordScriptText -EffectPaths @('.\\script-effect.txt')\n"
              "Disable-ApprenticeRecording\n")
    assert json.loads(received.read_text()) == str(project)
    assert not (tmp_path / "shell.jsonl").exists()
    envelopes = [json.loads(line) for line in (project / "shell.jsonl").read_text().splitlines()]
    assert envelopes[0]["payload"]["cwd"] == str(project)
    assert envelopes[0]["payload"]["effect_paths"] == [str(received)]
    assert envelopes[2]["payload"]["effect_paths"] == [str(project / "script-effect.txt")]


@pytest.mark.skipif(os.name != "nt", reason="The shell helper targets Windows.")
@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_async_completion_is_paired_once(tmp_path, shell):
    spool = tmp_path / "shell.jsonl"
    run_shell(tmp_path, shell, "Enable-ApprenticeRecording -Path " + ps_literal(spool) + "\n"
              "$submitted = Invoke-ApprenticeCommand -Executable " + ps_literal(sys.executable) +
              " -ArgumentList @('-c','import time;time.sleep(0.05)') -NoWait\n"
              "$submitted.Process.WaitForExit()\n"
              "Sync-ApprenticeCommands\nSync-ApprenticeCommands\nDisable-ApprenticeRecording\n")
    envelopes = [json.loads(line) for line in spool.read_text().splitlines()]
    assert [event["kind"] for event in envelopes] == ["command_started", "command_ongoing", "command_completed"]
    assert len({event["payload"]["command_id"] for event in envelopes}) == 1
    assert envelopes[-1]["payload"]["exit_code"] == 0
    session = RecordingSession("Background command")
    TerminalCapture(session, spool).poll_once()
    assert [event["kind"] for event in events(session)] == ["command_started", "command_ongoing", "command_completed"]


@pytest.mark.skipif(os.name != "nt", reason="The shell helper targets Windows.")
@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_redacts_before_spool_and_never_captures_output(tmp_path, shell):
    spool = tmp_path / "shell.jsonl"
    run_shell(tmp_path, shell, "Enable-ApprenticeRecording -Path " + ps_literal(spool) + "\n"
              "Invoke-ApprenticeCommand -Executable " + ps_literal(sys.executable) + " -ArgumentList @('-c','print(123)','--password','private_flag','--token=private_inline','private_explicit') -SensitiveArgument @(5)\n"
              "Invoke-ApprenticeScript -Script \"Write-Output 'private_script'\"\n"
              "Invoke-ApprenticeScript -Script \"Write-Output 'public' | Out-Null\" -RecordScriptText\n"
              "Invoke-ApprenticeScript -Script \"Write-Output 'API_KEY=private_forced' | Out-Null\" -RecordScriptText\n"
              "Disable-ApprenticeRecording\n")
    saved = spool.read_text(encoding="utf-8")
    assert "private" not in saved
    assert any(json.loads(line)["payload"].get("script") == "Write-Output 'public' | Out-Null"
               for line in saved.splitlines())
    session = RecordingSession("Private shell")
    capture = TerminalCapture(session, spool)
    assert capture.poll_once() == 8
    assert "private" not in json.dumps(session.snapshot())


@pytest.mark.skipif(os.name != "nt", reason="The shell helper targets Windows.")
@pytest.mark.parametrize("shell", SHELLS)
def test_real_interactive_ast_and_hook_restore_without_executing_input(tmp_path, shell):
    spool = tmp_path / "shell.jsonl"
    checks = tmp_path / "hook-checks.json"
    marker = tmp_path / "must-not-exist.txt"
    body = """Import-Module PSReadLine
$before = (Get-PSReadLineOption).AddToHistoryHandler
$oldPrompt = (Get-Item Function:\\prompt).ScriptBlock.ToString()
Enable-ApprenticeRecording -Path SPOOL -InteractiveHook -RecordScriptText
$handler = (Get-PSReadLineOption).AddToHistoryHandler
$literal = Get-ApprenticeInteractiveSubmission -Line 'python -m pytest -q'
$dynamic = Get-ApprenticeInteractiveSubmission -Line 'python $opaqueSecret'
$pipeline = Get-ApprenticeInteractiveSubmission -Line 'Get-ChildItem | Sort-Object Name' -RecordScriptText
$stopParsing = Get-ApprenticeInteractiveSubmission -Line 'python --% plain "two words" %USERNAME%'
$tilde = Get-ApprenticeInteractiveSubmission -Line 'python ~\\workspace'
$literalStop = Get-ApprenticeInteractiveSubmission -Line "python '--%'"
$skipped = Get-ApprenticeInteractiveSubmission -Line 'Invoke-ApprenticeCommand python'
$decision = $handler.Invoke('python --token private_interactive')
Complete-ApprenticeInteractiveSubmission -Succeeded $true -NativeExitCode 4
$unused = $handler.Invoke("New-Item -LiteralPath 'MARKER' | Out-Null")
Complete-ApprenticeInteractiveSubmission -Succeeded $true -NativeExitCode 0
Disable-ApprenticeRecording
$after = (Get-PSReadLineOption).AddToHistoryHandler
$restoredPrompt = (Get-Item Function:\\prompt).ScriptBlock.ToString()
@{literal_mode=$literal.mode; literal_args=$literal.args; dynamic_script=$dynamic.script;
  pipeline_script=$pipeline.script; helper_skipped=($null -eq $skipped); history_withheld=($decision -eq $false);
  stop_parsing_script=$stopParsing.script; tilde_script=$tilde.script; literal_stop_args=$literalStop.args;
  restored_handler=([object]::ReferenceEquals($before,$after)); restored_prompt=($oldPrompt -eq $restoredPrompt)} |
  ConvertTo-Json -Depth 6 | Set-Content -LiteralPath CHECKS
""".replace("SPOOL", ps_literal(spool)).replace("MARKER", str(marker).replace("'", "''")).replace("CHECKS", ps_literal(checks))
    run_shell(tmp_path, shell, body)
    result = json.loads(checks.read_text(encoding="utf-8-sig"))
    assert result["literal_mode"] == "exec" and result["literal_args"] == ["-m", "pytest", "-q"]
    assert result["dynamic_script"] == REDACTED
    assert result["pipeline_script"] == "Get-ChildItem | Sort-Object Name"
    assert result["stop_parsing_script"] == REDACTED and result["tilde_script"] == REDACTED
    assert result["literal_stop_args"] == ["--%"]
    assert result["helper_skipped"] and result["history_withheld"]
    assert result["restored_handler"] and result["restored_prompt"]
    assert not marker.exists()
    encoded = spool.read_text(encoding="utf-8")
    assert "private_interactive" not in encoded
    envelopes = [json.loads(line) for line in encoded.splitlines()]
    assert envelopes[1]["payload"]["exit_code"] == 4
