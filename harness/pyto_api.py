"""Grounding for Pyto's own bundled modules: catalogue, on-device introspection, rendering.

The problem this solves: a model writing a program for Pyto has the standard library in its
weights but *not* Pyto's own API.  ``photos.save_image`` vs ``photos.save_photo``,
``pasteboard.set_string`` vs ``set_clipboard``, ``notifications.send_notification(Notification(...))``
vs ``notifications.send(title, body)`` -- a niche, version-specific framework is exactly where
guessing fails, and every miss costs a run on the user's phone.

Three layers, deliberately separate:

* :data:`CURATED` -- a hand-written reference, every name traced to a primary source (the
  official docs at https://pyto.readthedocs.io/en/latest/library/index.html and Pyto's own
  ``Lib/*.py``).  It supplies the prose, the snippets and the caveats a ``dir()`` cannot.
* :func:`introspect` -- the **device** decides what exists and what the real signature is.
  The module is imported under a guard, public members are enumerated with ``dir()``, and
  ``inspect.signature`` is taken where possible.  Device truth overrides the catalogue; a
  member the catalogue knows but the device lacks is reported as missing, not hidden.
* :func:`render_reference` / :func:`render_doc` -- compact markdown for the model
  (capped) and the full ``PYTO_LIBS.md`` for the workspace.

:data:`NOT_AVAILABLE` is the other half of grounding: a model that keeps trying Reminders,
HealthKit or ``pip install`` of a C extension wastes turns.  Each entry carries the reason
and what to use instead.

Nothing here imports a Pyto module at import time: probing is lazy, guarded, and degrades to
"not available on this device" with the reason, so the module is fully usable (and testable)
off-device.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import inspect
import json
import os
import re
import types
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Bumped whenever the catalogue below changes in a way that should refresh ``PYTO_LIBS.md``.
CATALOGUE_VERSION = 1

#: Key this module owns inside the doctor's ``capabilities.json``.  The doctor writes
#: ``version``/``created_at``/``platform``/``python``/``signatures``; this module only ever
#: touches its own key, so the two can share one file without either breaking the other.
CACHE_KEY = "pyto_api"

#: Sentinel module names that are real on some Pyto versions and gone on others.  Ordered
#: so the reference and the prompt always list them the same way.
_MODULE_ORDER = (
    "pyto_ui",
    "pasteboard",
    "photos",
    "notifications",
    "background",
    "calendar_events",
    "file_system",
    "sharing",
    "share",
    "xcallback",
    "apps",
    "widgets",
    "watch",
    "sound",
    "music",
    "speech",
    "location",
    "motion",
    "multipeer",
    "userkeys",
    "console",
    "mainthread",
    "pyto",
    "usernotification",
    "_extensionsimporter",
)

#: Members whose *name* is confirmed by Pyto's source, with the prose from the same place.
#: ``verified`` is per member and per module: ``True`` means the name and the signature were
#: read out of a primary source (the docs page or ``Lib/<module>.py``), ``False`` means it is
#: a compatibility guess that only the device can settle.
CURATED: Dict[str, Dict[str, Any]] = {
    # ----------------------------------------------------------------------------------
    "pyto_ui": {
        "purpose": "Build a real UIKit window from a script: labels, buttons, text fields, "
        "tables, stacks and navigation, then present it with show_view().",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/pyto_ui.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/pyto_ui.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/pyto_ui.py",
        ],
        "caveats": [
            "show_view() blocks the script until the user closes the view; use "
            "show_view_without_waiting() only when you really want the script to carry on "
            "under the UI.",
            "A script that shows UI must keep the interpreter alive; the view closes when "
            "the script reports an uncaught exception.",
            "Appearance follows the system light/dark mode: prefer a Color from the "
            "COLOR_* constants over hard-coded RGB.",
        ],
        "snippet": (
            "import pyto_ui as ui\n"
            "\n"
            "def pressed(sender):\n"
            "    sender.superview.close()\n"
            "\n"
            "view = ui.View()\n"
            "view.title = \"Hello\"\n"
            "view.background_color = ui.COLOR_SYSTEM_BACKGROUND\n"
            "label = ui.Label(\"Tap the button\")\n"
            "label.size = (200, 32)\n"
            "label.center = (view.width / 2, 80)\n"
            "view.add_subview(label)\n"
            "button = ui.Button(title=\"Close\")\n"
            "button.size = (120, 44)\n"
            "button.center = (view.width / 2, 160)\n"
            "button.action = pressed\n"
            "view.add_subview(button)\n"
            "ui.show_view(view, ui.PRESENTATION_MODE_SHEET)\n"
            "print(\"closed\")\n"
        ),
        "members": [
            {
                "name": "View",
                "signature": "View()",
                "kind": "class",
                "description": "A rectangular container; every other view is added to one with add_subview().",
                "verified": True,
                "caveats": ["Set .size and .center before adding subviews, or lay out with .flex."],
            },
            {
                "name": "Label",
                "signature": "Label(text='')",
                "kind": "class",
                "description": "Non-editable text; set .text, .text_color, .font, .number_of_lines (0 shows everything).",
                "verified": True,
            },
            {
                "name": "Button",
                "signature": "Button(type='SYSTEM', title='', image=None)",
                "kind": "class",
                "description": "A tappable button; assign a function taking the button to .action.",
                "verified": True,
            },
            {
                "name": "TextField",
                "signature": "TextField(text='', placeholder=None)",
                "kind": "class",
                "description": "Single-line text entry; read .text plus the .did_end_editing callback.",
                "verified": True,
            },
            {
                "name": "TextView",
                "signature": "TextView(text='')",
                "kind": "class",
                "description": "Multiline editable text area; .text, .editable, .did_change.",
                "verified": True,
            },
            {
                "name": "ImageView",
                "signature": "ImageView(image=None, symbol_name=None, url=None)",
                "kind": "class",
                "description": "Displays a PIL image, an SF Symbol name or a URL; .load_from_url(url).",
                "verified": True,
            },
            {
                "name": "StackView",
                "signature": "StackView()",
                "kind": "class",
                "description": "Arranges children in a line; HorizontalStackView/VerticalStackView are the concrete subclasses.",
                "verified": True,
            },
            {
                "name": "VerticalStackView",
                "signature": "VerticalStackView()",
                "kind": "class",
                "description": "A StackView that lays its children out top to bottom.",
                "verified": True,
            },
            {
                "name": "HorizontalStackView",
                "signature": "HorizontalStackView()",
                "kind": "class",
                "description": "A StackView that lays its children out left to right.",
                "verified": True,
            },
            {
                "name": "ScrollView",
                "signature": "ScrollView()",
                "kind": "class",
                "description": "Scrollable container; set .content_width/.content_height, add to .content_view via add_subview().",
                "verified": True,
            },
            {
                "name": "TableView",
                "signature": "TableView(style=TableViewStyle.INSET_GROUPED, sections=[])",
                "kind": "class",
                "description": "A list of sections of cells; .set_cells(cells) and .did_select_cell(section, row).",
                "verified": True,
            },
            {
                "name": "TableViewCell",
                "signature": "TableViewCell(style=TableViewCellStyle.SUBTITLE, text=None, detail=None, image=None)",
                "kind": "class",
                "description": "One row; .text_label, .detail_text_label, .accessory_type, .removable.",
                "verified": True,
            },
            {
                "name": "NavigationView",
                "signature": "NavigationView(root_view=None)",
                "kind": "class",
                "description": "A stack of views with a title bar; .push(view), .pop(), .pop_to_root().",
                "verified": True,
            },
            {
                "name": "SegmentedControl",
                "signature": "SegmentedControl(segments=[])",
                "kind": "class",
                "description": "A row of exclusive buttons; .segments (list of titles), .selected_segment.",
                "verified": True,
            },
            {
                "name": "Slider",
                "signature": "Slider(value=0.5)",
                "kind": "class",
                "description": "Continuous value picker; .value, .minimum_value, .maximum_value.",
                "verified": True,
            },
            {
                "name": "Switch",
                "signature": "Switch(on=False)",
                "kind": "class",
                "description": "On/off control; .on and .action.",
                "verified": True,
            },
            {
                "name": "Stepper",
                "signature": "Stepper(minimum_value=0, maximum_value=100)",
                "kind": "class",
                "description": "Plus/minus control; .value, .step_value.",
                "verified": True,
            },
            {
                "name": "ButtonItem",
                "signature": "ButtonItem(title=None, image=None, system_item=None, style='PLAIN')",
                "kind": "class",
                "description": "A bar button for a view's .left_button_items / .right_button_items.",
                "verified": True,
            },
            {
                "name": "GestureRecognizer",
                "signature": "GestureRecognizer(type, action=None)",
                "kind": "class",
                "description": "Tap/long-press/swipe recogniser added with view.add_gesture_recognizer().",
                "verified": True,
            },
            {
                "name": "Color",
                "signature": "Color.rgb(red, green, blue, alpha=1)",
                "kind": "classmethod",
                "description": "A colour from components; Color.dynamic(light, dark) for light/dark pairs.",
                "verified": True,
            },
            {
                "name": "Font",
                "signature": "Font.system_font_of_size(size)",
                "kind": "classmethod",
                "description": "A font; also Font.bold_system_font_of_size(size), Font(name, size).",
                "verified": True,
            },
            {
                "name": "show_view",
                "signature": "show_view(view, mode=None)",
                "kind": "function",
                "description": "Presents a view and blocks until it is closed.",
                "verified": True,
            },
            {
                "name": "show_view_without_waiting",
                "signature": "show_view_without_waiting(view, mode)",
                "kind": "function",
                "description": "Presents a view without blocking the script.",
                "verified": True,
            },
            {
                "name": "image_with_system_name",
                "signature": "image_with_system_name(name)",
                "kind": "function",
                "description": "An SF Symbol UIImage for a name such as 'square.and.arrow.up'.",
                "verified": True,
            },
            {
                "name": "pick_color",
                "signature": "pick_color()",
                "kind": "function",
                "description": "Shows the system colour picker and returns the chosen Color.",
                "verified": True,
            },
            {
                "name": "pick_font",
                "signature": "pick_font(size=None)",
                "kind": "function",
                "description": "Shows the system font picker and returns the chosen Font.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "pasteboard": {
        "purpose": "Read and write the system clipboard (text, images, URLs, file providers).",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/pasteboard.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/pasteboard.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/pasteboard.py",
        ],
        "caveats": [
            "The clipboard is only readable while Pyto is in the foreground; iOS blocks "
            "background access to it, so a clipboard watcher is impossible.",
            "Reading the clipboard shows the iOS 'pasted from' banner; that is expected.",
            "Passing None to set_string/set_image clears that part of the clipboard.",
        ],
        "snippet": (
            "import pasteboard\n"
            "\n"
            "text = pasteboard.string()\n"
            "if text is None:\n"
            "    print(\"the clipboard has no text\")\n"
            "else:\n"
            "    pasteboard.set_string(text.upper())\n"
            "    print(\"upper-cased {} characters\".format(len(text)))\n"
        ),
        "members": [
            {
                "name": "string",
                "signature": "string() -> str",
                "kind": "function",
                "description": "The clipboard's text, or None when there is none.",
                "verified": True,
            },
            {
                "name": "strings",
                "signature": "strings() -> List[str]",
                "kind": "function",
                "description": "Every string on the clipboard.",
                "verified": True,
            },
            {
                "name": "set_string",
                "signature": "set_string(text)",
                "kind": "function",
                "description": "Copies a string or a list of strings; None clears the text.",
                "verified": True,
            },
            {
                "name": "image",
                "signature": "image() -> PIL.Image.Image",
                "kind": "function",
                "description": "The clipboard's image as a PIL image, or None.",
                "verified": True,
            },
            {
                "name": "set_image",
                "signature": "set_image(image)",
                "kind": "function",
                "description": "Copies a PIL image or a list of them; None clears the images.",
                "verified": True,
            },
            {
                "name": "url",
                "signature": "url() -> str",
                "kind": "function",
                "description": "The clipboard's URL as a string, or None.",
                "verified": True,
            },
            {
                "name": "set_url",
                "signature": "set_url(url)",
                "kind": "function",
                "description": "Copies a URL string or a list of them.",
                "verified": True,
            },
            {
                "name": "item_provider",
                "signature": "item_provider() -> ItemProvider",
                "kind": "function",
                "description": "The clipboard as an ItemProvider, so files and rich types can be loaded.",
                "verified": True,
            },
            {
                "name": "shortcuts_attachments",
                "signature": "shortcuts_attachments() -> List[ItemProvider]",
                "kind": "function",
                "description": "Files passed to the Shortcuts 'Attachments' parameter, when running from Shortcuts.",
                "verified": True,
            },
            {
                "name": "ItemProvider",
                "signature": "ItemProvider(foundation_item_provider)",
                "kind": "class",
                "description": "A clipboard/Shortcuts item; .get_type_identifiers(), .data(uti), "
                ".get_file_path(), .open() as a context manager, .get_suggested_name().",
                "verified": True,
            },
            {
                "name": "set_strings",
                "signature": "set_strings(array)",
                "kind": "function",
                "description": "Deprecated alias of set_string(list); emits a DeprecationWarning.",
                "verified": True,
            },
            {
                "name": "set_images",
                "signature": "set_images(array)",
                "kind": "function",
                "description": "Deprecated alias of set_image(list) for several images; emits a DeprecationWarning.",
                "verified": True,
                "caveats": ["Prefer set_image([img1, img2]); set_images() is deprecated."],
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "photos": {
        "purpose": "Pick or take a photo and save an image back to the photo library.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/photos.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/photos.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/photos.py",
        ],
        "caveats": [
            "Images are PIL (Pillow) images; Pyto bundles Pillow, so `from PIL import Image` works.",
            "save_image() writes to the library: iOS asks for the Photos add permission the "
            "first time, and the call fails if it was refused.",
            "pick_photo() and take_photo() present UI and block until the user finishes; they "
            "return None when the user cancels.",
        ],
        "snippet": (
            "import photos\n"
            "from PIL import Image\n"
            "\n"
            "image = photos.pick_photo()\n"
            "if image is None:\n"
            "    print(\"cancelled\")\n"
            "else:\n"
            "    small = image.resize((image.width // 2, image.height // 2))\n"
            "    photos.save_image(small)\n"
            "    print(\"saved a half-size copy\")\n"
        ),
        "members": [
            {
                "name": "pick_photo",
                "signature": "pick_photo() -> PIL.Image.Image",
                "kind": "function",
                "description": "Pick a photo from the library; returns a PIL image or None.",
                "verified": True,
            },
            {
                "name": "take_photo",
                "signature": "take_photo() -> PIL.Image.Image",
                "kind": "function",
                "description": "Take a photo with the camera; returns a PIL image or None.",
                "verified": True,
            },
            {
                "name": "save_image",
                "signature": "save_image(image)",
                "kind": "function",
                "description": "Save a PIL image to the photo library (asks for permission once).",
                "verified": True,
                "caveats": ["There is no save_photo(); the name is save_image."],
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "notifications": {
        "purpose": "Post or schedule local notifications, including the ones a background task delivers.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/notifications.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/notifications.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/notifications.py",
        ],
        "caveats": [
            "The API is Notification-object based: build notifications.Notification(message=...) "
            "and pass it to send_notification()/schedule_notification(). There is no "
            "notifications.send(title, body).",
            "iOS asks for notification permission on the first post; without it nothing appears.",
            "schedule_notification() takes three positional arguments: the notification, a "
            "delay in seconds and a repeat flag.",
        ],
        "snippet": (
            "import notifications as nc\n"
            "\n"
            "note = nc.Notification()\n"
            "note.message = \"The digest is ready\"\n"
            "note.url = \"pyto://\"\n"
            "nc.send_notification(note)\n"
            "print(\"notification posted\")\n"
        ),
        "members": [
            {
                "name": "Notification",
                "signature": "Notification(message=None, url=None, actions=None)",
                "kind": "class",
                "description": "A notification; set .message, .url (opened when tapped) and .actions "
                "(dict of action name -> URL).",
                "verified": True,
            },
            {
                "name": "send_notification",
                "signature": "send_notification(notification)",
                "kind": "function",
                "description": "Posts a Notification now.",
                "verified": True,
            },
            {
                "name": "schedule_notification",
                "signature": "schedule_notification(notification, delay, repeat)",
                "kind": "function",
                "description": "Delivers a Notification after `delay` seconds, repeating when `repeat` is true.",
                "verified": True,
            },
            {
                "name": "get_pending_notifications",
                "signature": "get_pending_notifications() -> List[Notification]",
                "kind": "function",
                "description": "The scheduled notifications; they cannot be edited after scheduling.",
                "verified": True,
            },
            {
                "name": "cancel_notification",
                "signature": "cancel_notification(notification)",
                "kind": "function",
                "description": "Cancels one pending Notification previously returned by get_pending_notifications().",
                "verified": True,
            },
            {
                "name": "cancel_all",
                "signature": "cancel_all()",
                "kind": "function",
                "description": "Cancels every pending notification.",
                "verified": True,
            },
            {
                "name": "remove_delivered_notifications",
                "signature": "remove_delivered_notifications()",
                "kind": "function",
                "description": "Clears already-delivered notifications from Notification Center.",
                "verified": True,
            },
            {
                "name": "UNUserNotificationCenter",
                "signature": "UNUserNotificationCenter",
                "kind": "constant",
                "description": "The UserNotifications framework class, re-exported from this module (may be None).",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "background": {
        "purpose": "Keep a script alive after the app leaves the foreground, and register the "
        "script for iOS background fetches.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/background.html",
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py",
            "https://pyto.readthedocs.io/en/latest/library/background.html",
        ],
        "caveats": [
            "BackgroundTask keeps Pyto alive by playing (silent) audio; Apple treats indefinite "
            "background audio as a grey area, so do not promise the user it will survive.",
            "iOS can still kill the app at any moment; a task should checkpoint its state to a file.",
            "request_background_fetch() asks iOS to re-run the script a few times a day; the OS "
            "decides when, the script has ~30 seconds, and it is unreliable by design.",
            "There is no daemon and no cron: this is the only 'run later on a schedule' hook.",
        ],
        "snippet": (
            "import background as bg\n"
            "\n"
            "with bg.BackgroundTask() as task:\n"
            "    for index in range(5):\n"
            "        print(\"{}s alive\".format(task.execution_time()))\n"
            "        task.wait(1)\n"
            "print(\"task finished\")\n"
        ),
        "members": [
            {
                "name": "BackgroundTask",
                "signature": "BackgroundTask(audio_path=None, id=None)",
                "kind": "class",
                "description": "A task that keeps the app running; usable as a context manager. "
                ".start(), .stop(), .wait(seconds), .execution_time().",
                "verified": True,
                "caveats": [
                    "The constructor keyword is `id`, not `name`; passing the same id again stops "
                    "the older task, which is what a Shortcuts automation wants.",
                ],
            },
            {
                "name": "request_background_fetch",
                "signature": "request_background_fetch()",
                "kind": "function",
                "description": "Registers the calling script with iOS Background Fetch (roughly a few "
                "runs a day, each under 30 seconds).",
                "verified": True,
            },
            {
                "name": "TaskExit",
                "signature": "TaskExit",
                "kind": "class",
                "description": "Exception raised inside a task when it is stopped; catch it to clean up.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "calendar_events": {
        "purpose": "Read and write Calendar events through EventKit.",
        "verified": True,
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/calendar_events.py",
        ],
        "caveats": [
            "Calendar access needs the user's permission; the first call prompts and the call "
            "raises RuntimeError when access is refused.",
            "save_event/get_events/remove_event raise RuntimeError with the EventKit error text "
            "instead of returning a status.",
            "get_events() takes datetime objects (not ISO strings) and only returns events in "
            "the given window.",
        ],
        "snippet": (
            "import calendar_events as cal\n"
            "from datetime import datetime, timedelta\n"
            "\n"
            "start = datetime.now()\n"
            "event = cal.Event()\n"
            "event.title = \"Tea\"\n"
            "event.start_date = start + timedelta(hours=1)\n"
            "event.end_date = start + timedelta(hours=2)\n"
            "cal.save_event(event)\n"
            "for item in cal.get_events(start, start + timedelta(days=1)):\n"
            "    print(item.title, item.start_date)\n"
        ),
        "members": [
            {
                "name": "Event",
                "signature": "Event()",
                "kind": "class",
                "description": "An event; set .title, .start_date, .end_date, .location, .notes, "
                ".url, .all_day, .alarms.",
                "verified": True,
            },
            {
                "name": "save_event",
                "signature": "save_event(event)",
                "kind": "function",
                "description": "Adds an Event to the default calendar; raises RuntimeError on refusal.",
                "verified": True,
            },
            {
                "name": "remove_event",
                "signature": "remove_event(event)",
                "kind": "function",
                "description": "Deletes an Event; raises RuntimeError on failure.",
                "verified": True,
            },
            {
                "name": "get_events",
                "signature": "get_events(start_date, end_date) -> List[Event]",
                "kind": "function",
                "description": "Events between two datetime objects; raises RuntimeError on failure.",
                "verified": True,
            },
            {
                "name": "Alarm",
                "signature": "Alarm(date=None, offset=None)",
                "kind": "class",
                "description": "A reminder on an event; attach with event.add_alarm(alarm).",
                "verified": True,
            },
            {
                "name": "RecurrenceRule",
                "signature": "RecurrenceRule(frequency, interval, end)",
                "kind": "class",
                "description": "A repeat rule for an event; assign to event.recurrence_rule.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "file_system": {
        "purpose": "Import and export files with the iOS document picker, share them, preview "
        "them with Quick Look, and keep bookmarks to folders outside the sandbox.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/external.html",
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/file_system.py",
            "https://pyto.readthedocs.io/en/latest/external.html",
        ],
        "caveats": [
            "Every function here presents UI and blocks until the user finishes with it.",
            "The iOS sandbox still applies: a file outside the app container is only reachable "
            "after the user picks it, or through a FileBookmark/FolderBookmark kept from a "
            "previous pick.",
            "FilePickerCancellation is raised when the user cancels a picker.",
        ],
        "snippet": (
            "import file_system as fs\n"
            "\n"
            "paths = fs.pick_directory()\n"
            "if paths:\n"
            "    print(\"picked {}\".format(paths))\n"
            "fs.share_text(\"hello from Pyto\")\n"
        ),
        "members": [
            {
                "name": "import_file",
                "signature": "import_file(multiple_selection=False, file_extension=None, mime_type=None, type_identifier=None)",
                "kind": "function",
                "description": "Shows the document picker and returns the chosen file paths.",
                "verified": True,
            },
            {
                "name": "pick_directory",
                "signature": "pick_directory()",
                "kind": "function",
                "description": "Shows the folder picker and returns the chosen directory path(s).",
                "verified": True,
            },
            {
                "name": "open_directory",
                "signature": "open_directory()",
                "kind": "function",
                "description": "Context manager that temporarily changes the working directory to a picked folder.",
                "verified": True,
            },
            {
                "name": "save_as",
                "signature": "save_as(path)",
                "kind": "function",
                "description": "Shows the export sheet and writes a copy of `path` where the user chooses.",
                "verified": True,
            },
            {
                "name": "share_text",
                "signature": "share_text(*text)",
                "kind": "function",
                "description": "Opens the share sheet with one or more strings (one argument per string).",
                "verified": True,
            },
            {
                "name": "share_files",
                "signature": "share_files(*path)",
                "kind": "function",
                "description": "Opens the share sheet with one or more file paths.",
                "verified": True,
            },
            {
                "name": "quick_look",
                "signature": "quick_look(*path)",
                "kind": "function",
                "description": "Previews one or more files without blocking.",
                "verified": True,
            },
            {
                "name": "FileBookmark",
                "signature": "FileBookmark(name=None, path=None)",
                "kind": "class",
                "description": "A remembered file path with lasting read/write access; the first "
                "use shows a file picker, later runs reuse it. Read .path, call .delete_from_disk().",
                "verified": True,
                "caveats": [
                    "Pyto warns that the bookmarks API is deprecated in favour of file_system, "
                    "which re-exports these two classes.",
                ],
            },
            {
                "name": "FolderBookmark",
                "signature": "FolderBookmark(name=None, path=None)",
                "kind": "class",
                "description": "A remembered folder with lasting access; Pyto's own clipboard sample "
                "uses FolderBookmark(\"clipboard_history\").path.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "sharing": {
        "purpose": "Legacy sharing module: kept so old scripts import, but every public "
        "function is deprecated in favour of file_system or webbrowser.",
        "verified": True,
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/sharing.py",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/_sharing.py",
        ],
        "caveats": [
            "Pyto itself emits DeprecationWarning for each of these. Prefer "
            "file_system.share_text/share_files, file_system.quick_look and "
            "file_system.import_file, or webbrowser.open().",
        ],
        "snippet": (
            "import file_system as fs  # not `sharing`: share_items() is deprecated\n"
            "\n"
            "fs.share_text(\"the modern way to share text\")\n"
        ),
        "members": [
            {
                "name": "share_items",
                "signature": "share_items(items)",
                "kind": "function",
                "description": "Deprecated. Opens the share sheet; use file_system.share_text/share_files.",
                "verified": True,
            },
            {
                "name": "quick_look",
                "signature": "quick_look(path, remove_previous=False)",
                "kind": "function",
                "description": "Deprecated. Previews a file; use file_system.quick_look.",
                "verified": True,
            },
            {
                "name": "pick_documents",
                "signature": "pick_documents(filePicker)",
                "kind": "function",
                "description": "Deprecated. Picks files with a FilePicker; use file_system.import_file.",
                "verified": True,
            },
            {
                "name": "picked_files",
                "signature": "picked_files()",
                "kind": "function",
                "description": "Deprecated. Paths from the last picker run; use file_system.import_file.",
                "verified": True,
            },
            {
                "name": "open_url",
                "signature": "open_url(url)",
                "kind": "function",
                "description": "Deprecated. Opens a URL; use webbrowser.open().",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "share": {
        "purpose": "Legacy module name some Pyto builds exposed for the share sheet. Not part "
        "of Pyto's current Lib/ or documentation.",
        "verified": False,
        "sources": [
            "https://github.com/ColdGrub1384/Pyto (Lib/ and docs/library: no share.py)",
        ],
        "caveats": [
            "The harness probes this name because older Pyto builds had it, but it could not be "
            "confirmed in the current source or docs: treat it as unavailable unless "
            "pyto_api reports it on this device.",
            "Use file_system.share_text/share_files for sharing.",
        ],
        "snippet": (
            "import file_system as fs\n"
            "\n"
            "fs.share_text(\"share me\")\n"
        ),
        "members": [
            {
                "name": "open",
                "signature": "open(items)",
                "kind": "function",
                "description": "Unverified legacy entry point that opened the share sheet with an "
                "item or a list of items.",
                "verified": False,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "xcallback": {
        "purpose": "Call another app's x-callback URL and wait for its result, including "
        "Shortcuts' own x-callback interface.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/xcallback.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/xcallback.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/xcallback.py",
        ],
        "caveats": [
            "Blocks until the other app answers: the user must come back to Pyto.",
            "Raises RuntimeError when the other app reports an error and SystemExit when the "
            "request is cancelled, so wrap it in try/except.",
        ],
        "snippet": (
            "import xcallback\n"
            "from urllib.parse import quote\n"
            "\n"
            "url = \"shortcuts://x-callback-url/run-shortcut?name={}&input=text&text={}\".format(\n"
            "    quote(\"My Shortcut\"), quote(\"hello\")\n"
            ")\n"
            "try:\n"
            "    print(xcallback.open_url(url))\n"
            "except RuntimeError as error:\n"
            "    print(\"failed: {}\".format(error))\n"
            "except SystemExit:\n"
            "    print(\"cancelled\")\n"
        ),
        "members": [
            {
                "name": "open_url",
                "signature": "open_url(url) -> str",
                "kind": "function",
                "description": "Opens an x-callback URL and returns the result the other app sent back.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "apps": {
        "purpose": "Drive other apps through their URL schemes: Shortcuts, Bear, Drafts, "
        "Things, Day One and many more.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/apps.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/apps.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/apps.py",
        ],
        "caveats": [
            "Each app is a class in the module with instance methods and no module-level "
            "instance, so instantiate it: apps.Shortcuts().run_a_shortcut(...). Pyto's docs "
            "list `class apps.Shortcuts` but show no call example; the instantiation is read "
            "from Lib/apps.py.",
            "These calls leave Pyto and use x-callback URLs under the hood, so they block and "
            "raise RuntimeError like xcallback.open_url().",
            "The other app must be installed, or the URL scheme does nothing.",
        ],
        "snippet": (
            "import apps\n"
            "\n"
            "shortcuts = apps.Shortcuts()\n"
            "result = shortcuts.run_a_shortcut(\"My Shortcut\", text=\"hello\")\n"
            "print(result)\n"
        ),
        "members": [
            {
                "name": "Shortcuts",
                "signature": "Shortcuts()",
                "kind": "class",
                "description": "Actions for the Shortcuts app; instantiate it before calling a method.",
                "verified": True,
            },
            {
                "name": "Shortcuts.run_a_shortcut",
                "signature": "run_a_shortcut(name, input=None, text=None) -> str",
                "kind": "method",
                "description": "Runs a shortcut in the user's collection and returns its output.",
                "verified": True,
            },
            {
                "name": "Shortcuts.open_a_shortcut",
                "signature": "open_a_shortcut(name)",
                "kind": "method",
                "description": "Opens the Shortcuts app at one shortcut.",
                "verified": True,
            },
            {
                "name": "Shortcuts.import_a_shortcut",
                "signature": "import_a_shortcut(url, name=None, silent=None) -> str",
                "kind": "method",
                "description": "Imports a .shortcut file from a URL into the user's collection.",
                "verified": True,
            },
            {
                "name": "Shortcuts.open_gallery",
                "signature": "open_gallery()",
                "kind": "method",
                "description": "Opens the Gallery tab of the Shortcuts app.",
                "verified": True,
            },
            {
                "name": "Shortcuts.search_gallery",
                "signature": "search_gallery(query)",
                "kind": "method",
                "description": "Searches the Shortcuts gallery.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "widgets": {
        "purpose": "Draw a Home Screen widget: rows of text, symbols and images laid out in "
        "small/medium/large layouts.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/widgets.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/widgets.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/widgets.py",
        ],
        "caveats": [
            "A widget script only runs in the widget context: show_widget()/save_widget() from a "
            "normal console run does nothing useful on its own.",
            "iOS decides when a widget reloads; schedule_next_reload() is a request, not a promise.",
            "TimelineProvider subclasses supply future content; the widget extension runs the "
            "script again for each requested date.",
        ],
        "snippet": (
            "import widgets as wd\n"
            "\n"
            "layout = wd.WidgetLayout()\n"
            "layout.add_row([wd.Text(\"Hello from Pyto\")])\n"
            "\n"
            "widget = wd.Widget()\n"
            "widget.small_layout = layout\n"
            "wd.show_widget(widget)\n"
        ),
        "members": [
            {
                "name": "Widget",
                "signature": "Widget()",
                "kind": "class",
                "description": "A widget with .small_layout, .medium_layout and .large_layout.",
                "verified": True,
            },
            {
                "name": "WidgetLayout",
                "signature": "WidgetLayout()",
                "kind": "class",
                "description": "One size's layout; .add_row(list_of_elements), .add_vertical_spacer(), "
                ".set_background_color(colour).",
                "verified": True,
            },
            {
                "name": "Text",
                "signature": "Text(text, color=None, font=None, background_color=None, corner_radius=0, padding=None, link=None)",
                "kind": "class",
                "description": "A text element in a widget row.",
                "verified": True,
            },
            {
                "name": "Image",
                "signature": "Image(image=None, url=None, fill=False, background_color=None, corner_radius=0, padding=None, link=None)",
                "kind": "class",
                "description": "An image element, from a PIL image or a URL.",
                "verified": True,
            },
            {
                "name": "SystemSymbol",
                "signature": "SystemSymbol(symbol_name, color=None, font_size=None, background_color=None, corner_radius=0, padding=None, link=None)",
                "kind": "class",
                "description": "An SF Symbol element, e.g. SystemSymbol('star.fill').",
                "verified": True,
            },
            {
                "name": "DynamicDate",
                "signature": "DynamicDate(date, style=DATE_STYLE_DATE, color=None, font=None, background_color=None, corner_radius=0, padding=None, link=None)",
                "kind": "class",
                "description": "A date the system keeps up to date without a reload.",
                "verified": True,
            },
            {
                "name": "Spacer",
                "signature": "Spacer()",
                "kind": "class",
                "description": "Flexible horizontal space inside a row.",
                "verified": True,
            },
            {
                "name": "Padding",
                "signature": "Padding(top=0, bottom=0, left=0, right=0)",
                "kind": "class",
                "description": "Custom padding for a widget element.",
                "verified": True,
            },
            {
                "name": "Color",
                "signature": "Color.rgb(red, green, blue, alpha=1)",
                "kind": "classmethod",
                "description": "A widget colour; also Color.dynamic(light, dark).",
                "verified": True,
            },
            {
                "name": "Font",
                "signature": "Font.system_font_of_size(size)",
                "kind": "classmethod",
                "description": "A widget font; also Font.bold_system_font_of_size(size).",
                "verified": True,
            },
            {
                "name": "TimelineProvider",
                "signature": "TimelineProvider()",
                "kind": "class",
                "description": "Subclass it and implement .widget(date), .timeline() and .reload_time().",
                "verified": True,
            },
            {
                "name": "provide_timeline",
                "signature": "provide_timeline(provider)",
                "kind": "function",
                "description": "Hands a TimelineProvider to the widget extension.",
                "verified": True,
            },
            {
                "name": "show_widget",
                "signature": "show_widget(widget)",
                "kind": "function",
                "description": "Shows a Widget while the script runs (for testing a layout).",
                "verified": True,
            },
            {
                "name": "save_widget",
                "signature": "save_widget(widget, key)",
                "kind": "function",
                "description": "Saves a Widget under a key so the Home Screen widget can show it.",
                "verified": True,
            },
            {
                "name": "reload_widgets",
                "signature": "reload_widgets(names)",
                "kind": "function",
                "description": "Asks iOS to reload the widgets of the given script names.",
                "verified": True,
            },
            {
                "name": "schedule_next_reload",
                "signature": "schedule_next_reload(time)",
                "kind": "function",
                "description": "Requests the next widget reload at a time; iOS may ignore it.",
                "verified": True,
            },
            {
                "name": "save_snapshot",
                "signature": "save_snapshot(view, key)",
                "kind": "function",
                "description": "Saves a pyto_ui view snapshot that a widget can be configured to show.",
                "verified": True,
            },
            {
                "name": "delete_in_app_widget",
                "signature": "delete_in_app_widget(key)",
                "kind": "function",
                "description": "Deletes a saved in-app widget snapshot.",
                "verified": True,
            },
            {
                "name": "wait_for_internet_connection",
                "signature": "wait_for_internet_connection()",
                "kind": "function",
                "description": "Blocks until the device has a network connection.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "watch": {
        "purpose": "Send an interface and complications to the paired Apple Watch.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/watch.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/watch.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/watch.py",
        ],
        "caveats": [
            "Requires a paired Apple Watch with the Pyto watch app installed; nothing happens without one.",
            "Complications must be added to the watch face by the user after add_complications_provider().",
        ],
        "snippet": (
            "import watch\n"
            "\n"
            "watch.add_complications_provider(MyProvider())\n"
            "watch.reload_complications()\n"
        ),
        "members": [
            {
                "name": "ComplicationsProvider",
                "signature": "ComplicationsProvider()",
                "kind": "class",
                "description": "Subclass it and implement .name(), .complication(date) and .timeline(after_date, limit).",
                "verified": True,
            },
            {
                "name": "Complication",
                "signature": "Complication()",
                "kind": "class",
                "description": "The layouts for one complication; .add_row(row, background_color=None, corner_radius=0, link=None).",
                "verified": True,
            },
            {
                "name": "Progress",
                "signature": "Progress(value=0, circular=False, label=None, color=None, background_color=None, corner_radius=0, padding=None, link=None)",
                "kind": "class",
                "description": "A progress element for a complication or widget.",
                "verified": True,
            },
            {
                "name": "add_complications_provider",
                "signature": "add_complications_provider(provider)",
                "kind": "function",
                "description": "Registers a ComplicationsProvider so the user can add it to a watch face.",
                "verified": True,
            },
            {
                "name": "reload_complications",
                "signature": "reload_complications()",
                "kind": "function",
                "description": "Reloads the complications currently on the watch face.",
                "verified": True,
            },
            {
                "name": "make_interface",
                "signature": "make_interface()",
                "kind": "function",
                "description": "Creates the interface object sent to the watch (a widgets.WidgetLayout).",
                "verified": True,
            },
            {
                "name": "delete_interface",
                "signature": "delete_interface()",
                "kind": "function",
                "description": "Deletes the previously sent watch interface.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "sound": {
        "purpose": "Play short sounds and audio files, with a player class for long ones.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/sound.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/sound.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/sound.py",
        ],
        "caveats": [
            "play_file() is for sounds under 30 seconds; use AudioPlayer for anything longer.",
            "Audio goes to the current audio session; it can be interrupted by a call or by "
            "other apps, and iOS may stop it when Pyto is backgrounded.",
        ],
        "snippet": (
            "import sound\n"
            "\n"
            "sound.play_beep()\n"
            "sound.play_file(\"done.wav\")\n"
            "sound.play_system_sound(1000)\n"
        ),
        "members": [
            {
                "name": "play_file",
                "signature": "play_file(path)",
                "kind": "function",
                "description": "Plays an audio file; use only for sounds under 30 seconds.",
                "verified": True,
            },
            {
                "name": "play_beep",
                "signature": "play_beep()",
                "kind": "function",
                "description": "Plays a short beep.",
                "verified": True,
            },
            {
                "name": "play_system_sound",
                "signature": "play_system_sound(id)",
                "kind": "function",
                "description": "Plays an iOS system sound by its numeric ID.",
                "verified": True,
            },
            {
                "name": "AudioPlayer",
                "signature": "AudioPlayer(path)",
                "kind": "class",
                "description": "A player for long audio; .play(), .pause(), .stop(), .volume, .current_time.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "music": {
        "purpose": "Control the system music player and pick items from the user's media library.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/music.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/music.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/music.py",
        ],
        "caveats": [
            "The media library needs the user's Apple Music permission; without it pick_music() "
            "returns nothing and playback calls fail.",
            "Playback happens in the system music app, not in Pyto; Pyto only sends commands.",
        ],
        "snippet": (
            "import music\n"
            "\n"
            "print(music.playback_state())\n"
            "music.play()\n"
        ),
        "members": [
            {
                "name": "play",
                "signature": "play()",
                "kind": "function",
                "description": "Starts playback of the current item.",
                "verified": True,
            },
            {
                "name": "stop",
                "signature": "stop()",
                "kind": "function",
                "description": "Ends playback of the current item.",
                "verified": True,
            },
            {
                "name": "next",
                "signature": "next()",
                "kind": "function",
                "description": "Skips to the next item in the queue.",
                "verified": True,
            },
            {
                "name": "previous",
                "signature": "previous()",
                "kind": "function",
                "description": "Goes back to the previous item in the queue.",
                "verified": True,
            },
            {
                "name": "restart",
                "signature": "restart()",
                "kind": "function",
                "description": "Restarts the current item from the beginning.",
                "verified": True,
            },
            {
                "name": "playback_state",
                "signature": "playback_state()",
                "kind": "function",
                "description": "The current playback state (compare with the PLAYBACK_STATE_* constants).",
                "verified": True,
            },
            {
                "name": "now_playing_item",
                "signature": "now_playing_item()",
                "kind": "function",
                "description": "The media item that is playing, if any.",
                "verified": True,
            },
            {
                "name": "set_queue_with_items",
                "signature": "set_queue_with_items(items)",
                "kind": "function",
                "description": "Replaces the playback queue with a collection of media items.",
                "verified": True,
            },
            {
                "name": "pick_music",
                "signature": "pick_music()",
                "kind": "function",
                "description": "Shows the system music picker and returns what the user chose.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "speech": {
        "purpose": "Text to speech with the system voices.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/speech.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/speech.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/speech.py",
        ],
        "caveats": [
            "This is speech *synthesis* only. Pyto has no speech recognition API: there is no "
            "microphone-to-text, so a voice-note transcriber needs a Shortcut or another app.",
            "say() returns immediately; use wait() or is_speaking() before the script exits, or "
            "the audio is cut off.",
        ],
        "snippet": (
            "import speech\n"
            "\n"
            "speech.say(\"The digest is ready\", language=\"en-US\", rate=0.5)\n"
            "speech.wait()\n"
        ),
        "members": [
            {
                "name": "say",
                "signature": "say(text, language=None, rate=None)",
                "kind": "function",
                "description": "Speaks text; language like 'en-US', rate 0..1 (default 0.5).",
                "verified": True,
            },
            {
                "name": "is_speaking",
                "signature": "is_speaking() -> bool",
                "kind": "function",
                "description": "True while the device is speaking.",
                "verified": True,
            },
            {
                "name": "wait",
                "signature": "wait()",
                "kind": "function",
                "description": "Blocks until speech finishes.",
                "verified": True,
            },
            {
                "name": "get_available_languages",
                "signature": "get_available_languages() -> List[str]",
                "kind": "function",
                "description": "The language codes the system voices support.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "location": {
        "purpose": "Read the device's current coordinates.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/location.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/location.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/location.py",
        ],
        "caveats": [
            "iOS asks for location permission the first time; without it the reading is None.",
            "Call start_updating() before get_location(), and stop_updating() when done.",
            "The accuracy constants (LOCATION_ACCURACY_*) can be assigned to location.accuracy.",
        ],
        "snippet": (
            "import location\n"
            "\n"
            "location.start_updating()\n"
            "spot = location.get_location()\n"
            "location.stop_updating()\n"
            "if spot is None:\n"
            "    print(\"no location (permission?)\")\n"
            "else:\n"
            "    print(spot.latitude, spot.longitude)\n"
        ),
        "members": [
            {
                "name": "start_updating",
                "signature": "start_updating()",
                "kind": "function",
                "description": "Starts receiving location updates; call it before get_location().",
                "verified": True,
            },
            {
                "name": "stop_updating",
                "signature": "stop_updating()",
                "kind": "function",
                "description": "Stops receiving updates, saving battery.",
                "verified": True,
            },
            {
                "name": "get_location",
                "signature": "get_location() -> Location",
                "kind": "function",
                "description": "The current longitude, latitude and altitude as a named tuple.",
                "verified": True,
            },
            {
                "name": "Location",
                "signature": "Location(longitude, latitude, altitude)",
                "kind": "class",
                "description": "Named tuple with .longitude, .latitude and .altitude.",
                "verified": True,
            },
            {
                "name": "accuracy",
                "signature": "accuracy",
                "kind": "constant",
                "description": "Module-level accuracy in metres; assign one of the LOCATION_ACCURACY_* constants.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "motion": {
        "purpose": "Read the accelerometer, gyroscope and magnetometer.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/motion.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/motion.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/motion.py",
        ],
        "caveats": [
            "iOS may ask for Motion & Fitness access; the Pyto docs do not describe the prompt.",
            "Call start_updating() before reading a sensor and stop_updating() afterwards.",
            "Readings are tuples (x, y, z); attitude is (roll, pitch, yaw).",
        ],
        "snippet": (
            "import motion\n"
            "\n"
            "motion.start_updating()\n"
            "gravity = motion.get_gravity()\n"
            "motion.stop_updating()\n"
            "print(gravity.x, gravity.y, gravity.z)\n"
        ),
        "members": [
            {
                "name": "start_updating",
                "signature": "start_updating()",
                "kind": "function",
                "description": "Starts the sensors.",
                "verified": True,
            },
            {
                "name": "stop_updating",
                "signature": "stop_updating()",
                "kind": "function",
                "description": "Stops the sensors.",
                "verified": True,
            },
            {
                "name": "get_gravity",
                "signature": "get_gravity() -> Gravity",
                "kind": "function",
                "description": "Gravity vector as (x, y, z).",
                "verified": True,
            },
            {
                "name": "get_rotation",
                "signature": "get_rotation() -> Rotation",
                "kind": "function",
                "description": "Rotation rate as (x, y, z).",
                "verified": True,
            },
            {
                "name": "get_acceleration",
                "signature": "get_acceleration() -> Acceleration",
                "kind": "function",
                "description": "User acceleration as (x, y, z).",
                "verified": True,
            },
            {
                "name": "get_magnetic_field",
                "signature": "get_magnetic_field() -> MagneticField",
                "kind": "function",
                "description": "Magnetic field as (x, y, z).",
                "verified": True,
            },
            {
                "name": "get_attitude",
                "signature": "get_attitude() -> Attitude",
                "kind": "function",
                "description": "Attitude as (roll, pitch, yaw).",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "multipeer": {
        "purpose": "Trade short strings with other devices running Pyto, over peer-to-peer "
        "Wi-Fi/Bluetooth, without a network.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/multipeer.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/multipeer.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/multipeer.py",
        ],
        "caveats": [
            "The other device must be running Pyto with the same API in listen mode; this is "
            "not a general Bluetooth stack.",
            "send() takes a string; there is no file transfer API here.",
            "wait() blocks until something arrives, so give the script a deadline.",
        ],
        "snippet": (
            "import multipeer\n"
            "\n"
            "multipeer.connect()\n"
            "multipeer.send(\"hello from the other phone\")\n"
            "print(multipeer.wait())\n"
            "multipeer.disconnect()\n"
        ),
        "members": [
            {
                "name": "connect",
                "signature": "connect()",
                "kind": "function",
                "description": "Starts connecting to other devices running Pyto.",
                "verified": True,
            },
            {
                "name": "disconnect",
                "signature": "disconnect()",
                "kind": "function",
                "description": "Disconnects from every connected device.",
                "verified": True,
            },
            {
                "name": "send",
                "signature": "send(data)",
                "kind": "function",
                "description": "Sends a string to the connected devices.",
                "verified": True,
            },
            {
                "name": "get_data",
                "signature": "get_data() -> str",
                "kind": "function",
                "description": "Returns one received message, or None when there is nothing.",
                "verified": True,
            },
            {
                "name": "wait",
                "signature": "wait()",
                "kind": "function",
                "description": "Blocks until a message arrives and returns it.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "userkeys": {
        "purpose": "Small persistent key/value store shared between the app and its Today widget.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/userkeys.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/userkeys.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/userkeys.py",
        ],
        "caveats": [
            "NOT secure storage: it is a JSON dictionary on disk (and shared with a widget), so "
            "never put a password, token or API key in it.",
            "Values must be JSON-compatible: no dates, sets or custom objects.",
            "Note the argument order: set(value, key), not set(key, value).",
        ],
        "snippet": (
            "import userkeys\n"
            "\n"
            "userkeys.set(\"dark\", \"theme\")\n"
            "print(userkeys.get(\"theme\"))\n"
            "userkeys.delete(\"theme\")\n"
        ),
        "members": [
            {
                "name": "get",
                "signature": "get(key)",
                "kind": "function",
                "description": "Returns the JSON value stored under `key`, or None.",
                "verified": True,
            },
            {
                "name": "set",
                "signature": "set(value, key)",
                "kind": "function",
                "description": "Stores a JSON-compatible value under `key` (value comes first).",
                "verified": True,
            },
            {
                "name": "delete",
                "signature": "delete(key)",
                "kind": "function",
                "description": "Deletes the value stored under `key`.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "console": {
        "purpose": "Pyto's own console helpers: read a line of input, clear the console, and "
        "the display/excepthook the app installs.",
        "verified": True,
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/console.py",
        ],
        "caveats": [
            "Undocumented in Pyto's library reference and partly internal: prefer print() and "
            "the builtin input() unless you specifically need console.clear().",
            "console.print() writes to the Pyto console directly, not to your program's stdout.",
        ],
        "snippet": (
            "import console\n"
            "\n"
            "console.clear()\n"
            "name = console.input(\"Your name: \")\n"
            "print(\"hello {}\".format(name))\n"
        ),
        "members": [
            {
                "name": "input",
                "signature": "input(prompt=None, highlight=False, print_prompt=True, shell=False)",
                "kind": "function",
                "description": "Requests a line of input in the Pyto console.",
                "verified": True,
            },
            {
                "name": "clear",
                "signature": "clear()",
                "kind": "function",
                "description": "Clears the console output.",
                "verified": True,
            },
            {
                "name": "print",
                "signature": "print(*objects, sep=None, end=None)",
                "kind": "function",
                "description": "Prints to the Pyto console rather than to stdout; builtin print is usually right.",
                "verified": True,
            },
            {
                "name": "displayhook",
                "signature": "displayhook(_value)",
                "kind": "function",
                "description": "The REPL display hook Pyto installs; internal.",
                "verified": True,
            },
            {
                "name": "excepthook",
                "signature": "excepthook(exc, value, tb, limit=None)",
                "kind": "function",
                "description": "The exception hook Pyto installs; internal.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "mainthread": {
        "purpose": "Run code on the main thread, which is required before touching UIKit "
        "objects from a worker thread.",
        "verified": True,
        "doc": "https://pyto.readthedocs.io/en/latest/library/mainthread.html",
        "sources": [
            "https://pyto.readthedocs.io/en/latest/library/mainthread.html",
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/mainthread.py",
        ],
        "caveats": [
            "Only needed when you use threads; a plain script already runs on the main thread.",
            "run_sync() supports a return value, run_async() does not.",
        ],
        "snippet": (
            "import mainthread\n"
            "\n"
            "def update():\n"
            "    print(\"on the main thread\")\n"
            "\n"
            "mainthread.run_async(update)\n"
        ),
        "members": [
            {
                "name": "mainthread",
                "signature": "mainthread(func)",
                "kind": "function",
                "description": "Decorator that makes a function run synchronously on the main thread.",
                "verified": True,
            },
            {
                "name": "run_async",
                "signature": "run_async(code)",
                "kind": "function",
                "description": "Runs a function asynchronously on the main thread.",
                "verified": True,
            },
            {
                "name": "run_sync",
                "signature": "run_sync(code)",
                "kind": "function",
                "description": "Runs a function on the main thread and returns its result.",
                "verified": True,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "pyto": {
        "purpose": "The app's own helper classes (Python interpreter, file picker, alert, "
        "sharing/photo/music helpers). Internal or plugin use.",
        "verified": True,
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/pyto.py",
        ],
        "caveats": [
            "Pyto's own source says: 'This module is only for internal or plugin use.' Prefer "
            "the higher-level modules (pyto_ui, file_system, photos, ...).",
            "Each name is an Objective-C class from the app; the methods are not documented and "
            "may change between builds.",
        ],
        "snippet": (
            "# Prefer a high-level module; `pyto` is internal.\n"
            "import file_system as fs\n"
            "\n"
            "print(fs.pick_directory())\n"
        ),
        "members": [
            {
                "name": "Python",
                "signature": "Python",
                "kind": "class",
                "description": "The running Python interpreter's app-side class; internal.",
                "verified": False,
            },
            {
                "name": "FilePicker",
                "signature": "FilePicker",
                "kind": "class",
                "description": "The app's document picker class; use file_system instead.",
                "verified": False,
            },
            {
                "name": "PyAlert",
                "signature": "PyAlert",
                "kind": "class",
                "description": "The app's alert class; internal, undocumented.",
                "verified": False,
            },
            {
                "name": "PySharingHelper",
                "signature": "PySharingHelper",
                "kind": "class",
                "description": "The app's sharing helper; use file_system.share_text/share_files.",
                "verified": False,
            },
            {
                "name": "PyPhotosHelper",
                "signature": "PyPhotosHelper",
                "kind": "class",
                "description": "The app's photo helper; use photos.",
                "verified": False,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "usernotification": {
        "purpose": "Legacy lower-case name for the UserNotifications framework that some "
        "harness paths probe.",
        "verified": False,
        "sources": [
            "https://pyto.readthedocs.io/en/latest/Objective-C.html (framework is UserNotifications)",
        ],
        "caveats": [
            "Pyto's Objective-C docs list `UserNotifications` (capitalised) as an importable "
            "framework, not `usernotification`; the lower-case name could not be confirmed.",
            "For local notifications use the `notifications` module, which wraps this framework.",
        ],
        "snippet": (
            "import notifications as nc  # the supported path\n"
            "\n"
            "note = nc.Notification(message=\"hi\")\n"
            "nc.send_notification(note)\n"
        ),
        "members": [
            {
                "name": "UNUserNotificationCenter",
                "signature": "UNUserNotificationCenter",
                "kind": "class",
                "description": "The UserNotifications centre class, reachable as "
                "notifications.UNUserNotificationCenter; this module alias is unverified.",
                "verified": False,
            },
        ],
    },
    # ----------------------------------------------------------------------------------
    "_extensionsimporter": {
        "purpose": "Pyto's internal loader for the compiled C extensions bundled with the app.",
        "verified": True,
        "sources": [
            "https://github.com/ColdGrub1384/Pyto/blob/main/Lib/extensionsimporter.py",
        ],
        "caveats": [
            "A private C module used by Pyto's own import machinery; it is not a user API and "
            "scripts should not import it. It cannot load extensions you compile yourself.",
            "Its presence is what lets the bundled C extensions (numpy, pandas, OpenCV) import.",
        ],
        "snippet": (
            "# Do not import this: use the bundled modules directly.\n"
            "import numpy\n"
            "\n"
            "print(numpy.arange(3))\n"
        ),
        "members": [
            {
                "name": "module_from_binary",
                "signature": "module_from_binary(fullname, spec)",
                "kind": "function",
                "description": "Loads a bundled compiled module for the import system; internal.",
                "verified": True,
            },
            {
                "name": "module_from_bitcode",
                "signature": "module_from_bitcode(path, spec, script_path)",
                "kind": "function",
                "description": "Loads a bundled bitcode extension for the import system; internal.",
                "verified": True,
            },
            {
                "name": "raise_exception_if_needed",
                "signature": "raise_exception_if_needed()",
                "kind": "function",
                "description": "Re-raises a pending loader error; internal.",
                "verified": True,
            },
        ],
    },
}

#: What Pyto does **not** have.  Each entry: ``name`` (what the model might try to import or
#: call), ``reason`` (why it cannot work) and ``instead`` (the closest thing that does).
NOT_AVAILABLE: Tuple[Dict[str, str], ...] = (
    {
        "name": "Reminders (EventKit reminders)",
        "reason": "Pyto has no Python API for Reminders. EventKit is reachable through the "
        "Objective-C bridge, but no wrapper exists and accessing reminders that way is "
        "unverified on device.",
        "instead": "Write a note file in the workspace, or run a Shortcut that adds the reminder.",
    },
    {
        "name": "HealthKit",
        "reason": "No Pyto API. HealthKit is in the bridged-framework list, but HealthKit "
        "requires an entitlement and a permission flow Pyto does not expose to scripts.",
        "instead": "Export the data from the Health app by hand, or use a Shortcut.",
    },
    {
        "name": "Bluetooth / CoreBluetooth",
        "reason": "No Pyto API for scanning or connecting to Bluetooth devices. CoreBluetooth "
        "is bridged, but it needs entitlements and background modes an app script does not get.",
        "instead": "multipeer, for device-to-device strings between two devices running Pyto.",
    },
    {
        "name": "Speech recognition (microphone to text)",
        "reason": "Pyto's speech module is text-to-speech only: say(), is_speaking(), wait(), "
        "get_available_languages(). There is no microphone transcription API.",
        "instead": "Dictate into a Shortcut, or type/paste the text.",
    },
    {
        "name": "pip install of C extensions at runtime",
        "reason": "iOS apps must be self-contained, so compiled packages cannot be installed "
        "or updated inside Pyto; only libraries bundled with the app exist (numpy, pandas, "
        "Pillow, OpenCV, ...).",
        "instead": "Use the standard library or a bundled module; choose a pure-Python design.",
    },
    {
        "name": "A daemon, cron or launchd job",
        "reason": "There is no such thing on iOS. A script cannot stay resident: leaving the "
        "app suspends it and iOS eventually kills it.",
        "instead": "background.BackgroundTask while the user keeps the app alive, or "
        "background.request_background_fetch() for a few short OS-scheduled runs a day.",
    },
    {
        "name": "A PTY / real interactive terminal",
        "reason": "Pyto's console is an hterm shell backed by ios_system, not a kernel PTY; "
        "there is no pty module and no /dev/tty to attach to.",
        "instead": "Run the command with os.system()/subprocess.Popen (both are the embedded "
        "ios_system shell) or do the work in Python.",
    },
    {
        "name": "Real subprocess / os.fork / multiprocessing",
        "reason": "Pyto stubs fork and runs a 'subprocess' in-process, synchronously: "
        "kill()/terminate() are no-ops and os.waitpid() returns (-1, 0).",
        "instead": "threading for concurrency inside the script, with a deadline; the "
        "harness's run_program timeout is cooperative for the same reason.",
    },
    {
        "name": "git",
        "reason": "Not bundled and not installable: it is a native binary, and iOS apps "
        "cannot ship or run arbitrary executables.",
        "instead": "Keep versions as timestamped copies of files in the workspace.",
    },
    {
        "name": "ffmpeg",
        "reason": "Not bundled and not installable in a sandboxed iOS app.",
        "instead": "Use a Shortcut's media actions, or AVFoundation through the Objective-C bridge (advanced).",
    },
    {
        "name": "wget / curl as an external binary",
        "reason": "No external binaries; there is no shell PATH with those tools.",
        "instead": "urllib.request / http.client from the standard library.",
    },
    {
        "name": "make / gcc / a compiler",
        "reason": "No compiler runs inside the app, and compiled output could not be loaded "
        "anyway.",
        "instead": "Pure Python, or a bundled module.",
    },
    {
        "name": "Clipboard access in the background",
        "reason": "iOS blocks pasteboard reads while the app is not in the foreground; Pyto's "
        "own background module documents this, so a clipboard watcher cannot work.",
        "instead": "Read the clipboard when the user opens the app (a Shortcut can launch it).",
    },
)

#: Names that mean "the model tried something Pyto does not have" for hint purposes.  The
#: keys are lower-case module names; the values point at the NOT_AVAILABLE entry.
_MISSING_MODULE_REASONS: Dict[str, str] = {
    "reminders": "Reminders (EventKit reminders)",
    "eventkit": "Reminders (EventKit reminders)",
    "healthkit": "HealthKit",
    "health": "HealthKit",
    "bluetooth": "Bluetooth / CoreBluetooth",
    "corebluetooth": "Bluetooth / CoreBluetooth",
    "speech_recognition": "Speech recognition (microphone to text)",
    "pyaudio": "Speech recognition (microphone to text)",
    "git": "git",
    "ffmpeg": "ffmpeg",
    "wget": "wget / curl as an external binary",
    "curl": "wget / curl as an external binary",
    "make": "make / gcc / a compiler",
    "pty": "A PTY / real interactive terminal",
    "multiprocessing": "Real subprocess / os.fork / multiprocessing",
    "numpy": "pip install of C extensions at runtime",
    "pandas": "pip install of C extensions at runtime",
    "scipy": "pip install of C extensions at runtime",
    "cv2": "pip install of C extensions at runtime",
    "lxml": "pip install of C extensions at runtime",
    "requests": "pip install of C extensions at runtime",
}


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def module_names() -> List[str]:
    """Every curated module name, in a stable, human-sensible order."""
    ordered = [name for name in _MODULE_ORDER if name in CURATED]
    extra = sorted(name for name in CURATED if name not in _MODULE_ORDER)
    return ordered + extra


def known_module(name: str) -> bool:
    """True when ``name`` is a module this catalogue knows anything about."""
    return _normalise_module(name) in CURATED


def verified_module_names() -> List[str]:
    """Module names confirmed against a primary source — what the system prompt may promise."""
    return [name for name in module_names() if CURATED[name].get("verified")]


#: Verified modules a generated program should still never import directly: Pyto's own source
#: calls ``pyto`` "internal or plugin use", and ``_extensionsimporter`` is a private C loader.
INTERNAL_MODULES = frozenset({"pyto", "_extensionsimporter"})


def prompt_module_names() -> List[str]:
    """The module list the system prompt hands the model: verified, minus the internals."""
    return [name for name in verified_module_names() if name not in INTERNAL_MODULES]


def entry_for(name: str) -> Optional[Dict[str, Any]]:
    return CURATED.get(_normalise_module(name))


def not_available_entry(name: str) -> Optional[Dict[str, str]]:
    wanted = _normalise_module(name).replace("-", "_")
    for item in NOT_AVAILABLE:
        label = item["name"].split("(")[0].strip().lower().replace(" ", "_").replace("/", "_")
        if wanted == label or wanted in label:
            return item
    return None


def _normalise_module(name: str) -> str:
    text = (name or "").strip().strip("\"'").strip()
    if text.endswith(".py"):
        text = text[:-3]
    return text.split(".")[0] if "." in text else text


def _signature_of(value: Any) -> str:
    try:
        return str(inspect.signature(value))
    except (TypeError, ValueError, AttributeError):
        return ""


def _first_doc_line(value: Any) -> str:
    try:
        doc = inspect.getdoc(value) or ""
    except Exception:  # noqa: BLE001 - a bridge object may raise anything here
        return ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _kind_of(value: Any) -> str:
    if inspect.isclass(value):
        return "class"
    if callable(value):
        return "function"
    if isinstance(value, (str, int, float, bool, tuple, list, dict, frozenset)):
        return "constant"
    return "other"


#: Names that ``dir()`` exposes but that are plumbing rather than API.
_DEVICE_NOISE = frozenset(
    {
        "annotations",
        "os",
        "sys",
        "re",
        "json",
        "time",
        "math",
        "threading",
        "warnings",
        "typing",
        "base64",
        "random",
        "string",
        "sleep",
        "abspath",
        "check",
        "deprecated",
        "deprecation_msg",
        "List",
        "Dict",
        "Union",
        "Optional",
        "Any",
        "Tuple",
        "Callable",
        "Iterable",
        "Sequence",
    }
)


def _device_members(module: Any) -> Dict[str, Dict[str, str]]:
    """Enumerate the public members of an imported module, best effort."""
    found: Dict[str, Dict[str, str]] = {}
    try:
        names = [name for name in dir(module) if not name.startswith("_")]
    except Exception:  # noqa: BLE001 - a bridge module can refuse dir()
        return found
    for name in names:
        if name in _DEVICE_NOISE:
            continue
        try:
            value = getattr(module, name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, types.ModuleType):
            continue
        owner = getattr(value, "__module__", None)
        if callable(value) and isinstance(owner, str) and owner not in ("", module.__name__):
            # Imported from somewhere else (`from Foundation import NSURL`), not this module's API.
            continue
        found[name] = {
            "signature": _signature_of(value),
            "doc": _first_doc_line(value),
            "kind": _kind_of(value),
        }
    return found


def _probe(name: str) -> Tuple[bool, Dict[str, Dict[str, str]], str]:
    """Import ``name`` under a guard.  Returns ``(present, members, error)``.

    ``BaseException`` is caught on purpose: Pyto's bridge modules can raise anything at
    import time, and "the module is unusable here" must be a value, not a crash.
    """
    try:
        module = importlib.import_module(name)
    except BaseException as exc:  # noqa: BLE001 - bridge modules raise anything
        return False, {}, "{}: {}".format(type(exc).__name__, exc)
    if module is None:  # pragma: no cover - a fake module in sys.modules
        return False, {}, "import returned None"
    return True, _device_members(module), ""


# --------------------------------------------------------------------------------------
# Cache (the doctor's capabilities.json, extended not replaced)
# --------------------------------------------------------------------------------------


def capabilities_path(state_dir: Optional[str] = None) -> str:
    """Where the doctor keeps its discovery file; ``~/.pyto_harness`` by default."""
    if state_dir:
        return os.path.join(os.path.abspath(os.path.expanduser(state_dir)), "capabilities.json")
    from .config import default_state_dir

    return os.path.join(default_state_dir(), "capabilities.json")


def load_cache(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Read the catalogue's slice of ``capabilities.json`` (empty when absent/damaged)."""
    path = capabilities_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    cached = payload.get(CACHE_KEY)
    return cached if isinstance(cached, dict) else {}


