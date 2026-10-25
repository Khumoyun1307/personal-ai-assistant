# Provision production: Oracle Cloud + Backblaze B2 + Healthchecks.io (M02 S02.6)

One small ARM VM runs the whole stack (ARCHITECTURE §15.4–§15.5). Owner
decisions of 2026-10-22:

| | Choice |
|---|---|
| Host (H1) | Oracle Cloud **Always Free**, home region **US East (Ashburn, `us-ashburn-1`)** (the region the owner chose at signup, 2026-10-23; the script checks it), `VM.Standard.A1.Flex` (arm64) **1 OCPU / 6 GB**, Ubuntu 24.04 aarch64, 50 GB boot volume, separate **50 GB block volume** for Postgres. Account upgraded to Pay As You Go; a USD 1 budget alerts on any spend. Inbound **SSH 22 only** (security list + ufw); long polling needs no inbound port. |
| Off-site backups (H2) | **Backblaze B2, EU Central**: one private bucket, one bucket-restricted application key. rclone's native `b2` backend with `hard_delete`; dumps stay age-encrypted. |
| Bots (H3) | **Alviro** is production and is polled **only by the server**. A separate dev bot (neutral name) is used locally, connected to the second account (`botfather.md`). |
| Heartbeat | Healthchecks.io, one check: period 10 min, grace 5 min. |

Part A is console-only and belongs to the **owner**. Part B is run by a
**coding agent** (Claude Code or OpenAI Codex CLI) from the repository root
on the owner's laptop; it needs only Docker, `just` and an authenticated `gh`.
Every agent command prints status lines, resource names, OCIDs and the public
IP — never a secret (AGENTS §9).

All credentials live in git-ignored files under `var/`; the agent references
them by path and never opens them:

| File | Content | Who writes it |
|---|---|---|
| `var/cloud/oci/config`, `var/cloud/oci/oci_api_key.pem` | OCI CLI config (`key_file=/oci/oci_api_key.pem`, `region=us-ashburn-1`) and API private key | owner (A3) |
| `var/cloud/b2.env` | `B2_BUCKET`, `B2_ENDPOINT`, `B2_KEY_ID`, `B2_APP_KEY` | owner (A4) |
| `var/prod.env` | `BOT_TOKEN` (Alviro), `OPENAI_API_KEY` (prod key), `HEARTBEAT_URL`; `OWNER_TELEGRAM_IDS` (the main account); optional `PRIVACY_POLICY_URL` (default: the published page), `LLM_MONTHLY_BUDGET_USD`, `FEATURE_FLAGS` | owner (A5, A6); `OWNER_TELEGRAM_IDS` copied from the laptop settings by `just dev-bot-switch` |
| `var/dev-switch/env.before-dev-bot` | the laptop settings as they were before the switch (old, revoked Alviro token) | agent (`just dev-bot-switch`); owner deletes it once the dev bot works |
| `var/cloud/ssh/id_ed25519{,.pub}`, `known_hosts` | the agent's SSH key for `deploy@<ip>` | agent (B2) |
| `var/cloud/age-identity.txt` | backup key pair; only the public key leaves the laptop | agent (B3); owner moves it (A8) |
| `var/cloud/prod-generated.env` | generated `POSTGRES_PASSWORD` (hex) | agent (B5) |
| `var/cloud/oci-state.env`, `var/cloud/server-deploy-key.pub` | OCIDs + public IP; the server's public GitHub deploy key | agent (B2, B4) |

## A. Owner: console-only steps

**A1. Oracle Cloud account.** Sign up at <https://signup.cloud.oracle.com>.
The home region is permanent and Always Free resources exist only there.
For this deployment the owner chose **US East (Ashburn), `us-ashburn-1`**
(Frankfurt was planned; done 2026-10-23). A card is verified but not charged.
**Done 2026-10-23 (A1–A3).**

