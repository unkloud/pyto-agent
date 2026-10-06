# Goal 13: Verify the complete novice workflow

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** A release should prove that people can create and reuse useful device tools with little manual work.

**Prerequisites:** 01–12; Shortcut checks apply only where supported. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** examples/; tests/; README.md; implementations delivered by goals 01–12.

**Implement:**

Create and run a reproducible acceptance pack for a clipboard notebook, selected-folder organizer with preview/undo, persistent counter, network-backed form and a tool requiring a device permission. Cover ask/build/try/save/reopen/run/edit, interruption recovery and denial. Add missing test fixtures or example behavior needed for these journeys. Record successful completion, manual code/path edits, repeated explanations and time to rerun. Fix failures within these workflows and document remaining platform limitations. Do not treat mock-native tests as proof of device behavior.

**Acceptance criteria:**

All automated journeys pass and device results are recorded with Pyto/iOS versions when available. Each saved tool reruns without model access where its operation permits it. The folder organizer can undo its demonstrated changes. Publish a concise result matrix and explicit unverified checks; do not claim device acceptance without executing it.

## Implementation record — local v1.0.11 candidate (desktop gate complete; device gate pending)

Added `harness/projects/easy-device-programming/acceptance/README.md` as the reproducible
acceptance pack and result matrix. The clipboard notebook now supports the registered
`main(inputs)` contract as well as its existing CLI, reads the Pyto clipboard when the
optional text input is omitted, and stores notes beside the saved entry. A fresh-process
test registers it, runs it twice without any API-key environment variables, checks that the
note appends and verification survives a new workspace load, and confirms the edit prompt
selects the saved file. It can print each fresh-process wall time with
`PYTO_HARNESS_ACCEPTANCE_TIMINGS=1`.

The folder organizer now exposes an explicit Undo action after Apply. Undo checks every
original path and destination before changing anything, refuses occupied paths, rolls back
partial work where possible, and removes only empty directories created by the original
apply. Desktop tests cover preview-only behavior, Apply then Undo, conflicts, permission
denial without a false-success result, the persistent counter/form with a mocked network
response, and recovery without replaying a side effect of unknown outcome. The detailed
command, matrix, measures and device checklist are in the [acceptance pack](../acceptance/README.md).

Desktop measures are limited to deterministic fixtures. The saved CLI reruns use zero model
requests and the timing switch captures local process wall time. No person performed a novice
session here, so manual source/path edits, repeated explanations during creation, perceived
device rerun time and all native results remain unmeasured. The pack keeps those fields explicit
instead of treating test fixture inputs or mocked APIs as user/device evidence.

### Pyto device acceptance — pending

Follow every item in the [acceptance pack device checklist](../acceptance/README.md),
record the device model, iOS version and Pyto version, then fill the result measures for each
journey. The environment used for this implementation has no iPhone/iPad, Pyto runtime or
simulator, so Goal 13 is not device-verified and is not yet a complete release gate.
