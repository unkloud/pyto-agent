#!/usr/bin/env python3
"""Turn a markdown/text note into a short daily digest.

Reads one note (or a folder of notes), pulls out the parts that matter, and writes a
digest with a fixed template: what happened, what is open, what is next.  The point is a
summary you can read in thirty seconds, not another document to maintain.

Usage::

    python note_digest.py --note ~/notes/2024-03-02.md
    python note_digest.py --folder ~/notes --outdigest ~/notes/digest.md --days 7
    python note_digest.py --note note.md --template "Standup {date}: {open_count} open item(s)"

Standard library only, Python 3.10+, safe inside Pyto.  It only ever writes the file you
name with ``--outdigest`` (or prints to stdout when you do not name one).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Tuple

HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")
TODO = re.compile(r"\b(todo|fixme|later|follow up|open question)\b", re.IGNORECASE)
DONE = re.compile(r"^\s*[-*+]\s*\[[xX]\]\s*", re.MULTILINE)
UNCHECKED = re.compile(r"^\s*[-*+]\s*\[\s\]\s+(?P<text>.+?)\s*$", re.MULTILINE)
CHECKED = re.compile(r"^\s*[-*+]\s*\[[xX]\]\s+(?P<text>.+?)\s*$", re.MULTILINE)

#: Sections are bucketed by keyword so the digest can say *what kind* of thing is open.
OPEN_HINTS = ("next", "todo", "open", "follow", "question", "blocked", "pending")
DONE_HINTS = ("done", "shipped", "finished", "completed", "wins", "decisions")

DEFAULT_TEMPLATE = """# Digest for {date}

{note_count} note(s), {word_count} words.
{checklist_line}
## What happened
{done}

## Still open
{open}

## Next
{next}
"""


def read_note(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def split_sections(text: str) -> List[Tuple[str, List[str]]]:
    """Split a note into ``(heading, lines)`` pairs, keeping the preamble as ``''``."""
    sections: List[Tuple[str, List[str]]] = [("", [])]
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            sections.append((match.group("title").strip(), []))
            continue
        sections[-1][1].append(line)
    return sections


def bullets(lines: Iterable[str]) -> List[str]:
    out = []
    for line in lines:
        match = BULLET.match(line)
        if match:
            text = match.group("text")
            if not TODO.search(text) and len(text) > 2:
                out.append(text)
    return out


def classify(sections: List[Tuple[str, List[str]]]) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {"done": [], "open": [], "next": []}
    for heading, lines in sections:
        lowered = heading.lower()
        items = bullets(lines)
        if any(hint in lowered for hint in DONE_HINTS):
            buckets["done"].extend(items)
        elif any(hint in lowered for hint in OPEN_HINTS):
            buckets["next" if "next" in lowered else "open"].extend(items)
        else:
            # An unlabelled section still contributes: it is the body of the note, and
            # the first few bullets of a note are usually what happened.
            buckets["done"].extend(items[:5])
    return buckets


def checklists(text: str) -> Tuple[List[str], List[str]]:
    return (
        [match.group("text") for match in CHECKED.finditer(text)],
        [match.group("text") for match in UNCHECKED.finditer(text)],
    )


def build_digest(notes: Dict[str, str], template: str = DEFAULT_TEMPLATE) -> str:
    done: List[str] = []
    open_items: List[str] = []
    next_items: List[str] = []
    words = 0
    for name in sorted(notes):
        text = notes[name]
        words += len(text.split())
        buckets = classify(split_sections(text))
        done.extend(buckets["done"][:6])
        open_items.extend(buckets["open"][:6])
        next_items.extend(buckets["next"][:6])
        checked, unchecked = checklists(text)
        done.extend("(done) {}".format(item) for item in checked[:3])
        open_items.extend("(todo) {}".format(item) for item in unchecked[:5])

    def render(items: List[str]) -> str:
        if not items:
            return "_nothing recorded_"
        seen = set()
        unique = []
        for item in items:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append("- {}".format(item.strip()))
        return "\n".join(unique[:12])

    if len(notes) == 1:
        date = os.path.splitext(os.path.basename(next(iter(notes))))[0]
    else:
        date = datetime.date.today().isoformat()
    checklist_line = ""
    if open_items:
        checklist_line = "\n{} item(s) still open.\n".format(len(open_items))
    return template.format(
        date=date,
        note_count=len(notes),
        word_count=words,
        done=render(done),
        open=render(open_items),
        next=render(next_items),
        checklist_line=checklist_line,
    )


def gather(args: argparse.Namespace) -> Dict[str, str]:
    notes: Dict[str, str] = {}
    if args.note:
        path = os.path.abspath(os.path.expanduser(args.note))
        if not os.path.isfile(path):
            raise SystemExit("No such note: {}".format(path))
        notes[os.path.basename(path)] = read_note(path)
        return notes
    folder = os.path.abspath(os.path.expanduser(args.folder or "."))
    if not os.path.isdir(folder):
        raise SystemExit("Not a folder: {}".format(folder))
    cutoff = None
    if args.days:
        cutoff = datetime.date.today() - datetime.timedelta(days=int(args.days))
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith((".md", ".markdown", ".txt")):
            continue
        path = os.path.join(folder, name)
        if cutoff is not None:
            stamp = datetime.date.fromtimestamp(os.path.getmtime(path))
            if stamp < cutoff:
                continue
        notes[name] = read_note(path)
    return notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise notes into a short digest.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--note", help="a single note file")
    source.add_argument("--folder", help="a folder of .md/.txt notes")
    parser.add_argument("--outdigest", help="write the digest here (default: print it)")
    parser.add_argument("--days", type=int, help="only notes modified in the last N days")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE, help="Python format string for the digest")
    args = parser.parse_args(argv)

    notes = gather(args)
    if not notes:
        print("No notes found to summarise.", file=sys.stderr)
        return 1
    digest = build_digest(notes, args.template)

    if args.outdigest:
        target = os.path.abspath(os.path.expanduser(args.outdigest))
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(digest)
        print("Wrote {} ({} note(s), {} characters).".format(target, len(notes), len(digest)))
        return 0
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
