# Goal 07: Replace argument editing with friendly inputs

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** A phone user should select a folder or fill a form instead of editing sys.argv.

**Prerequisites:** 06 saved-program-library; 04 in-app-approvals. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** Program library and runner introduced by preceding goals; harness/ui.py; harness/ios.py; examples/.

**Implement:**

Add a small validated input schema for saved programs and render appropriate controls for text, numbers, choices and files/folders. Provide a terminal equivalent. Use supported Pyto pickers and accessible-path handling; represent cancellation and unavailable capabilities explicitly. Translate submitted values into the documented program input contract without generated shell commands. Remember only appropriate non-sensitive defaults. Supply a folder organizer example with a preview and explicit apply step; make the proposed changes understandable before execution.

**Acceptance criteria:**

Run a saved program requiring a folder and options without code/path editing. Test missing/invalid inputs, picker cancellation, inaccessible files, Unicode and spaces. Validate that preview changes nothing and apply performs the reviewed operation. Confirm rerunning with different inputs uses the new values.

## Implementation record — local v1.0.9 candidate (unpublished)

Added a bounded input schema for text, number, choice, file and folder values. Registration stores
the schema definition, while chat and terminal runs collect fresh values. The Pyto chat form uses
text fields, a choice control and the documented file/folder pickers; terminal `/run ID` prompts
for values, and `--run-saved ID --input NAME=VALUE` supports direct runs and `NAME=@pick` for
picker-backed paths. Only a stable, non-sensitive choice default may be stored; text, numbers and
paths are requested each run. Values are validated before execution and passed to `def main(inputs)`
without constructing a shell command. Batch and app programs use the same entry contract.

The new folder organizer separates deterministic read-only planning from applying moves. Its app
shows the selected folder and proposed destinations, then requires an explicit **Apply these
moves** action. Apply rechecks the whole plan, refuses stale/colliding destinations and rolls back
completed moves if a later move fails. Desktop fake-UI and filesystem tests cover validation,
cancellation, Unicode names, preview without changes and explicit apply.

**Desktop verification:** `python3 -m unittest discover -s tests -t .` passed **832 tests** in
87.852 seconds. `python3 -m py_compile` passed for the changed runtime, example and test modules;
`python3 stdlib_audit.py --json` returned `ok: true` with no problems. These results do not
establish Pyto picker, file-provider or native form behavior.

### Pyto device checklist — pending

- [ ] Register the organizer as an app with folder, grouping, collection-name and file-limit inputs.
- [ ] Use the Run form to select a folder through Pyto's picker and choose a grouping option. Confirm
  the form shows validation errors and that Cancel or picker cancellation starts no program.
- [ ] Open the organizer preview and compare the displayed move list to the selected folder. Confirm
  no directory or file changes before tapping **Apply these moves**.
- [ ] Apply the reviewed plan. Confirm the files are in the shown destinations and existing files
  were not replaced.
- [ ] Run the saved app again with a different folder and grouping choice. Confirm it requests fresh
  values and does not reuse the prior paths or selections.
- [ ] Run a saved batch program from the Pyto console with `--run-saved ID --input name=@pick` and
  confirm the file/folder picker returns a usable path.
- [ ] Record behavior for a folder with unreadable contents or a selected file that becomes
  unavailable before submission.

Record device model, iOS version and Pyto version with results. Desktop tests do not establish
Pyto picker, file-provider or native form behavior.
