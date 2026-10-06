"""Reusable PytoUI app scaffold: counter, saved form, network work and close cleanup.

Launch this file with the harness's ``preview_program`` tool. The small ``update_count``,
``validate_form`` and ``decode_status`` functions are pure logic; they can be checked with
``run_program`` before opening the view. Network work uses a finite timeout and can be
replaced with a mocked response in tests.
"""

from __future__ import annotations

import json
import os
import threading
from urllib.request import urlopen

import pyto_ui as ui
from interactive_logic import decode_status, update_count, validate_form


STATUS_URL = "https://www.iana.org/domains/reserved"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "interactive_app_state.json")

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("saved state must be an object")
        return {"count": int(value.get("count", 0)), "note": str(value.get("note", ""))}
    except FileNotFoundError:
        return {"count": 0, "note": ""}


def save_state(value: dict) -> None:
    """Atomically replace this app's own small JSON state file."""
    temporary = STATE_PATH + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, STATE_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


state = load_state()
view = ui.View()
view.title = "Interactive app scaffold"
view.background_color = ui.COLOR_SYSTEM_BACKGROUND
content_width = max(240, view.width - 32)

heading = ui.Label("Counter, form and network status")
heading.number_of_lines = 0
heading.frame = (16, 18, content_width, 42)
view.add_subview(heading)

counter_label = ui.Label("Count: {}".format(state["count"]))
counter_label.frame = (16, 68, content_width, 32)
view.add_subview(counter_label)

note_field = ui.TextField(text=state["note"], placeholder="A note to save")
note_field.frame = (16, 108, content_width, 42)
view.add_subview(note_field)

status_label = ui.Label("Network: tap Refresh to load the public IANA page")
status_label.number_of_lines = 0
status_label.frame = (16, 204, content_width, 76)
view.add_subview(status_label)

background_label = ui.Label("Background worker: ready")
background_label.frame = (16, 288, content_width, 30)
view.add_subview(background_label)

error_label = ui.Label("")
error_label.number_of_lines = 0
error_label.text_color = ui.COLOR_SYSTEM_RED
error_label.frame = (16, 324, content_width, 54)
view.add_subview(error_label)


def show_error(error: Exception) -> None:
    error_label.text = "Error: {}".format(error)


def on_increment(_sender) -> None:
    state["count"] = update_count(state["count"])
    save_state(state)
    counter_label.text = "Count: {}".format(state["count"])
    error_label.text = ""


def on_save(_sender) -> None:
    state["note"] = validate_form(note_field.text)
    save_state(state)
    status_label.text = "Saved note: {}".format(state["note"])
    error_label.text = ""


def fetch_status() -> None:
    try:
        with urlopen(STATUS_URL, timeout=8) as response:
            body = response.read(4096)
        status_label.text = "Network response: {}".format(decode_status(body))
        error_label.text = ""
    except Exception as exc:
        harness_preview.report_error(exc, label="network request")
        show_error(exc)


background_tasks = []


def on_refresh(_sender) -> None:
    task = threading.Thread(target=fetch_status, name="preview-network-fetch", daemon=True)
    background_tasks.append(task)
    task.start()


def background_status() -> None:
    ticks = 0
    while not harness_preview.stop_event.wait(1.0):
        ticks += 1
        background_label.text = "Background worker: alive ({})".format(ticks)


background_worker = threading.Thread(
    target=background_status, name="preview-background-status", daemon=True
)
background_worker.start()

increment_button = ui.Button(title="Add one")
button_width = (content_width - 16) / 3
increment_button.frame = (16, 156, button_width, 40)
increment_button.action = harness_preview.guard(on_increment, on_error=show_error, label="add one")
view.add_subview(increment_button)

save_button = ui.Button(title="Save note")
save_button.frame = (24 + button_width, 156, button_width, 40)
save_button.action = harness_preview.guard(on_save, on_error=show_error, label="save note")
view.add_subview(save_button)

refresh_button = ui.Button(title="Refresh")
refresh_button.frame = (32 + 2 * button_width, 156, button_width, 40)
refresh_button.action = harness_preview.guard(on_refresh, on_error=show_error, label="refresh")
view.add_subview(refresh_button)


def on_close(_sender) -> None:
    harness_preview.close(view, reason="Close button")


close_button = ui.Button(title="Close")
close_button.frame = (16, 388, button_width, 40)
close_button.action = harness_preview.guard(on_close, on_error=show_error, label="close preview")
view.add_subview(close_button)

harness_preview.present(view, ui)

# Pyto's show_view returns after dismissal. Signal all owned workers and allow them a
# short cooperative exit window; the harness retains its execution lease if any survive.
harness_preview.stop_event.set()
background_worker.join(timeout=1.0)
for task in background_tasks:
    task.join(timeout=1.0)
