# Suspected breach or leak (SEC-13, NFR-09)

Act in this order; do not skip to analysis first.

1. **Contain (minutes).**
   - `docker compose -f deploy/compose.prod.yml stop app` — no more sends, no
     more model calls.
   - If the host itself is suspect: power it off from the provider console
     and snapshot the volume for later analysis. Otherwise revoke the
     attacker's access: rotate the SSH keys, review `last -a` and
     `/var/log/auth.log`.
2. **Rotate everything** (`rotate-secrets.md`): bot token first (it can send
   as the owner), then the OpenAI key, the database password, the Spaces
   keys, the heartbeat URL, the deploy key.
3. **Telegram side (owner):** Settings → Chat Automation → disconnect the
   bot until the host is trusted again. The owner's own account is not
   affected by a bot-token leak, but the bot could have read and sent in the
   included chats.
4. **Assess what was reachable.** The database holds up to 90 days of
   message content from the included chats, contact names, the owner's
   control-chat text, settings and consent records (privacy policy, "What is
   stored"). Backups are age-encrypted; without the offline private key they
   are not readable. Check `app.audit_log` and `app.actions` for sends the
   owner did not approve; check `app.llm_calls` for unexpected volume.
5. **Decide and notify.** The owner decides whether the people in the
   included chats should be told. For the personal phase there is one
   controller (the owner); a public product would follow the applicable
   breach-notification law (M09 legal review).
6. **Rebuild rather than clean.** Provision a fresh host (`provision.md`),
   restore from the last backup taken before the incident (`restore.md`),
   run post-restore, reconnect.
7. **Record** what happened, when it was noticed, what was rotated and what
   changed, in `docs/milestones/` under the current milestone.
