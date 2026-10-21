# Deploy (M02 S02.5)

One path (ARCHITECTURE §15.4): the server's checkout tracks `main`; a deploy
is a fast-forward pull, an image rebuild and a restart. Migrations run first
(`migrate` service), the app starts only after they succeeded.

```powershell
just deploy deploy@<ip>        # pull --ff-only, up -d --build, ps
just prod-status deploy@<ip>   # containers, healthcheck, last backup time
just prod-logs deploy@<ip>     # content-free app log (AGENTS §9)
```

Rules:

- Deploy only what is on `main` with CI green (AGENTS §15). Never edit files
  on the server; never `git pull` a branch.
- A migration that drops or rewrites content needs a backup taken first
  (`just prod-backup deploy@<ip>`) and a data-handling note in the commit.
- The app exits non-zero when a role dies and Compose restarts it; a crash
  loop shows in `just prod-status`. Roll back by deploying the previous
  commit (`git checkout <sha>` on the server is *not* the path: revert on
  `main`, then deploy).
- After a deploy, confirm in the control chat that `/status` answers and the
  heartbeat monitor is still receiving pings.
