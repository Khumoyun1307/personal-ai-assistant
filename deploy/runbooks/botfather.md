# Create and configure a bot in @BotFather (FR-ONB-10)

Used for the probe/dev bot (M00) and the production bot **Alviro** (S02.5).
Only the owner can do this: @BotFather has no API. Never paste the token in
chat, an issue or a document — it goes straight into the right `.env`.

1. `/newbot` → name (for prod: `Alviro`; for dev: any neutral working name)
   → username ending in `bot` (not containing "Telegram").
2. Copy the token into `.env` (`BOT_TOKEN=`), dev on the dev machine, prod on
   the server (`provision.md` §5).
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
4. Verify: `just healthcheck` on the machine that holds the token, then the
   app's startup log says `poller_started` and does **not** warn
   `secretary_mode_disabled` (`getMe.can_connect_to_business` is true).
5. **Do not press Start** in a brand-new probe bot before LV-09-style checks
   if any are planned; for Alviro, press Start from the main account and
   `/start` once the server runs (`provision.md` §7).
