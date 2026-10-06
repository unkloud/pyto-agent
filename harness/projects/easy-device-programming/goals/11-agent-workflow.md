# Goal 11: Align the agent prompt with the finished user workflow

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** The agent should consistently deliver something usable and reusable with minimal clarification.

**Prerequisites:** 05 interactive-preview; 06 saved-program-library; 07 simple-program-inputs; 09 durable-project-context. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/loop.py: SYSTEM_PROMPT, build_system_prompt; harness/pyto_api.py; tool descriptions; examples/README.md.

**Implement:**

Revise instructions around infer intent → inspect capabilities → build from supported examples → validate/preview → save → run again. Distinguish batch tasks from interactive apps and render platform-specific claims conditionally. Consult relevant Pyto API references before using them. Infer routine choices and ask only for information affecting correctness or user intent. Require precise verification language and registration of reusable artifacts when appropriate. Preserve diagnosis and real-outcome reporting. Describe only existing tools and behavior; policy enforcement stays in code.

**Acceptance criteria:**

Test prompt construction for desktop, Pyto and missing capabilities. Use deterministic mocked conversations for a one-time action, reusable batch program and GUI app. Confirm no instruction requires short non-interactive execution for an app, no unsupported APIs are promised, and final summaries identify the real saved artifact and achieved verification.

## Implementation record — local v1.0.10 candidate

The system prompt now reports the active desktop or Pyto runtime conditionally and distinguishes
one-time actions, reusable batch programs, and interactive apps. It directs reusable work through
existing examples, execution evidence, and registration when appropriate. App previews have a
separate path and distinguish syntax/import validation, presentation, instrumented interaction,
callback errors, and cleanup. Final guidance reports the saved title, id, path, and recorded result.
Reusable programs are registered before their final run or preview so the library can persist the
actual verification status instead of leaving a verified run unrecorded.

The prompt also directs native-framework work to narrow, documented recipes. The new
examples/objc_framework_recipes.py demonstrates read-only NSBundle and UIDevice lookups, with
Pyto, Rubicon-ObjC, and Apple references in examples/README.md. It does not claim general
Objective-C selector discovery. The example reports missing iOS modules on desktop, and the
ios.speak adapter has a mocked AVFoundation fallback test when Pyto's speech wrapper is missing.

Focused desktop verification: python3 -m unittest tests.test_loop -v passed 50 tests;
python3 -m unittest tests.test_examples tests.test_ios -v passed 81 tests. These include
prompt construction for desktop/Pyto/missing capabilities and mocked one-time, batch, and app
conversations. Fake Objective-C modules validate example control flow, not Pyto's native bridge.

### Pyto device checklist — pending

- [ ] Launch the harness on Pyto and confirm the prompt reports Pyto/iOS plus the features detected
  on that installation; compare with a desktop prompt and confirm it makes no device claims.
- [ ] Run examples/objc_framework_recipes.py on-device. Record Pyto version, iOS version, device
  model, bundle path, and the reported UIDevice values. Confirm the program reports an error if a
  framework symbol is unavailable, and do not treat this probe as proof of unrelated selectors.
- [ ] Ask for a one-time action, a reusable batch task, and an interactive app. Confirm the harness
  chooses the corresponding tool flow, reports a real registered artifact for reusable work, and
  distinguishes preview presentation from an interaction you actually tried.

This environment is Linux-only with no connected iPhone/iPad or Pyto runtime. Native bridge,
prompt-on-device, and interactive-device results remain unverified.
