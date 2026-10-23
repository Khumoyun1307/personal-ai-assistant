# Rotate secrets (SEC-13)

Every production secret lives in exactly two places: its source file on the
owner's laptop (`var/prod.env`, `var/cloud/b2.env`, `var/cloud/prod-generated.env`,
all git-ignored) plus the owner's password manager, and the server `.env`
(`/opt/assistant/.env`, mode 600, owner `deploy`) that
`deploy/oci/push-env.sh` assembles from them. Never edit the server `.env` by
hand and never echo a secret into a shell. Rotation is: obtain the new value,
put it into the source file, then

```powershell
just prod-env-push <ip> <owner-telegram-id>   # validates names, streams .env (600)
just deploy <ip>                              # containers restart with it
just prod-status <ip>
```

| Secret | How to rotate | Side effects |
|---|---|---|
| `BOT_TOKEN` (Alviro) | @BotFather → Alviro → API Token → Revoke current token → `var/prod.env`. | The old token stops at once; the business connection survives (it is bound to the bot, not the token). |
| `OPENAI_API_KEY` | OpenAI console → API keys (prod project) → create new, delete old → `var/prod.env`. | None beyond a restart. Prod and dev keys are separate, so one rotation never touches the other. |
| `POSTGRES_PASSWORD` | On the server: `docker compose --env-file .env -f deploy/compose.prod.yml exec db psql -U assistant -c "\password assistant"` (interactive prompt, nothing in history), enter a new 64-hex-character value; put the same value in `var/cloud/prod-generated.env`; push and deploy. | Seconds of connection errors while the app restarts; jobs retry. |
| `HEARTBEAT_URL` | Healthchecks.io: create a new check (period 10 min, grace 5 min), swap the URL in `var/prod.env`, push, deploy, delete the old check. | None. |
| B2 application key (`RCLONE_CONFIG_OFFSITE_*`) | B2 → Application Keys → add a new bucket-restricted Read and Write key → `var/cloud/b2.env` → push, deploy → `just prod-backup <ip>` must log `off-site sync complete` → delete the old key. | None. |
| age key pair | The owner moves the old `var/cloud/age-identity.txt` into the password manager (labelled with the date) and removes it from `var/cloud/`; then `just prod-age-key` creates a new pair; push, deploy, `just prod-backup <ip>`. Keep the **old private key** in the password manager until every backup encrypted with it has rotated out (14 daily + 4 weekly ≈ 5 weeks, locally and off-site). | New backups use the new key; old ones still need the old key. |
| OCI API key | Console → My profile → API keys → add a new key, update `var/cloud/oci/`, run `just oci-provision <email> --dry-run` then a real run (idempotent), delete the old key. | None. |
| GitHub deploy key (server) | On the server: `rm ~/.ssh/github_deploy*`; `just prod-bootstrap <ip>` creates and adds a new read-only key; remove the old one in GitHub → Settings → Deploy keys. | None. |
| Agent SSH key | Generate a new key, append its public part to `/home/deploy/.ssh/authorized_keys` over the old key's session, verify a login with the new key, remove the old line. | None. |

After any rotation caused by a suspected leak, follow `breach.md` as well.
