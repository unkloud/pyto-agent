# pyto-agent v1.0.28

## Restore browser sessions

Starting `python run.py --web` without `--resume` now opens a session chooser. Continue the
most recent session, select another valid saved session, or start a new independent session.
Resumed sessions restore their durable model context and show recent user and assistant
messages in Chat; the full projected conversation remains available in History. An explicit
`--resume` continues to open the requested session directly.

Session choices are discovered only from valid JSONL logs in the configured sessions
directory. The browser receives session IDs and safe display metadata, never filesystem
paths.

## Verification status

- The full offline test suite passed: 884 tests under CPython 3.12.
- Pyto/Safari device behavior remains unverified; use the web interface device checklist.
