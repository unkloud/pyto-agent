# Examples

Three programs the agent is expected to be able to write for an iOS user. They are here
for two reasons:

1. **As a target.** They show the shape of a good answer: a self-contained `.py` file
   with a docstring, `argparse`, a **dry run by default**, no third-party imports, and a
   summary printed at the end. When you ask the agent for something similar, this is what
   it should produce.
2. **As a test fixture.** `tests/test_examples.py` runs them for real — including through
   `run_program` — so the harness's own tooling is exercised against programs it did not
   write.

They are **not** part of the harness. Nothing imports them.

## The three

| File | What it does | Ask for it like this |
|---|---|---|
| `rename_by_date.py` | Renames files so they sort chronologically, using the date already in the name (falling back to the modification time). Dry run unless `--apply`. | *"rename my screenshots by date"* |
| `note_digest.py` | Turns one note or a folder of notes into a short digest: what happened, what is still open, what is next. | *"summarise my notes folder into a digest"* |
| `clipboard_note.py` | Reads the clipboard, classifies it (links, dates, amounts, contacts, checklist items, key/value pairs) and appends a structured markdown entry to today's note. | *"turn what I just copied into a note"* |

Each runs standalone inside Pyto:

```python
import runpy
runpy.run_path("examples/rename_by_date.py", run_name="__main__")
```

or from the Pyto console with arguments:

```python
import sys
sys.argv = ["rename_by_date.py", "--folder", "~/Screenshots", "--apply"]
runpy.run_path("examples/rename_by_date.py", run_name="__main__")
```

## How the agent is meant to produce them

The system prompt tells the model to prefer a reusable program over inline work, so the
intended flow is:

1. `memory_read` — does it already know where the user's screenshots live?
2. `list_files` / `read_file` — look at the real data before writing code for it.
3. `write_program(path, source, purpose)` — write the program into the workspace. The
   harness prepends a header naming the purpose and the Python version.
4. `run_program(path, args=[...])` — **run it before claiming it works.** The tool returns
   stdout, stderr and the exit code, and a dry run is normally the first invocation.
5. `finish(message)` — tell the user, in plain language, what was created and how to run
   it again.

Two habits these examples are meant to teach, and that the agent should copy:

* **Dry run by default.** `rename_by_date.py` prints the plan and changes nothing unless
  you pass `--apply`. A program that renames or deletes on the first run is a program that
  needs a backup.
* **Say what actually happened.** When `~/Screenshots` does not exist,
  `rename_by_date.py` prints "nothing renamed" and exits 0 — it does not pretend to have
  done work. The harness's system prompt asks the model for the same honesty.
