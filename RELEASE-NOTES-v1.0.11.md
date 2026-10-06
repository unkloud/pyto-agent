# pyto-harness v1.0.11 candidate

Prepared 6 October 2026; not released. The latest published release is
v1.0.8. This candidate includes the execution, approval, recovery, preview, saved-program,
input, project-memory, phone-chat and agent-workflow changes recorded in the
[v1.0.9 notes](RELEASE-NOTES-v1.0.9.md) and [v1.0.10 notes](RELEASE-NOTES-v1.0.10.md).

## Run saved batch programs from Shortcuts

`run.py --shortcut-run PROGRAM_ID` runs one registered batch program directly, without an
LLM request or API key. A Shortcut must also pass `--allow-unattended-saved-programs`, an
explicit opt-in for running that reviewed saved Python code without per-run approval.
Validated `--input NAME=VALUE` arguments preserve Unicode, spaces and `=` characters.
Missing or invalid inputs, picker requests and interactive app programs are refused before
the saved file runs. This option does not enable generated programs or blanket approval.

The README describes Pyto's Run Script action, passing separate argument values and
retrieving output. A path input must already be readable by Pyto; the native picker is not
available in this headless flow. iOS scheduling and background execution remain subject to
Pyto and iOS lifecycle limits.

## Novice workflow acceptance pack

The clipboard notebook accepts its registered `main(inputs)` form, reads Pyto's clipboard
when the optional text field is omitted, and writes the daily note beside its saved entry.
The organizer preview now has a confirmed Undo action; it restores files without overwriting
occupied original paths and removes only empty directories created during Apply. Added a
reproducible acceptance matrix joining the saved notebook, selected-program edit context,
organizer preview/apply/undo, persistent counter/form with mocked networking, Photos
permission denial, and interruption recovery. See
[`harness/projects/easy-device-programming/acceptance/README.md`](harness/projects/easy-device-programming/acceptance/README.md).

On Linux / CPython 3.12.3, the fresh-process saved-notebook runs measured 122.3 ms and
115.4 ms. This measures the local process start and saved action, not novice effort or iPhone
performance.

## Verification

- `python3 -m unittest discover -s tests -t .`: **875 tests passed** in 91.136 seconds, including the four retained lifecycle regression tests. The
  suite's mock provider uses localhost; no production API was contacted.
- The reproducible novice acceptance command in the linked matrix passed **19 tests** in
  0.485 seconds. The new `tests.test_novice_acceptance` module passed **3 tests**.
- `python3 -m unittest tests.test_shortcut_saved_run tests.test_program_inputs -v`:
  **15 tests passed**.
- `python3 -m py_compile run.py harness/program_inputs.py harness/__init__.py
  tests/test_shortcut_saved_run.py tests/test_program_inputs.py` passed.
- `python3 stdlib_audit.py --json` returned `ok: true`, with no syntax or suspicious-import
  findings. `py_compile` passed for the changed Python files.
- Pyto/iOS acceptance remains pending: native app presentation and keyboard behavior, folder
  picker/file provider, live network, Photos permission grant/denial, interruption, Objective-C
  recipes, and Shortcuts argument/output/lifecycle behavior have not been checked on a device.
  The linked goal files contain the exact device checklists for Goals 03–13.
