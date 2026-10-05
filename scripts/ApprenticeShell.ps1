<#
.SYNOPSIS
Opt one PowerShell session into explicit, privacy-aware command teaching.
.DESCRIPTION
Dot-source this file, then Enable-ApprenticeRecording -Path a-new-private.jsonl.
Invoke-ApprenticeCommand / Invoke-ApprenticeScript / Invoke-ApprenticeProject
submissions are captured. Optional -InteractiveHook also observes newly submitted
PSReadLine commands without executing them again. No profile, existing history, transcript, console
output, clipboard, or password input is read. Disable-ApprenticeRecording ends
enrollment. The event file must be new and belongs in an ignored recording dir.

Native executable arguments use ProcessStartInfo and Windows CRT quoting on
Windows PowerShell 5.1, or ArgumentList on PowerShell 7. This preserves empty
arguments, embedded quotes, spaces and trailing backslashes without a shell.
Batch files and PowerShell pipelines require the reviewed script helper.

Examples:
  . .\scripts\ApprenticeShell.ps1
  Enable-ApprenticeRecording -Path .\recordings\demo-shell.jsonl
  Invoke-ApprenticeCommand -Executable python -ArgumentList @('-m','pytest','-q')
  Invoke-ApprenticeCommand -Executable client.exe -ArgumentList @('--token',$secret)
  Invoke-ApprenticeScript -Script 'Get-ChildItem | Sort-Object Name' -RecordScriptText
  Disable-ApprenticeRecording

Arguments following credential flags and credential assignments, headers, URIs,
and recognizable token forms are redacted before JSON serialization. Use
-SensitiveArgument @(index) for other secret arguments, or -Sensitive for all
arguments. Script text is fully redacted unless -RecordScriptText explicitly
marks it as reviewed nonsecret text; recognizable secrets force redaction even
with that switch. Native password prompts are inherited and never recorded.
#>

$script:ApprenticeEventFile = $null
$script:ApprenticeRepoRoot = Split-Path -Parent $PSScriptRoot
$script:ApprenticePendingCommands = @{}
$script:ApprenticeInteractiveEnabled = $false
$script:ApprenticeInteractivePending = $null
$script:ApprenticeOriginalHistoryHandler = $null
$script:ApprenticeOriginalPrompt = $null
$script:ApprenticeInteractiveRecordScriptText = $false
$script:ApprenticeCredentialName = '(?i)(password|passwd|pwd|passphrase|secret|(?:access|refresh|auth|api)[_-]?token|token|api[_-]?key|authorization|cookie|credential|client[_-]?secret|private[_-]?key)'
$script:ApprenticeCredentialValue = '(?i)(\bBearer\s+\S+|\bBasic\s+[A-Za-z0-9+/=]+|\b(?:sk|pk|ghp|gho|github_pat)[_-][A-Za-z0-9_-]{8,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[a-z][a-z0-9+.-]*://[^/\s]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----)'

function Enable-ApprenticeRecording {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Path, [switch]$InteractiveHook, [switch]$RecordScriptText)
    if ($script:ApprenticeEventFile) { throw 'This shell is already enrolled in recording.' }
    if ((Get-Location).Provider.Name -ne 'FileSystem') { throw 'Use a filesystem working directory for shell recording.' }
    $destination = Resolve-ApprenticeFilesystemPath -Path $Path
    $parent = [IO.Path]::GetDirectoryName($destination)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    # CreateNew prevents accidentally adding this session to an old recording.
    $file = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    $file.Dispose()
    $script:ApprenticeEventFile = $destination
    $script:ApprenticePendingCommands = @{}
    if ($InteractiveHook) {
        try { Enable-ApprenticeInteractiveHook -RecordScriptText:$RecordScriptText }
        catch {
            $script:ApprenticeEventFile = $null
            throw 'Interactive enrollment needs PSReadLine with AddToHistoryHandler; explicit command helpers remain available.'
        }
    }
}

