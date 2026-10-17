# M00 S00.2 — Telegram probe (owner-run)

Throwaway code, never imported by `src/` (AGENTS §5). It exists to answer
LV-01…LV-19 (TELEGRAM_FEASIBILITY §11) against real accounts.

> **The owner runs this, not a coding agent.** It needs a real `BOT_TOKEN`, and
> `var/probe/` will hold real chat content. Agents never read that directory
> (AGENTS §9); they see only what `sanitize.py` produces.

## Before the first run

1. @BotFather → `/newbot` → a neutral working name, **not** the product name and
   without "Telegram" in it. Copy the token straight into `.env` as `BOT_TOKEN` —
   never paste it into a chat, issue or commit.
2. @BotFather → the bot → **Mode Settings** (older clients: *Bot Settings*) →
   enable **Secretary Mode** (formerly "Business Mode").
3. `/setjoingroups` → **Disable**. Leave Bot-to-Bot Communication Mode off.
4. Add the chat ids you intend to observe to `.env`:
   `PROBE_ALLOWED_CHAT_IDS=<second account id>,<third account id>,<bot id>`.
   Any chat not listed is reduced to its update type, `update_id` and a hash of
   `chat.id` — nothing else is written.
5. `just probe me` — it must report `can_connect_to_business: true`. If not,
   Secretary Mode is still off.
6. **Do not open or /start the probe bot from any account until LV-09 is
   recorded**, or that result is lost.

Then set up connections A and B exactly as in TF §11. **Never widen the main
account's Chat Automation scope.**

## Running

```powershell
just probe listen            # long-poll; writes var/probe/updates-YYYYMMDD.jsonl
just probe me
just probe connection <connection_id>
just probe send <connection_id> <chat_id> "text"
just probe dm <chat_id> "text"                      # no connection — LV-09
just probe edit <connection_id> <chat_id> <msg_id> "text"
just probe typing <connection_id> <chat_id>
just probe delete <connection_id> <msg_id> [<msg_id> ...]
just probe getfile <file_id> --download
just probe sendphoto <connection_id> <chat_id> <file_id>
just probe burst <connection_id> <chat_id> "text" --count 5
```

Every API call — success *or* failure — is appended to the same JSONL file with
the exact error class, message and `retry_after`. Those strings are the
deliverable for LV-04, LV-08 and LV-16, so run the failing cases deliberately
rather than avoiding them.

Only one process may poll a token at a time. **Stop `just probe listen` before
`just run`**, or the second poller gets HTTP 409.

## Which command serves which LV

| LV | Commands |
|---|---|
| LV-01, LV-05, LV-12, LV-13, LV-19 | `listen` + `connection` (change rights/recipients in the client) |
| LV-02, LV-06, LV-18 | `listen` (owner types; observe what arrives) |
| LV-03 | `send`, then `listen` — correlate the returned `message_id` with any echo |
| LV-04 | `send` / `typing` while the chat is paused; record the exact error |
| LV-07 | `delete`, plus deletions made in the client |
| LV-08 | `send` at T0+1 min, then at T0+24 h+10 min `edit`, `send`, `typing` |
| LV-09 | `dm` **before** any account presses Start |
| LV-10 | `listen` while the owner taps "Manage Bot" |
| LV-11 | `getfile --download` now, after 24 h, after 7 days; `sendphoto` to re-send |
| LV-15, LV-17 | `listen` while messages are edited on both sides |
| LV-16 | `burst` |

## Producing fixtures

```powershell
just sanitize var/probe/updates-20261018.jsonl
```

`sanitize.py` is **default-deny for strings**: a string survives only if its key
is a structural enum (`type`, `status`, `mime_type`, `language_code`).
Everything else — text, captions, names, usernames, phone numbers, file ids — is
replaced with synthetic content of the same UTF-16 length, so message entity
offsets still line up. Identifiers are remapped consistently, so the same real
chat id becomes the same synthetic one everywhere.

Its contract is pinned by `tests/unit/test_probe_sanitizer.py`. **Read the
output before committing it** — the sanitiser is a safety net, not a substitute
for looking.
