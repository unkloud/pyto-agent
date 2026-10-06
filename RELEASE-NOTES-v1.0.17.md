# pyto-agent v1.0.17

Released 6 October 2026.

## Shortcuts diagnostic capture fix

The v1.0.16 device report showed that the five Shortcuts saved-run cases reached their
expected return codes and preserved the no-execution checks, but their expected response
text was missing from captured stdout/stderr. This is consistent with Pyto's native console
not following `contextlib.redirect_stdout` and `redirect_stderr` in the test process.

The diagnostic tests now capture `run.py`'s module-level print calls directly, without
replacing the interpreter's global console streams. They still assert the opt-in and refusal
messages, and the Unicode saved input. No Shortcuts command behavior changed in this release.

This scripted check does not launch the Shortcuts app or certify a real Pyto Run Script
handoff. Manual Goal 12 remains `NOT RUN` until that device interaction is performed.

## Verification

- The focused Shortcuts saved-run tests pass on desktop, including in-process program execution.
- The complete offline suite and standard-library audit are run for this release.
- Desktop results do not count as native iPhone/Pyto acceptance.

The standalone diagnostic is attached as `device_release_diagnostic_v1.0.17.py`.
