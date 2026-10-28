#requires -Version 5.1
<#!
Self-paced P2 QA controller. No task is registered merely by loading this file.
Run/Once/Install require an explicitly selected, dedicated git worktree.
Stop and Pause take effect between workers; no command kills a Codex process.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('Run', 'Once', 'Status', 'Stop', 'Pause', 'Resume', 'Recover', 'Install', 'Uninstall')]
    [string]$Command = 'Status',
    [string]$Workspace,
    [string]$StateDirectory,
    [ValidateRange(1, 86400)][int]$IntervalSeconds = 300,
    [ValidateRange(1, 86400)][int]$RetrySeconds = 300,
    [string]$CodexPath = 'codex.exe',
    [switch]$Start,
    [switch]$ConfirmWorkerStopped
)

Set-StrictMode -Version 2.0
$script:QARepository = 'Khumoyun1307/personal-ai-assistant'
$script:QAOwner = 'Khumoyun1307'
$script:QATaskName = 'P2-Antony-QA'
$script:QAControllerPath = $PSCommandPath

function Get-QAValue($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
    } elseif ($null -ne $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}

function ConvertTo-QANativeArgument([AllowEmptyString()][string]$Value) {
    # CommandLineToArgvW quoting, including backslashes before quotes/end quote.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-QANative {
    param([string]$Executable, [string[]]$Arguments, [string]$Directory,
        [string]$InputText, [switch]$Capture, [scriptblock]$OnStarted)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-QANativeArgument $_ }) -join ' ')
    $info.WorkingDirectory = $Directory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    $begun = $false
    try {
        if (-not $process.Start()) { throw 'process_start_failed' }
        $begun = $true
        if ($Capture) { $output = $process.StandardOutput.ReadToEndAsync() }
        else { $output = $process.StandardOutput.BaseStream.CopyToAsync([System.IO.Stream]::Null) }
        $errors = $process.StandardError.BaseStream.CopyToAsync([System.IO.Stream]::Null)
        if ($OnStarted) { & $OnStarted $process }
        if ($null -ne $InputText) {
            # .NET Framework/PS5 lacks ProcessStartInfo.StandardInputEncoding.
            $bytes = [Text.Encoding]::UTF8.GetBytes($InputText)
            $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
            $process.StandardInput.BaseStream.Flush()
        }
        $process.StandardInput.Close()
        # Deliberately no timeout/kill: Stop must never terminate an active worker.
        $process.WaitForExit()
        $output.GetAwaiter().GetResult() | Out-Null
        $errors.GetAwaiter().GetResult() | Out-Null
        $body = ''
        if ($Capture) {
            $body = $output.Result
            if ($body.Length -gt 16777216) { throw 'native_output_too_large' }
        }
        return @{ exit_code = $process.ExitCode; output = $body }
    } finally {
        # Even a failed PID-state write must not release the lock while the child runs.
        if ($begun) { $process.StandardInput.Close(); $process.WaitForExit() }
        $process.Dispose()
    }
}

function Get-QAHash([string]$Value) {
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Write-QAJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 30 -Compress), (New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Path)) {
            $backup = $Path + '.old'
            [IO.File]::Replace($temporary, $Path, $backup)
            [IO.File]::Delete($backup)
        }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Initialize-QADirectory([string]$Directory) {
    [IO.Directory]::CreateDirectory($Directory) | Out-Null
    # Create the ignore rule before any state, lock or temporary worker result.
    [IO.File]::WriteAllText((Join-Path $Directory '.gitignore'), "*`n", (New-Object Text.UTF8Encoding($false)))
}

function Get-QAState([string]$Directory) {
    $path = Join-Path $Directory 'state.json'
    $state = @{}
    if ([IO.File]::Exists($path)) {
        if ((Get-Item -LiteralPath $path).Length -gt 1048576) { throw 'state_too_large' }
        $saved = [IO.File]::ReadAllText($path) | ConvertFrom-Json
        foreach ($property in $saved.PSObject.Properties) { $state[$property.Name] = $property.Value }
    }
    return $state
}

