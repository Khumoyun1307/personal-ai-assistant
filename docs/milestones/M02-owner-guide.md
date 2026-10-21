# M02 — the owner's guide to finishing the milestone

*Written for the owner, in plain language. Last updated 2026-10-21. The agent
keeps this in step with [M02.md](M02.md); do the steps in order, one at a
time, and tell the agent what you saw after each one.*

## Where things stand

- All five M02 slices are built, tested and merged (M02.md has the detail).
- What is left needs **you**: a server to run the bot on, and a two-hour live
  test. Everything else is the agent's.
- There is **one bot, Alviro**. It is the bot you have been using since M00
  and it will be the production bot. (The documents originally assumed a
  separate test bot; that was wrong and has been corrected.)
- Already done, do not repeat: the M00 live tests (LV-01…LV-19 except the
  LV-08 "reopen" half), the 24 h window test (closed-window half), the
  privacy policy v3 (published), the M00 review ("M00 OK", 2026-10-21).

## Step 1 — one BotFather tap (1 minute, do any time)

In Telegram open **@BotFather** → `/mybots` → Alviro → **Edit Bot** →
**Edit Privacy Policy** → paste exactly:

```
https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19
```

and send. Tell the agent "privacy URL set". (Re-sending the same URL is
harmless.)

## Step 2 — Session A: the server (about 45 minutes of your time)

The bot must run on a machine that is always on, because Telegram keeps
undelivered messages for only 24 hours. Signing up and entering a card is
yours; creating the actual server, storage, firewall and backup bucket is the
agent's, once it has an API key.

### 2.1 Choose a provider (tell the agent which)

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud "Always Free"**, Frankfurt | $0 | Best fit (large ARM VM, free 20 GB object storage for backups). Needs a card for identity; free ARM capacity is sometimes "out", so creating the VM can take a few retries. |
| **Google Cloud free tier** e2-micro | $0 | Always on, but only 1 GB RAM and US regions. Works; less comfortable. |
| **DigitalOcean** (Amsterdam) | about $18/month | Simplest, EU, no capacity games. |

The heartbeat monitor (healthchecks.io) is free in every case.

### 2.2 Create the account and add a card

At the provider you chose. Oracle and Google do not charge on the free tier.
Tell the agent "account created".

### 2.3 Make an API key for the agent

- **Oracle:** top-right profile → *My profile* → *API keys* → *Add API key*
  → *Generate API key pair* → download the private key file → copy the
  "configuration file preview" text.
- **Google:** the agent gives you three exact clicks once you choose Google.
- **DigitalOcean:** *API* → *Generate New Token* → read and write.

The agent creates a git-ignored file `var/cloud.env` for you; paste the key
there, save, and tell the agent "key saved". The agent never prints it.

### 2.4 SSH key

The agent gives you one PowerShell command; you paste the printed public
line (starts with `ssh-ed25519`) where the agent says. That line is not a
secret.

### 2.5 Secrets file

The agent creates the template `var/prod.env`. You fill in:

- the **Alviro bot token** (BotFather → `/mybots` → Alviro → API Token);
- a **new OpenAI API key** made for production (OpenAI console → API keys);
- the **backup bucket keys** (the provider shows them when the agent has
  created the bucket, or you create them where the agent points);
- the **heartbeat URL** from healthchecks.io (free account → *Add Check*:
  name `assistant`, period 10 minutes, grace 5 minutes → copy the ping URL).

Save and tell the agent "secrets saved". The agent copies the file to the
server without opening it.

### 2.6 Backup key

The agent generates a key pair and leaves the **private** key in
`var/age-identity.txt`. Copy its contents into your password manager (and on
paper if you like), then delete the file, and tell the agent "key stored".
Only the public key goes to the server; without the private key the backups
cannot be read, so do not lose it.

### 2.7 The agent's part (about an hour, nothing for you to do)

Provision the server, deploy, first backup, restore test, heartbeat test,
restart and reboot checks. The agent reports the results in M02.md.

### 2.8 Two taps when the agent says the server is running

1. Main account → Settings → **Chat Automation** → make sure **Alviro** is
   connected with the chats you want (a few trusted contacts plus your second
   account) and **Reply to Messages** is on. If it is still connected from
   the tests, there is nothing to do.
2. Open Alviro's chat and send `/status`. Tell the agent what it says.

## Step 3 — Session B: the live test (about 2 hours, after Step 2)

You sit with your phone (main account), Telegram Desktop (main account) and
your second account. You send a numbered list of test messages the agent
prepares, edit a few, delete a few, pause and resume the bot, reconnect it
once, while the agent restarts the server at marked moments and proves every
message was stored exactly once. The checklist is
[tools/acceptance/r0-session.md](../../tools/acceptance/r0-session.md); the
agent walks you through it one checkpoint at a time.

This session also covers the last two small live checks: the connection
notices from the app (reconnect, rights change) and the "does an owner
message or an edit reopen the 24 h window?" half of LV-08.

## Step 4 — review (10 minutes)

Read the R0 section the agent adds to M02.md and reply "M02 OK". That
closes R0; M03 (summaries, memory, search) is already approved and its code
starts without further questions.

## Things the agent does on its own (for your information)

- 2026-10-25 or later: the 7-day media re-check from M00.
- M03 code, in parallel with Step 2 if you like.
- Keeping this guide and M02.md current.
