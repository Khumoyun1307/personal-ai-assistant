# F0-E — eval, test-list, CI and ops seams

| | |
|---|---|
| **Unit** | F0-E — eval-target registry, derived exact-set test lists, prompt-registry reshape and registry-read labels, scenario packages, log-hygiene convention, CI on `int/p3`, `server.sh` `report`/`p3-verify`, `p3_verify.py` skeleton |
| **Team / slot** | Edvard / E3 |
| **Size** | M+ (≈ 1.25 engineer-days) |
| **Depends on** | CP5, F0-S (flags: `core/flags.py::P3_FEATURE_FLAGS`, `P3_TOOL_FLAGS`). Rebases on `p3/f0-s` and `p3/f0-r` (it reads `default_registry()` after F0-R's tuple refactor) and **merges third** (P3-interfaces §15.5 step 1) |
| **Branch → base** | `p3/f0-e` → `int/p3` (draft stacked on `p3/f0-r` until it merges) |
| **Peer reviewer** | a Josh engineer (Claude, the TL picks) + SQ (shared test lists; `p3_verify` is SQ's tool after CP1) |
| **QA** | QE1, after merge (on `int/p3` before R1) |
| **Tier** | 2 (product flows and tooling): TL scope check |
| **Normative inputs** | P3-interfaces §9.2, §14.1–§14.3, §15.1; decisions P3-T27; TL rulings R55, R56, R57, R77; WORKFLOW §5, §6.5; AGENTS §5 (rule 4: `tools/` outside `src/`), §8 (evals), §9, §11; P2-T41, P2-T44, P2-T45 |

## 1. Goal

Remove the shared exact-set lists and hot files that cost P2 most of its CP4 conflicts, before any P3 unit needs them: a target registry that every eval CLI list derives from, test lists derived from registries, one isolated line per prompt key, scenario packages whose driver never errors on an unbuilt task, a per-unit log-hygiene file convention, CI on every `int/p3` merge, and generic server actions so ops units add only SQL or tool files. Behaviour-neutral for the product: no code under `src/assistant/` changes except `agent/prompt_registry.py`'s layout.

## 2. Scope

**In:**
1. `evals/registry.py` (new) and the derived lists in `evals/cli.py`, `evals/targets.py`, `evals/live.py`, `evals/agent_mode.py`.
2. Derived exact-set lists in `tests/integration/agent_env.py`, `tests/unit/test_tool_registry.py` (managed set; the pinned "active version" test made version-agnostic), `tests/unit/test_e1_dataset.py` (manifest set).
3. `agent/prompt_registry.py` reshape (`ACTIVE` one line per key, `PROMPT_HISTORY`, commented placeholders) and registry-read labels in `tests/integration/test_agent_runtime.py` and `tests/scenarios/test_find_the_agreed_meeting_time.py`.
4. `tests/scenarios/p3_drive.py` (new driver) and `tests/scenarios/{m06,m07,p3_cross}/{__init__,conftest}.py`.
5. `tests/log_hygiene.py` (helper) and the template `tests/unit/test_log_hygiene_f0e.py`.
6. `.github/workflows/ci.yml`: `push: branches: [main, int/p3]`.
7. `deploy/server.sh`: generic `report <name> [days]` and `p3-verify <args>`; `trial-check` passes through to the P3 verifier (§3.7, contract gap noted).
8. `tools/acceptance/p3_verify.py` skeleton and the four empty subcommand modules.
9. ARCHITECTURE §16 and §17 as built.

**Out:** any eval target body (EV-AUT, EV-GATE, EV-E4L); any check, view or report subcommand (OPS-TV3, OPS-VIEWS, OPS-RT3s); any SQL file (OPS-AV, OPS-RATE); any scenario (QA-S6, QA-S7, QA-X); any prompt file or version switch (DELEG-DRAFT, M7-MEX, PR-OR10).

## 3. Design

Paths under `src/assistant/` unless rooted. The CP5 tree has `main`'s targets (`retrieval`, `summary`, `drafts`) and `int/m05`'s (`retrieval`, `summary`, `triage`, `monitors`) merged; build on what CP5 has.

### 3.1 Eval-target registry (`evals/registry.py`; P3-interfaces §14.1)

1. `EvalTargetEntry` exactly as §14.1 (`name`, `datasets`, `run_type`, `module`, `factory`), plus two optional fields pending the contract fix (§9 note 1): `review: str | None = None` and `comparison: str | None = None`, each a `"module:attribute"` path. `factory`, `review` and `comparison` are imported lazily (`importlib.import_module`), so importing `evals.registry` imports no target module.
2. `TARGETS` lists the CP5 targets, one per line, each under a comment naming its owner, then EV-AUT's commented line:

   | `name` | `datasets` | `run_type` | `module` / `factory` | `review` | `comparison` |
   |---|---|---|---|---|---|
   | `retrieval` | `("e1", "retrieval")` | `owner_request` | `evals.cli` / `_retrieval` | — | — |
   | `summary` | `("e1",)` | `summarize` | `evals.cli` / `_summary` | — | — |
   | `drafts` | `("e2",)` | `draft_reply` | `evals.cli` / `_drafts` | `evals.cli:_review` (the CP5 `review drafts` body) | — |
   | `triage` | `("e3",)` | `triage` | `evals.cli` / `_triage` | — | `evals.triage:TRIAGE_COMPARISON` |
   | `monitors` | `("e3",)` | `evaluate_monitor` | `evals.cli` / `_monitors` | — | `evals.monitors:MONITOR_COMPARISON` |
   | `# EvalTargetEntry("autonomy", ("e4", "e2"), "draft_reply", "evals.autonomy", "build"),  # EV-AUT` | | | | | |

   Use the CP5 function names as they are; if a name differs at CP5, use it and say so in the PR.
3. Helpers: `names() -> tuple[str, ...]` (registration order), `get(name) -> EvalTargetEntry` (`KeyError` for an unknown name), `routes() -> frozenset[str]`, `datasets() -> frozenset[str]`, `load(path: str) -> object`. A duplicate `name` raises `ValueError` at import.
4. `evals/targets.py`: `DatasetName = Literal["e1", "e2", "e3", "e4", "retrieval"]` (adds `e4`; `e3` comes from CP5).
5. `evals/cli.py`: `run` target `choices=list(registry.names())`; `main` dispatches through `registry.load(f"{entry.module}:{entry.factory}")(args)`, replacing the `if args.target == …` chain (each CP5 runner keeps its body); `review` choices = entries with `review`, dispatched the same way; `COMPARISONS = {c.target: c for c in (registry.load(e.comparison) for e in TARGETS if e.comparison)}`. Every CLI flag, help text, exit code and refusal message stays as it is.
6. `evals/live.py`: the effort map is derived: for each route in `registry.routes() | {"owner_request"}`, the effort is `getattr(settings, f"llm_{route}_effort")` when that setting exists, else `_FIXED_EFFORTS.get(route)` (the existing private fallback). `resolve_model` keeps its signature, and its settings-less path (`LiveSetupError` with an override) still returns `_FIXED_EFFORTS.get(route)`. One deliberate alignment: on `main` `_FIXED_EFFORTS` pins `draft_reply` to `DraftConfig`'s default (`low`) over the setting, while the product sends `LLM_DRAFT_REPLY_EFFORT` (bootstrap's `draft_config`); the derived map uses the setting, so the eval measures what runs. Under the default `low` nothing changes; the PR states it. Unit test per CP5 route under default settings.

### 3.2 Derived exact-set lists (P2-T41; WORKFLOW §6.5)

1. **`evals/agent_mode.py::PRODUCTION_FLAGS`** becomes `frozenset(tool.release_flag for tool in default_registry().all()) | BACKGROUND_FLAGS`, with `BACKGROUND_FLAGS = frozenset({"summary.refresh"})` the one explicit background set. At CP5 the derived set must equal the CP5 literal (the existing superset test already forces the literal to cover every registered tool); the PR shows the one-off comparison, so the CP4/CP5 agent-mode baseline that PR-OR10's re-run compares against stays comparable. P3 tool flags join automatically when their units append to their `TOOLS`. The comment says why: the agent-mode eval measures the prompt with every tool the product can offer.
2. **`tests/integration/agent_env.py`**: `PRODUCTION_FLAGS` is imported from `evals.agent_mode`; `PRODUCTION_LINE = ",".join(sorted(PRODUCTION_FLAGS))` (callers that read the line keep working). Add `P3_TOOL_FLAGS = core.flags.P3_TOOL_FLAGS` for P3 agent tests. `READ_FLAGS`, `PROPOSE_FLAGS`, `AUTONOMY_FLAGS`, `MEMORY_FLAGS`, `DELETION_FLAGS` stay explicit (each pins the tool offer an older test was written against).
3. **Managed set** (`tests/unit/test_tool_registry.py::test_the_blocked_stub_returns_no_message_data_and_makes_no_db_read_of_content`, the S03.7 B5 test): replace the literal set with `EXPECTED_MANAGED = P2_MANAGED | P3_MANAGED`, where `P2_MANAGED` is today's literal and `P3_MANAGED = frozenset({"list_delegations", "list_tasks", "list_automations"})` (the P3 `returns_managed` tools, P3-interfaces §9.4); assert `managed == {n for n in EXPECTED_MANAGED if n in registered}`. A tool that starts returning managed content without being in the contract still fails the test; a P3 read tool merging later needs no edit.
4. **Manifest set** (`tests/unit/test_e1_dataset.py::test_manifest_matches_the_dataset_files`): keep `manifest.problems() == []`; replace the literal file set with: every frozen file sits directly in a directory named by `get_args(DatasetName)`, and every directory named by a registry target's `datasets` has at least one frozen file. A new data file (DATA-E4b's `e4/decoys.jsonl`, DATA-E3F) needs no test edit; a file in an unknown directory fails.
5. **Flag consistency** (`tests/unit/test_flag_sets.py`, new): the test holds one literal `P2_TOOL_FLAGS` (the CP5 tool flags, written out once in this file, never edited after F0-E); every registered tool's `release_flag` is in `P2_TOOL_FLAGS` or in `P3_TOOL_FLAGS`, so a tool flag that is in neither fails CI; `P3_FEATURE_FLAGS` and `P3_TOOL_FLAGS` are disjoint, every `P3_TOOL_FLAGS` member starts with `tool.` and no `P3_FEATURE_FLAGS` member does.

