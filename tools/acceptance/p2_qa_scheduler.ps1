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
    [string]$ReviewedCommit,
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

function Get-QAFileHash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Get-QAControlFiles($Options) {
    if (-not (Get-QAValue $Options 'ControlRoot')) { throw 'control_root_required' }
    $paths = [ordered]@{
        controller = 'tools/acceptance/p2_qa_scheduler.ps1'
        prompt = 'docs/engineering/p2/qa/p2-self-check-prompt.md'
        schema = 'tools/acceptance/p2_qa_result.schema.json'
    }
    foreach ($name in $paths.Keys) {
        [pscustomobject]@{ name = $name; path = [IO.Path]::GetFullPath((Join-Path $Options.ControlRoot $paths[$name])) }
    }
}

function New-QAIntegrityPin($Options, [string]$Commit) {
    if ($Commit -cnotmatch '^[a-f0-9]{40}$') { throw 'reviewed_commit_required' }
    $controls = @(Get-QAControlFiles $Options | ForEach-Object {
        [pscustomobject]@{ name = $_.name; path = $_.path; sha256 = Get-QAFileHash $_.path }
    })
    return @{ version = 1; reviewed_commit = $Commit; workspace = $Options.Workspace; controls = $controls }
}

function Test-QAIntegrity($Options) {
    try {
        $path = Join-Path $Options.StateDirectory 'integrity.json'
        if (-not [IO.File]::Exists($path) -or (Get-Item -LiteralPath $path).Length -gt 16384) { return $false }
        $pin = [IO.File]::ReadAllText($path) | ConvertFrom-Json
        if ((@($pin.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'controls,reviewed_commit,version,workspace') { return $false }
        if ($pin.version -ne 1 -or $pin.reviewed_commit -cnotmatch '^[a-f0-9]{40}$' -or $pin.workspace -ne $Options.Workspace) { return $false }
        if ($pin.controls -isnot [array] -or $pin.controls.Count -ne 3) { return $false }
        foreach ($expected in @(Get-QAControlFiles $Options)) {
            if ($expected.name -eq 'controller' -and $Options.ControllerPath -ne $expected.path) { return $false }
            $found = @($pin.controls | Where-Object { $_.name -ceq $expected.name })
            if ($found.Count -ne 1) { return $false }
            $control = $found[0]
            if ((@($control.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'name,path,sha256') { return $false }
            if ($control.path -ne $expected.path -or $control.sha256 -cnotmatch '^[a-f0-9]{64}$') { return $false }
            if ((Get-QAFileHash $expected.path) -cne $control.sha256) { return $false }
        }
        return $true
    } catch { return $false }
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
    function Add-CIEntry([string]$Id, $Value, [bool]$Pending) {
        # A rerun may temporarily replace its completed row with a queued row.
        # Keep the acknowledged terminal fingerprint until conclusions are ready.
        $previous = @(Get-QAValue $State 'acknowledged_entries' @() | Where-Object { $_.id -eq $Id })
        if ($Pending -and $previous.Count -eq 1) {
            $entries.Add([pscustomobject][ordered]@{ id = $Id; hash = $previous[0].hash })
        } else { Add-Entry $Id $Value }
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
            $runs = @($checks.check_runs)
            $completed = @($runs | Where-Object { $_.status -eq 'completed' -and $_.conclusion })
            $pending = $runs.Count -eq 0 -or $completed.Count -ne $runs.Count
            $conclusions = @($completed | Sort-Object id | ForEach-Object { @($_.id, $_.name, $_.conclusion) })
            Add-CIEntry ('ci:' + $pr.number) $conclusions $pending
            $statuses = & $Gh ($root + '/commits/' + $pr.head.sha + '/status?per_page=100') $false
            $contexts = @($statuses.statuses)
            $terminal = @($contexts | Where-Object { $_.state -in @('success', 'failure', 'error') })
            $pending = $contexts.Count -eq 0 -or $terminal.Count -ne $contexts.Count
            $conclusions = @($terminal | Sort-Object context | ForEach-Object { @($_.context, $_.state) })
            Add-CIEntry ('status:' + $pr.number) $conclusions $pending
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
    if (-not (Test-QAIntegrity $Options)) { throw 'worker_integrity_failed' }
    $promptPath = Join-Path $Options.ControlRoot 'docs/engineering/p2/qa/p2-self-check-prompt.md'
    $schema = Join-Path $Options.ControlRoot 'tools/acceptance/p2_qa_result.schema.json'
    $prompt = [IO.File]::ReadAllText($promptPath)
    if (-not [IO.File]::Exists($schema)) { throw 'missing_worker_schema' }
    foreach ($pair in @(@('WORKSPACE', $Options.Workspace), @('COMPOSE_PROJECT', 'antony-scheduled'), @('CHECK_ID', $CheckId), @('STATE_DIRECTORY', $Options.StateDirectory))) {
        $prompt = $prompt.Replace('{{' + $pair[0] + '}}', $pair[1])
    }
    $resultPath = Join-Path $Options.StateDirectory ('result-' + $CheckId + '.json')
    try {
        # Explicit equivalent of --approve-for-me; its shorthand conflicts with -s.
        $arguments = @('exec', '-s', 'workspace-write', '--ignore-user-config', '--ignore-rules', '--strict-config', '--ephemeral', '-c', 'approval_policy="on-request"', '-c', 'approvals_reviewer="auto_review"', '--cd', $Options.Workspace, '--color', 'never', '--output-schema', $schema, '--output-last-message', $resultPath, '-')
        $run = Invoke-QANative $Options.CodexPath $arguments $Options.Workspace -InputText $prompt -OnStarted $OnStarted
        if ($run.exit_code -ne 0 -or -not [IO.File]::Exists($resultPath)) { throw 'worker_process_failed' }
        if ((Get-Item -LiteralPath $resultPath).Length -gt 65536) { throw 'worker_result_too_large' }
        $result = [IO.File]::ReadAllText($resultPath) | ConvertFrom-Json
        Test-QAResult $result $CheckId
        return $result
    } finally { if ([IO.File]::Exists($resultPath)) { [IO.File]::Delete($resultPath) } }
}

function Reset-QAWorkerFailures($Options, $State) {
    $State.consecutive_worker_failures = 0
    if ((Get-QAValue $State 'phase') -in @('paused', 'paused_after_failures')) { $State.phase = 'resumed' }
    Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $State
    [IO.File]::Delete((Join-Path $Options.StateDirectory 'resume'))
}

function Resume-QAController($Options) {
    # The owner can resume while a worker runs without racing its state writes.
    # Clear current requests here, never later: a newer Pause/Stop must win.
    foreach ($name in @('stop', 'pause')) { [IO.File]::Delete((Join-Path $Options.StateDirectory $name)) }
    [IO.File]::WriteAllText((Join-Path $Options.StateDirectory 'resume'), '')
    $lock = Open-QALock $Options.StateDirectory
    if ($null -eq $lock) { return 'resume_requested' }
    try { Reset-QAWorkerFailures $Options (Get-QAState $Options.StateDirectory); return 'resumed_not_started' }
    finally { $lock.Dispose() }
}

function Set-QAAutomaticPause($Options, $State, [string]$Phase) {
    [IO.File]::WriteAllText((Join-Path $Options.StateDirectory 'pause'), '')
    $State.phase = $Phase
}

function Invoke-QAController {
    param($Options, [switch]$Once,
        [scriptblock]$Snapshot = { param($options, $state) Get-QASnapshot $options $state },
        [scriptblock]$Worker = { param($options, $id, $started) Invoke-QAWorker $options $id $started },
        [scriptblock]$Now = { [DateTimeOffset]::UtcNow },
        [scriptblock]$Sleep = { param($seconds) Start-Sleep -Milliseconds ([int]($seconds * 1000)) },
        [scriptblock]$WorkerAlive = { param($state) Test-QAWorkerAlive $state },
        [scriptblock]$Integrity = { param($options) Test-QAIntegrity $options },
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
            if (Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'resume')) { Reset-QAWorkerFailures $Options $state }
            if (Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'stop')) {
                $state.phase = 'stopped'
                Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                return 'stopped'
            }
            if (Test-Path -LiteralPath (Join-Path $Options.StateDirectory 'pause')) {
                if ((Get-QAValue $state 'phase') -notin @('paused', 'paused_after_failures', 'integrity_failed')) {
                    $state.phase = 'paused'
                    Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                }
                if ($Once) { return $state.phase }
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
                $validControls = $false
                try { $validControls = [bool](& $Integrity $Options) } catch { $validControls = $false }
                if (-not $validControls) {
                    Set-QAAutomaticPause $Options $state 'integrity_failed'
                    Write-QAJson (Join-Path $Options.StateDirectory 'state.json') $state
                    if ($Once) { return 'integrity_failed' }
                    continue
                }
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
                    $state.consecutive_worker_failures = 0
                    $state.reviewed_count = @($result.reviewed_heads).Count
                    $state.posted_count = @($result.posted_urls).Count
                    $state.ignored_urls = @(@(Get-QAValue $state 'ignored_urls' @()) + @($result.posted_urls) | Sort-Object -Unique)
                } catch {
                    if ($_.Exception.Message -eq 'worker_integrity_failed') { Set-QAAutomaticPause $Options $state 'integrity_failed' }
                    else {
                        $state.phase = 'worker_failed'
                        $state.consecutive_worker_failures = [int](Get-QAValue $state 'consecutive_worker_failures' 0) + 1
                        if ($state.consecutive_worker_failures -ge 3) { Set-QAAutomaticPause $Options $state 'paused_after_failures' }
                    }
                    $delay = $Options.RetrySeconds
                }
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
    return @{ Workspace = $SelectedWorkspace; StateDirectory = [IO.Path]::GetFullPath($SelectedState); IntervalSeconds = $Interval; RetrySeconds = $Retry; CodexPath = $Executable; ControlRoot = $controllerRoot; ControllerPath = $script:QAControllerPath }
}

function Assert-QAInstallCheckout {
    param($Options, [string]$Commit,
        [scriptblock]$Git = { param($arguments, $directory) Invoke-QANative 'git.exe' (@('-C', $directory) + $arguments) $directory -Capture })
    if ($Commit -cnotmatch '^[a-f0-9]{40}$') { throw 'reviewed_commit_required' }
    $root = [IO.Path]::GetFullPath($Options.ControlRoot)
    $workerRoot = [IO.Path]::GetFullPath($Options.Workspace).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if ($root -eq $Options.Workspace -or $root.StartsWith($workerRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'controls_must_be_outside_worker' }
    if (-not [IO.File]::Exists((Join-Path $root '.git'))) { throw 'dedicated_control_worktree_required' }
    if ($Options.ControllerPath -ne (Join-Path $root 'tools/acceptance/p2_qa_scheduler.ps1')) { throw 'controller_path_mismatch' }
    $check = & $Git @('rev-parse', '--show-toplevel') $root
    if ($check.exit_code -ne 0 -or [IO.Path]::GetFullPath($check.output.Trim()) -ne $root) { throw 'control_root_mismatch' }
    $check = & $Git @('rev-parse', 'HEAD') $root
    if ($check.exit_code -ne 0 -or $check.output.Trim() -cne $Commit) { throw 'reviewed_commit_mismatch' }
    $check = & $Git @('symbolic-ref', '-q', 'HEAD') $root
    if ($check.exit_code -ne 1) { throw 'detached_control_checkout_required' }
    $check = & $Git @('status', '--porcelain', '--untracked-files=all') $root
    if ($check.exit_code -ne 0 -or $check.output.Trim()) { throw 'clean_control_checkout_required' }
}

function Install-QATask($Options, [switch]$StartNow, [string]$Commit) {
    $lock = Open-QALock $Options.StateDirectory
    if ($null -eq $lock) { throw 'controller_running_during_install' }
    try {
        $state = Get-QAState $Options.StateDirectory
        if ((Test-QAWorkerAlive $state) -or (Get-QAValue $state 'launch_id')) { throw 'worker_recovery_required_before_install' }
        Assert-QAInstallCheckout $Options $Commit
        $Options.CodexPath = (Get-Command $Options.CodexPath -CommandType Application -ErrorAction Stop).Source
        Write-QAJson (Join-Path $Options.StateDirectory 'integrity.json') (New-QAIntegrityPin $Options $Commit)
        if (-not (Test-QAIntegrity $Options)) { throw 'install_integrity_failed' }
        Register-QATask $Options
    } finally { $lock.Dispose() }
    # Starting while the installer holds the lease would make the new Run fail.
    if ($StartNow) { Start-ScheduledTask -TaskName $script:QATaskName -ErrorAction Stop }
}

function Register-QATask($Options) {
    $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-File', $Options.ControllerPath, 'Run', '-Workspace', $Options.Workspace, '-StateDirectory', $Options.StateDirectory, '-IntervalSeconds', [string]$Options.IntervalSeconds, '-RetrySeconds', [string]$Options.RetrySeconds, '-CodexPath', $Options.CodexPath)
    $action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument (($arguments | ForEach-Object { ConvertTo-QANativeArgument $_ }) -join ' ')
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -DisallowHardTerminate -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $script:QATaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Self-paced P2 Antony QA; one worker, five minutes after completion.' -Force | Out-Null
}

function Get-QACommandExitCode([string]$Action, $Result) {
    if ($Action -in @('Run', 'Once', 'Recover', 'Uninstall') -and [string]$Result -in @('already_running', 'worker_still_running', 'recovery_required', 'github_failed', 'worker_failed', 'paused_after_failures', 'integrity_failed', 'stop_requested_retry_uninstall_after_worker_finishes')) { return 1 }
    return 0
}

function Invoke-QAMain {
    if ($Command -in @('Run', 'Once', 'Install') -and -not $Workspace) { throw 'explicit_workspace_required' }
    if ($Start -and $Command -ne 'Install') { throw 'start_only_with_install' }
    if ($ReviewedCommit -and $Command -ne 'Install') { throw 'reviewed_commit_only_with_install' }
    $options = New-QAOptions $Workspace $StateDirectory $IntervalSeconds $RetrySeconds $CodexPath
    Initialize-QADirectory $options.StateDirectory
    switch ($Command) {
        'Run' { Invoke-QAController $options }
        'Once' { Invoke-QAController $options -Once }
        'Install' { Install-QATask $options -StartNow:$Start -Commit $ReviewedCommit; 'installed' }
        'Status' {
            $state = Get-QAState $options.StateDirectory
            $lock = Open-QALock $options.StateDirectory
            $active = $null -eq $lock
            if ($lock) { $lock.Dispose() }
            $workerActive = Test-QAWorkerAlive $state
            $elapsed = 0
            if ($workerActive -and (Get-QAValue $state 'worker_started_utc')) { $elapsed = [Math]::Max(0, ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($state.worker_started_utc)).TotalSeconds) }
            @{ controller_running = $active; worker_running = $workerActive; worker_elapsed_seconds = [int][Math]::Floor($elapsed); worker_overdue = $workerActive -and $elapsed -gt 7200; paused = (Test-Path -LiteralPath (Join-Path $options.StateDirectory 'pause')); stop_requested = (Test-Path -LiteralPath (Join-Path $options.StateDirectory 'stop')); state = $state } | ConvertTo-Json -Depth 15
        }
        'Stop' { [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'stop'), ''); 'stop_requested' }
        'Pause' { [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'pause'), ''); 'pause_requested' }
        'Resume' { Resume-QAController $options }
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
    try {
        $result = Invoke-QAMain
        $result
        exit (Get-QACommandExitCode $Command $result)
    }
    catch { [Console]::Error.WriteLine('scheduler_failed type=' + $_.Exception.GetType().Name); exit 1 }
}