def save_cache(modules: Dict[str, Any], state_dir: Optional[str] = None) -> bool:
    """Merge this catalogue's probe into ``capabilities.json`` without touching other keys.

    The doctor writes ``version``/``created_at``/``platform``/``python``/``signatures`` into
    the same file; a read-modify-write here (and a matching preserve in the doctor) means
    neither tool erases the other's data.
    """
    path = capabilities_path(state_dir)
    payload: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
        if isinstance(existing, dict):
            payload = existing
    except (OSError, ValueError):
        payload = {}
    payload[CACHE_KEY] = {
        "version": CATALOGUE_VERSION,
        "created_at": _now_ms(),
        "platform": _platform_label(),
        "python": _python_label(),
        "modules": modules,
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _platform_label() -> str:
    from . import ios

    return ios.platform_label()


def _python_label() -> str:
    import platform

    return platform.python_version()


def _cached_module(name: str, cached_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    modules = cached_payload.get("modules")
    if not isinstance(modules, dict):
        return None
    item = modules.get(name)
    return item if isinstance(item, dict) else None


# --------------------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------------------


def introspect(
    module_name: str,
    *,
    state_dir: Optional[str] = None,
    use_cache: bool = True,
    persist: bool = False,
) -> Dict[str, Any]:
    """Merge the curated entry for ``module_name`` with what this device actually has.

    Device truth wins: a member the device lacks is reported as missing, a signature the
    device reports replaces the curated one, and members that exist only on the device are
    added with the device's own docstring.  When the module cannot be imported the result
    still carries the catalogue, clearly marked, plus the import error.

    ``persist=True`` writes the merged result into ``capabilities.json`` under the
    catalogue's own key (the doctor's data there is preserved).
    """
    name = _normalise_module(module_name)
    curated = CURATED.get(name)
    present, device, error = _probe(name) if name else (False, {}, "no module name given")

    cached = _cached_module(name, load_cache(state_dir)) if (use_cache and not present) else None
    stale_members: Dict[str, Any] = {}
    if isinstance(cached, dict) and cached.get("present"):
        stale_members = cached.get("members") or {}

    members: List[Dict[str, Any]] = []
    curated_names = set()
    if curated:
        for item in curated.get("members", []):
            member = dict(item)
            member.setdefault("kind", "function")
            member.setdefault("verified", False)
            member.setdefault("caveats", [])
            curated_names.add(member["name"])
            device_item = device.get(member["name"])
            if present:
                member["on_device"] = bool(device_item)
                if device_item:
                    if device_item.get("signature") and device_item["signature"] != member.get("signature"):
                        member["catalogue_signature"] = member.get("signature", "")
                        member["signature"] = device_item["signature"]
                        member["signature_source"] = "device"
                    if device_item.get("kind") and device_item["kind"] != "other":
                        member["kind"] = device_item["kind"]
                    if device_item.get("doc") and not member.get("description"):
                        member["description"] = device_item["doc"]
            else:
                member["on_device"] = None
            members.append(member)
        for device_name in sorted(device):
            if device_name in curated_names:
                continue
            info = device[device_name]
            members.append(
                {
                    "name": device_name,
                    "signature": info.get("signature") or device_name,
                    "kind": info.get("kind", "other"),
                    "description": info.get("doc") or "(no docstring on this device)",
                    "verified": True,
                    "caveats": [],
                    "curated": False,
                    "on_device": True,
                    "signature_source": "device",
                }
            )

    result: Dict[str, Any] = {
        "module": name,
        "known": bool(curated),
        "available": bool(present),
        "error": error,
        "purpose": (curated or {}).get("purpose", ""),
        "verified": bool((curated or {}).get("verified", False)),
        "caveats": list((curated or {}).get("caveats", [])),
        "snippet": (curated or {}).get("snippet", ""),
        "sources": list((curated or {}).get("sources", [])),
        "members": members,
        "stale": False,
        "stale_at": 0,
    }
    if present:
        result["device_member_count"] = len(device)
    if stale_members and not present:
        result["stale"] = True
        result["stale_at"] = int((cached or {}).get("created_at") or 0)
        for member in members:
            if member["name"] in stale_members:
                member["on_device"] = True
                member["stale"] = True
    if present and persist:
        merged = dict(load_cache(state_dir).get("modules") or {})
        merged[name] = _cache_entry(result)
        save_cache(merged, state_dir=state_dir)
    return result


def _cache_entry(info: Dict[str, Any]) -> Dict[str, Any]:
    """The compact, JSON-safe shape stored in ``capabilities.json``."""
    return {
        "present": bool(info.get("available")),
        "checked_at": _now_ms(),
        "members": {
            member["name"]: {
                "signature": member.get("signature", ""),
                "kind": member.get("kind", "other"),
                "on_device": member.get("on_device"),
                "verified": bool(member.get("verified")),
            }
            for member in info.get("members", [])
        },
    }


#: Per-process memo so a turn that calls the tool several times does not re-import.
_MEMO: Dict[str, Dict[str, Any]] = {}


def introspect_cached(module_name: str, *, state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Like :func:`introspect`, but reuses the result within this process."""
    name = _normalise_module(module_name)
    key = "{}|{}".format(name, state_dir or "")
    if key not in _MEMO:
        _MEMO[key] = introspect(name, state_dir=state_dir)
    return _MEMO[key]


def clear_memo() -> None:
    """Forget the per-process probe memo (tests, and after an app update)."""
    _MEMO.clear()


def probe_all(*, state_dir: Optional[str] = None, persist: bool = False) -> Dict[str, Dict[str, Any]]:
    """Introspect every curated module.  Used by the index, the prompt and the doctor."""
    infos: Dict[str, Dict[str, Any]] = {}
    for name in module_names():
        infos[name] = introspect(name, state_dir=state_dir)
    if persist:
        save_cache({name: _cache_entry(info) for name, info in infos.items()}, state_dir=state_dir)
    return infos


def available_modules(*, state_dir: Optional[str] = None) -> List[str]:
    return [name for name, info in probe_all(state_dir=state_dir).items() if info["available"]]


def missing_modules(*, state_dir: Optional[str] = None) -> List[str]:
    return [name for name, info in probe_all(state_dir=state_dir).items() if not info["available"]]


def module_member_names(module_name: str, *, state_dir: Optional[str] = None) -> List[str]:
    """Real member names for a module: the device's when importable, else the catalogue's."""
    info = introspect_cached(module_name, state_dir=state_dir)
    if info.get("available"):
        live = [m["name"] for m in info["members"] if m.get("on_device")]
        if live:
            return live
    return [m["name"] for m in info.get("members", [])]


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _availability_line(info: Dict[str, Any]) -> str:
    if info["available"]:
        count = sum(1 for m in info["members"] if m.get("on_device"))
        return "available on this device ({} member(s) found with dir())".format(count)
    if not info["known"]:
        return "NOT a known Pyto module (nothing in the catalogue and not importable here)"
    line = "NOT importable on this device"
    if info.get("error"):
        line += " — {}".format(info["error"])
    if info.get("stale"):
        line += " (it was present at the last probe; the cached names below are marked stale)"
    return line


def _render_member(member: Dict[str, Any], indent: str = "- ") -> str:
    name = member.get("name", "?")
    signature = member.get("signature") or name
    marks: List[str] = []
    if not member.get("verified", False):
        marks.append("unverified")
    if member.get("signature_source") == "device":
        marks.append("signature from this device")
    if member.get("stale"):
        marks.append("stale")
    tail = "  [{}]".format(", ".join(marks)) if marks else ""
    lines = ["{}{} — {}{}".format(indent, signature, member.get("description", ""), tail)]
    for caveat in member.get("caveats", [])[:2]:
        lines.append("{}  note: {}".format(indent, caveat))
    return "\n".join(lines)


def _render_module(info: Dict[str, Any], *, member: Optional[str] = None) -> str:
    name = info["module"]
    lines: List[str] = []
    header = "## {}".format(name)
    if info.get("purpose"):
        header += " — {}".format(info["purpose"])
    lines.append(header)
    lines.append("status: {}".format(_availability_line(info)))
    if info.get("error") and info.get("available"):
        lines.append("note: {}".format(info["error"]))
    for caveat in info.get("caveats", []):
        lines.append("caveat: {}".format(caveat))

    members = list(info.get("members", []))
    if member:
        wanted = member.strip()
        members = [m for m in members if m["name"] == wanted or m["name"].endswith("." + wanted)]
        if not members:
            return "\n".join(lines + ["", "no member named {!r} is recorded for {}.".format(wanted, name)])

    usable = [m for m in members if m.get("on_device") is not False]
    absent = [m for m in members if m.get("on_device") is False]
    if usable:
        lines.append("")
        if info.get("available"):
            lines.append("Members (use only these):")
        else:
            lines.append(
                "Members from the catalogue (this module does not import here, so treat every "
                "name as unconfirmed on this device):"
            )
        for item in usable:
            lines.append(_render_member(item))
    if absent:
        lines.append("")
        lines.append(
            "In the catalogue but NOT on this device (do not use): {}".format(
                ", ".join(item["name"] for item in absent)
            )
        )
    device_only = [m for m in members if not m.get("curated", True) and m.get("on_device")]
    if device_only:
        lines.append("")
        lines.append(
            "Found on this device but not in the catalogue (names only; read their docstrings): "
            + ", ".join(m["name"] for m in device_only)
        )
    if info.get("snippet") and not member:
        lines.append("")
        lines.append("Example:")
        for snippet_line in info["snippet"].rstrip().splitlines():
            lines.append("    " + snippet_line)
    if info.get("sources") and not member:
        lines.append("")
        lines.append("source: {}".format(info["sources"][0]))
    return "\n".join(lines)


def _render_index(infos: Dict[str, Dict[str, Any]], *, available_only: bool = False) -> str:
    present = [(n, i) for n, i in infos.items() if i["available"]]
    absent = [(n, i) for n, i in infos.items() if not i["available"]]
    lines = ["# Pyto library reference", ""]
    lines.append(
        "Platform: {}. {} of {} catalogued Pyto modules import here.".format(
            _platform_label(), len(present), len(infos)
        )
    )
    lines.append(
        "Call pyto_api(module=\"<name>\") for one module's members, signatures, snippet and caveats."
    )
    if present:
        lines.append("")
        lines.append("Available here ({}):".format(len(present)))
        for name, info in present:
            count = sum(1 for m in info["members"] if m.get("on_device") is not False)
            lines.append("- {} — {} ({} member(s))".format(name, info.get("purpose") or "", count))
    if absent and not available_only:
        lines.append("")
        lines.append("In the catalogue but NOT importable here ({}):".format(len(absent)))
        for name, info in absent:
            reason = info.get("error") or "not present"
            lines.append("- {} — {}".format(name, reason))
    return "\n".join(lines)


def not_available_text() -> str:
    """The NOT-AVAILABLE summary, shared by the tool, the prompt's doc and the doctor."""
    lines = ["# Pyto does NOT have these (do not write programs that depend on them)"]
    for item in NOT_AVAILABLE:
        lines.append("- {}: {}".format(item["name"], item["reason"]))
        lines.append("  instead: {}".format(item["instead"]))
    return "\n".join(lines)


def _not_available_markdown() -> str:
    lines = ["## Not available in Pyto (do not try)", ""]
    for item in NOT_AVAILABLE:
        lines.append("- **{}** — {}".format(item["name"], item["reason"]))
        lines.append("  - instead: {}".format(item["instead"]))
    return "\n".join(lines)


def _cap(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[: max(0, max_chars - 400)]
    omitted = len(text) - len(head)
    return head + (
        "\n\n... [truncated: {} characters omitted; call pyto_api(module=\"<name>\") for one "
        "module at a time, or read PYTO_LIBS.md in the workspace for the full reference] ...\n".format(omitted)
    )


def render_reference(
    modules: Optional[Any] = None,
    member: Optional[str] = None,
    max_chars: int = 8000,
    *,
    state_dir: Optional[str] = None,
) -> str:
    """A compact markdown cheat-sheet, capped at ``max_chars``.

    ``modules=None`` renders the index (available modules + the NOT-AVAILABLE summary);
    a module name renders that module; ``member`` narrows a module to one member.
    """
    if modules is None or modules == "" or modules == []:
        infos = probe_all(state_dir=state_dir)
        text = _render_index(infos) + "\n\n" + not_available_text()
        return _cap(text, max_chars)
    if isinstance(modules, str):
        wanted: Sequence[str] = [modules]
    else:
        wanted = list(modules)
    chunks: List[str] = []
    for raw in wanted:
        name = _normalise_module(raw)
        if not name:
            continue
        info = introspect_cached(name, state_dir=state_dir)
        if not info["known"] and not info["available"]:
            chunks.append(
                "## {}\nstatus: unknown module.\n{}\n\n{}".format(
                    name,
                    "It is not one of Pyto's modules and it does not import here: {}".format(
                        info.get("error") or "no such module"
                    ),
                    _missing_module_note(name),
                )
            )
            continue
        chunks.append(_render_module(info, member=member))
    if not chunks:
        return _cap(_render_index(probe_all(state_dir=state_dir)), max_chars)
    return _cap("\n\n".join(chunks), max_chars)


def _missing_module_note(name: str) -> str:
    """A one-liner for a module that is neither curated nor importable."""
    entry = _missing_reason(name)
    if entry is None:
        close = difflib.get_close_matches(name, module_names(), n=3, cutoff=0.6)
        hint = " Closest Pyto modules: {}.".format(", ".join(close)) if close else ""
        return "Known Pyto modules: {}.{}".format(", ".join(module_names()), hint)
    return "Not available here: {} Instead: {}".format(entry["reason"], entry["instead"])


def _missing_reason(name: str) -> Optional[Dict[str, str]]:
    key = (name or "").strip().lower()
    label = _MISSING_MODULE_REASONS.get(key)
    if label is None:
        return None
    for item in NOT_AVAILABLE:
        if item["name"] == label:
            return item
    return None


# --------------------------------------------------------------------------------------
# PYTO_LIBS.md
# --------------------------------------------------------------------------------------

#: The freshness marker the doctor compares.  The hash covers the body only, so rendering
#: twice on the same device produces the same hash.
DOC_MARKER = "<!-- pyto-libs: sha256={digest} catalogue={version} platform={platform} -->"
DOC_NAME = "PYTO_LIBS.md"
_MARKER_RE = re.compile(r"<!--\s*pyto-libs:\s*sha256=([0-9a-f]{64}).*?-->")


def render_doc(*, state_dir: Optional[str] = None) -> str:
    """The full reference written to ``PYTO_LIBS.md``: every module, plus NOT-AVAILABLE."""
    infos = probe_all(state_dir=state_dir)
    parts = [
        "# Pyto libraries available to programs written here",
        "",
        "Generated by the pyto-harness doctor (`python run.py --doctor --fix`) from the "
        "harness's catalogue merged with this device's own `dir()`/`inspect` probe. "
        "Regenerate it after a Pyto update: it is only as fresh as the last probe.",
        "",
        "How to use it: before writing a program that imports a Pyto module, look the module "
        "up here (or call the `pyto_api` tool), and use only the members listed. Pyto's API is "
        "small, version-specific and easy to misremember. When `run_program` reports an "
        "`AttributeError` or `ImportError` about one of these modules, call `pyto_api` and fix "
        "the name instead of guessing again.",
        "",
        "Platform: {} · Python {}".format(_platform_label(), _python_label()),
        "",
        _render_index(infos),
        "",
        "## Modules in detail",
        "",
    ]
    for name in module_names():
        parts.append(_render_module(infos[name]))
        parts.append("")
    parts.append(_not_available_markdown())
    parts.append("")
    body = "\n".join(parts).rstrip() + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    marker = DOC_MARKER.format(digest=digest, version=CATALOGUE_VERSION, platform=_platform_label())
    return marker + "\n\n" + body


def doc_digest(text: str) -> str:
    """The hash recorded in a rendered document, or '' when it has no marker."""
    match = _MARKER_RE.search(text or "")
    return match.group(1) if match else ""


def body_digest(text: str) -> str:
    """The hash of the document's body as it is on disk (detects a hand edit)."""
    marker = _MARKER_RE.search(text or "")
    if marker is None:
        return ""
    body = text[marker.end():].lstrip("\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def doc_is_fresh(path: str, *, state_dir: Optional[str] = None) -> Tuple[bool, str]:
    """``(fresh, reason)`` for an existing ``PYTO_LIBS.md``."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)
    recorded = doc_digest(text)
    if not recorded:
        return False, "no pyto-libs marker: written by an older harness"
    expected = doc_digest(render_doc(state_dir=state_dir))
    if recorded != expected:
        return False, "stale: the catalogue or this device's modules changed"
    if body_digest(text) != recorded:
        return False, "stale: it was edited after it was generated; regenerate it"
    return True, "current"


def write_doc(path: str, *, state_dir: Optional[str] = None) -> str:
    """Write the full reference; returns the text written.  Raises ``OSError`` on failure."""
    text = render_doc(state_dir=state_dir)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


# --------------------------------------------------------------------------------------
# Error hints for run_program
# --------------------------------------------------------------------------------------

_HAS_NO_ATTRIBUTE = re.compile(r"module '([A-Za-z_][\w.]*)' has no attribute '([^']+)'")
_CANNOT_IMPORT = re.compile(r"cannot import name '([^']+)' from '([A-Za-z_][\w.]*)'")
_NO_MODULE = re.compile(r"No module named '([A-Za-z_][\w.]*)'")


def parse_api_error(stderr: str) -> Optional[Tuple[str, str, str]]:
    """Classify a Python error about a module.

    Returns ``(kind, module, member)`` where kind is ``"attribute"``, ``"import"`` or
    ``"missing_module"``; ``member`` is empty for a missing module.
    """
    text = stderr or ""
    match = _HAS_NO_ATTRIBUTE.search(text)
    if match:
        return "attribute", _normalise_module(match.group(1)), match.group(2)
    match = _CANNOT_IMPORT.search(text)
    if match:
        return "import", _normalise_module(match.group(2)), match.group(1)
    match = _NO_MODULE.search(text)
    if match:
        return "missing_module", _normalise_module(match.group(1)), ""
    return None


def is_pyto_name(module: str) -> bool:
    """True when the name is one of Pyto's modules (or a known absent one)."""
    name = _normalise_module(module)
    return bool(name) and (name in CURATED or _missing_reason(name) is not None)


def hint_for_stderr(stderr: str, *, state_dir: Optional[str] = None) -> str:
    """A short "here is the real name" hint for an API error, or ''.

    The original stderr is never rewritten: this is appended to the tool result so the
    model can correct itself without another guessing round.
    """
    parsed = parse_api_error(stderr)
    if parsed is None:
        return ""
    kind, module, member = parsed
    if not module:
        return ""

    if kind == "missing_module" and not is_pyto_name(module):
        return ""
    if kind != "missing_module" and not is_pyto_name(module):
        return ""

    lines = ["--- Pyto API hint ---"]
    if kind == "missing_module":
        reason = _missing_reason(module)
        if reason is not None:
            lines.append("`{}` is not available here: {}".format(module, reason["reason"]))
            lines.append("instead: {}".format(reason["instead"]))
        elif module in CURATED:
            lines.append("`{}` is not importable on this device.".format(module))
        else:
            lines.append("`{}` is not importable here.".format(module))
        if module in CURATED:
            lines.append(
                'Call pyto_api(module="{}") for the catalogue entry and what this device has.'.format(module)
            )
            return "\n".join(lines)
        close_modules = difflib.get_close_matches(module, module_names(), n=3, cutoff=0.6)
        if close_modules:
            lines.append("Closest Pyto modules: {}.".format(", ".join(close_modules)))
        lines.append('Call pyto_api(module="<name>") for the real module name and members.')
        return "\n".join(lines)

    info = introspect_cached(module, state_dir=state_dir) if module in CURATED else None
    if info is None:
        return ""
    if not info["available"]:
        lines.append(
            "`{}` is not importable on this device{}.".format(
                module, ": " + info["error"] if info.get("error") else ""
            )
        )
        lines.append("So `{}.{}` cannot work here at all.".format(module, member))
        lines.append('Call pyto_api(module="{}") for what is available instead.'.format(module))
        return "\n".join(lines)

    candidates = [name for name in module_member_names(module, state_dir=state_dir)]
    short = [name.split(".")[-1] for name in candidates]
    close = difflib.get_close_matches(member, short, n=3, cutoff=0.5)
    if not close:
        close = difflib.get_close_matches(member, short, n=3, cutoff=0.3)
    lines.append("`{}.{}` does not exist on this device.".format(module, member))
    if close:
        lines.append("Closest real members of {}: {}.".format(module, ", ".join(close)))
    else:
        lines.append("No close match in {}'s members.".format(module))
    lines.append(
        '{} is available here; call pyto_api(module="{}") and use only its members.'.format(
            module, module
        )
    )
    return "\n".join(lines)
