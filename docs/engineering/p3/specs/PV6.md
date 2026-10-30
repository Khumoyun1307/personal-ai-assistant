# PV6 — privacy policy v6 and the SEC-12 in-bot notice

| | |
|---|---|
| **Unit** | PV6 — the v6 policy text covering M04–M07 (TL), and the code: `POLICY_VERSION = "v6"`, the v6 entry in `POLICY_CHANGES` (the notice text), notice tests (Edvard E2) |
| **Team / slot** | TL (text) / Edvard E2 (code) |
| **Size** | S |
| **Depends on** | the owner's approval of the text (OD-24, by D+3). The code PR may merge into `int/p3` with the TL's draft notice text; the approved wording lands before R1 (§3.3). E2 writes the code on D+2 while an eval run is in flight, never ahead of DELEG-DRAFT or EV-AUT work (R28) |
| **Branch → base** | text: `docs/p3-pv6` → `main` (TL docs PR); code: `p3/pv6` → `int/p3` |
| **Peer reviewer** | text: the three leads + SQ read it, the owner approves; code: a Josh engineer (Claude), dispatched by the TL |
| **QA** | code: QE1, after merge (on `int/p3` before R1) |
| **Tier** | 2 (product flows and tooling): TL scope check |
| **Normative inputs** | AGENTS §9 ("new external processors" rule; none added here), §16 (version bump; owner notice with opt-out before processing widens); PRD §11.4, §13, §16 SEC-12, §21 R2 #7; IMPLEMENTATION_PLAN §1.5 (global DoD); decisions conflict 6, OD-24; TL ruling R59; P3-schema §5–§6 (what is stored and kept); `docs/legal/privacy-policy.md` (v5); `inbox/policy_notice.py` |

## 1. Goal

Before autonomous replies, memory suggestions or any other M04–M07 processing reach production, the published policy describes them truthfully (v5 says "Alviro doesn't send messages to other people on its own", which M06 makes false), and every owner with a consent under an older version gets one in-bot notice that says what changed and how to opt out. Autonomous sends and memory suggestions start no earlier than 24 hours after that notice (enable order, OD-24).

## 2. Scope

