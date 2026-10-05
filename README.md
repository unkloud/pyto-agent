# pyto-harness

**Free for noncommercial use** under the [PolyForm Noncommercial License 1.0.0](LICENSE).
Commercial uses outside the license's permitted purposes require a separate paid
commercial agreement. See [Licensing](#licensing).

An LLM agent that runs **inside Pyto on an iPhone or iPad** and writes small Python
programs for you. You describe a chore — "rename my screenshots by date", "summarise this
week's notes", "put the thing I just copied into a note" — and the agent writes a `.py`
file into a workspace on the device, runs it, and tells you what happened.

* **Standard library only.** No `pip install`, no `requests`, no `pydantic`. Every claim
  in this README is checked by `stdlib_audit.py` and 664 tests.
* **Python 3.10**, the version Pyto ships. Verified on real CPython 3.10.22 and 3.12.
* **OpenAI-compatible API** — DeepSeek by default (`deepseek-chat`), anything
  chat-completions-shaped otherwise.
* **Survives the app being killed.** Every turn is appended to a JSONL session log, so
  `--resume` picks up exactly where iOS interrupted you.

---

## 1. Installing on the device, from GitHub

Source: **https://github.com/unkloud/pyto-agent**

Pyto has **no `git`, no `unzip` and no `pip`** — but it has Python, so the installer uses
nothing but the standard library: it downloads the repository archive over HTTPS and unpacks
it. You do not need a computer, a cable or a Mac.

### Option A — one snippet, the whole setup (recommended)

Open Pyto, tap the console, choose a GitHub release tag or copy the full commit SHA you want,
replace `PIN` with it, and paste. The installer itself comes from `main`; the code it installs
comes from exactly the ref in `PIN`:

```python
import sys, urllib.request, runpy
PIN = "PASTE_GITHUB_TAG_OR_FULL_40_CHARACTER_COMMIT_SHA_HERE"
open("install.py", "wb").write(urllib.request.urlopen("https://raw.githubusercontent.com/unkloud/pyto-agent/main/install.py", timeout=120).read())
sys.argv = ["install.py", "--ref", PIN]
runpy.run_path("install.py", run_name="__main__")
```

Use a full commit SHA when you want an immutable source snapshot; tags can be moved. GitHub
supports ZIP archives for specific commits, and the commit ID keeps the source contents fixed
even if a tag moves. The installer prints the archive's SHA-256 on every run. To also check
the archive bytes against a known digest, add `"--sha256", "<digest>"` to `sys.argv`.

**That single run is the whole setup. There is nothing else to paste.** It unpacks
`pyto-agent` next to where Pyto starts, checks that every file parses as Python 3.10 and
that the package imports, and then:

1. **asks for your API key**, once:

   ```
   setup: an API key is needed for model calls; local tools work without one.
          It is not echoed, and it will be stored in /…/pyto_harness/config.json (mode 0600).
   API key (input hidden):
   ```

   The key is read with `getpass` where the platform can hide it, and with a plain line
   (saying so) where it cannot. The installer never echoes it and never logs it.
2. **proves the key before saving it**, with one minimal chat request through the
   harness's own doctor probe. `401`/`403` means the key is wrong and you are asked again
   (three tries, then it stops without saving anything); `404` makes it try the `/v1` form
   of `api_base` and keep whichever answers; an unreachable network is reported and you are
   offered `--save-anyway` (or `--yes` to accept it) instead of being blocked. The key is
   only ever shown as `<set:35 chars, ...AB12>`.
3. **writes `~/pyto_harness/config.json`** through the harness's own hardened writer:
   mode `0600`, created `O_EXCL`, never silently replacing a config that already holds a
   working key (pass `--reconfigure` to replace it deliberately), and keeping every field
   it does not own — `workspace`, `sessions_dir`, extra headers, `max_turns`, anything else
   you put there.
4. **health-checks and repairs**, printing one compact line instead of the whole report:

   ```
   doctor: 17 ok, 4 fixed, 0 need you
   ```

   Only fixes nobody can object to are applied: create the workspace and session
   directories, tighten file modes, cut a torn session line, write the `PYTO_LIBS.md` and
   `SHORTCUTS.md` references into the workspace. `--skip-fixes` runs the checks without
   changing anything.
5. **writes `start.py`** next to `run.py` and prints **exactly one** command:

   ```python
   import os, runpy, sys; os.chdir('/…/pyto-agent'); sys.argv = ['run.py']; runpy.run_path('run.py', run_name='__main__')
   ```

That is the last thing you have to paste. Or **open `start.py` in Pyto's editor and press
Run**: it does the `chdir` and the `runpy` with the real path for you, forwarding any
arguments, so you never copy a line at all.

**The state folder is now visible in Files.** Open **Files → On My iPhone/iCloud → Pyto →
`pyto_harness`** to see `config.json` (your API key, mode 0600), `sessions/` (the
transcripts), `backups/` (source snapshots) and `health.json`/`capabilities.json` — so you
can back it up, copy it to another device or delete it from the app UI. (Releases before
this one kept it hidden as `.pyto_harness`; the next run moves it out of hiding — before
anything can create the new folder — and when `pyto_harness` is already there without a
`config.json`, the old entries are moved in one by one and nothing already in the new
folder is ever overwritten. If the new folder already has a `config.json`, the old one is
left exactly where it is and named, with the command that deletes it.)

> **If Pyto has no home directory.** Some Pyto installs cannot resolve `~`: there is no
> usable `HOME` and `os.path.expanduser("~")` returns the string `"~"`, so `~/pyto_harness`
> is a *relative* path with a literal tilde and iOS refuses to create it —
> `[Errno 1] Operation not permitted: '~/pyto_harness'`. The harness does not trust `~`, and
> it does not trust the working directory either: the state is **anchored to the install**.
> In order it uses `PYTO_HARNESS_HOME`, then `PYTO_HARNESS_STATE_DIR`/`PYTO_HARNESS_CONFIG`,
> then the folder remembered in `<install>/pyto_harness_home.txt`, then the one candidate
> folder that already holds a `pyto_harness/config.json` (nothing is ever moved or merged),
> then a usable `HOME`, then a really-expanded `~`, then the folder **above the install**
> (the default: it survives replacing the install folder), then the install folder, then the
> folder Pyto runs scripts from, and finally — with a loud warning — the temporary
> directory. `python run.py --paths` prints every resolved path and the rule that chose the
> home; `--doctor` prints the same block. If two folders hold a `config.json`, or a
> `pyto_harness 2` copy appears beside the one in use, the harness names all of them and
> never merges or deletes anything. If the installer tells you it cannot find a writable
> folder, paste this once in the same console and re-run the installer:
>
> ```python
> import os; os.environ["PYTO_HARNESS_HOME"] = os.getcwd()
> ```
>
> Everything (`config.json`, sessions, memory, backups) then lives in `./pyto_harness`
> next to `run.py` — see `PYTO_HARNESS_HOME` in §2.

If you would rather the installer started the agent immediately, ask it to:

```python
sys.argv = ["install.py", "--chat"]                    # terminal chat REPL
sys.argv = ["install.py", "--ui"]                      # Pyto chat window
sys.argv = ["install.py", "--task", "rename my screenshots by date"]
```

**No terminal and no key? Nothing blocks.** The installer finishes, prints the one command
above, and exits 0 with a line telling you how to add the key later
(`python install.py --api-key sk-...`, or `export DEEPSEEK_API_KEY=…`).

Other flags: `--api-key`, `--key-file`, `--api-base`, `--model`, `--yes` (accept every
default and never prompt), `--save-anyway`, `--reconfigure`, `--skip-fixes`, and
`--no-network` (make no network request at all). **`--no-setup` installs the files only** —
no key, no config, no doctor — and prints the old step-by-step instructions; that is the
form to use from automation and CI.

To install the moving tip of `main` instead of a pinned release or commit, set `PIN = "main"`.

```python
import urllib.request, runpy
open("install.py", "wb").write(urllib.request.urlopen("https://raw.githubusercontent.com/unkloud/pyto-agent/main/install.py", timeout=120).read())
runpy.run_path("install.py", run_name="__main__")
```

**Updating is the same command.** Re-run the snippet above and it replaces the code in
place. To move to a newer release later, take that release's digest from its GitHub release
page and pass it as `--sha256` — the installer will refuse anything that does not match.

A digest that does not match **refuses to install and writes nothing**: the tag moved, the
download was modified in transit, or the pin is for a different ref.

Choose where it lands by passing an argument:

```python
import sys; sys.argv = ["install.py", "--into", "pyto-agent"]   # default
import sys; sys.argv = ["install.py", "--into", "~/Documents/pyto-agent"]
```

### Option B — the same thing without pasting a URL

If you would rather read the installer before running it (a good habit), fetch it with
`curl`, which Pyto ships, and run it:

```python
import os
os.system("curl -L -o install.py https://raw.githubusercontent.com/unkloud/pyto-agent/main/install.py")
import runpy
runpy.run_path("install.py", run_name="__main__")
```

The file is one self-contained script (no package, no imports beyond the standard library:
`argparse`, `ast`, `getpass`, `hashlib`, `importlib`, `io`, `json`, `os`, `re`, `runpy`,
`shutil`, `sys`, `types`, `urllib`, `zipfile`).

### Option C — download the zip on the phone or on a computer

1. Download **https://github.com/unkloud/pyto-agent/archive/refs/heads/main.zip**
   (in Safari, or on a computer and AirDrop/iCloud it across).
2. Put `pyto-agent-main.zip` somewhere Pyto can read — On My iPhone → Pyto, or iCloud Drive.
3. In the Pyto console, install from that file — the same one-stop setup, from local bytes
   instead of a download:

```python
import sys, runpy
sys.argv = ["install.py", "--zip", "pyto-agent-main.zip"]
runpy.run_path("install.py", run_name="__main__")
```

…or unzip it yourself, which is what the installer does anyway:

```python
import zipfile, os
zipfile.ZipFile("pyto-agent-main.zip").extractall(".")
os.rename("pyto-agent-main", "pyto-agent")
```

### Option D — from a computer, over Files or a cable

Copy the `pyto-agent` folder into Pyto's documents directory (Files → On My iPhone → Pyto),
then confirm where it landed:

```python
import os
print(os.getcwd())          # where Pyto starts
os.chdir("pyto-agent")      # or the full path you copied it to
```

### Updating later

Re-run the **same** Option A lines, or from inside the folder:

```python
import runpy, sys
sys.argv = ["install.py", "--into", "."]
runpy.run_path("install.py", run_name="__main__")
```

An update replaces the code and nothing else. Your API key, sessions, memory and backups
live in `~/pyto_harness`, and the programs the agent writes live in the workspace
(`~/pyto_harness_workspace` by default) — neither is inside the code directory, so
updating cannot lose them. A working key already in the config is kept and re-checked, not
rewritten. The installer refuses to touch a directory that is not a `pyto-agent` checkout
unless you pass `--force`.

### Checking it worked

The install already told you: the last line it printed is the command that starts the
agent, and `doctor: … 0 need you` means the checks passed. To look closer:

```python
import os, runpy, sys
os.chdir("pyto-agent")                                    # or just open start.py and press Run
sys.argv = ["run.py", "--version"]; runpy.run_path("run.py", run_name="__main__")
sys.argv = ["run.py", "--tools"];   runpy.run_path("run.py", run_name="__main__")   # 34 tools
```

`run.py` adds its own directory to `sys.path`, so it does not matter which directory Pyto
started in — `os.chdir` is only there to keep relative paths (workspace, `start.py`) sane.

### What still needs you

The installer does these last three things for you only where it can, and says so when it
cannot:

* **The Shortcut automation** — starting the harness from the Home screen, the Share sheet
  or Siri is a Shortcut you build once, in the Shortcuts app, following §5a. The doctor
  writes `SHORTCUTS.md` into the workspace with the exact URL scheme and task variable, so
  you are copying from a document, not guessing.
* **iOS permissions** — the first time a generated program touches the photo library, the
  calendar, notifications or speech, iOS shows *its* prompt. Grant it then; a denial is
  reported and never retried silently (§6).
* **The account behind the key** — the installer proves the key is accepted with one
  minimal request. It cannot buy credit, lift a rate limit or un-suspend an account.

> Pyto's `sys.executable` is not a real interpreter you can spawn. That is fine — the
> harness detects this (`harness/ios.py: has_fake_subprocess()`) and runs programs
> in-process instead. See §6 and [SECURITY.md](SECURITY.md).

---

## 2. Setting the API key

Any one of these, highest priority last:

```bash
# 1. environment (per session)
export DEEPSEEK_API_KEY=sk-...
export OPENAI_API_KEY=sk-...        # used if DEEPSEEK_API_KEY is unset

# 2. a config file (persists across launches) -- written for you by install.py,
#    or by hand with:
python run.py --init                # writes ~/pyto_harness/config.json, mode 0600
```

`install.py` (see §1) already does this one: it asks for the key once, proves it with one
minimal request, and writes the file 0600 through the same writer `--init` uses. Reach for
`--init` only if you are setting a machine up by hand.

The config file looks like this:

```json
{
  "api_base": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "api_key": "sk-REPLACE-ME",
  "max_turns": 8,
  "timeout": 60,
  "workspace": "~/pyto_harness_workspace"
}
```

Environment variables: `PYTO_HARNESS_MODEL`, `PYTO_HARNESS_API_BASE`,
`PYTO_HARNESS_MAX_TURNS`, `PYTO_HARNESS_TIMEOUT`, `PYTO_HARNESS_WORKSPACE`,
`PYTO_HARNESS_SESSIONS_DIR`, `PYTO_HARNESS_MAX_TOKENS`, `PYTO_HARNESS_TEMPERATURE`,
`PYTO_HARNESS_STREAM` — plus the three path overrides:

* **`PYTO_HARNESS_HOME`** — the directory the harness treats as your home: state, config
  and sessions live in `$PYTO_HARNESS_HOME/pyto_harness`. Set it when the device has no
  usable `~` (see §1); it is the documented escape hatch and always wins when it is
  writable.
* **`PYTO_HARNESS_CONFIG`** — the config file itself (its directory becomes the state
  directory unless `PYTO_HARNESS_STATE_DIR` says otherwise).
* **`PYTO_HARNESS_STATE_DIR`** — where `health.json`, `capabilities.json` and the source
  backups go.

With neither `PYTO_HARNESS_HOME` nor the two above set, the home is remembered in a plain
`pyto_harness_home.txt` next to `run.py` (delete it to choose again), an existing
`pyto_harness/config.json` in one of the known candidates is adopted, and the fallback
default is the folder **above** the install. `python run.py --paths` shows the winner, the
rule that chose it and every path derived from it.

A `~` in any of these, or in `--workspace`/`--resume`/`--into`, is expanded, and **refused
with an explanation** when this device cannot expand it — it is never used literally.
Command-line flags beat environment variables, which beat the file.

**The key is never printed.** `--dry-run`, the banner, the session log and every log line
show `<set:51 chars, ...AB12>` instead. The session log redacts any field whose name
contains `api_key`, `token`, `secret`, `password` or `authorization`.

> **About storing the key on iOS.** Pyto's `userkeys` / `NSUserDefaults` is convenient
> but it is **not secure storage** — it is a plist inside the app container, readable by
> anything that can read the container, and it is included in unencrypted backups. Keep
> the key in `~/pyto_harness/config.json` (chmod 600) and treat the device passcode plus
> FileVault-style iOS data protection as your actual protection. If that is not good
> enough for your threat model, do not put a key on the phone: run the harness against a
> local model on your network instead, with `--api-base http://your-box:8080/v1`.

---

## 3. Running it

**On the device there is no shell to type `python run.py` into.** Pyto runs a script, so the
easiest thing is to open **`start.py`** — the launcher the installer wrote next to `run.py` —
and press Run. For flags and arguments, use the one-line form the installer prints:

```python
import os, runpy, sys
os.chdir("pyto-agent")                              # where you installed it
sys.argv = ["run.py", "--doctor", "--fix"]          # the arguments you want
runpy.run_path("run.py", run_name="__main__")
```

**In a checkout on a computer** (or if you prefer Pyto's shell) the plain form works:

```bash
python run.py "rename my screenshots by date"     # one task, then exit
python run.py                                     # interactive terminal chat
python run.py --ui                                # Pyto chat window
python run.py --resume <session.jsonl> "and July too?"
python run.py --dry-run "..."                     # print the request, contact nothing
python run.py --capabilities                      # what this device can actually do
python run.py --tools                             # list the tools and their timeouts
python run.py --yolo "..."                        # skip approvals (see §7)
python run.py --allow-unattended-programs "..."     # headless Shortcuts: run programs, still deny sharing/URLs
python run.py --workspace ~/Documents/agent       # somewhere else to work

python run.py --doctor                            # diagnose this installation (exit 0/1/2)
python run.py --doctor --fix                      # repair what a machine can, then re-check
python run.py --doctor --deep                     # ... plus the offline test suite
python run.py --repair "the api_base keeps 404ing"  # gated edit of the harness's own source
python run.py --backups                           # list source snapshots
python run.py --restore <backup_id>               # put one back (also gated)
```

The same arguments work in both forms — only the `sys.argv = [...]` line changes.

Flags: `--model`, `--api-base`, `--api-key`, `--max-turns`, `--no-stream`, `--no-compact`,
`--verbose`, `--init`, `--version`, `--doctor`, `--fix`, `--deep`, `--deep-tests`,
`--no-network`, `--no-doctor`, `--repair`, `--backups`, `--restore`.

**The first normal run of a day also runs a fast, local health pass** and prints one line:

```
doctor: 12 ok, 1 fixed, 1 needs you (run --doctor for details)
```

It never blocks the run, never prints the key, and only applies the fixes nobody can object
to (create a missing directory, tighten the config mode, cut a torn session line).
`--no-doctor` or `PYTO_HARNESS_NO_DOCTOR=1` turns it off.

The task can also arrive as `?task=<urlencoded text>` on a `pyto://` URL, or in
`PYTO_HARNESS_TASK` — that is how a Shortcut starts a run without a terminal (§5a).

`--dry-run` is the safe way to see exactly what would be sent: the URL, the headers (with
the key redacted), the full JSON body, and every tool the model would be offered. It makes
**no network connection at all** — there is a test that points `--api-base` at a live mock
server and asserts the mock received zero requests.

---

## 3a. Writing programs with Pyto's libraries

A generated program is not limited to the standard library: Pyto ships its own modules
(`pasteboard`, `photos`, `notifications`, `pyto_ui`, `background`, `calendar_events`,
`file_system`, `speech`, `location`, `motion`, `apps`, `xcallback`, and more), and they are
usually the only way to reach the clipboard, the photo library, the calendar, the share
sheet, Shortcuts or a real UIKit window.

The model does not know that API from training, and guessing it (`photos.save_photo` instead
of `photos.save_image`, `pasteboard.set_clipboard` instead of `pasteboard.set_string`) costs
a run on your phone. So the loop is:

```
ask  ->  pyto_api(module="pasteboard")  ->  write_program  ->  run_program  ->  fix (+ pyto_api)
```

| Tool | What it gives the model |
|---|---|
| `pyto_api` | The grounding call. With no argument: the Pyto modules that import on **this** device plus the list of what Pyto does not have (Reminders, HealthKit, Bluetooth, speech recognition, `pip install` of C extensions, a daemon, a PTY, real `subprocess`, git/ffmpeg/wget/make). With `module=` : that module's real members, signatures, a minimal snippet, caveats and an `unverified` mark on anything the catalogue could not confirm from Pyto's docs or source. `member=` narrows further. Read-only, no approval needed, capped at 8 000 characters. |
| `write_program` | Writes the program into the workspace so you can re-run it later. |
| `run_program` | Runs it and returns stdout/stderr. If stderr contains `module 'X' has no attribute 'Y'`, `cannot import name 'Y' from 'X'` or `No module named 'X'` for a Pyto module, the result gets an appended **Pyto API hint**: the closest real member names (`difflib`), whether the module is even available here, and the `pyto_api` call to make. The original traceback is never rewritten. |

The same reference exists as a file: `PYTO_LIBS.md` in the workspace, generated from the
catalogue merged with this device's own `dir()`/`inspect` probe by

```bash
python run.py --doctor --fix        # writes/refreshes PYTO_LIBS.md
```

`--doctor` reports a `libs_reference` check (`ok` when the file exists and matches this
device, `warn` + fix when it is missing, stale after a Pyto update, or hand-edited). The
system prompt tells the model to consult `pyto_api` or `PYTO_LIBS.md` before importing a
Pyto module, and to fix the name it is given after an `AttributeError`/`ImportError`
instead of guessing again.

The catalogue lives in `harness/pyto_api.py`; every entry is traced to Pyto's documentation
or its `Lib/*.py` source, and entries that could not be confirmed that way are marked
`verified: false` (they are rendered with an `[unverified]` tag and the tool says so on
device).

---

## 4. The Pyto UI

```bash
python run.py --ui
```

Opens a small window: a scrollable transcript, a text field, a Send button. If `pyto_ui`
is not importable, `--ui` prints why and exits with code 3 — you get the terminal chat
instead.

The one design rule that matters: **the model call never runs on the UI thread.** The
button handler starts a `threading.Thread`, and that thread pushes text back through
`pyto_ui.main_thread`. A Pyto button handler that calls an API endpoint freezes the app
until the response lands, which is how you get the watchdog to kill your process.

---

## 5. Wiring iOS Shortcuts

### (a) A Shortcut that runs the harness

This is the normal entry point: **Shortcuts → Automation → Run Script, with "Show
Console" off.** The Shortcut passes its input on `sys.stdin`, and reads the script's
answer from **stdout** — "Get Script Output" is literally what the harness prints.

1. Shortcuts → **+** → *Run Script*.
2. Script: the path to `run.py` inside Pyto's container, e.g.
   `pyto-harness/run.py`.
3. Arguments: your task text, e.g. `rename my screenshots by date`.
4. Turn **Show Console off**, so the run is headless.
5. Add *Get Script Output* if you want the closing summary back in the Shortcut.

Because the task is an argument, the Shortcut itself can take input:

```
Ask for Input (Text)  ->  Run Script (run.py, argument: provided input)  ->  Show Result
```

To trigger it from outside, give the Shortcut a name and use its own URL scheme:

```
shortcuts://run-shortcut?name=Ask%20My%20Agent
```

Or run the script directly through Pyto's URL scheme:

```
pyto://python/<urlencoded path to run.py>
pyto://python/<urlencoded path to run.py>?task=<urlencoded task>
```

`run.py` reads a `task` query parameter as the task text, so a Shortcut (or a bookmarklet,
or another app) can start it without stdin.

**Editing Shortcuts unattended:** a Shortcut that runs a script with sharing or URL tools
needs someone to answer the approval prompt. In a headless Shortcut there is nobody to
answer, so the harness **denies** those calls and the summary says so.

Generated programs are treated the same way: `run_program` is auto-approved while a human
can answer, but in a headless run it is denied unless you opt in explicitly.

```python
import sys
sys.argv = ["run.py", "--allow-unattended-programs", "rename my screenshots by date"]
```

`--allow-unattended-programs` is **narrower than `--yolo`**: it lets the agent run the
programs it writes, while sharing, URLs and Shortcuts still fail closed with "nothing is
attached to answer". The trade-off in one sentence: in an unattended run an injected model
can execute code on your device without you seeing it, so use the flag only for a Shortcut
whose task and inputs you control — but prefer it to `--yolo`, which removes every
approval.

### (b) A Shortcut the harness runs

The agent has two tools for this. Both build a URL and hand it to iOS:

```
shortcuts://run-shortcut?name=Take%20a%20Photo
shortcuts://run-shortcut?name=<name>&input=text&text=<urlencoded input>

# with a result coming back through x-callback-url:
shortcuts://x-callback-url/run-shortcut?name=<name>&input=text&text=<input>&x-success=pyto%3A%2F%2F
```

* `shortcut_run(name, input_text)` — fire and forget. Returns the exact URL used.
* `shortcut_run_wait(name, input_text)` — same, plus an `x-success` callback. iOS reopens
  Pyto with `result=<the Shortcut's output>` as a URL parameter. Nothing in the app can
  *block* waiting for that round trip, so the tool reports the URL and where the answer
  will arrive rather than pretending it already has it.

### (c) Other URL schemes the harness uses

| Purpose | URL |
|---|---|
| Files app, workspace | `shareddocuments://<urlencoded absolute path>` |
| Files app, root | `shareddocuments://` |
| Run a Pyto script | `pyto://python/<urlencoded path>` |
| Run a Shortcut | `shortcuts://run-shortcut?name=...` |
| Run a Shortcut, get output | `shortcuts://x-callback-url/run-shortcut?name=...&x-success=pyto://` |

Everything the agent can reach from these is listed by `python run.py --capabilities`.

---

## 6. What works, and what cannot work on iOS

**Verified limits — these are properties of Pyto and iOS, not bugs in this harness.**

| Limit | Consequence | What the harness does |
|---|---|---|
| Python **3.10** only | No `tomllib`, no `asyncio.TaskGroup`, no `except*`, no `typing.Self` | The whole codebase is 3.10 syntax, checked by `stdlib_audit.py` |
| `subprocess` is **fake**: in-process, synchronous, no `fork`, `kill()`/`terminate()` are no-ops, `os.waitpid` returns `(-1, 0)` | A "timeout kill" would silently never kill anything | On device, programs run in-process with a **cooperative** timeout; on desktop, a real subprocess with process-group kill. `has_fake_subprocess()` detects which |
| No `multiprocessing` | No parallelism across processes | Tool calls overlap on threads inside one process |
| Scripts are stopped when free memory nears **~500 MB** (Pyto stops *all* of them) | An unbounded log or a chatty program crashes the session | Session logs compact near 4 000 events / 4 MB; tool output is capped and spilled; `memory_status` reports `os_proc_available_memory()` |
| No background daemon; the app is **suspended then killed** | Nothing runs while you are elsewhere | Everything durable is a file; `keepalive_start` uses `background.BackgroundTask` for a short reprieve |
| No PTY, no shell | `input()`, curses and progress bars misbehave | The system prompt tells the model to write non-interactive programs; programs get plain pipes |
| Clipboard works **only in the foreground** | Reading it from a background task fails | Documented in the tool description; the adapter reports the failure instead of guessing |
| No Reminders / HealthKit / Bluetooth module | Those tasks are impossible from Python | Route them through a Shortcut you build; `shortcut_run` triggers it |
| Compiled `pip` packages impossible (numpy/pandas are a paid in-app purchase) | No scientific stack | Standard library only; `array`, `statistics`, `math`, `sqlite3` cover a surprising amount |
| Photo library, calendar, notifications, speech need **permission** | First call may be denied | Adapters report the denial; nothing is retried silently |
| `background.BackgroundTask` is a **store-review grey area** (it plays silence to stay alive) | Apple has rejected apps for abusing it | Opt-in, labelled as such, and stopped as soon as the work is done |
| An app can only write inside its own container | You cannot reach arbitrary paths | The workspace is a path jail: `../` and symlink escapes are refused before any I/O |

**What genuinely works well on device:** reading and writing files in the workspace,
running real Python, the clipboard (foreground), the share sheet, opening URLs, running
Shortcuts, notifications, speech, saving images to Photos, adding calendar events, and
resuming a conversation after the app was killed.

---

## 6a. Diagnosing and repairing itself

`--doctor` runs 22 checks (interpreter, imports, stdlib-only, the home folder and its
state directory, config, key shape, DNS/TLS, auth, model, workspace, session-log integrity,
memory, iOS modules and call shapes, Shortcuts wiring, and the offline suite with
`--deep`) and reports each as `ok`, `warn`,
`fail`, `fixed`, `skipped` or `unfixable`, with an exit code of 0 / 1 / 2. `--doctor --fix`
applies the repairs a machine can and prints a before/after report. `--repair "<problem>"`
lets the model change the harness's own source, subject to a path jail, an AST/stdlib
pre-check, a snapshot with hashes, and the offline test suite as the gate: a red suite
reverts the edit byte-for-byte and hands back the failure verbatim. A bounded gate (~3 s,
232 tests) is the default; `--deep-tests` asks for all 664.

**The full story — the three tiers, the guardrail list, what is deliberately not automated,
and a worked transcript — is in [SELF-REPAIR.md](SELF-REPAIR.md).**

## 7. Safety

The full model — threat model, what approvals do *not* protect against, what is enforced
versus policy-level, key handling, data at rest and how to wipe it — is
**[SECURITY.md](SECURITY.md)**. The short version:

* **Approval is a policy function, not a prompt string.** It is evaluated *before*
  dispatch, so no tool can take a path that skips it; a policy that raises or returns
  anything but a decision denies. Workspace file work and reads are auto-approved;
  **sharing text, opening external URLs, running Shortcuts, writing to the calendar or
  photo library, notifications, speech and the background keepalive all need a yes** unless
  you pass `--yolo`. The prompt shows the whole argument (up to 4 000 characters, with an
  explicit "...(N more characters)" marker), and for `run_program` the program's path and
  the SHA-256 of the bytes that will run.
* With no interactive terminal (a Shortcut, a pipe, a cron-ish run) there is nobody to
  answer, so those calls are **denied**, not allowed — and so is `run_program`, unless you
  pass `--allow-unattended-programs` (or set `allow_unattended_programs: true`). Failing
  closed is the whole point.
* **`run_program` is the hole in the model, and this is the honest part.** On iOS a
  generated program runs *inside this process*, so it can read what the app can read,
  including `~/pyto_harness/config.json` and the session logs. The key is removed from its
  environment and scrubbed out of logs and spills, but it is not out of reach of code
  running in-process. Treat a program the agent wrote as code you are about to run.
* **The workspace is a jail for the file tools.** `write_program("../escape.py")`, absolute
  paths outside the workspace, and symlinks pointing out are all refused, checked on the
  real path. A *program* ignores the jail — it uses plain file I/O.
* **Nothing is deleted by the harness.** It writes files and runs programs; `run_program`
  can of course do anything you asked the model to write.
* `--dry-run` contacts nothing, and redacts credential-shaped headers and URL userinfo.
  Run it first when you are unsure what a task will do.
* **The self-repair gate refuses to touch `tests/`**, so an edit cannot weaken the suite that
  judges it, and `harness/repair.py` cannot be edited by the gate it implements. Source
  snapshots contain only `harness/*.py` and `run.py` — never a key, a session log or a
  workspace file — and their manifests are signed with a per-install key, so a snapshot
  dropped into `backups/` by anything else is refused instead of restored.
* Sessions, memory and spill files are **plaintext**, created `0600` in `0700` directories
  (the doctor reports and fixes looser modes). They hold your prompts, the model's replies
  and whatever the agent read. Delete them when you are done with them; SECURITY.md §6 has
  the list.

---

## 8. First three tasks to try

1. **`rename my screenshots by date`** — the agent writes `rename_by_date.py`, runs it in
   dry-run mode first, shows you the plan, and only renames when you agree. Exercises
   `write_program`, `run_program`, and reading its own output.
2. **`summarise my notes folder into a digest`** — it writes `note_digest.py` and produces
   a one-screen digest of what happened, what is open and what is next. Exercises
   `list_files`, `read_file` and a program with arguments.
3. **`turn what I just copied into a note`** — it calls `clipboard_get`, classifies the
   text (links, dates, amounts, checklists) and appends a structured entry to today's
   note. Exercises the device adapters and `memory_write`, so next time it remembers where
   your notes live.

Then try **`remember that my screenshots live in ~/Screenshots`** and start a new session —
the agent reads it back with `memory_read` and stops asking.

---

## 9. Layout

```
install.py             one-stop installer: GitHub -> files -> key -> config -> doctor -> start line
                       (stdlib only; `--no-setup` is the files-only form for CI)
run.py                 CLI entry point
harness/
  llm.py               chat-completions client: SSE, retries, cancellation, non-streaming fallback
  schema.py            JSON-Schema-subset validator (bool is not an integer)
  tools.py             tool registry: validation, approval, bounded timeout, concurrent dispatch
  tools_ios.py         the 34 model-facing tools (8 of them diagnose, repair or ground the API)
  ios.py               device capability adapters, all degrading gracefully off-device
  loop.py              agent loop, system prompt, approval policy, result truncation
  session.py           append-only JSONL log: append, resume, projection, compaction
  config.py            defaults + config.json + environment + CLI flags
  home.py              the one resolver for ~/pyto_harness (never a literal '~')
  budget.py            the size limits that keep iOS from killing the process
  textbudget.py        head/tail truncation with spill-to-file
  ui.py                Pyto UI window, terminal REPL, terminal approval prompt
  doctor.py            self-diagnosis: 22 structured checks + the fixes a machine can apply
  repair.py            self-repair: path-jailed, snapshot-first, test-gated source edits
  pyto_api.py          Pyto library grounding: 25 modules / 160 members, curated + introspected
  errors.py            error taxonomy with retryability
tests/                 664 offline tests against a stdlib mock OpenAI server
examples/              three programs the agent is expected to be able to write
stdlib_audit.py        proves "stdlib only" and "parses as Python 3.10"
```

What the harness keeps, in folders you can see (Files → On My iPhone/iCloud → Pyto):

```
pyto_harness/                     the state directory (no leading dot: Files shows it)
  config.json                     the API key (mode 0600) and your settings
  sessions/*.jsonl                one append-only log per conversation
  backups/                        signed source snapshots (harness/ + run.py only)
  health.json, capabilities.json  the last health pass, and what this device can do
pyto_harness_workspace/           the agent's own files: programs, memory.json, tool-output/
pyto_harness_home.txt             inside the install: the home this run remembered
```

The two visible folders sit in the home `--paths` reports — the folder above the install by
default, so replacing the install folder (the usual way to update on iOS) never moves your
key, sessions or memory. A `pyto_harness 2` folder next to the one in use is an iCloud/Files
copy: the harness names it, uses only the folder it resolved, and never merges or deletes
anything.

A copy of the tree made by the self-repair tests keeps its state beside itself, in
`pyto_harness_state/` — older copies used a hidden `.pyto_harness_state/`, which is still
read so their snapshots stay restorable.

## 10. Verifying it yourself

```bash
python3 -m unittest discover -s tests -t .     # 664 tests, offline, no network (~34 s)
python3 stdlib_audit.py                        # third_party_modules: [], 18 files parse at (3,10)
python3 run.py --dry-run "hello"               # prints the request, sends nothing
python3 run.py --paths                         # every resolved path and the rule that chose it
python3 run.py --doctor                        # health report, exit 0 healthy / 1 fixable / 2 human
python3 run.py --doctor --fix                  # repair, then re-check
```

The test suite talks only to `127.0.0.1`: `tests/mock_provider.py` is a scriptable
OpenAI-compatible server built on `http.server`, so the suite runs with no network at all.
The installer is covered the same way (`python3 -m unittest tests.test_install -v`): it
installs from an in-memory zip of this repository, points `--api-base` at the mock, and
asserts the key is validated before it is written, that a rejected key is never saved, and
that a run with no terminal finishes instead of blocking.

## Licensing

This project is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE)
(`SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0`).

**Noncommercial use is 100% free of license fees.** You may use, modify, and
redistribute the software for the purposes permitted by the license, subject to
its terms, including preservation of the required license information and notices.
The license also expressly permits use by charitable organizations, educational
institutions, public research organizations, public safety or health organizations,
environmental protection organizations, and government institutions, regardless of
their funding sources or obligations resulting from that funding.

Commercial uses outside those permissions, including commercial repackaging or
resale outside the permitted purposes, require a separate paid commercial agreement
before use. Contact the maintainer through the
[project repository](https://github.com/unkloud/pyto-agent) to discuss commercial
licensing. No commercial license is granted by this README.

Free use refers to this project's license fee; Pyto and model-provider charges are
separate. This summary does not replace or modify the [license terms](LICENSE).
