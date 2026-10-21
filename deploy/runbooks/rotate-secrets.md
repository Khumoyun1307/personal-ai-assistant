# Rotate secrets (SEC-13)

Every secret lives in exactly one place: the server `.env` (`/opt/assistant/.env`,
600, owner `deploy`) and the owner's password manager. Rotation is: obtain the
new value, edit `.env` over SSH (`nano`/`vim`, never echo it into a shell
history), then `docker compose -f deploy/compose.prod.yml up -d` so the
containers restart with it. Check `just prod-status` afterwards.

| Secret | How to rotate | Side effects |
|---|---|---|
| `BOT_TOKEN` | @BotFather → the bot → API Token → Revoke current token. | The old token stops at once; the business connection survives (it is bound to the bot, not the token). Update `.env`, restart. |
| `OPENAI_API_KEY` | OpenAI console → API keys → create new, delete old. | None beyond a restart. Keep prod and dev keys separate so one rotation never touches the other. |
| `POSTGRES_PASSWORD` | `docker compose exec db psql -U assistant -c "ALTER USER assistant PASSWORD '<new>'"`, then edit `.env`, then `up -d`. | Seconds of connection errors while the app restarts; jobs retry. |
| `HEARTBEAT_URL` | Create a new check in the monitor, swap the URL, delete the old check. | None. |
| age key pair | Generate a new pair (`age-keygen`), put the new **public** key in `.env` as `AGE_RECIPIENT`, restart `backup`, take a backup (`just prod-backup`). Keep the **old private key** until every backup encrypted with it has rotated out (14 daily + 4 weekly ≈ 5 weeks). | New backups use the new key; old ones still need the old key. |
| Spaces keys (`RCLONE_CONFIG_SPACES_*`) | Create a new key pair in the provider console, update `.env`, restart `backup`, delete the old pair. | None. |
| GitHub deploy key | New `ssh-keygen` on the server, add as read-only deploy key, remove the old one. | None. |
| SSH access | Add the new public key to `/home/deploy/.ssh/authorized_keys`, remove the old line. | None. |

After any rotation caused by a suspected leak, follow `breach.md` as well.
