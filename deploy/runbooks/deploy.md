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
- **Triage and alert latency (NFR-05, M05 onwards).** `just prod <ip> nfr05
  [days]` (default 1) prints three rows: triage completion (finished run
  minus the newest message it read; p95 limit 90 s), urgent alert delivery
  (sent alert minus its newest referenced message; p95 limit 120 s), and the
  share of immediate alerts the owner marked "not useful" (informational,
  PRD R2 #7), each with a count and a verdict. It reads
  `deploy/sql/nfr05.sql` in a read-only transaction: timestamps, ids, run
  types, kinds, statuses and feedback codes only, never a content column.

- **P2 verification and daily trial audit.** `just prod <ip> p2-verify release` reports the
  schema, flag names and action counts. Other modes are `actions --since
  <ISO timestamp with timezone>`, `action <id>`, `script P2-4 --expect 401-409`,
  `notifications`, `tasks` and `digests` (the last two and notifications also
  accept `--since`). The one-off container mounts the verifier read-only;
  one read-only transaction returns ids, states, counts and times. SQL extracts
  script numbers without returning message text. `just prod <ip> trial-check
  --since <trial start with timezone> [--until <end with timezone>]` checks
  approval-before-claim, matching text hashes, one possible delivery per
  action, no send after a terminal denial, matching recipients and orphan
  sends. The interval includes `since` and excludes `until`; it defaults to
  the Unix epoch through the current UTC time. Definite rate-limited
  non-delivery may be claimed again; uncertain delivery is never retried.
  Each trial day, record the RESULT line and counts in the M04 trial log;
  the owner checks `/activity` and the listed uncertain chats. Any incorrect
  send stops the trial. Exit 0 is clean, 1 reports violations, and 2 rejects
  bad arguments. See [the verifier record](../../docs/engineering/p2/qa/p2-verifier.md)
  for checks and evidence limits. Run only in the planned owner-approved
  server window; the CP3 dry-run and seven trial days remain separate checks.
- A `backup_upload_stale` alert means dumps are produced but no longer reach
  B2: run `just prod-backup <ip>` and read its log lines (`off-site sync of
  … failed` / `… refused`); see `restore.md` §C for the delete guard —
  after an outage of more than `BACKUP_MAX_REMOTE_DELETE` days (default 7)
  every later sync is refused until the one-off override is run.
- Reboots are planned (unattended upgrades never reboot on their own):
  `just prod <ip> reboot`, then `just prod-status <ip>`.
