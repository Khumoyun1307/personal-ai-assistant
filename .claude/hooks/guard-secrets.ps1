# PreToolUse guard for Claude Code (AGENTS.md §9): blocks shell commands that
# would read a secret file, anything under var/, or print the environment.
# Exit 2 blocks the call and shows the message to the agent. Any error while
# inspecting the command also blocks (fail closed).
$ErrorActionPreference = 'Stop'
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $cmd = [string]$payload.tool_input.command
    if (-not $cmd) { exit 0 }

    # Existence checks name a path without reading it.
    $scan = [regex]::Replace($cmd, '(?i)(test\s+-[ef]|Test-Path(\s+-Path)?)\s+("[^"]*"|''[^'']*''|\S+)', ' ')

    $patterns = @(
        '(?i)\.env(?!\.example)\b',                         # .env, prod.env, b2.env, .env.local
        '(?i)(^|[\s"''=]|\.[\\/]|Agent[\\/])var[\\/]',       # the repo's var/ folder
        '(?i)docker(-|\s+)compose\b[^|;&]*\bconfig\b',      # prints interpolated secrets
        '(?i)\bprintenv\b',
        '(?i)(^|[;&|(]\s*)env(\s*$|\s*[;&|)])',             # bare `env`
        '(?i)\b(Get-ChildItem|gci|dir|ls)\s+env:'
    )
    foreach ($p in $patterns) {
        if ($scan -match $p) {
            [Console]::Error.WriteLine("Blocked by .claude/hooks/guard-secrets.ps1: the command touches a secret file, var/, or prints the environment (AGENTS.md section 9). To check that a file exists, use: test -e <path>")
            exit 2
        }
    }
    exit 0
} catch {
    [Console]::Error.WriteLine('guard-secrets: could not inspect the command; blocked to be safe.')
    exit 2
}
