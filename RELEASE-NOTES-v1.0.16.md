# pyto-agent v1.0.16

Released 6 October 2026.

## Shortcuts diagnostic follow-up

The v1.0.15 iPhone report showed that five scripted Shortcuts saved-run cases failed,
but omitted the failure details. The diagnostic now includes a short exception summary for
each failed case, while redacting credential-shaped values, URLs and local paths. This makes
the next device report actionable without including test tracebacks or user files.

The saved-run behavior and its test cases are unchanged in this release. The next on-device
run will show whether those failures are caused by the saved-run implementation or by a
Pyto-specific test/runtime difference. The checks still do not invoke the Shortcuts app or
prove native handoff behavior.

## Verification

- The full offline desktop test suite and standard-library audit are run for this release.
- Desktop execution does not count as iPhone/Pyto acceptance; report the updated Goal 12
  result from the device to identify the remaining cause.
- All manual UI acceptance items remain `NOT RUN` until performed and recorded on-device.

The standalone diagnostic is attached as `device_release_diagnostic_v1.0.16.py`.
