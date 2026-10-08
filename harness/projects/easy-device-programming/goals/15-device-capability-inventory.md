# Goal 15: Device capability inventory and evidence-backed override

## Executable prompt

Work in this repository. Read the existing project goals, `AUDIT-2026-10-05.md`, the current capability audit in `docs/pyto-capabilities.md`, `docs/capability-contracts.json`, `harness/pyto_api.py`, `harness/ios.py`, and `harness/tools_ios.py` before editing. Inspect current code and tests; prior goals may have changed the cited files. Implement the outcome end to end. Do the work automatically wherever the environment supports it: inspect the repository, research official Pyto and Apple sources online, populate and validate the inventory, update schemas and documentation, implement the checks, and run the relevant tests. Do not pause for routine review, confirmation, or manual curation. Preserve existing behavior and user data. Device verification must come from an automated end-to-end check run in the target Pyto installation and recorded with its date and runtime versions; documentation or desktop tests alone cannot establish it. If target-device execution is unavailable, complete all desktop- and internet-verifiable work and leave the automated on-device check runnable with its limitation clearly recorded. Finish with changed files, test results, and remaining limitations.

**Outcome:** The harness has a modest, documentation-backed inventory of capability directions, records device-specific evidence separately, and lets the LLM choose how to solve a task using that evidence.

**Prerequisites:** The saved-program and custom-tool creation paths. Verify their current schemas and behavior before extending them; do not infer APIs from earlier goal text.

**Starting points:** `docs/capability-contracts.json`; `docs/pyto-capabilities.md`; `harness/pyto_api.py`; `harness/ios.py`; `harness/tools_ios.py`; `harness/programs.py`; `harness/custom_tools.py`; `harness/handles.py`; `harness/config.py`; `tests/`.

## Implement

### 1. Two-layer inventory

- Add a checked-in global inventory at `docs/capability-inventory.json`. It ships with the software, is read-only at runtime, and describes a basic set of documented capability directions. It is not intended to be complete.
- Add a separate device override file, `capability-overrides.json`, in the harness state directory. The harness creates and updates it on the target device with evidence from that installation.
- Keep both files as distinct JSON data files with a top-level integer `version`. Do not introduce a contract-versioning scheme or fold these records into `docs/capability-contracts.json`. That file describes registered tool contracts and retains its existing purpose.
- The global inventory does not claim device verification. Treat a point without a fresh device override as `unverified`. A current override for the target device takes precedence for that point. When the device model, iOS version, or Pyto version changes, treat its old evidence as stale and the effective status as `unverified`; retain the old evidence for audit rather than presenting it as current.
- Record enough non-unique runtime information to assess freshness: device model, iOS version, Pyto version/build, and evidence date. Do not use a unique device identifier.

### 2. Capability point format

Each point uses a stable `<resource>.<action>` ID in lowercase, dot-separated form, such as `clipboard.read`, `clipboard.write`, `calendar.read`, `calendar.write`, `notification.send`, or `location.read`.

Each point records its ID, one-line description, at least one Pyto or Apple official documentation link, status, evidence, and date. Status is exactly one of `verified`, `unavailable`, or `unverified`. The global inventory and any device override record must not store concrete Python API signatures; the links let the LLM consult the source for the actual API.

### 3. Seed the global inventory

- Derive a basic set of current capability points from `harness/tools_ios.py` and `harness/ios.py`, including directions already represented by adapters such as clipboard read/write.
- Research Pyto and Apple official documentation online and add a basic set of clearly possible directions. Generate and populate the inventory as part of the task; do not stop for a person to curate or approve entries. Do not expand into an exhaustive catalog or add new iOS capabilities as part of seeding.
- Give every point at least one official documentation link. Where Pyto has no published API page, use the relevant official Pyto source or an applicable Apple reference; do not invent API signatures.
- Global entries remain `unverified` for a target device even when official documentation describes the direction.

### 4. Minimum-path testing during tool creation

When a newly created tool or saved program depends on an `unverified` point, run an inline minimal-path test during its creation. Isolate the smallest step that can fail: import, attribute lookup, instantiation, permission request, or call return. Record the exact step and exception or platform result. Do not build a general-purpose probe framework, and do not use a broad probe when a minimal test will answer the question.

A failed test indicates `unavailable` only when its evidence points to a deterministic Pyto or platform limitation. A defect in the generated tool or test code is a code failure to fix, not evidence that the device lacks the capability.

### 5. Device override write rules

