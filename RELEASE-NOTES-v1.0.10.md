# pyto-harness v1.0.10 candidate

Prepared 6 October 2026; not released. This work is carried forward in
the local v1.0.11 candidate. Earlier v1.0.9 notes cover the larger execution, approval,
recovery, preview, saved-program, input, memory and phone-chat changes.

## Platform-aware agent workflow

The agent's instructions distinguish desktop from Pyto/iOS and one-time actions from reusable
batch programs and interactive apps. They guide reusable work through documented capabilities,
examples, execution evidence and registration when appropriate. App previews have a separate
lifecycle and report validation, presentation, observed interaction, callback errors and
cleanup separately. Saved programs are registered before final verification so the library
stores the real result.

## Read-only Objective-C examples

`examples/objc_framework_recipes.py` demonstrates focused Foundation and UIKit reads through
Pyto's documented Rubicon-ObjC bridge. It reports unavailable framework symbols explicitly;
it does not claim that importing a framework grants permissions or exposes every selector.
The example and README link to Pyto, Rubicon-ObjC and Apple references.

## Verification

- Full desktop suite: `python3 -m unittest discover -s tests -t .` passed **860 tests** in
  90.580 seconds.
- Focused agent-loop workflow tests: **50 passed**.
- Example and iOS adapter coverage: **81 passed**.
- `python3 -m py_compile` passed for the changed prompt, recipe and test modules.
- `python3 stdlib_audit.py --json` returned `ok: true` with no suspicious imports or syntax
  problems.
- Pyto device verification remains pending. The prompt's native framework imports,
  Objective-C bridge behavior, PytoUI interaction and device-specific workflow need an
  iPhone or iPad running Pyto.
