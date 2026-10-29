# RRULE — Recurrence mapper and owner-local echo (junior unit)

| | |
|---|---|
| **Unit** | RRULE — pure structured recurrence → RRULE mapper (daily, weekdays, weekly, every N days at a local time), the reverse reader, a plain-words describer and the owner-local echo formatter (exact time + timezone name) |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor** | E1 (Codex; the peer review, dispatched by the TL) |
| **QA** | QD1, after merge (on `int/p3` before R1) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | The contract only (F0-C) for the module itself. The schedule maths it builds on, `automation/schedule.py`, is M05 code and is on `int/p3` from D0. No F0 code is imported by `recurrence.py`. Only the hygiene test of step 9 needs F0-E's test helper `tests/log_hygiene.py` and its template; if F0-E is not on `int/p3` yet, step 9 waits (§6.2 step 9) |
| **Consumed by** | M7-REM (`create_reminder`, its echo and card describer), M7-AUTO (scheduled-summary recurrence), and optionally SET-P3 and RD-M7 (`recurrence_from_rule`, to describe a stored automation in plain words) |
| **Branch → base** | `p3/rrule` from `origin/int/p3` → PR to `int/p3` (D0; no stacking needed) |
| **Normative inputs** | [P3-interfaces §1.1](../contracts/P3-interfaces.md) (the module row), §8.2 (reminder shapes); [specs/M7-REM.md §3.3](M7-REM.md) (the names M7-REM calls); decisions OD-17, P3-T36; ARCHITECTURE §12.4 (timezone policies, DST rules); m05 `src/assistant/automation/schedule.py` (unchanged) |

## 1. Goal

One small pure module, `automation/recurrence.py`, that turns a structured recurrence ("every weekday at 09:00") into the RRULE string and `ScheduleSpec` the M05 scheduler already understands, reads such a rule back, describes it in plain English, and formats an instant as the owner sees it ("Fri 30 Oct 2026, 09:00 (Asia/Tashkent)"). No database, no clock, no logging, no I/O. The DST maths stays in M05's `automation/schedule.py`; you call it, never copy it.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2 (safety), §3 (workspace), §4 (branches, PR template), §5 (tests), §6.2 (Definition of Done).
3. `src/assistant/automation/schedule.py` (M05): `ScheduleSpec`, `ScheduleError`, `validate`, `to_utc`, `next_occurrence`. Note the DST rules in `to_utc`'s docstring: a time in the spring gap becomes the first valid instant; an ambiguous autumn time takes the first occurrence (`fold=0`).
4. `tests/unit/test_schedule.py` (M05): the Berlin and Tashkent examples and the style of exact expected values.
5. `src/assistant/automation/schedules.py::create_scheduled` (M05): what M7-REM does with your `ScheduleSpec` (it requires `dtstart_local` to whole minutes).
6. [specs/M7-REM.md](M7-REM.md) §3.2 and §3.3: how your four functions are called.
7. This spec, to the end, before you start.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/automation/recurrence.py` | new | The whole module (§6 gives its API) |
| `tests/unit/test_recurrence.py` | new | The tests of §7.1. Its first line is `# ruff: noqa: DTZ001` (naive wall-clock datetimes are the point of this unit; the file-level directive replaces a `pyproject.toml` edit, which you may not make; M05's `test_schedule.py` uses line-level `# noqa: DTZ001` for the same reason) |
| `tests/unit/test_log_hygiene_rrule.py` | new | The test of §7.2, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