**In:**
1. **Text (TL):** `docs/legal/privacy-policy.md` bumped to v6 (header table, the published section between the two `---` lines, the publication record); the plain-text copy `docs/legal/privacy-policy-v6-telegraph.txt` (as v5's); publication on the same page with `tools/legal/publish_policy.py` before deploy #1, with the owner's go-ahead.
2. **Notice text (TL drafts; E2 puts it in code):** `POLICY_CHANGES["v6"]` in `inbox/policy_notice.py`.
3. **Code (E2):** `accounts/service.py::POLICY_VERSION = "v6"`; the module docstring of `inbox/policy_notice.py` mentions v6; the notice tests; every test that pins `"v5"` as the current policy version reads `service.POLICY_VERSION` instead.
4. ARCHITECTURE §14.5 (policy version) as built.

**Out:** any change to the notice mechanism (`deliver_policy_update_notices`, retries, the `policy_notice_version` marker: unchanged since v4); consent texts and processors (no new processor in P3; OpenAI stays the only AI processor); the professional privacy and onboarding tone rewrite (deferred by the owner); enabling any flag (the TL does that on the owner's word, README §6).

## 3. Design

### 3.1 What v6 must say (TL; OD-24)

One v6 covering M04–M07, in the v5 structure and plain register. Additions and changes, section by section:

| v5 section | v6 change |
|---|---|
| Information Alviro keeps | Add: drafts and replies you approve and their outcomes, and an activity log of what was sent and why (M04); triage notes on which chats need you, tasks and commitments found in chats, alerts and the daily digest, monitors you set (M05); delegations you confirm (chats, topic, allowed behaviours, limits, AI-notice wording, start and end) and knowledge you allow Alviro to share in them (M06); reminders, to-dos, follow-ups, scheduled summaries and other automations you set, note suggestions Alviro infers from your chats until you approve, edit or discard them, and your weekly rating (M07). A minute-by-minute record that Alviro was reachable, with no information about you or your chats (NFR-08) |
| How Alviro uses it | Replace "Alviro doesn't send messages to other people on its own" with: Alviro sends a message to someone only when you approve it on a card, or, inside a delegation you confirmed, as an automatic reply after telling that person that an AI is replying (on the first automatic reply of each delegation session and again after 24 hours without one, OD-32). It never starts a conversation, never claims to be you or a person, and stops automatic replies in a chat when you write there yourself, and everywhere when you tap Pause or send /pause. Note suggestions are used only after you approve them, never in replies before that, and are never kept about health, religion, politics, sexuality, finances (including debts and salary), ethnicity or race, legal or criminal matters, union membership, biometric or genetic data, immigration status, or a precise home address or location (OD-15, the final list the owner approves) |
| Service providers | Unchanged providers; add that the people in delegated chats receive the automatic replies through Telegram, and that their messages are processed by OpenAI to draft those replies (already covered for drafts; state it for automatic replies) |
| Where it is kept, and backups | Unchanged (no new location or processor; the new data classes are in the same database and backups) |
| How long it is kept | Add rows (P3-schema §6): delegations — until they end, then 1 year; knowledge entered for one delegation — until that delegation ends; saved shareable knowledge — until you delete it; note suggestions not reviewed — 14 days (OD-16); approved notes — until you delete them; tasks and reminders — open ones until done or deleted, closed ones 1 year; automations — until ended, then 1 year; drafts, replies and the activity log — 1 year; alerts — the M05 period (state the existing rule as built); weekly ratings — the score is the content-free audit row `usefulness_rated` (an activity-log entry; no new table), kept 1 year like the rest of the activity log (R59); the reachability record — 90 days |
| Your choices | Add: /pause or the Pause button stops automatic replies at once, while replies you already approved on a card are still sent (OD-31); End on a delegation (in /settings or by asking); Resume here on the one-time notice after you write in a delegated chat; approve, edit or discard each note suggestion; /export sends you a copy of your data as files in this chat |
| Deleting your data | Add: deleting a chat or a person also ends the delegations that name them, deletes their tasks, and deletes or cancels their reminders and follow-ups; "delete managed data" ends active delegations and deletes inferred notes; an exported file stays in this chat's history until you delete it in Telegram (Alviro cannot delete it) (OD-20) |
| Other people in your chats | Add: in a chat you delegate, people may receive automatic replies and are told that an AI is replying (OD-32); if they sincerely ask whether they are talking to a bot or a person, they are never told it is a person: they get a truthful answer, or no automatic reply and you are alerted (OD-10) |
| Changes | Unchanged wording |
| Contact | Unchanged |
| Publication record | Item 8: v6 drafted (date), what it adds, why it widens processing (AGENTS §16), the owner's approval date, the publication date |

The text contains no IP address, hostname of our infrastructure beyond what v5 already states, no email and no secret. The disclosure line itself is not quoted (its wording is the owner's, OD-4); the policy says the notice exists and that the owner can change its wording but never remove it.

### 3.2 The in-bot notice (`POLICY_CHANGES["v6"]`)

Plain, short, with the existing `{link}` placeholder and the opt-outs. Draft (the TL proposes, the owner approves; HTML markup limited to `<code>`, as v4/v5). **TL draft 2, 2026-10-29, written with the v6 text; awaiting the owner's approval (OD-24):**

> Alviro's privacy policy was updated (version 6). It now covers the replies Alviro prepares for you to approve, chat triage, tasks, alerts and monitors, reminders, follow-ups and scheduled summaries, note suggestions you approve or discard, and <code>/export</code> for a copy of your data. It also covers delegations and the knowledge you allow Alviro to share in them: in chats you delegate and confirm, Alviro can reply for you automatically, after telling the other person that an AI is replying. Automatic replies and note suggestions start no earlier than 24 hours after this message. Your settings stay as they are. Read it here: {link}. If you don't agree: <code>/pause</code> stops automatic replies at once, <code>/privacy revoke</code> stops all AI processing, <code>/privacy delete</code> deletes everything stored (after asking), and to stop Alviro saving your chats, remove it under Settings → Chat Automation.

Draft 1 (the planning package's wording) is in git history; draft 2 matches the v6 text's order and register, and adds "Your settings stay as they are" (as v4/v5).

Rules for the text: fixed application text, no user content, no names, no numbers other than the version and "24 hours"; every promise in it must stay true (the 24-hour rule is enforced by the enable order: enable 4 and 5 require the notice to be ≥ 24 h old, README §6).

### 3.3 Code (E2; `p3/pv6`)

1. `accounts/service.py`: `POLICY_VERSION = "v6"`.
2. `inbox/policy_notice.py`: add the `"v6"` entry (§3.2 text, or the TL's latest draft if the owner has not approved yet) with a one-line comment above it like v5's; the module docstring's version list `(policy v4, v5)` gains v6 (part of the notice text, the only docstring change). Nothing else changes: the notice goes out once per owner with a consent recorded under an older version (active or revoked), on the first `system.ingestion_watchdog` run after the deploy, settled durably by `accounts.policy_notice_version`, never resent after an uncertain delivery (M02 D59; invariant 6).
3. Consents stay valid across the bump (nothing is re-asked); new consents record `v6`.
4. Tests that pin the current version as a literal `"v5"` (at least `tests/integration/test_policy_update_notice.py`, `test_connections.py`, `test_visibility.py` on `main`; grep the CP5 tree) read `service.POLICY_VERSION`, except the one explicit assertion `POLICY_VERSION == "v6"`.
5. If the owner changes the notice wording after this PR merged, E2 opens one follow-up PR (`p3/pv6-text`) changing only the `"v6"` string, before R1.
6. Escape once: the notice is application markup; the link is escaped with `html.escape` (existing code); the gateway sanitizer keeps entities (P2 #133).

### 3.4 Order and gates (OD-24, OD-25)

1. The TL drafts the text and the notice (pre-start, docs only) and opens `docs/p3-pv6`; the leads and SQ read it; the owner approves by D+3 (11-05).
2. E2 merges the code into `int/p3` (it reaches production only with R1).
3. Before deploy #1 the TL, with the owner's go-ahead, publishes v6 on the same page (`docker compose -f compose.dev.yml run --rm app python tools/legal/publish_policy.py`; the script keeps its token under `var/legal/`, which no agent opens or prints), checks the live page shows version 6, records the date in the publication record, and merges the docs PR. The notice links to the published page, so publication always comes first.
4. Deploy #1 sends the notice. Enable 4 (`autonomy.send`) and enable 5 (`memory.extract`) wait until the notice is ≥ 24 h old (README §6, OD-24).

### 3.5 Edge cases

- `PRIVACY_POLICY_URL` unset: the notice says `/privacy` instead of a link (existing behaviour).
- An owner whose only consent was recorded under v6 (a new install after deploy #1): no notice (nothing changed for them).
- An owner who revoked consent earlier: still gets the notice (their data is still stored; existing rule).
- The notice delivery is uncertain: it is settled and never resent (existing rule); the 24-hour clock starts at the time of the attempt whose audit row says `delivered` or `uncertain` (an uncertain notice may be on screen). A failed attempt (bot blocked, network) never starts the clock: the owner has not seen the notice, and enables 4 and 5 wait.
- An attempt cut off after its claim committed (a `system_alert_claimed` row for key `policy_update:v6` with no `system_alert` row whose `claim_id` matches): it may be on screen, so it counts as uncertain for the 24-hour promise, and the clock starts at the claim's `at`. The marker stays unset and later runs get `in_progress` without resending (CP4, ICR #132; a known M05 open item; the mechanism is unchanged and never edited by PV6).

### 3.6 Failure modes

| Situation | What happens |
|---|---|
| The owner approves the text late | Publication and deploy #1's notice slip with it; enables 4 and 5 slip day for day (OD-24). The code PR may still merge into `int/p3` with the draft notice |
| Publication fails or the live page does not show version 6 | No deploy #1: the notice links to the page, so publication comes first (§3.4 step 3) |
| The notice cannot be delivered (bot blocked, no control chat) | Existing behaviour: retried once per hour, at most 24 times, then given up; the TL sees no delivered/uncertain audit row and no unresolved claim (§3.5) and holds enables 4 and 5 |
| The owner changes the wording after the code merged | One follow-up PR changing only the `"v6"` string, before R1 (§3.3 step 5) |
| A P3 behaviour changes after approval, making a sentence untrue | The TL updates the text and gets a new approval before deploy #1; after publication it is a v7 (§9 note 2) |

## 4. Files

**Text (TL; P3-interfaces §15.1):** `docs/legal/privacy-policy.md`; `docs/legal/privacy-policy-v6-telegraph.txt` (new).

**Code (E2; P3-interfaces §15.1: "`POLICY_VERSION`, `POLICY_CHANGES` and the notice text only"):** `accounts/service.py` (the constant), `inbox/policy_notice.py` (the `"v6"` entry and docstring). **Named touch points:** the version-pinning lines of the tests in §3.3 step 4 (each changed only to read `service.POLICY_VERSION`); `tests/integration/test_policy_update_notice.py` (new tests §5).

**Never edit:** the notice mechanism in `inbox/policy_notice.py` and `inbox/notify.py`; consent code (`accounts/service.py` other than the constant; `llm/consent.py`); `tools/legal/publish_policy.py`; every file on P3-interfaces §15.2.

## 5. Tests (red first)

IMPLEMENTATION_PLAN names no test for the policy; the global DoD requires the policy update before activation. `tests/integration/test_policy_update_notice.py` (the existing file; its v5 assertions move to v6):
- `test_the_current_policy_version_is_v6_and_has_a_notice` (`POLICY_VERSION == "v6"`, in `POLICY_CHANGES`, contains `{link}`)
- `test_the_v6_notice_names_every_opt_out` (`/pause`, `/privacy revoke`, `/privacy delete`, Chat Automation)
- `test_the_v6_notice_states_that_automatic_replies_need_a_confirmed_delegation_and_wait_24_hours`
- `test_an_owner_with_a_v5_consent_gets_the_v6_notice_once` (two watchdog runs; one notice; marker `v6`)
- `test_an_owner_with_only_a_v6_consent_gets_no_notice`
- `test_a_revoked_v5_consent_still_gets_the_notice`
- `test_new_consents_record_v6`
- `test_the_notice_text_holds_no_user_content` (fixed text; only `{link}` is substituted)
- the existing notice tests (retry, uncertain, settled marker) pass unchanged except the version literal

## 6. Acceptance criteria

1. The owner approved the v6 text (date recorded in the publication record); v6 is published on the same page before deploy #1.
2. `POLICY_VERSION == "v6"` on `int/p3`, then on `main` at R1; the notice goes out once per owner with an older consent on the first minute of deploy #1.
3. No promise in the policy or the notice is untrue for the P3 build; v6 names every data class P3 adds (P3-schema §5) with its retention (§6).
4. CI green; QE1's after-merge review clean; the leads and SQ read the text.
5. ARCHITECTURE §14.5 states v6.

## 7. Docs updated in the same PR

- Text PR: `docs/legal/privacy-policy.md` (header table, published text, publication record) and the plain-text copy.
- Code PR: ARCHITECTURE §14.5 (the policy version and that the v6 notice gates enables 4 and 5); PR description: the notice text as merged and whether it is the owner-approved wording.

## 8. Owner decisions

- **OD-24** (one v6 for M04–M07, approved by D+3, published at deploy #1; autonomous sends and memory suggestions ≥ 24 h after the notice): the whole unit. A late approval delays deploy #1's publication and, day for day, enables 4 and 5.
- **OD-4** (AI line wording is the owner's; never removable): §3.1 "the disclosure line is not quoted".
- **OD-10** (truthful identity answer): §3.1 "Other people".
- **OD-15** (closed sensitive categories): §3.1 "How Alviro uses it".
- **OD-16** (suggestions kept 14 days unreviewed): §3.1 retention.
- **OD-20** (`/export` file stays in Telegram): §3.1 "Deleting your data".
- **OD-12** (restore and deletion end delegations): §3.1 "Deleting your data".
- **OD-25** (production rules): §3.4.
- **OD-31** (approvals survive `/pause`): §3.1 "Your choices". If approvals lapsed on `/pause`, that sentence would say so instead.
- **OD-32** (when the AI notice re-appears): §3.1 "How Alviro uses it" and "Other people". Another reading changes only that clause.

## 9. Risks and notes

1. **The 24-hour promise** is operational, not coded: the TL checks the notice's delivery time (content-free audit rows) before enables 4 and 5. SQ's `p3-verify release` should report how many owners still lack the v6 notice, counting an owner with an unresolved `system_alert_claimed` row for `policy_update:v6` as uncertain (clock at the claim's `at`), not as lacking the notice (a request to OPS-TV3, not a contract change).
2. **Truthfulness drift.** If a P3 feature changes after the owner approves the text (for example the suggestion retention), the TL updates the text with a new approval before deploy #1; a change after publication is a v7.
3. **Notice before publication.** The code merges early, but the notice reaches production only at deploy #1, after publication (§3.4 step 3); the TL's deploy checklist holds it.
4. **Public repository.** The policy text is public; it names only the providers v5 already names and adds no infrastructure detail.
