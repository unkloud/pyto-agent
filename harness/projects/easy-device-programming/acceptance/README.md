# Novice workflow acceptance pack

This pack combines saved-program, organizer, interactive-app, permission-denial, and
interruption checks. Its UI and iOS bridge fixtures run on desktop; they check control flow,
not Pyto's native behavior.

## Run the acceptance pack

From the repository root:

```sh
python3 -m unittest \
  tests.test_novice_acceptance \
  tests.test_programs.TestProgramLibrary.test_edit_prompt_carries_selected_program_context_and_change \
  tests.test_loop.TestToolRoundTrip.test_reusable_batch_is_written_registered_and_verified \
  tests.test_loop.TestToolRoundTrip.test_interactive_app_uses_preview_and_reports_interaction_as_unverified \
  tests.test_previews.TestInteractivePreview.test_folder_organizer_preview_is_read_only_until_apply_button \
  tests.test_previews.TestInteractivePreview.test_scaffold_counter_form_and_mocked_network_run_through_preview \
  tests.test_session.TestInterruptedToolRecovery \
  tests.test_loop.TestResume.test_unknown_side_effect_is_not_replayed_and_history_is_provider_valid \
  tests.test_ios.TestPhotos.test_valid_image_with_a_bridge \
  -v
```

To print the two saved-notebook subprocess timings for this computer, run:

```sh
PYTO_HARNESS_ACCEPTANCE_TIMINGS=1 python3 -m unittest \
  tests.test_novice_acceptance.TestSavedClipboardNotebookJourney.test_save_reopen_run_twice_without_model_and_edit_selected_program -v
```

The timing includes Python and harness startup plus the saved program. It is not a user's
perceived time on an iPhone. The saved run is launched twice in fresh CLI processes with
all supported API-key environment variables removed; the notebook appends both entries,
the workspace library reloads its saved record, and the selected entry is supplied to the
edit prompt.

Sample measurement on Linux / CPython 3.12.3 on 6 October 2026: first fresh-process run
122.3 ms; second run 115.4 ms. These are one host's automated timings, not a release target.

Run the complete desktop suite with:

```sh
python3 -m unittest discover -s tests -t .
```

The complete suite passed **871 tests** in 91.465 seconds on 6 October 2026. The acceptance
command above passed **19 tests** in 0.485 seconds. Neither result exercises native iOS UI,
permissions, backgrounding, or Pyto's Objective-C bridge.

## Desktop evidence and user measures

| Journey | Desktop check | Manual edits / repeated explanations / rerun time |
|---|---|---|
| Clipboard notebook: build, save, reopen, run twice, prepare an edit | The saved entry runs in fresh processes without an API key; inputs preserve Unicode and embedded `=`; notes append; verification persists; the edit prompt carries the selected file. A fake `pasteboard` bridge checks clipboard fallback. | Automated fixture: no hand edits to source or paths. Saved reruns make zero model requests. Novice creation/edit repetitions are not measured. Per-process rerun times are emitted by the timing command above. |
| Selected-folder organizer: preview, apply, undo | Preview is read-only; guarded Apply moves the sample file; guarded Undo restores it. Conflict tests confirm no overwritten file and no partial undo. | Test paths are fixture inputs, not novice path edits. Model requests and human rerun time are not applicable to the app callbacks. Device picker friction remains unmeasured. |
| Persistent counter and form with a network action | Fake PytoUI taps increment and persist the counter, save/reload the form, and exercise a mocked HTTP response. | No human editing or explanations occur in the fixture. Real network, perceived rerun time, and native UI remain unmeasured. |
| Photo-library permission denial | A fake Photos bridge raises `PermissionError`; the adapter reports failure and required permission instead of claiming success. | Native permission prompt and user recovery are unmeasured. |
| Interrupted operation | Session recovery produces provider-valid history and does not replay a side effect with an unknown outcome. | Automated recovery only; force-quit timing and duplicate-effect behavior on Pyto remain unmeasured. |

Do not interpret desktop fixture counts as novice usability results. In a device session,
record one row per journey with: success/failure; manual code edits; manual path edits; how
many times the user had to repeat an explanation; elapsed time from reopening to a successful
rerun; device model; iOS version; and Pyto version. Use `not run` until a person has performed
that step.

## Pyto and iOS device checklist — pending

Record the device model, iOS version and Pyto version before starting.

- [ ] Ask the harness to build the clipboard notebook from a short copied sample. Record any
  source or path editing and repeated explanations. Save it, force-quit Pyto, reopen it, and
  run it twice; record both rerun times and confirm each entry appears in the note.
- [ ] Use `/edit ID <change>` on the notebook. Confirm the agent selects the saved entry and
  asks no one to locate or retype its path. Record whether the requested edit works.
- [ ] Choose a disposable folder containing a few files. Open the organizer and confirm the
  preview leaves all files in place. Apply the reviewed plan, then Undo; verify names and
  contents are restored. Record any file-provider limitations.
- [ ] Open the counter/form app, increment and save a note, close it, and open it again.
  Confirm both values persist. Tap Refresh and record the live network result or the exact
  error shown when offline.
- [ ] Ask the harness to save a harmless image using `save_photo`. First deny Photos access
  and confirm the tool reports failure without claiming a save. Then grant add-only access
  and verify the image appears in Photos. Record any settings recovery needed after denial.
- [ ] Interrupt a long-running or side-effecting task, force-quit Pyto, and resume its session.
  Confirm the harness reports an unknown result when appropriate and does not duplicate the
  action. Record what the user must do to reconcile the state.
- [ ] If validating Goal 12, invoke a reviewed saved batch program from Pyto's Shortcuts
  Run Script action and confirm its input/output behavior; do not infer background-service
  support from a successful foreground run.

No iPhone/iPad, Pyto runtime, or simulator is available in this environment. Every item above
remains unverified on-device; completion of the desktop pack is not release device acceptance.
