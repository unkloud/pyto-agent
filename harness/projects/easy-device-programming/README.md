# Easy device programming — executable goals

Source: [audit](../../../AUDIT-2026-10-05.md). These prompts turn every audit finding into implementation work. No implementation is started by creating this folder.

## How to use

Give an agent one entire goal file as its task, or say “Implement the goal in `<path>`.” Each file includes scope, prerequisites, starting points and acceptance criteria. The original rollout sequence is 01/02, 03/04, 08, 05, 06/07, 09–12, then the Goal 13 release gate. Goal 14 retires the native harness chat after the browser interface is available; it preserves PytoUI support for generated apps. Device checks remain attached to each relevant goal, not only the final gate.

Readable output comes first because it improves the current default terminal experience immediately. A working GUI, predictable execution and accessible approvals follow because they unblock basic use. Preview, saved programs and friendly inputs create the reusable-tool experience. Recovery, memory and phone polish sustain it. Prompt consolidation, Shortcut handoff and the final acceptance pack complete the workflow. Device acceptance checks also belong to each relevant goal, not only the last one.

## Ordered goals

| Order | Executable goal | User benefit | Prerequisites |
| --- | --- | --- | --- |
| 01 | [Make everyday output readable](goals/01-readable-output.md) | Users must see progress, actionable errors and the final result without scrolling through internal tool output. | None |
| 02 | [Make chat open, respond and close reliably](goals/02-working-chat-gui.md) | A person must be able to chat in Pyto without crashes, frozen windows or callbacks writing to closed sessions. | None |
| 03 | [Keep program runs from interfering with each other](goals/03-safe-program-execution.md) | One generated program must not corrupt another run or make the chat silently unusable. | None; required before interactive preview |
| 04 | [Let users answer approvals where they are working](goals/04-in-app-approvals.md) | A visible chat should never require switching to an invisible console or enabling blanket approval. | 02 working-chat-gui |
| 08 | [Resume safely after the app is interrupted](goals/08-interruption-recovery.md) | Returning after an app kill should preserve progress and avoid duplicated actions. | 03 safe-program-execution |
| 05 | [Build and try real interactive apps](goals/05-interactive-preview.md) | Users should be able to try an app, leave it open, report a problem and rerun the revision. | 02 working-chat-gui; 03 safe-program-execution; 04 in-app-approvals |
| 06 | [Make saved programs easy to find, run and edit](goals/06-saved-program-library.md) | Users can return later and use what they built without locating files or making another model request. | 03 safe-program-execution; 05 interactive-preview |
| 07 | [Replace argument editing with friendly inputs](goals/07-simple-program-inputs.md) | A phone user should select a folder or fill a form instead of editing sys.argv. | 06 saved-program-library; 04 in-app-approvals |
| 09 | [Remember what the user is building](goals/09-durable-project-context.md) | Users should not need to explain their app again after a new chat or compaction. | 06 saved-program-library; 08 interruption-recovery |
| 10 | [Keep chat usable with the keyboard and long histories](goals/10-responsive-phone-chat.md) | The interface must remain readable and responsive during long conversations and on small screens. | 01 readable-output; 02 working-chat-gui |
| 11 | [Align the agent prompt with the finished user workflow](goals/11-agent-workflow.md) | The agent should consistently deliver something usable and reusable with minimal clarification. | 05 interactive-preview; 06 saved-program-library; 07 simple-program-inputs; 09 durable-project-context |
| 12 | [Make saved tools available through Shortcuts](goals/12-shortcut-handoff.md) | Users can invoke suitable saved automations from familiar iOS entry points. | 06 saved-program-library; 07 simple-program-inputs; 08 interruption-recovery; 11 agent-workflow |
| 13 | [Verify the complete novice workflow](goals/13-usability-release-gate.md) | A release should prove that people can create and reuse useful device tools with little manual work. | 01–12; Shortcut checks apply only where supported |
| 14 | [Retire the native harness chat window](goals/14-remove-native-pyto-ui.md) | Keep one supported graphical chat interface while preserving Pyto's native app capabilities. | Browser interface available and accepted |

## Audit coverage

| Audit area | Goals |
| --- | --- |
| Native harness chat API and lifetime | 02, 10; retired by 14 |
| Shared-process concurrency and survivors | 03 |
| Interactive app execution and scaffolds | 05 |
| Output noise, duplicate transcript and performance | 01, 10 |
| GUI approvals and console detection | 04 |
| Interrupted tool recovery | 08 |
| Program library, input forms and discoverability | 06, 07 |
| Project memory and context budgets | 09 |
| Prompt/platform alignment | 05, 11 |
| Shortcuts | 12 |
| Contract tests, real-device checks and usability measures | Per-goal acceptance criteria and 13 |

## Completion tracking

Treat implementation complete, automated verification complete and device verification complete as separate facts. Do not mark a goal fully verified when its required native checks are unavailable. Record results and any remaining device checks beside each goal or in a linked result note.

Implementation work for Goals 01–12 and the desktop acceptance pack for Goal 13 are included in the published v1.0.17 source. The goal-specific implementation notes and manual procedures remain in each linked goal file. The device report below records what the automated scripts verified; it does not promote manual/native checklist items to PASS.

Goal 14 is implemented for v1.0.20: the native harness chat window and its installer launch option are removed, while browser and terminal chat and PytoUI support for generated programs remain. The focused suite passed 149 tests, the full suite passed 880 tests, and the standard-library audit passed. No new device test was run for this change; see [Goal 14](goals/14-remove-native-pyto-ui.md).

## Current device revalidation — 6 October 2026

The v1.0.17 diagnostic report is from an iPhone running iOS 26.6.2 and Pyto 19.0.1 (438). The Objective-C bridge probe passed, the offline doctor passed (23 OK, 0 fixed, 0 needing attention), and all seven focused behavior rows passed:

- [x] **Automated on-device diagnostic milestone complete** — all seven focused behavior rows passed on the target Pyto device.

| Goal | Device-script result | Scope limit |
| --- | --- | --- |
| 03 — Managed program execution | PASS, 5 cases | Does not certify every UI-launched run or native-blocked operation. |
| 06 — Saved-program library | PASS, 2 cases | Does not cover the full chat library/restart interaction. |
| 07 — Program inputs | PASS, 3 cases | Does not open real Pyto file/folder pickers. |
| 08 — Interruption recovery | PASS, 2 simulated cases | Does not force-quit Pyto or establish an unknown native side effect. |
| 09 — Project context | PASS, 2 cases | Does not cover long-history UI behavior or every restart path. |
| 11 — Objective-C recipes | PASS | Read-only Foundation/UIKit recipe only; does not verify agent workflow, permissions or entitlements. |
| 12 — Shortcuts saved-run contract | PASS, 6 cases | Does not launch the Shortcuts app or verify a real Run Script handoff. |

The report's manual checklist still marks all 13 entries `NOT RUN` by design. Goals 01, 02, 04, 05, 10 and 13 have no focused behavior-script row. Native portions of Goals 03, 06–09, 11 and 12 also remain unverified as described above. Their acceptance work is recorded as deferred in the repository [TODO](../../../TODO.md).

Network checks (`network_reachable`, `api_auth`, `model_accepted`) were disabled in this run; `selftest` was skipped because it requires `--deep`; memory headroom is unavailable through this Pyto interpreter. These are coverage limits, not reported failures. The automated on-device diagnostic milestone has no failing checks or release blockers.
