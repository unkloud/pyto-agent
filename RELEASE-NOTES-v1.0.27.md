# pyto-agent v1.0.27

## Reuse the echo fixture

The A1 return-type check and A20 semantic-marker check now use the existing
`pyto-harness-test-echo` fixture with fixed marker inputs. The separate
`pyto-harness-test-return` and `pyto-harness-test-semantic-failure` fixtures are no longer
required by the validation suite. This reduces the `all` suite's required fixture names
from 11 to 9; the three A15 name-encoding fixtures remain necessary.

## Verification status

- The validation script compiles, and whitespace checks pass.
- No automated test suite or new Pyto device run was performed for this release.
- Device behavior remains unverified until a person runs the kit in Pyto and reviews the
  timestamped JSONL output.