- Record `unavailable` only for a substantive, deterministic failure such as `ImportError`, `PermissionError`, `AttributeError`, or an equivalent platform failure. Store the failed step and evidence, not merely a generic failure label.
- Do not change the override for timeouts, network flakiness, user cancellation, or other incidental failures. A passing minimal-path test alone also does not mark a point `verified`.
- Mark `verified` automatically only after an automated end-to-end check has a meaningful success result on the target Pyto installation. Store the test identity/result, date, and device/runtime versions with that evidence. Do not ask the user to review or confirm the result. If a safe, deterministic end-to-end success condition cannot be established for a point, leave it `unverified` rather than inferring success from documentation or an import-only probe.
- Initialize the versioned override envelope as needed, but do not persist an unverified status merely because a test was attempted.

### 6. Capability dependencies and autonomous problem solving

- Newly created tools and saved programs declare their capability dependencies using the `<resource>.<action>` IDs. Do not retrofit existing registered tools with declarations in this goal. Do not reinterpret existing human-readable capability labels as IDs.
- A `verified` dependency may be used directly. An `unverified` dependency requires the minimal-path test during tool creation. An `unavailable` point rules out that capability for the target device.
- The inventory is evidence for the LLM, not a route planner. Do not encode alternatives, route priority, fallback chains, or a hard-coded selector in capability metadata or harness code. An unavailable point does not imply that other APIs or approaches are unavailable. The LLM may independently consult the documentation and choose how to solve the task. If its attempted approaches fail, it can explain the limitation; record deterministic failures against the individual points they tested rather than inventing a broader unavailable status. The harness does not enumerate every possible solution or create an “all routes exhausted” capability state.
- Include a focused tool-creation case where a direct capability is unavailable and the creator supplies a tool using another documented approach. Verify the harness does not require predeclared alternative-route metadata or reject the tool merely because an unrelated point is unavailable. An overall inability to solve belongs to the result of the LLM's attempts, not to the inventory schema.

### 7. Documentation links in tool context

When a tool or saved program declares a capability dependency, make the matching documentation links available to the LLM before it writes or finalizes code that uses the capability. Surface links and evidence only; signatures remain in the linked official documentation or existing API-reference mechanism.

### 8. Persistent typed handles

- Persist handles as typed artifacts in the harness workspace so they remain usable across a session restart. Keep stable IDs and record each artifact's type, shape, size, preview policy, and lifetime.
- Store operation state separately from handle metadata.
- Demonstrate ingest, use, and retrieval of a handle across a process/session restart. Preserve the existing workspace boundary and private-file behavior.
- Data-egress constraints are outside this goal.

### 9. Automated Real-Pyto checks

Add a no-LLM automated check script that a person can launch in Pyto. It should run ordinary checks by default, separate opt-in stress checks, and record device model, iOS version, Pyto version/build, date, per-step outcomes, and evidence automatically. Do not require manual review or confirmation of successful automated results. Do not enumerate Shortcuts or invoke unknown or personal Shortcuts; if a Shortcut check is included, use only a clearly named test fixture and make it opt-in. Keep checks unrun on the target installation `unverified`.

### 10. Regression coverage and exclusions

Cover all three statuses; override writes for substantive versus incidental failures; automatic `verified` writes after successful target-Pyto end-to-end checks without a human confirmation step; stale evidence after device, iOS, or Pyto version changes; documentation-link surfacing; and the unavailable-direct-capability case where the tool creator supplies another documented approach. Verify that no device behavior becomes `verified` without a dated target-Pyto test result.

Do not modify the Web UI; implement EventKit, Contacts, HealthKit, generic App Intents, Shortcut enumeration, or background scheduling; add broad new iOS capabilities; build a standalone probe subsystem; change the LLM provider integration or session-log format; or retrofit existing tools with dependency declarations.

## Acceptance criteria

- The checked-in global inventory and device override are separate versioned data files with the defined capability point format and documentation links.
- Minimal-path testing is used during creation of tools with unverified dependencies. The override records qualifying unavailable evidence or automatically verified end-to-end results from the target Pyto installation; no human review or confirmation gate is required.
- A new tool declares a capability dependency and exercises the override path; a tool-creation test covers an unavailable direct capability and another documented approach without a predefined alternatives list.
- Typed handles persist and remain usable across a session restart, with operation state separate from handle metadata.
- A no-LLM automated Real-Pyto check script exists, separates ordinary and opt-in stress checks, records evidence automatically, and avoids unknown or personal Shortcuts.
- Documentation is updated. No device behavior is claimed verified without a dated entry from the target Pyto installation.
