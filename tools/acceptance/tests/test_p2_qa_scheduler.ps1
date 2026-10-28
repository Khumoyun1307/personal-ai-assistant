#requires -Version 5.1
param([switch]$LockProbe, [string]$ProbeDirectory, [string]$ExitProbe, [string]$ProbeAction = 'Once')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../p2_qa_scheduler.ps1')
if ($LockProbe) {
    $probe = Open-QALock $ProbeDirectory
    if ($null -eq $probe) { 'busy' } else { $probe.Dispose(); 'free' }
    exit 0
}
if ($ExitProbe) { exit (Get-QACommandExitCode $ProbeAction $ExitProbe) }

$script:TestDirectory = Join-Path ([IO.Path]::GetTempPath()) ('antony-scheduler-test-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($script:TestDirectory) | Out-Null
$script:Passed = 0
function Assert-True($Condition, [string]$Message) { if (-not $Condition) { throw ('assertion_failed: ' + $Message) } }
function Assert-Equal($Actual, $Expected, [string]$Message) { if ($Actual -cne $Expected) { throw ('assertion_failed: ' + $Message + ' actual=' + $Actual + ' expected=' + $Expected) } }
function New-Fixture {
    $directory = Join-Path $script:TestDirectory ([guid]::NewGuid().ToString('N'))
    return @{ Workspace = $script:TestDirectory; StateDirectory = $directory; ControlRoot = $script:TestDirectory; ControllerPath = (Join-Path $script:TestDirectory 'synthetic-controller.ps1'); IntervalSeconds = 300; RetrySeconds = 30; CodexPath = 'never-run-codex.exe' }
}
function New-Result([string]$Id, [string]$Status = 'completed') {
    return @{ check_id = $Id; status = $Status; reviewed_heads = @(); posted_urls = @(); summary = 'Synthetic QA finished.' }
}
function New-Snapshot([string]$Hash) { return @{ fingerprint = $Hash; entries = @(@{ id = 'pr:92'; hash = $Hash }) } }
function New-ControlFixture {
    $options = New-Fixture
    $root = Join-Path $script:TestDirectory ([guid]::NewGuid().ToString('N'))
    $options.ControlRoot = Join-Path $root 'reviewed controls'
    $options.Workspace = Join-Path $root 'worker checkout'
    $options.ControllerPath = Join-Path $options.ControlRoot 'tools/acceptance/p2_qa_scheduler.ps1'
    [IO.Directory]::CreateDirectory($options.Workspace) | Out-Null
    $files = @{
        controller = $options.ControllerPath
        prompt = Join-Path $options.ControlRoot 'docs/engineering/p2/qa/p2-self-check-prompt.md'
        schema = Join-Path $options.ControlRoot 'tools/acceptance/p2_qa_result.schema.json'
    }
    foreach ($name in $files.Keys) {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $files[$name])) | Out-Null
        [IO.File]::WriteAllText($files[$name], ('Synthetic reviewed ' + $name), (New-Object Text.UTF8Encoding($false)))
    }
    [IO.File]::WriteAllText((Join-Path $options.ControlRoot '.git'), 'gitdir: synthetic-only')
    Initialize-QADirectory $options.StateDirectory
    return @{ options = $options; files = $files }
}
function Test-Case([string]$Name, [scriptblock]$Body) {
    & $Body
    $script:Passed++
    Write-Output ('PASS ' + $Name)
}

