# Goal 08: Resume safely after the app is interrupted

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** Returning after an app kill should preserve progress and avoid duplicated actions.

**Prerequisites:** 03 safe-program-execution. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/session.py: project, _prune_orphan_tool_messages; harness/loop.py: run_turn, _dispatch.

**Implement:**

Journal tool intent and completion durably and save each completed tool result without waiting for the entire batch. Reconcile assistant tool declarations with missing results before creating the next provider request. Represent interrupted and unknown outcomes explicitly while retaining valid tool-message relationships. Never automatically replay a possibly completed side effect. Explain uncertainty in user language and offer evidence-based recovery. Maintain compatibility with old session logs and torn-tail handling.

**Acceptance criteria:**

Fault-inject termination before launch, during execution and after a side effect but before result logging. Resume yields valid provider history and never duplicates the side effect. Cover partially completed multi-tool batches, old logs and malformed tails. Use a mock provider that rejects invalid tool-call sequences.

## Implementation record — local v1.0.9 candidate (unpublished)

The loop now persists a `tool.intent` before scheduling a handler, a `tool.started` immediately
before it runs, and a `tool.completed` containing the provider-facing result in one durable
event as soon as that call finishes. Projection supports both these result events and older
logs that stored `message.tool` separately, and places results directly after their
assistant declaration in model-call order.

Before a resumed turn makes a provider request, it reconciles any declared call without a
result. An intent with no start is recorded as not run; a start with no saved result is
recorded as unknown. Neither state is replayed. The recovered tool message keeps the
provider history valid, while a concise status tells the user what evidence to check. A
partially completed batch keeps each saved result and marks only unfinished calls unknown.
Session header version 1, old event projection and torn-tail handling remain supported.

Desktop fault-injection covers a declaration before launch, a start without a result, a
side effect applied before interruption, and one completed result beside an unfinished
batch member. A strict mock provider rejects orphan or missing tool results. The focused
loop/session recovery suites passed 73 tests; the combined full suite passed 779. The
resumed side-effect test passes with the existing effect count unchanged. Device behavior
remains pending.

### Pyto device checklist — pending

Record device model, iOS version and Pyto version, then verify:

1. Resume a session with an interrupted call before launch. Confirm the chat says the action
   was not run and offers a new request, and that the provider accepts the repaired history.
2. Run a test program that writes one marker file and then waits. Force-quit Pyto after the
   marker appears but before the run returns. Resume the session and confirm it reports an
   unknown outcome, does not write a second marker, and the next provider request succeeds.
3. Run a batch with one fast marker write and one long-running action. Force-quit after the
   fast result is visible but before the slow action completes. Resume and confirm the saved
   fast result is retained, the slow result is marked unknown, and neither call is repeated.
4. Confirm recovered messages stay attached to their matching assistant tool-call IDs in
   a session that also contains earlier completed turns.

Do not mark device verification complete until these checks have been run on the installed
Pyto version.
