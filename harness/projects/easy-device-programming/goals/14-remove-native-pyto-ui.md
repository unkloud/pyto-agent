# Goal 14: Retire the native harness chat window

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation and release state before changing anything. Implement the outcome end to end rather than returning only a plan. Preserve existing user files, policy guarantees, and Pyto capabilities used by generated programs.

**Outcome:** The browser interface is the harness's only graphical chat front end. The harness no longer offers its own native Pyto chat window.

**Starting points:** `harness/ui.py` (shared browser/terminal event formatting and former native chat view), `harness/web.py`, `run.py`, `install.py`, `tests/test_ui.py`, `tests/test_ui_lifecycle.py`, `tests/test_install.py`, `tests/test_web.py`, `README.md`, `docs/DESIGN.md`.

**Preserve:**

- `python run.py --web`, including chat, browser approvals, saved-program forms, session history, cancellation, shutdown, authentication, and the server's same-device security boundary.
- Terminal chat, one-shot runs, diagnostics, saved-program commands, and other CLI entry points.
- Pyto device integrations and the ability for generated user programs to use `pyto_ui` and other Pyto modules.
- Existing CLI and Shortcuts file/folder input and picker behavior, input validation, saved-program execution, and Shortcuts support.
- Interactive approval policy for the browser front end. It must continue to treat the browser as an attached user even when stdin is not a TTY.

**Implement:** Remove `run.py --ui` and the installer's `install.py --ui` launch option, along with the native chat view, lifecycle helpers, and native Pyto chat form. Offer `--web` as the installer's graphical launch option; keep `--chat` for terminal chat. Remove only code and tests that exist solely for the native chat front end. Keep shared event formatting, interactive approvals, history formatting, and turn execution used by the browser and terminal. Update the CLI, docs, and tests. Do not remove PytoUI capability discovery, interactive preview, or PytoUI support for programs written by the agent. Avoid moving modules or refactoring unrelated features solely to make the diff smaller.

**Acceptance:**

- `--ui` is absent from both `run.py` and installer help and is rejected as an unknown option.
- Installer `--web` launches browser chat after setup; `--chat` still launches the terminal REPL.
- `--web` still uses interactive approvals and retains all browser functions and security/lifecycle behavior.
- Terminal chat, one-shot tasks, saved-program commands, CLI inputs and pickers, Shortcuts, and Pyto device tools retain their existing behavior.
- No native harness chat-view implementation or dead imports remain; PytoUI references needed for generated programs and previews remain.
- README and design docs describe the browser and terminal paths accurately.
- Add or update focused regression tests, run `python3 -m unittest discover -s tests -t .`, and run `python3 stdlib_audit.py`. Report device behavior as unverified unless tested on a device.

**Release:** Check the current main branch and latest published tag; use the next unused patch version (expected `v1.0.20` after `v1.0.19`). Update the package version, install instructions, and release notes. Commit to `main`, push `main` and the annotated version tag, create the GitHub release from that tag, and verify the published release. Do not claim Pyto device validation.

## Implementation record — v1.0.20

Removed the native chat view, native input form, lifecycle/stream helpers, `run.py --ui`,
and installer `install.py --ui`. The installer uses `--web` for browser chat and retains
`--chat` for terminal chat. Shared browser/terminal event formatting, approvals, history,
turn execution, PytoUI capability discovery, and generated-program previews remain.

Focused regressions passed **149 tests**. The full offline suite passed **880 tests** in
98.363 seconds, and `python3 stdlib_audit.py` passed with no third-party runtime modules
and Python 3.10 syntax compatibility. No Pyto device test was run for this change.
