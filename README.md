# Apprentice

**Show it once. It remembers.** Demonstrate a task, review its workflow, and replay it.

The repo covers Phases 0–5 of [PLAN.md](PLAN.md): the local model smoke test,
mock finance portal, browser recorder, model-free labeling, verified browser replay,
unified Windows recording, filesystem replay, and reviewed application/terminal
operations. Browser workflows remain the
MVP. System recording captures Explorer navigation, application context, file changes,
opt-in PowerShell commands, verified project launches, and native interaction metadata
in one timeline. Native control and clipboard replay follow in Phase 6.

Phase 2 runs offline. It uses recorded DOM labels and changes, makes typed/selected
values parameters, links exported files to later uploads, and marks uncertain
results for human review. Passwords are requested each run and have no saved examples.

## Setup on Windows

Use Python 3.11 or later. In PowerShell, from this repo:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Serve the mock portal in one terminal:

```powershell
.\.venv\Scripts\python.exe -m http.server 8000 --directory src/apprentice/portal
```

Record in another terminal, then close the browser window to finish:

```powershell
.\.venv\Scripts\python.exe src/apprentice/recorder/record.py --url "http://localhost:8000/?v=baseline" --name my_task
```

## Build and review a workflow

```powershell
.\.venv\Scripts\python.exe src/apprentice/labeler/build_workflow.py recordings/my_task/steps.json --output workflows/my_task.yaml
.\.venv\Scripts\python.exe src/apprentice/store/workflow.py workflows/my_task.yaml
```

Read the YAML. Replace each `review_required` postcondition with an observed check,
resolve any `review_notes`, and check the parameters and confirmation steps. Then
set `status: ready` and validate:

```powershell
.\.venv\Scripts\python.exe src/apprentice/store/workflow.py workflows/my_task.yaml --ready
```

For reproducible review, pass `--review <review.yaml>` when building. The included
[monthly-report review](workflows/reviews/monthly_report.yaml) adds the missing Month
selection and confirms the sign-in and submission checks. Rebuild the included
example from its synthetic fixture:

```powershell
.\.venv\Scripts\python.exe src/apprentice/labeler/build_workflow.py tests/fixtures/monthly_report.json --review workflows/reviews/monthly_report.yaml --ready --output workflows/monthly_report.yaml --overwrite
```

The [reviewed workflow](workflows/monthly_report.yaml) has eight steps. Month is a
choice parameter with September as its example; examples are not runtime defaults.
The amount is a number parameter with 1500 as its example. The downloaded CSV supplies the upload file, and
the final check includes the selected month and amount. Upload and Submit require
confirmation before execution.

The original recording did not select Month and entered an amount different from
the CSV total. Phase 2 makes these inputs explicit; it does not extract totals from
files. See [DEVELOPMENT.md](DEVELOPMENT.md) for the contract and review format.

## Preview and execute

Preview opens no browser, asks for no passwords, and creates no run files:

```powershell
.\.venv\Scripts\python.exe src/apprentice/executor/run_workflow.py workflows/monthly_report.yaml
```

Start the local mock portal as above, then replay with explicit inputs:

```powershell
.\.venv\Scripts\python.exe src/apprentice/executor/run_workflow.py workflows/monthly_report.yaml --execute --param username=demo --param month=September --param total_amount=1500
```

The CLI prompts privately for Password, confirms the whole workflow, then asks again
before Upload and Submit. Keep passwords out of command-line arguments. Every step
must pass its postcondition before the next starts. Failed or ambiguous steps stop
for review; an uncertain submission is not automatically repeated.

Stop with **Ctrl+Alt+Esc** or **Ctrl+C**. `--no-hotkey` disables the global hotkey;
Ctrl+C remains available. `--headless` runs Chromium without a visible window.
`--timeout 10000` sets each action/check timeout in milliseconds. Confirmations wait
for an answer or cancellation. `--url "http://localhost:8000/?v=renamed"` selects a
portal variant. Targets use labels/roles/text first and recorded CSS as a fallback.

Each execution saves downloads and metadata-only `audit.jsonl` in a unique folder
under `runs/`. Passwords, typed values, DOM contents, and raw browser errors are not
written to these logs. Run artifacts remain private and gitignored.

## Teach a system task

Record a chosen workspace, with optional browser recording in the same session:

```powershell
.\.venv\Scripts\python.exe src/apprentice/recorder/record_system.py --name organize_files --root "D:\MyWorkspace"
# Add --url "http://localhost:8000/?v=baseline" to enroll semantic browser recording.
```

Enter stops and saves `recordings/organize_files/session.json`; `p` pauses, `r`
resumes, and Ctrl+C stops. Repeat `--root` for additional folders or `--exclude-app`
for sensitive applications. Native typing is recorded as activity without characters;
clipboard contents and window titles are omitted. Browser inputs retain the Phase 2
parameterization and password redaction. Recording automatically suspends during
Apprentice replay, preventing its own actions from being learned again.

Build a draft, then explicitly review the captured intent:

```powershell
.\.venv\Scripts\python.exe src/apprentice/labeler/build_system_workflow.py recordings/organize_files/session.json --output workflows/organize_files.yaml
.\.venv\Scripts\python.exe src/apprentice/store/workflow.py workflows/organize_files.yaml
```

File observations need review because a watcher cannot tell who intended a change.
App switches and unsupported gestures remain visible review items. An event review
can replace an event with a complete semantic step, or ignore it with a reason.
See [DEVELOPMENT.md](DEVELOPMENT.md) for the review YAML format and `--review --ready`
build commands. Unresolved operations cannot become a ready workflow.

