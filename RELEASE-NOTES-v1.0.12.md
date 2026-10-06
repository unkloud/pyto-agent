# pyto-harness v1.0.12

Released 6 October 2026. This packaging release carries forward the v1.0.11 harness and
includes the device diagnostic in the tagged source archive, so the normal stable install
options deliver the script beside `run.py` and `harness/`.

## Run the device diagnostic

Open `device_release_diagnostic.py` in Pyto and press Run. It writes a Markdown report beside
the script with offline doctor results and a Goals 01–13 device acceptance checklist. The
checklist starts as **NOT RUN**; run and record each device step before treating it as passed.

Automatic checks make no network requests and apply no doctor fixes. The doctor reads
configuration metadata and checks recent session-log integrity; it may create and remove a
temporary workspace probe file and follows the normal legacy state-directory migration
behavior. On iOS, the script imports documented `Foundation` and `UIKit` modules and reads
`UIDevice` properties. It does not trigger permissions or run saved programs. The standalone
script is also available as a [release asset](https://github.com/unkloud/pyto-agent/releases/download/v1.0.12/device_release_diagnostic_v1.0.12.py).

## Verification

- The diagnostic script compiles and its offline report flow was smoke-checked on CPython
  3.12.3. The Linux run is not device acceptance.
- The carried-forward v1.0.11 harness passed 875 desktop tests; this packaging release does
  not change the harness behavior.
- Native Pyto/iOS acceptance remains pending. Use the report checklist to record Pyto, iOS
  and device details and the results for Goals 01–13.
