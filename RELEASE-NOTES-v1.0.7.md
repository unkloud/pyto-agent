# pyto-harness v1.0.7

Released 5 October 2026.

## Pyto chat lifecycle

The Pyto chat now uses the documented `pyto_ui.show_view(view)` presentation API and wires its actions before presenting the window. Model turns run on a worker thread. Stop cancels the active request; Close stops transcript writes, cancels and drains the active worker before the session and client are closed. PytoUI update failures are no longer silently swallowed.

The API check used Pyto's [UI guide](https://pyto.readthedocs.io/en/latest/library/pyto_ui.html), [upstream `pyto_ui` implementation](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/pyto_ui.py) and [separate `mainthread` API](https://pyto.readthedocs.io/en/latest/library/mainthread.html). The code only uses PytoUI's high-level wrappers; it does not call UIKit directly.

## Verification

- `python3 -m unittest tests.test_ui_lifecycle`: 4 tests passed.
- `python3 -m unittest discover -s tests -t .`: 721 tests passed in 40.432 seconds. The tests use localhost mock-provider servers; no production API was contacted.
- `python3 run.py --version`: reported `pyto-harness 1.0.7`.

Pyto/iOS device checks were not available. Follow the device acceptance checklist in [goal 02](harness/projects/easy-device-programming/goals/02-working-chat-gui.md) before claiming native verification.