function Get-ApprenticeInteractiveSubmission {
    param([Parameter(Mandatory=$true)][string]$Line, [switch]$RecordScriptText)
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($Line, [ref]$tokens, [ref]$parseErrors)
    $commands = @($ast.FindAll({param($node) $node -is [Management.Automation.Language.CommandAst]}, $true))
    # Helpers produce their own stronger evidence. Avoid a second terminal step,
    # including for assignments or pipes around an explicit helper call.
    foreach ($command in $commands) {
        if ($command.GetCommandName() -match '(?i)^(?:Invoke|Enable|Disable|Sync|Write|Get|Complete)-Apprentice') { return $null }
    }
    $commandId = [Guid]::NewGuid().ToString('N')
    $cwd = Get-ApprenticeWorkingDirectory
    $simple = -not $parseErrors -and $ast.EndBlock.Statements.Count -eq 1 -and
              $ast.EndBlock.Statements[0] -is [Management.Automation.Language.PipelineAst] -and
              $ast.EndBlock.Statements[0].PipelineElements.Count -eq 1 -and $commands.Count -eq 1
    $arguments = [Collections.Generic.List[string]]::new()
    $resolved = $null
    if ($simple) {
        $command = $commands[0]
        $simple = $command.Redirections.Count -eq 0 -and $command.InvocationOperator -ne 'Dot'
        if ($simple) {
            try {
                $found = Get-Command -Name $command.GetCommandName() -ErrorAction Stop | Select-Object -First 1
                if ($found.CommandType -eq 'Application') { $resolved = Resolve-ApprenticeExecutable -Executable $found.Source }
                else { $simple = $false }
            } catch { $simple = $false }
        }
        if ($simple) {
            for ($index = 1; $index -lt $command.CommandElements.Count; $index++) {
                $element = $command.CommandElements[$index]
                if ($element.Extent.Text -eq '--%') { $simple = $false; break }
                if ($element -is [Management.Automation.Language.StringConstantExpressionAst]) {
                    # Stop-parsing tails and native tilde expansion depend on
                    # shell syntax/environment, not a portable literal argv.
                    if ($element.StringConstantType -eq 'BareWord' -and $element.Value.StartsWith('~')) {
                        $simple = $false; break
                    }
                    $arguments.Add($element.Value)
                }
                elseif ($element -is [Management.Automation.Language.ConstantExpressionAst]) {
                    $arguments.Add([Convert]::ToString($element.Value, [Globalization.CultureInfo]::InvariantCulture))
                } elseif ($element -is [Management.Automation.Language.CommandParameterAst] -and -not $element.Argument) {
                    $arguments.Add($element.Extent.Text)
                } else { $simple = $false; break }
            }
            # Old PowerShell native quoting can alter empty/quoted arguments.
            # Preserve those cases as reviewed original shell syntax instead.
            $modern = $PSVersionTable.PSVersion -ge [Version]'7.3' -and $PSNativeCommandArgumentPassing -ne 'Legacy'
            if ($PSNativeCommandArgumentPassing -eq 'Windows' -and
                [IO.Path]::GetFileName($resolved) -in @('cmd.exe', 'cscript.exe', 'wscript.exe')) { $modern = $false }
            if (-not $modern -and @($arguments | Where-Object { $_ -eq '' -or $_.Contains('"') }).Count -gt 0) { $simple = $false }
        }
    }
    if ($simple) {
        $payload = Get-ApprenticeRedactedArguments -Arguments $arguments.ToArray()
        $payload.command_id = $commandId
        $payload.mode = 'exec'
        $payload.executable = $resolved
        $payload.cwd = $cwd
        $payload.interactive = $true
        return $payload
    }
    $redacted = -not $RecordScriptText -or $Line -match $script:ApprenticeCredentialName -or
                $Line -match $script:ApprenticeCredentialValue -or $Line -match '(?i)(encodedcommand|frombase64string|securestring)'
    $saved = if ($redacted) { '<redacted>' } else { $Line }
    $bindings = if ($redacted) { @(@{field='script'}) } else { @() }
    return @{command_id=$commandId; mode='powershell'; cwd=$cwd; script=$saved; interactive=$true;
             executable=[Diagnostics.Process]::GetCurrentProcess().MainModule.FileName;
             script_text_reviewed=([bool]$RecordScriptText -and -not $redacted); secret_bindings=$bindings;
             redacted=$redacted; requires_review=$true}
}

