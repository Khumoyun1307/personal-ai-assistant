#requires -Version 5.1
param([switch]$LockProbe, [string]$ProbeDirectory)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../p2_qa_scheduler.ps1')
if ($LockProbe) {
    $probe = Open-QALock $ProbeDirectory
    if ($null -eq $probe) { 'busy' } else { $probe.Dispose(); 'free' }
    exit 0
}

$script:TestDirectory = Join-Path ([IO.Path]::GetTempPath()) ('antony-scheduler-test-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($script:TestDirectory) | Out-Null
$script:Passed = 0
function Assert-True($Condition, [string]$Message) { if (-not $Condition) { throw ('assertion_failed: ' + $Message) } }
function Assert-Equal($Actual, $Expected, [string]$Message) { if ($Actual -cne $Expected) { throw ('assertion_failed: ' + $Message + ' actual=' + $Actual + ' expected=' + $Expected) } }
function New-Fixture {
    $directory = Join-Path $script:TestDirectory ([guid]::NewGuid().ToString('N'))
    return @{ Workspace = $script:TestDirectory; StateDirectory = $directory; IntervalSeconds = 300; RetrySeconds = 30; CodexPath = 'never-run-codex.exe' }
}
function New-Result([string]$Id, [string]$Status = 'completed') {
    return @{ check_id = $Id; status = $Status; reviewed_heads = @(); posted_urls = @(); summary = 'Synthetic QA finished.' }
}
function New-Snapshot([string]$Hash) { return @{ fingerprint = $Hash; entries = @(@{ id = 'pr:92'; hash = $Hash }) } }
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
        Assert-Equal (Invoke-QAController $o -Once -Snapshot $snapshot -Worker $worker -Now $now) 'completed' 'first unknown baseline invokes worker'
        $s = Get-QAState $o.StateDirectory
        Assert-Equal ([DateTimeOffset]::Parse($s.next_due_utc).ToString('HH:mm:ss')) '10:07:00' 'interval starts after two-minute worker'
        $sleep = { param($seconds) $clock.sleeps += $seconds; $clock.now = $clock.now.AddSeconds($seconds) }.GetNewClosure()
        Invoke-QAController $o -Snapshot $snapshot -Worker $worker -Now $now -Sleep $sleep -MaxIterations 61 | Out-Null
        Assert-Equal $clock.calls 1 'quiet unchanged snapshot never invokes Codex'
        Assert-Equal $clock.sleeps.Count 60 'respects full completion-based delay'
        Assert-Equal (Get-QAState $o.StateDirectory).phase 'idle' 'quiet metadata check finishes idle'
    }

    Test-Case 'failed_preflight_and_worker_do_not_acknowledge' {
        $o = New-Fixture
        $snap = { param($options, $state) New-Snapshot 'A' }
        $ok = { param($options, $id, $started) New-Result $id }
        Invoke-QAController $o -Once -Snapshot $snap -Worker $ok | Out-Null
        $failedGh = { param($options, $state) throw 'synthetic-gh-error' }
        $forbidden = { throw 'worker_must_not_be_invoked' }
        Assert-Equal (Invoke-QAController $o -Once -Snapshot $failedGh -Worker $forbidden) 'github_failed' 'GH failure is contained'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'GH failure retains cursor'
        $new = { param($options, $state) New-Snapshot 'B' }
        $failedWorker = { param($options, $id, $started) New-Result $id 'failed' }
        Assert-Equal (Invoke-QAController $o -Once -Snapshot $new -Worker $failedWorker) 'worker_failed' 'failed worker is retried later'
        $s = Get-QAState $o.StateDirectory
        Assert-Equal $s.acknowledged_fingerprint 'A' 'failed worker retains cursor'
        Assert-Equal (([DateTimeOffset]::Parse($s.next_due_utc) - [DateTimeOffset]::Parse($s.last_finished_utc)).TotalSeconds) 30 'failure uses retry delay'
        Invoke-QAController $o -Once -Snapshot $new -Worker $ok | Out-Null
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'B' 'later success acknowledges pending snapshot'
    }

    Test-Case 'new_updates_during_work_remain_pending' {
        $o = New-Fixture
        $observed = @{ hash = 'A'; workers = 0 }
        $snapshot = { param($options, $state) New-Snapshot $observed.hash }.GetNewClosure()
        $worker = { param($options, $id, $started) $observed.workers++; $observed.hash = 'B'; New-Result $id }.GetNewClosure()
        Invoke-QAController $o -Once -Snapshot $snapshot -Worker $worker | Out-Null
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'only prelaunch A is acknowledged'
        Invoke-QAController $o -Once -Snapshot $snapshot -Worker $worker | Out-Null
        Assert-Equal $observed.workers 2 'B arriving during A triggers next worker'
    }

    Test-Case 'one_controller_across_processes_and_stop_waits_for_worker' {
        $o = New-Fixture
        $observed = @{ finished = $false; nested = ''; child = '' }
        $testPath = $PSCommandPath
        $worker = {
            param($options, $id, $started)
            [IO.File]::WriteAllText((Join-Path $options.StateDirectory 'stop'), '')
            $observed.nested = Invoke-QAController $options -Once -Snapshot { throw 'no_snapshot' }
            $process = Invoke-QANative (Join-Path $PSHOME 'powershell.exe') @('-NoLogo', '-NoProfile', '-File', $testPath, '-LockProbe', '-ProbeDirectory', $options.StateDirectory) $script:TestDirectory -Capture
            Assert-Equal $process.exit_code 0 'child lock probe succeeds'
            $observed.child = $process.output.Trim()
            $observed.finished = $true
            New-Result $id
        }.GetNewClosure()
        Assert-Equal (Invoke-QAController $o -Snapshot { param($options, $state) New-Snapshot 'A' } -Worker $worker -MaxIterations 3) 'stopped' 'stop observed after worker returns'
        Assert-True $observed.finished 'worker finishes after stop request'
        Assert-Equal $observed.nested 'already_running' 'second controller is skipped'
        Assert-Equal $observed.child 'busy' 'exclusive lock also excludes another process'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'finished worker acknowledged before stop'
    }

    Test-Case 'pause_and_orphan_launch_fail_closed' {
        $o = New-Fixture
        Initialize-QADirectory $o.StateDirectory
        [IO.File]::WriteAllText((Join-Path $o.StateDirectory 'pause'), '')
        Assert-Equal (Invoke-QAController $o -Once -Snapshot { throw 'no_snapshot' }) 'paused' 'pause is observed before GH or worker'
        [IO.File]::Delete((Join-Path $o.StateDirectory 'pause'))
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ phase = 'launching'; launch_id = ('a' * 32) }
        Assert-Equal (Invoke-QAController $o -Once -Snapshot { throw 'no_snapshot' }) 'recovery_required' 'unknown spawned PID never launches a duplicate'
        $self = Get-Process -Id $PID
        Write-QAJson (Join-Path $o.StateDirectory 'state.json') @{ phase = 'working'; launch_id = ('b' * 32); worker_pid = $PID; worker_started_utc = $self.StartTime.ToUniversalTime().ToString('o') }
        Assert-Equal (Invoke-QAController $o -Once -Snapshot { throw 'no_snapshot' }) 'worker_still_running' 'PID plus creation time guards orphan worker'
    }

    Test-Case 'unique_check_ids_and_safe_result_validation' {
        $o = New-Fixture
        $observed = @{ ids = @() }
        $worker = { param($options, $id, $started) $observed.ids += $id; New-Result $id 'blocked' }.GetNewClosure()
        Invoke-QAController $o -Once -Snapshot { param($options, $state) New-Snapshot 'A' } -Worker $worker | Out-Null
        Invoke-QAController $o -Once -Snapshot { param($options, $state) New-Snapshot 'B' } -Worker $worker | Out-Null
        Assert-Equal $observed.ids.Count 2 'blocked work is acknowledged without retry loops'
        Assert-True ($observed.ids[0] -cmatch '^[a-f0-9]{32}$') 'run id is 32 hex'
        Assert-True ($observed.ids[0] -cne $observed.ids[1]) 'result paths cannot reuse run ids'
        $stale = { param($options, $id, $started) New-Result ('a' * 32) }
        Assert-Equal (Invoke-QAController $o -Once -Snapshot { param($options, $state) New-Snapshot 'C' } -Worker $stale) 'worker_failed' 'a stale result cannot acknowledge C'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'B' 'mismatched check id preserves cursor'
        $stored = [IO.File]::ReadAllText((Join-Path $o.StateDirectory 'state.json'))
        Assert-True (-not $stored.Contains('Synthetic QA finished.')) 'worker summary is never persisted'
        Assert-Equal ([IO.File]::ReadAllText((Join-Path $o.StateDirectory '.gitignore'))) "*`n" 'state ignores itself before writes'
    }

    Test-Case 'metadata_fingerprint_owner_only_ci_and_exact_self_post_suppression' {
        $notes = New-Object 'System.Collections.Generic.List[object]'
        $data = @{ notes = $notes; conclusion = $null; updated = 'time-one'; body = 'TL: synthetic incoming work'; fork = $false; login = 'Khumoyun1307'; labels = @(@{ name = 'needs-qa' }); branch = 'qa/scenarios'; draft = $false }
        $gh = {
            param($path, $paged)
            if ($path -eq 'user') { @{ login = $data.login } }
            elseif ($path -match '/pulls\?') {
                @{ number = 92; user = @{ login = 'Khumoyun1307' }; head = @{ sha = ('a' * 40); ref = $data.branch; repo = @{ full_name = $(if ($data.fork) { 'outsider/fork' } else { 'Khumoyun1307/personal-ai-assistant' }) } }; base = @{ sha = ('b' * 40); ref = 'int/m05' }; draft = $data.draft; labels = $data.labels; updated_at = $data.updated }
            } elseif ($path -match '/check-runs\?') { @{ check_runs = @(@{ id = 7; name = 'CI'; status = 'completed'; conclusion = $data.conclusion }) } }
            elseif ($path -match '/status\?') { @{ state = 'success'; statuses = @() } }
            elseif ($path -match '/issues\?') { @() }
            elseif ($path -match '/issues/43$') { @{ number = 43; title = 'Synthetic integration'; body = $data.body; user = @{ login = 'Khumoyun1307' }; state = 'open'; labels = @() } }
            elseif ($path -match '/issues/92/comments') { $data.notes.ToArray() }
            elseif ($path -match '/comments\?|/reviews\?') { @() }
            else { throw ('unexpected_fake_endpoint:' + $path) }
        }.GetNewClosure()
        $before = Get-QASnapshot @{} @{} -Gh $gh
        $data.updated = 'time-two'
        Assert-Equal (Get-QASnapshot @{} @{} -Gh $gh).fingerprint $before.fingerprint 'updatedAt cannot trigger work'
        $data.conclusion = 'success'
        $green = Get-QASnapshot @{} @{} -Gh $gh
        Assert-True ($green.fingerprint -ne $before.fingerprint) 'same-head CI completion wakes review'
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

    Test-Case 'native_worker_stdin_result_and_failure_boundaries' {
        $o = New-Fixture
        $o.Workspace = Join-Path $script:TestDirectory ('worker space ' + [char]0x00a7)
        $promptPath = Join-Path $o.Workspace 'docs/engineering/p2/qa/p2-self-check-prompt.md'
        $schemaPath = Join-Path $o.Workspace 'tools/acceptance/p2_qa_result.schema.json'
        [IO.Directory]::CreateDirectory((Split-Path -Parent $promptPath)) | Out-Null
        [IO.Directory]::CreateDirectory((Split-Path -Parent $schemaPath)) | Out-Null
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
        if (args.Length != 11 || args[0] != "exec" || args[1] != "--approve-for-me" || args[2] != "--cd" || args[4] != "--color" || args[5] != "never" || args[6] != "--output-schema" || args[8] != "--output-last-message" || args[10] != "-") return 41;
        if (!File.Exists(args[7]) || !Directory.Exists(args[3]) || !args[3].Contains("\u00a7") || !prompt.Contains("\u00a7")) return 42;
        string id = Path.GetFileNameWithoutExtension(args[9]).Substring(7);
        if (!prompt.Contains("Check=" + id) || !prompt.Contains("Compose=antony-scheduled") || !prompt.Contains("Workspace=" + args[3]) || prompt.Contains("{{")) return 43;
        Console.WriteLine("synthetic-private-stdout-canary");
        Console.Error.WriteLine("synthetic-private-stderr-canary");
        if (prompt.Contains("Mode=missing")) return 0;
        if (prompt.Contains("Mode=stale")) id = new String('f', 32);
        string result = "{\"check_id\":\"" + id + "\",\"status\":\"completed\",\"reviewed_heads\":[],\"posted_urls\":[],\"summary\":\"Synthetic \u00a7\"}";
        if (prompt.Contains("Mode=malformed")) result = "invalid synthetic JSON";
        File.WriteAllText(args[9], result, new UTF8Encoding(false));
        return prompt.Contains("Mode=nonzero") ? 13 : 0;
    }
}
'@
        Add-Type -TypeDefinition $source -OutputAssembly $exe -OutputType ConsoleApplication
        $o.CodexPath = $exe
        $template = "Check={{CHECK_ID}}`nWorkspace={{WORKSPACE}}`nCompose={{COMPOSE_PROJECT}}`nState={{STATE_DIRECTORY}}`nUnicode=" + [char]0x00a7 + "`nMode="
        [IO.File]::WriteAllText($promptPath, ($template + 'valid'), (New-Object Text.UTF8Encoding($false)))
        Assert-Equal (Invoke-QAController $o -Once -Snapshot { param($options, $state) New-Snapshot 'A' }) 'completed' 'real launcher runs fake executable with correct stdin/argv/UTF8'
        Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'valid native final result acknowledged'
        foreach ($mode in @('stale', 'missing', 'nonzero', 'malformed')) {
            [IO.File]::WriteAllText($promptPath, ($template + $mode), (New-Object Text.UTF8Encoding($false)))
            Assert-Equal (Invoke-QAController $o -Once -Snapshot { param($options, $state) New-Snapshot 'B' }) 'worker_failed' ('native result refused: ' + $mode)
            Assert-Equal (Get-QAState $o.StateDirectory).acknowledged_fingerprint 'A' 'failed native result retains previous cursor'
            Assert-Equal @(Get-ChildItem -LiteralPath $o.StateDirectory -Filter 'result-*.json').Count 0 'temporary final message removed even on failure'
        }
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
