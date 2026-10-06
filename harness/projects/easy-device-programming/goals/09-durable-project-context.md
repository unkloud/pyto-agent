# Goal 09: Remember what the user is building

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** Users should not need to explain their app again after a new chat or compaction.

**Prerequisites:** 06 saved-program-library; 08 interruption-recovery. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/session.py: compact, project; harness/budget.py; harness/loop.py: prompt construction; program library.

**Implement:**

Persist a concise versioned project brief with user requirements, decisions, entry files, current verification and unresolved work. Update it at meaningful checkpoints and load the relevant brief for a selected program in a new session. Treat repository files and fresh execution evidence as authoritative over stale summaries. Budget provider request context separately from disk/event limits, preserving current tool-call integrity and necessary user intent. Keep projects separate and prevent untrusted program content becoming higher-priority instructions.

**Acceptance criteria:**

Create a program, record a requirement, force compaction, restart into a new chat and revise the program while retaining the requirement. Test stale/deleted files, two projects, oversized results and interrupted turns. Demonstrate bounded request construction without orphaned tool messages or silently discarding current requirements.

## Implementation record — local v1.0.9 candidate (unpublished)

Saved-program metadata is now schema version 3. Each record has a versioned project brief with
bounded requirements, decisions, related workspace paths and unresolved work. Entry path and
latest verification remain on the program record and are included with live file status when a
brief is read, so these changing facts are not copied into a stale summary. Schema versions 1
and 2 migrate with an empty brief. Read/update tools are scoped by saved-program id, and `/edit ID`
loads that program's brief and current file status automatically. The system prompt asks the agent
to record user-stated requirements at meaningful checkpoints and treats brief/source/tool text as
project data below system instructions.

Provider requests now have a separate 128 KiB serialized-message-and-tool-schema budget; this is a
conservative byte proxy, not a provider tokenizer. When needed, the loop removes only complete old
conversation turns, then shortens earlier assistant/tool result text while retaining tool-call IDs,
matching results and the entire current user request. If the current request and required schemas
cannot fit, it records a clear context-limit error and sends no provider request. The durable
session log is not changed by request trimming.

**Desktop verification:** `python3 -m unittest discover -s tests -t .` passed **845 tests** in
88.906 seconds. The 46 focused Goal 09 and adjacent tests passed. `py_compile` passed for all changed
runtime/test modules, and `python3 stdlib_audit.py --json` returned `ok: true` with no suspicious
imports or syntax problems. Tests cover schema migration, isolated briefs, live deleted-file status,
brief persistence through forced compaction and a new edit session, explicit update/read tools,
large tool-result trimming with intact call/result structure, old-turn omission, and refusal to
send when the current user request cannot fit.

### Pyto device checklist — pending

- [ ] On an iPhone or iPad, save two programs and give each distinct requirements. Read each brief
  and confirm the second program never receives the first program's notes.
- [ ] Add a requirement, force a long chat to compact, close/reopen Pyto, start a new chat and run
  `/edit ID <change>`. Confirm the requirement is present, the selected source is read before the
  edit, and the latest run result is reported separately from the brief.
- [ ] Delete a listed helper file, then the entry file. Read the brief again and confirm each is
  reported missing and the agent asks to restore or reselect a file before relying on it.
- [ ] Create a long history and a large file result. Confirm the request remains responsive, the
  current request is preserved, and a following provider call succeeds with its tool result still
  attached to the matching call id.
- [ ] Force-quit Pyto during a tool call, resume, and confirm Goal 08's recovery outcome is shown
  without replaying a possibly completed action; then edit the selected saved program.

Record device model, iOS version and Pyto version with results. This Linux environment has no
Pyto runtime or connected iOS device, so native persistence, long-history responsiveness and
provider behavior on-device remain unverified.
