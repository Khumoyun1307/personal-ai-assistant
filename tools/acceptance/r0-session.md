# R0 live session script (M02 acceptance; PRD §21 R0, IMPLEMENTATION_PLAN §4)

**Setup:** the production deployment runs (S02.5); the prod bot **Alviro** is
connected to the **main account** with the second account inside the Chat
Automation scope and **Reply** granted; the dev/probe bot is connected to the
**second account** (ARCHITECTURE §15.3). The agent runs `just prod-logs` and
the verification tool; the owner holds the phone (main account), Telegram
Desktop (main account) and the second account (phone or web).

Every message is **synthetic**: send the numbered lines exactly as written.
The agent never reads message text; verification is by number and structure
(`tools/acceptance/verify_r0.py`, which prints only numbers and states).

Timing: about 2 hours wall time, roughly 40 minutes of typing. Tick each
line; tell the agent "checkpoint N done" at each checkpoint.

## Part 0 — before the session (owner, 2 min)

- [ ] 0.1 In Alviro's control chat: `/status`. The agent confirms it shows
      the connection, `can reply` and the test chat (or no chats yet).

## Part 1 — contact messages (second account → main account), checkpoint 1

From the **second account**, in the chat with the main account, one line per
message, about ten seconds apart:

```
R0-001 Salom! Bu sinov xabari.
R0-002 Ertaga soat 10 da uchrashamizmi?
R0-003 Привет, это тестовое сообщение.
R0-004 Hello, this is a synthetic test message.
R0-005 Mana hujjat keyinroq yuboraman.
R0-006 Oʻzbekiston Respublikasi — apostrof testi.
R0-007 Ўзбекистон Республикаси — кирилл тести.
R0-008 Question: can we move the meeting to 11?
R0-009 Ok.
R0-010 Rahmat!
```

Then media, each with the caption shown:

```
R0-011 (photo) caption: R0-011 photo test
R0-012 (voice note, 2–3 s)                      — no caption
R0-013 (document, any small PDF) caption: R0-013 document test
R0-014 (round video note, 2–3 s)                — no caption
R0-015 (sticker)                                — no caption
R0-016 (location, any)                          — no caption
```

**Checkpoint 1:** agent runs `verify_r0.py --expect 1-16`.

## Part 2 — owner replies from two clients, checkpoint 2

From the **main account on the phone**:

```
R0-017 Ha, 11 boʻladi.
R0-018 Hujjatni kutaman.
```

From the **main account on Telegram Desktop**:

```
R0-019 Desktop reply test.
R0-020 Спасибо, принято.
```

If the main account has Telegram Premium: enable an **away message** with
the text `R0-AWAY synthetic away message`, then from the second account
send `R0-021 Trigger the away message`, wait for the automatic reply, and
disable the away message again. Skip R0-021 without Premium.

**Checkpoint 2:** `verify_r0.py --expect 1-21` — 17–20 must be
`owner_manual`; the away reply, if any, `owner_automated`.

## Part 3 — edits and deletions, checkpoint 3

From the **second account**:

- [ ] 3.1 Edit R0-002 to `R0-002 Ertaga soat 10 da uchrashamizmi? (edited)`.
- [ ] 3.2 Edit R0-004 without changing anything (open edit, press send).
- [ ] 3.3 Delete R0-009 for both.
- [ ] 3.4 Delete R0-010 for yourself only (if offered).

From the **main account**:

- [ ] 3.5 Edit R0-017 to `R0-017 Ha, 11 boʻladi. (edited)`.
- [ ] 3.6 Delete R0-019 for both.

Then from the second account send `R0-022 After edits and deletions`.

**Checkpoint 3:** `verify_r0.py --expect 1-22 --deleted 9,10,19 --edited 2,17`.

## Part 4 — pause, rights, restart, disconnect, reboot, checkpoint 4

- [ ] 4.1 Main account: in the chat's bot bar, **pause** the bot. Second
      account sends `R0-023 Sent while paused` (expected: never delivered).
      Resume. Second account sends `R0-024 After resume`.
- [ ] 4.2 Main account: Settings → Chat Automation → turn **Reply** off, then
      on. The control chat shows two rights notices.
- [ ] 4.3 Agent restarts the app (`docker compose restart app`) while the
      second account sends `R0-025`, `R0-026`, `R0-027` five seconds apart.
- [ ] 4.4 Main account: **disconnect** the bot; second account sends
      `R0-028 Sent while disconnected` (expected: lost); **reconnect** with
      the same scope and Reply; the control chat shows the reconnect notice
      ("never reached me"). Second account sends `R0-029 After reconnect`.
- [ ] 4.5 Agent reboots the server; once it is back, the second account
      sends `R0-030 After reboot`.
- [ ] 4.6 Filler to reach 60: from the second account, ten seconds apart,
      `R0-031 filler 31` … `R0-060 filler 60` (the agent pastes the list in
      the control chat so you can copy it).

**Checkpoint 4:** `verify_r0.py --expect 1-60 --missing-expected 23,28
--deleted 9,10,19 --edited 2,17`. The agent also computes NFR-02 (p95 of
`processed_at − date` < 5 s over the session) and, at the very end, NFR-03
(stop the app for 16 minutes; the heartbeat monitor must alert within 15).

## Part 5 — the deferred LV-08 half (probe bot on the second account, 5 min)

On the dev connection the **second account is the owner** and the **main
account is the contact**. With `just probe listen` running (agent):

1. Second account (owner) sends one message to the main account. Agent:
   `just probe send <conn> <main chat id> LV08-reopen-1` → expected
   `PEER_ID_INVALID` if an owner message does not reopen the window.
2. Main account (contact) **edits** an old message in that chat, sends
   nothing new. Agent: `just probe send … LV08-reopen-2` → expected still
   closed.
3. Main account (contact) sends a new message. Agent:
   `just probe send … LV08-reopen-3` → expected success.

Precondition: the chat has had no contact message for 24 h on that
connection (the agent checks with `probe summary` before step 1; otherwise
this part moves to the next day). The agent records the results in
TF §12 (LV-08 reopen half, LV-15 add-on).

## Part 6 — close

- [ ] Owner: `/status`, "Manage Bot" in the test chat (opens
      `/start bizChat<id>`), `/privacy` — say in one line whether they read
      correctly.
- [ ] Agent: final `verify_r0.py`, NFR-02/NFR-03 numbers, backup and
      heartbeat checks, the R0 section of `docs/milestones/M02.md`; owner
      reviews → M02 `done`.
