# Goal 05: Build and try real interactive apps

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** Users should be able to try an app, leave it open, report a problem and rerun the revision.

**Prerequisites:** 02 working-chat-gui; 03 safe-program-execution; 04 in-app-approvals. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/tools_ios.py; harness/loop.py: SYSTEM_PROMPT, tool dispatch; harness/pyto_api.py; examples/.

**Implement:**

Create a supported interactive preview lifecycle separate from bounded batch execution. Validate syntax and imports safely, test pure logic, launch the preview, capture callback errors, expose close/stop state and support rerun after editing. Do not treat an open view beyond 30 seconds as a failed test. Distinguish validation passed, preview opened and user interaction verified in tool results and summaries. Add a verified reusable scaffold covering layout, background work, persistence, error display and cleanup. Correct timeout descriptions and script-thread versus UIKit-main-thread guidance. Update the agent prompt only to describe implemented capabilities.

**Acceptance criteria:**

Exercise a counter, a persistent form and a network-backed view using mocked network data. Leave previews open beyond the batch timeout, trigger actions and errors, then close and rerun. Prove closing cleans up owned work or reports surviving work honestly. Include device checks for actual presentation and callbacks.

## Implementation record — local v1.0.9 candidate (unpublished)

Added `preview_program`, a separate managed in-process path for saved PytoUI apps. It statically
checks syntax and top-level import availability without executing the app first, then presents
the root view through the documented blocking `pyto_ui.show_view` lifecycle. The preview has no
batch timeout. Its result reports validation, completed presentation, instrumented callback
success, callback/background errors, close state and surviving workers separately. A normal close
signals the injected `harness_preview.stop_event`; if program-owned work survives the close grace,
the tool reports it and the shared-process lane stays owned until it exits. This is concurrency
control, not a sandbox or a guarantee that native work can be stopped.

The injected `harness_preview` API provides `guard`, `report_error`, `present` and `close`. The
reusable [`interactive_app_scaffold.py`](../../../examples/interactive_app_scaffold.py) combines a
persistent counter and form with bounded network work, visible errors, a background status label
and cooperative cleanup. Its pure functions live in
[`interactive_logic.py`](../../../examples/interactive_logic.py). Desktop contract tests use a
fake PytoUI module and mocked HTTP response to exercise counter updates, form persistence, network
rendering, callback errors, close cleanup, a surviving worker and a view kept open for more than
30 seconds. The focused Goal 05 suite passed 10 tests, and the complete desktop suite passed
790 tests in 85.282 seconds. These mocks do not prove that Pyto actually displayed a native view.

Static import preflight verifies only top-level availability; it cannot prove imported members or
dynamic imports exist. `pyto_api` remains the source of per-build Pyto signatures. For an instrumented
interaction to count as verified, the program must wrap the user callback with `harness_preview.guard`.

### Pyto device checklist — pending

No iPhone/iPad, Pyto app session or iOS simulator is available on this host. The connected app
surface was empty, and `xcrun`, `idevice_id` and `ios-deploy` are unavailable. Record device model,
iOS version and installed Pyto version when a device is connected, then verify:

1. Run the harness in visible Pyto chat with a saved copy of the scaffold and confirm it passes
   syntax/import validation before opening a view.
2. Confirm `preview_program` presents the view from the tool worker over the chat and the Close
   button returns to chat without freezing it.
3. Leave the view open for more than 30 seconds. Tap Add one, save a Unicode note, tap Refresh,
   and confirm the network result or its visible error. Confirm the preview is not called timed out.
4. Close and reopen the preview; verify the counter and form state persisted. Change the source,
   rerun it, and confirm the new revision opens.
5. Trigger a guarded callback exception. Confirm the error label changes and the final tool result
   reports the callback error while distinguishing presentation from interaction verification.
6. Confirm the background label updates while open, then stops after Close. With a deliberately
   slow owned worker, confirm the result reports cleanup pending and conflicting runs/workspace
   access stay blocked until it exits.
7. After a normal close, run another short program and confirm stdout, stderr, argv, cwd, env and
   the Pyto chat remain usable.

Do not mark native presentation or callback behavior verified until these checks are run on the
installed Pyto build.
