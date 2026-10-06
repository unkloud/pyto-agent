# Goal 02: Make chat open, respond and close reliably

> **Superseded for the harness chat interface by Goal 14 in v1.0.20.** The native `--ui` chat window is retired; use `--web` for graphical chat. This does not remove `pyto_ui` support for generated programs or interactive previews. Keep this file as the historical record of the former front end.

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** A person must be able to chat in Pyto without crashes, frozen windows or callbacks writing to closed sessions.

**Prerequisites:** None. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/ui.py: run_ui, _ChatLifecycle, _UIStream; run.py: main; harness/pyto_api.py.

**Implement:**

Check supported Pyto documentation and source and, when available, the installed version. Replace unsupported presentation/dispatch assumptions with supported APIs. Wire callbacks before presentation and keep session/model resources alive for the window lifetime. Distinguish high-level pyto_ui wrappers from direct UIKit main-thread requirements. Add explicit lifecycle state, stop/close behavior and safe handling of callbacks after dismissal. Surface dispatch errors instead of swallowing them. Do not block UIKit’s main thread waiting for presentation or network work.

**Acceptance criteria:**

Add strict API-contract and lifecycle tests that cannot invent missing Pyto members. Verify two consecutive turns and dismissal during a request. On device, open, send, stop, close and reopen with no frozen window, orphan callbacks or closed-resource writes. Report any unavailable device checks explicitly.

## Implementation record — v1.0.7

Implemented 5 October 2026. The chat now presents through Pyto's documented `ui.show_view(view)` API. It registers Send, Stop and Close callbacks before presentation, runs each model turn on a worker thread, and keeps the session and client alive until the window is dismissed and the active worker has drained. Stop signals the turn and cancels provider I/O; Close prevents further transcript writes, cancels the request and joins the worker before `run.py` closes its resources. PytoUI update errors are surfaced instead of discarded.

The API check used the [Pyto UI guide](https://pyto.readthedocs.io/en/latest/library/pyto_ui.html), [Pyto's `pyto_ui` source](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/pyto_ui.py) and [the separate `mainthread` API](https://pyto.readthedocs.io/en/latest/library/mainthread.html). Pyto documents that `show_view` blocks the Python script until the view closes and allows another thread to update PytoUI views. The harness uses only those high-level PytoUI wrappers; it does not call UIKit directly. No installed Pyto runtime or iOS device was available, so this source check does not establish behavior on the user's installed version.

Desktop contract/lifecycle coverage verifies callback setup before presentation, consecutive turns, Stop, Close during a pending turn, the stream write barrier and session lifetime. The full suite passed: 738 tests in 39.706 seconds. The focused UI set passed 18 tests.

### Device acceptance checklist — still unverified

1. Record device model, iOS version and installed Pyto version.
2. Launch `run.py --ui`, send two consecutive prompts and confirm both responses appear without freezing.
3. Start a slow request, tap Stop, wait for “Interrupted,” then send another prompt successfully.
4. Start another request and dismiss with Close and with the sheet's native dismissal. Reopen chat and confirm no stale callback changes the dismissed view and no write reaches a closed session.
5. Repeat with the keyboard open and after rotating the device; check the entry and controls remain usable.

Do not mark native/device verification complete until these steps are run on Pyto.
