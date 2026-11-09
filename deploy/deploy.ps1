# Laptop-only release selection. gh credentials stay on the laptop; the OCI
# container receives only the selected SHA and the existing SSH key mount.
param(
    [Parameter(Mandatory = $true)][string]$Ip,
    [string]$Sha = '',
    [string]$Flags = '',
    [string]$OpsImage = 'ghcr.io/oracle/oci-cli:20261007',
    [string]$Repository = 'Khumoyun1307/personal-ai-assistant'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    $skipCi = $false
    # With just's optional positional SHA, the emergency flag may occupy it.
    if ($Sha -eq '--skip-ci-check') { $Sha = ''; $skipCi = $true }
    foreach ($flag in ($Flags -split '\s+' | Where-Object { $_ })) {
        if ($flag -ne '--skip-ci-check') { throw "Unknown deploy flag: $flag" }
        $skipCi = $true
    }
    if ($Sha -and $Sha -cnotmatch '^[0-9a-fA-F]{7,40}$') {
        throw 'SHA must contain 7 to 40 hexadecimal characters.'
    }
    $repoRoot = Split-Path -Parent $PSScriptRoot
    & git -C $repoRoot fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main'
    if ($LASTEXITCODE -ne 0) { throw 'Could not fetch origin/main.' }
    $revision = if ($Sha) { $Sha } else { 'origin/main' }
    $resolved = & git -C $repoRoot rev-parse --verify "$revision^{commit}"
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the release commit.' }
    $selected = "$resolved".Trim()
    if ($selected -cnotmatch '^[0-9a-f]{40}$') { throw 'Git did not return one full commit SHA.' }
    & git -C $repoRoot merge-base --is-ancestor $selected origin/main
    if ($LASTEXITCODE -ne 0) { throw 'Selected SHA is not an ancestor of origin/main.' }
    Write-Host "Selected release: $selected"
    # Pre-protocol commits cannot select immutable images or record rollback
    # state. Fail before the server checkout is changed, even in emergencies.
    $protocol = & git -C $repoRoot show "${selected}:deploy/release.sh"
    if ($LASTEXITCODE -ne 0 -or $protocol -cnotcontains '# Release protocol: 1') {
        throw 'Selected commit predates the release protocol; use a forward fix on main.'
    }
    if ($skipCi) {
        Write-Warning "EMERGENCY: skipping all CI verification for $selected (--skip-ci-check). Record the incident and reason."
    } else {
        # Only the latest main push run of this workflow can authorize a
        # release. PR checks for a merge ref or a different SHA cannot do so.
        $runsJson = & gh run list --repo $Repository --workflow ci.yml --branch main --event push `
            --commit $selected --limit 1 --json databaseId,headSha
        if ($LASTEXITCODE -ne 0) { throw 'Could not query CI with gh.' }
        $runs = @($runsJson | ConvertFrom-Json)
        if ($runs.Count -ne 1 -or $runs[0].headSha -cne $selected) {
            throw 'No CI main push run exists for this exact SHA.'
        }
        $runJson = & gh run view $runs[0].databaseId --repo $Repository --json headSha,status,conclusion,jobs
        if ($LASTEXITCODE -ne 0) { throw 'Could not read the CI run with gh.' }
        $run = $runJson | ConvertFrom-Json
        if ($run.headSha -cne $selected) { throw 'CI run SHA does not match the selected release.' }
        $required = @('lint', 'tests (1)', 'tests (2)', 'tests (3)', 'tests (4)', 'tests (5)', 'deploy-scripts', 'images', 'check')
        foreach ($name in $required) {
            $jobs = @($run.jobs | Where-Object { $_.name -ceq $name })
            if ($jobs.Count -ne 1) { throw "Required CI job is missing or ambiguous: $name" }
            if ($jobs[0].status -cne 'completed' -or $jobs[0].conclusion -cne 'success') {
                throw "Required CI job did not succeed: $name"
            }
        }
        if ($run.status -cne 'completed' -or $run.conclusion -cne 'success') {
            throw 'CI run has not completed successfully.'
        }
        Write-Host 'Verified lint, five test shards, deploy-scripts, images and check for the selected SHA.'
    }
    $cloudDir = Join-Path $repoRoot 'var/cloud'
    & docker run --rm -i --user 0 --mount "type=bind,src=$repoRoot,dst=/work" `
        --mount "type=bind,src=$cloudDir,dst=/cloud" -w /work -e CLOUD_DIR=/cloud `
        --entrypoint bash $OpsImage deploy/oci/ssh.sh $Ip deploy $selected
    if ($LASTEXITCODE -ne 0) { throw 'Server deployment failed; inspect prod-status and the release runbook.' }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
exit 0