### 3.3 Prompt registry (`agent/prompt_registry.py`; P3-interfaces §9.2)

1. `ACTIVE: dict[str, str]` holds one line per key, each under a one-line comment, in this order: `owner_request`, `summarize`, `draft_reply`, `triage`, `evaluate_monitor` (the CP5 keys), then two commented placeholders:
   ```python
   # draft_delegated: DELEG-DRAFT sets this line (P3-interfaces §9.2).
   # "draft_delegated": "v1",
   # extract_memory: M7-MEX sets this line.
   # "extract_memory": "v1",
   ```
   A prompt owner uncomments its own line and never edits F0-E's placeholder test, which accepts either state (R77, §5.3).
2. `PROMPT_HISTORY: Mapping[str, str]` (a `MappingProxyType`) holds the version notes now in comments inside `ACTIVE`, one entry per key, moved word for word. A prompt owner edits only its own `ACTIVE` line and its own `PROMPT_HISTORY` entry; only PR-OR10 edits `owner_request`'s.
3. `Prompt`, `load`, `active` unchanged.
4. **Registry-read labels:** every test that pins an active label as a literal (`"owner_request.v8"`/`"v9"` in `tests/integration/test_agent_runtime.py` and `tests/scenarios/test_find_the_agreed_meeting_time.py`; grep for others) reads `prompt_registry.active("owner_request").label` instead, so PR-OR10's switch needs no test edit there.
5. **Version-agnostic pin** in `tests/unit/test_tool_registry.py`: the CP5 test `test_owner_request_v9_is_active_and_older_versions_remain` (named `v8` before CP4) becomes `test_owner_request_v1_to_v9_remain_and_the_active_version_is_at_least_v9` (every file `v1`…`v9` loads; the active version number ≥ 9). The content tests (`test_owner_request_v7_is_v6_plus_…`, `…_v8_is_v7_plus_…`, the CP4 `v9` one) are fixed files and stay as they are.

