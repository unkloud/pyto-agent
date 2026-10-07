# pyto-agent v1.0.25

## Shortcut fixture instructions printed in Pyto

The validation script now supports `--setup`, which prints the setup steps for all A1–A20
Shortcut fixtures. Before running a case, it also prints the recipe for each fixture that
case will invoke. This keeps the required fixture names, actions, safety notes, and A1
iCloud install link available on-device without returning to the documentation.

Pyto cannot author or install Shortcuts from Python. A1 remains available through the
shared iCloud link; other fixtures are created once in the Shortcuts app. The script still
invokes only explicitly named `pyto-harness-test-` fixtures and requires typing `RUN`.

## Verification status

- No automated test suite or new Pyto device run was performed for this release.
- Device behavior remains unverified until the user runs the kit in Pyto and reviews the
  timestamped JSONL output.