function Complete-ApprenticeInteractiveSubmission {
    param([bool]$Succeeded, [int]$NativeExitCode)
    if (-not $script:ApprenticeInteractivePending -or -not $script:ApprenticeEventFile) { return }
    $commandId = $script:ApprenticeInteractivePending
    $script:ApprenticeInteractivePending = $null
    $exitCode = if ($NativeExitCode -ne 0) { $NativeExitCode } elseif ($Succeeded) { 0 } else { 1 }
    Write-ApprenticeEvent -Kind 'command_completed' -Payload @{command_id=$commandId; exit_code=$exitCode; interactive=$true}
}

function Enable-ApprenticeInteractiveHook {
    param([switch]$RecordScriptText)
    Import-Module PSReadLine -ErrorAction Stop
    $options = Get-PSReadLineOption -ErrorAction Stop
    if (-not $options.PSObject.Properties['AddToHistoryHandler']) { throw 'PSReadLine submitted-command hooks are unavailable.' }
    $script:ApprenticeOriginalHistoryHandler = $options.AddToHistoryHandler
    $originalPrompt = Get-Item -LiteralPath Function:\prompt -ErrorAction SilentlyContinue
    $script:ApprenticeOriginalPrompt = if ($originalPrompt) { $originalPrompt.ScriptBlock } else { $null }
    $script:ApprenticeInteractiveRecordScriptText = [bool]$RecordScriptText
    Set-PSReadLineOption -AddToHistoryHandler {
        param([string]$Line)
        try {
            $payload = Get-ApprenticeInteractiveSubmission -Line $Line -RecordScriptText:$script:ApprenticeInteractiveRecordScriptText
            if ($payload) {
                Write-ApprenticeEvent -Kind 'command_started' -Payload $payload
                $script:ApprenticeInteractivePending = $payload.command_id
                $global:LASTEXITCODE = 0
            }
        } catch {
            # Fail closed: never include input or an exception containing input.
            if ($script:ApprenticeEventFile) {
                Write-ApprenticeEvent -Kind 'review_required' -Payload @{reason='A submitted PowerShell command needs manual review.'}
            }
        }
        # Newly submitted lines are withheld from PSReadLine history while
        # enrolled, so an inline password cannot leak to its memory/history file.
        # Existing history and original history policy are never read or modified.
        return $false
    }
    function global:prompt {
        $apprenticeSucceeded = $?
        $apprenticeNativeExit = [int]$global:LASTEXITCODE
        Complete-ApprenticeInteractiveSubmission -Succeeded $apprenticeSucceeded -NativeExitCode $apprenticeNativeExit
        Sync-ApprenticeCommands
        if ($script:ApprenticeOriginalPrompt) { & $script:ApprenticeOriginalPrompt }
        else { 'PS ' + (Get-Location) + '> ' }
    }
    $script:ApprenticeInteractiveEnabled = $true
}

