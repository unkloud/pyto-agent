#!/usr/bin/env python3
"""Turn the clipboard into a structured note.

The clipboard on iOS is where half-formed things live: an address, a shopping list, a
flight confirmation, a pile of links.  This program classifies what it reads, formats it
as markdown, and appends it to a dated note — so the thing you copied survives the next
copy.

On device the clipboard comes from Pyto's ``pasteboard``; anywhere else (or when you pass
``--from-file``) it reads a file or stdin, which is what makes the program testable.

Usage::

    python clipboard_note.py                          # clipboard -> ~/pyto_harness_workspace/notes/2024-03-02.md
    python clipboard_note.py --title "Trip" --tags travel
    python clipboard_note.py --from-file clip.txt --out note.md
    echo "https://example.com" | python clipboard_note.py --stdin

Standard library only, Python 3.10+, safe inside Pyto.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

URL = re.compile(r"https?://[^\s<>\"')]+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?){2,4}\d{2,4}")
MONEY = re.compile(r"(?:[$£€¥]\s?\d[\d,.]*|\d[\d,.]*\s?(?:USD|EUR|GBP|JPY|SEK|kr))", re.IGNORECASE)
DATEISH = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\b)",
    re.IGNORECASE,
)
TIMEISH = re.compile(r"\b\d{1,2}[:.]\d{2}\s?(?:am|pm)?\b", re.IGNORECASE)
BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(?P<text>.+?)\s*$")
CHECKBOX = re.compile(r"^\s*[-*+]\s*\[(?P<mark>[ xX])\]\s*(?P<text>.+?)\s*$")
KEY_VALUE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z ]{1,24}):\s*(?P<value>.+?)\s*$")


def clipboard_text() -> Tuple[str, str]:
    """Return ``(text, source_description)``.  Never raises for a missing bridge."""
    try:
        import pasteboard  # type: ignore
    except ImportError:
        return "", "no clipboard bridge (run inside Pyto, or pass --from-file/--stdin)"
    try:
        value = pasteboard.get()
    except Exception as exc:  # noqa: BLE001 - a locked device can refuse
        return "", "the clipboard could not be read: {}".format(exc)
    return (value or ""), "the iOS clipboard"


def classify(lines: List[str]) -> Dict[str, List[str]]:
    """Bucket each line: links, contacts, money, dates, checklist, key/value, plain."""
    out: Dict[str, List[str]] = {
        "links": [],
        "contacts": [],
        "money": [],
        "dates": [],
        "checklist": [],
        "pairs": [],
        "plain": [],
    }
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        checkbox = CHECKBOX.match(line)
        if checkbox:
            mark = "x" if checkbox.group("mark").lower() == "x" else " "
            out["checklist"].append("[{}] {}".format(mark, checkbox.group("text")))
            continue
        bullet = BULLET.match(line)
        if bullet:
            out["checklist"].append("[ ] {}".format(bullet.group("text")))
            continue
        if URL.search(line):
            out["links"].extend(URL.findall(line))
            continue
        if EMAIL.search(line) or (PHONE.fullmatch(line) and len(re.sub(r"\D", "", line)) >= 7):
            out["contacts"].append(line)
            continue
        if MONEY.search(line):
            out["money"].append(line)
            continue
        if DATEISH.search(line) or TIMEISH.search(line):
            out["dates"].append(line)
            continue
        pair = KEY_VALUE.match(line)
        if pair:
            out["pairs"].append((pair.group("key").strip(), pair.group("value")))
            continue
        out["plain"].append(line)
    return out


def render_note(text: str, title: str, tags: List[str], source: str) -> str:
    lines = text.splitlines()
    buckets = classify(lines)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out: List[str] = ["## {} ({})".format(title or "Clipboard note", stamp), ""]
    if tags:
        out.append("tags: {}".format(", ".join("#" + tag.lstrip("#") for tag in tags)))
        out.append("")

    def section(heading: str, entries: List[str]) -> None:
        if not entries:
            return
        out.append("### {}".format(heading))
        out.extend("- {}".format(entry) for entry in entries)
        out.append("")

    section("Links", buckets["links"])
    if buckets["pairs"]:
        out.append("### Details")
        out.extend("- **{}**: {}".format(key, value) for key, value in buckets["pairs"])
        out.append("")
    section("Checklist", buckets["checklist"])
    section("Dates and times", buckets["dates"])
    section("Amounts", buckets["money"])
    section("Contacts", buckets["contacts"])
    section("Notes", buckets["plain"][:40])
    out.append("<!-- captured from {} -->".format(source))
    out.append("")
    return "\n".join(out)


def default_note_path(workspace: str) -> str:
    return os.path.join(workspace, "notes", "{}.md".format(datetime.date.today().isoformat()))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Turn the clipboard into a structured note.")
    parser.add_argument("--title", default="", help="heading for this entry")
    parser.add_argument("--tags", default="", help="comma-separated tags")
    parser.add_argument("--out", help="note file to append to")
    parser.add_argument("--workspace", default=os.path.expanduser("~/pyto_harness_workspace"))
    parser.add_argument("--from-file", help="read the text from a file instead of the clipboard")
    parser.add_argument("--stdin", action="store_true", help="read the text from stdin")
    parser.add_argument("--print", action="store_true", help="print the note instead of writing it")
    args = parser.parse_args(argv)

    if args.from_file:
        try:
            with open(os.path.expanduser(args.from_file), "r", encoding="utf-8", errors="replace") as handle:
                text, source = handle.read(), args.from_file
        except OSError as exc:
            print("Could not read {}: {}".format(args.from_file, exc), file=sys.stderr)
            return 2
    elif args.stdin:
        text, source = sys.stdin.read(), "stdin"
    else:
        text, source = clipboard_text()
        if not text:
            print("Nothing to file: {}".format(source), file=sys.stderr)
            return 1

    text = text.strip()
    if not text:
        print("Nothing to file: the text was empty.", file=sys.stderr)
        return 1

    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    note = render_note(text, args.title, tags, source)

    if args.print:
        print(note)
        return 0

    target = os.path.abspath(os.path.expanduser(args.out or default_note_path(args.workspace)))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    is_new = not os.path.exists(target)
    with open(target, "a", encoding="utf-8") as handle:
        if is_new:
            handle.write("# Notes for {}\n\n".format(datetime.date.today().isoformat()))
        handle.write(note)
        handle.write("\n")
    print("{} {} ({} line(s) classified).".format("Created" if is_new else "Appended to", target, len(text.splitlines())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
