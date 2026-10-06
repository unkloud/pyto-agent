# pyto-harness v1.0.9 candidate

Prepared 6 October 2026; not released. This work is carried forward in
the local v1.0.11 candidate.

## Managed in-process execution

Pyto runs generated Python inside the harness process. Runs now hold a single execution
lease while they use shared stdout, stderr, argv, cwd and environment state. A second
conflicting run cannot overlap. If a run outlives its wait limit in native code or while a
program-created thread remains active, the harness reports that it may still be changing
files, refuses conflicting operations until it exits, and tells the user when restarting
Pyto is the recovery.

The batch dispatcher now orders workspace/process conflicts, including a write followed by
a run in the same model response. Independent reads can still overlap. Cooperative timeouts
are described accurately; in-process Python still is not sandboxed, and native calls and
threads cannot be forcibly terminated.

## In-app approvals

Visible Pyto chats now show each protected action and its relevant arguments with explicit
Allow and Deny buttons. Requests are serialized through a FIFO queue, and answer callbacks
are tied to request tokens so a repeated or stale tap cannot authorize a later action.
Stopping the turn or closing the window denies outstanding requests. Terminal approval and
headless denial continue through the existing policy.

## Interruption recovery

Each handler now has a durable intent and start record, and each completed tool result is
saved with its provider-facing message before the rest of a batch finishes. When a session
resumes, calls that never started are recorded as not run; calls that started without a
saved result are recorded as unknown. Both are returned to the provider as valid tool
messages, and neither is replayed automatically. Older session logs and torn-tail recovery
remain supported.

## Interactive PytoUI previews

`preview_program` is a separate lifecycle for saved interactive apps. It validates syntax and
static top-level import availability before execution, then presents through Pyto's blocking
`pyto_ui.show_view` API without the batch-run timeout. The result separates validation,
presentation completion, successful guarded interactions, callback errors, close state and
surviving program threads. A survivor keeps the managed process lease until it exits. The injected
`harness_preview` helper supports guarded callbacks, visible error hooks, presentation and close.

The reusable scaffold combines a persistent counter and form, bounded network work on a background
thread, error display and cooperative cleanup. Its pure logic is separated for short `run_program`
checks. Tests use a fake PytoUI module and mocked response; they do not establish native presentation.

## Saved-program library

Reusable programs can now be registered with a title, purpose, workspace-relative entry file,
batch/app mode, required capabilities and latest verification result. Versioned metadata lives in
`pyto-programs.json`; registration validates workspace paths, writes atomically, rejects duplicate
titles and preserves malformed metadata for inspection. It does not move or delete user source.

Use the Programs button or `/programs` in chat, `/run ID` to run through the existing managed
runner without a model request, and `/edit ID <requested change>` to revise the selected source
with its saved context. The terminal accepts the same commands. `python run.py --programs` and
`python run.py --run-saved ID` also work without provider credentials. Batch verification is
recorded; app-mode programs use the interactive preview lifecycle. Harness-mediated source edits
clear the previous verification status.

## Validated saved-program inputs

Saved programs can declare text, number, choice, file and folder inputs. Chat runs display a form;
terminal runs prompt for values or accept repeated `--input NAME=VALUE` assignments. File and
folder inputs use Pyto's pickers where available. The registry keeps only the schema, not submitted
values; only a stable, non-sensitive choice default may be stored. Each run passes validated data
to `main(inputs)` without a generated shell command.

The new folder-organizer example previews a bounded list of grouped file moves. Nothing changes
until the user taps **Apply these moves**. Applying rechecks source and destination paths, refuses
stale or conflicting plans and rolls back completed moves if a later move fails. Desktop contract,
form, picker-mock, CLI and filesystem tests cover the behavior; actual Pyto picker and file-provider
behavior still needs an iPhone or iPad.

## Durable project context

Each saved program now carries a bounded, versioned brief for user-stated requirements, decisions,
related workspace files and unresolved work. `/edit ID` loads the selected brief plus the current
entry path, verification result and live file status. Separate programs keep separate briefs, and
legacy program indexes migrate without losing existing records. The agent is instructed to treat
briefs and source as project data, not higher-priority instructions.

