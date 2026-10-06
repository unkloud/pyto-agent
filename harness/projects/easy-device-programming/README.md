# Easy device programming — executable goals

Source: [audit](../../../AUDIT-2026-10-05.md). These prompts turn every audit finding into implementation work. No implementation is started by creating this folder.

## How to use

Give an agent one entire goal file as its task, or say “Implement the goal in `<path>`.” Each file includes scope, prerequisites, starting points and acceptance criteria. The numbers identify goals; the current rollout sequence is 01/02, 03/04, 08, 05, 06/07, 09–12, then the Goal 13 release gate. Device checks remain attached to each relevant goal, not only the final gate.

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

## Audit coverage

| Audit area | Goals |
| --- | --- |
| GUI API and lifetime | 02, 10 |
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

Goal 02 is implemented for v1.0.7 and has desktop contract/lifecycle coverage. Its real-device acceptance remains open; see the [implementation record and device checklist](goals/02-working-chat-gui.md).

Goal 03 is implemented in the local v1.0.9 candidate with desktop execution/concurrency coverage. Its Pyto device acceptance remains open; see the [implementation record and device checklist](goals/03-safe-program-execution.md).

Goal 04 is implemented in the same local candidate with GUI approvals, queued request handling and cancellation coverage. Its Pyto device acceptance remains open; see the [implementation record and device checklist](goals/04-in-app-approvals.md).

Goal 08 is implemented in the same local candidate with durable tool intent/results and no-replay recovery. Its Pyto interruption acceptance remains open; see the [implementation record and device checklist](goals/08-interruption-recovery.md).

Goal 05 is implemented in the local v1.0.9 candidate with a no-batch-timeout PytoUI preview path, callback tracking, a reusable app scaffold and desktop lifecycle coverage. Its native presentation and callback checks remain open; see the [implementation record and device checklist](goals/05-interactive-preview.md).

Goal 06 is implemented in the same local candidate: a versioned workspace library, persistent listing and registration, direct Run actions that make no model request, and Edit actions that pass the selected program context to the agent. The 811-test desktop suite covers persistence, malformed metadata, duplicate titles, file recovery, verification updates, CLI no-key execution and chat/terminal commands. Pyto restart and UI acceptance remain open; see the [implementation record and device checklist](goals/06-saved-program-library.md).

Goal 07 is implemented in the same local candidate with typed ephemeral input forms, terminal prompts, CLI assignments, picker-backed paths and a folder-organizer preview/apply example. Its desktop checks cover validation, cancellation, path handling, app/batch `main(inputs)` execution and collision-safe apply. Pyto picker, form and file-provider acceptance remains open; see the [implementation record and device checklist](goals/07-simple-program-inputs.md).

Goal 09 is implemented in the same local candidate with versioned per-program briefs, selected-program loading in `/edit`, and a separate provider-request byte budget that preserves the complete local session. The 845-test desktop suite covers brief migration/isolation, compaction persistence, stale file reporting, oversized tool results, current-request preservation and tool-call integrity. Pyto restart and long-history checks remain open; see the [implementation record and device checklist](goals/09-durable-project-context.md).

Goal 10 is implemented in the same candidate with a bounded 80,000-character chat display, coalesced TextView updates, paged session history and PytoUI autoresizing flags. Its 37 focused desktop tests cover batching, final flush, close safety and History navigation. Native keyboard, rotation and small-screen checks remain open; see the [implementation record and device checklist](goals/10-responsive-phone-chat.md).

Goal 11 is implemented in the local v1.0.10 candidate with platform-aware prompts, one-time/batch/app workflow separation, registration before final verification, and a documented read-only Objective-C recipe. Desktop coverage includes mocked conversations and Foundation/UIKit bridge fakes. Pyto prompt behavior, framework imports, and native interaction remain device checks; see the [Goal 11 record and checklist](goals/11-agent-workflow.md).

Goal 12 is implemented in the local v1.0.11 candidate with an opt-in, model-free Shortcuts entry point for saved batch programs. Desktop coverage checks explicit unattended consent, validated inputs, picker refusal and app-mode refusal. Pyto's Run Script argument mapping, output and scheduling behavior remain device checks; see the [Goal 12 record and checklist](goals/12-shortcut-handoff.md).

Goal 13's desktop acceptance pack is implemented in the local v1.0.11 candidate. It joins saved clipboard-notebook reruns, selected-program edit context, organizer preview/apply/undo, the persistent counter and form with mocked networking, permission denial, and interruption recovery. The reproducible command and result matrix are in [the acceptance pack](acceptance/README.md). The complete novice measures and all Pyto/iOS checks remain pending a real device session; see the [Goal 13 record and checklist](goals/13-usability-release-gate.md).

## Current revalidation — 6 October 2026

The focused desktop suite for Goals 03, 04, 05, 06, 07, 08, 09, 11 and 12 passed **233 tests** in 41.083 seconds on CPython 3.12.3 / Linux. It covers in-process run ownership and cleanup, chat approvals, interruption recovery, preview callbacks, saved-program persistence/edit context, validated inputs and model-free Shortcut runs. Goal 13's desktop pack separately passed **19 tests**; the full desktop suite passed **871 tests**.

This host has no Pyto, PytoUI, Foundation or UIKit modules, and the connected app inventory contains no iPhone/iPad runtime. Consequently the device checklists above remain pending; these desktop results do not verify native presentation, permission prompts, file-provider behavior, interruption on iOS, or Shortcuts execution on Pyto. The currently published GitHub release remains v1.0.8; later candidates are not published.
