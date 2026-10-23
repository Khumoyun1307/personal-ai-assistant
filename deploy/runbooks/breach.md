# Suspected breach or leak (SEC-13, NFR-09)

Act in this order; do not skip to analysis first.

1. **Contain (minutes).**
   - `just prod <ip> app-stop` — no more sends, no more model calls.
   - If the host itself is suspect: stop the instance from the Oracle Cloud
     console (Compute → Instances → `alviro` → Stop) and keep its boot and
     `alviro-data` volumes for later analysis (do not terminate). Otherwise
     revoke the attacker's access: rotate the SSH keys, review `last -a` and
     `journalctl -u ssh` on the server.
2. **Rotate everything** (`rotate-secrets.md`): bot token first (it can send
   as the owner), then the OpenAI key, the database password, the B2
   application key, the OCI API key, the heartbeat URL, the GitHub deploy
   key. The age private key never was on the server; rotate it only if the
   laptop or password manager is suspect.
3. **Telegram side (owner):** Settings → Chat Automation → disconnect the
   bot until the host is trusted again. The owner's own account is not
   affected by a bot-token leak, but the bot could have read and sent in the
   included chats.
4. **Assess what was reachable.** The database holds up to 90 days of
   message content from the included chats, contact names, the owner's
   control-chat text, settings and consent records (privacy policy, "What is
   stored"). Backups — on the server and in B2 — are age-encrypted; without
   the offline private key they are not readable. Check `app.audit_log` and
   `app.actions` for sends the owner did not approve; check `app.llm_calls`
   for unexpected volume; check the OCI audit log and the B2 bucket's file
   list for unexpected changes.
5. **Decide and notify.** The owner decides whether the people in the
   included chats should be told. For the personal phase there is one
   controller (the owner); a public product would follow the applicable
   breach-notification law (M09 legal review).
6. **Rebuild rather than clean.** Terminate the suspect instance, provision
   a fresh one (`provision.md` Part B; the scripts are idempotent and create
   only what is missing), then restore from the last backup taken before the
   incident following `restore.md` §C (fetch the off-site history first),
   run post-restore, reconnect.
7. **Record** what happened, when it was noticed, what was rotated and what
   changed, in `docs/milestones/` under the current milestone.
