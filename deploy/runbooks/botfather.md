# Create and configure the bots in @BotFather (FR-ONB-10)

There are **two bots** (owner decision H3, 2026-10-22):

| Bot | Role | Polled by | Connected to | Token lives in |
|---|---|---|---|---|
| **Alviro** | production | **the server only** | the owner's **main account** | `var/prod.env` → server `.env` (`provision.md` A6, B5) |
| dev bot (any neutral working name) | development, probe, acceptance rehearsals | the laptop (`just run`, `just probe`) | the owner's **second account** | the laptop's `.env` |

A bot token may be polled by only one process: `getUpdates` from a second
place steals updates (and the app's advisory lock only protects one
database). So Alviro's token never goes into the laptop's `.env`, and the dev
bot's token never goes to the server. Only the owner can do the steps below:
@BotFather has no API. Never paste a token in chat, an issue or a document —
it goes straight into the right file.

1. `/newbot` → name (`Alviro` for production; for dev any neutral working
   name) → username ending in `bot` (not containing "Telegram").
2. Copy the token into the right file (table above).
3. Open the bot in @BotFather (`/mybots` → the bot, or the BotFather Mini App):
   - **Bot Settings → Secretary Mode** (older clients: "Business Mode") → **On**.
     This is what lets accounts connect the bot under Chat Automation.
   - Keep **Bot-to-Bot Communication Mode off**.
   - `/setjoingroups` → **Disable** (the product never works in groups).
   - `/setprivacy` is irrelevant with groups disabled; leave the default.
   - Optional: `/setdescription`, `/setabouttext` (neutral wording, no
     "Telegram" in the name or description), `/setuserpic`.
   - Privacy policy: Bot Settings → Privacy Policy (where the client offers
     it) → the URL from `PRIVACY_POLICY_URL`
     (https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19).
4. Verify: for the dev bot `just healthcheck` on the laptop; for Alviro
   `just prod-status <ip>`. The app's startup log says `poller_started` and
   does **not** warn `secretary_mode_disabled`
   (`getMe.can_connect_to_business` is true).
5. If Alviro's token was ever used on the laptop, revoke it (API Token →
   Revoke current token) before it goes to the server.
6. Connections: Alviro to the main account, the dev bot to the second
   account only (`provision.md` §D). For a brand-new probe bot, do **not**
   press Start before LV-09-style checks if any are planned.