Before every provider request, the harness applies an independent 128 KiB serialized request
budget covering messages and tool schemas. It drops complete old turns first, then shortens older
assistant/tool result text without separating declared calls from their results or cutting the
current user prompt. The full local session log stays intact. If the current prompt and required
schemas cannot fit, the request is not sent and the user gets a context-limit error. The byte budget
is a conservative size proxy, not a model-specific tokenizer.

## Responsive Pyto chat

The chat display is capped at 80,000 characters and ordinary TextView assignments are coalesced
at 60 ms or 2,048 pending characters. Final output, program listings and approval prompts flush
immediately. A History/Older/Chat control pages the retained session conversation and tool details;
long entries are shortened only in the view, and the durable JSONL session is unchanged. PytoUI
`flex` autoresizing flags let the transcript and bottom controls respond when the root view changes
size. The [PytoUI guide](https://pyto.readthedocs.io/en/latest/library/pyto_ui.html) documents
view resizing behavior; keyboard, rotation and accessibility still need device verification.

## Verification

- Full desktop suite: **790 tests passed** in 85.282 seconds. The standard unittest discovery
  suite used a periodic event-loop wake wrapper for the environment's asyncio shutdown issue and
  approved localhost access for the mock provider.
- After Goal 06: **811 tests passed** in 86.083 seconds with `python3 -m unittest discover -v`;
  `python3 -m py_compile harness/programs.py harness/tools_ios.py harness/loop.py harness/ui.py
  run.py` passed, and `python3 stdlib_audit.py --json` returned `ok: true`.
- Focused GUI/printer/policy coverage: **35 tests passed**.
- Focused Goal 08 loop/session recovery coverage: **73 tests passed**.
- Goal 05 coverage includes static validation without source execution, counter and persistent-form
  behavior, mocked network data, callback errors, clean close, a surviving worker and a preview
  held open for 30.1 seconds without a batch timeout.
- `python3 stdlib_audit.py --json`: `ok: true`; `python3 -m py_compile` passed for changed runtime,
  example and test modules.
- Focused coverage includes simultaneous run requests, output separation, write-then-run
  ordering, overlapping reads, global-state restoration after exceptions, child threads,
  cooperative timeout, recovery after an overdue worker exits, in-chat allow/deny, queued
  prompts, repeated taps, stop and close cancellation, durable per-tool batch results,
  incomplete-call reconciliation and a strict mock provider that rejects invalid histories.
- Pyto/iOS device verification remains pending. See the [Goal 03 device checklist](harness/projects/easy-device-programming/goals/03-safe-program-execution.md#pyto-device-checklist--pending),
  [Goal 04 device checklist](harness/projects/easy-device-programming/goals/04-in-app-approvals.md#pyto-device-checklist--pending),
  [Goal 08 device checklist](harness/projects/easy-device-programming/goals/08-interruption-recovery.md#pyto-device-checklist--pending),
  [Goal 05 device checklist](harness/projects/easy-device-programming/goals/05-interactive-preview.md#pyto-device-checklist--pending),
  [Goal 06 device checklist](harness/projects/easy-device-programming/goals/06-saved-program-library.md#pyto-device-checklist--pending),
  and [Goal 07 device checklist](harness/projects/easy-device-programming/goals/07-simple-program-inputs.md#pyto-device-checklist--pending).

- After Goal 07: `python3 -m unittest discover -s tests -t .` passed **832 tests** in 87.852 seconds.
- After Goal 09: `python3 -m unittest discover -s tests -t .` passed **845 tests** in 88.906 seconds.
  The focused project-brief, request-budget and provider-loop set passed **46 tests**. Changed
  runtime and test modules passed `python3 -m py_compile`; `python3 stdlib_audit.py --json`
  returned `ok: true` with no suspicious imports or syntax problems.
- Goal 09's Pyto restart, long-history and interrupted-call device checklist remains open because
  this environment is Linux-only and has no connected iOS device or Pyto runtime.
- Goal 10's focused UI suite passed **37 tests**. Its Pyto keyboard, rotation, small-screen and
  scrolling checklist remains open for the same device limitation.