**A2. Upgrade to Pay As You Go.** Console → Billing & Cost Management →
Upgrade and Manage Payment → Pay As You Go. Always Free resources stay free
after the upgrade ("Oracle doesn't charge for Always Free resources after you
upgrade"); the upgrade makes A1 capacity easier to obtain. Wait for the
"upgrade complete" e-mail before B2. The agent then creates a USD 1 budget
that mails you on any actual spend.

**A3. API key for the agent.** Console → Profile menu → **User settings** →
**Tokens and keys** tab (or Resources → API keys) → **Add API key** →
*Generate API key pair* → **Download private key** → save it as
`var/cloud/oci/oci_api_key.pem`. Click Add, then copy the
*Configuration file preview* into `var/cloud/oci/config` (the template is
there), keeping the line `key_file=/oci/oci_api_key.pem` and
`region=us-ashburn-1`
(<https://docs.oracle.com/en-us/iaas/Content/Identity/access/to_upload_an_API_signing_key.htm>). Your user is a tenancy administrator, which the
agent needs for compartments, networking, compute and budgets.

**A4. Backblaze B2.** Sign up at <https://www.backblaze.com/sign-up/cloud-storage>
choosing region **EU Central** (fixed per account). Then:
1. Buckets → Create a Bucket: a unique name (e.g. `alviro-backups-<random>`),
   **Private**, Default Encryption **Enable**, Object Lock **Disable**.
2. The bucket's Lifecycle Settings → **Keep only the last version of the
   file** (a second guard next to rclone's `hard_delete`: nothing deleted
   survives as a hidden version).
3. Application Keys → Add a New Application Key: name `alviro-backup`,
   **Allow access to Bucket(s): only this bucket**, Type of Access **Read and
   Write**, no file-name prefix, no expiry.
4. Put the values into `var/cloud/b2.env`: `B2_BUCKET` (bucket name),
   `B2_ENDPOINT` (the bucket's S3 endpoint, kept for reference; the native
   API does not use it), `B2_KEY_ID` (keyID), `B2_APP_KEY` (applicationKey —
   shown only once).

**A5. Heartbeat.** <https://healthchecks.io> → new project → Add Check: name
`alviro`, **Period 10 minutes, Grace 5 minutes**, e-mail integration on.
Copy the ping URL into `var/prod.env` as `HEARTBEAT_URL`.

**A6. Bot token and OpenAI key.**
- @BotFather → Alviro → API Token → *Revoke current token* if it was ever
  used on the laptop, then copy the new token into `var/prod.env` as
  `BOT_TOKEN`. From now on only the server polls Alviro; the laptop uses the
  dev bot (`botfather.md`).
- <https://platform.openai.com> → a separate production project/key →
  `var/prod.env` as `OPENAI_API_KEY` (never the dev key, so either can be
  rotated alone). Optional: `LLM_MONTHLY_BUDGET_USD`, `PRIVACY_POLICY_URL`.

**A7. Hand over to the agent**, in chat: "provision production" and the
e-mail address for budget alerts. The main account's Telegram user id
(`OWNER_TELEGRAM_IDS`) is not typed anywhere: `just dev-bot-switch` copies it
from the laptop settings into `var/prod.env`. No secret goes into chat.

Owner steps that come later: **A8** after the restore test (B7.4) move
`var/cloud/age-identity.txt` into your password manager (and on paper), then
delete it from the laptop; **A9** the Telegram client steps in §D.

## B. Agent: commands in order

Run from the repository root. Each step lists what to check; **stop and hand
back** where marked. Never pass `--verbose`, never open `var/`, never print
`.env` (AGENTS §9).

**B0. Preconditions.** `docker version`, `just --version`, `gh auth status`
succeed; `Test-Path var/cloud/oci/config, var/cloud/oci/oci_api_key.pem,
var/cloud/b2.env, var/prod.env` (PowerShell) or `test -f …` reports all
present (existence only — do not read them). Missing → hand back (A3–A6).

**B1. Dry run.** `just oci-provision <owner-email> --dry-run` → "dry run:
config and SSH key present, cloud-init rendered …". It generates the agent
SSH key on first use. No OCI call is made.

**B2. Provision.** `just oci-provision <owner-email>` (idempotent; safe to
re-run). Check the lines: `authenticated; home region us-ashburn-1`,
compartment `alviro`, VCN/igw/route table/security list ("inbound TCP 22
only")/subnet, the Ubuntu 24.04 aarch64 image, `launched instance alviro`,
block volume `alviro-data` attached at `/dev/oracleoci/oraclevdb`, budget
`alviro-monthly-usd1` with alerts, and the public IP on the last line (also
in `var/cloud/oci-state.env`).
- *Out of host capacity in every availability domain* → run
  `just oci-provision <owner-email> --wait` in the background (retries with
  backoff up to 12 h, one status line per round) and report back. HTTP 429
  (*TooManyRequests*) is retried the same way; if the launch had already
  been accepted, the existing instance is picked up instead of a second one.
- *Authentication failed* → stop; hand back to A3. *Home region is not
  us-ashburn-1* → stop; the owner decides (it cannot be changed).
- *In-transit encryption rejected* for the shape/image → re-run with
  `PV_TRANSIT_ENCRYPTION=false` only after telling the owner (volumes stay
  encrypted at rest either way).

**B3. Backup key.** `just prod-age-key` → prints `age public key
(AGE_RECIPIENT): age1…`. The private half stays in
`var/cloud/age-identity.txt` until A8.

**B4. Bootstrap the host.** `just prod-bootstrap <ip>`. Waits for SSH and
cloud-init (a few minutes on first boot), then checks: `arch: aarch64`,
Ubuntu 24.04, Docker + Compose versions, `Status: active` (ufw),
`passwordauthentication no permitrootlogin no`, chrony/fail2ban/
unattended-upgrades/docker active, `swap: none`; formats the block volume
only if `blkid -p` and `wipefs` find no signature at all (any probe error
stops instead), mounts it at `/mnt/data` (fstab by UUID, `nofail`),
verifies it is the 50 GiB attached volume and not the boot disk, makes
Docker wait for the mount (`docker.service` drop-in
`RequiresMountsFor=/mnt/data`, echoed as `docker.service waits for:
/mnt/data`) and writes the sentinel `/mnt/data/.alviro-data-volume` that
the stack and `deploy/server.sh` require (ARCHITECTURE §15.4 mount guard);
creates a read-only GitHub deploy key on the server (the repo is private),
adds it with `gh repo deploy-key add` (read-only is gh's default; "already
in use" on a re-run is fine), and clones `main` into `/opt/assistant`.
Any `ERROR:` line → stop and report it.

**B5. Server `.env`.** `just prod-env-push <ip>` (the owner id comes from
`OWNER_TELEGRAM_IDS` in `var/prod.env`, written by `just dev-bot-switch`) →
`valid: …` (names only) and `server: /opt/assistant/.env 600 deploy`.
`invalid: <NAME> …` → hand back to the owner to fix that value in
`var/prod.env` or `var/cloud/b2.env`. `PRIVACY_POLICY_URL` defaults to the
published policy page.

**B6. First start.** Before this, confirm with the owner that nothing else
polls Alviro (the laptop's `.env` holds the dev bot's token). Then
`just deploy <ip>` → `compose ps`: `db` healthy, `migrate` exited 0, `app`
healthy, `backup` up. Then `just prod-status <ip>`: healthcheck OK,
`data dir: /dev/sdX /mnt/data (sentinel present)`.

*Data volume not mounted* (`ERROR: … the data volume is not mounted` or
`… .alviro-data-volume is missing`, from any `just prod-*`/`just deploy`,
typically after a reboot): Docker has not started, by design, and nothing
has run against the boot disk. Check in the OCI console that `alviro-data`
is attached, then on the server `sudo mount /mnt/data && sudo systemctl
start docker` and `just prod-status <ip>`. Never create the sentinel or
`/mnt/data/postgres` by hand on an unmounted `/mnt/data`: that disables the
guard and lets Postgres start an empty cluster on the boot disk.

**B7. Post-deploy checks** (IMPLEMENTATION_PLAN S02.5; record results with
dates in the milestone report):
1. **Data on the block volume:** `just prod-status <ip>` shows the
   Postgres directory on the attached device (`findmnt`), not `/`.
2. **Backup produced and off-site:** `just prod-backup <ip>` exits 0 and
   logs `wrote <ts>.dump.age` and `off-site sync complete`; `just prod-status
   <ip>` shows fresh `last_success` and `last_upload_success`;
   `just prod <ip> offsite-list` lists the same file under `daily/`.
3. **Restart < 60 s:** `just prod <ip> restart-timing` → "app healthy again
   N s after restart" with N < 60.
4. **Isolated restore test with the offline identity:**
   `just prod-restore-test <ip>` (restore.md §A) → dump name and size, row
   counts, `restore gap row: restore/possible`, `restore_test dropped`. Then
   ask the owner to do **A8**.
5. **Heartbeat alert:** `just prod <ip> app-stop`; the owner confirms the
   Healthchecks.io e-mail arrives after > 15 min; `just prod <ip> app-start`
   and the check turns green again.
6. **Reboot:** `just prod <ip> reboot`; after ~2 min `just prod-status <ip>`
   shows every service back (`restart: unless-stopped`, Docker enabled, the
   volume mounted from fstab).
7. **Budget:** the owner confirms the budget and alert rules appear under
   Billing → Budgets (no e-mail is expected while spend is zero).

**B8. Hand back** for §D with the results of B7.

## C. Keeping the Always Free instance

Oracle may reclaim an **Always Free** instance as idle when, over 7 days, the
95th-percentile CPU use is below 20 %, network use is below 20 % **and** (A1
only) memory use is below 20 %
(<https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>).
1 OCPU / 6 GB keeps the 20 % memory mark at ~1.2 GB. `just prod-status`
prints the memory share; check it one week after B6 and tell the owner if
it stays below 20 % (the decision on how to respond is the owner's). The
backups mean a reclaimed host is a rebuild (B2–B6) plus restore
(`restore.md` §C), not data loss beyond the last dump.

## D. Owner: the Telegram side (client taps)

1. Settings → Chat Automation on the **main account**: connect **Alviro**
   with the Q-10 scope (a few trusted contacts plus the second account),
   grant **Reply**.
2. Alviro's chat: Start, `/start`, agree.
3. The **dev bot** stays connected to the **second account** only; the
   laptop's `.env` holds the dev bot's token and the second account as
   `OWNER_TELEGRAM_IDS` (ARCHITECTURE §15.3).

## For Codex

OpenAI Codex CLI reads `AGENTS.md` automatically, so every rule above
applies unchanged. In addition:
- Run it with **network access permitted** (its default sandbox blocks the
  network, and Part B needs Docker, SSH, GitHub and the OCI API), e.g. the
  workspace-write sandbox with network enabled, or an approval mode that
  lets you allow each `just` command.
- Obey AGENTS §9: never open, list or print anything under `var/`, never
  print `.env` values, never pass `--verbose` to probe tools; verify by the
  scripts' name-only output.
- At every console-only step (Part A, §D, the heartbeat e-mail in B7.5 and
  A8) **stop and hand back to the owner**; do not try to automate a console
  page or ask for a secret in chat.

## References (checked 2026-10-22)

- OCI Always Free resources and idle reclamation:
  <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
- Block volume encryption — "The Block Volume service always encrypts all
  block volumes, boot volumes, and volume backups at rest by using the
  Advanced Encryption Standard (AES) algorithm with 256-bit encryption. By
  default all volumes and their backups are encrypted using the
  Oracle-provided encryption keys." (TQ-05 evidence):
  <https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/blockvolumeencryption.htm>
  (in-transit encryption for paravirtualized attachments is optional; the
  scripts enable it).
- Consistent device paths (`/dev/oracleoci/oraclevdb`):
  <https://docs.oracle.com/en-us/iaas/Content/Block/References/consistentdevicepaths.htm>
- OCI CLI (official image `ghcr.io/oracle/oci-cli`, pinned `20261007` = CLI
  3.94.2; every flag used was checked with `oci <command> --help` in that
  image): <https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/>
- Docker Engine on Ubuntu (apt repository):
  <https://docs.docker.com/engine/install/ubuntu/>
- rclone B2 backend (`account` = keyID, `key`, `hard_delete`):
  <https://rclone.org/b2/>; rclone environment configuration
  (`RCLONE_CONFIG_<REMOTE>_<OPTION>`): <https://rclone.org/docs/#config-file>
- Compose merge and `!override`: <https://docs.docker.com/reference/compose-file/merge/>;
  `--env-file` and interpolation: <https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/>
- `gh repo deploy-key add`: <https://cli.github.com/manual/gh_repo_deploy-key_add>
- cloud-init modules: <https://cloudinit.readthedocs.io/en/latest/reference/modules.html>
