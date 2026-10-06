# pyto-agent v1.0.13

Released 6 October 2026.

## Fixes

- Use Pyto's documented `file_system.share_text()` and `file_system.share_files()` APIs for the native share sheet. The previous `share.open` probe did not match this device's Pyto API and caused a file fallback.
- Update the offline doctor to inspect the documented file sharing functions instead of the unsupported `share.open` shape.
- Split the Objective-C diagnostic into framework imports, `UIDevice.currentDevice()`, and three individual read-only property reads. The report now names a failed step without exposing exception messages or local paths.

## Device follow-up

Please rerun `device_release_diagnostic.py` after installing this release. The v1.0.12 report cannot identify which `UIDevice` operation raised `TypeError`, because that version grouped the steps together and suppressed the failure stage. The revised report will isolate it. Manual Goals 01–13 still require on-device execution and evidence.

The standalone diagnostic is attached as `device_release_diagnostic_v1.0.13.py`.

## Verification

- 30 focused adapter, doctor, and device-tool tests passed on CPython.
- The diagnostic's staged failure reporting was checked with a simulated property `TypeError`; it identified the property and withheld the simulated path.
- These checks do not count as native Pyto/iOS acceptance. Goals 01–13 still require the target device.
