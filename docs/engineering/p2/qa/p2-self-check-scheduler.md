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
The worker is a new execution, using repository/GitHub context; it does not wake
or share the running state of the existing IDE chat. Official OpenAI
documentation supports `codex exec` for scripted jobs and explicit structured
output; the installed CLI supports `--approve-for-me`, `--cd`,
`--output-schema`, `-o`, and stdin prompts. The controller does not use
`resume --last` or inject a message into an active chat.
([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[CLI reference](https://learn.chatgpt.com/docs/cli/reference))

## Prerequisites and workspace

- Windows PowerShell, Windows Task Scheduler, Git, GitHub CLI and Codex CLI must
  be available to the same Windows account running the controller. The owner
  signs in through the normal `gh`/Codex login flows before unattended use;
  never put a token in this guide, the task arguments or controller state.
- GitHub access must resolve to owner account `Khumoyun1307` and the expected
  repository. Only that account's same-repository work is trusted; public
  contributions and fork code never become runnable queue input.
- Supply a dedicated QA worktree with the controller, prompt and schema present.
  Do not use a directory with another engineer's uncommitted work or a running
  bind-mounted test. Its test configuration uses placeholders from committed
  `.env.example`, never the owner's real `.env`.
- Docker Desktop must be available for relevant tests/commit hooks. Python
  tooling runs in Compose service `app`, with project `antony-scheduled` and
  only a synthetic test database. Local integration work remains serial; CI
  owns the full test gate. Missing capabilities produce an honest blocker.
- The laptop must stay awake and connected while checks are expected. The
  installed task runs on logon; it does not provide a cloud runner when the
  laptop is asleep or the account is logged out.

The examples below run from the checkout containing the script. Replace the
workspace example with the dedicated worktree chosen for scheduled QA.

```powershell
$qaWorkspace = 'C:\Programming\Telegram AI Assistant Agent\.claude\worktrees\antony-scheduled'
$qaController = '.\tools\acceptance\p2_qa_scheduler.ps1'
```

## Run and inspect

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
The worker preserves this base workspace's branch, HEAD and files. Unit fixes
and review checkouts use separate Antony-owned worktrees. Scheduler source,
prompt, schema, guide, tests and state are protected from worker modification;
maintenance of those controls requires a separate manual assignment.

## Install the one logon task

Registration alone does not start a controller:

```powershell
& $qaController Install -Workspace $qaWorkspace -IntervalSeconds 300
```

Register and explicitly start it now:

```powershell
& $qaController Install -Workspace $qaWorkspace -IntervalSeconds 300 -Start
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
another's busy state. Before manual work, pause the controller and inspect
status until any active worker has finished:

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
If one is no longer running, start it explicitly after resuming:

```powershell
& $qaController Run -Workspace $qaWorkspace
```

Request a graceful controller exit after its current worker completes:

```powershell
& $qaController Stop
& $qaController Status
```

The stop request persists, so later `Run`/`Once` honors it until `Resume`.
Use this control instead of killing Codex or using Task Scheduler's End action.
To remove future logon starts, request the graceful stop and unregister:

```powershell
& $qaController Stop
& $qaController Uninstall
& $qaController Status
```

## What changes wake a worker

The metadata check tracks ready QA heads and their base/CI state, relevant
Antony issue/peer feedback, and current #43 coordination. It checks actual
head/feedback identity rather than using a PR's `updatedAt` as a wake signal.
Exact comment/review IDs returned for completed worker posts are excluded from
its next snapshot so those posts do not create a recurring model loop. A broad
"Antony" prefix filter would also hide useful manual coordination on the shared
GitHub account and is not an appropriate substitute. No changed work means no model
call and no issue comment. The first useful check establishes current context;
it does not assume historical QA labels or reviews are current.

An `idle`, `completed` or handled `blocked` result can settle the current
snapshot. A `failed` result, invalid output or unavailable GitHub/Codex
capability is not silently acknowledged as complete; retry pacing uses
`-RetrySeconds` (default 300). Inspect status and the relevant issue for the
remaining dependency. Do not work around authentication/approval failures by
copying credentials or disabling the sandbox.

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

The worker completes one batch and checks the queue again, with at most one
additional follow-up batch. It may fix and publish authorized QA-owned work,
but feature defects go to their owners. It never self-approves Antony PRs,
merges, pushes `main`/`int/*`, edits product code/prompts/migrations, runs a full
local suite, reads secrets/real data, calls Telegram/providers/evals, or runs
production/server/Alviro operations. Live windows, eval spend, trial days and
owner gates stay pending until separately performed and evidenced.

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
