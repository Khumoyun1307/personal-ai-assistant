# M02 — the owner's guide to finishing the milestone

*Written for the owner, in plain language. Last updated 2026-10-22. The agent
keeps this in step with [M02.md](M02.md). Do the steps in order and send the
agent the short phrase at the end of each step. Steps 1–5 can be done in any
order, on the same day.*

## Where things stand

- All five M02 slices are built and merged. A review on 2026-10-22 found bugs
  in the deploy and backup scripts (the backup image did not even build) and
  a few smaller code issues; the agent is fixing them now (slice S02.6).
- Decisions of 2026-10-22: **Oracle Cloud free tier** (Frankfurt) runs the
  bot; **Backblaze B2** keeps the encrypted off-site backups (a different
  company, so one account problem cannot lose both copies);
  **two bots** — **Alviro is production** and runs only on the server, a new
  **dev bot** is for tests on the laptop with your second account.
- Already done, do not repeat: the M00 live tests (except the LV-08 "reopen"
  half), the closed-window test, privacy policy v3, the M00 review.
- Everything below that is not marked "you" is done by the agent with
  scripts. You never paste a secret into the chat: you paste it into a file
  on your own computer, and the agent's scripts move it to the server
  without displaying it.

All files mentioned below are inside the project folder
`C:\Programming\Telegram AI Assistant Agent\`. They are already created with
instructions inside; open them with Notepad.

## Step 0 — BotFather privacy URL (1 minute)

@BotFather → `/mybots` → **Alviro** → **Bot Settings** → **Privacy Policy**
(some apps: **Edit Bot** → **Edit Privacy Policy**) → paste `https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19` →
send. (If the agent later publishes a new policy version it will give you the
new link.) Tell the agent: **"privacy URL set"**.

## Step 1 — Oracle Cloud account (about 20 minutes)

1. Open <https://www.oracle.com/cloud/free/> → **Start for free**.
2. Fill in email, country, name. When asked for the **Home Region**, choose
   **Germany Central (Frankfurt)**. This cannot be changed later.
3. Card check: a **Visa or Mastercard** (Oracle shows a small temporary hold
   that is released; local-only cards are often refused — if yours is,
   tell the agent).
4. Wait for the "your account is ready" email (minutes to a few hours), then
   sign in to the console.
5. **Upgrade to Pay As You Go**: console menu ☰ → **Billing & Cost
   Management** → **Upgrade and Manage Payment** → **Pay As You Go** →
   upgrade. (If the menu has moved, type "Upgrade" into the console search.)
   Why: Oracle may switch off *free-only* servers it thinks are idle, and our
   bot is idle most of the time. Free resources stay $0 after the upgrade;
   the agent adds a **$1 budget alert** so any charge at all emails you.
6. **API key for the agent**: profile icon (top right) → **My profile** →
   **API keys** → **Add API key** → **Generate API key pair** → **Download
   private key** → **Add**.
   - Save the downloaded file as `var\cloud\oci\oci_api_key.pem`.
   - Oracle now shows a **Configuration file preview**. Copy it and paste it
     into `var\cloud\oci\config` under the comment lines, **without** its
     `key_file=` line (the correct one is already there). Save.
7. Tell the agent: **"oracle ready"** and the email address that should get
   the budget alert.

The agent then creates the network, the server (ARM, 1 CPU, 6 GB), the data
disk, the firewall (SSH only) and the budget, all by script. Free ARM servers
are sometimes "out of capacity" in Frankfurt; the script keeps retrying on
its own, which can take hours. Nothing for you to do.

## Step 2 — Backblaze B2 for backups (about 10 minutes)

1. Open <https://www.backblaze.com/sign-up/cloud-storage>. Choose the region
   **EU Central** during sign-up (it is fixed per account). No card is needed
   for the free 10 GB.
2. **B2 Cloud Storage → Buckets → Create a Bucket**: name
   `alviro-backups-` plus a few random letters (names are global), **Private**,
   encryption **Enable**, Object Lock **Disable**.
3. On that bucket: **Lifecycle Settings → Keep only the last version of the
   file** → save. (Deleted backups must really disappear.)
4. **Application Keys → Add a New Application Key**: name `alviro-server`,
   access to **only this bucket**, type **Read and Write**. Backblaze shows
   the `keyID` and `applicationKey` **once**.
5. Fill `var\cloud\b2.env`: bucket name, the bucket's **Endpoint** (shown in
   the bucket list, like `s3.eu-central-003.backblazeb2.com`), keyID,
   applicationKey. Save. Tell the agent: **"b2 ready"**.

## Step 3 — heartbeat monitor (3 minutes)

<https://healthchecks.io> → free account → **Add Check**: name `alviro`,
period **10 minutes**, grace **5 minutes**. Copy its **Ping URL** into
`var\prod.env` (`HEARTBEAT_URL=`). Email alerts to you are on by default.

## Step 4 — production secrets (5 minutes)

In `var\prod.env`:

- `BOT_TOKEN=` — @BotFather → `/mybots` → **Alviro** → **API Token**.
- `OPENAI_API_KEY=` — OpenAI console → **API keys** → **Create new secret
  key**, name it `alviro-prod` (a separate key from the laptop one, so either
  can be revoked alone).

Save. Tell the agent: **"secrets saved"**.

## Step 5 — the dev bot (5 minutes)

1. @BotFather → `/newbot` → any neutral name (not containing "Telegram"),
   e.g. "Lab Assistant Dev" → a username ending in `bot`. Copy the token into
   `var\dev-bot.env` (`DEV_BOT_TOKEN=`).
2. @BotFather → `/mybots` → the new bot → **Bot Settings** → **Secretary
   Mode** (older apps: "Business Mode") → **On**. Then send `/setjoingroups`
   → choose the new bot → **Disable**.
3. In your **second account**, open @userinfobot and press Start; copy the
   number after "Id" into `var\dev-bot.env` (`DEV_OWNER_TELEGRAM_ID=`).
4. Tell the agent: **"dev bot ready"**. Do not connect it to anything yet. The
   laptop keeps using Alviro until the agent finishes the 7-day media check
   on 2026-10-25; then the agent switches the laptop to the dev bot itself and
   tells you the two taps that connect the dev bot to your second account.

## Step 6 — the backup key (2 minutes, when the agent asks)

The agent creates `var\cloud\age-identity.txt`. Copy its whole content into
your password manager (and on paper if you like), then delete the file and
tell the agent **"key stored"**. Without it, backups cannot be read; the
server only ever gets the public half.

## Step 7 — two taps when the agent says the server is running

1. Main account → Settings → **Chat Automation** → **Alviro** connected, with
   the chats you want (a few trusted contacts plus your second account) and
   **Reply to Messages** on. If it is still connected from the tests, nothing
   to do.
2. Open Alviro's chat and send `/status`. Tell the agent what it says.

## Step 8 — Session B: the live test (about 2 hours)

Phone (main account), Telegram Desktop (main account) and your second
account. You send a numbered list of test messages the agent prepares, edit
some, delete some, pause and resume the bot, reconnect it once, while the
agent restarts the server at marked moments and proves every message was
stored exactly once. Checklist:
[tools/acceptance/r0-session.md](../../tools/acceptance/r0-session.md); the
agent walks you through it one checkpoint at a time. It also covers the
connection notices and the "does an edit reopen the 24 h window?" half of
LV-08.

## Step 9 — review (10 minutes)

Read the R0 section the agent adds to M02.md and reply **"M02 OK"**.

## Prefer to let Codex do the agent part?

The server work is a script-driven runbook any coding agent can follow:
[deploy/runbooks/provision.md](../../deploy/runbooks/provision.md) ("Agent
part"). Open Codex in the project folder **with network access allowed**
(its default sandbox blocks the network that SSH and the Oracle tools need)
and say: *"Read AGENTS.md, then follow deploy/runbooks/provision.md, agent
part, from the first unchecked step. Never open var/ files or print
secrets; stop and tell me when a step needs me."* Only one agent should work
on the server at a time.

## Things the agent does on its own (for your information)

- S02.6 fixes, then M03 (search, summaries, memory) right after they merge.
- 2026-10-25 or later: the 7-day media re-check from M00, then the switch of
  the laptop to the dev bot.
- Keeping this guide and M02.md current.