function Write-ApprenticeEvent {
    [CmdletBinding()]
    param([string]$Surface = 'terminal', [Parameter(Mandatory=$true)][string]$Kind,
          [Parameter(Mandatory=$true)][hashtable]$Payload)
    if (-not $script:ApprenticeEventFile) { throw 'Enable-ApprenticeRecording in this shell first.' }
    $processName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh' } else { 'powershell' }
    $envelope = @{version=1; surface=$Surface; kind=$Kind; payload=$Payload;
                  recorded_at=[DateTime]::UtcNow.ToString('o'); context=@{app=$processName; pid=$PID}}
    $json = ($envelope | ConvertTo-Json -Depth 12 -Compress) + "`n"
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
    $file = [IO.File]::Open($script:ApprenticeEventFile, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    try { $file.Write($bytes, 0, $bytes.Length); $file.Flush($true) }
    finally { $file.Dispose() }
}

function Get-ApprenticeRedactedArguments {
    param([AllowEmptyCollection()][AllowEmptyString()][string[]]$Arguments = @(),
          [int[]]$SensitiveArgument = @(), [switch]$Sensitive)
    $safe = [Collections.Generic.List[string]]::new()
    $bindings = [Collections.Generic.List[object]]::new()
    $nextSensitive = $false
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        $value = $argument
        $binding = $null
        $prefix = $null
        if ($Sensitive -or $nextSensitive -or $SensitiveArgument -contains $index) {
            $value = '<redacted>'
            $binding = @{field='args'; index=$index}
        } elseif ($argument -match '^([^=\s]+)=(.*)$' -and $Matches[1] -match $script:ApprenticeCredentialName) {
            # Save the match before another -match operation changes $Matches.
            $prefix = ($argument -split '=', 2)[0] + '='
            $value = $prefix + '<redacted>'
            $binding = @{field='args'; index=$index; prefix=$prefix}
        } elseif ($argument -match $script:ApprenticeCredentialValue -or
                  ($argument -match $script:ApprenticeCredentialName -and $argument -match '[:=\s]')) {
            $value = '<redacted>'
            $binding = @{field='args'; index=$index}
        }
        $safe.Add($value)
        if ($binding) { $bindings.Add($binding) }
        $nextSensitive = $argument.StartsWith('-') -and $argument -match $script:ApprenticeCredentialName -and -not $argument.Contains('=')
    }
    return @{args=$safe.ToArray(); secret_bindings=$bindings.ToArray(); redacted=($bindings.Count -gt 0);
             requires_review=($bindings.Count -gt 0)}
}

function ConvertTo-ApprenticeNativeArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows CRT convention: double backslashes before embedded/trailing quotes.
    $escaped = [regex]::Replace($Value, '(\\*)"', {
        param($match)
        ($match.Groups[1].Value * 2) + '\"'
    })
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function New-ApprenticeProcessInfo {
    param([Parameter(Mandatory=$true)][string]$Executable,
          [AllowEmptyCollection()][AllowEmptyString()][string[]]$Arguments = @(),
          [Parameter(Mandatory=$true)][string]$WorkingDirectory)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Executable
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    if ($info.PSObject.Properties['ArgumentList']) {
        foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    } else {
        $quoted = foreach ($argument in $Arguments) { ConvertTo-ApprenticeNativeArgument -Value $argument }
        $info.Arguments = $quoted -join ' '
    }
    return $info
}

function Resolve-ApprenticeExecutable {
    param([Parameter(Mandatory=$true)][string]$Executable)
    $command = Get-Command -Name $Executable -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $path = $command.Source
    if ([IO.Path]::GetExtension($path) -notin @('.exe', '.com')) {
        throw 'A structured command requires a native executable. Use Invoke-ApprenticeScript for scripts or batch files.'
    }
    return $path
}

function Get-ApprenticeWorkingDirectory {
    $location = Get-Location
    if ($location.Provider.Name -ne 'FileSystem') { throw 'Submitted commands need a filesystem working directory.' }
    return $location.ProviderPath
}

function Resolve-ApprenticeFilesystemPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    # .NET's process cwd can differ from PowerShell's current location after
    # Set-Location. Use the shell provider so relative spools/effects refer to
    # the directory where the user actually submitted the command.
    $provider = $null
    $drive = $null
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        $Path, [ref]$provider, [ref]$drive)
    if ($provider.Name -ne 'FileSystem') { throw 'Recording paths must belong to the filesystem provider.' }
    return [IO.Path]::GetFullPath($resolved)
}

