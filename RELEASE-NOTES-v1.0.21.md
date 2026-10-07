# pyto-agent v1.0.21

Released 7 October 2026.

## Evidence-based capability foundation

The documentation now inventories all 46 fixed model-facing capabilities with their
inputs, outputs, effects, prerequisites, retry metadata and evidence sources. Effects and
data-egress entries are descriptive metadata; they do not enforce policy or prevent
generated Python from using Pyto APIs directly.

The manual A1–A20 Shortcut validation kit and its focused guide describe harmless test
fixtures, structured observations and optional stress/recovery checks. It makes no LLM or
API calls. Shortcut callback behavior remains unverified until someone runs the script on
a real Pyto installation and records the results.

The installer extracts `shortcut_validation.py` into the installed `pyto-agent` folder
beside `run.py`. Open that script in Pyto and tap Run; a separate script download or copy
is not required. For the quickest check, create `pyto-harness-test-return`, choose `A1`,
and confirm the named fixture when prompted.

## Local handle pipeline prototype

An example demonstrates a workspace-file flow through local transformation and output.
Handles expose metadata while retaining text or typed binary artifacts outside model
context. This remains a local prototype; it does not add a registered model-facing
capability or enforce data-egress policy.

## Verification status

- No automated test suite or Pyto device run was performed for this release.
- Device-dependent Shortcut behavior remains unknown pending a dated run log from Pyto.
- No new iOS capabilities were registered.
