# Antony's self-paced P2 QA checks

The owner requested recurring unattended QA checks and bounded QA delegation.
Tracking issue: [#114](https://github.com/Khumoyun1307/personal-ai-assistant/issues/114).
The controller is [p2_qa_scheduler.ps1](../../../../tools/acceptance/p2_qa_scheduler.ps1).
It polls GitHub cheaply, starts a fresh Codex worker only for changed work, waits
for that worker to finish, then waits five minutes before its next check.
It does not interrupt a running batch. This five-minute controller interval is
the new requested QA cadence, rather than the earlier manual role intervals.

Each worker receives [the full QA prompt](p2-self-check-prompt.md) and returns
[structured results](../../../../tools/acceptance/p2_qa_result.schema.json).
It is a new execution, using repository/GitHub context; it does not wake or
share the running state of the existing IDE chat. The controller does not use
`resume --last` or inject a message into an active chat. Official OpenAI
documentation supports scripted `codex exec` jobs, stdin prompts and structured
final output. ([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[CLI reference](https://learn.chatgpt.com/docs/cli/reference))

This fix round belongs on `main` as phase tooling. It replaces the earlier
integration-branch PR through a new branch/PR; do not rewrite its pushed history.
Source changes alone neither register nor reactivate a task. Review the final
commit and native test evidence before an explicit operator installation/start.

**Observed status, 2026-10-28:** recurrence is paused pending this fix round and
peer/TL review. The original worker completed at `12:47:16.3485455Z`, its result
was acknowledged, and its next due time was `12:52:16.3485455Z` (exactly 300
seconds after completion), with phase `paused` and no worker PID. That verifies
the original pacing only; it does not validate these new controls or activate
them. [Recorded evidence](https://github.com/Khumoyun1307/personal-ai-assistant/issues/114#issuecomment-6469395435).

## Prerequisites and workspace

- Windows PowerShell, Windows Task Scheduler, Git, GitHub CLI and Codex CLI must
  be available to the same Windows account running the controller. The owner
  signs in through the normal `gh`/Codex login flows before unattended use;
  never put a token in this guide, the task arguments or controller state.
- GitHub access must resolve to owner account `Khumoyun1307` and the expected
  repository. Only that account's same-repository work is trusted; public
  contributions and fork code never become runnable queue input.
- Use a dedicated, clean control checkout at the reviewed commit for the
  controller, prompt and schema. Keep it immutable between reviewed upgrades;
  never install from the author's active fix-round worktree. Supply a separate
  dedicated QA workspace, preserving its branch, HEAD and files across workers.
  Fixes/reviews use their own Antony-owned worktrees. Do not use another
  engineer's dirty tree or a running bind-mounted test. Test configuration uses
  placeholders from committed `.env.example`, never the owner's real `.env`.
- Docker Desktop must be available for relevant tests/commit hooks. Python
  tooling runs in Compose service `app`, with project `antony-scheduled` and
  only a synthetic test database. Local integration work remains serial; CI
  owns the full test gate. Missing capabilities produce an honest blocker.
- The laptop must stay awake and connected while checks are expected. The
  installed task runs on logon; it does not provide a cloud runner when the
  laptop is asleep or the account is logged out.

The examples below run from the clean, reviewed control checkout containing the
script. Replace the workspace example with the separate scheduled QA worktree.

```powershell
$qaWorkspace = 'C:\Programming\Telegram AI Assistant Agent\.claude\worktrees\antony-scheduled'
$qaController = '.\tools\acceptance\p2_qa_scheduler.ps1'
$qaReviewedCommit = '<reviewed full 40-character commit SHA>'
```

## Install the reviewed controls, then run

Replace the commit placeholder with the actual reviewed full SHA. `Install`
requires the controller checkout to be clean, detached at that commit and
separate from `-Workspace`. Only `Install` accepts `-ReviewedCommit` and records
the control pins. Registration alone does not start a controller:

```powershell
& $qaController Install -Workspace $qaWorkspace -ReviewedCommit $qaReviewedCommit -IntervalSeconds 300
```

Installation records `integrity.json` with the control checkout's paths and
SHA-256 hashes of the controller, prompt and result schema. The controller
reads its prompt/schema from that control checkout, never from the writable
worker workspace. Every worker launch verifies the pins; a missing manifest,
changed path or changed control file pauses launches as `integrity_failed`,
returns failure and leaves the queue unacknowledged. `Run` and `Once` do not
create or refresh the manifest. Do not repair integrity by editing state or
letting the worker alter its controls.

One immediate check, then exit (an unchanged snapshot does not invoke Codex):

```powershell
& $qaController Once -Workspace $qaWorkspace
```

Run the self-paced controller in the foreground; this command remains active:

```powershell
& $qaController Run -Workspace $qaWorkspace -IntervalSeconds 300
```

Inspect structural local status without a model invocation or GitHub write:

```powershell
& $qaController Status
```

The default state directory is `.claude/antony-qa-scheduler` under the common
repository root so worktrees share the same single-controller lease. Use the
same explicit `-StateDirectory` on every command if overriding it. State holds
structural queue/completion metadata, not secrets or real message content.
The worker preserves its base workspace's branch, HEAD and files. Scheduler
source, prompt, schema, guide, tests and state are outside its authorized edit
scope; maintenance requires a separate manual assignment. That instruction is
not an operating-system access-control guarantee; see the security boundary
below.

Alternatively, register and explicitly start the one logon task now, after the
same reviewed checkout and prerequisites are ready:

```powershell
& $qaController Install -Workspace $qaWorkspace -ReviewedCommit $qaReviewedCommit -IntervalSeconds 300 -Start
& $qaController Status
```

The task is named `P2-Antony-QA`. Task Scheduler uses `IgnoreNew`, and the
controller holds an exclusive lease across the worker process. A second `Run`
or `Once` cannot start overlapping work. The task has no execution time limit
that kills a worker. There is one controller and one pacing loop, with no
second repeating trigger or worker-created schedule. Check `Status` to verify
the actual state; creating these source files does not activate a schedule.

## Manual QA handoff and graceful controls

Manual Antony work and the scheduled controller do not automatically know one
another's busy state. The lease covers controller-launched workers; it does not
detect an active IDE turn or other manual Codex process. Before manual work,
pause the controller and inspect status until any active worker has finished:

```powershell
& $qaController Pause
& $qaController Status
```

`Pause` persists across controller restarts and prevents another worker. It
does not terminate the current worker. Begin manual QA only after status shows
it is inactive; leave the pause in place throughout that manual batch. After
finishing manual work:

```powershell
& $qaController Resume
& $qaController Status
```

`Resume` clears pause/stop requests; it does not launch a stopped controller.
It also resets the consecutive worker-failure count at the next safe boundary;
use it only after resolving the failure. It does not accept changed control
pins or clear an unfinished launch intent.
If one is no longer running, start it explicitly after resuming:

```powershell
& $qaController Run -Workspace $qaWorkspace
```

Request a graceful controller exit after its current worker completes:

```powershell
& $qaController Stop
& $qaController Status
```

For a control upgrade, request `Stop`, wait for the worker and controller to
exit, and prepare a new clean detached checkout at the newly reviewed commit.
From that checkout run `Install -ReviewedCommit` with the same workspace and
state directory, then `Resume` and explicitly start `Run` or the registered
task. Reinstallation neither resumes a paused/stopped controller nor resets
failures by itself. Never update the live author's working tree in place and
assume its next logon execution is the reviewed version.

The stop request persists, so later `Run`/`Once` honors it until `Resume`.
Use this control instead of killing Codex or using Task Scheduler's End action.
To remove future logon starts, request the graceful stop and unregister:

```powershell
& $qaController Stop
& $qaController Uninstall
& $qaController Status
```

## What changes wake a worker

The metadata check tracks ready QA heads and completed CI conclusions, relevant
Antony issue/peer feedback, and current #43 coordination. It checks actual
head/feedback identity rather than using a PR's `updatedAt` as a wake signal.
Queued/in-progress CI transitions alone do not warrant a new worker. A new
owner-authored head or relevant owner feedback may still wake one while its CI
is pending; the worker must not call that head CI-green or QA-complete early.
Exact comment/review IDs returned for completed worker posts are excluded from
its next snapshot so those posts do not create a recurring model loop. A broad
"Antony" prefix filter would also hide useful manual coordination on the shared
GitHub account and is not an appropriate substitute. No changed work means no model
call and no issue comment. The first useful check establishes current context;
it does not assume historical QA labels or reviews are current.

An `idle`, `completed` or handled `blocked` result can settle the current
snapshot. A `failed` worker result, nonzero exit, missing/stale/malformed result
or worker exception does not acknowledge it. Failed attempts use
`-RetrySeconds` (default 300) after completion; three consecutive worker failures
automatically pause future launches (`paused_after_failures`). Inspect and fix
the cause before manually resuming, rather than repeatedly paying for the same
failed batch. A metadata fetch failure must not launch a worker or acknowledge
the snapshot. Do not copy credentials or disable the sandbox to bypass a
capability/approval failure.

Every result must carry the current 32-character `check_id`, exactly matching
the launch intent. A well-formed result from a previous invocation cannot
acknowledge the current snapshot.

## Controller crashes and orphan workers

The controller records launch intent before starting Codex, then records the
worker PID and process creation time. On restart, an exact matching live worker
produces `worker_still_running`; the controller starts no duplicate. Let that
process finish before recovery. PID alone is not proof: Windows can reuse it
after a process exits, so creation time must also match.
If the controller died between launch and recording the PID, the launch state
is ambiguous (`recovery_required`). It blocks new workers until the operator explicitly resolves it;
silently deleting that intent would risk starting a duplicate.

Use `Pause` and `Status` first. Establish whether the prior worker is still
running and let it finish; do not kill it to make the next check run. After
verifying that it has ended, explicitly recover the recorded launch:

```powershell
& $qaController Recover -ConfirmWorkerStopped
& $qaController Resume
& $qaController Run -Workspace $qaWorkspace
```

`Recover` refuses while a worker matching the recorded PID and creation time
is alive. The confirmation is the operator's assertion that no orphan remains
when the launch state is ambiguous. `Resume` alone never clears launch intent.
A stale controller lease alone does not prove that its worker exited. Manual
IDE activity still requires the Pause/Status handoff above; crash recovery does
not discover or control that chat's active turn.

`Status` reports `worker_elapsed_seconds` and flags `worker_overdue` after two
hours. Inspect that condition manually; it does not interrupt or kill the
worker. `already_running`, `worker_still_running` and `recovery_required` from a
requested `Run`/`Once` are nonzero non-runs, so a logon collision cannot look like
a successful active controller. Confirm `Status` after starting.

The worker completes one batch and checks the queue again, with at most one
additional follow-up batch. It may fix and publish authorized QA-owned work,
but feature defects go to their owners. It never self-approves Antony PRs,
merges, pushes `main`/`int/*`, edits product code/prompts/migrations, runs a full
local suite, reads secrets/real data, calls Telegram/providers/evals, or runs
production/server/Alviro operations. Live windows, eval spend, trial days and
owner gates stay pending until separately performed and evidenced.

## Current governing rules and security boundary

Each worker verifies the trusted origin, fetches `origin/main` afresh and reads
the governing AGENTS, milestone approvals, WORKFLOW, decisions, Antony handoff,
test plan and related phase documents with `git show` at that fetched commit.
The stable control/worker checkout is not the source of current governance.
Failure to fetch/read required rules returns `failed` before QA tests, edits or
public writes; a cached ref is not a fallback. The reviewed unit's exact head
still supplies its implementation and test evidence.

All WORKFLOW §2 restrictions apply, including only owner-account coordination
and same-repository code, no notifying bare at-sign names, and no weakening or
deletion of invariant/regression tests. Every delegated brief repeats those
limits. Routine QA authorization cannot expand ownership or override gates.

The reviewed worker launch explicitly selects `-s workspace-write`,
`--ignore-user-config`, `--ignore-rules`, `--strict-config` and `--ephemeral`,
with `approval_policy="on-request"` and `approvals_reviewer="auto_review"` set
through `-c`. This preserves automatic approval review without combining
`--approve-for-me` with its conflicting `--sandbox` flag. `--strict-config`
rejects unknown settings; `--ignore-user-config` separately avoids inherited
user configuration, and authentication still uses the existing Codex sign-in.
([CLI reference](https://learn.chatgpt.com/docs/cli/reference),
[Agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security))

The controller discards worker stdout/stderr and does not persist transcripts
or result summaries. Codex ordinarily saves session/rollout files under its
state directory; this launch deliberately uses `--ephemeral` to avoid those
session files. Temporary structured results are validated, reduced to
structural state and removed. This is not a claim that every external tool or
operating-system artifact is ephemeral.
([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode))

Workspace-write restricts filesystem writes; it does not make other local files
unreadable or make Docker a low-privilege boundary. Approved GitHub/git commands
use the owner's account, and Docker can reach the host. Prompt restrictions,
automatic approval review and control integrity checks are safeguards, not a
guarantee against a compromised process or account. Branch protections/rulesets
remain an owner/TL repository decision; this controller does not install them
and must not claim that its no-main/int-push instruction is server-enforced.
The local pin manifest is also writable by the Windows account: it detects
unreviewed control changes during normal operation, but cannot protect against
a malicious process that can replace both the controls and their trusted pins.

## Native regression checks

From the checkout containing the controller, run the synthetic PowerShell
harness before installing or activating the scheduler:

```powershell
powershell -NoProfile -File tools/acceptance/tests/test_p2_qa_scheduler.ps1
```

It uses temporary synthetic state and fake callbacks/processes to exercise the
controller. It makes no Codex/model, GitHub/network or Docker call and registers
no Windows task. The output identifies each passing case and the final total;
record the actual command/result on #114 after running it. Those checks do not
establish that a real worker or installed task has run successfully; a live
scheduler activation remains a separate, explicitly recorded step.
