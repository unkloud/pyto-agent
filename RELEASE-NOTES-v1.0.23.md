# pyto-agent v1.0.23

## Shortcut validation diagnostics

The A1 result now records the installed harness version and includes a short exception
message when the direct `xcallback.open_url` call fails. URL strings are redacted and the
message is capped at 240 characters. This gives the on-device check enough detail to
distinguish common callback and handoff errors without logging the callback URL.

The updated script remains bundled with the installer beside `run.py`; no separate script
download is needed.

## Verification status

- No automated tests or new Pyto device runs were performed for this release.
- Existing device observations remain specific to the kit versions recorded in their logs.