Version 2 supports opening directories, creating files/folders, copying, moving,
renaming, and deleting paths. File contents and source/destination state verify
transfers; opening a directory requires Explorer's actual folder report. Writes stay
within declared workspace roots, destinations must be new, and deletion requires
confirmation. Links, junctions, other reparse points, and Windows device paths require
review. A stopped or failed transfer can leave partial files; inspect its state
before rerunning. Version 1 browser workflows and their recorder commands still work.

The reviewed [file organizer](workflows/file_organizer.yaml) is ready to preview:

```powershell
.\.venv\Scripts\python.exe src/apprentice/executor/run_workflow.py workflows/file_organizer.yaml
.\.venv\Scripts\python.exe src/apprentice/executor/run_workflow.py workflows/file_organizer.yaml --execute --param "workspace=D:\MyWorkspace" --param archive_name=Archive --param source_name=report.csv --param destination_name=reviewed-report.csv
```

Use a sandbox containing `report.csv` and a new archive folder name. Filesystem-only
replay starts no browser; mixed workflows open Chromium when the first browser step
needs it. Parameters and verified file outputs work across surfaces, including a
browser download moved into a workspace and then uploaded.

The [coverage catalog](src/apprentice/adapters/capabilities.py) tracks operation
families beyond these examples: apps/projects, shell commands and services, native
menus/dialogs, shortcuts, dragging, scrolling, clipboard, settings/devices, and
unrecognized operations. Capture/review coverage is broader than executable replay;
the remaining adapters are explicit roadmap work.

## Teach projects and terminal commands

Enroll one PowerShell session with the provided helper. Use a new event file under
the ignored recording directory:

```powershell
. .\scripts\ApprenticeShell.ps1
Enable-ApprenticeRecording -Path .\recordings\project-shell.jsonl
```

In another terminal, start the system recorder with the same event file, then
demonstrate opening a project and running its tests in the enrolled shell:

```powershell
# Recorder terminal:
.\.venv\Scripts\python.exe src/apprentice/recorder/record_system.py --name project_tests --root "D:\MyProject" --shell-events .\recordings\project-shell.jsonl

# Enrolled shell:
Invoke-ApprenticeProject -Path "D:\MyProject"
Set-Location -LiteralPath "D:\MyProject"
Invoke-ApprenticeCommand -Executable "D:\Apprentice\.venv\Scripts\python.exe" -ArgumentList @('-m', 'pytest', '-q')
Disable-ApprenticeRecording
```

If local script execution is blocked, use
`Set-ExecutionPolicy -Scope Process Bypass` in that shell before dot-sourcing.
No PowerShell profile or persistent policy change is needed. Optional
`Enable-ApprenticeRecording -Path ... -InteractiveHook` also captures newly submitted
PSReadLine commands. Explicit command helpers preserve the actual expanded argument
values; scripts, pipelines, session-dependent commands, and uncertain submissions
need review. Password prompts, existing command history, and terminal output are
omitted. Use `-SensitiveArgument @(index)` or `-Sensitive` for secret arguments whose
meaning cannot be identified from credential flags; redacted inputs become fresh
secret parameters when reviewed.

Build/review `recordings/project_tests/session.json` with the system labeler above.
The ready [project-and-test workflow](workflows/project_tests.yaml) can also be previewed
and run directly:

```powershell
.\.venv\Scripts\python.exe src/apprentice/executor/run_workflow.py workflows/project_tests.yaml
.\.venv\Scripts\python.exe src/apprentice/executor/run_workflow.py workflows/project_tests.yaml --execute --param "project_dir=D:\MyProject" --param "python_executable=D:\Apprentice\.venv\Scripts\python.exe"
```

VS Code project replay uses an isolated profile and verifies its exact workspace
through the editor API. Every application or terminal step requires confirmation.
Commands preserve argv and working directory, verify exit status or an owned local
HTTP service, and can bind fresh file artifacts for later browser uploads. Commands
are never retried automatically; their output and environment values stay out of
audit logs. Stop/timeout cleans up process trees started by the run. Services last
only for that workflow; successfully opened GUI applications remain available.
Reviewed workspace roots constrain file adapters, command working directories, and
verified artifact paths; commands themselves are ordinary local programs and can
write elsewhere. Recorded commands outside watched roots or with nonzero exits need
explicit review. Application executables must be available before the run; creating
an executable and then launching it as an application step needs a further adapter.

## Tests and privacy

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic recordings and local HTML in headless Chromium. They need no
model, API key, microphone, or external server. The Phase 0 screen/model smoke test
runs only when invoked directly with `python tests/test_setup.py`.

Real Windows acceptance scripts preview by default:

```powershell
.\.venv\Scripts\python.exe scripts/smoke-system.py
.\.venv\Scripts\python.exe scripts/smoke-project.py
```

Add `--execute` to run them. The Phase 4 script captures/replays directory and file
operations; Phase 5 captures Explorer navigation, proves the VS Code workspace,
records a real PowerShell test command, and replays the reviewed workflow against
a different project. Both create fresh sandboxes under `runs/` and save private
evidence/results. Phase 5 closes only its own VS Code processes. Explorer sandbox
tabs remain open.

`.env`, `recordings/`, and `runs/` are gitignored. Recordings include screenshots and ordinary
typed values; keep them private. Password controls and fields marked `data-sensitive`
or `data-apprentice-sensitive` are redacted in events and masked in screenshots.
