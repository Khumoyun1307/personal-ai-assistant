# Deploy a selected release (stabilization I22)

One path (ARCHITECTURE §15.4): the laptop selects and prints a full commit
SHA, verifies its CI, and passes that SHA to the server. The server fetches
`origin/main`, refuses a commit outside its ancestry, and checks out exactly
the selected commit in detached mode. A newer `main` arriving meanwhile
cannot change the selection. App and backup images are built on the server
(arm64; CI checks both architectures), from git archives of that commit,
excluding untracked files and secrets. Existing release tags are reused,
never rebuilt; a conflicting full-SHA image label refuses the operation.

```powershell
just deploy <ip>          # fetch main on laptop, print selected SHA, verify CI, deploy exactly it
just deploy <ip> <sha>    # select a specific main ancestor (7–40 hex; resolved to full SHA)
just prod-rollback <ip>   # previous recorded images/SHA; no build or migration; identical heads only
just prod-status <ip>     # current/previous SHA, image tags, heads; containers, health, backup markers
just prod-logs <ip>       # last 200 app log lines, content-free (AGENTS §9)
just prod-backup <ip>     # a backup now (exit 3: dump ok, off-site upload failed)
just prod <ip> restart-timing
```

The laptop needs git, authenticated GitHub CLI (`gh`), Docker and PowerShell
(Windows PowerShell is built in; Linux/macOS use `pwsh`). `deploy/deploy.ps1`
queries the latest **main push run of `ci.yml` for that exact SHA**. It
requires one successful, completed job for each of `lint`, `tests (1)` through
`tests (5)`, `deploy-scripts`, `images`, and `check`, plus a successful completed
run. Missing, ambiguous, failed, skipped, cancelled or pending checks, a
different SHA, malformed responses and gh/auth/network errors all refuse
before SSH. PR checks for another merge SHA do not authorize deployment.
CI's aggregate `check` also depends on deployment scripts and image builds.
Documentation-only commits with no CI run fail closed too; there is no
automatic fallback to an older green commit. Select an earlier verified SHA
explicitly or use the emergency process below.

The SSH wrapper runs in the pinned OCI CLI container with the existing key
in `var/cloud/ssh`; gh credentials stay on the laptop. The wrapper bootstraps
the selected server script, so the first upgrade does not execute the old
pull-main deploy path. Deployable commits must include release protocol 1
(`deploy/release.sh`); pre-protocol SHAs require a forward fix, even with CI
verification skipped. Deployment and rollback hold `deploy/.release-lock`
across checkout and restart, refusing concurrent release operations. Tracked
server modifications are refused. On the server every compose command is
`docker compose --env-file .env -f deploy/compose.prod.yml …`: the project
directory is `deploy/`, so without `--env-file .env` the root `.env` would
not be used for `${...}` interpolation.

Release tags are `assistant:<first-12-sha-chars>` and
`assistant-backup:<first-12-sha-chars>`, both labeled with the full SHA.
Compose selects them through `ASSISTANT_IMAGE` and `BACKUP_IMAGE`; mutable
`:prod` tags remain only as defaults for legacy operations and placeholder
smoke stacks. Deploy does not move those aliases or prune images. Keep both
the current and previous release tags: **never run a broad image prune**.
Any later manual cleanup must preserve both recorded releases. This remains
a single-VM build, with no registry or new external service.

After building, deploy records a content-free `deploy/release-in-progress`
marker, stops the app with its existing grace period, waits for a healthy db,
and runs the migration container once. Only successful migration completion
permits app startup. Backup starts first to initialize shared-volume ownership.
After Compose accepts startup, an atomic rename writes `deploy/release-state`:
one tab-separated row for current, then an optional row for previous, each
holding full SHA, app tag, backup tag and sorted Alembic image heads. The
marker is then removed. These local files are git-ignored and are parsed as
data, never sourced as shell commands. Re-deploying the current SHA preserves
the previous release. `prod-status` prints both records and flags an incomplete
transition; these records describe the last completed script transition, not
proof of app health. Verify the healthcheck and owner checks below separately.

Rollback is deliberately narrow: the previous release must be recorded,
its two labeled images must still exist, its image heads must match the
record, and current and previous recorded heads must be identical. An
incomplete transition blocks rollback even if the old recorded pair matches.
Rollback stops the app, checks out the recorded previous SHA and recreates
backup/app with `--no-build --no-deps`, so it never runs migration or rebuilds.
It swaps the records only after Compose startup succeeds. The first release
has no recorded previous artifact; rollback becomes available after a second
distinct release. Settings and secrets are not versioned or restored by this
command; review any configuration change for compatibility before rollback.

