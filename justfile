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
    {{ run }} uv run mypy src

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

# Run the assistant (from S01.2).
run:
    {{ compose }} run --rm --service-ports app uv run python -m assistant run

# Run the M00 Telegram probe (owner only; needs a real BOT_TOKEN in .env).
probe *args:
    {{ run }} python tools/probe/probe.py {{ args }}

# Sanitize a probe dump into committable fixtures (owner only).
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
