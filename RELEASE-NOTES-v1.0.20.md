# pyto-agent v1.0.20

## Browser chat is the only graphical harness interface

This release removes the harness's native Pyto chat window. The `--ui` option is no
longer accepted by `run.py` or `install.py`. The installer now offers `--web` to start
browser chat after setup; `--chat` still starts terminal chat. Running PytoUI from
generated programs and interactive previews remains supported.

The existing browser interface retains chat, approvals, saved-program forms, session
history, cancellation, and same-device server protections. Terminal chat, saved-program
commands, Pyto device tools, Shortcuts, and CLI/Shortcut input handling remain available.

## Verification

- Focused browser, installer, CLI, terminal, and loop regressions passed: 149 tests.
- The full offline suite passed: 880 tests in 98.363 seconds.
- `python3 stdlib_audit.py` passed with no third-party runtime modules and Python 3.10
  syntax compatibility.
- No new Pyto device test was performed for this release.
