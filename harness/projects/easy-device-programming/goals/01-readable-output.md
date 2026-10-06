# Goal 01: Make everyday output readable

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** Users must see progress, actionable errors and the final result without scrolling through internal tool output.

**Prerequisites:** None. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/ui.py: Printer; harness/web.py: browser stream; run.py; install.py: START_PY.

**Implement:**

Implement concise default rendering shared by terminal and GUI. Show short meaningful operation statuses and one final answer; keep complete tool bodies, arguments and timing behind verbose mode or an accessible Details path. Preserve full information needed for approval decisions and useful failure diagnostics. Fix the uncleared streamed partial buffer and duplicate completion text. Keep start.py as a launcher rather than patching output there. Make non-streaming answers visible too. Preserve diagnostic data without leaking credentials.

**Acceptance criteria:**

A large pyto_api response produces one concise normal status rather than its full body; verbose/details still exposes the diagnostic content. Streamed and non-streamed responses appear exactly once. Test error, denied, interrupted, finish-tool and normal assistant completion paths. Verify that changing presentation does not change model-visible tool results or approval policy.

## Implementation and verification status — 5 October 2026

**Implementation complete; automated verification complete; device verification pending.** The shared `Printer` now shows concise operation statuses, useful artifact paths and short actionable errors by default. Tool arguments, result bodies and timings are available with `--verbose` in both terminal and GUI runs; when a tool result was shortened, verbose output points to the full spill file. Verbose output scrubs credentials. Assistant deltas are held until the complete message can be scrubbed, then the answer is displayed once; this also prevents credentials split across provider chunks from leaking. Delta-free completions remain visible, identical finish-tool text is not repeated, and the rendered GUI history is capped at 200,000 characters. `start.py` remains a launcher; output handling stays in `harness/ui.py`.

Regression coverage exercises large `pyto_api` results, verbose details, credential scrubbing, streamed and non-streamed replies, error and denial messages, interruption, finish-tool completion, transcript deduplication, bounded GUI history and an unchanged tool result in the next model request. The repository's approval-policy tests still pass.

**Desktop results:** `python3 -m unittest tests.test_ui -v` — 18 tests passed. `python3 -m unittest discover -s tests -t .` — 735 tests passed. The integration suite used its localhost mock provider. These checks do not verify Pyto's native UI or iOS behavior.

## On-device checklist — pending

Run these in Pyto from the folder containing `run.py`. Leave `start.py` unchanged; it remains the launcher. To pass run flags from the Pyto console, use this snippet and replace the folder path with the installed harness directory:

```python
import os, runpy, sys
os.chdir("/path/to/pyto-harness")
sys.argv = ["run.py", "--web"]
runpy.run_path("run.py", run_name="__main__")
```

Repeat by changing only the `sys.argv` list. For example, add `"--no-stream"` to check the non-streaming provider path or `"--verbose"` to see tool details.

- [ ] **Normal GUI output:** launch with `--web` and ask: “Use `pyto_api` with no arguments to list the Pyto modules, then summarize the result.” Confirm that the reference body is not dumped into the chat, one short “Checking the Pyto API reference…” status appears, and the final answer appears once.
- [ ] **Non-streaming answer:** launch with `--web --no-stream` and ask: “Reply exactly: non-stream check complete.” Confirm the answer is visible once.
- [ ] **Verbose details:** launch with `--web --verbose` and repeat the Pyto API request. Confirm tool arguments, result body and timing are visible, the answer is not duplicated, and credentials remain redacted.
- [ ] **Error and denial:** ask it to run a harmless program that raises `ValueError('readable output check')`; confirm a short actionable error appears. Then ask it to open `https://example.com` and deny the request if prompted; confirm the URL is not opened and one concise denial reason appears.
- [ ] **Interruption:** in terminal chat mode, interrupt a deliberately slow request using Pyto's Stop/Ctrl-C control. Confirm the transcript reports “Interrupted.” If Pyto stops the script before it can render that status, record that as a device limitation.

Record the device model, iOS version and Pyto version with pass/fail notes here after running the checklist. Until then, native GUI, interruption and layout behavior remain unverified.
