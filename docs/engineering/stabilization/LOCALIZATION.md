# Operational wording review — I28

D-S12 retains four supported locales: Uzbek Latin (`uz-Latn`), Uzbek Cyrillic
(`uz-Cyrl`), Russian (`ru`) and English (`en`). Implementation is prepared;
container tests, CI and owner wording review have **not run**. This record does
not assert acceptance, deployment or live verification.

The owner reviews every Uzbek and Russian entry in
[`core/control_texts.json`](../../../src/assistant/core/control_texts.json).
Uzbek Cyrillic conveys the same meaning as Uzbek Latin. The inventory is:

| Review group | Catalogue keys / screens |
|---|---|
| Help and kill switch | `help`, `paused`, `resumed` |
| Settings and validation | `settings*`, `language*`, `timezone_saved`, `invalid_*`, `setting_keys`, `unknown_command`; new-chat mode labels and observed/ignored counts |
| Reply preferences | `reply_body`, `same_chat`, language/tone/length labels |
| Retention and deletion controls | `retention*`, `keep_days`, short data-class labels, `delete_all*`, `managed_delete_body`, `cannot_undo`, `arriving_note`, `backup_note` |
| Privacy and consent | `privacy*`, `consent_state`, `no_consent`, `none_named`, `managed_*`, `turn_on`, `not_now`, `revoked`, `nothing_to_revoke`, `mode_body`, `without_ai`, `stop_observing`, `observing*`, `stopped*` |
| Operational alerts | `alerts*`, `urgent_wait`, `bypass_*`, `on`, `off`, all `alert_*`, connection capability/rights/coverage notices, `policy_v4`, `policy_v5` |
| Progress and failure | `restore_warning`, `queued_deletion`, `delete_receipt`, `delete_failed`, `delete_unclear`, `delete_not_found`, `retry_*`, `expired`, `text_only`, `panel_*` availability/ingestion/coverage warnings |
| Navigation and feedback | `back`, `cancel`, `saved*`, `settings`, `confirm_first`, `nothing_changed`, `deleting`, `nothing_deleted`, `kept*`, alert feedback/open-chat labels |
| I9 scoped feedback and derived mutes (rebase follow-up) | `not_useful` (first mark, no delivery change), `feedback_muted` (threshold reached, urgent exception and recovery path), `alerts_title`, `mutes_title`, `mutes_button`, `mutes_body`, `mutes_empty`, `mute_kind_*` (all eight notification kinds), `mute_contact`, `mute_no_contact`, `mute_monitor`, `mute_reason`, `mute_reset`, `mute_reset_done`, `mutes_reset_all`, `mutes_all_reset`, `previous`, `next` |
| I20 unresolved recovery (rebase follow-up) | `consent_blocked` (both control and managed grants refused), `restore_pending`, `restore_consent_pending`; onboarding's `CONSENT_BLOCKED` is a compatibility view of the shared catalogue |
| Review fixes: per-chat privacy | `delete_chat_body`, `ignore_question`, `ignore_delete_body`, `chat_data_button`, `chat_notes_button`, `ignore_button`, `ignore_delete_button`, `confirm_delete`, `confirm_ignore`; same chat/contact scopes and retained-note exceptions, irreversible-deletion, arriving-message and backup caveats |
| Review fixes: standard alert bodies | `alert_unnamed`; `triage_urgent_*`, `triage_decision_*`, `triage_needs_reply_*`, `triage_suspicious_*` (headline/next/event); `triage_earlier`, `triage_unavailable`, `triage_historical`; `task_closed`, `task_due`, `task_earlier`, `task_evidence`, `task_status_done`, `task_status_cancelled`, `task_status_expired` |

Consent review must check the same facts in each language: control-chat scope
versus connected-chat scope; configured processor names; no model training;
already-saved messages become readable when managed processing is enabled;
storage continues while it is disabled (up to 90 days); refusal does not grant
consent; `/privacy revoke` stops processing for both scopes; settings and privacy
remain available. Restore wording must retain the warning that post-backup
deletions and revocations may have been undone. Deletion wording must distinguish
definite failure from an unknown outcome, preserve the arriving-message caveat,
and state that old encrypted backups can retain deleted data for up to 35 days.

Approval/draft cards and their recovery remain I27. This unit does not translate
model-written content or change prompts, notification scheduling, policy or consent
decisions. Other existing activity/status/conversation-detail prose is outside
this operational-screen catalogue; shared lookup is available for later wording
work without another locale system.

Validation prepared: complete catalogue/placeholder guards, rendered templates
and long values, consent-fact checks, each settings subscreen, stale buttons,
language changes followed by controls and background notices, both managed-consent
entry points, persisted consent scope/processor facts and revocation. The I28
journey interleaves controls with a fake-model owner turn in all four locales.
The lead runs the focused tests in the dev container; CI is the full-suite gate.
Owner dev-bot wording review remains A3 and requires owner participation.

Rebase follow-up: I9's scoped threshold check, owner-timezone timestamps,
pagination and reset-one/reset-all behavior are retained. I20's unresolved
privacy recovery still refuses fresh consent. New four-locale integration
regressions cover feedback before/after the threshold, scoped reset receipts,
no replay or delivery-state changes, and both refused consent entry points plus
recovery status lines. These tests have **not run**; no git staging or rebase
continuation was performed. All added Uzbek and Russian wording above awaits
owner review.

Fix-round follow-up: the localized feedback regression explicitly registers the
notification callback route, matching production wiring and the I9 test setup.
The alerts picker uses a separate `off_button` key in all four locales to retain
English “Off” while status prose retains “off”. The new Uzbek and Russian button
wording also awaits owner review. Tests were not rerun in this fix round.

Review-fix regressions cover both per-chat privacy confirmation paths with their
buttons and callback scopes in all four locales, and actual standard notification
delivery for every triage kind (current, historical and unavailable) and task
state, including resolution evidence. Dynamic names, reasons and task titles are
preserved and escaped. The delivery regression also switches language between
alerts. Catalogue guards cover the dynamically selected notification keys and
long-name privacy rendering. All new Uzbek and Russian entries listed above
await owner review. Tests and Docker commands were **not run** in this round.
