# Task runner. Runs on the Windows host; every recipe except up/down/hooks
# executes inside the dev container (AGENTS §4).
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

compose := "docker compose -f compose.dev.yml"
run := compose + " run --rm app"

# List the available recipes.
default:
    @just --list

# First-time setup: create .env, build the image, install dependencies.
setup:
    @if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env from .env.example - fill in real values before connecting a bot." }
    {{ compose }} build
    {{ run }} uv sync
    @Write-Host "Setup complete. Next: just up, then just check"

# Start the dev stack (app idles; db serves).
up:
    {{ compose }} up -d db
    @Write-Host "Database up. Run commands with: just test / just check"

# Stop the dev stack (data volumes are kept).
down:
    {{ compose }} down

# Stop the dev stack and delete its volumes, including the database.
down-hard:
    {{ compose }} down -v

# Full Definition-of-Done gate: lint, format, types, layering, tests.
check: lint typecheck layers test

# Lint and format check.
lint:
    {{ run }} uv run ruff check .
    {{ run }} uv run ruff format --check .

# Apply formatting and autofixes.
fmt:
    {{ run }} uv run ruff check --fix .
    {{ run }} uv run ruff format .

# Static types (strict on src/).
typecheck:
    {{ run }} uv run mypy src evals

# Import/layer contracts.
layers:
    {{ run }} uv run lint-imports

# Run the test suite. Extra arguments are passed to pytest.
test *args:
    {{ run }} uv run pytest {{ args }}

# Validate configuration and reach the database.
healthcheck:
    {{ run }} uv run python -m assistant healthcheck

# Apply database migrations (from S01.1).
migrate:
    {{ run }} uv run python -m assistant migrate

# Re-key retained messages after a search-normalizer change (S03.1); idempotent. Pass --force to re-key anyway.
reindex-search *args:
    {{ run }} uv run python -m assistant reindex-search {{ args }}

# Run the assistant (from S01.2).
run:
    {{ compose }} run --rm --service-ports app uv run python -m assistant run

# Run the M00 Telegram probe with the DEV bot's token in .env (never Alviro: Alviro is production and only the server polls it; owner or agent per AGENTS §9).
probe *args:
    {{ run }} python tools/probe/probe.py {{ args }}

# One-off non-polling probe call (`me`, `getfile`) with Alviro's token from var/prod.env; needs the owner's go-ahead per run (D40, AGENTS §9).
probe-prod *args:
    {{ run }} python tools/probe/probe.py --prod-token {{ args }}

# Step D (D40): move the main account's id into var/prod.env, put the dev bot into .env, then recreate the dev database. Every run drops the dev database; the venv and uv cache are kept.
dev-bot-switch:
    {{ compose }} run --rm --no-deps app python tools/dev/switch_dev_bot.py
    {{ compose }} down
    docker volume rm -f assistant-dev_db-data
    {{ compose }} up -d db
    {{ run }} uv run python -m assistant migrate

# Sanitize a probe dump into committable fixtures (owner or agent per AGENTS §9).
sanitize input output="tests/fixtures/telegram":
    {{ run }} python tools/probe/sanitize.py --input {{ input }} --output {{ output }}

# Dependency vulnerability audit.
audit:
    {{ run }} uv run pip-audit

# Open a shell in the dev container.
shell:
    {{ run }} bash

# Install the git hooks (gitleaks + ruff before every commit).
hooks:
    git config core.hooksPath .githooks
    @Write-Host "Hooks installed (core.hooksPath=.githooks)."

# ---------------------------------------------------------------- production
# Oracle Cloud host (runbooks/provision.md). Every recipe below runs its bash
# script inside the official OCI CLI image (it ships bash, ssh, jq, python3),
# so the host needs only Docker — on Windows, Linux or macOS alike. The
# server is reached as `deploy@<ip>` with the agent key in var/cloud/ssh;
# its checkout lives in /opt/assistant and tracks `main` (read-only deploy
# key). Server-side steps are deploy/server.sh. Nothing prints a secret.
oci_image := "ghcr.io/oracle/oci-cli:20261007"
cloud_dir := justfile_directory() / "var" / "cloud"
prod_env := justfile_directory() / "var" / "prod.env"
repo_slug := "Khumoyun1307/personal-ai-assistant"
_ops := 'docker run --rm -i --user 0 --mount "type=bind,src=' + justfile_directory() + ',dst=/work" --mount "type=bind,src=' + cloud_dir + ',dst=/cloud" -w /work -e CLOUD_DIR=/cloud'
ops := _ops + " --entrypoint bash " + oci_image
oci_ops := _ops + ' --mount "type=bind,src=' + cloud_dir / "oci" + ',dst=/oci,readonly" -e OCI_CLI_CONFIG_FILE=/oci/config --entrypoint bash ' + oci_image

