# pyto-agent v1.0.22

Released 7 October 2026.

## Shortcut validation URL correction

The A1 validation case now follows Pyto's documented `open_shortcut` example: it passes
the `shortcuts://x-callback-url/run-shortcut` action URL to `xcallback.open_url` without
adding an `x-success` parameter itself. Pyto's xcallback module manages the return path.
The registered `shortcut_run_wait` wrapper still constructs its own callback URL; A7
checks that adapter separately.

This corrects the validation kit after an A1 run using v1.0.21 returned `RuntimeError`.
That observation used the former test URL and does not establish whether the direct Pyto
callback path works. Re-run A1 with this release before drawing a device conclusion.

## Verification status

- No new automated or Pyto device tests were run for this release.
- Device behavior remains unverified until a person runs the corrected A1 case and keeps
  the dated JSONL result.
