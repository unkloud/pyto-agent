# Goal 12: Make saved tools available through Shortcuts

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** Users can invoke suitable saved automations from familiar iOS entry points.

**Prerequisites:** 06 saved-program-library; 07 simple-program-inputs; 08 interruption-recovery; 11 agent-workflow. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** run.py: task_from_environment and launch parsing; harness/ios.py; program library; README.md.

**Implement:**

Inspect supported Pyto URL/Shortcuts interfaces for the target version and implement an optional handoff for compatible saved batch programs with validated inputs. Prefer direct saved-program execution without an unnecessary model request. Explain foreground requirements, unavailable capabilities and unattended policy in the setup flow. Supply clear installation/run instructions or supported shareable configuration. Keep interactive-app launching distinct; do not promise background daemons or silently enable unattended execution.

**Acceptance criteria:**

Validate parsing, encoding, workspace boundaries and policy with automated tests. On device, launch a saved automation with inputs from a Shortcut and confirm the observed result. Verify cancellation and unsupported configurations produce helpful guidance and do not silently run with broader permissions.

## Implementation record — local v1.0.11 candidate (unpublished)

Added `--shortcut-run PROGRAM_ID` as a model-free entry point for saved **batch** programs.
It uses the existing library lookup, workspace-relative entry path and validated `--input
NAME=VALUE` schema, so Unicode, spaces and embedded `=` values arrive without URL or shell
decoding. The Shortcut must explicitly include `--allow-unattended-saved-programs`; without
it the command stops before invoking the saved file and explains the opt-in. The flag is
only valid with `--shortcut-run` and does not change normal chat approval settings.

Interactive app previews are rejected with a Pyto-run instruction. Headless runs cannot open
the native file/folder picker, so `NAME=@pick` is rejected before execution; callers may
provide a readable path that passes the existing validator. Missing or invalid fields also
stop before the source runs. The README now describes the Pyto Shortcuts Run Script action,
separate argument tokens, output retrieval, the source-review requirement, and iOS/Pyto's
background limits. The instructions link to Pyto's
[documented Shortcuts interface](https://pyto.readthedocs.io/en/latest/automation.html).

Focused desktop verification: `python3 -m unittest tests.test_shortcut_saved_run
tests.test_program_inputs -v` passed 15 tests after the Goal 12 changes. Tests cover argument
parsing, exact Unicode/special-character preservation, opt-in enforcement, model-free
execution without an API key, invalid/missing inputs, picker rejection, and app-mode refusal.
They do not establish Shortcuts action argument mapping on Pyto.

### Pyto device checklist — pending

Record device model, iOS version and Pyto version.

- [ ] Register and verify a harmless batch program with a text input and a choice or number
  input. Review its source and note its stable id.
- [ ] In Shortcuts, use Pyto's Run Script action to select this `run.py`; pass
  `--shortcut-run`, the id, `--allow-unattended-saved-programs`, and each `--input` plus
  `name=value` as separate arguments. Pass values containing spaces, Unicode and `=`; confirm
  the program receives them unchanged and Get Script Output returns its result without a
  model request or API key.
- [ ] Omit the unattended flag. Confirm the command explains the required opt-in and the
  program's side-effect marker remains absent. Add the flag only after reviewing the source.
- [ ] Omit a required input and submit an invalid number or choice. Confirm each fails before
  the entry file runs.
- [ ] Pass `folder=@pick`; confirm the action reports that Shortcuts cannot open Pyto's
  picker and does not start the program. Pass a Pyto-readable path and confirm it is validated.
- [ ] Try an app-mode saved program; confirm it is refused with guidance to run it from Pyto.
- [ ] Cancel the Shortcut before the Run Script action; confirm no program side effect occurs.
- [ ] Check Show Console on and off, Get Script Output, and one user-triggered run versus an
  iOS automation. Record any foreground, permission or scheduling limits; do not interpret
  this as support for a persistent background service.

This environment has no iOS device or Pyto runtime, so all native Shortcut checks remain open.
