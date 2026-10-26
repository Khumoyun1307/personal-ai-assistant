# Deploy (M02 S02.5, S02.6)

One path (ARCHITECTURE §15.4): the server's checkout (`/opt/assistant`)
tracks `main` through a read-only deploy key; a deploy is a fast-forward
pull, an image rebuild and a restart. Migrations run first (`migrate`
service); the app starts only after they succeeded. The images are built on
the server itself (arm64; CI proves both architectures build).

```powershell
just deploy <ip>          # pull --ff-only, compose up -d --build, ps
just prod-status <ip>     # containers, healthcheck, backup + upload markers, data dir, memory
just prod-logs <ip>       # last 200 app log lines, content-free (AGENTS §9)
just prod-backup <ip>     # a backup now (exit 3: dump ok, off-site upload failed)
just prod <ip> restart-timing
```

The recipes run `deploy/server.sh <action>` on the server over SSH from the
OCI CLI container (agent key in `var/cloud/ssh`), so the laptop needs only
Docker. On the server every compose command is
`docker compose --env-file .env -f deploy/compose.prod.yml …`: the project
directory is `deploy/`, so without `--env-file .env` the root `.env` would
not be used for `${...}` interpolation.

Rules:

- Deploy only what is on `main` with CI green (AGENTS §15). Never edit files
  on the server (the `.env` comes from `just prod-env-push`); never
  `git pull` a branch.
- A migration that drops or rewrites content needs a backup taken first
  (`just prod-backup <ip>`) and a data-handling note in the commit.
- The app exits non-zero when a role dies and Compose restarts it; a crash
  loop shows in `just prod-status`. Roll back by reverting on `main` and
  deploying again (`git checkout <sha>` on the server is *not* the path).
- After a deploy, confirm in the control chat that `/status` answers and the
  heartbeat monitor is still receiving pings.
- **Search re-key (S03.1 onwards).** When a deploy changes the search
  normalizer (`NORMALIZER_VERSION`, ARCHITECTURE §11.2 — first time: the
  S03.1 deploy, v1 → v2), `just prod-status <ip>` prints a healthcheck
  `WARNING: search keys were built with normalizer v…`. Run
  `just prod <ip> reindex-search` once after the deploy: it re-keys every
  retained message in short batches beside the running app (an edit made
  meanwhile always wins), records the new version and prints counts only.
  Running it again is a no-op. Locally: `just reindex-search`.
- **Answer times (NFR-04, M03 onwards).** `just prod <ip> nfr04 [days]`
  (default 30) prints, per class of owner request (simple answer, catch-up,
  attention list, all), the count, p50/p95 of the run and of the wait from
  the owner's message to the finished reply, the p95 limit and a verdict.
  It reads `deploy/sql/nfr04.sql` in a read-only transaction: timings, run
  types, statuses and tool names only, never a content column.
- A `backup_upload_stale` alert means dumps are produced but no longer reach
  B2: run `just prod-backup <ip>` and read its log lines (`off-site sync of
  … failed` / `… refused`); see `restore.md` §C for the delete guard —
  after an outage of more than `BACKUP_MAX_REMOTE_DELETE` days (default 7)
  every later sync is refused until the one-off override is run.
- Reboots are planned (unattended upgrades never reboot on their own):
  `just prod <ip> reboot`, then `just prod-status <ip>`.
