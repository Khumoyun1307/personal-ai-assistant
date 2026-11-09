# Personal AI assistant (Telegram-native)

A Telegram-native personal AI agent: one bot that is both the owner's private
control chat and a connected business bot on the owner's account. See
[docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) for the vision and
[AGENTS.md](AGENTS.md) for the rules every change must follow.

**Status (2026-11-09):** Stabilization of M02–M05 is approved; M04/M05 acceptance remains open and P3 is on hold. See the maintained [release and privacy record](docs/RELEASE.md) for last recorded production values, merged repairs and owner actions. Two bots: **Alviro** is production-only and polled only by the server; local runs use a separate dev bot (AGENTS §9).

## Development

Everything runs in the dev container. The Windows host needs only Docker
Desktop (WSL2 backend), Git and [`just`](https://github.com/casey/just)
(`winget install Casey.Just`). Never run `uv`, `python`, `pytest` or `alembic`
on the host — native Windows is unsupported (AGENTS §4).

```powershell
just setup      # create .env, build the image, install dependencies
just up         # start PostgreSQL
just check      # lint + format + types + layer contracts + tests
just healthcheck
```

`just` with no arguments lists every recipe.

Local secrets live in `.env` (git-ignored); `.env.example` lists every setting
with a placeholder. Never print `.env` or paste secrets anywhere — verify
configuration with `just healthcheck`, which reports setting *names* only.

## Documentation

| Question | Document |
|---|---|
| Vision and history | [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) |
| What Telegram actually allows | [docs/TELEGRAM_FEASIBILITY.md](docs/TELEGRAM_FEASIBILITY.md) |
| What to build | [docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) |
| How it is built | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| In what order | [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) |
| Current release, privacy and acceptance | [docs/RELEASE.md](docs/RELEASE.md) |
