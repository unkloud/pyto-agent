# Goal 04: Let users answer approvals where they are working

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** A visible chat should never require switching to an invisible console or enabling blanket approval.

**Prerequisites:** 02 working-chat-gui. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** run.py: main, make_options_factory; harness/ui.py: TerminalApprover, UIApprover; harness/web.py: WebController, run_web; harness/loop.py: make_policy.

**Implement:**

Add GUI approval requests with understandable action descriptions, relevant arguments, Allow and Deny. Connect answers to the existing policy without blocking the UI thread. Resolve requests once, handle queued requests, and deny outstanding requests when the window closes or the turn is cancelled. Preserve terminal approvals and unattended denial. Verify Pyto console interaction independently of desktop isatty assumptions; do not make --yolo the remedy for an inaccessible approval UI.

**Acceptance criteria:**

Approve and deny mocked share-sheet and URL operations entirely through the GUI flow. Test cancellation, close, repeated taps and multiple requests. Prove denied actions do not execute and approved actions execute once. Test terminal and unattended behavior for regressions; complete native interaction checks when a device is available.

## Implementation record — local v1.0.9 candidate (unpublished)

`run.py --web` now supplies a UI-specific prompter to the same policy used by terminal runs.
The chat displays the approval description and arguments alongside explicit Allow and Deny
buttons. A FIFO ticket queue serializes simultaneous requests; each answer is accepted only
for its request token. Stop and Close deny every unresolved request, and terminal approval
and unattended denial retain their existing paths.

Desktop contract tests drive `share_text` and `open_url` through `make_policy` and the actual
chat callback flow. The allowed share action is recorded once; the denied URL is not run.
At the Goal 04 checkpoint, the focused GUI/printer/policy suite passed 35 tests and the
full desktop suite passed 773. The later combined candidate, including Goal 08, passed 779.
Additional checks cover queued requests, repeated/stale callbacks, Stop and Close. The UI
uses Pyto's documented `pyto_ui.show_view` model, which blocks until dismissal and permits
view updates from a worker thread; native interaction itself remains unverified here.

### Pyto device checklist — pending

Record device model, iOS version and Pyto version, then verify:

1. Start the harness in its visible Pyto chat when `sys.stdin.isatty()` is false. Request a
   `share_text` action and confirm the chat shows the action, arguments, reason, Allow and
   Deny without asking for input in the console.
2. Deny the share request and confirm no share sheet opens. Request it again, allow it, and
   confirm the native share sheet opens once.
3. Request `open_url`. Deny it and confirm Safari or the browser does not open; allow it and
   confirm the requested URL opens once.
4. Trigger two protected operations close together. Confirm each approval appears in order
   and a repeated tap on the first prompt cannot approve the second.
5. Tap Stop while a prompt is visible. Confirm the protected operation does not run, the
   turn ends, and a new turn can show an approval. Repeat with Close and confirm the worker
   drains without executing the pending action.
6. Run the terminal REPL with a TTY and confirm its y/n approval still works. Run a
   headless/Shortcut invocation and confirm protected actions are denied without an
   interactive approver.

Do not mark device verification complete until these checks have been run on the installed
Pyto version.
