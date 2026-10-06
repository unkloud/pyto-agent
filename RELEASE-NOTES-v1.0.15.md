# pyto-agent v1.0.15

Released 6 October 2026.

## On-device behavior diagnostic

`device_release_diagnostic.py` now runs seven focused, offline check groups when the
diagnostic and installed harness versions match on iOS/Pyto:

- managed program execution and process-state restoration;
- saved-program persistence and model-free execution;
- typed input validation and argument preservation;
- simulated interruption recovery;
- project-context isolation and persistence;
- read-only Foundation/UIKit Objective-C recipes;
- Shortcuts saved-run argument, opt-in and refusal rules.

The test cases use disposable temporary folders. They make no provider/network requests and
do not open Pyto UI, system pickers, approval prompts or the Shortcuts app. They test the
harness logic in Pyto's Python runtime; they do not count as native UI acceptance. All 13
manual checklist entries remain `NOT RUN` until performed and recorded on the device.

The standalone diagnostic is attached as `device_release_diagnostic_v1.0.15.py`.

## Verification

- Desktop tests cover the report's seven-group scope, skip behavior outside iOS, and preservation
  of every manual `NOT RUN` status.
- The complete offline desktop suite passed **880 tests** in 90.568 seconds. The six fixture-based
  groups also passed with in-process program execution selected. This does not exercise native
  Pyto UI or Objective-C on an iPhone.
- The focused on-device checks have not been run on an iPhone in this release environment.