Nothing else. In particular `src/assistant/automation/schedule.py` is read, never edited.

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/`, `src/assistant/telegram/` (including `gateway.py` and `ingress/`), `migrations/`, `src/assistant/agent/` (including `prompts/` and `prompt_registry.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/`, `src/assistant/jobs/`, `src/assistant/bootstrap.py`, `AGENTS.md`, every `docs/` file.
- Every other file in `src/assistant/automation/` (F0-R, M7-REM, M7-AUTO and M7-FUP own them).
- `pyproject.toml`, `uv.lock` (no new dependency), `.env*`, `var/`.
- Imports in `recurrence.py` other than the standard library and `assistant.automation.schedule`. Not SQLAlchemy, not `assistant.db`, not `dateutil` directly, not any logger.
- **Every file not in §3.**

## 5. Shared lists you must update

None. RRULE registers nothing: no tool, job, renderer, flag, setting, notification kind or audit event, and no exact-set test lists it.

## 6. Step-by-step

### 6.1 The exact API (`src/assistant/automation/recurrence.py`)

```python
"""Structured recurrence -> RRULE, and the owner-local echo (P3 RRULE).

Pure: no database, no clock reads, no logging, no I/O. The DST rules live in
automation/schedule.py (to_utc, next_occurrence); this module only builds the
rule they evaluate and formats instants for the owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.automation.schedule import ScheduleError, ScheduleSpec, validate

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Frequency = Literal["daily", "weekdays", "weekly", "every_n_days"]

WEEKDAYS: Final[tuple[Weekday, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_INTERVAL_DAYS: Final = 2
MAX_INTERVAL_DAYS: Final = 30


@dataclass(frozen=True, slots=True)
class Recurrence:
    frequency: Frequency
    time_local: time  # naive, whole minutes
    weekdays: tuple[Weekday, ...] = ()  # weekly only
    interval_days: int | None = None  # every_n_days only


def validate_recurrence(rec: Recurrence) -> None: ...  # raises ScheduleError
def to_rrule(rec: Recurrence) -> str: ...
def to_schedule(rec: Recurrence, *, timezone: str, starts_on: date) -> ScheduleSpec: ...
def recurrence_from_rule(rrule: str, dtstart_local: datetime) -> Recurrence | None: ...
def describe_recurrence(rec: Recurrence) -> str: ...
def echo_instant(at: datetime, timezone: str) -> str: ...
```

These names are binding (M7-REM calls `Recurrence`, `to_schedule`, `describe_recurrence`, `echo_instant`, and imports `Weekday`). Constructing a `Recurrence` never raises; the functions that take one call `validate_recurrence` first.

**Rules (each is tested in §7.1):**

1. **`validate_recurrence(rec)`** raises `ScheduleError` with a short fixed message (never the input values) when:
   - `rec.frequency` is not one of the four values (it may arrive unchecked at runtime) → `"unknown frequency"`;
   - `rec.time_local.tzinfo is not None`, or `second != 0`, or `microsecond != 0` → `"the time must be a local time in whole minutes"`;
   - `daily` or `weekdays` with `weekdays != ()` or `interval_days is not None` → `"this frequency takes no days and no interval"`;
   - `weekly` with `interval_days is not None`, or with `weekdays` empty, longer than 7, holding a value not in `WEEKDAYS`, or holding a value twice → `"weekly needs one to seven different days"`;
   - `every_n_days` with `weekdays != ()`, or `interval_days` that is `None`, a `bool`, not an `int`, or outside `MIN_INTERVAL_DAYS`–`MAX_INTERVAL_DAYS` → `"every N days needs N from 2 to 30"`.
2. **`to_rrule(rec)`** (after `validate_recurrence`) returns exactly:

   | `frequency` | RRULE |
   |---|---|
   | `daily` | `FREQ=DAILY` |
   | `weekdays` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR` |
   | `weekly` | `FREQ=WEEKLY;BYDAY=` + the given days as `MO TU WE TH FR SA SU` codes, **Monday first** whatever the input order, joined by `,` |
   | `every_n_days` | `FREQ=DAILY;INTERVAL=<n>` |

   So `weekly` with exactly Monday to Friday gives the same string as `weekdays`; that is intended (the reverse reader returns `weekdays` for it).
3. **`to_schedule(rec, *, timezone, starts_on)`** (after `validate_recurrence`, so an invalid `rec` raises `ScheduleError` before anything else) returns `ScheduleSpec(timezone=timezone, tz_policy="follow_owner", rrule=to_rrule(rec), dtstart_local=datetime.combine(starts_on, rec.time_local), run_at=None)` after calling `schedule.validate(spec)` on it (which raises `ScheduleError("Unknown timezone")` for a bad zone). `follow_owner` because a recurring routine keeps its local time when the owner moves (ARCHITECTURE §12.4). `dtstart_local` is naive and has no seconds. You do **not** compute the next occurrence: callers use `schedule.next_occurrence`. With `dateutil`, `dtstart_local` is the first occurrence only if it matches the rule (a weekly rule starting on a non-matching day starts on the next matching day); the tests in §7.1 prove both cases.
4. **`recurrence_from_rule(rrule, dtstart_local)`** is the reverse of rule 2 for the four shapes and returns `None` for anything else, never raising:
   - `None` when `dtstart_local` is aware, or has seconds or microseconds;
   - upper-case the rule, split on `;`, each part `KEY=VALUE`; `None` if a part is empty or has no `=`, or a key repeats;
   - keys exactly `{"FREQ"}` with `FREQ=DAILY` → `daily`;
   - keys exactly `{"FREQ", "INTERVAL"}` with `FREQ=DAILY` and `INTERVAL` a canonical decimal integer from 2 to 30 → `every_n_days` (canonical: `value.isascii() and value.isdigit() and str(int(value)) == value`, so `03`, `+3`, `1_0` and non-ASCII digits are refused; `INTERVAL=1` and anything else → `None`);
   - keys exactly `{"FREQ", "BYDAY"}` with `FREQ=WEEKLY` and `BYDAY` a comma list of distinct two-letter codes from `MO`…`SU` (no numeric prefix such as `1MO`) → `weekdays` when the set is exactly Monday to Friday, else `weekly` with the days Monday first;
   - every other rule (`MONTHLY`, `COUNT`, `UNTIL`, `BYMONTHDAY`, a lone `FREQ=WEEKLY`, …) → `None`;
   - `time_local = dtstart_local.time()` (the stored first occurrence's wall time).
5. **`describe_recurrence(rec)`** (after `validate_recurrence`) returns English text, times as `HH:MM` (24 h, zero-padded):
   - `daily` → `every day at 09:00`;
   - `weekdays` → `every weekday at 09:00`;
   - `weekly`, one day → `every Monday at 18:30`; two days → `every Monday and Thursday at 18:30`; three or more → `every Monday, Wednesday and Friday at 07:15` (Monday first, full day names `Monday`…`Sunday`, an Oxford comma is **not** used);
   - `every_n_days` → `every 3 days at 07:00`.
6. **`echo_instant(at, timezone)`** formats an instant the way the owner reads it:
   - `at` naive → `ValueError("at must be timezone-aware")`; `timezone` that `ZoneInfo` cannot load → `ScheduleError("Unknown timezone")` (catch `ZoneInfoNotFoundError` and `ValueError`, nothing broader).
   - `local = at.astimezone(ZoneInfo(timezone))`; text = `f"{DAY} {local.day} {MON} {local.year}, {local:%H:%M} ({timezone})"`, with `DAY` from the fixed tuple `("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")` indexed by `local.weekday()` and `MON` from `("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")` indexed by `local.month - 1`. Never `%a` or `%b` (they depend on the process locale). The day of the month is not zero-padded (`6 Nov`).
   - **Ambiguous wall time** (the autumn hour that happens twice): when `local.replace(fold=1 - local.fold).utcoffset() != local.utcoffset()`, the bracket gains the offset: `(Europe/Berlin, UTC+02:00)`. Offset format: `UTC`, sign (`+` for zero or more), hours and minutes zero-padded (`UTC+05:30`, `UTC-03:00`). Otherwise no offset.
   - The echo always shows the **resolved** instant: for a reminder asked for 02:30 on a spring-forward day the caller passes the instant `to_utc` returned, and the echo says 03:00 (truthful, §8).

### 6.2 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd2'` set, from PowerShell on the host (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR (WORKFLOW §5).

1. **Branch.** `git fetch origin`; `git switch -c p3/rrule origin/int/p3`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_schedule.py -q` → all pass (the M05 maths you build on is there).
2. **Red tests.** Create `tests/unit/test_recurrence.py` (first line `# ruff: noqa: DTZ001`) with every test of §7.1 and `src/assistant/automation/recurrence.py` with the module docstring, the type aliases, the constants, the `Recurrence` dataclass, and each function's body `raise NotImplementedError`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_recurrence.py -q` → every test fails with `NotImplementedError` (none with `ImportError` or `NameError`).
3. **`validate_recurrence`.** **Check:** `… uv run pytest tests/unit/test_recurrence.py -q -k "rejected or accepted or validation"` → those pass.
4. **`to_rrule`.** **Check:** `… -k "rrule_for"` → pass.
5. **`to_schedule`.** **Check:** `… -k "schedule"` → pass, including the Berlin DST tests.
6. **`describe_recurrence`.** **Check:** `… -k "describe"` → pass.
7. **`echo_instant`.** **Check:** `… -k "echo"` → pass.
8. **`recurrence_from_rule`.** **Check:** `… -k "from_rule or round_trip"` → pass; then the whole file passes.
9. **Hygiene test.** First **Check:** `git cat-file -e origin/int/p3:tests/log_hygiene.py` exits 0 (F0-E is merged). If it does not, push steps 1–8 as a draft, comment on the RRULE issue "hygiene test waits for F0-E", and do this step after `git fetch origin` and `git rebase origin/int/p3` once F0-E has merged (you may start from `origin/p3/f0-e` only if the TL says so on the issue). Then copy `tests/unit/test_log_hygiene_f0e.py` to `tests/unit/test_log_hygiene_rrule.py`, keep its header comment, replace its tests with §7.2's. **Check:** `… uv run pytest tests/unit/test_log_hygiene_rrule.py -q` → pass.
10. **Static checks.** `docker compose -f compose.dev.yml run --rm app uv run ruff check src/assistant/automation/recurrence.py tests/unit/test_recurrence.py tests/unit/test_log_hygiene_rrule.py`; the same files with `uv run ruff format --check`; `… uv run mypy src/assistant/automation/recurrence.py`; `… uv run lint-imports`. **Check:** all clean.
11. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the three files of §3.
12. **Commit and PR** (§12). Commit at step 2 (red tests) and at step 10 (green), each with `git commit -F <message file>`.

## 7. Named tests (red first)

Dates used below: 2026-11-02 is a Monday, so 2026-10-30 is a Friday and 2026-10-29 a Thursday. 2026-03-29 and 2026-10-25 are the last Sundays of March and October; Europe/Berlin changes at 01:00 UTC on both (UTC+1 in winter, UTC+2 in summer). Asia/Tashkent is UTC+5 with no DST. `Z` = UTC; in code write `UTC` (from `datetime`). `next_occurrence` and `to_utc` are `automation.schedule.next_occurrence` and `automation.schedule.to_utc`; "after X" means `next_occurrence(spec, after=X)`. Write each expected instant as an aware datetime in `UTC` (for example `datetime(2026, 10, 30, 4, 0, tzinfo=UTC)`).

### 7.1 `tests/unit/test_recurrence.py`

**Validation**
- `test_valid_recurrences_are_accepted` — each passes `validate_recurrence` without raising: `Recurrence("daily", time(9, 0))`; `Recurrence("weekdays", time(9, 0))`; `Recurrence("weekly", time(18, 30), weekdays=("thu", "mon"))`; `Recurrence("weekly", time(8, 0), weekdays=WEEKDAYS)`; `Recurrence("every_n_days", time(7, 0), interval_days=2)`; the same with `interval_days=30`.
- `test_invalid_recurrences_are_rejected` — parametrized; each raises `ScheduleError`: weekly with `()`; weekly with `("mon", "mon")`; weekly with `("mon", "xyz")` (`# type: ignore[arg-type]`); weekly with 8 days (`WEEKDAYS + ("mon",)`); weekly with `weekdays=("mon",)` and `interval_days=2`; daily with `weekdays=("mon",)`; daily with `interval_days=2`; weekdays with `interval_days=3`; every_n_days with `interval_days=None`; with `1`; with `31`; with `True`; every_n_days with `weekdays=("mon",)`; `time(9, 0, 30)`; `time(9, 0, 0, 1)`; `time(9, 0, tzinfo=UTC)`; frequency `"monthly"` (`# type: ignore[arg-type]`).
- `test_validation_errors_never_echo_the_input` — for weekly with `("mon", "xyz")` and every_n_days with `interval_days=31`, the message contains neither `xyz` nor `31`.

**Mapping**
- `test_rrule_for_daily` — `to_rrule(Recurrence("daily", time(9, 0))) == "FREQ=DAILY"`.
- `test_rrule_for_weekdays` — `"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"`.
- `test_rrule_for_weekly_sorts_days_monday_first` — `weekdays=("thu", "mon")` → `"FREQ=WEEKLY;BYDAY=MO,TH"`; `weekdays=("sun", "sat", "fri", "thu", "wed", "tue", "mon")` → `"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU"`.
- `test_rrule_for_every_n_days` — `interval_days=3` → `"FREQ=DAILY;INTERVAL=3"`.
- `test_rrule_for_an_invalid_recurrence_raises` — weekly with `()` → `ScheduleError`.

**Schedules**
- `test_schedule_is_follow_owner_with_a_naive_whole_minute_start` — `to_schedule(Recurrence("weekdays", time(9, 0)), timezone="Asia/Tashkent", starts_on=date(2026, 10, 30)) == ScheduleSpec(timezone="Asia/Tashkent", tz_policy="follow_owner", rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", dtstart_local=datetime(2026, 10, 30, 9, 0), run_at=None)`.
- `test_schedule_with_an_unknown_timezone_raises` — `timezone="Mars/Base"` → `ScheduleError`.
- `test_schedule_of_an_invalid_recurrence_raises` — `to_schedule(Recurrence("weekly", time(9, 0)), timezone="Asia/Tashkent", starts_on=date(2026, 10, 30))` (no days) → `ScheduleError`, and the message is `"weekly needs one to seven different days"`.
- `test_weekday_schedule_skips_the_weekend` — the spec above: after `2026-10-30T00:00Z` → `2026-10-30T04:00Z`; after `2026-10-30T04:00Z` → `2026-11-02T04:00Z`.
- `test_weekly_schedule_starting_on_a_non_matching_day_starts_on_the_next_matching_day` — `Recurrence("weekly", time(18, 30), weekdays=("mon", "thu"))`, Tashkent, `starts_on=date(2026, 10, 28)` (a Wednesday): after `2026-10-28T00:00Z` → `2026-10-29T13:30Z`; after that → `2026-11-02T13:30Z`.
- `test_every_n_days_schedule_counts_from_the_start_day` — `Recurrence("every_n_days", time(7, 0), interval_days=3)`, Tashkent, `starts_on=date(2026, 11, 1)`: after `2026-11-01T00:00Z` → `2026-11-01T02:00Z`; then `2026-11-04T02:00Z`; then `2026-11-07T02:00Z`.
- `test_a_daily_schedule_whose_time_passed_today_starts_tomorrow` — `Recurrence("daily", time(9, 0))`, Tashkent, `starts_on=date(2026, 11, 2)`: after `2026-11-02T05:00Z` → `2026-11-03T04:00Z`.
- `test_schedule_in_the_spring_gap_fires_at_the_first_valid_instant` — `Recurrence("daily", time(2, 30))`, `"Europe/Berlin"`, `starts_on=date(2026, 3, 28)`: after `2026-03-28T00:00Z` → `2026-03-28T01:30Z`; then `2026-03-29T01:00Z`; then `2026-03-30T00:30Z`.
- `test_schedule_in_the_autumn_fold_fires_once` — same rule, `starts_on=date(2026, 10, 24)`: after `2026-10-24T00:00Z` → `2026-10-24T00:30Z`; then `2026-10-25T00:30Z`; then `2026-10-26T01:30Z` (never `2026-10-25T01:30Z`).
- `test_weekday_schedule_keeps_local_time_across_spring_forward` — `Recurrence("weekdays", time(9, 0))`, Berlin, `starts_on=date(2026, 3, 27)`: after `2026-03-27T00:00Z` → `2026-03-27T08:00Z`; then `2026-03-30T07:00Z`.

**Describer**
- `test_describe_daily` — `"every day at 09:00"`.
- `test_describe_weekdays` — `"every weekday at 09:00"`.
- `test_describe_weekly_one_two_and_three_days` — `("mon",)` at 18:30 → `"every Monday at 18:30"`; `("thu", "mon")` at 18:30 → `"every Monday and Thursday at 18:30"`; `("fri", "mon", "wed")` at 07:15 → `"every Monday, Wednesday and Friday at 07:15"`.
- `test_describe_all_seven_days_lists_them` — `WEEKDAYS` at 08:00 → `"every Monday, Tuesday, Wednesday, Thursday, Friday, Saturday and Sunday at 08:00"`.
- `test_describe_every_n_days` — `interval_days=3` at 07:00 → `"every 3 days at 07:00"`.
- `test_describe_an_invalid_recurrence_raises` — weekly with `()` → `ScheduleError`.

**Echo**
- `test_echo_shows_the_local_day_time_and_zone` — `echo_instant(datetime(2026, 10, 30, 4, 0, tzinfo=UTC), "Asia/Tashkent") == "Fri 30 Oct 2026, 09:00 (Asia/Tashkent)"`.
- `test_echo_does_not_pad_the_day` — `datetime(2026, 11, 6, 4, 0, tzinfo=UTC)`, Tashkent → `"Fri 6 Nov 2026, 09:00 (Asia/Tashkent)"`.
- `test_echo_accepts_any_aware_input` — `datetime(2026, 10, 30, 9, 0, tzinfo=ZoneInfo("Asia/Tashkent"))` → `"Fri 30 Oct 2026, 09:00 (Asia/Tashkent)"`.
- `test_echo_of_a_spring_gap_reminder_shows_the_resolved_time` — `echo_instant(to_utc(datetime(2026, 3, 29, 2, 30), "Europe/Berlin"), "Europe/Berlin") == "Sun 29 Mar 2026, 03:00 (Europe/Berlin)"`.
- `test_echo_of_an_ambiguous_autumn_time_adds_the_offset` — `datetime(2026, 10, 25, 0, 30, tzinfo=UTC)`, Berlin → `"Sun 25 Oct 2026, 02:30 (Europe/Berlin, UTC+02:00)"`; `datetime(2026, 10, 25, 1, 30, tzinfo=UTC)` → `"Sun 25 Oct 2026, 02:30 (Europe/Berlin, UTC+01:00)"`.
- `test_echo_of_an_unambiguous_berlin_time_has_no_offset` — `datetime(2026, 10, 25, 3, 0, tzinfo=UTC)` → `"Sun 25 Oct 2026, 04:00 (Europe/Berlin)"`.
- `test_echo_rejects_a_naive_time_and_an_unknown_zone` — naive `datetime(2026, 10, 30, 9, 0)` → `ValueError`; `"Mars/Base"` → `ScheduleError`.

**Reverse reader**
- `test_from_rule_reads_the_four_shapes` — with `dtstart = datetime(2026, 11, 2, 9, 0)`: `"FREQ=DAILY"` → `Recurrence("daily", time(9, 0))`; `"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"` → `Recurrence("weekdays", time(9, 0))`; `"FREQ=WEEKLY;BYDAY=TH,MO"` → `Recurrence("weekly", time(9, 0), weekdays=("mon", "thu"))`; `"FREQ=DAILY;INTERVAL=3"` → `Recurrence("every_n_days", time(9, 0), interval_days=3)`; `"freq=daily"` → `Recurrence("daily", time(9, 0))`.
- `test_from_rule_returns_none_for_any_other_rule` — parametrized, each `None`: `"FREQ=MONTHLY;BYMONTHDAY=1"`, `"FREQ=DAILY;COUNT=2"`, `"FREQ=DAILY;INTERVAL=1"`, `"FREQ=DAILY;INTERVAL=31"`, `"FREQ=DAILY;INTERVAL=x"`, `"FREQ=DAILY;INTERVAL=03"`, `"FREQ=DAILY;INTERVAL=1_0"`, `"FREQ=WEEKLY"`, `"FREQ=WEEKLY;BYDAY=MO,MO"`, `"FREQ=WEEKLY;BYDAY=1MO"`, `"FREQ=WEEKLY;BYDAY=XX"`, `"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29"`, `"FREQ=DAILY;FREQ=DAILY"`, `"FREQ=DAILY;;"`, `""`; and `"FREQ=DAILY"` with `datetime(2026, 11, 2, 9, 0, 30)` and with `datetime(2026, 11, 2, 9, 0, tzinfo=UTC)`.
- `test_round_trip_returns_the_same_recurrence` — for each of `Recurrence("daily", time(6, 5))`, `Recurrence("weekdays", time(9, 0))`, `Recurrence("weekly", time(18, 30), weekdays=("mon", "thu"))`, `Recurrence("weekly", time(8, 0), weekdays=WEEKDAYS)`, `Recurrence("weekly", time(10, 0), weekdays=("sat",))`, `Recurrence("every_n_days", time(7, 0), interval_days=2)`: `recurrence_from_rule(to_rrule(r), datetime.combine(date(2026, 11, 2), r.time_local)) == r`. (Weekly days in this list are Monday first; weekly Monday-to-Friday is left out because it reads back as `weekdays`, by rule 2.)

### 7.2 `tests/unit/test_log_hygiene_rrule.py`

- `test_recurrence_functions_log_nothing` — inside `with capture_logs() as output:` (F0-E's `tests/log_hygiene.py`), call each of the six public functions (`validate_recurrence`, `to_rrule`, `to_schedule`, `recurrence_from_rule`, `describe_recurrence`, `echo_instant`) once with valid input and once with input that raises or returns `None` (a weekly recurrence with no days; `recurrence_from_rule("FREQ=MONTHLY", …)`; `echo_instant` with a naive time and with `"Mars/Base"`; catch `ScheduleError`/`ValueError` in the test); then assert `output == []`. Build every naive datetime in this file with `datetime.fromisoformat("2026-11-02T09:00")` (and the aware ones with `datetime.fromisoformat("2026-10-30T04:00:00+00:00")`), never `datetime(...)`, so this file needs no `DTZ001` suppression.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **The echo states what will happen, not what was asked.** A reminder asked for 02:30 on 29 Mar 2026 in Berlin fires at 03:00; the echo says 03:00 (`test_echo_of_a_spring_gap_reminder_shows_the_resolved_time`). Never format the requested wall time.
- **The doubled autumn hour is never ambiguous in the echo:** the offset tells the two 02:30s apart.
- **The timezone name is always shown,** so "09:00" never means the wrong zone.
- **Unknown shapes are unknown:** `recurrence_from_rule` returns `None` rather than a guess, so a reader shows the raw schedule instead of a wrong sentence.
- **"Every 3 days" counts from the start day,** not from today: the describer says only "every 3 days at HH:MM", and callers echo the next instant from `next_occurrence`, never a computed guess.
- **Weekly with all seven days stays weekly** in the describer (it lists the days); it is never silently turned into another rule.
- **No time read:** nothing here knows "now", so nothing here can say "today" or "tomorrow".
- **The reverse reader never raises and never guesses:** any input it does not recognise exactly (an extra key, `INTERVAL=03`, `COUNT`, a numeric `BYDAY` prefix, an aware or non-minute `dtstart_local`) gives `None`, so a stored rule is never shown as a different schedule.
- **Messages never carry input.** A `ScheduleError` text is one of the fixed strings of §6.1, so a bad value is never echoed into a log, a card or a model prompt.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing with `NotImplementedError`, and passes.
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the three files of §3.
- [ ] `recurrence.py` imports only the standard library and `assistant.automation.schedule`; `lint-imports` is clean.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) are clean on the changed files; the only suppressions are the `# ruff: noqa: DTZ001` first line of `test_recurrence.py` (none in `test_log_hygiene_rrule.py`) and the `# type: ignore[arg-type]` comments on the lines that build a `Recurrence` with an invalid `Weekday` or `Frequency` (§7.1; no unused ignore).
- [ ] No function reads the clock, the database or the environment; nothing logs.
- [ ] No `assert` in `recurrence.py`; no broad `except`; only `ZoneInfoNotFoundError` and `ValueError` are caught, and only where §6.1 says.
- [ ] `ScheduleError` messages are the fixed strings of §6.1 and never contain input values.
- [ ] Docstrings say *why* (the DST rules are `schedule.py`'s; `follow_owner` keeps local time; `%a`/`%b` are avoided because of locales); no other comments are needed.
- [ ] The PR description names the six public functions and `Weekday`, for M7-REM, M7-AUTO, SET-P3 and RD-M7.

## 10. When to stop and ask

Ask on the RRULE issue, starting the comment with "Question for E1:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- An expected value in §7 looks wrong when you work it out by hand: do **not** change it; ask.
- `ZoneInfo("Europe/Berlin")` or `ZoneInfo("Asia/Tashkent")` fails inside the container.
- `schedule.next_occurrence` gives a different instant than §7 for a spec built exactly as §6.1 rule 3 says.
- You think you need an import outside the standard library and `assistant.automation.schedule`, or any file outside §3.
- M7-REM's spec or code calls a name that is not in §6.1.
- `ruff check` reports `RUF100` (unused suppression) for the `DTZ001` directive: do not add other suppressions; ask.
- You have spent more than half a day and are not close.

**Contract gaps already known (the TL tracks them; do not wait on them):** P3-interfaces §1.1 names the module but does not freeze the signatures of §6.1 (M7-REM §3.3 asks the TL to copy them into the contract); the example comment in M7-REM §3.3 says "Thu 30 Oct 2026", but 2026-10-30 is a Friday (this spec's tests say "Fri"); the `DTZ001` suppression is file-level because tests may not edit `pyproject.toml`.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); catch only the exceptions §6.1 names.
- No `assert` in production code, neither for control flow nor for type narrowing.
- Full type hints; `mypy --strict` clean; names exactly as §6.1.
- No `datetime.now()`, no clock; everything comes in as an argument.
- No logging at all in this module.
- No Telegram, no database, no files.
- GitHub: every PR body and comment through `--body-file <file>` (never an inline body in PowerShell); no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/` or real data.

## 12. Pull request

- **Title:** `RRULE: recurrence mapper and owner-local echo`.
- **Size:** about 400 changed lines or fewer (expected ≈ 150 code + ≈ 200 tests).
- **Labels:** `team:david`, `eng:d2` on the draft; when CI is green, mark it ready and add `needs-review:e1` and `needs-qa`; comment on the issue that it is ready.
- **Template** (WORKFLOW §4.3), filled honestly:
  - *Unit:* `WU: RRULE · Spec: docs/engineering/p3/specs/RRULE.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: after merge.
  - *Safety invariants touched:* none (a pure helper; nothing sends, stores or reads content).
  - *Tests:* the §7 names; the commands you ran and their result, or "not run locally: <reason>; CI is the gate".
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column; logs: `test_log_hygiene_rrule.py`.
  - *Prompts, flags and settings:* none.
  - *Docs:* none (RRULE has no ARCHITECTURE section; M7-REM documents reminders).
  - *Interface changes:* none; out-of-list edits: none.
  - *Open items for M07.md:* anything non-blocking the review raised.
- After merge: QD1 checks it on `int/p3`; a finding becomes one fix PR from you.