### 3.4 Scenario packages (`tests/scenarios/`; P2-T44)

1. `tests/scenarios/p3_drive.py` (new): `p3_environment(migrated_url, *, flags: frozenset[str]) -> AsyncContextManager[P3Scenario]` built on the CP5 scenario environment (`p2_drive.m04_environment` plus the M05 delivery and triage wiring `m05_drive` uses), with the P3 wire functions called through a test `P3WireContext` (`FakeLLM`, `FakeChannel`, `FakeClock`, `FakeTelegramSender`, `FakeAutonomyPlanner`/`FakeDelegatedDrafter` available), so a scenario exercises the units' real wiring.
2. `P3Scenario.run_task(name, **args)` and `P3Scenario.run_periodic(name)`: when `name` is not in `app.tasks`, call `pytest.xfail(f"{name} not registered yet")` (never `assert`, never raise from a fixture): a strict-xfail scenario stays xfailed until its unit lands, and an unmarked scenario reports the missing task as an expected failure instead of an error. `P3Scenario.require(*names)` does the same check up front. Every helper returns ids and states only, never message text.
3. `tests/scenarios/m06/`, `m07/`, `p3_cross/`: `__init__.py` and a `conftest.py` each, with one fixture (`m06`, `m07`, `p3x`) yielding a `P3Scenario` with the package's default flags (none; scenarios set their own). No scenario file: QA-S6, QA-S7 and QA-X own them.
4. Self-test `tests/scenarios/test_p3_drive.py`.

