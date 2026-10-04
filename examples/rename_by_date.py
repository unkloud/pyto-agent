#!/usr/bin/env python3
"""Rename a folder of files so they sort by the date already in their name.

The problem this solves: iOS names screenshots and scans things like
``IMG_4821.PNG``, ``Photo 2024-03-02 at 09.14.22.jpeg`` and
``scan 03-02-2024.pdf``.  None of those sort chronologically in the Files app.

This program finds a date in each file name (or in the file's modification time as a
last resort), rewrites it as ``YYYY-MM-DD``, and prefixes a counter so names stay unique:

    IMG_4821.PNG                       -> 2024-03-02_001_IMG_4821.PNG
    Photo 2024-03-02 at 09.14.22.jpeg  -> 2024-03-02_002_Photo.jpeg
    scan 03-02-2024.pdf                -> 2024-03-02_003_scan.pdf

Usage::

    python rename_by_date.py --folder ~/Screenshots              # dry run, prints the plan
    python rename_by_date.py --folder ~/Screenshots --apply      # actually renames
    python rename_by_date.py --folder . --apply --pattern "*.png"

Standard library only, Python 3.10+, safe to run inside Pyto.  Nothing is deleted and
nothing leaves the folder; without ``--apply`` it only prints.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

#: Day-first or month-first ambiguity is resolved by trying both and keeping the first
#: interpretation that produces a real calendar date.
DATE_PATTERNS = (
    re.compile(r"(?P<y>20\d{2})[-_.](?P<m>\d{1,2})[-_.](?P<d>\d{1,2})"),  # 2024-03-02
    re.compile(r"(?P<d>\d{1,2})[-_.](?P<m>\d{1,2})[-_.](?P<y>20\d{2})"),  # 02-03-2024
    re.compile(r"(?P<m>\d{1,2})[-_.](?P<d>\d{1,2})[-_.](?P<y>20\d{2})"),  # 03-02-2024
)
TIME_PATTERN = re.compile(r"(?P<h>\d{1,2})[.\-:](?P<min>\d{2})[.\-:](?P<s>\d{2})")
COUNTER_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<n>\d{3})_")
#: iOS keeps its own metadata in these; renaming them breaks things.
SKIP_NAMES = {".DS_Store", "Icon\r"}


def parse_date(name: str) -> Optional[datetime.date]:
    """Find a calendar date in ``name``, or ``None``."""
    for pattern in DATE_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        parts = {key: int(value) for key, value in match.groupdict().items()}
        for month, day in ((parts["m"], parts["d"]), (parts["d"], parts["m"])):
            try:
                return datetime.date(parts["y"], month, day)
            except ValueError:
                continue
    return None


def parse_time(name: str) -> Optional[datetime.time]:
    match = TIME_PATTERN.search(name)
    if not match:
        return None
    try:
        return datetime.time(int(match.group("h")), int(match.group("min")), int(match.group("s")))
    except ValueError:
        return None


def clean_stem(stem: str) -> str:
    """Strip the date and time we are about to re-add, plus separator noise."""
    stripped = stem
    for pattern in DATE_PATTERNS:
        stripped = pattern.sub(" ", stripped)
    stripped = TIME_PATTERN.sub(" ", stripped)
    stripped = re.sub(r"\b(at|on|am|pm)\b", " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"[\s_\-.]{2,}", " ", stripped).strip(" _-.")
    return stripped or "file"


def plan_renames(folder: str, pattern: str = "*") -> List[Tuple[str, str, str]]:
    """Return ``(old_name, new_name, reason)`` for every file that would change."""
    import fnmatch

    entries = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if name in SKIP_NAMES or name.startswith(".") or not os.path.isfile(path):
            continue
        if pattern not in ("*", "") and not fnmatch.fnmatch(name, pattern):
            continue
        entries.append((name, path))

    dated: List[Tuple[datetime.date, Optional[datetime.time], str, str]] = []
    for name, path in entries:
        stem, extension = os.path.splitext(name)
        moment = parse_date(stem)
        source = "name"
        if moment is None:
            moment = datetime.date.fromtimestamp(os.path.getmtime(path))
            source = "modified time"
        dated.append((moment, parse_time(stem), stem, extension + "\0" + source))

    # Sort by date then time then original name, so the counter is chronological.
    dated.sort(key=lambda item: (item[0], item[1] or datetime.time(0, 0), item[2]))

    plan: List[Tuple[str, str, str]] = []
    counters: Dict[str, int] = {}
    for moment, _time, stem, packed in dated:
        extension, source = packed.split("\0")
        key = moment.isoformat()
        counters[key] = counters.get(key, 0) + 1
        cleaned = clean_stem(stem)
        candidate = "{}_{:03d}_{}{}".format(key, counters[key], cleaned, extension)
        if candidate != stem + extension:
            plan.append((stem + extension, candidate, "date from {}".format(source)))
    return plan


def apply_plan(folder: str, plan: List[Tuple[str, str, str]]) -> Tuple[int, List[str]]:
    """Rename through a temporary name so two files cannot collide mid-run."""
    problems: List[str] = []
    renamed = 0
    for old, new, _reason in plan:
        source = os.path.join(folder, old)
        target = os.path.join(folder, new)
        if not os.path.exists(source):
            problems.append("{} disappeared".format(old))
            continue
        if os.path.exists(target):
            problems.append("{} already exists, skipped {}".format(new, old))
            continue
        temporary = source + ".pyto-tmp"
        try:
            os.rename(source, temporary)
            os.rename(temporary, target)
            renamed += 1
        except OSError as exc:
            problems.append("{}: {}".format(old, exc))
            if os.path.exists(temporary) and not os.path.exists(source):
                try:
                    os.rename(temporary, source)
                except OSError:
                    pass
    return renamed, problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rename files so they sort by date.")
    parser.add_argument("--folder", required=True, help="folder to work in")
    parser.add_argument("--pattern", default="*", help="file-name glob, e.g. '*.png'")
    parser.add_argument("--apply", action="store_true", help="actually rename (default is a dry run)")
    args = parser.parse_args(argv)

    folder = os.path.abspath(os.path.expanduser(args.folder))
    if not os.path.isdir(folder):
        print("Not a folder: {}".format(folder), file=sys.stderr)
        return 2

    plan = plan_renames(folder, args.pattern)
    if not plan:
        print("Nothing to rename in {} ({} file(s) already look right).".format(folder, len(os.listdir(folder))))
        return 0

    print("{} file(s) would be renamed in {}".format(len(plan), folder))
    for old, new, reason in plan:
        print("  {}  ->  {}   [{}]".format(old, new, reason))

    if not args.apply:
        print("\nDry run: nothing was changed. Add --apply to rename for real.")
        return 0

    renamed, problems = apply_plan(folder, plan)
    print("\nRenamed {} file(s).".format(renamed))
    for problem in problems:
        print("  ! {}".format(problem))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
