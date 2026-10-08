# Goal 15 Pyto capability checks

Run `device_capability_checks.py` from the repository in the target Pyto installation. It
does not call an LLM, contact a model provider, enumerate Shortcuts, or invoke a Shortcut.
It records evidence in `capability-overrides.json` in the configured harness state directory.

## Ordinary checks

1. Launch the script in Pyto. It records the non-unique device model, iOS version, Pyto
   version/build, date, and the result of import/member checks for the documented Pyto
   modules. A passing import/member lookup remains `unverified`; it is only a minimal-path
   result. A deterministic `ImportError` or missing documented member can record
   `unavailable` with the failed step.
2. The first launch creates a private text fixture and typed handle in the harness
   workspace, then reports `pending_restart`.
3. Close and reopen Pyto, then launch the script again. It retrieves the same handle ID,
   checks the fixture checksum, removes the temporary fixture, and records
   `workspace.persist` as `verified` when the end-to-end check passes.

The script prints a compact JSON report. It does not print file contents, location values,
or selected photo data. The override stores only check identity, result, date, status, and
the non-unique runtime fingerprint. Existing stale evidence is retained and shown as
`unverified` when any fingerprint component changes.

## Opt-in stress checks

Pass `--stress` to include two interactive checks:

- `location.read` requests the current location and discards the coordinates after checking
  that they are numeric and in range. A permission prompt may appear. A canceled or empty
  result leaves the override unchanged.
- `photo.pick` opens Pyto's photo picker and discards the selected image after checking its
  dimensions. Canceling leaves the override unchanged.

These checks are not run by default. They do not upload or persist location/photo data.
Other capabilities that would send notifications, speak, modify Photos, launch a Shortcut,
or share a file are not exercised by this script.

No device result is inferred from running this script on desktop or from documentation.
