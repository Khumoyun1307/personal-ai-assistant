# Provision the production host (M02 S02.5)

One small Linux VM runs the whole stack (ARCHITECTURE §15.4–§15.5). Default:
**DigitalOcean Basic 2 GB, Amsterdam (AMS3), Ubuntu 24.04 LTS**, plus a
**block storage volume** for the database. Everything after step 2 is run by
the agent over SSH; steps 1–2 need the owner's account and payment method.

## 1. Owner: create the resources (~15 min)

1. Droplet: Basic, 2 GB / 1 vCPU, Ubuntu 24.04, region AMS3, hostname `assistant`.
   Authentication: **SSH key only** (add the key the agent will use from the
   dev machine; never a password).
2. Block storage volume: 10 GB, same region, attached to the droplet, name
   `assistant-data`. DigitalOcean volumes are encrypted at rest with
   provider-managed keys (TQ-05 — confirm the current statement in
   DigitalOcean's docs when you create it, and note the date in M02.md).
3. Cloud firewall on the droplet: inbound **SSH (22) only** from anywhere
   (or your IP); outbound all. Long polling needs no inbound port.
4. Object storage for off-site backups (DigitalOcean Spaces, same region, one
   private bucket `assistant-backups`) and an access key pair for it.
5. A heartbeat monitor (Healthchecks.io free tier or similar): one check,
   period 10 minutes, grace 5 minutes — paste its ping URL as `HEARTBEAT_URL`
   in step 4 below.
6. Tell the agent: droplet IP, the SSH user (`root` at first), the volume
   device name (`/dev/disk/by-id/scsi-0DO_Volume_assistant-data`), the Spaces
   endpoint and bucket, and that the keys exist (never paste secrets in chat:
   the agent writes `.env` on the server from values you type into a file it
   names, or you paste them directly over SSH).

## 2. Owner: secrets to hand over out of band

- Prod bot token (BotFather, `runbooks/botfather.md`).
- OpenAI API key for production (a separate key from dev, so it can be
  rotated alone).
- Spaces access key id and secret.
- The **age public key** (`AGE_RECIPIENT`). The agent generates the pair
  into a git-ignored file with `age-keygen` and shows only the public key;
  the private key goes into your password manager and onto paper, then the
  local file is deleted. Only the public key ever reaches the server.

## 3. Agent over SSH: harden the host

```bash
ssh root@<ip>
# non-root sudo user for deploys
adduser --disabled-password --gecos "" deploy && usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/ && chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/deploy && chmod 440 /etc/sudoers.d/deploy
# SSH: keys only, no root login
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config && systemctl restart ssh
# unattended security upgrades, never auto-reboot (monthly manual reboot instead)
apt-get update && apt-get install -y unattended-upgrades
echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/52assistant
dpkg-reconfigure -f noninteractive unattended-upgrades
# time sync, fail2ban
apt-get install -y chrony fail2ban && systemctl enable --now chrony fail2ban
# host firewall mirrors the cloud firewall
ufw default deny incoming && ufw default allow outgoing && ufw allow OpenSSH && ufw --force enable
```

## 4. Agent: the encrypted data volume

```bash
DEV=/dev/disk/by-id/scsi-0DO_Volume_assistant-data
mkfs.ext4 -L assistant-data "$DEV"          # first time only
mkdir -p /mnt/data
echo "LABEL=assistant-data /mnt/data ext4 defaults,nofail,noatime 0 2" >> /etc/fstab
mount -a && findmnt /mnt/data                # must show the volume device
mkdir -p /mnt/data/postgres && chown 999:999 /mnt/data/postgres
swapon --show                                # must be empty (no swap) or encrypted
```

`findmnt --target /mnt/data/postgres` must report the volume device: the
healthcheck in step 6 asserts the database directory is on it.

## 5. Agent: Docker and the checkout

```bash
curl -fsSL https://get.docker.com | sh && usermod -aG docker deploy
cat > /etc/docker/daemon.json <<'EOF'
{"log-driver":"json-file","log-opts":{"max-size":"20m","max-file":"5"}}
EOF
systemctl restart docker
# as deploy:
sudo -iu deploy
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N "" -C "assistant-deploy"
cat ~/.ssh/deploy_key.pub     # owner adds it as a read-only Deploy key on the GitHub repo
git clone git@github.com:Khumoyun1307/personal-ai-assistant.git /opt/assistant  # via the deploy key
```

The server `.env` (`/opt/assistant/.env`, `chmod 600`, owner `deploy`) holds
every setting in `.env.example` with production values: `APP_ENV=prod`,
`LOG_FORMAT=json`, `BOT_TOKEN` (prod bot), `OWNER_TELEGRAM_IDS` (main
account), `OPENAI_API_KEY`, `LLM_MONTHLY_BUDGET_USD`, `PRIVACY_POLICY_URL`,
`HEARTBEAT_URL`, `POSTGRES_PASSWORD` (generated: `openssl rand -base64 32`),
`DATA_DIR=/mnt/data`, `AGE_RECIPIENT`, `BACKUP_REMOTE=spaces:assistant-backups`,
and the rclone variables for Spaces:
`RCLONE_CONFIG_SPACES_TYPE=s3`, `RCLONE_CONFIG_SPACES_PROVIDER=DigitalOcean`,
`RCLONE_CONFIG_SPACES_ENDPOINT=ams3.digitaloceanspaces.com`,
`RCLONE_CONFIG_SPACES_ACCESS_KEY_ID`, `RCLONE_CONFIG_SPACES_SECRET_ACCESS_KEY`.

## 6. Agent: first start and checks

```bash
cd /opt/assistant
docker compose -f deploy/compose.prod.yml up -d --build
docker compose -f deploy/compose.prod.yml ps                       # db healthy, migrate exited 0, app healthy, backup up
docker compose -f deploy/compose.prod.yml exec app python -m assistant healthcheck
docker compose -f deploy/compose.prod.yml exec backup backup.sh once
docker compose -f deploy/compose.prod.yml exec backup ls -la /backups/daily
findmnt --target /mnt/data/postgres                                # on the volume
```

Then the checks of IMPLEMENTATION_PLAN S02.5, recorded in M02.md:
backup produced and decryptable (`runbooks/restore.md` test into a scratch
database), heartbeat alert fires when the app is stopped for > 15 min,
`docker compose restart app` restores processing in < 60 s, server reboot
brings everything back (`restart: unless-stopped`), data directory on the
encrypted device.

## 7. Owner: the Telegram side (client taps)

1. Disconnect the dev bot from the main account (Settings → Chat Automation).
2. Connect the **prod bot (Alviro)** with the Q-10 scope (a few trusted
   contacts plus the second account), grant **Reply**.
3. Press Start in the prod bot's chat, `/start`, agree.
4. Connect the dev bot to the **second account**, so development continues
   there (ARCHITECTURE §15.3); the agent reconfigures the dev `.env`
   (`OWNER_TELEGRAM_IDS` = second account) and recreates the dev database.