function Open-QALock([string]$Directory) {
    try { return [IO.File]::Open((Join-Path $Directory 'controller.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch [IO.IOException] { return $null }
}

function Test-QAWorkerAlive($State) {
    $workerId = Get-QAValue $State 'worker_pid'
    if (-not $workerId) { return $false }
    try {
        $worker = Get-Process -Id $workerId -ErrorAction Stop
        return $worker.StartTime.ToUniversalTime().ToString('o') -eq (Get-QAValue $State 'worker_started_utc')
    } catch [Microsoft.PowerShell.Commands.ProcessCommandException] { return $false }
    catch { return $true } # Cannot establish that the previous worker ended: fail closed.
}

function Invoke-QAGitHub([string]$Path, [bool]$Paged) {
    $arguments = @('api', '-H', 'Accept: application/vnd.github+json')
    if ($Paged) { $arguments += @('--paginate', '--slurp') }
    $arguments += $Path
    $result = Invoke-QANative 'gh.exe' $arguments (Get-Location).Path -Capture
    if ($result.exit_code -ne 0) { throw 'github_preflight_failed' }
    $parsed = $result.output | ConvertFrom-Json
    if ($Paged) {
        foreach ($page in @($parsed)) { foreach ($item in @($page)) { if ($null -ne $item) { $item } } }
    } else { $parsed }
}

function Test-QAOwnNote($Item, $State) {
    $url = [string](Get-QAValue $Item 'html_url' '')
    if ($url -and $url -in @(Get-QAValue $State 'ignored_urls' @())) { return $true }
    return $false # A shared login or an Antony prefix alone cannot prove authorship.
}

function Get-QASnapshot {
    param($Options, $State, [scriptblock]$Gh = { param($path, $paged) Invoke-QAGitHub $path $paged })
    $identity = & $Gh 'user' $false
    if ((Get-QAValue $identity 'login') -cne $script:QAOwner) { throw 'github_account_mismatch' }
    $entries = New-Object 'System.Collections.Generic.List[object]'
    function Add-Entry([string]$Id, $Value) {
        $entries.Add([pscustomobject][ordered]@{ id = $Id; hash = Get-QAHash ($Value | ConvertTo-Json -Depth 25 -Compress) })
    }
    function Add-Notes([string]$Prefix, $Notes) {
        foreach ($note in @($Notes)) {
            if ((Get-QAValue (Get-QAValue $note 'user') 'login') -ne $script:QAOwner) { continue }
            if (Test-QAOwnNote $note $State) { continue }
            Add-Entry ($Prefix + ':' + $note.id) @($note.body, (Get-QAValue $note 'state' ''), (Get-QAValue $note 'commit_id' ''))
        }
    }
    $root = 'repos/' + $script:QARepository
    $pulls = @(& $Gh ($root + '/pulls?state=open&per_page=100') $true)
    foreach ($pr in $pulls) {
        if ($pr.user.login -ne $script:QAOwner -or (Get-QAValue (Get-QAValue $pr.head 'repo') 'full_name') -ne $script:QARepository) { continue }
        $labels = @($pr.labels | ForEach-Object { $_.name } | Sort-Object)
        $queued = -not $pr.draft -and 'needs-qa' -in $labels
        $owned = $pr.head.ref -match '^(qa|ops)/'
        if ($queued -or $owned) {
            Add-Entry ('pr:' + $pr.number) @($pr.head.sha, $pr.base.sha, $pr.base.ref, $labels)
            $checks = & $Gh ($root + '/commits/' + $pr.head.sha + '/check-runs?per_page=100') $false
            Add-Entry ('ci:' + $pr.number) @(@($checks.check_runs | Sort-Object id | ForEach-Object { @($_.id, $_.name, $_.status, $_.conclusion) }))
            $statuses = & $Gh ($root + '/commits/' + $pr.head.sha + '/status?per_page=100') $false
            Add-Entry ('status:' + $pr.number) @($statuses.state, @($statuses.statuses | Sort-Object context | ForEach-Object { @($_.context, $_.state) }))
        }
        if ($queued -or $owned) {
            # Author units need CI/fix feedback; feature units need same-head QA requests.
            Add-Notes ('pr-comment:' + $pr.number) @(& $Gh ($root + '/issues/' + $pr.number + '/comments?per_page=100') $true)
            Add-Notes ('pr-review:' + $pr.number) @(& $Gh ($root + '/pulls/' + $pr.number + '/reviews?per_page=100') $true)
            Add-Notes ('pr-inline:' + $pr.number) @(& $Gh ($root + '/pulls/' + $pr.number + '/comments?per_page=100') $true)
        }
    }
    $issues = @(& $Gh ($root + '/issues?state=open&labels=eng%3Aantony&per_page=100') $true)
    $coordination = & $Gh ($root + '/issues/43') $false
    foreach ($issue in @($issues) + @($coordination)) {
        if ($issue.user.login -ne $script:QAOwner -or $null -ne (Get-QAValue $issue 'pull_request')) { continue }
        Add-Entry ('issue:' + $issue.number) @($issue.title, $issue.body, $issue.state, @($issue.labels | ForEach-Object { $_.name } | Sort-Object))
        Add-Notes ('issue-comment:' + $issue.number) @(& $Gh ($root + '/issues/' + $issue.number + '/comments?per_page=100') $true)
    }
    $ordered = @($entries | Sort-Object id -Unique)
    return @{ entries = $ordered; fingerprint = Get-QAHash ($ordered | ConvertTo-Json -Depth 5 -Compress) }
}

function Test-QAResult($Result, [string]$CheckId) {
    if ($null -eq $Result -or (Get-QAValue $Result 'status') -notin @('idle', 'completed', 'blocked', 'failed')) { throw 'invalid_worker_result' }
    $names = @($Result.PSObject.Properties.Name)
    if ($Result -is [Collections.IDictionary]) { $names = @($Result.Keys) }
    if ((@($names | Sort-Object) -join ',') -ne 'check_id,posted_urls,reviewed_heads,status,summary') { throw 'invalid_worker_fields' }
    if ((Get-QAValue $Result 'check_id') -cnotmatch '^[a-f0-9]{32}$' -or $Result.check_id -cne $CheckId) { throw 'worker_check_id_mismatch' }
    if ((Get-QAValue $Result 'summary') -isnot [string] -or $Result.summary.Length -gt 2000) { throw 'invalid_worker_summary' }
    if ($Result.reviewed_heads -isnot [array] -or $Result.posted_urls -isnot [array]) { throw 'invalid_worker_lists' }
    if (@($Result.reviewed_heads).Count -gt 100 -or @($Result.posted_urls).Count -gt 100) { throw 'worker_lists_too_large' }
    foreach ($head in @($Result.reviewed_heads)) {
        $keys = @($head.PSObject.Properties.Name)
        if ($head -is [Collections.IDictionary]) { $keys = @($head.Keys) }
        if ((($keys | Sort-Object) -join ',') -ne 'pr,sha') { throw 'invalid_reviewed_head_fields' }
        $number = Get-QAValue $head 'pr'
        if ($number -isnot [int] -and $number -isnot [long]) { throw 'invalid_reviewed_pr' }
        if ($number -lt 1 -or (Get-QAValue $head 'sha') -cnotmatch '^[0-9a-f]{40}$') { throw 'invalid_reviewed_head' }
    }
    foreach ($url in @($Result.posted_urls)) {
        if ($url -isnot [string] -or $url -cnotmatch '^https://github\.com/Khumoyun1307/personal-ai-assistant/(?:pull|issues)/[1-9][0-9]*(?:#[A-Za-z0-9_-]+)?$') { throw 'invalid_posted_url' }
    }
}

function Invoke-QAWorker($Options, [string]$CheckId, [scriptblock]$OnStarted) {
    $promptPath = Join-Path $Options.Workspace 'docs/engineering/p2/qa/p2-self-check-prompt.md'
    $schema = Join-Path $Options.Workspace 'tools/acceptance/p2_qa_result.schema.json'
    $prompt = [IO.File]::ReadAllText($promptPath)
    if (-not [IO.File]::Exists($schema)) { throw 'missing_worker_schema' }
    foreach ($pair in @(@('WORKSPACE', $Options.Workspace), @('COMPOSE_PROJECT', 'antony-scheduled'), @('CHECK_ID', $CheckId), @('STATE_DIRECTORY', $Options.StateDirectory))) {
        $prompt = $prompt.Replace('{{' + $pair[0] + '}}', $pair[1])
    }
    $resultPath = Join-Path $Options.StateDirectory ('result-' + $CheckId + '.json')
    try {
        $arguments = @('exec', '--approve-for-me', '--cd', $Options.Workspace, '--color', 'never', '--output-schema', $schema, '--output-last-message', $resultPath, '-')
        $run = Invoke-QANative $Options.CodexPath $arguments $Options.Workspace -InputText $prompt -OnStarted $OnStarted
        if ($run.exit_code -ne 0 -or -not [IO.File]::Exists($resultPath)) { throw 'worker_process_failed' }
        if ((Get-Item -LiteralPath $resultPath).Length -gt 65536) { throw 'worker_result_too_large' }
        $result = [IO.File]::ReadAllText($resultPath) | ConvertFrom-Json
        Test-QAResult $result $CheckId
        return $result
    } finally { if ([IO.File]::Exists($resultPath)) { [IO.File]::Delete($resultPath) } }
}

function Invoke-QAController {
    param($Options, [switch]$Once,
        [scriptblock]$Snapshot = { param($options, $state) Get-QASnapshot $options $state },
        [scriptblock]$Worker = { param($options, $id, $started) Invoke-QAWorker $options $id $started },
        [scriptblock]$Now = { [DateTimeOffset]::UtcNow },
        [scriptblock]$Sleep = { param($seconds) Start-Sleep -Milliseconds ([int]($seconds * 1000)) },
        [scriptblock]$WorkerAlive = { param($state) Test-QAWorkerAlive $state },
        [int]$MaxIterations = 0)
    Initialize-QADirectory $Options.StateDirectory
    $lock = Open-QALock $Options.StateDirectory
    if ($null -eq $lock) { return 'already_running' }
    try {
        $state = Get-QAState $Options.StateDirectory
        if (& $WorkerAlive $state) { return 'worker_still_running' }
        if (Get-QAValue $state 'launch_id') { return 'recovery_required' }
        $iteration = 0
        while ($MaxIterations -eq 0 -or $iteration -lt $MaxIterations) {
            $iteration++
            if (Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'stop')) {
                $state.phase = 'stopped'
                Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                return 'stopped'
            }
            if (Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'pause')) {
                $state.phase = 'paused'
                Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                if ($Once) { return 'paused' }
                & $Sleep 5
                continue
            }
            $nowUtc = & $Now
            $next = Get-QAValue $state 'next_due_utc'
            if (-not $Once -and $next -and [DateTimeOffset]::Parse($next) -gt $nowUtc) {
                & $Sleep ([Math]::Min(5, ([DateTimeOffset]::Parse($next) - $nowUtc).TotalSeconds))
                continue
            }
            $state.last_check_utc = $nowUtc.ToString('o')
            try { $snapshotValue = & $Snapshot $Options $state }
            catch {
                $state.phase = 'github_failed'
                $state.next_due_utc = (& $Now).AddSeconds($Options.RetrySeconds).ToString('o')
                Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                if ($Once) { return 'github_failed' }
                continue
            }
            if ((Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'stop')) -or (Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'pause'))) { continue }
            $delay = $Options.IntervalSeconds
            if ($snapshotValue.fingerprint -ne (Get-QAValue $state 'acknowledged_fingerprint')) {
                $id = [guid]::NewGuid().ToString('N')
                $state.phase = 'launching'
                $state.check_id = $id
                $state.launch_id = $id
                $state.worker_pid = $null
                $state.worker_started_utc = $null
                Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                $started = {
                    param($process)
                    $state.worker_pid = $process.Id
                    $state.worker_started_utc = $process.StartTime.ToUniversalTime().ToString('o')
                    $state.phase = 'working'
                    Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                }.GetNewClosure()
                try {
                    $result = & $Worker $Options $id $started
                    Test-QAResult $result $id
                    if ($result.status -eq 'failed') { throw 'worker_reported_failed' }
                    # Acknowledge the PRE-launch snapshot only. Changes during work remain pending.
                    $state.acknowledged_fingerprint = $snapshotValue.fingerprint
                    $state.acknowledged_entries = $snapshotValue.entries
                    $state.phase = $result.status
                    $state.reviewed_count = @($result.reviewed_heads).Count
                    $state.posted_count = @($result.posted_urls).Count
                    $state.ignored_urls = @(@(Get-QAValue $state 'ignored_urls' @()) + @($result.posted_urls) | Sort-Object -Unique)
                } catch { $state.phase = 'worker_failed'; $delay = $Options.RetrySeconds }
                finally { $state.worker_pid = $null; $state.worker_started_utc = $null; $state.launch_id = $null }
            } else { $state.phase = 'idle' }
            $state.last_finished_utc = (& $Now).ToString('o')
            $state.next_due_utc = ([DateTimeOffset]::Parse($state.last_finished_utc)).AddSeconds($delay).ToString('o')
            Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
            if ($Once) { return $state.phase }
        }
        return $state.phase
    } finally { $lock.Dispose() }
}