### 3.5 Log-hygiene convention

1. `tests/log_hygiene.py` (new helper): `CANARY = "CANARY-SYNTH-P3"`, `capture_logs() -> ContextManager[list[str]]` (the structlog + stdlib capture the existing `test_log_hygiene.py` uses), `assert_content_free(output, *canaries)`. No production code.
2. `tests/unit/test_log_hygiene_f0e.py`: the template, with a header comment telling units to copy it as `test_log_hygiene_<unit>.py` (under `tests/integration/` when the database is needed) and never to edit another unit's file. Its own tests cover F0-E's output: `p3_verify` prints no canary (§3.8) and `evals.registry` logs nothing.
3. The existing `tests/integration/test_log_hygiene.py` (RG-10) is unchanged.

### 3.6 CI (`.github/workflows/ci.yml`)

`on.push.branches: [main, int/p3]` with the same `paths-ignore` list; `pull_request` unchanged (already covers PRs to `int/p3`). Nothing else in the workflow changes. The TL needs a green `push` run on `int/p3` after each merge batch (WORKFLOW §7).

### 3.7 Server actions (`deploy/server.sh`; P3-interfaces §14.3)

1. `report)`: `name="${1:-}"`; refuse unless `[[ "$name" =~ ^[a-z0-9_]+$ ]]` and `[[ -f "deploy/sql/$name.sql" ]]` (the file list is the whitelist); `days="${2:-7}"` checked with `^[1-9][0-9]{0,3}$`; then exactly the `nfr04)` invocation (`require_data_volume`; `compose exec -T db sh -c 'export PGOPTIONS="-c default_transaction_read_only=on"; exec psql -X -q -v ON_ERROR_STOP=1 -v days="$1" …' sh "$days" < "deploy/sql/$name.sql"`). `days` reaches SQL only as a psql variable.
2. `p3-verify)`: the `p2-verify)` block with `tools/acceptance/p3_verify.py "$@"` (one-off container, the checkout's `tools/acceptance` mounted read-only).
3. `trial-check)`: passes through to `tools/acceptance/p3_verify.py trial-check "$@"` (OPS-TV3 registers the subcommand; until then the CLI exits 2 "not available"). The M04 trial is over by R1, so the action moves to the P3 verifier then (R56, accepted); it reaches production only at R1, when OPS-TV3 is merged (WORKFLOW §7 R1 conditions), and `main` keeps the P2 behaviour until then. The contract adopts this text (R32). The one existing assertion that changes is the last line of `tests/unit/test_server_sh.py::test_trial_check_uses_the_same_read_only_mount_and_passes_arguments_unchanged` (`p2_verify.py trial-check` → `p3_verify.py trial-check`); its other assertions (data volume, one-off container, read-only mount) stay.
4. `nfr04)`, `nfr05)` and `p2-verify)` stay as they are; the usage line adds `report NAME [DAYS]|p3-verify ARGS`.

### 3.8 `tools/acceptance/p3_verify.py` (P3-interfaces §14.2)

1. `Subcommand` dataclass exactly as §14.2; `SUBCOMMAND_MODULES = ("p3_checks", "p3_views", "p3_report", "p3_restore_check")`; each module `tools/acceptance/<name>.py` is created with a docstring naming its owner and `SUBCOMMANDS: tuple[Subcommand, ...] = ()`.
2. `parser(modules=SUBCOMMAND_MODULES) -> argparse.ArgumentParser`: imports each module, collects `SUBCOMMANDS`, raises `ValueError` on a duplicate name, adds one subparser per subcommand with its `add_arguments`.
3. `main(argv=None) -> int`: no subcommand registered or none given ⇒ usage on stderr, exit 2; otherwise open one connection with `open_database(url)` (the `p2_verify.open_database` pattern: `default_transaction_read_only=on`, `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`), call `run(conn, args)`, return its code (0 pass, 1 FAIL, 2 error or NOT EVALUABLE). An unexpected exception prints only its class name and returns 2. The database URL comes from `DATABASE_URL` as in `p2_verify.py`; nothing prints it.
4. Standard library + psycopg only; never imported by `src/` (AGENTS §5 rule 4).

### 3.9 Failure modes (fail closed)

| Situation | Behaviour |
|---|---|
| Unknown eval target or review name | Refused with the CP5 message and exit code; nothing imported, no spend |
| Duplicate `EvalTargetEntry.name` or `Subcommand.name` | `ValueError` at import / parser build (CI fails) |
| A registry path that does not resolve | `test_every_factory_review_and_comparison_path_resolves` fails in CI; at run time the CLI exits with the import error class only |
| `report` name with `/`, `.`, a capital, or no matching `deploy/sql/<name>.sql`; a bad `days` | Exit 2 before any `compose` call |
| `p3_verify` with no subcommand registered, or an unexpected exception | Exit 2; only the exception class name is printed (never a row, a URL or a value) |
| A scenario calls a task that is not registered yet | `pytest.xfail`, never an error or a pass |
| A tool flag in neither `P2_TOOL_FLAGS` nor `P3_TOOL_FLAGS`, or a managed tool outside the contract | `test_flag_sets.py` / the B5 test fails |

## 4. Files

**Owned (P3-interfaces §15.1):**

| File | After CP1 |
|---|---|
| `evals/registry.py` (new) | one line per target owner (EV-AUT adds `autonomy`) |
| `evals/{cli,targets,live,agent_mode}.py` | nobody |
| `agent/prompt_registry.py` (layout) | each prompt owner: its own `ACTIVE` line and `PROMPT_HISTORY` entry |
| `tests/integration/{agent_env,test_agent_runtime}.py`, `tests/scenarios/test_find_the_agreed_meeting_time.py`, `tests/unit/{test_tool_registry,test_e1_dataset}.py` | nobody |
| `tests/unit/test_flag_sets.py` (new) | nobody |
| `tests/scenarios/p3_drive.py` (new) | SQ owns it after CP1 (R57; QJ1 and QE1 ask SQ for helpers) |
| `tests/scenarios/{m06,m07,p3_cross}/{__init__,conftest}.py` (new) | QA-S6 (QJ1), QA-S7 (QE1), QA-X (SQ) |
| `tests/scenarios/test_p3_drive.py` (new) | SQ |
| `tests/log_hygiene.py`, `tests/unit/test_log_hygiene_f0e.py` (new) | nobody (units copy the template) |
| `.github/workflows/ci.yml`, `deploy/server.sh` | nobody |
| `tools/acceptance/p3_verify.py`; `tools/acceptance/{p3_checks,p3_views,p3_report,p3_restore_check}.py` (new, empty) | SQ (OPS-TV3, OPS-VIEWS); OPS-RT3s for `p3_restore_check.py` |

**Named touch points:** `tests/unit/test_server_sh.py` (the `trial-check` assertion follows §3.7 step 3; new tests §5.6); `tests/integration/test_eval_retrieval.py` (its `PRODUCTION_FLAGS` superset test stays and now holds by construction).

**Never edit:** every file on P3-interfaces §15.2; any prompt file; `evals/datasets/**` and `MANIFEST.sha256`; any `deploy/sql/*.sql`; `tools/acceptance/p2_verify.py` (P2's verifier stays for P2 views).

## 5. Tests (red first)

IMPLEMENTATION_PLAN names no test for this unit. Every existing test stays green; the only existing tests that change are the list-holding ones in §3.2–§3.3 and the `trial-check` source assertion (§3.7), each changed as stated there and nowhere else. All RG tests stay as they are.

### 5.0 Existing tests that prove behaviour neutrality (unchanged)

`tests/unit/test_eval_cli.py`, `tests/unit/test_eval_budget.py`, `tests/unit/test_eval_triage.py`, `tests/unit/test_eval_monitors.py`, `tests/unit/test_eval_summary.py`, `tests/unit/test_eval_drafts.py`, `tests/integration/test_eval_retrieval.py` (its `PRODUCTION_FLAGS` superset test now holds by construction), `tests/integration/test_eval_summary.py`, `tests/integration/test_eval_triage.py`, `tests/integration/test_eval_monitors.py`, `tests/integration/test_eval_drafts.py`; `tests/unit/test_tool_registry.py` (RG-17 tests and the v7/v8/v9 prompt content tests); `tests/integration/test_agent_runtime.py` and `tests/scenarios/test_find_the_agreed_meeting_time.py` (only their label literals re-read from the registry); `tests/unit/test_server_sh.py` (`test_p2_verify_uses_a_one_off_read_only_tool_mount`, `test_server_usage_lists_the_daily_trial_check` and the restore tests unchanged); `tests/unit/test_ci_split.py`; `tests/integration/test_log_hygiene.py` (RG-10). QE1 confirms in the review that no other assertion line changed.

### 5.1 `tests/unit/test_eval_registry.py`
- `test_targets_have_unique_names`
- `test_importing_the_registry_imports_no_target_module`
- `test_every_target_dataset_is_a_dataset_name` (each `datasets` member ∈ `get_args(DatasetName)`)
- `test_every_factory_review_and_comparison_path_resolves`
- `test_cli_run_choices_come_from_the_registry`
- `test_cli_review_choices_are_the_targets_with_a_review`
- `test_comparisons_come_from_the_registry` (the CP5 `triage` and `monitors` comparisons, same objects)
- `test_the_effort_map_matches_cp5_for_every_route` (`owner_request`, `summarize`, `draft_reply`, `triage`, `evaluate_monitor`)
- `test_an_unknown_target_is_refused_with_the_cp5_message`

### 5.2 Derived lists
- `tests/unit/test_flag_sets.py`: `test_production_flags_are_every_registered_tool_flag_plus_the_background_set`, `test_agent_env_production_line_equals_production_flags`, `test_every_registered_tool_flag_is_known`, `test_p3_feature_and_tool_flag_sets_are_disjoint_and_prefixed`.
- `tests/unit/test_tool_registry.py`: the B5 managed-set test as §3.2 step 3 (renamed nothing).
- `tests/unit/test_e1_dataset.py`: `test_manifest_matches_the_dataset_files` as §3.2 step 4; plus `test_a_frozen_file_outside_a_known_dataset_directory_fails` (temporary directory, `manifest` pointed at it).

### 5.3 `tests/unit/test_prompt_registry_layout.py`
- `test_each_active_key_is_on_its_own_line_under_a_comment` (parses the module source)
- `test_every_active_prompt_file_exists`
- `test_prompt_history_has_one_entry_per_active_key`
- `test_the_draft_delegated_and_extract_memory_lines_are_a_commented_placeholder_or_an_active_line_whose_prompt_file_exists` (R77: parses the module source; for each of the two keys the line is either the commented placeholder or an active entry whose `<key>.<version>.md` file exists, so DELEG-DRAFT and M7-MEX uncomment their lines without editing this test)
- `test_active_returns_the_same_prompt_as_before_the_reshape` (each CP5 key → same label and text)

### 5.4 Scenario driver (`tests/scenarios/test_p3_drive.py`)
- `test_an_unregistered_task_xfails_instead_of_erroring` (run inside `pytester`: outcome `xfailed`, no error)
- `test_an_unregistered_periodic_xfails_instead_of_erroring`
- `test_the_p3_environment_calls_the_three_wire_functions`
- `test_the_package_fixtures_yield_a_scenario` (one per package)

### 5.5 `tests/unit/test_p3_verify.py`
- `test_duplicate_subcommand_names_raise`
- `test_no_registered_subcommand_exits_2_with_usage`
- `test_a_registered_subcommand_runs_in_one_read_only_repeatable_read_snapshot` (fake connection records the two settings)
- `test_the_subcommand_exit_code_is_returned_unchanged` (0, 1, 2)
- `test_unexpected_failures_print_only_the_exception_class`
- `test_src_never_imports_tools` (AST scan of `src/assistant/**`; kept even if import-linter already covers it)

### 5.6 `tests/unit/test_server_sh.py` (additions)
- `test_report_runs_only_whitelisted_sql_names` (source: the name pattern and the file check precede the psql call)
- `test_report_refuses_a_name_with_a_slash_dot_or_capital` (subprocess with `compose` stubbed on `PATH`, like the restore tests: exit 2, no `compose` call)
- `test_report_passes_days_only_as_a_psql_variable_in_a_read_only_transaction`
- `test_p3_verify_uses_a_one_off_read_only_tool_mount`
- `test_trial_check_uses_the_same_read_only_mount_and_passes_arguments_unchanged` (existing; only its last assertion now names `p3_verify.py trial-check`, §3.7 step 3)
- `test_server_usage_lists_report_and_p3_verify`

### 5.7 CI
- `tests/unit/test_ci_workflow.py`: `test_ci_runs_on_pushes_to_main_and_int_p3`, `test_docs_only_changes_still_skip_ci` (YAML parsed; the `paths-ignore` list unchanged).

### 5.8 Log hygiene
- `tests/unit/test_log_hygiene_f0e.py`: `test_p3_verify_output_is_content_free` (a subcommand that raises an error carrying the canary prints only the class name), `test_the_eval_registry_logs_nothing`.

## 6. Acceptance criteria

1. CI green on the full suite; no RG test changed.
2. `python -m evals run --help` lists the same targets as at CP5; each CP5 target runs exactly as before (`--estimate-only` output unchanged on the deterministic modes).
3. Adding EV-AUT's registry line (tried locally, not committed) makes `autonomy` appear in the CLI `run` choices and its route in the effort map, with no other edit to `evals/`.
4. A test file of a later unit can drive `tests/scenarios/{m06,m07,p3_cross}` with no edit to `p3_drive.py`; an unregistered task xfails, never errors.
5. `deploy/server.sh report nfr04 30` runs the same query as `nfr04 30`; `report ../x` is refused before any compose call.
6. `python tools/acceptance/p3_verify.py` with no subcommands exits 2 with usage; a registered fake subcommand runs in a read-only `REPEATABLE READ` snapshot.
7. A push to `int/p3` starts the CI workflow.
8. ARCHITECTURE §16 and §17 describe the seams as built.

## 7. Docs updated in the same PR

- ARCHITECTURE §16 (testing): the scenario packages and their driver rule (xfail, never error), the `test_log_hygiene_<unit>.py` convention, the derived exact-set lists and the "append your own line" rule for `evals/registry.py` and `agent/prompt_registry.py`.
- ARCHITECTURE §17 (operations): `server.sh report <name> [days]` (whitelist = `deploy/sql/*.sql`; read-only), `p3-verify`, and `trial-check` → the P3 verifier.
- `evals/README.md`: one paragraph on `evals/registry.py` ("a new target adds one line").
- PR description: the CP5 runner names used in `TARGETS`, and every list that changed from literal to derived.

## 8. Owner decisions

- **OD-2** (one `int/p3`): the CI `push` trigger names `int/p3`. Two integration branches would list both.
- **OD-9** (prompt key `draft_delegated` under `draft_reply`): the placeholder line in §3.3. A `delegated_reply` run type would add a key and a route entry instead.
- **OD-22 / OD-23** (eval budget, per-run go-ahead): unchanged by this unit; the registry adds no live run and no spend.

## 9. Risks and notes

1. **Registry fields beyond the contract.** P3-interfaces §14.1 gives `EvalTargetEntry` five fields, but `evals/cli.py` is frozen after F0-E while a review sheet or comparison path may be needed. This spec adds optional `review` and `comparison` paths; the contract adopts them (R32). EV-AUT and EV-GATE set neither (the gate runs through `python -m evals.gate`).
2. **`trial-check` target.** WORKFLOW §10 runs `just prod <ip> trial-check` daily, which on `main` calls `p2_verify.py` and fails every `delegation_auto` by design. Ruled (R56): `trial-check` moves to the P3 verifier at R1; the M04 trial is over by then.
3. **Driver ownership.** Ruled (R57): SQ owns `tests/scenarios/p3_drive.py` after CP1; the contract's §15.1 adopts it.
4. **Derived lists hide meaning.** A derived list cannot catch "a tool that should not be in production". The managed-set and flag-consistency tests keep explicit safety content (the contract's managed tools; known flag names), so a surprise still fails CI.
5. **Prompt-history move.** Moving the version comments must be word for word; reviewers diff the moved text. No prompt file changes, so no eval re-run is needed (AGENTS §8).
6. **CI cost.** Every `int/p3` merge now runs the full workflow; the TL batches merges about twice a day (WORKFLOW §7), so this adds about two runs a day.