# Create or reconcile the Oracle Cloud resources (idempotent). Flags: --wait (retry capacity for hours), --dry-run.
oci-provision email *flags:
    {{ oci_ops }} deploy/oci/provision.sh --email "{{ email }}" {{ flags }}

# Generate the backup key pair once (var/cloud/age-identity.txt); prints only the public key.
prod-age-key:
    docker build -q -t assistant-backup:prod deploy/backup
    docker run --rm --user 0 --mount "type=bind,src={{ justfile_directory() }},dst=/work" --mount "type=bind,src={{ cloud_dir }},dst=/cloud" -e CLOUD_DIR=/cloud -w /work --entrypoint bash assistant-backup:prod deploy/oci/age-key.sh

# Prepare the new host: cloud-init done, data volume mounted, read-only deploy key, checkout.
prod-bootstrap ip:
    {{ ops }} deploy/oci/bootstrap-host.sh {{ ip }} prepare
    -gh repo deploy-key add "{{ cloud_dir }}/server-deploy-key.pub" --repo {{ repo_slug }} --title alviro-server
    {{ ops }} deploy/oci/bootstrap-host.sh {{ ip }} clone

# Assemble the server .env from var/prod.env + var/cloud/* and stream it to the server (mode 600). Owner ids default to OWNER_TELEGRAM_IDS in var/prod.env.
prod-env-push ip owner_ids="":
    {{ _ops }} --mount "type=bind,src={{ prod_env }},dst=/prod.env,readonly" -e PROD_ENV=/prod.env --entrypoint bash {{ oci_image }} deploy/oci/push-env.sh {{ ip }} {{ if owner_ids == "" { "" } else { "--owner-ids " + owner_ids } }}

# Deploy the current `main` to the server: pull, rebuild, migrate, restart.
deploy ip:
    {{ ops }} deploy/oci/ssh.sh {{ ip }} deploy

# The last production app log lines (content-free by construction, AGENTS §9).
prod-logs ip lines="200":
    {{ ops }} deploy/oci/ssh.sh {{ ip }} logs {{ lines }}

# Production health: containers, healthcheck, backup and off-site upload markers, data dir.
prod-status ip:
    {{ ops }} deploy/oci/ssh.sh {{ ip }} status

# Take a backup now (otherwise daily at BACKUP_HOUR_UTC). Exit 3: dump ok, off-site upload failed.
prod-backup ip:
    {{ ops }} deploy/oci/ssh.sh {{ ip }} backup

# R0 acceptance verifier against the production database (tools/acceptance/r0-session.md).
prod-verify-r0 ip *args:
    {{ ops }} deploy/oci/ssh.sh {{ ip }} verify-r0 {{ args }}

# Restore the newest dump into a scratch database with the offline key (var/cloud/age-identity.txt, streamed via stdin).
prod-restore-test ip:
    {{ _ops }} -e SSH_STDIN_FILE=/cloud/age-identity.txt --entrypoint bash {{ oci_image }} deploy/oci/ssh.sh {{ ip }} restore-test

# Replace the production database with one dump (app stopped first; runbooks/restore.md §B).
prod-restore ip file:
    {{ _ops }} -e SSH_STDIN_FILE=/cloud/age-identity.txt --entrypoint bash {{ oci_image }} deploy/oci/ssh.sh {{ ip }} restore {{ file }}

# Any other deploy/server.sh action: offsite-list, post-restore <ts>, app-stop, app-start, restart-timing, reboot, reindex-search, nfr04 [days], nfr05 [days].
prod ip +args:
    {{ ops }} deploy/oci/ssh.sh {{ ip }} {{ args }}

# Backup smoke test of the production stack: separate Compose project, placeholder secrets, removed afterwards.
[unix]
smoke-backup:
    bash deploy/smoke/backup-smoke.sh

# Backup smoke test of the production stack: separate Compose project, placeholder secrets, removed afterwards.
[windows]
smoke-backup:
    & "$env:ProgramFiles\Git\bin\bash.exe" deploy/smoke/backup-smoke.sh
