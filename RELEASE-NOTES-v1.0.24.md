# pyto-agent v1.0.24

## A1 Shortcut fixture install link

The validation guide and A1 prompt now include a shared iCloud link to the harmless
`pyto-harness-test-return` fixture. This avoids manually building the A1 fixture. Apple
still requires a person to tap **Get Shortcut** to add it. The validation script itself
does not make network calls; the one-time import is handled by Shortcuts and iCloud.

The A1 fixture is the only test shortcut included through this link. Other A1–A20 fixtures
retain their existing instructions.

## Verification status

- The shared iCloud URL was supplied by the user; this environment could not inspect its
  contents. The fixture’s expected behavior is based on the user’s successful A1 run.
- No automated tests or new Pyto device tests were performed for this release.
