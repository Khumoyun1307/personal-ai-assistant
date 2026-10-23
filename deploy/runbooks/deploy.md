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
- A `backup_upload_stale` alert means dumps are produced but no longer reach
  B2: run `just prod-backup <ip>` and read its log lines (`off-site sync of
  … failed` / `… refused`); see `restore.md` §C for the delete guard.
- Reboots are planned (unattended upgrades never reboot on their own):
  `just prod <ip> reboot`, then `just prod-status <ip>`.
