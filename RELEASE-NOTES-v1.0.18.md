# pyto-agent v1.0.18

## Browser interface for the harness

This release adds an optional `python run.py --web` front end for using the existing
Pyto harness from Safari on the same device. It provides chat, approval controls, saved
program forms, and session history. The server listens only on loopback, uses a random
per-run token, and keeps the provider API key inside the Python process.

The interface includes controls to stop a turn or close the web session. When started in
Pyto it uses a background task to help the server stay active while Safari is foregrounded.
As with other iOS background work, suspension or termination under system pressure remains
possible. The [browser interface guide](docs/web-interface.md) lists the device checks.

## Verification

- The complete offline suite passed: 889 tests.
- The focused web tests passed, covering the local HTTP interface, authentication, chat,
  approvals, saved-program execution, cancellation, and shutdown.
- A desktop browser smoke check covered the main page and a narrow mobile viewport.
- Safari behavior, Pyto background behavior, and iOS lifecycle handling still need
  acceptance on a real device.