If rollback refuses because heads differ, artifacts are missing, or a release
was interrupted, **forward-fix**: keep the app stopped if compatibility is
uncertain (`just prod <ip> app-stop`), inspect content-free status and migration
failure evidence in the approved operations window, create a reviewed fix on
`main` compatible with the current schema, wait for its exact-SHA CI, then
`just deploy <ip> <fixed-sha>`. Do not delete the incomplete marker to bypass
the gate, run a downgrade, or start an old image against a changed schema.
I22 compares recorded image heads only; compatibility/drain design, actual
database-head reconciliation, app health waits, automatic recovery and a
schema-changing failure rehearsal remain **I23**. No schema rollback or live
recovery verification is claimed here.

Emergency CI bypass is explicit:

```powershell
just deploy <ip> <sha> --skip-ci-check
# Or select and print the fetched origin/main SHA:
just deploy <ip> --skip-ci-check
```

Use only during an owner-authorized incident when CI cannot supply the gate.
Record the selected SHA, reason, missing checks, operator and follow-up
verification in the incident record. The script prints an `EMERGENCY` warning;
it still resolves/prints the SHA and enforces main ancestry, protocol support,
server locking, immutable tags, mount protection and the migration exit status.
The flag does not authorize a deployment without the owner's go-ahead.

Verification for this implementation remains offline: the lead runs
`docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_release_deploy.py tests/unit/test_server_sh.py`
and `pwsh -NoProfile -File deploy/smoke/release-smoke.ps1`. CI runs those
tests in the test shards, ShellCheck on the shell scripts, the fake-only
PowerShell smoke in `deploy-scripts`, the production backup smoke, and the
two-architecture image builds. The implementation agent did not run Docker,
pytest, SSH, gh/network calls, migrations or a production deployment. An
owner-approved live deploy/rollback rehearsal is still required.

The backup sidecar checks at startup and every five minutes. It dumps
immediately if `last_success` is missing/invalid, at least 24 hours old, or
older than today's scheduled `BACKUP_HOUR_UTC` once that time has passed.
A successful catch-up persists `last_success`, so a restart does not repeat
it. Failed dumps retry on the next check. Off-site uploads remain pending
when the newest completed dump or `last_success` is newer than
`last_upload_success`. `last_upload_attempt` persists the hourly retry
interval across restarts; a failed upload keeps the local dump. All three
markers live in the backups volume. `BACKUP_TEST_NOW` is a smoke-test clock
override; leave it unset in production. A rebuilt host needs the recovery
order in [restore.md §C](restore.md#c-rebuilding-a-lost-host) before starting
the sidecar, because an empty volume now causes an immediate backup.

Rules:

- Manage production settings only through `just prod-env-push <ip>` from
  the operator's `var/prod.env` and existing cloud inputs. Optional non-secret
  tuning keys are listed once in [settings-manifest.txt](../settings-manifest.txt):
  all route model overrides, reasoning efforts, flags, budgets, debounce and
  approval windows, log level, privacy URL and backup hour. Missing keys keep
  defaults; explicit empty values survive (an empty effort sends no effort
  parameter). Invalid values still fail the app's configuration check; the push
  log prints names only. Secrets and generated/fixed settings use their existing
  paths. When adding an app setting, update the manifest or its explicit
  dev-only/secret/generated classification test. Restart/recreate the containers
  after a settings push to apply it.
- Deploy only a selected `main` ancestor with that SHA's CI green (AGENTS §15),
  except the explicit documented emergency bypass. Never edit tracked files
  on the server (the `.env` comes from `just prod-env-push`).
- A migration that drops or rewrites content needs a backup taken first
  (`just prod-backup <ip>`) and a data-handling note in the commit.
- The app exits non-zero when a role dies and Compose restarts it; a crash
  loop shows in `just prod-status`. Use the guarded `prod-rollback` above
  when eligible; otherwise forward-fix on `main` and deploy the verified SHA.
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
  schema, effective flag names, model and reasoning effort for every configured
  route, and action counts. Run it in the approved post-deploy window to check
  defaults and overrides after a settings push/restart. `effort=omitted` means
  no configured effort parameter (including reserved routes without an effort
  setting). This is an explicit non-secret projection, never an environment
  dump; credentials, database URLs and heartbeat URLs are excluded. The verifier
  uses a fresh one-off container with current Compose settings; it does not
  prove that an already-running app was restarted. Other modes are `actions --since
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