function Sync-ApprenticeCommands {
    [CmdletBinding()]
    param()
    if (-not $script:ApprenticeEventFile) { return }
    foreach ($commandId in @($script:ApprenticePendingCommands.Keys)) {
        $process = $script:ApprenticePendingCommands[$commandId]
        if ($process.HasExited) {
            Write-ApprenticeEvent -Kind 'command_completed' -Payload @{command_id=$commandId; exit_code=$process.ExitCode; process_id=$process.Id}
            $script:ApprenticePendingCommands.Remove($commandId)
            $process.Dispose()
        }
    }
}

function Invoke-ApprenticeCommand {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Executable,
          [AllowEmptyCollection()][AllowEmptyString()][string[]]$ArgumentList = @(),
          [int[]]$SensitiveArgument = @(), [switch]$Sensitive,
          [string[]]$EffectPaths = @(), [switch]$NoWait)
    if (-not $script:ApprenticeEventFile) { throw 'Enable-ApprenticeRecording in this shell first.' }
    Sync-ApprenticeCommands
    $cwd = Get-ApprenticeWorkingDirectory
    $resolved = Resolve-ApprenticeExecutable -Executable $Executable
    $commandId = [Guid]::NewGuid().ToString('N')
    $payload = Get-ApprenticeRedactedArguments -Arguments $ArgumentList -SensitiveArgument $SensitiveArgument -Sensitive:$Sensitive
    $payload.command_id = $commandId
    $payload.mode = 'exec'
    $payload.executable = $resolved
    $payload.cwd = $cwd
    $payload.effect_paths = @(foreach ($path in $EffectPaths) { Resolve-ApprenticeFilesystemPath -Path $path })
    Write-ApprenticeEvent -Kind 'command_started' -Payload $payload
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = New-ApprenticeProcessInfo -Executable $resolved -Arguments $ArgumentList -WorkingDirectory $cwd
    try {
        if (-not $process.Start()) { throw 'Native process did not start.' }
        if ($NoWait) {
            $script:ApprenticePendingCommands[$commandId] = $process
            Write-ApprenticeEvent -Kind 'command_ongoing' -Payload @{command_id=$commandId; process_id=$process.Id; requires_review=$true}
            return [pscustomobject]@{CommandId=$commandId; Process=$process}
        }
        $process.WaitForExit()
        $global:LASTEXITCODE = $process.ExitCode
        Write-ApprenticeEvent -Kind 'command_completed' -Payload @{command_id=$commandId; exit_code=$process.ExitCode; process_id=$process.Id}
    } catch {
        Write-ApprenticeEvent -Kind 'command_failed' -Payload @{command_id=$commandId; requires_review=$true}
        # Native error strings may include arguments or other private values.
        throw 'The submitted native command could not start or complete.'
    } finally {
        if (-not $NoWait -or -not $script:ApprenticePendingCommands.ContainsKey($commandId)) { $process.Dispose() }
    }
}

