# pyto-agent v1.0.26

## One-confirmation Shortcut test batch

The validation script's `all` suite now displays the complete fixture list and runs all
scriptable A1–A20 checks after one `RUN ALL` confirmation. It no longer asks for a separate
confirmation before each Shortcut call. The full suite includes A5/A13 stress sizes and
the 60-second A6 wait, so A6 may block if the callback does not return.

A3 and A18 can still require a tap on an iOS prompt. A7, A8, A16, A17, and A19 need manual
checks that this standalone script cannot perform; during `all` they are logged as
`unknown` and the batch continues. Fixture recipes remain available through `setup` or
`--setup`.

## Verification status

- No automated test suite or new Pyto device run was performed for this release.
- Device behavior remains unverified until a person runs the kit in Pyto and reviews the
  timestamped JSONL output.
