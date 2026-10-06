# Examples

Three batch programs the agent is expected to be able to write for an iOS user. They are here
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
| `clipboard_note.py` | Reads the clipboard or a saved-program text input, classifies it (links, dates, amounts, contacts, checklist items, key/value pairs) and appends a structured markdown entry to today's note. | *"turn what I just copied into a note"* |

The clipboard notebook supports `main(inputs)` for the saved-program library. Its optional
`text`, `title` and `tags` fields are requested for each run; if `text` is omitted, it reads
Pyto's `pasteboard` module. When registered from the workspace, it writes the daily note under
`notes/` beside the saved script, so it can run again without an agent request.

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

## How the agent is meant to produce batch programs

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

## Interactive PytoUI preview

[`interactive_app_scaffold.py`](interactive_app_scaffold.py) is the reusable Goal 05 app
pattern. It demonstrates a persistent counter and form, a network request on a background
thread, visible callback and request errors, and cooperative worker cleanup. The separate
[`interactive_logic.py`](interactive_logic.py) module holds pure counter, form-validation
and response-decoding functions. Check those with a short `run_program` call before launch.

Use `preview_program(path="interactive_app_scaffold.py")` to try it. That tool checks
syntax and static top-level import availability, then presents the view without a batch
timeout. It returns after the view closes. Wrap callbacks with the injected
`harness_preview.guard(...)`, report background failures with
`harness_preview.report_error(...)`, and present/close with `harness_preview.present(view, ui)`
and `harness_preview.close(view)`. The result distinguishes validation, completed
presentation, successful instrumented interaction and any worker that survived close.
Static preflight does not execute imports or prove that an Objective-C member exists; use
`pyto_api` for Pyto members and treat actual Pyto presentation as a device check.

## Direct Objective-C framework recipes

[`objc_framework_recipes.py`](objc_framework_recipes.py) is a read-only bridge primer for two
small probes: Foundation's `NSBundle.mainBundle.bundleURL.path`, which follows Pyto's documented
example, and UIKit's `UIDevice.currentDevice()` model and OS-version properties. The sample keeps
framework imports inside the functions so it can be imported by desktop tests; running it on
desktop reports that those iOS modules are unavailable.

Pyto exposes listed framework modules through its Rubicon-ObjC bridge. Rubicon maps Objective-C
selector colons to underscores in Python method names. Use a Pyto wrapper first when it covers the
task. For another native API, confirm the framework, class, selector, return type, and any permission
or entitlement requirements in the relevant Pyto, Rubicon, and Apple references. A successful import
or class lookup only confirms that symbol lookup worked; it does not prove the feature's full device
flow. Keep a probe narrow, use public APIs, and record Pyto/iOS versions with device results.

References: [Pyto Objective-C guide](https://pyto.readthedocs.io/en/latest/Objective-C.html),
[Rubicon-ObjC selector mapping](https://rubicon-objc.beeware.org/en/stable/topics/type-mapping/),
[Apple NSBundle](https://developer.apple.com/documentation/foundation/bundle), and
[Apple UIDevice](https://developer.apple.com/documentation/uikit/uidevice).
