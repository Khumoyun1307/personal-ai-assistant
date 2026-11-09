# Runs on CI's PowerShell; every external command is a fake function.
$ErrorActionPreference = 'Stop'
# Negative cases deliberately return nonzero from the child PowerShell.
$PSNativeCommandUseErrorActionPreference = $false
Set-StrictMode -Version Latest
$target = Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy.ps1'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('release-smoke-' + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $scratch
$oldCalls = $env:RELEASE_SMOKE_CALLS
try {
    $harness = Join-Path $scratch 'harness.ps1'
    @'
param([string]$Target, [string]$Scenario, [string]$Release = '', [string]$Flags = '')
$global:SmokeScenario = $Scenario
$global:SmokeSha = if ($Release -and $Release -ne '--skip-ci-check') { 'b' * 40 } else { 'a' * 40 }
function global:git {
    Add-Content -LiteralPath $env:RELEASE_SMOKE_CALLS -Value "git $args"
    $global:LASTEXITCODE = 0
    switch ($args[2]) {
        fetch { if ($global:SmokeScenario -eq 'git-error') { $global:LASTEXITCODE = 1 } }
        rev-parse { $global:SmokeSha }
        merge-base { if ($global:SmokeScenario -eq 'foreign') { $global:LASTEXITCODE = 1 } }
        show {
            if ($global:SmokeScenario -eq 'old-protocol') { '# old release' }
            else { '# Release protocol: 1' }
        }
        default { throw "unexpected git command: $args" }
    }
}
function global:gh {
    Add-Content -LiteralPath $env:RELEASE_SMOKE_CALLS -Value "gh $args"
    $global:LASTEXITCODE = 0
    if ($global:SmokeScenario -eq 'gh-error') { $global:LASTEXITCODE = 1; return }
    if ($global:SmokeScenario -eq 'malformed') { '{bad json'; return }
    if ($args[1] -eq 'list') {
        if ($global:SmokeScenario -eq 'no-run') { '[]'; return }
        ConvertTo-Json -Compress -InputObject @(@{ databaseId = 123; headSha = $global:SmokeSha })
        return
    }
    if ($args[1] -ne 'view') { throw "unexpected gh command: $args" }
    $jobs = @()
    foreach ($name in @('lint', 'tests (1)', 'tests (2)', 'tests (3)', 'tests (4)', 'tests (5)', 'deploy-scripts', 'images', 'check')) {
        if ($global:SmokeScenario -eq "missing:$name") { continue }
        $job = @{ name = $name; status = 'completed'; conclusion = 'success' }
        if ($global:SmokeScenario -eq "failed:$name") { $job.conclusion = 'failure' }
        if ($global:SmokeScenario -eq "pending:$name") { $job.status = 'in_progress'; $job.conclusion = '' }
        if ($global:SmokeScenario -eq "skipped:$name") { $job.conclusion = 'skipped' }
        $jobs += $job
        if ($global:SmokeScenario -eq "duplicate:$name") { $jobs += $job }
    }
    $sha = if ($global:SmokeScenario -eq 'wrong-sha') { 'c' * 40 } else { $global:SmokeSha }
    $status = if ($global:SmokeScenario -eq 'run-pending') { 'in_progress' } else { 'completed' }
    @{ headSha = $sha; status = $status; conclusion = 'success'; jobs = $jobs } | ConvertTo-Json -Depth 4 -Compress
}
function global:docker {
    Add-Content -LiteralPath $env:RELEASE_SMOKE_CALLS -Value "docker $args"
    $global:LASTEXITCODE = 0
}
& $Target -Ip '203.0.113.10' -Sha $Release -Flags $Flags
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath $harness -Encoding utf8

    function Assert-ReleaseCase([string]$Scenario, [bool]$Success, [string]$Release = '', [string]$Flags = '') {
        $env:RELEASE_SMOKE_CALLS = Join-Path $scratch 'calls'
        Set-Content -LiteralPath $env:RELEASE_SMOKE_CALLS -Value ''
        $output = & pwsh -NoLogo -NoProfile -File $harness -Target $target -Scenario $Scenario -Release $Release -Flags $Flags 2>&1
        $code = $LASTEXITCODE
        # Keep the child's result for assertions without leaking expected
        # failures into the CI PowerShell wrapper's final exit status.
        $global:LASTEXITCODE = 0
        $calls = Get-Content -LiteralPath $env:RELEASE_SMOKE_CALLS
        $dockerCalls = @($calls | Where-Object { $_ -like 'docker *' })
        if (($code -eq 0) -ne $Success) { throw "Unexpected result for ${Scenario}: $output" }
        if ($Success) {
            if ($dockerCalls.Count -ne 1) { throw "No single deploy for $Scenario" }
            $expected = if ($Release -and $Release -ne '--skip-ci-check') { 'b' * 40 } else { 'a' * 40 }
            if (-not $dockerCalls[0].EndsWith("deploy $expected")) { throw "Unpinned deploy: $dockerCalls" }
            if ("$output" -notlike "*Selected release: $expected*") { throw 'Selected SHA was not printed' }
        } elseif ($dockerCalls.Count -ne 0) { throw "Failed preflight reached Docker: $Scenario" }
        if ($Flags -eq '--skip-ci-check' -or $Release -eq '--skip-ci-check') {
            if (@($calls | Where-Object { $_ -like 'gh *' }).Count -ne 0) { throw 'Skip still called gh' }
            if ($Success -and "$output" -notlike '*EMERGENCY*') { throw 'Emergency warning missing' }
        }
    }
    Assert-ReleaseCase 'ok' $true
    Assert-ReleaseCase 'ok' $true ('b' * 12)
    foreach ($job in @('lint', 'tests (1)', 'tests (2)', 'tests (3)', 'tests (4)', 'tests (5)', 'deploy-scripts', 'images', 'check')) {
        foreach ($problem in @('missing', 'failed', 'pending', 'skipped', 'duplicate')) {
            Assert-ReleaseCase "${problem}:$job" $false
        }
    }
    foreach ($problem in @('no-run', 'wrong-sha', 'run-pending', 'malformed', 'gh-error', 'git-error', 'foreign', 'old-protocol')) {
        Assert-ReleaseCase $problem $false
    }
    Assert-ReleaseCase 'ok' $false 'main'
    Assert-ReleaseCase 'ok' $false '' '--unknown'
    Assert-ReleaseCase 'ok' $true '--skip-ci-check'
    Assert-ReleaseCase 'ok' $true ('b' * 40) '--skip-ci-check'
    Assert-ReleaseCase 'foreign' $false '' '--skip-ci-check'
    Assert-ReleaseCase 'old-protocol' $false '' '--skip-ci-check'
    Write-Host 'ok: pinned release selection and fail-closed CI gate (fake commands only)'
} finally {
    $env:RELEASE_SMOKE_CALLS = $oldCalls
    Remove-Item -LiteralPath $scratch -Recurse -Force
}
exit 0
