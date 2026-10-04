# An agent harness living inside Pyto (iOS) — design notes

**Goal:** the user talks to an agent on their iPhone/iPad; the agent writes small Python programs that automate things *for them*, on-device, inside [Pyto](https://pyto.readthedocs.io/).

Companion code: [`pyto-harness/`](pyto-harness/) — stdlib-only, Python 3.10, runs off-device for testing.
Full capability research with 112 citations: [`.scratch/pyto/research/pyto-capabilities.md`](.scratch/pyto/research/pyto-capabilities.md).

Anchor: **Pyto 19.0.1, CPython 3.10, last update 2024-06-09** ([iTunes lookup](https://itunes.apple.com/lookup?id=1436650069&country=us)), source [ColdGrub1384/Pyto](https://github.com/ColdGrub1384/Pyto).

---

## 1. What iOS/Pyto actually allows

### Works

| Capability | How | Source |
|---|---|---|
| **A Shortcut can run a script headlessly** | `Run Script` / `Run Code` / `Run Command` actions; with **“Show Console” off** the script runs in the background **inside the main app process** (which holds the increased-memory-limit entitlement), and the action returns immediately | [automation](https://pyto.readthedocs.io/en/latest/automation.html), [RunScriptIntentHandler.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/Handlers/RunScriptIntentHandler.swift) |
| **Getting the result back** | Print to stdout; the app writes `ShortcutOutput.txt` into app-group `group.pyto`; the `Get Script Output` action waits and returns it as `console.txt` (plus matplotlib figures as PNGs), and notifies on failure | [GetScriptOutputIntentHandler.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/Handlers/GetScriptOutputIntentHandler.swift) |
| **Feeding input in** | `sys.stdin` (the “sys.stdin” parameter, honored when Show Console is off), `Arguments` → `sys.argv`, `Attachments` → `pasteboard.shortcuts_attachments()` | [automation](https://pyto.readthedocs.io/en/latest/automation.html) |
| **Pyto driving other apps / Shortcuts** | `xcallback.open_url` and `sharing.open_url`, e.g. `shortcuts://x-callback-url/run-shortcut?name=…&input=text&text=…` (x-success returns a `result` param), plus an `apps` module with 100+ app integrations | [Apple](https://support.apple.com/guide/shortcuts/use-x-callback-url-apdcd7f20a6f/ios), [open_shortcut.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Samples/Examples/open_shortcut.py) |
| **Deep-linking *into* Pyto with code** | `pyto://x-callback/?code=[code]&x-success=[url]&x-error=[url]&x-cancel=[url]` — success passes stdout+stderr as `result`, failure as `errorMessage` | [automation](https://pyto.readthedocs.io/en/latest/automation.html) |
| **Writing the user's calendar directly** | Undocumented but shipped `calendar_events` (`save_event`, `get_events`, `remove_event`) | [calendar_events.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/calendar_events.py) |
| **Native iOS surfaces** | `notifications`, `photos` (pick/take/save), `speech` (TTS), `sound`, `music`, `location`, `motion`, `pasteboard` (foreground only), `pyto_ui` (UIKit widgets), `widgets`, `watch`, `multipeer`, plus an **ObjC bridge to ~100 frameworks** (`import EventKit`, `Security`, `WebKit`, …) | [library index](https://pyto.readthedocs.io/en/latest/library/index.html), [Objective-C](https://pyto.readthedocs.io/en/latest/Objective-C.html) |
| **Staying alive in the background** | Only via `background.BackgroundTask(id=…)`, which loops a bundled silent audio file (`UIBackgroundModes` = audio/fetch/processing). Its docstring explicitly targets Shortcuts Personal Automations | [background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py) |
| **Reading files outside the container** | One-time user grant via `file_system.FileBookmark` / `FolderBookmark` / `import_file` (security-scoped, persistable) | [external](https://pyto.readthedocs.io/en/latest/external.html) |

### Does not work (design around these)

| Limit | Consequence |
|---|---|
| **No real processes.** `subprocess.Popen` is an in-process synchronous shim; `kill()`/`terminate()` are empty no-ops; `os.fork()` is a stub; `os.waitpid` returns `(-1, 0)`; no `multiprocessing` | **No kill-based timeouts, no isolation, no supervisor pattern.** Run generated programs with `runpy` in-process and keep them short ([_ios_popen.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/_ios_popen.py), [Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py)) |
| **Memory watchdog** | Pyto polls `os_proc_available_memory()` and **stops all running scripts** when free memory hits ~500 MB. Cap context, tool output and the session log; check before big writes ([MemoryManager.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/MemoryManager.swift)) |
| **No compiled packages** | pip installs must be pure Python; NumPy/Pandas/SciPy/OpenCV are bundled and behind a paid IAP. The harness is stdlib-only for this reason ([faq](https://pyto.readthedocs.io/en/latest/faq.html)) |
| **The shell is not bash** | The embedded hterm shell is a Python shell over [ios_system](https://github.com/holzschu/ios_system); commands (`curl`, `ssh`, `tar`, `grep`, `sed`, `awk`, …) run **in-process and block**. No `git`, `ffmpeg`, `wget`, `make`; no pipes to external processes ([terminal](https://pyto.readthedocs.io/en/latest/terminal.html)) |
| **No clipboard in the background** | Clipboard tools are foreground-only (`Lib/background.py`) |
| **No Reminders / HealthKit / Bluetooth modules** (missing `Info.plist` usage strings) | Route those through a user-built Shortcut via `shortcut_run` |
| **No secure keychain module** | `userkeys` is plain NSUserDefaults — not for API keys. Keep the key in a container file and be explicit about the risk |
| **Python is frozen at 3.10** | No 3.11+ syntax or stdlib |
| **Interrupts are cooperative** | Pyto injects exceptions per thread at bytecode boundaries; a thread blocked in a C call/DNS/socket ignores them |
| **No daemon, no cron** | Shortcuts automations are the scheduler; nothing runs while suspended |

---

## 2. Architecture

```
        ┌──────────────────────────── iPhone / iPad ────────────────────────────┐
        │                                                                       │
  Shortcuts automation ──"Run Script"──►  run.py  ──►  harness loop            │
  (time / alarm / NFC / app /          (Show Console      │                     │
   Wi-Fi / focus / battery)             off → bg)         │                     │
        │                                    │             ▼                    │
        │  "Get Script Output" ◄─────────────┼──── LLM over HTTPS + SSE (stdlib)│
        │  ← console.txt / stdout            │             │                    │
        ▼                                    │             ▼                    │
  Notify / Speak / Show ◄────────────────────┘      26 tools, two classes       │
                                                     auto: write_program, run_program, list/read/write/edit/
                                                           search_files, clipboard_get*, memory_read/write,
                                                           memory_status, calendar_list_events,
                                                           device_capabilities, finish
                                                     ask:  clipboard_set*, share_text, open_url, notify, speak,
                                                           shortcut_run(+_wait), save_photo, open_in_files,
                                                           calendar_add_event, keepalive_start/stop
                                                     (* clipboard is foreground-only)
```

Entry points implemented and tested:

- `python run.py "task"` — one-shot from the Pyto console or Files.
- `python run.py --resume <session>` — continue after iOS suspended the app.
- `pyto://python/<path to run.py>?task=<url-encoded task>` — how a Shortcut (or any app) starts it.
- `PYTO_HARNESS_TASK="…"` in the environment — same thing without a URL.
- Shortcuts → **Run Script**, input on `sys.stdin`, **Show Console off**, output read back from **stdout** by **Get Script Output**.
- `--ui` for the `pyto_ui` chat window (falls back with a clear message when `pyto_ui` is absent).

Rules that follow from the platform:

1. **Sessions must be resumable.** iOS suspends or kills the app mid-conversation; every turn is appended to a JSONL log so `--resume` continues where it stopped, with hard size caps to respect the memory watchdog.
2. **Prefer producing an artifact over performing a one-off action.** When a task is repeatable, the agent writes a `.py` into the workspace — the user can then bind it to a Shortcut and get it forever.
3. **Approvals on the irreversible.** Workspace file ops auto-allow; `share_text`, `open_url`, `shortcut_run`, `save_photo`, calendar writes and anything that sends data outward prompt first (`--yolo` to skip).
4. **Programs are cooperative citizens.** The model is told: no subprocess, no long blocking calls, no infinite loops, keep output bounded — because nothing can be killed.
5. **The model must know where it is.** The system prompt states the sandbox, the workspace path, the lack of a daemon, the available iOS actions, and the "write a program, don't just do it once" preference.
6. **The model must know Pyto's API, not guess it.** Pyto's modules are the whole point — they are the only way to reach the clipboard, photos, the calendar, notifications, the share sheet, Shortcuts and a real UIKit window — but the API is small, version-specific and easy to misremember (`photos.save_image` is real; `photos.save_photo` is not). So the harness ships a **library-grounding layer**: a curated catalogue (25 modules / 160 members, each marked verified or not against Pyto's docs and source) merged with **on-device introspection** (`dir()`, `inspect.signature`, docstrings), exposed as the read-only `pyto_api` tool and as a generated `PYTO_LIBS.md` cheat-sheet in the workspace. The prompt requires consulting it before importing a Pyto module, and when a generated program fails with `AttributeError`/`ImportError` on a Pyto name, `run_program` appends the closest real member names instead of letting the model guess again.

---

## 3. Wiring it up on the device

**Shortcuts → agent** (the primary trigger):

1. Shortcuts → Automation → *Time of Day* / *Alarm* / *NFC* / *App Open* / *Wi-Fi* → **Ask Before Running off**.
2. Action **Run Script** → `run.py`; put the prompt in the `sys.stdin` field; **Show Console off**.
3. Action **Get Script Output** → the agent's final message (whatever `run.py` printed last).
4. Action **Show Notification** / **Speak Text** / **Append to Note** → deliver it.

**Agent → iOS** (three routes, cheapest first):

1. Direct modules: `notifications`, `photos`, `speech`, `calendar_events`, `pasteboard`, `sharing`.
2. `shortcut_run` / `shortcut_run_wait` → any Shortcut the user built (`shortcuts://x-callback-url/run-shortcut?name=…`) — this covers Reminders, Health, Home, and third-party apps.
3. `open_url` → app URL schemes (`xcallback`/`apps` modules).

**Deep-linking a task** without touching Shortcuts: `pyto://x-callback/?code=<runpy…>` from any app that can open URLs.

---

## 4. What this cannot do (say it plainly)

- No always-on agent: nothing runs while suspended; Shortcuts automations are the substitute, and a background run is best-effort.
- No arbitrary binaries, no PTY, no fork — only in-process `ios_system` commands.
- No compiled dependencies; NumPy/Pandas are the frozen bundled versions behind an IAP.
- No kill switch for generated code: a blocked program blocks the run until Pyto's watchdog or the user stops it.
- Background execution via `BackgroundTask` is a silent-audio hack and a known App Review gray area ([2.5.4](https://developer.apple.com/app-store/review/guidelines/)) — treat it as a personal-device feature, not a product foundation.
- The clipboard, and therefore clipboard-driven flows, only work in the foreground.

---

## 5. Self-diagnosis and self-repair

The harness can diagnose itself after the API key is set and fix what a machine can fix. Details and the full guardrail list: [`pyto-harness/SELF-REPAIR.md`](pyto-harness/SELF-REPAIR.md).

Three tiers, deliberately kept separate:

| Tier | What it covers | Who acts | Examples |
|---|---|---|---|
| **1. Auto-fixable state** | deterministic, bounded, reversible | the harness, no model needed | create the workspace/session dirs, `chmod 600` the config, repair a torn session-log tail (fragment kept as `.torn`), quarantine a corrupt log, compact an oversized log, re-probe `api_base` when it 404s (`…/v1` variants), fall back to an accepted model, write `SHORTCUTS.md` |
| **2. Model-authored code repair** | the harness editing *its own source* | the agent, gated | `--repair "<problem>"` or the `self_edit` tool: snapshot with SHA-256 → `ast.parse` at 3.10 → run a bounded covering test subset → **promote only if green, else restore the previous bytes and show the verbatim failure** |
| **3. Human-only** | anything the sandbox forbids | the user, with a plain-language instruction | granting iOS permissions (Photos, Calendar, Notifications), building a Shortcut automation, installing compiled packages, fixing a wrong/expired API key, network/provider outages, free memory below ~500 MB |

Why tier 2 is safe enough to ship: the kit carries **458 offline tests**, so "did I break myself?" is a question with a machine-checkable answer that runs in ~3 s for the bounded gate (the full suite is opt-in, and the gate refuses to edit `tests/` so it cannot be weakened to pass). Measured behaviour: a patch that breaks a test is reverted **byte-for-byte** with the failure quoted verbatim and a pre-edit backup on disk; a patch that passes is promoted with a diff and can be undone with `--restore`.

**iOS caveats that bound this:** there is no fork/exec, so the harness cannot restart itself into the new code — an accepted edit takes effect on the next launch (re-open `run.py`, or trigger it from the Shortcut); the memory watchdog can kill a repair mid-flight, which is why every edit is snapshotted and written atomically (`os.replace`); and no amount of self-repair can add a capability Pyto does not have, so tier 3 always ends with an instruction rather than a fix.

---

## 6. Open questions for the user

1. **Pyto, or something with a real shell?** [a-Shell](https://holzschu.github.io/a-Shell_iOS/) (ios_system commands, Shortcuts actions like Execute Command/Put File/Get File) and [iSH](https://ish.app/) (emulated Alpine, real fork/exec, no URL scheme so not Shortcuts-drivable) trade Pyto's native iOS APIs for a real process model. If the automation is mostly *files and shell*, they are stronger; if it is *iOS-native actions*, Pyto is.
2. **Where does the model run?** On-device harness calling a hosted API (assumed), or a remote box with the phone as UI?
3. **What is the first real automation?** The kit ships three examples; one concrete personal workflow (screenshots, notes, expenses, reminders, travel…) will shape the tool set better than any generic list.