try {
    Test-Case 'completion_based_interval_and_quiet_poll' {
        $o = New-Fixture
        $clock = @{ now = [DateTimeOffset]::Parse('2026-10-28T10:00:00Z'); calls = 0; sleeps = @() }
        $now = { $clock.now }.GetNewClosure()
        $worker = { param($options, $id, $started) $clock.calls++; $clock.now = $clock.now.AddSeconds(120); New-Result $id }.GetNewClosure()
        $snapshot = { param($options, $state) New-Snapshot 'A' }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot $snapshot -Worker $worker -Now $now) 'completed' 'first unknown baseline invokes worker'
        $s = Get-QAState $o.StateDirectory
        Assert-Equal ([DateTimeOffset]::Parse($s.next_due_utc).ToString('HH:mm:ss')) '10:07:00' 'interval starts after two-minute worker'
        $sleep = { param($seconds) $clock.sleeps += $seconds; $clock.now = $clock.now.AddSeconds($seconds) }.GetNewClosure()
        Invoke-QAController $o -Integrity { $true } -Snapshot $snapshot -Worker $worker -Now $now -Sleep $sleep -MaxIterations 61 | Out-Null
        Assert-Equal $clock.calls 1 'quiet unchanged snapshot never invokes Codex'
        Assert-Equal $clock.sleeps.Count 60 'respects full completion-based delay'
        Assert-Equal (Get-QAState $o.StateDirectory).phase 'idle' 'quiet metadata check finishes idle'
    }

    Test-Case 'failed_preflight_and_worker_do_not_acknowledge' {
        $o = New-Fixture
        $snap = { param($options, $state) New-Snapshot 'A' }
        $ok = { param($options, $id, $started) New-Result $id }
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot $snap -Worker $ok | Out-Null
        $failedGh = { param($options, $state) throw 'synthetic-gh-error' }
        $forbidden = { throw 'worker_must_not_be_invoked' }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot $failedGh -Worker $forbidden) 'github_failed' 'GH failure is contained'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'GH failure retains cursor'
        $new = { param($options, $state) New-Snapshot 'B' }
        $failedWorker = { param($options, $id, $started) New-Result $id 'failed' }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot $new -Worker $failedWorker) 'worker_failed' 'failed worker is retried later'
        $s = Get-QAState $o.StateDirectory
        Assert-Equal $s.acknowledged_fingerprint 'A' 'failed worker retains cursor'
        Assert-Equal (([DateTimeOffset]::Parse($s.next_due_utc) - [DateTimeOffset]::Parse($s.last_finished_utc)).TotalSeconds) 30 'failure uses retry delay'
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot $new -Worker $ok | Out-Null
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'B' 'later success acknowledges pending snapshot'
    }

    Test-Case 'new_updates_during_work_remain_pending' {
        $o = New-Fixture
        $observed = @{ hash = 'A'; workers = 0 }
        $snapshot = { param($options, $state) New-Snapshot $observed.hash }.GetNewClosure()
        $worker = { param($options, $id, $started) $observed.workers++; $observed.hash = 'B'; New-Result $id }.GetNewClosure()
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot $snapshot -Worker $worker | Out-Null
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'only prelaunch A is acknowledged'
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot $snapshot -Worker $worker | Out-Null
        Assert-Equal $observed.workers 2 'B arriving during A triggers next worker'
    }

    Test-Case 'persistent_failures_pause_after_three_attempts_without_losing_cursor' {
        foreach ($mode in @('failed-result', 'nonzero-process', 'invalid-result')) {
            $o = New-Fixture
            Initialize-QADirectory $o.StateDirectory
            Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ acknowledged_fingerprint = 'A'; acknowledged_entries = @(@{ id = 'pr:92'; hash = 'A' }) }
            $clock = @{ now = [DateTimeOffset]::Parse('2026-10-28T10:00:00Z'); starts = @(); polls = 0; mode = $mode }
            $now = { $clock.now }.GetNewClosure()
            $sleep = { param($seconds) $clock.now = $clock.now.AddSeconds($seconds) }.GetNewClosure()
            $snapshot = { param($options, $state) $clock.polls++; New-Snapshot 'B' }.GetNewClosure()
            $worker = {
                param($options, $id, $started)
                $clock.starts += $clock.now
                $clock.now = $clock.now.AddSeconds(2)
                if ($clock.mode -eq 'nonzero-process') { throw 'worker_process_failed' }
                if ($clock.mode -eq 'invalid-result') { return @{ check_id = $id; status = 'completed' } }
                New-Result $id 'failed'
            }.GetNewClosure()
            Invoke-QAController $o -Integrity { $true } -Snapshot $snapshot -Worker $worker -Now $now -Sleep $sleep -MaxIterations 60 | Out-Null
            $state = Get-QAState $o.StateDirectory
            Assert-Equal $clock.starts.Count 3 ('exactly three starts for ' + $mode)
            Assert-Equal $clock.polls 3 'automatic pause stops metadata polling as well as launches'
            Assert-Equal (($clock.starts[1] - $clock.starts[0]).TotalSeconds) 32 'first retry waits after failed worker completion'
            Assert-Equal (($clock.starts[2] - $clock.starts[1]).TotalSeconds) 32 'second retry also waits after completion'
            Assert-True ([IO.File]::Exists((Join-Path $o.StateDirectory 'pause'))) 'third failure persists pause across controller restarts'
            Assert-Equal $state.consecutive_worker_failures 3 'third failure is journaled before pause'
            Assert-Equal $state.acknowledged_fingerprint 'A' 'failure cutoff retains last acknowledged fingerprint'
            Assert-Equal $state.acknowledged_entries[0].hash 'A' 'failure cutoff retains acknowledged entry hashes'
            Assert-True (-not $state.launch_id -and -not $state.worker_pid) 'completed failed attempts clear launch and PID guards'
            Invoke-QAController $o -Integrity { $true } -Once -Snapshot { throw 'paused_controller_must_not_poll' } -Worker { throw 'paused_controller_must_not_launch' } | Out-Null
            Assert-Equal (Get-QAState $o.StateDirectory).consecutive_worker_failures 3 'restart does not silently reset failure budget'
        }
    }

    Test-Case 'resume_and_success_reset_failure_budget_without_acknowledging_work' {
        $o = New-Fixture
        Initialize-QADirectory $o.StateDirectory
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ phase = 'paused_after_failures'; consecutive_worker_failures = 3; acknowledged_fingerprint = 'A'; acknowledged_entries = @(@{ id = 'pr:92'; hash = 'A' }) }
        [IO.File]::WriteAllText((Join-Path $o.StateDirectory 'pause'), '')
        [IO.File]::WriteAllText((Join-Path $o.StateDirectory 'stop'), '')
        Resume-QAController $o | Out-Null
        $state = Get-QAState $o.StateDirectory
        Assert-Equal $state.consecutive_worker_failures 0 'idle explicit Resume clears persisted failure budget'
        Assert-Equal $state.acknowledged_fingerprint 'A' 'Resume does not claim pending work was checked'
        Assert-True (-not [IO.File]::Exists((Join-Path $o.StateDirectory 'pause')) -and -not [IO.File]::Exists((Join-Path $o.StateDirectory 'stop'))) 'idle Resume clears boundary controls without launching'
        $state.consecutive_worker_failures = 2
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') $state
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $saved) New-Snapshot 'B' } -Worker { param($options, $id, $started) New-Result $id } | Out-Null
        $state = Get-QAState $o.StateDirectory
        Assert-Equal $state.consecutive_worker_failures 0 'a valid completed worker resets consecutive failures'
        Assert-Equal $state.acknowledged_fingerprint 'B' 'success still acknowledges its prelaunch snapshot'
        $failed = { param($options, $id, $started) New-Result $id 'failed' }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $saved) New-Snapshot 'C' } -Worker $failed) 'worker_failed' 'a failure after success starts a fresh budget'
        Assert-Equal (Get-QAState $o.StateDirectory).consecutive_worker_failures 1 'success prevents cumulative lifetime failure counting'
    }

    Test-Case 'resume_during_worker_is_consumed_at_boundary_without_overwriting_active_journal' {
        $o = New-Fixture
        Initialize-QADirectory $o.StateDirectory
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ consecutive_worker_failures = 1; acknowledged_fingerprint = 'A' }
        $clock = @{ now = [DateTimeOffset]::Parse('2026-10-28T10:00:00Z'); calls = 0 }
        $worker = {
            param($options, $id, $started)
            $clock.calls++
            & $started ([pscustomobject]@{ Id = 31415; StartTime = [DateTime]::SpecifyKind([DateTime]::Parse('2026-10-28T10:00:00'), [DateTimeKind]::Utc) })
            [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'pause'), '')
            $before = Get-QAState $options.StateDirectory
            Resume-QAController $options | Out-Null
            $during = Get-QAState $options.StateDirectory
            Assert-Equal $during.launch_id $before.launch_id 'active Resume cannot overwrite launch guard'
            Assert-Equal $during.check_id $id 'active Resume cannot overwrite worker identity'
            Assert-Equal $during.worker_pid 31415 'active Resume cannot clear active PID journal'
            Assert-Equal $during.consecutive_worker_failures 1 'active Resume defers state reset to exclusive controller'
            Assert-True (-not [IO.File]::Exists((Join-Path $options.StateDirectory 'pause'))) 'Resume clears the pause that preceded its request'
            New-Result $id 'failed'
        }.GetNewClosure()
        $now = { $clock.now }.GetNewClosure()
        $sleep = { param($seconds) $clock.now = $clock.now.AddSeconds($seconds) }.GetNewClosure()
        Invoke-QAController $o -Integrity { $true } -Snapshot { param($options, $saved) New-Snapshot 'B' } -Worker $worker -Now $now -Sleep $sleep -MaxIterations 2 | Out-Null
        $state = Get-QAState $o.StateDirectory
        Assert-Equal $clock.calls 1 'boundary Resume does not start an overlapping worker'
        Assert-Equal $state.consecutive_worker_failures 0 'controller consumes requested reset after failed worker returns'
        Assert-Equal $state.acknowledged_fingerprint 'A' 'failed in-flight worker remains unacknowledged after Resume'
        Assert-True (-not [IO.File]::Exists((Join-Path $o.StateDirectory 'pause'))) 'boundary Resume removes pause'
    }

    Test-Case 'later_pause_or_stop_wins_over_resume_requested_during_worker' {
        foreach ($control in @('pause', 'stop')) {
            $o = New-Fixture
            Initialize-QADirectory $o.StateDirectory
            Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ consecutive_worker_failures = 1; acknowledged_fingerprint = 'A' }
            $seen = @{ calls = 0; control = $control }
            $worker = {
                param($options, $id, $started)
                $seen.calls++
                Resume-QAController $options | Out-Null
                [IO.File]::WriteAllText((Join-Path $options.StateDirectory $seen.control), '')
                New-Result $id 'failed'
            }.GetNewClosure()
            $expected = $(if ($control -eq 'pause') { 'paused' } else { 'stopped' })
            Assert-Equal (Invoke-QAController $o -Integrity { $true } -Snapshot { param($options, $state) New-Snapshot 'B' } -Worker $worker -Sleep { param($seconds) } -MaxIterations 2) $expected 'later boundary control wins over queued Resume'
            $state = Get-QAState $o.StateDirectory
            Assert-Equal $seen.calls 1 'later pause or stop prevents another worker'
            Assert-Equal $state.consecutive_worker_failures 0 'queued Resume still resets failure budget'
            Assert-Equal $state.acknowledged_fingerprint 'A' 'failed worker remains pending despite Resume'
            Assert-True ([IO.File]::Exists((Join-Path $o.StateDirectory $control))) ('deferred reset preserves later ' + $control)
        }
    }

    Test-Case 'all_reviewed_controls_are_pinned_and_tampering_pauses_before_launch' {
        foreach ($name in @('controller', 'prompt', 'schema')) {
            $fixture = New-ControlFixture
            $o = $fixture.options
            $pin = New-QAIntegrityPin $o ('a' * 40)
            Write-QAJson (Join-Path $o.StateDirectory 'integrity.json') $pin
            Assert-True (Test-QAIntegrity $o) ('clean reviewed control passes integrity: ' + $name)
            Assert-Equal $pin.reviewed_commit ('a' * 40) 'pin records exact reviewed commit'
            Assert-Equal $pin.workspace $o.Workspace 'pin binds separate worker checkout'
            Assert-Equal @($pin.controls).Count 3 'pin contains exactly three control artifacts'
            $control = @($pin.controls | Where-Object { $_.name -eq $name })
            Assert-Equal $control.Count 1 'each named control appears once'
            Assert-Equal $control[0].path $fixture.files[$name] 'control pin uses exact trusted control path'
            Assert-True ($control[0].sha256 -cmatch '^[a-f0-9]{64}$') 'control bytes are hashed'
            Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ acknowledged_fingerprint = 'A' }
            [IO.File]::AppendAllText($fixture.files[$name], ' synthetic tamper')
            Assert-True (-not (Test-QAIntegrity $o)) ('changed control bytes rejected: ' + $name)
            $seen = @{ workers = 0 }
            $worker = { param($options, $id, $started) $seen.workers++; New-Result $id }.GetNewClosure()
            Assert-Equal (Invoke-QAController $o -Once -Snapshot { param($options, $state) New-Snapshot 'B' } -Worker $worker) 'integrity_failed' 'integrity fault is explicit'
            $state = Get-QAState $o.StateDirectory
            Assert-Equal $seen.workers 0 'tampered controls never start a worker'
            Assert-Equal $state.phase 'integrity_failed' 'integrity failure is persisted'
            Assert-Equal $state.acknowledged_fingerprint 'A' 'integrity failure cannot consume pending work'
            Assert-True ([IO.File]::Exists((Join-Path $o.StateDirectory 'pause'))) 'tampered controls persist a pause'
        }
        $fixture = New-ControlFixture
        Assert-True (-not (Test-QAIntegrity $fixture.options)) 'missing pin fails closed'
        Write-QAJson (Join-Path $fixture.options.StateDirectory 'integrity.json') @{ version = 1; reviewed_commit = ('a' * 40); workspace = $fixture.options.Workspace; controls = @() }
        Assert-True (-not (Test-QAIntegrity $fixture.options)) 'empty control list cannot vacuously pass'
        $pin = New-QAIntegrityPin $fixture.options ('a' * 40)
        $pin.controls[0].path = Join-Path $fixture.options.Workspace 'untrusted-controller.ps1'
        Write-QAJson (Join-Path $fixture.options.StateDirectory 'integrity.json') $pin
        Assert-True (-not (Test-QAIntegrity $fixture.options)) 'manifest cannot redirect a control path into worker checkout'
        $fixture = New-ControlFixture
        Write-QAJson (Join-Path $fixture.options.StateDirectory 'integrity.json') (New-QAIntegrityPin $fixture.options ('a' * 40))
        $fixture.options.ControllerPath = Join-Path $fixture.options.Workspace 'different-controller.ps1'
        Assert-True (-not (Test-QAIntegrity $fixture.options)) 'integrity checks the executing controller path as well as pinned files'
    }

    Test-Case 'install_checkout_requires_exact_detached_clean_reviewed_control_commit' {
        $fixture = New-ControlFixture
        $o = $fixture.options
        $gitState = @{ mode = 'clean'; calls = @() }
        $git = {
            param([string[]]$arguments, [string]$directory)
            Assert-Equal $directory $o.ControlRoot 'install checks the controller checkout, not mutable worker'
            $command = $arguments -join ' '
            $gitState.calls += $command
            switch ($command) {
                'rev-parse --show-toplevel' { return @{ exit_code = 0; output = $(if ($gitState.mode -eq 'root-mismatch') { $o.Workspace } else { $o.ControlRoot }) } }
                'rev-parse HEAD' { return @{ exit_code = 0; output = $(if ($gitState.mode -eq 'wrong-head') { 'b' * 40 } else { 'a' * 40 }) } }
                'symbolic-ref -q HEAD' { return @{ exit_code = $(if ($gitState.mode -eq 'attached') { 0 } else { 1 }); output = $(if ($gitState.mode -eq 'attached') { 'refs/heads/mutable' } else { '' }) } }
                'status --porcelain --untracked-files=all' { return @{ exit_code = 0; output = $(if ($gitState.mode -eq 'dirty') { ' M synthetic-control.ps1' } else { '' }) } }
                default { throw ('unexpected_fake_git:' + $command) }
            }
        }.GetNewClosure()
        Assert-QAInstallCheckout $o ('a' * 40) -Git $git | Out-Null
        Assert-True ($gitState.calls -contains 'rev-parse HEAD' -and $gitState.calls -contains 'symbolic-ref -q HEAD' -and $gitState.calls -contains 'status --porcelain --untracked-files=all') 'clean install validation checks commit, detached HEAD and dirty/untracked state'
        foreach ($mode in @('dirty', 'attached', 'wrong-head', 'root-mismatch')) {
            $gitState.mode = $mode
            $rejected = $false
            try { Assert-QAInstallCheckout $o ('a' * 40) -Git $git | Out-Null } catch { $rejected = $true }
            Assert-True $rejected ('unreviewed install checkout rejected: ' + $mode)
        }
        $gitState.mode = 'clean'
        $o.Workspace = $o.ControlRoot
        $rejected = $false
        try { Assert-QAInstallCheckout $o ('a' * 40) -Git $git | Out-Null } catch { $rejected = $true }
        Assert-True $rejected 'install cannot trust mutable worker checkout as controller checkout'
    }

    Test-Case 'task_metadata_uses_pinned_controller_and_one_nonterminating_logon_task' {
        $fixture = New-ControlFixture
        $o = $fixture.options
        $seen = @{ registrations = 0 }
        $mocks = @{
            'New-ScheduledTaskAction' = { param($Execute, $Argument) @{ execute = $Execute; argument = $Argument } }
            'New-ScheduledTaskTrigger' = { param([switch]$AtLogOn, $User) @{ at_logon = $AtLogOn.IsPresent } }
            'New-ScheduledTaskPrincipal' = { param($UserId, $LogonType, $RunLevel) @{ logon_type = $LogonType; run_level = $RunLevel } }
            'New-ScheduledTaskSettingsSet' = { param($MultipleInstances, $ExecutionTimeLimit, [switch]$DisallowHardTerminate, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries) @{ instances = $MultipleInstances; limit = $ExecutionTimeLimit; no_kill = $DisallowHardTerminate.IsPresent } }
            'Register-ScheduledTask' = {
                param($TaskName, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
                $seen.registrations++; $seen.name = $TaskName; $seen.action = $Action; $seen.trigger = $Trigger; $seen.principal = $Principal; $seen.settings = $Settings
            }
        }
        $saved = @{}
        try {
            foreach ($name in $mocks.Keys) {
                $saved[$name] = Get-Item -LiteralPath ('Function:global:' + $name) -ErrorAction SilentlyContinue
                Set-Item -LiteralPath ('Function:global:' + $name) -Value ($mocks[$name].GetNewClosure())
                Assert-Equal (Get-Command $name).CommandType 'Function' 'task API must be replaced before metadata test'
            }
            Register-QATask $o
            Assert-Equal $seen.registrations 1 'exactly one fake task registration occurs'
            Assert-Equal $seen.name 'P2-Antony-QA' 'all installs use a single stable task name'
            Assert-True $seen.action.argument.Contains((ConvertTo-QANativeArgument $o.ControllerPath)) 'task runs the pinned control checkout'
            Assert-True $seen.action.argument.Contains((ConvertTo-QANativeArgument $o.Workspace)) 'task keeps worker workspace separate and explicit'
            Assert-True $seen.action.argument.Contains('-WindowStyle Hidden') 'logon task runs hidden'
            Assert-True $seen.trigger.at_logon 'task uses Windows logon trigger'
            Assert-Equal $seen.principal.run_level 'Limited' 'task does not elevate worker privileges'
            Assert-Equal $seen.settings.instances 'IgnoreNew' 'Windows task also prevents duplicate controllers'
            Assert-Equal $seen.settings.limit ([TimeSpan]::Zero) 'Windows task cannot time out an active worker'
            Assert-True $seen.settings.no_kill 'task disallows hard termination'
        } finally {
            foreach ($name in $mocks.Keys) {
                if ($saved[$name]) { Set-Item -LiteralPath ('Function:global:' + $name) -Value $saved[$name].ScriptBlock }
                else { Remove-Item -LiteralPath ('Function:global:' + $name) -ErrorAction SilentlyContinue }
            }
        }
    }

    Test-Case 'one_controller_across_processes_and_stop_waits_for_worker' {
        $o = New-Fixture
        $observed = @{ finished = $false; nested = ''; child = '' }
        $testPath = $PSCommandPath
        $worker = {
            param($options, $id, $started)
            [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'stop'), '')
            $observed.nested = Invoke-QAController $options -Integrity { $true } -Once -Snapshot { throw 'no_snapshot' }
            $process = Invoke-QANative (Join-Path $PSHOME 'powershell.exe') @('-NoLogo', '-NoProfile', '-File', $testPath, '-LockProbe', '-ProbeDirectory', $options.StateDirectory) $script:TestDirectory -Capture
            Assert-Equal $process.exit_code 0 'child lock probe succeeds'
            $observed.child = $process.output.Trim()
            $observed.finished = $true
            New-Result $id
        }.GetNewClosure()
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Snapshot { param($options, $state) New-Snapshot 'A' } -Worker $worker -MaxIterations 3) 'stopped' 'stop observed after worker returns'
        Assert-True $observed.finished 'worker finishes after stop request'
        Assert-Equal $observed.nested 'already_running' 'second controller is skipped'
        Assert-Equal $observed.child 'busy' 'exclusive lock also excludes another process'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'finished worker acknowledged before stop'
    }

    Test-Case 'pause_and_orphan_launch_fail_closed' {
        $o = New-Fixture
        Initialize-QADirectory $o.StateDirectory
        [IO.File]::WriteAllText((Join-Path $o.StateDirectory 'pause'), '')
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { throw 'no_snapshot' }) 'paused' 'pause is observed before GH or worker'
        [IO.File]::Delete((Join-Path $o.StateDirectory 'pause'))
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ phase = 'launching'; launch_id = ('a' * 32) }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { throw 'no_snapshot' }) 'recovery_required' 'unknown spawned PID never launches a duplicate'
        $self = Get-Process -Id $PID
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ phase = 'working'; launch_id = ('b' * 32); worker_pid = $PID; worker_started_utc = $self.StartTime.ToUniversalTime().ToString('o') }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { throw 'no_snapshot' }) 'worker_still_running' 'PID plus creation time guards orphan worker'
    }

    Test-Case 'unique_check_ids_and_safe_result_validation' {
        $o = New-Fixture
        $observed = @{ ids = @() }
        $worker = { param($options, $id, $started) $observed.ids += $id; New-Result $id 'blocked' }.GetNewClosure()
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $state) New-Snapshot 'A' } -Worker $worker | Out-Null
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $state) New-Snapshot 'B' } -Worker $worker | Out-Null
        Assert-Equal $observed.ids.Count 2 'blocked work is acknowledged without retry loops'
        Assert-True ($observed.ids[0] -cmatch '^[a-f0-9]{32}$') 'run id is 32 hex'
        Assert-True ($observed.ids[0] -cne $observed.ids[1]) 'result paths cannot reuse run ids'
        $stale = { param($options, $id, $started) New-Result ('a' * 32) }
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $state) New-Snapshot 'C' } -Worker $stale) 'worker_failed' 'a stale result cannot acknowledge C'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'B' 'mismatched check id preserves cursor'
        $stored = [IO.File]::ReadAllText((Join-Path $o.StateDirectory 'state.json'))
        Assert-True (-not $stored.Contains('Synthetic QA finished.')) 'worker summary is never persisted'
        Assert-Equal ([IO.File]::ReadAllText((Join-Path $o.StateDirectory '.gitignore'))) "*`n" 'state ignores itself before writes'
    }

    Test-Case 'metadata_fingerprint_owner_only_ci_and_exact_self_post_suppression' {
        $notes = New-Object 'System.Collections.Generic.List[object]'
        $data = @{ notes = $notes; checkStatus = 'queued'; conclusion = $null; legacyState = 'pending'; legacyStatuses = @(); updated = 'time-one'; body = 'TL: synthetic incoming work'; fork = $false; login = 'Khumoyun1307'; labels = @(@{ name = 'needs-qa' }); branch = 'qa/scenarios'; draft = $false }
        $gh = {
            param($path, $paged)
            if ($path -eq 'user') { @{ login = $data.login } }
            elseif ($path -match '/pulls\?') {
                @{ number = 92; user = @{ login = 'Khumoyun1307' }; head = @{ sha = ('a' * 40); ref = $data.branch; repo = @{ full_name = $(if ($data.fork) { 'outsider/fork' } else { 'Khumoyun1307/personal-ai-assistant' }) } }; base = @{ sha = ('b' * 40); ref = 'int/m05' }; draft = $data.draft; labels = $data.labels; updated_at = $data.updated }
            } elseif ($path -match '/check-runs\?') { @{ check_runs = @(@{ id = 7; name = 'CI'; status = $data.checkStatus; conclusion = $data.conclusion }) } }
            elseif ($path -match '/status\?') { @{ state = $data.legacyState; statuses = $data.legacyStatuses } }
            elseif ($path -match '/issues\?') { @() }
            elseif ($path -match '/issues/43$') { @{ number = 43; title = 'Synthetic integration'; body = $data.body; user = @{ login = 'Khumoyun1307' }; state = 'open'; labels = @() } }
            elseif ($path -match '/issues/92/comments') { $data.notes.ToArray() }
            elseif ($path -match '/comments\?|/reviews\?') { @() }
            else { throw ('unexpected_fake_endpoint:' + $path) }
        }.GetNewClosure()
        $before = Get-QASnapshot @{} @{} -Gh $gh
        $data.updated = 'time-two'
        Assert-Equal (Get-QASnapshot @{} @{} -Gh $gh).fingerprint $before.fingerprint 'updatedAt cannot trigger work'
        $data.checkStatus = 'in_progress'
        Assert-Equal (Get-QASnapshot @{} @{} -Gh $gh).fingerprint $before.fingerprint 'running check cannot wake another worker'
        $data.checkStatus = 'completed'
        Assert-Equal (Get-QASnapshot @{} @{} -Gh $gh).fingerprint $before.fingerprint 'completed without a conclusion is not final feedback'
        $data.conclusion = 'success'
        $green = Get-QASnapshot @{} @{} -Gh $gh
        Assert-True ($green.fingerprint -ne $before.fingerprint) 'same-head CI completion wakes review'
        $data.legacyStatuses = @(@{ context = 'synthetic-lint'; state = 'pending' })
        Assert-Equal (Get-QASnapshot @{} @{} -Gh $gh).fingerprint $green.fingerprint 'pending legacy status cannot wake work'
        $data.legacyState = 'success'; $data.legacyStatuses = @(@{ context = 'synthetic-lint'; state = 'success' })
        $legacyGreen = Get-QASnapshot @{} @{} -Gh $gh
        Assert-True ($legacyGreen.fingerprint -ne $green.fingerprint) 'final legacy status is meaningful feedback'
        $green = $legacyGreen
        $acknowledged = @{ acknowledged_entries = $green.entries }
        $data.checkStatus = 'queued'; $data.conclusion = $null
        $data.legacyState = 'pending'; $data.legacyStatuses = @(@{ context = 'synthetic-lint'; state = 'pending' })
        Assert-Equal (Get-QASnapshot @{} $acknowledged -Gh $gh).fingerprint $green.fingerprint 'queued rerun preserves acknowledged terminal feedback'
        $data.checkStatus = 'in_progress'
        Assert-Equal (Get-QASnapshot @{} $acknowledged -Gh $gh).fingerprint $green.fingerprint 'running rerun cannot wake by removing prior conclusions'
        $data.checkStatus = 'completed'; $data.conclusion = 'failure'
        $data.legacyState = 'failure'; $data.legacyStatuses = @(@{ context = 'synthetic-lint'; state = 'failure' })
        Assert-True ((Get-QASnapshot @{} $acknowledged -Gh $gh).fingerprint -ne $green.fingerprint) 'rerun terminal failure wakes review'
        $data.conclusion = 'success'; $data.legacyState = 'success'; $data.legacyStatuses = @(@{ context = 'synthetic-lint'; state = 'success' })
        $ownUrl = 'https://github.com/Khumoyun1307/personal-ai-assistant/pull/92#issuecomment-101'
        $notes.Add(@{ id = 101; body = '**Antony QA** synthetic worker result'; user = @{ login = 'Khumoyun1307' }; html_url = $ownUrl })
        $notes.Add(@{ id = 102; body = 'synthetic untrusted outside account'; user = @{ login = 'outsider' }; html_url = 'https://github.com/example' })
        $selfFiltered = Get-QASnapshot @{} @{ ignored_urls = @($ownUrl) } -Gh $gh
        Assert-Equal $selfFiltered.fingerprint $green.fingerprint 'exact own posted URL and outside author are excluded'
        $notes.Add(@{ id = 103; body = 'Antony, please inspect this new manual request'; user = @{ login = 'Khumoyun1307' }; html_url = 'https://github.com/Khumoyun1307/personal-ai-assistant/pull/92#issuecomment-103' })
        $manual = Get-QASnapshot @{} @{ ignored_urls = @($ownUrl) } -Gh $gh
        Assert-True ($manual.fingerprint -ne $selfFiltered.fingerprint) 'Antony prefix alone must not hide manual/peer requests'
        $data.labels = @()
        $authorOnly = Get-QASnapshot @{} @{} -Gh $gh
        Assert-True (@($authorOnly.entries | ForEach-Object { $_.id }) -contains 'ci:92') 'author unit CI is observed without needs-qa label'
        $data.labels = @(@{ name = 'needs-qa' }); $data.branch = 'm05/feature'
        $feature = Get-QASnapshot @{} @{} -Gh $gh
        Assert-True (@($feature.entries | ForEach-Object { $_.id }) -contains 'pr-comment:92:103') 'ready feature same-head followup is observed'
        $data.draft = $true
        $draft = Get-QASnapshot @{} @{} -Gh $gh
        Assert-True (-not (@($draft.entries | ForEach-Object { $_.id }) -contains 'pr:92')) 'draft feature never enters ready queue'
        $data.draft = $false
        $data.fork = $true
        $forked = Get-QASnapshot @{} @{ ignored_urls = @($ownUrl) } -Gh $gh
        Assert-True (-not (@($forked.entries | ForEach-Object { $_.id }) -contains 'pr:92')) 'fork code never becomes queued work'
        $text = $manual | ConvertTo-Json -Depth 10
        Assert-True (-not $text.Contains('synthetic incoming work')) 'snapshot stores hashes not source bodies'
        $data.login = 'wrong-account'
        $refused = $false
        try { Get-QASnapshot @{} @{} -Gh $gh | Out-Null } catch { $refused = $true }
        Assert-True $refused 'wrong authenticated GitHub account refuses before work'
    }

    Test-Case 'worker_result_schema_types_and_extra_fields_are_rejected' {
        $id = 'a' * 32
        $invalid = @()
        $bad = New-Result $id; $bad.posted_urls = 'https://github.com/Khumoyun1307/personal-ai-assistant/pull/92'; $invalid += $bad
        $bad = New-Result $id; $bad.reviewed_heads = @{ pr = 92; sha = ('a' * 40) }; $invalid += $bad
        $bad = New-Result $id; $bad.reviewed_heads = @(@{ pr = 92; sha = ('a' * 40); unexpected = 1 }); $invalid += $bad
        $bad = New-Result $id; $bad.reviewed_heads = @(@{ pr = 92; sha = ('A' * 40) }); $invalid += $bad
        $bad = New-Result $id; $bad.posted_urls = @('https://github.com/outsider/repository/pull/92'); $invalid += $bad
        foreach ($value in $invalid) {
            $refused = $false
            try { Test-QAResult $value $id } catch { $refused = $true }
            Assert-True $refused 'malformed worker output is rejected'
        }
    }

    Test-Case 'silent_controller_failures_have_meaningful_native_exit_codes' {
        foreach ($phase in @('already_running', 'worker_still_running', 'recovery_required', 'github_failed', 'worker_failed', 'paused_after_failures', 'integrity_failed', 'stop_requested_retry_uninstall_after_worker_finishes')) {
            foreach ($action in @('Run', 'Once', 'Recover', 'Uninstall')) {
                Assert-Equal (Get-QACommandExitCode $action $phase) 1 ('unsuccessful ' + $action + ' reports nonzero: ' + $phase)
            }
        }
        foreach ($phase in @('completed', 'blocked', 'idle', 'paused', 'stopped', 'recovered_not_started', 'uninstalled')) {
            Assert-Equal (Get-QACommandExitCode 'Once' $phase) 0 ('legitimate result remains successful: ' + $phase)
        }
        $shell = Join-Path $PSHOME 'powershell.exe'
        $testPath = $PSCommandPath
        $failed = Invoke-QANative $shell @('-NoLogo', '-NoProfile', '-File', $testPath, '-ExitProbe', 'github_failed', '-ProbeAction', 'Once') $script:TestDirectory -Capture
        Assert-Equal $failed.exit_code 1 'silent failure exit mapping survives actual native process boundary'
        Assert-Equal $failed.output '' 'nonzero exit does not depend on incidental console text'
        $paused = Invoke-QANative $shell @('-NoLogo', '-NoProfile', '-File', $testPath, '-ExitProbe', 'paused', '-ProbeAction', 'Once') $script:TestDirectory -Capture
        Assert-Equal $paused.exit_code 0 'intentional pause is a successful native control outcome'
        $controllerPath = Join-Path $PSScriptRoot '../p2_qa_scheduler.ps1'
        $missingWorkspace = Invoke-QANative $shell @('-NoLogo', '-NoProfile', '-File', $controllerPath, 'Once') $script:TestDirectory -Capture
        Assert-Equal $missingWorkspace.exit_code 1 'actual controller rejects missing explicit workspace before any worker or API'
        Assert-Equal $missingWorkspace.output '' 'entrypoint error is not represented as a successful phase string'
    }

    Test-Case 'native_worker_stdin_result_and_failure_boundaries' {
        $fixture = New-ControlFixture
        $o = $fixture.options
        $o.Workspace = Join-Path $o.Workspace ('worker space ' + [char]0x00a7)
        [IO.Directory]::CreateDirectory($o.Workspace) | Out-Null
        $promptPath = $fixture.files.prompt
        $schemaPath = $fixture.files.schema
        [IO.File]::WriteAllText($schemaPath, '{}')
        $exe = Join-Path $script:TestDirectory 'fake worker.exe'
        $source = @'
using System;
using System.IO;
using System.Text;
public class QAFakeWorker {
    public static int Main(string[] args) {
        Console.InputEncoding = new UTF8Encoding(false);
        string prompt = Console.In.ReadToEnd();
        if (args.Length != 20 || args[0] != "exec" || args[1] != "-s" || args[2] != "workspace-write" || args[3] != "--ignore-user-config" || args[4] != "--ignore-rules" || args[5] != "--strict-config" || args[6] != "--ephemeral" || args[7] != "-c" || args[8] != "approval_policy=\"on-request\"" || args[9] != "-c" || args[10] != "approvals_reviewer=\"auto_review\"" || args[11] != "--cd" || args[13] != "--color" || args[14] != "never" || args[15] != "--output-schema" || args[17] != "--output-last-message" || args[19] != "-") return 41;
        if (!File.Exists(args[16]) || !Directory.Exists(args[12]) || !args[12].Contains("\u00a7") || !prompt.Contains("\u00a7")) return 42;
        if (args[16].StartsWith(args[12], StringComparison.OrdinalIgnoreCase)) return 44;
        string id = Path.GetFileNameWithoutExtension(args[18]).Substring(7);
        if (!prompt.Contains("Check=" + id) || !prompt.Contains("Compose=antony-scheduled") || !prompt.Contains("Workspace=" + args[12]) || prompt.Contains("{{")) return 43;
        File.AppendAllText(Path.Combine(Path.GetDirectoryName(args[18]), "synthetic-starts.txt"), "start\n");
        if (prompt.Contains("Mode=silent-nonzero")) return 17;
        Console.WriteLine("synthetic-private-stdout-canary");
        Console.Error.WriteLine("synthetic-private-stderr-canary");
        if (prompt.Contains("Mode=missing")) return 0;
        if (prompt.Contains("Mode=stale")) id = new String('f', 32);
        string result = "{\"check_id\":\"" + id + "\",\"status\":\"completed\",\"reviewed_heads\":[],\"posted_urls\":[],\"summary\":\"Synthetic \u00a7\"}";
        if (prompt.Contains("Mode=malformed")) result = "invalid synthetic JSON";
        File.WriteAllText(args[18], result, new UTF8Encoding(false));
        return prompt.Contains("Mode=nonzero") ? 13 : 0;
    }
}
'@
        Add-Type -TypeDefinition $source -OutputAssembly $exe -OutputType ConsoleApplication
        $o.CodexPath = $exe
        $template = "Check={{CHECK_ID}}`nWorkspace={{WORKSPACE}}`nCompose={{COMPOSE_PROJECT}}`nState={{STATE_DIRECTORY}}`nUnicode=" + [char]0x00a7 + "`nMode="
        [IO.File]::WriteAllText($promptPath, ($template + 'valid'), (New-Object Text.UTF8Encoding($false)))
        Write-QAJson (Join-Path $o.StateDirectory 'integrity.json') (New-QAIntegrityPin $o ('a' * 40))
        Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $state) New-Snapshot 'A' }) 'completed' 'real launcher runs fake executable with correct stdin/argv/UTF8'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'valid native final result acknowledged'
        foreach ($mode in @('stale', 'missing', 'nonzero', 'malformed', 'silent-nonzero')) {
            Resume-QAController $o | Out-Null
            [IO.File]::WriteAllText($promptPath, ($template + $mode), (New-Object Text.UTF8Encoding($false)))
            Write-QAJson (Join-Path $o.StateDirectory 'integrity.json') (New-QAIntegrityPin $o ('a' * 40))
            Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $state) New-Snapshot 'B' }) 'worker_failed' ('native result refused: ' + $mode)
            Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'failed native result retains previous cursor'
            Assert-Equal (Get-QAState $o.StateDirectory).consecutive_worker_failures 1 'native nonzero or invalid result consumes one failure attempt'
            Assert-Equal @(Get-ChildItem -LiteralPath $o.StateDirectory -Filter 'result-*.json').Count 0 'temporary final message removed even on failure'
        }
        Resume-QAController $o | Out-Null
        $startsPath = Join-Path $o.StateDirectory 'synthetic-starts.txt'
        $beforeStarts = [IO.File]::ReadAllLines($startsPath).Length
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $expected = $(if ($attempt -lt 3) { 'worker_failed' } else { 'paused_after_failures' })
            Assert-Equal (Invoke-QAController $o -Integrity { $true } -Once -Snapshot { param($options, $state) New-Snapshot 'B' }) $expected 'silent native nonzero reaches persisted failure cutoff'
        }
        Invoke-QAController $o -Integrity { $true } -Once -Snapshot { throw 'paused_native_worker_must_not_poll' } | Out-Null
        Assert-Equal ([IO.File]::ReadAllLines($startsPath).Length - $beforeStarts) 3 'persistent silent native failure starts no fourth process'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'silent failure cutoff retains old cursor'
        $stateText = [IO.File]::ReadAllText((Join-Path $o.StateDirectory 'state.json'))
        Assert-True (-not $stateText.Contains('private-stdout-canary') -and -not $stateText.Contains('private-stderr-canary') -and -not $stateText.Contains('Synthetic')) 'no transcript or summary persisted'
    }

    Test-Case 'native_windows_quoting_round_trip' {
        $exe = Join-Path $script:TestDirectory 'argument echo.exe'
        Add-Type -TypeDefinition 'using System; using System.Text; public class QAArgumentEcho { public static void Main(string[] args) { foreach (string value in args) Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(value))); } }' -OutputAssembly $exe -OutputType ConsoleApplication
        $values = @('path with spaces', 'C:\synthetic path\trailing\', 'embedded"quote', 'backslash\"quote', '', '$(literal) `literal`', 'ordinary')
        $result = Invoke-QANative $exe $values $script:TestDirectory -Capture
        Assert-Equal $result.exit_code 0 'fake native echo exits cleanly'
        $lines = @($result.output.Replace("`r", '').Split("`n"))
        Assert-Equal $lines.Count ($values.Count + 1) 'all empty/nonempty argument boundaries survive'
        for ($i = 0; $i -lt $values.Count; $i++) {
            Assert-Equal $lines[$i] ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($values[$i]))) ('argument round trip ' + $i)
        }
    }

    Write-Output ('PASS total=' + $script:Passed + ' fake-only; no Codex, GitHub, Docker or task registration')
} finally {
    $resolved = [IO.Path]::GetFullPath($script:TestDirectory)
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolved.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $resolved) -notmatch '^antony-scheduler-test-[a-f0-9]{32}$') { throw 'unsafe_test_cleanup_path' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