function Get-QAGitRoot([string]$Directory, [switch]$Common) {
    $arguments = @('-C', $Directory, 'rev-parse')
    if ($Common) { $arguments += @('--path-format=absolute', '--git-common-dir') }
    else { $arguments += '--show-toplevel' }
    $result = Invoke-QANative 'git.exe' $arguments $Directory -Capture
    if ($result.exit_code -ne 0) { throw 'workspace_not_git' }
    return [IO.Path]::GetFullPath($result.output.Trim())
}

function New-QAOptions([string]$SelectedWorkspace, [string]$SelectedState, [int]$Interval, [int]$Retry, [string]$Executable) {
    $controllerRoot = Get-QAGitRoot (Split-Path -Parent $script:QAControllerPath)
    $commonGit = Get-QAGitRoot $controllerRoot -Common
    $commonRoot = Split-Path -Parent $commonGit
    if (-not $SelectedState) { $SelectedState = Join-Path $commonRoot '.claude/antony-qa-scheduler' }
    if ($SelectedWorkspace) {
        $SelectedWorkspace = [IO.Path]::GetFullPath($SelectedWorkspace)
        if ((Get-QAGitRoot $SelectedWorkspace) -ne $SelectedWorkspace -or $SelectedWorkspace -eq $commonRoot -or -not [IO.File]::Exists((Join-Path $SelectedWorkspace '.git'))) { throw 'dedicated_worktree_required' }
        if ((Get-QAGitRoot $SelectedWorkspace -Common) -ne $commonGit) { throw 'workspace_repository_mismatch' }
    }
    return @{ Workspace = $SelectedWorkspace; StateDirectory = [IO.Path]::GetFullPath($SelectedState); IntervalSeconds = $Interval; RetrySeconds = $Retry; CodexPath = $Executable }
}

