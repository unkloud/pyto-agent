# pyto-agent v1.0.14

Released 6 October 2026.

## Fixes

- Use Pyto's property-style `UIDevice.currentDevice` access. The v1.0.13 device report narrowed its `TypeError` to invoking this value as a function. Pyto's own UIKit sample accesses it without parentheses; the recipe also handles a bridge that exposes a callable.
- Update `examples/objc_framework_recipes.py` and the device diagnostic to use that compatible access pattern.

## Device follow-up

Install this release and rerun `device_release_diagnostic.py`. A passing automatic probe still does not complete native acceptance. Record the Pyto app version and device details, then run and record Goals 01–13.

The standalone diagnostic is attached as `device_release_diagnostic_v1.0.14.py`.

## Verification

- Focused Objective-C recipe, adapter, doctor, and device-tool tests passed on CPython.
- The diagnostic's Objective-C property-style path was checked with simulated UIKit objects; this does not verify the native bridge on an iPhone.
