"""Preview and explicitly apply a grouped move plan to one user-selected folder.

The harness launches this as an interactive app and calls ``main(inputs)`` after the user
fills its form. Importing the module or opening the preview never changes the selected folder.
"""

from __future__ import annotations

import os

import pyto_ui as ui
from folder_organizer_logic import OrganizerError, apply_plan, build_plan, undo_plan


def main(inputs):
    folder = inputs["folder"]
    group_by = inputs["group_by"]
    collection_name = inputs["collection_name"]
    max_files = inputs["max_files"]
    plan = build_plan(
        folder,
        group_by=group_by,
        collection_name=collection_name,
        max_files=max_files,
    )

    view = ui.View()
    view.title = "Organize folder"
    view.background_color = ui.COLOR_SYSTEM_BACKGROUND
    width = max(240, view.width)
    heading = ui.Label("Review these moves before applying them")
    heading.number_of_lines = 0
    heading.frame = (16, 16, width - 32, 46)
    view.add_subview(heading)

    location = ui.Label("Folder: {}".format(folder))
    location.number_of_lines = 2
    location.frame = (16, 64, width - 32, 48)
    view.add_subview(location)

    preview = ui.TextView()
    preview.editable = False
    preview.background_color = ui.COLOR_SECONDARY_SYSTEM_BACKGROUND
    preview.frame = (16, 120, width - 32, max(280, view.height - 250))
    rows = []
    for move in plan["moves"][:40]:
        rows.append("{}  →  {}/{}".format(
            move["source"], os.path.dirname(move["destination"]), os.path.basename(move["destination"])
        ))
    if len(plan["moves"]) > 40:
        rows.append("… and {} more".format(len(plan["moves"]) - 40))
    rows.extend("Skipped {} ({})".format(item["source"], item["reason"]) for item in plan["skipped"][:10])
    if plan["unplanned_count"]:
        rows.append("{} additional files are outside the selected limit.".format(plan["unplanned_count"]))
    preview.text = "\n".join(rows) if rows else "No files need moving."
    view.add_subview(preview)

    status = ui.Label("{} file(s) will move. Nothing has changed yet.".format(len(plan["moves"])))
    status.number_of_lines = 2
    status.frame = (16, view.height - 112, width - 32, 42)
    view.add_subview(status)

    apply_button = ui.Button(title="Apply these moves")
    apply_button.frame = (width - 178, view.height - 58, 162, 42)
    apply_button.enabled = bool(plan["moves"])
    applied_result = {"value": None}

    def apply_or_undo_reviewed_plan(_sender):
        try:
            if applied_result["value"] is None:
                result = apply_plan(plan, confirmed=True)
                applied_result["value"] = result
                status.text = "Moved {} file(s). Tap Undo to put them back.".format(result["moved"])
                apply_button.title = "Undo these moves"
            else:
                result = undo_plan(applied_result["value"], confirmed=True)
                applied_result["value"] = None
                status.text = "Restored {} file(s).".format(result["restored"])
                apply_button.title = "Apply these moves"
                apply_button.enabled = bool(plan["moves"])
        except OrganizerError as error:
            status.text = str(error)
            harness_preview.report_error(error, label="folder organizer")

    apply_button.action = harness_preview.guard(
        apply_or_undo_reviewed_plan, label="apply or undo folder organization"
    )
    view.add_subview(apply_button)

    cancel_button = ui.Button(title="Cancel")
    cancel_button.frame = (16, view.height - 58, 112, 42)
    cancel_button.action = harness_preview.guard(
        lambda _sender: harness_preview.close(view, reason="Cancelled"),
        label="cancel folder organization",
    )
    view.add_subview(cancel_button)

    harness_preview.present(view, ui)
