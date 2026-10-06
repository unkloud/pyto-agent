# Goal 06: Make saved programs easy to find, run and edit

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** Users can return later and use what they built without locating files or making another model request.

**Prerequisites:** 03 safe-program-execution; 05 interactive-preview. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/tools_ios.py: write_program, run_program; harness/ui.py; harness/home.py; harness/session.py; examples/.

**Implement:**

Implement a lightweight persistent program library with title, purpose, workspace-relative entry file, batch/app mode, required capabilities and last verification result. Register programs through an explicit tool-backed workflow. Provide Run and Edit actions, plus a terminal equivalent. Run saved programs without calling the model; Edit must provide the selected program context to the agent. Handle missing, renamed or invalid entry files with actionable recovery and preserve user-authored files. Use versioned metadata and respect workspace boundaries. Update completion summaries to point to real usable actions.

**Acceptance criteria:**

Create a program, restart the harness, locate it by title and run it with model access disabled. Edit the selected program without manually supplying its path. Test missing files, malformed metadata, duplicate titles and failed verification. Confirm registration and reruns survive process restart.

## Implementation record — local v1.0.9 candidate

Implemented `harness/programs.py` as a versioned (`schema_version: 1`) JSON index stored in the workspace. Each record stores an id, title, purpose, workspace-relative Python entry file, `batch`/`app` mode, required capabilities and the latest verification result. Index writes are bounded, private and atomic. Paths are resolved through the workspace jail; malformed or unsupported metadata is left untouched with a recovery message. Source files stay in place. Harness-mediated edits clear stale verification, and runs update the record without retaining program output in the index.

Programs are registered with the `register_program` tool and listed with `list_saved_programs`. The Pyto chat has a Programs button and `/programs`, `/run ID` and `/edit ID <requested change>` commands; the terminal REPL accepts the same commands. `--programs` and `--run-saved ID` provide CLI equivalents. Explicit Run actions invoke the existing batch or preview handler directly, with no provider/model request. Edit carries the selected record and entry path into an agent turn and instructs the agent to read the chosen source before editing. The saved-program index itself is protected from direct file-write tools.

### Desktop verification

The full suite passed **811 tests** in 86.083 seconds. New coverage checks persistence across library reloads, Unicode and spaces in paths/titles, duplicate titles, malformed-index preservation, workspace path boundaries, missing/renamed entries, stale verification invalidation, failed verification, model-free saved runs, failure recording, CLI operation without a provider key, chat/terminal commands and selected-program edit context. `python3 -m py_compile harness/programs.py harness/tools_ios.py harness/loop.py harness/ui.py run.py` passed. `python3 stdlib_audit.py --json` returned `ok: true` with no suspicious imports or syntax problems. These are desktop checks; they do not verify Pyto UI behavior on iOS.

### Pyto device checklist — pending

Record the device model, iOS version and Pyto version with the results.

- [ ] Create a reusable batch program in chat; confirm it is registered with a stable id, title and workspace-relative path.
- [ ] Force-quit and reopen Pyto. Confirm the Programs button and `/programs` still show the same record and last verification.
- [ ] Run it using `/run ID`; confirm the expected output and that the saved program runs without a model turn.
- [ ] From Pyto's script launcher, run `run.py --programs`, then `run.py --run-saved ID` with provider credentials unavailable; confirm listing and batch execution work.
- [ ] Use `/edit ID <change>` and confirm the agent reads and edits the selected entry file without asking the user to locate or retype its path.
- [ ] Rename or remove an entry file. Confirm it stays listed with a clear recovery message, then register its repaired workspace path using the existing id.
- [ ] Try a duplicate title and a malformed `pyto-programs.json`; confirm registration reports the problem and leaves the existing file intact.
- [ ] Confirm a failed run is recorded, a successful rerun updates the status, and editing the source clears the old verification.
- [ ] Register an app-mode program and confirm `/run ID` opens its interactive preview; close it and confirm the chat remains usable.