function Invoke-ApprenticeScript {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Script,
          [switch]$RecordScriptText, [switch]$Sensitive, [string[]]$EffectPaths = @())
    if (-not $script:ApprenticeEventFile) { throw 'Enable-ApprenticeRecording in this shell first.' }
    Sync-ApprenticeCommands
    $cwd = Get-ApprenticeWorkingDirectory
    $commandId = [Guid]::NewGuid().ToString('N')
    $redacted = -not $RecordScriptText -or $Sensitive -or $Script -match $script:ApprenticeCredentialName -or
                $Script -match $script:ApprenticeCredentialValue -or $Script -match '(?i)(encodedcommand|frombase64string|securestring)'
    $saved = if ($redacted) { '<redacted>' } else { $Script }
    $bindings = if ($redacted) { @(@{field='script'}) } else { @() }
    $shell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    Write-ApprenticeEvent -Kind 'command_started' -Payload @{command_id=$commandId; mode='powershell'; cwd=$cwd;
        executable=$shell; script=$saved; script_text_reviewed=([bool]$RecordScriptText -and -not $redacted);
        secret_bindings=$bindings; redacted=$redacted; requires_review=$true;
        effect_paths=@(foreach ($path in $EffectPaths) { Resolve-ApprenticeFilesystemPath -Path $path })}
    $completed = $false
    try {
        # A script block stays in this shell's actual session: functions, modules,
        # variables, pipelines and native prompts retain their PowerShell meaning.
        # Its scope is local to this helper; stateful session changes need review.
        $global:LASTEXITCODE = 0
        & ([scriptblock]::Create($Script))
        $succeeded = $?
        $exitCode = if ($global:LASTEXITCODE -ne 0) { [int]$global:LASTEXITCODE } elseif ($succeeded) { 0 } else { 1 }
        Write-ApprenticeEvent -Kind 'command_completed' -Payload @{command_id=$commandId; exit_code=$exitCode}
        $completed = $true
        $global:LASTEXITCODE = $exitCode
    } catch {
        Write-ApprenticeEvent -Kind 'command_completed' -Payload @{command_id=$commandId; exit_code=1}
        $completed = $true
        $global:LASTEXITCODE = 1
        throw 'The submitted PowerShell script failed.'
    } finally {
        if (-not $completed) { Write-ApprenticeEvent -Kind 'command_failed' -Payload @{command_id=$commandId; requires_review=$true} }
    }
}

function Invoke-ApprenticeProject {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Path, [string]$App = 'vscode', [string]$Python)
    if (-not $script:ApprenticeEventFile) { throw 'Enable-ApprenticeRecording in this shell first.' }
    $project = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    if (-not [IO.Directory]::Exists($project)) { throw 'Opening a project requires an existing directory.' }
    if (-not $Python) { $Python = $env:APPRENTICE_PYTHON }
    if (-not $Python) { $Python = Join-Path $script:ApprenticeRepoRoot '.venv\Scripts\python.exe' }
    $resolved = Resolve-ApprenticeExecutable -Executable $Python
    $arguments = @('-m', 'apprentice.recorder.application', '--event-file', $script:ApprenticeEventFile,
                   '--app', $App, '--path', $project)
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = New-ApprenticeProcessInfo -Executable $resolved -Arguments $arguments -WorkingDirectory (Get-ApprenticeWorkingDirectory)
    $process.StartInfo.EnvironmentVariables['PYTHONPATH'] = Join-Path $script:ApprenticeRepoRoot 'src'
    try {
        if (-not $process.Start()) { throw 'Project capture helper did not start.' }
        $process.WaitForExit()
        $global:LASTEXITCODE = $process.ExitCode
        if ($process.ExitCode -ne 0) { throw 'Project opening could not be verified.' }
    } catch { throw 'The project capture helper could not open and verify this project.' }
    finally { $process.Dispose() }
}

function Disable-ApprenticeRecording {
    [CmdletBinding()]
    param()
    Sync-ApprenticeCommands
    if ($script:ApprenticeInteractiveEnabled) {
        Set-PSReadLineOption -AddToHistoryHandler $script:ApprenticeOriginalHistoryHandler
        if ($script:ApprenticeOriginalPrompt) {
            Set-Item -LiteralPath Function:\global:prompt -Value $script:ApprenticeOriginalPrompt
        } else {
            Remove-Item -LiteralPath Function:\global:prompt -ErrorAction SilentlyContinue
        }
        $script:ApprenticeInteractiveEnabled = $false
        $script:ApprenticeInteractivePending = $null
        $script:ApprenticeOriginalHistoryHandler = $null
        $script:ApprenticeOriginalPrompt = $null
    }
    foreach ($commandId in @($script:ApprenticePendingCommands.Keys)) {
        # Disenrollment does not terminate programs the user deliberately started.
        $script:ApprenticePendingCommands[$commandId].Dispose()
    }
    $script:ApprenticePendingCommands = @{}
    $script:ApprenticeEventFile = $null
}