function Install-QATask($Options, [switch]$StartNow) {
    $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-File', $script:QAControllerPath, 'Run', '-Workspace', $Options.Workspace, '-StateDirectory', $Options.StateDirectory, '-IntervalSeconds', [string]$Options.IntervalSeconds, '-RetrySeconds', [string]$Options.RetrySeconds, '-CodexPath', $Options.CodexPath)
    $action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument (($arguments | ForEach-Object { ConvertTo-QANativeArgument $_ }) -join ' ')
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -DisallowHardTerminate -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $script:QATaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Self-paced P2 Antony QA; one worker, five minutes after completion.' -Force | Out-Null
    if ($StartNow) { Start-ScheduledTask -TaskName $script:QATaskName }
}

function Invoke-QAMain {
    if ($Command -in @('Run', 'Once', 'Install') -and -not $Workspace) { throw 'explicit_workspace_required' }
    if ($Start -and $Command -ne 'Install') { throw 'start_only_with_install' }
    $options = New-QAOptions $Workspace $StateDirectory $IntervalSeconds $RetrySeconds $CodexPath
    Initialize-QADirectory $options.StateDirectory
    switch ($Command) {
        'Run' { Invoke-QAController $options }
        'Once' { Invoke-QAController $options -Once }
        'Install' { Install-QATask $options -StartNow:$Start; 'installed' }
        'Status' {
            $state = Get-QAState $options.StateDirectory
            $lock = Open-QALock $options.StateDirectory
            $active = $null -eq $lock
            if ($lock) { $lock.Dispose() }
            @{ controller_running = $active; worker_running = (Test-QAWorkerAlive $state); paused = (Test-Path -LiteralPath (Join-Path $options.StateDirectory 'pause')); stop_requested = (Test-Path -LiteralPath (Join-Path $options.StateDirectory 'stop')); state = $state } | ConvertTo-Json -Depth 15
        }
        'Stop' { [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'stop'), ''); 'stop_requested' }
        'Pause' { [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'pause'), ''); 'pause_requested' }
        'Resume' { foreach ($name in @('stop', 'pause')) { $path = Join-Path $options.StateDirectory $name; if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) } }; 'resumed_not_started' }
        'Recover' {
            if (-not $ConfirmWorkerStopped) { throw 'confirm_worker_stopped_required' }
            $lock = Open-QALock $options.StateDirectory
            if ($null -eq $lock) { return 'already_running' }
            try {
                $state = Get-QAState $options.StateDirectory
                if (Test-QAWorkerAlive $state) { return 'worker_still_running' }
                $state.launch_id = $null; $state.worker_pid = $null; $state.worker_started_utc = $null
                $state.phase = 'recovered'
                Write-QAJson (Join-Path $options.StateDirectory 'state.json') $state
                'recovered_not_started'
            } finally { $lock.Dispose() }
        }
        'Uninstall' {
            [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'stop'), '')
            $lock = Open-QALock $options.StateDirectory
            if ($null -eq $lock) { return 'stop_requested_retry_uninstall_after_worker_finishes' }
            try {
                if (Test-QAWorkerAlive (Get-QAState $options.StateDirectory)) { return 'worker_still_running' }
                Unregister-ScheduledTask -TaskName $script:QATaskName -Confirm:$false -ErrorAction Stop
                'uninstalled'
            } finally { $lock.Dispose() }
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try { Invoke-QAMain }
    catch { [Console]::Error.WriteLine('scheduler_failed type=' + $_.Exception.GetType().Name); exit 1 }
}
