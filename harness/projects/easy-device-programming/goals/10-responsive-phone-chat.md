# Goal 10: Keep chat usable with the keyboard and long histories

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** The interface must remain readable and responsive during long conversations and on small screens.

**Prerequisites:** 01 readable-output; 02 working-chat-gui. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/web.py, harness/web_assets/app.js, harness/web_assets/style.css; session-backed details/history views.

**Implement:**

Coalesce streaming updates, bound rendered text and provide access to older history/details without losing the durable log. Replace fixed initial frames with supported layout behavior for rotation, keyboard appearance and different screen sizes. Preserve the user’s scroll position when reading history and make send/stop/approval controls reachable. Avoid copying an ever-growing transcript for every token. Reuse the lifecycle and renderer contracts introduced earlier.

**Acceptance criteria:**

Stress a long synthetic conversation and verify bounded display buffers and update frequency. Test final flush, close during pending flush and scrollback access. On phone/tablet, check keyboard, rotation, small-screen controls and long messages. Record measured results rather than assuming desktop tests establish native responsiveness.

## Implementation record — local v1.0.9 candidate (unpublished)

The Pyto chat now keeps its rendered transcript to 80,000 characters and batches ordinary
TextView assignments until 60 ms has elapsed or 2,048 characters are pending. Final output,
program listings and approval prompts force a flush, so a waiting approval is immediately visible.
Closing still forms a barrier against writes to the dismissed view. The display cap affects only
the view; it does not rewrite the session log.

A History/Older/Chat control pages the retained provider conversation 12 messages at a time,
including tool-call names, ids, arguments and results. Long individual entries are shortened in
the page with an explicit marker; the session path remains visible for the full JSONL details.
Returning to Chat restores its own bounded display buffer. PytoUI's documented `flex` autoresizing
flags now resize the transcript and keep the composer, approval controls and navigation attached
to the available edges as the root view changes size. See the [PytoUI layout guide](https://pyto.readthedocs.io/en/latest/library/pyto_ui.html).

**Desktop verification:** `python3 -m unittest tests.test_ui -v` passed **37 tests**. New coverage
checks coalesced writes, the 80,000-character cap, forced final flush, close barriers, bounded
history pages, explicit older-message access, history/chat buffer separation and approval prompt
visibility. These tests use a strict PytoUI fake and do not establish native keyboard, rotation or
screen-reader behavior.

### Pyto device checklist — pending

- [ ] Record device model, iOS version and Pyto version. In portrait on a small iPhone, focus the
  composer and confirm the keyboard does not cover Send, Stop or an Allow/Deny prompt; send a
  request with Return and confirm the next turn remains usable.
- [ ] Rotate to landscape, then test on an iPad or split-screen size. Confirm the transcript grows
  or shrinks with the view and Programs, History, composer and action buttons stay reachable without
  overlap.
- [ ] Run a long response while scrolling the transcript. Confirm the UI stays responsive, new
  output is batched, and the view remains bounded. Scroll to the top of the retained view, then use
  History and Older to inspect earlier user, assistant and tool details without changing the log.
- [ ] Return to Chat and confirm the latest view is restored. Close while an output flush is pending,
  reopen the saved session and confirm the session file remains readable.
- [ ] Repeat with an approval queued during a long response. Confirm the request appears promptly,
  the keyboard does not hide Allow/Deny, and tapping either control resolves only that request.

Record any device-side timing or layout measurements. No Pyto runtime or iOS device is connected to
this Linux environment, so native keyboard avoidance, autoresizing, scrolling performance and
accessibility remain unverified.
