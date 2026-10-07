# Running an LLM agent harness inside Pyto on iOS — capability report

**Scope.** What an on-device, stdlib-only Python agent harness (HTTP+SSE to an OpenAI-compatible API, tools, JSONL sessions) can and cannot do when hosted by **Pyto IDE** (iOS CPython IDE by Emma Labbé / ColdGrub1384). The agent loop itself is assumed solved; this report is about the **iOS/Pyto runtime surface**.

**Verification method & honesty rules used below.**
- Primary sources only: Pyto's official docs (`pyto.readthedocs.io`), Pyto's own source on the `main` branch of `github.com/ColdGrub1384/Pyto` (fetched 2026-10-04; sparse clone + raw.githubusercontent), the App Store listing / iTunes lookup API, Apple developer & support documentation, and the upstream `ios_system` project that Pyto embeds.
- Every factual claim carries a URL. Direct quotes are in quotation marks.
- Anything I could not confirm from a primary source is marked **NOT VERIFIED** or **INFERRED** (with the evidence the inference rests on). I found **no** Pyto GitHub issues to cite because the tracker is disabled (see §7).
- Version anchor: **Pyto IDE 19.0.1, App Store "current version release date" 2024-06-09, minimum iOS 14.0, 975,650,816 bytes** ([iTunes lookup API](https://itunes.apple.com/lookup?id=1436650069&country=us)); docs header says "Pyto version: 19.0 (424) Python version: 3.10.0+" ([docs index](https://pyto.readthedocs.io/en/latest/)).

---

## 1. Capability matrix

| Capability | Supported? | Module / API | Evidence URL | Design implication |
|---|---|---|---|---|
| CPython 3.10 interpreter, full stdlib, in-process | ✅ YES | built in | [docs index](https://pyto.readthedocs.io/en/latest/) | Target **3.10 syntax/API only**; no 3.11+ features (`tomllib`, `ExceptionGroup`, `asyncio.TaskGroup`) |
| `threading`, queues, `socket`, HTTP client | ✅ YES | stdlib; `threading.Thread` is subclassed by Pyto | [scripts_runner.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/scripts_runner.py) | Agent loop + SSE reader on threads is fine |
| `pip install` pure-Python wheels | ✅ YES ("a minimal version of pip") | PyPI tab / installer | [Welcome to Pyto](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Welcome%20to%20Pyto.md) | Prefer stdlib-only; vendor pure-Python deps |
| `pip install` C-extension wheels from PyPI | ❌ NO | — | [FAQ](https://pyto.readthedocs.io/en/latest/faq.html), [third_party](https://pyto.readthedocs.io/en/latest/third_party.html) | No `numpy`-dependent package installs; design stdlib-only |
| Import pre-bundled compiled packages (numpy, pandas, scipy, cv2, lxml, cryptography…) | 💰 PAID (Full Version IAP) | `extensionsimporter` | [App Store listing](https://apps.apple.com/us/app/pyto-ide/id1436650069) | Must degrade gracefully when the upgrade isn't bought |
| Compile simple C extensions in-app (clang → LLVM IR → `llvm-link` → import) | ⚠️ PARTIAL / weakly documented | `clang`, `llvm-link`, `lli`, `setuptools`, `Lib/_clang.py`, `build_cproj` | [docs/llvm.rst](https://github.com/ColdGrub1384/Pyto/blob/main/docs/llvm.rst) (published `llvm.html` **404s**) | Nice-to-have only; do not make the harness depend on it |
| **Real subprocesses** (`subprocess.Popen`, fork/exec) | ❌ **NO** — patched to run **in-process, synchronously**; `kill()`/`terminate()` are no-ops | `_ios_popen.Popen` | [_ios_popen.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/_ios_popen.py) | Never spawn helper processes; run tools on threads; there are **no timeouts you can enforce by killing** |
| `os.fork` / `os.waitpid` | ❌ NO — `fork()` is a silent no-op, `waitpid` returns `(-1, 0)` | `Pyto/Startup.py` | [Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py) | `multiprocessing` is unusable (**INFERRED** from the fork stub); use threads |
| `os.system("cmd")` | ⚠️ PARTIAL — in-process `ios_system` dispatch, blocking | `_extensionsimporter.system` | [terminal docs](https://pyto.readthedocs.io/en/latest/terminal.html) | Fine for short commands; not a process boundary |
| Real POSIX shell binary (`sh`/`bash`) | ❌ NO — Python shell derived from StaSh | `Lib/_shell/` | [App Store command list](https://apps.apple.com/us/app/pyto-ide/id1436650069), [ios_system README](https://github.com/holzschu/ios_system) | Don't assume shell semantics; call commands directly |
| Bundled UNIX commands (`curl`, `ssh`, `scp`, `tar`, `grep`, `sed`, `awk`, `clang`, `lli`, `nc`, `ping`…) | ✅ YES (in-process) | `ios_system` | [App Store listing](https://apps.apple.com/us/app/pyto-ide/id1436650069) | No `git`, `ffmpeg`, `wget`, `make`, `jq`, `unzip` (use `tar -xz`) |
| Long-running child process that outlives the call | ❌ NO | — | [_ios_popen.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/_ios_popen.py) | Long jobs must be in-process threads/tasks inside Pyto |
| Keep running when backgrounded / screen locked | ✅ YES — indefinite **while silent audio plays** | `background.BackgroundTask` | [Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py), [Info.plist `UIBackgroundModes`](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist) | **This is the only viable "always-on" host** for a persistent agent |
| OS-scheduled refresh | ⚠️ YES but unreliable, **≤30 s** per run | `background.request_background_fetch()` (BGAppRefresh) | [Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py) | Do **not** build the agent loop on this |
| Run a Pyto script from Shortcuts **without** opening the app | ✅ YES (`Show Console` = off) | App Intents: `Run Script`, `Run Code`, `Run Command` | [automation docs](https://pyto.readthedocs.io/en/latest/automation.html) | Primary automation entry point |
| Get script output back into Shortcuts | ✅ YES (poll app-group file) | `Get Script Output` action | [Intents intentdefinition](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Base.lproj/Intents.intentdefinition) | Return results as text on stdout; images returned as PNG files |
| Pass input *into* a Shortcut-triggered script | ✅ YES: `sys.stdin`, `Arguments`, `Attachments` | `pasteboard.shortcuts_attachments()` | [automation docs](https://pyto.readthedocs.io/en/latest/automation.html) | Feed the prompt/JSON via `sys.stdin` |
| Third-party app runs Pyto **code** and gets stdout back | ✅ YES (code only, not a script file) | `pyto://x-callback/?code=…&x-success=…` | [automation docs](https://pyto.readthedocs.io/en/latest/automation.html) | Cheap "run this snippet" RPC |
| Pyto calls a Shortcut and **reads its result** | ✅ YES | `xcallback.open_url` + `shortcuts://x-callback-url/run-shortcut` | [shipped sample](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Samples/Examples/open_shortcut.py), [Apple](https://support.apple.com/guide/shortcuts/use-x-callback-url-apdcd7f20a6f/ios) | The agent's escape hatch to iOS capabilities Pyto lacks (Reminders, Health, …) |
| Open URLs / drive other apps | ✅ YES | `sharing.open_url`, `apps.*`, `webbrowser` | [Lib/sharing.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/sharing.py), [apps docs](https://pyto.readthedocs.io/en/latest/library/apps.html) | 100+ app integrations (Things, OmniFocus, Drafts, Fantastical…) |
| Shortcuts **personal automations** (time, NFC, app open, focus, Wi-Fi…) trigger the agent | ✅ YES (with `Ask Before Running` off) | automation → `Run Script` (Show Console off) → `BackgroundTask` | [Apple automation doc](https://support.apple.com/guide/shortcuts/apd602971e63/7.0/ios/17.0), [background.py docstring](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py) | Best-effort scheduling; user must configure; no true cron |
| Memory | ⚠️ App has `increased-memory-limit`; Pyto **kills scripts** when free memory ≤ ~500 MB | `MemoryManager` + `os_proc_available_memory()` | [MemoryManager.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/MemoryManager.swift), [AppDelegate.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/UI/Life%20Cycle/AppDelegate.swift) | Budget a few hundred MB; stream/limit context and JSONL growth |
| App sandbox + iCloud "Pyto" folder, visible in Files | ✅ YES | `UIFileSharingEnabled`, iCloud container | [Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist), [external files docs](https://pyto.readthedocs.io/en/latest/external.html) | Sessions/JSONL live in `~/Documents`; user can reach them in Files |
| Read/write **other apps'** files | ✅ with one-time user grant (security-scoped bookmark) | `file_system.FileBookmark`, `FolderBookmark`, `import_file`, `pick_directory`, `save_as` | [Lib/file_system.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/file_system.py) | Ask the user once to pick a folder; persist the bookmark |
| Clipboard (pasteboard) | ✅ foreground only | `pasteboard` | [pasteboard docs](https://pyto.readthedocs.io/en/latest/library/pasteboard.html), [background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py) | **Unavailable in background** — not a valid I/O channel for a background agent |
| Notifications (immediate + scheduled + actions) | ✅ YES | `notifications` | [notifications docs](https://pyto.readthedocs.io/en/latest/library/notifications.html) | Use as the "agent needs attention / finished" channel |
| Photos & camera (read + **write to library**) | ✅ YES | `photos.pick_photo`, `take_photo`, `save_image` | [Lib/photos.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/photos.py) | Agent can act on the user's photo library |
| Location, motion sensors (read-only) | ✅ YES | `location`, `motion` | [Lib/location.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/location.py), [Lib/motion.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/motion.py) | Sensor context available; pedometer/activity **not** (no `NSMotionUsageDescription`) |
| Text-to-speech (acts on user) | ✅ YES | `speech.say`, `wait`, `get_available_languages` | [Lib/speech.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/speech.py) | Spoken output channel |
| Sound playback / Apple Music library | ✅ YES | `sound`, `music` | [Lib/sound.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/sound.py), [Lib/music.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/music.py) | Audio feedback; Music needs permission |
| Calendar events (read + create/delete) | ✅ YES — **undocumented module** | `calendar_events.get_events/save_event/remove_event` (EventKit) | [Lib/calendar_events.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/calendar_events.py), [Info.plist `NSCalendarsUsageDescription`](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist) | Agent can create real calendar events without Shortcuts |
| Reminders / HealthKit / Bluetooth / Contacts module | ❌ NO first-class module (HealthKit & CoreBluetooth also lack required Info.plist usage strings) | — | [Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist) | Route through a Shortcut the user builds |
| Keychain (iOS Keychain, not `userkeys`) | ⚠️ via raw `Security` framework only | `from Security import …` | [Objective-C docs](https://pyto.readthedocs.io/en/latest/Objective-C.html) | Prefer `userkeys` (NSUserDefaults, plaintext) for tokens, or Shortcuts-managed secrets |
| Raw Objective-C bridge (Rubicon) over ~100 frameworks | ✅ YES | `import Foundation`, `UIKit`, `EventKit`, `Contacts`, `Security`, `WebKit`, `Vision`, `Speech`… | [Objective-C docs](https://pyto.readthedocs.io/en/latest/Objective-C.html) | Ultimate escape hatch; but Info.plist permissions and entitlements still gate access |
| Build native UI (UIKit widgets, tables, web view) | ✅ YES | `pyto_ui` | [pyto_ui docs](https://pyto.readthedocs.io/en/latest/library/pyto_ui.html) | Agent can build native interfaces in generated programs; harness chat uses the browser UI |
| Home-screen widgets / Apple Watch complications | ✅ YES | `widgets`, `watch` | [widgets docs](https://pyto.readthedocs.io/en/latest/library/widgets.html) | Status surface; widget runs under a **tight RAM budget** unless the "Start Handling Widgets In App" intent is used |
| Peer-to-peer LAN transport | ✅ YES | `multipeer` | [Lib/multipeer.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/multipeer.py) | Optional device-to-device channel |
| Official LLM/OpenAI/Copilot examples | ❌ NONE | — | [Pyto/Samples/Examples](https://github.com/ColdGrub1384/Pyto/tree/main/Pyto/Samples/Examples) | You own the HTTP/SSE client entirely |

---

## 2. Q1 — Python runtime, packages, install locations

**Version.** "Pyto version: 19.0 (424) Python version: 3.10.0+" ([docs index](https://pyto.readthedocs.io/en/latest/)). The interpreter is a prebuilt binary: "The Python 3.10 binary that comes with the app is from the [Python-Apple-support](https://github.com/beeware/Python-Apple-support/tree/3.10) project by beeware." The 3.10 runtime is reproducible in-app: `python` prints `Python 3.10.0 (default, Apr 20 2022, 13:28:58) [Clang 13.1.6 …] on ios` (visible in Pyto's own screenshot fixture, [previewConsole.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/UI/Screenshots/previewConsole.swift)).

**Can the interpreter be updated?** ❌ No. CPython is compiled into the app binary: "The app uses the Python C API to run Python code in the same process of the app, due to iOS restrictions." ([docs index](https://pyto.readthedocs.io/en/latest/)). Updating Python means the developer shipping a new app build — the last App Store update was **2024-06-09** ([iTunes API](https://itunes.apple.com/lookup?id=1436650069&country=us)).

**pip: pure Python ✅.** "Pyto has a minimal version of pip." ([Welcome to Pyto](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Welcome%20to%20Pyto.md)). Packages install from the in-app "PyPI" tab. Installed console scripts land in the terminal `$PATH` too: "Scripts installed from PyPI or packages embedded in-app will also be recognized by the shell. These scripts are installed in `~/Documents/bin` for user installed scripts and `Pyto.app/site-packages/bin` for bundled packages." ([terminal docs](https://pyto.readthedocs.io/en/latest/terminal.html)).

**pip: C extensions ❌.** "Pyto cannot compile modules and cannot link shared libraries from outside the app bundle. So `pip` will fail for packages like `pandas`, `scipy` or `numpy`." ([Welcome to Pyto](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Welcome%20to%20Pyto.md)); "It's not possible to install them at runtime because iOS doesn't allow loading compiled plugins not included by the developer." ([third_party](https://pyto.readthedocs.io/en/latest/third_party.html)); "Some libraries contain native code (C extensions). They cannot be installed because iOS / iPadOS apps must be self contained." ([FAQ](https://pyto.readthedocs.io/en/latest/faq.html)).

**Prebuilt C-extension packages are bundled but paywalled.** The App Store description lists under "* Full version exclusive third party modules *": "Cython, cryptography, typed_ast, cv2, _cffi_backend, kiwisolver, matplotlib, numpy, pandas, lxml, Bio, sklearn, skimage, scipy, erfa, pywt, nacl, bcrypt, statsmodels, zmq, regex, gensim, astropy, emd, wasm3, yaml" ([App Store](https://apps.apple.com/us/app/pyto-ide/id1436650069)). The importer raises an upgrade prompt: "Upgrade to import C extensions {price}" with `url="pyto://upgrade"` ([extensionsimporter.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/extensionsimporter.py)). Pricing on the vendor site: "$7.99 / $14.99 / 3-day Trial" ([pyto.app](https://pyto.app/)).

**There IS a build toolchain — with a broken doc link.** Pyto bundles `clang`, `llvm-link` and `lli` ([App Store command list](https://apps.apple.com/us/app/pyto-ide/id1436650069)). The repo's LLVM page says: "there is limited support for C and C++ code compilation via `clang` and interpretation via `lli`" and gives a worked C-extension recipe (`clang -S -emit-llvm mycext.c -o mycext.o`, glue with `~/Documents/lib/cext_glue.c`, `llvm-link … -o mycext.cpython-310-darwin.so`, then `import mycext`), adding "Compilation and importation of Python C extensions is also possible, and should work normally with `setuptools`. … `setuptools` will automatize this process. Cython is also supported." ([docs/llvm.rst](https://github.com/ColdGrub1384/Pyto/blob/main/docs/llvm.rst)). ⚠️ **The published page is missing**: `https://pyto.readthedocs.io/en/latest/llvm.html` returns **404** (verified) even though `projects.html` links to it as "LLVM Environment" ([projects docs](https://pyto.readthedocs.io/en/latest/projects.html)). Whether a *non-trivial* C extension (e.g. one needing BLAS/Fortran) actually builds in-app is **NOT VERIFIED** — and note the contradiction with the Welcome doc's flat "Pyto cannot compile modules".

**Where installed packages live.** `site.USER_SITE` is redirected to the app sandbox: `~/Documents/lib/python3.10/site-packages`, with legacy `~/Documents/site-packages` migrated on first launch, and stdlib at `$APP/site-packages/python3.10` ([Pyto/Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py)). The script runner also injects `~/Documents/site-packages` into `sys.path` per run ([Lib/console.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/console.py) line ~455).

**Design implication.** Write the harness **stdlib-only** (as you already have). Optionally *detect* numpy/pandas and use them if the user paid, but never require them.

---

## 3. Q2 — Process model

**Everything is one process.** Pyto does not spawn processes; it dispatches commands into an in-process C library. "The terminal embedded in Pyto is hterm… It runs a shell that provides access to many UNIX commands from ios_system… **These commands are embedded in the app and are executed in the same process and can be executed with `os.system()` and `subprocess.Popen`.**" ([terminal docs](https://pyto.readthedocs.io/en/latest/terminal.html)).

**`subprocess.Popen` is monkeypatched.** At startup: `os.allows_subprocesses = True` then `subprocess.Popen = with_docstring(_ios_popen.Popen, subprocess.Popen.__doc__)` ([Pyto/Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py)). The replacement module's own docstring: "Support for running Python scripts with 'subprocess.Popen' when the program is 'sys.executable'." ([_ios_popen.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/_ios_popen.py)). Behaviour, read from source:

- If `argv[0]` is `sys.executable`/`"Pyto"`/`"python"`, it imports and runs `_shell.bin.python.main()` **inside the current interpreter**, swapping `sys.stdout`/`sys.stdin`/`sys.argv`, then restores them — i.e. a blocking, in-process call, not a child process.
- Any other command is re-entered as `python -m _system <cmd>` → `_extensionsimporter.system(cmd)` → `ios_system`.
- `kill()` and `terminate()` are **empty function bodies**; `wait(timeout)` immediately returns `self.returncode`; `communicate()` reads back in-memory buffers.

**`os.fork` is neutered.** From [Pyto/Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py):

```python
def getpgid():
    raise OSError()

def fork():
    pass

def waitpid(pid, options):
    return (-1, 0)

os.getpgid = getpgid
os.fork = fork
os.waitpid = waitpid
os._exit = sys.exit

os.system = system
```

So `os.fork()` silently returns `None` (a classic `if pid == 0:` child branch never runs, and the parent continues double-executing logic). `os._exit` raises `SystemExit` instead of exiting. `os.system` is the in-process `ios_system` dispatcher. **Consequence: `multiprocessing` cannot work** — POSIX default start method is fork. *This specific failure mode is INFERRED from the stubs, not observed at runtime on a device; treat as high-confidence but unverified.*

**`threading` works and is instrumented.** Pyto replaces `threading.Thread` with a subclass that registers `script_path` and calls `Python.shared.handleCrashesForCurrentThread()` ([scripts_runner.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/scripts_runner.py)). Scripts are stopped by injecting exceptions into threads via `stopit.async_raise` (`SystemExit`, `KeyboardInterrupt`, or `MemoryError`) — i.e. interruption only lands at Python bytecode boundaries, so a thread blocked in a C call (DNS, a long `ios_system` command, a wedged socket read) may not be interruptible.

**Which executables are bundled.** Pyto's App Store description enumerates its built-in UNIX commands: "alias, awk, cat, chflags, chmod, cksum, clang, compress, cp, curl, date, diff, dig, du, egrep, env, fgrep, find, grep, gunzip, gzip, head, host, ifconfig, link, lli, llvm-link, ln, ls, md5, mkdir, mv, nc, nslookup, open, openurl, pbcopy, pbpaste, ping, printenv, pwd, readlink, rlogin, rm, rmdir, say, scp, sed, sftp, sort, ssh, ssh-keygen, stat, sum, tail, tar, tee, telnet, touch, tr, unalias, uname, uncompress, uniq, unlink, uptime, wc, whoami, whois, wol" ([App Store](https://apps.apple.com/us/app/pyto-ide/id1436650069)). Notably **absent: `git`, `ffmpeg`, `wget`, `make`, `gcc`, `unzip`, `sh`, `bash`, `jq`, `rsync`.** Upstream confirms the intent: "`sh`, `bash`, `zsh`: shells are hard to compile, even without the sandbox/API limitations. They also tend to take a lot of memory, which is a limited asset." and "`git`: [WorkingCopy](https://workingcopyapp.com) does it very well… Also difficult to compile." ([ios_system README](https://github.com/holzschu/ios_system)). Upstream also warns command authors away from "`fork`, `exec`, `system`, `popen`… (some of these fail at compile time, others fail silently at run time)".

**Is there a real shell?** No binary shell. Pyto ships a **Python-implemented shell derived from StaSh** (`Lib/_shell/shell.py` + `Lib/_stash/`), plus 10 Python "binaries" (`cd, clear, echo, env, exit, export, help, python, which, xargs`) — [Lib/_shell/bin](https://github.com/ColdGrub1384/Pyto/tree/main/Lib/_shell/bin). The terminal UI is hterm (Chrome OS terminal) ([terminal docs](https://pyto.readthedocs.io/en/latest/terminal.html)). Redirection/pipe operators are supported per the docs but this is emulation, not a kernel.

**Can a script spawn a long-running child?** ❌ **No.** Every "child" is a synchronous in-process call with no kill switch. Long-running work must be a **thread or task inside Pyto**, and the only way it survives backgrounding is `background.BackgroundTask` (§4).

---

## 4. Q3 — App lifecycle, background execution, memory

**Normal suspension.** Pyto is a normal iOS app and normally suspends: "When the user exits a foreground app, that app moves to the background state briefly before UIKit suspends it… When your app is in the background, it should do as little as possible, and preferably nothing." ([Apple: Preparing your UI to run in the background](https://developer.apple.com/documentation/uikit/preparing-your-ui-to-run-in-the-background)). Without one of Pyto's escape hatches, a running script stops when the app is suspended.

**Escape hatch 1 — silent-audio background task (the important one).** Pyto declares `UIBackgroundModes` = **`audio`, `fetch`, `processing`** ([Pyto/Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist)) and exposes:

> "Run code in background indefinitely. This module allows you to keep running a script in the background indefinitely." — [Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py)

> "Represents a task to run in background. **When started, the audio at the path passed to the initializer is played. If no audio is passed, a blank audio is used so Pyto isn't killed by the system.**" — `background.BackgroundTask` docstring

Implementation: `playAudio()` sets `AVAudioSession` category `.playback` with `.mixWithOthers`, loads `blank.wav` from the bundle and sets `player.numberOfLoops = -1` ("Play audio forever by setting num of loops to -1") ([BackgroundTask.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/Background/BackgroundTask.swift)). While at least one task is active (`BackgroundTask.count > 0`) the player keeps running and the app is not suspended. By default it posts a reminder notification every 6 hours (`delay: Double = 3600*6`) — disable with `reminder_notifications = False` or change with `notification_delay`.

**Caveats to design around:**
- It is an **audio session**: any interruption (a call, another app taking the session, the user pressing play on media) can stop playback; Pyto re-plays on `AVAudioSession.interruptionNotification` but only for interruption type 1.
- It is a **gray area under App Review**: "Multitasking apps may only use background services for their intended purposes: VoIP, audio playback, location, task completion, local notifications, etc." ([App Review Guidelines 2.5.4](https://developer.apple.com/app-store/review/guidelines/)). It ships in Pyto today, so it works — but it is not a contract Apple guarantees.
- Battery drain, and the user sees a notification banner periodically unless disabled.
- Clipboard is unavailable: "Because of privacy, apps cannot access to the clipboard in background, so coding a clipboard manager is not possible." (same file).

**Escape hatch 2 — BGAppRefresh, ~30 s, unreliable.**

> "This function is used to start fetching information in background. It tells the system to execute the script from which the function is called multiple times a day. **The OS decides when it's appropiate to perform the fetch, so it's pretty unreliable. The script cannot take more than 30 seconds to execute.**" — `background.request_background_fetch()` docstring

Backed by `BGAppRefreshTaskRequest(identifier: "pyto.backgroundfetch")`, `earliestBeginDate = now + 60` ([BackgroundTask.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/Background/BackgroundTask.swift)). Apple on BGTaskScheduler generally: "the system decides the best time to launch your background task", and for `beginBackgroundTask`-style work "The system grants your app a limited amount of time… The system terminates your app if you fail to call this method." ([Apple: Choosing background strategies](https://developer.apple.com/documentation/backgroundtasks/choosing-background-strategies-for-your-app)). Use this at most as a "wake up and poll once" tick, never as the agent loop.

**Escape hatch 3 — `Start Handling Widgets In App` intent.** "Starts running the app in background indefinitely so all widgets are executed in app. That removes the RAM limit of the widgets and they can use libraries like Numpy or Pandas." ([Intents.intentdefinition](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Base.lproj/Intents.intentdefinition)). This is a second documented "run in background indefinitely" switch, aimed at widgets.

**Memory limits.** Apple's API returns the actual headroom: `os_proc_available_memory()` "Determines the amount of memory available to the current app… The number of bytes the app may allocate before it hits its memory limit… Use the returned value as advisory information only and don't cache it." ([Apple docs](https://developer.apple.com/documentation/os/os_proc_available_memory)). Pyto polls it every 0.01 s and acts when the budget is nearly gone:

> "I don't use the app's memory warnings because it's sent too soon for scripts to stop when the memory doesn't stop to increase. **When the usable memory is equal or smaller than 300MB, the listener calls the cleanup function.**" — comment in [MemoryManager.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/MemoryManager.swift) (the code actually uses `leftLimit = 500` for the main app build, `0.0` for widgets).

The cleanup is brutal but graceful-ish: `Python.shared.tooMuchUsedMemory = true` and **every running script is stopped** ([AppDelegate.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/UI/Life%20Cycle/AppDelegate.swift)); the script sees `MemoryError` rather than silent death ([scripts_runner.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/scripts_runner.py) `exitScript_`). So the practical budget is **several hundred MB free**, not Apple's absolute per-device jetsam number (which Apple does not publish — **NOT VERIFIED**, no primary source exists).

**Pyto raises its own ceiling.** Entitlements include `com.apple.developer.kernel.increased-memory-limit` and `com.apple.developer.kernel.extended-virtual-addressing` ([Pyto.entitlements](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Pyto.entitlements)). Apple: "Add this entitlement to your app to inform the system that some of your app's core features may perform better by exceeding the default app memory limit on supported devices… An increased memory limit is only available on some device models." ([Apple docs](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.kernel.increased-memory-limit)). **The Intents extension does NOT have this entitlement** (only `app-sandbox`, `application-groups: group.pyto`, `network.client` — [Pyto Intents.entitlements](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/Pyto%20Intents.entitlements)); the *script execution* triggered from Shortcuts runs in the **main app** process (see §6), so it inherits the higher limit, while widget/`Get Script Output` work runs in the tighter extension.

**Design implication.** Serialize everything into one process; keep one `BackgroundTask` alive with a stable `id`; cap context size, JSONL rotation, and any in-memory buffers; treat `MemoryError` as a recoverable "compact and retry" signal; never assume you can reap a wedged thread.

---

## 5. Q4 — Native iOS APIs available from Python

### 5.1 First-class Pyto modules (documented, stable)

Source of truth: [Pyto Libraries index](https://pyto.readthedocs.io/en/latest/library/index.html) and the module sources in [`Lib/`](https://github.com/ColdGrub1384/Pyto/tree/main/Lib).

| Module | Key API | Read / Act |
|---|---|---|
| `pyto_ui` | UIKit widget tree, [`pyto_ui` docs](https://pyto.readthedocs.io/en/latest/library/pyto_ui.html) | **Act** (renders UI) |
| `widgets` | `Widget`, `Text`, `save_widget`, layouts, `Reload Widgets` | **Act** (home screen) |
| `watch` | `ComplicationsProvider`, `Complication`, `reload_complications` | **Act** (watch face) |
| `file_system` | `import_file`, `pick_directory`, `open_directory`, `save_as`, `share_text`, `share_files`, `quick_look`, `FileBookmark`, `FolderBookmark` | **Read+Act** (share sheet, Files) |
| `notifications` | `Notification`, `schedule_notification(n, delay, repeat)`, `send_notification`, `cancel_all`, `get_pending_notifications`, `remove_delivered_notifications` | **Act** |
| `remote_notifications` | remote/push notification handling | **Read** |
| `background` | `BackgroundTask`, `request_background_fetch` | **Act** (keeps app alive) |
| `music` | Apple Music library, `NowPlaying`, pickers ([docs](https://pyto.readthedocs.io/en/latest/library/music.html)) | **Read + Act** |
| `photos` | `pick_photo()`, `take_photo()`, `save_image(img)` | **Read + Act (writes to library)** |
| `location` | `start_updating()`, `get_location()`, `stop_updating()`, `accuracy` | Read-only |
| `motion` | `get_gravity/rotation/acceleration/magnetic_field/attitude` | Read-only |
| `multipeer` | `connect`, `send`, `get_data`, `wait` | Read + Act (LAN) |
| `speech` | `say(text, language, rate)`, `wait()`, `is_speaking()`, `get_available_languages()` | **Act** (TTS) |
| `sound` | `AudioPlayer`, `play_file`, `play_beep`, `play_system_sound` | **Act** |
| `pasteboard` | `string/set_string`, `image/set_image`, `url/set_url`, `item_provider()`, **`shortcuts_attachments()`** | Read + Act (foreground only) |
| `userkeys` | `get/set/delete` — JSON in `NSUserDefaults` suite `group.pyto` | Read + Act (persistence; **not** Keychain) |
| `xcallback` | `open_url(url) -> str` (raises `RuntimeError`/`SystemExit`) | **Act** (cross-app RPC) |
| `apps` | 100+ third-party app actions (see below) | **Act** |
| `sf_symbols` | SF Symbol name table | data |
| `htmpy` | HTML rendering helper | Act |
| `mainthread` | run code on the main thread | Act |
| `OpenCV` | cv2 + Pyto view helpers | compute |
| `calendar_events` | `get_events(start,end)`, `save_event(e)`, `remove_event(e)`, `Event`, `Alarm`, `RecurrenceRule`, `StructuredLocation` | **Read + Act — undocumented** |
| `console` | `print`, `input`, `clear`, `run_script(...)`, ANSI output | Act (terminal) |
| `sharing` | `share_items`, `open_url`, `FilePicker`, `pick_documents`, `picked_files` | **Act** |

`apps` deserves emphasis: it wraps x-callback-url integrations for **Agenda, Airmail, Bear, beorg, Bitly, Drafts 5, Due, Fantastical 2, Google Maps, Instapaper, Notes, OmniFocus 3, Opener, Overcast, Scriptable (`run_script`), Shortcuts (`run_a_shortcut(name, input, text)`), Spark, Tally 2, Things 3 (`add`, `json`, `update`), Timepage, Textastic, 1Writer, DEVONthink To Go, Day One**, and more ([apps docs](https://pyto.readthedocs.io/en/latest/library/apps.html)).

### 5.2 Objective-C bridge — the escape hatch
"Pyto has the [Rubicon-ObjC](https://rubicon-objc.readthedocs.io) library as its bridge between Python and Objective-C. To make the usage of Objective-C classes easier, Pyto has the iOS system frameworks as modules containing a list of classes." ([Objective-C docs](https://pyto.readthedocs.io/en/latest/Objective-C.html)). Importable frameworks include `Foundation, UIKit, AVFoundation, AVFAudio, AudioToolbox, BackgroundTasks, Contacts, CoreBluetooth, CoreData, CoreHaptics, CoreImage, CoreLocation, CoreML, CoreMIDI, CoreMotion, CoreSpotlight, EventKit, HealthKit, Intents, JavaScriptCore, LocalAuthentication, MapKit, MediaPlayer, MessageUI, Metal, MultipeerConnectivity, NaturalLanguage, Network, NotificationCenter, PDFKit, PencilKit, Photos, PushKit, QuickLook, SafariServices, Security, SoundAnalysis, Speech, StoreKit, SwiftUI, UniformTypeIdentifiers, UserNotifications, Vision, WatchConnectivity, WebKit, WidgetKit` ([same page](https://pyto.readthedocs.io/en/latest/Objective-C.html); generated stubs live in [`Lib/objc/`](https://github.com/ColdGrub1384/Pyto/tree/main/Lib/objc)). `ctypes.CDLL(None)` is also used in-process by Pyto itself to reach app symbols ([extensionsimporter.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/extensionsimporter.py)).

### 5.3 What the Info.plist/entitlements *gate* (important negative list)
Pyto's `Pyto/Info.plist` declares usage descriptions for exactly: `NSAppleMusicUsageDescription`, `NSCalendarsUsageDescription`, `NSCameraUsageDescription`, `NSContactsUsageDescription`, `NSLocalNetworkUsageDescription`, `NSLocationWhenInUseUsageDescription`, `NSMicrophoneUsageDescription`, `NSPhotoLibraryUsageDescription`, `NSPhotoLibraryAddUsageDescription` ([Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist)). There is **no** `NSRemindersUsageDescription`/`NSRemindersFullAccessUsageDescription`, **no** `NSHealthShareUsageDescription`/`NSHealthUpdateUsageDescription`, **no** `NSBluetoothAlwaysUsageDescription`, **no** `NSMotionUsageDescription`, **no** `NSSpeechRecognitionUsageDescription`. So although `EventKit`, `HealthKit` and `CoreBluetooth` are importable via the ObjC bridge, **Reminders, HealthKit, Bluetooth and speech recognition are not usable out of the box** (the app would be denied or terminated for accessing them without the required string; the user cannot add these keys). Likewise `HealthKit` needs an entitlement that is absent from `Pyto.entitlements`. *Exact runtime behaviour (denial vs. crash) is **NOT VERIFIED** on device — but the missing keys are verifiable fact, and the design conclusion (don't rely on them) is safe.*

Entitlements present in `Pyto.entitlements`: `aps-environment`, iCloud `CloudDocuments` (`iCloud.ch.marcela.ada.Pyto`), `extended-virtual-addressing`, `increased-memory-limit`, **`com.apple.developer.siri`**, `user-fonts`, app-sandbox, **app group `group.pyto`**, audio-input, camera, network client, addressbook, location, photos-library. Note `com.apple.developer.siri` = Shortcuts/Siri intents.

**Read vs. act summary.** Pure reads: `location`, `motion`, `pasteboard.string`, `photos.pick_photo`, `calendar_events.get_events`, `music` library, `file_system` pickers, `remote_notifications`. Genuine **actions on the user's behalf**: `photos.save_image`, `calendar_events.save_event/remove_event`, `notifications.*`, `speech.say`, `sound/music` playback, `file_system.share_files/save_as`, `apps.*` (creates notes/tasks/events in other apps), `xcallback.open_url` + `apps.Shortcuts.run_a_shortcut`, `pyto_ui`/`widgets`/`watch` output, `userkeys.set`, `background.BackgroundTask` (keeps the app alive).

---

## 6. Q5 — Automation bridges (the most important section)

### 6.1 Pyto's Shortcuts actions (App Intents)
Pyto ships Shortcuts actions. From the docs: "Pyto provides Shortcuts for running scripts and code. Shortcuts will open Pyto if the `Show Console` parameter is enabled, if not, the code will run asynchronously in background and you can use the `Get Script Output` action to wait for the script and get the output." ([automation docs](https://pyto.readthedocs.io/en/latest/automation.html)).

Exact action names and parameters, from the compiled intent definition in the repo ([Pyto/Base.lproj/Intents.intentdefinition](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Base.lproj/Intents.intentdefinition)):

| Action | Parameters | Vendor description |
|---|---|---|
| **Run Code** | `Code`, `Arguments`, `Attachments` (files), `Working Directory`, `Show Console` (bool), `sys.stdin` | "Runs Python code. The files passed to the 'Attachments' parameter can be retrieved with 'pasteboard.shortcuts_attachments()'." |
| **Run Script** | `Script` (file), `Arguments`, `Attachments`, `Working Directory`, `Show Console`, `sys.stdin` | "Runs a Python script. …" |
| **Run Command** | `Command`, `Attachments`, `Working Directory`, `Show Console`, `sys.stdin` | "Runs a command." |
| **Get Script Output** | *(none)* | "Returns the output from a script executed from Shortcuts. Waits until the script finished running." |
| **Reload Widgets** | `Widgets` | "Reloads the selected widgets." |
| **Start Handling Widgets In App** | — | "Starts running the app in background indefinitely so all widgets are executed in app. That removes the RAM limit of the widgets…" |

Input/output mechanics (docs + source):
- **Into the script:** `sys.stdin` = "The input passed to the script if 'Show Console' is disabled."; `Attachments` = files, retrieved via `pasteboard.shortcuts_attachments()` → `List[ItemProvider]` with `.data(uti)`, `.get_file_path()`, `.get_suggested_name()`, `.open()` ([automation docs](https://pyto.readthedocs.io/en/latest/automation.html), [pasteboard docs](https://pyto.readthedocs.io/en/latest/library/pasteboard.html)). `Arguments` become `sys.argv`.
- **Out of the script:** the app writes a status-prefixed file into the app group; `Get Script Output` polls it:

> `let outputURL = group.appendingPathComponent("ShortcutOutput.txt")` … `if prefix == "Success" { code = .success } else { scheduleNotification(title: "Script thrown an exception", …); code = .failure }` … appends `INFile(data: out…, filename: "console.txt", typeIdentifier: "public.plain-text")`, and separately returns matplotlib figures as `image.png` `public.image` files — [GetScriptOutputIntentHandler.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/Handlers/GetScriptOutputIntentHandler.swift)

So the round trip is: **Shortcut → `Run Script` (Show Console off, text in `sys.stdin`) → script prints to stdout → `Get Script Output` returns `console.txt` + optional PNGs.** ANSI escape codes are stripped before returning (`sendOutputToShortcuts`, [PyOutputHelper.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/Python%20Bridging/PyOutputHelper.swift)).

**Which target runs what (verified from source, and it matters for memory):** the **main app** declares `INIntentsSupported = [RunScriptIntent, RunCodeIntent, StartHandlingWidgetsInAppIntent, RunCommandIntent]` ([Pyto/Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist)), and its handler — compiled with `MAIN` — executes the script itself when `Show Console` is off, via `RunShortcutsScript(...)`, which takes a `UIApplication.beginBackgroundTask` token, dispatches the Python runtime on a background queue, and completes with `.success` immediately ([RunShortcutsScript.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/RunShortcutsScript.swift), [RunScriptIntentHandler.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/Handlers/RunScriptIntentHandler.swift)). Only when `Show Console` is on does it return `.continueInApp`. The **Intents extension** declares only `[GetScriptOutputIntent, ReloadWidgetsIntent, ScriptIntent, SetContentInAppIntent]` ([Pyto Intents/Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto%20Intents/Info.plist)) — it is the piece that waits and reads the output. Both `IntentsRestrictedWhileLocked` and `IntentsRestrictedWhileProtectedDataUnavailable` are **empty arrays**, i.e. Pyto declares no intent as restricted while the device is locked.

### 6.2 x-callback URLs — third-party apps driving Pyto
From the docs, verbatim:

> `pyto://x-callback/?code=[code]&x-success=[x-success]&x-error=[x-error]&x-cancel=[x-cancel]`
> "`code` is the code to execute. `x-success` is the URL to open when a script was executed successfully. **Passes the output (stdout + stderr) to a `result` parameter.** `x-error` … Passes the exception message to a `errorMessage` parameter. `x-cancel` is the URL to open when a script was stopped by the user or when the script raised `SystemExit` or `KeyboardInterrupt`."
> "With this method you can only run code and not a script but you can use the [runpy](https://docs.python.org/3/library/runpy.html) module for running scripts." — [automation docs](https://pyto.readthedocs.io/en/latest/automation.html)

Implementation: the SceneDelegate parses `pyto://x-callback` query parameters into `PyCallbackHelper.{successURL,errorURL,cancelURL}` and runs `code` on the console ([SceneDelegate.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/SceneDelegate.swift)). Pyto's `Info.plist` declares two URL schemes: **`pyto`** and **`pyto-run`** ([Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist)).

> ⚠️ **Honest gap:** I found **no handler** for the `pyto-run` scheme anywhere in the source I searched (all `Scene`/`App`/`URL`/`Shortcut`/`Callback`/`Widget` files, then a full sparse clone grep). It appears only in `Info.plist`. Its behaviour is **NOT VERIFIED** — do not design against it. Documented, code-backed entry points are `pyto://x-callback/?code=…`, `pyto://upgrade`, `pyto://inspector?…`, and `pyto://widget|automator?bookmark=<base64>&arguments=<base64 JSON>&link=…` (the last one runs a bookmarked script and is installed via the app's "Automator" menu item — [MainMenu.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/UI/Life%20Cycle/MainMenu.swift), [SceneDelegate.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/SceneDelegate.swift)). Opening any non-`pyto` URL whose query string is Python code also runs it (`sidebarSplitViewController?.sidebar?.run(code: query)`), which is legacy-ish behaviour.

### 6.3 Pyto scripts driving Shortcuts and other apps
- **Recommended, result-returning:** `shortcuts://x-callback-url/run-shortcut?name=…&input=text&text=…&x-success=…`. Apple: "If a shortcut is run, a parameter named `result` is appended to the URL and contains the textual output of the shortcut." ([Apple: Use x-callback-url with Shortcuts](https://support.apple.com/guide/shortcuts/use-x-callback-url-apdcd7f20a6f/ios)). Pyto ships this exact recipe as a sample:

```python
url = f"shortcuts://x-callback-url/run-shortcut?name={quote(shortcut_name)}&input=text&text={quote(shortcut_input)}"
res = xcallback.open_url(url)   # returns the shortcut's result
```
([open_shortcut.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Samples/Examples/open_shortcut.py), also reproduced in the [automation docs](https://pyto.readthedocs.io/en/latest/automation.html)).

- **Fire-and-forget:** `shortcuts://run-shortcut?name=[name]&input=[input]&text=[text]` — Apple documents `input` as either a text string or the literal word `clipboard`, and `text` as the payload when `input=text` ([Apple: Run a shortcut from a URL](https://support.apple.com/guide/shortcuts/run-a-shortcut-from-a-url-apd624386f42/ios)). Note the **documented** form returns nothing; use the x-callback-url variant when you need the result. Apple's own guidance: "If you'd like to run one shortcut from another shortcut, use the Run Shortcut action instead of a URL scheme. You should only run shortcuts with a URL if you're integrating from another app outside of Shortcuts." — which is exactly Pyto's situation.
- **Via the app catalogue:** `apps.Shortcuts.run_a_shortcut(name, input=None, text=None)`, `open_a_shortcut`, `import_a_shortcut`, plus `xcallback.open_url` for arbitrary x-callback apps ([apps docs](https://pyto.readthedocs.io/en/latest/library/apps.html)).
- **Generic URL opening / `webbrowser`:** `sharing.open_url(url)` and `webbrowser` are wired to Safari — Pyto registers a `mobile-safari` browser whose `open()` calls `sharing.open_url(url)` ([Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py)). Scripts can therefore open any app URL scheme the system allows (Pyto's `LSApplicationQueriesSchemes` contains only `pisth`, but that only limits `canOpenURL`-style probing, not opening).

### 6.4 Can Shortcuts **automations** trigger a Pyto script? Yes — with configuration.
Apple lists which automation triggers can run without confirmation: "The following automations can be run automatically: **Time of Day, Alarm, Sleep, Arrive, Leave, CarPlay, Email, Message, Transaction, Wi-Fi, Bluetooth, Apple Watch Workout, NFC, App, Airplane Mode, Do Not Disturb, Low Power Mode, Battery Level, Charger, Sound Recognition**" and "The following automation cannot be run automatically: Before I Commute", with the procedure: "Turn off **Ask Before Running**, then tap **Don't Ask** to confirm your choice… The automation will not notify you when it's triggered." ([Apple: Enable or disable a personal automation](https://support.apple.com/guide/shortcuts/apd602971e63/7.0/ios/17.0)). There is also an explicit note: "You also may need to set individual actions to run automatically."

Pyto's own code assumes this pattern. `background.BackgroundTask.id` docstring: "A string that identifies the task. If a task with the same ID of a task that is already running is started, the other task will be stopped. **That is useful with Shortcuts Personal Automations where you can create a background task multiple times to make sure it's running without having to worry about the task already being running.**" ([Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py)).

**Practical automation recipe for the harness:**
1. User creates a Personal Automation (e.g. Time of Day 07:00, or an NFC tag, or "When ChatGPT opens"), turns **Ask Before Running** off.
2. Action: **Run Script** → your `agent_boot.py`, `Show Console` = **off**, `sys.stdin` = a JSON job descriptor (or empty), `Working Directory` = your project folder.
3. The script starts a `background.BackgroundTask(id="agent")` with silent audio → the agent keeps running after the Shortcut ends and after the app is backgrounded/locked.
4. The script polls a queue file in `~/Documents` for new prompts and writes results to JSONL; it posts `notifications` for anything needing the user.
5. To fetch a result into Shortcuts: a second Shortcut uses **Get Script Output** (or `Get File`-style actions) to read `console.txt`.

**Limitations to state plainly:** automations are user-configured (an app cannot create them programmatically); iOS may delay or skip them; the "Show Console" path brings the app to the foreground; without the audio `BackgroundTask` a script started from a Shortcut is only protected by a short `beginBackgroundTask` grace period; and Apple can change automation semantics between releases.

### 6.5 Known-working recipes
- **Pyto docs publish one ready-made Shortcut**: "The Shortcuts app supports opening x-callback URLs. [Here](https://www.icloud.com/shortcuts/b85b8afe92e54dc9b54be5ab1495995f) is an example of a Shortcut that shows the current Python version." ([automation docs](https://pyto.readthedocs.io/en/latest/automation.html)). *(The iCloud link is a user-installable Shortcut; I did not install it — its internals are **NOT VERIFIED**.)*
- **Pyto → Shortcuts → result**: shipped `open_shortcut.py` (§6.3).
- **Background web server + notifications** (a directly reusable "long-running agent with an HTTP front door" pattern): the shipped sample binds `('', 80)`, sends a notification per request, and wraps `serve_forever()` in `with BackgroundTask() as b:` ([web_server.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Samples/Examples/web_server.py)). The FAQ points at the same mechanism: "How to run a web server? Use the `background` module to run a script in background." ([FAQ](https://pyto.readthedocs.io/en/latest/faq.html)). ⚠️ The FAQ's linked "Using Django" page is **broken (`background.html` renders empty and `django.html` is 404)** — the docs' own example is missing.
- **Reminders/Calendar/Photos:** no Pyto module for Reminders. Two routes: (a) `calendar_events` for calendar events directly (undocumented but implemented); (b) delegate to a Shortcut via `xcallback.open_url("shortcuts://x-callback-url/run-shortcut?…")` and read the `result` — this is how a Pyto agent gets Reminders/Health/etc. done, by asking the user to build a one-action Shortcut once.
- **Files hand-off:** `file_system.save_as(path)`, `share_files(*paths)`, `share_text(*text)`, `quick_look(*paths)` ([Lib/file_system.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/file_system.py)).

---

## 7. Q6 — Files, persistence, sandbox

**Containers.** "The iOS / iPadOS file system is very different than the file system on other operating systems. Files are placed inside containers… other apps don't have access to files owned by other ones. Instead, the system gives access to individual files or folders selected by the user to be edited with the desired app." ([external files docs](https://pyto.readthedocs.io/en/latest/external.html)).

**What the script can read/write.**
- Its own sandbox, chiefly `~/Documents` (i.e. `Pyto/Documents`), plus `~/Library` and `~/tmp` — upstream: "In iOS, you cannot write in the `~` directory, only in `~/Documents/`, `~/Library/` and `~/tmp`." ([ios_system README](https://github.com/holzschu/ios_system); the same paragraph appears in [a-Shell's README](https://github.com/holzschu/a-shell)).
- The shared app-group container `group.pyto` (App Group entitlement) — used for `userkeys`, Shortcuts attachments and `ShortcutOutput.txt`.
- The **iCloud Drive "Pyto" folder**: `NSUbiquitousContainers` → `iCloud.ch.marcela.ada.Pyto` with `NSUbiquitousContainerIsDocumentScopePublic = true` and an `NSUbiquitousContainerName` ([Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist)); the docs reference a "Pyto" folder in iCloud and note "This will not work for third party cloud providers such as Google Drive or Dropbox because they don't have a real file system." ([external files docs](https://pyto.readthedocs.io/en/latest/external.html)).
- **Visible in the Files app**: `UIFileSharingEnabled = true` ([Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist)) — the user can browse/copy the agent's `.py` and `.jsonl` files from Files with no extra work. This is the simplest export path for generated code.
- Directory-scoped access to *other apps'* folders needs a user pick: "Scripts stored in the Pyto container can access scripts in the same directory. If access isn't granted, a lock icon is displayed at the bottom of the code editor. Pressing this button shows a folder picker." ([external files docs](https://pyto.readthedocs.io/en/latest/external.html)). Programmatically: `file_system.pick_directory()` / `open_directory()` / `import_file()`, and **security-scoped bookmarks** survive relaunches — "A Bookmark to a file makes it possible to keep read and write access to a file outside the app's sandbox across launches." ([file_system docs](https://pyto.readthedocs.io/en/latest/library/file_system.html)); implemented with `NSURL.bookmarkDataWithOptions(1024, …)` + `startAccessingSecurityScopedResource()` and persisted in `userkeys` ([Lib/file_system.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/file_system.py)). The deprecated `bookmarks` module does the same ([Lib/bookmarks.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/bookmarks.py)).
- **Getting files out:** `save_as()`, `share_files()`, `share_text()`, `quick_look()` (share sheet / Quick Look), the Files app (file sharing enabled), or `xcallback.open_url` to hand a path/URL to another app.

**Design implication.** Store sessions/tool outputs under `~/Documents/agent/` (visible + exportable), keep a single security-scoped bookmark for the one external folder the user grants, and never assume you can read another app's container without an explicit pick.

---

## 8. Q7 — Selling points and pitfalls people actually report

**Meta-finding first: Pyto's GitHub issue tracker is disabled.** `GET https://api.github.com/repos/ColdGrub1384/Pyto` returns `"has_issues": false` (verified). There are therefore **no GitHub issues to cite**; the 92 "open issues" on the repo are pull requests. User reports live in the App Store reviews, the [r/PytoIDE](https://www.reddit.com/r/PytoIDE) subreddit (the docs' own community pointer: "Is there a Pyto users community that I could join? The r/PytoIDE subreddit." — [FAQ](https://pyto.readthedocs.io/en/latest/faq.html)) and email. I could not retrieve review text or Reddit threads from this environment (Apple's review RSS returned zero entries; Reddit's JSON endpoint blocked the request) — so **review- and Reddit-derived claims are NOT VERIFIED here**; the pitfalls below are each backed by Pyto's own docs/source or Apple's docs instead.

Ranked pitfalls, each with a primary source:

1. **`subprocess`/`fork` are illusions.** `subprocess.Popen` runs in-process and synchronously, `kill()`/`terminate()` are empty, `os.fork()` is a no-op stub, `os.waitpid` returns `(-1,0)`. Any agent code that assumes it can spawn-and-supervise a child, enforce a timeout by killing it, or isolate a crash will silently misbehave. ([_ios_popen.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/_ios_popen.py), [Startup.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Startup.py), [ios_system README](https://github.com/holzschu/ios_system))
2. **A hidden per-app watchdog kills your script.** When `os_proc_available_memory()` drops to ~500 MB of headroom, Pyto flips `tooMuchUsedMemory` and **stops every running script** with `MemoryError`. Long conversations, big tool outputs, unbounded JSONL reads, or pandas imports can trip this. ([MemoryManager.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/MemoryManager.swift), [AppDelegate.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/UI/Life%20Cycle/AppDelegate.swift))
3. **Background execution depends on a silent-audio hack.** Without `background.BackgroundTask` (or the widget intent) the app suspends when backgrounded; with it you must live with an audio session, periodic reminder notifications (every 6 h by default), interruption handling, and a gray-area App Review rationale ("Multitasking apps may only use background services for their intended purposes…" — [2.5.4](https://developer.apple.com/app-store/review/guidelines/)). ([BackgroundTask.swift](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Model/Background/BackgroundTask.swift))
4. **You cannot install compiled dependencies.** "`pip` will fail for packages like `pandas`, `scipy` or `numpy`" and even the bundled ones are locked behind the Full Version IAP. ([Welcome to Pyto](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Welcome%20to%20Pyto.md), [FAQ](https://pyto.readthedocs.io/en/latest/faq.html), [App Store](https://apps.apple.com/us/app/pyto-ide/id1436650069))
5. **Documentation gaps around exactly the features you need.** `library/background.html` renders as an empty page (the `.rst` is just `.. automodule::`), the FAQ links to a "Using Django" page that 404s, and the LLVM/C-extension page (`llvm.html`) is also 404 even though `projects.html` links to it. Verify behaviour in source, not docs. ([background.rst](https://github.com/ColdGrub1384/Pyto/blob/main/docs/library/background.rst), [FAQ](https://pyto.readthedocs.io/en/latest/faq.html), [docs/llvm.rst](https://github.com/ColdGrub1384/Pyto/blob/main/docs/llvm.rst))
6. **Missing permission strings silently rule out whole frameworks.** No Reminders, HealthKit, Bluetooth, motion-activity or speech-recognition usage descriptions in `Info.plist`, and no HealthKit entitlement — so the ObjC bridge being importable does **not** mean the API is usable. Route those through user-built Shortcuts. ([Info.plist](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Info.plist), [Pyto.entitlements](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Pyto.entitlements))
7. **Interruptions only land at bytecode boundaries.** Scripts are stopped by injecting exceptions into threads (`stopit.async_raise`), so a thread blocked in a C call, a DNS lookup or a wedged socket read can ignore `KeyboardInterrupt` and keep the app pinned. ([scripts_runner.py](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/scripts_runner.py), [Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py))
8. **The clipboard is off-limits in the background** — "apps cannot access to the clipboard in background". ([Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py))
9. **Release cadence is slow / Python is frozen at 3.10.** Last App Store update 2024-06-09; the interpreter is compiled in and "cannot be updated" by the user. ([iTunes API](https://itunes.apple.com/lookup?id=1436650069&country=us), [FAQ](https://pyto.readthedocs.io/en/latest/faq.html))
10. **The app is ~1 GB** (975,650,816 bytes), because it bundles Python + the LLVM/clang toolchain + scientific wheels. ([iTunes API](https://itunes.apple.com/lookup?id=1436650069&country=us))

**Selling points that are real** (and worth leaning on in the harness design): a genuine on-device CPython with a rich first-party iOS API surface ([docs index](https://pyto.readthedocs.io/en/latest/)); **first-class Shortcuts App Intents both directions** ([automation docs](https://pyto.readthedocs.io/en/latest/automation.html)); the ability to keep a script alive indefinitely with a documented, shipped API ([Lib/background.py](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py)); an ObjC bridge over ~100 frameworks ([Objective-C docs](https://pyto.readthedocs.io/en/latest/Objective-C.html)); Files-app visibility of the sandbox (`UIFileSharingEnabled`); a paid IAP that unlocks numpy/pandas/scipy/matplotlib when the user wants them; and a vendor site that markets exactly this use case: "Shortcuts — Run scripts or custom code with Shortcuts." ([pyto.app](https://pyto.app/)).

---

## 9. Q8 — Alternatives worth naming

**Pythonista 3** (Ole Zorn, $9.99, v3.4, last updated **2023-04-27**, min iOS 12 — [App Store](https://apps.apple.com/us/app/pythonista-3/id1085978097)). The classic iOS Python IDE with the deepest native module set (`ui`, `scene`, `objc_util`, `appex`, `clipboard`, `keychain`, `photos`, `reminders`, `contacts`, `location`, `motion`, `speech`, `console`). Its `shortcuts` module is **outbound-only**: "You can also launch the Shortcuts app (and run shortcuts) using this module, **but support for 'native' Python shortcuts in the Shortcuts app is planned for a future version of Pythonista**" ([Pythonista shortcuts docs](https://omz-software.com/pythonista/docs/ios/shortcuts.html)) — i.e. no App Intents, so a Shortcut automation cannot call *into* Pythonista the way it can call into Pyto; `shortcuts.open_shortcuts_app()` just fires `shortcuts://run-shortcut?name=…`. Extension execution is in-process via `appex` (share sheet / Today widget), with the docs warning that extensions get a much smaller RAM allowance and that "The camera is not available to app extensions" and "`webbrowser.open()` … will not work within a share extension script" ([appex docs](https://omz-software.com/pythonista/docs/ios/appex.html)). No documented subprocess/fork support at all — **NOT VERIFIED** from a single authoritative statement, but there is no `subprocess` entry in its iOS module index and the whole design is in-process extensions. Verdict: **Real shell/subprocess: NO — Shortcuts-drivable: PARTIAL (can launch Shortcuts, cannot be triggered by Shortcuts; no App Intents per its own docs).**

**a-Shell** (Nicolas Holzschuch, free, v2.2.2, updated **2026-09-21** — [App Store](https://apps.apple.com/us/app/a-shell/id1473805438)). A proper terminal *front-end* with the largest command set of the group, built on the same `ios_system` in-process dispatch as Pyto: "It uses [ios_system] for command interpretation, and includes all commands from the ios_system ecosystem (nslookup, whois, python3, lua, pdflatex, lualatex…)" — and it is explicit about the limits: "We have the limitations of WebAssembly: **no sockets, no forks**"; "For Python, you can install more packages with `pip install packagename`, but only if they are pure Python. The C compiler is not yet able to produce dynamic libraries that could be used by Python." ([a-shell README](https://github.com/holzschu/a-shell)). `git` is available only via `lg2` (libgit2) and extra binaries arrive as WebAssembly via `pkg install zip|unzip|xz|ffmpeg` ([App Store description](https://apps.apple.com/us/app/a-shell/id1473805438)). Shortcuts support is first-class and documented, including the extension-vs-app tradeoff: "There are three shortcuts: `Execute Command` … `Put File` and `Get File`… 'In Extension' means the shortcut runs in a lightweight version of the App… It is good for light commands… 'In App' opens the main application to execute the shortcut. It has access to all the commands, but will take longer." ([a-shell README](https://github.com/holzschu/a-shell)). Verdict: **Real shell/subprocess: PARTIAL (full-featured in-process shell emulation, no fork/exec) — Shortcuts-drivable: YES (`Execute Command`, `Put File`, `Get File`).**

**iSH** (free, v1.3.2, last updated **2023-05-21** — [App Store](https://apps.apple.com/us/app/ish-shell/id1436902243)). The only option here with a **genuine Linux userland**: "A project to get a Linux shell running on iOS, using usermode x86 emulation and syscall translation" ([iSH README](https://github.com/ish-app/ish)), so inside Alpine you get real `fork`/`exec`/`sh`/`apk`/`ssh` — at the cost of emulation speed and a slow release cadence. It is **not** Shortcuts-drivable: the app's `Info.plist` declares no `CFBundleURLSchemes` (verified in [app/Info.plist](https://github.com/ish-app/ish/blob/master/app/Info.plist)), it stays alive via the `location` background mode rather than audio, and the request to add Shortcuts integration has been open since 2018: issue [#59 "Feature request: Siri Shortcuts integration"](https://github.com/ish-app/ish/issues/59) (state: open). Verdict: **Real shell/subprocess: YES (inside the emulated Linux VM) — Shortcuts-drivable: NO.**

**Carnets** (Holzschuch, free, v1.9.1, updated **2025-11-26** — [App Store](https://apps.apple.com/us/app/carnets-jupyter/id1450994949)) is a fully offline, on-device Jupyter/JupyterLab with a big preinstalled scientific stack — "Numpy, Sympy, Matplotlib, Pandas, lxml, bokeh… are pre-installed… You can add more packages using `%pip install packageName`, but only if they are pure Python" — and it is notebook-shaped rather than agent-loop-shaped (no documented background execution or Shortcuts triggering: **NOT VERIFIED**). **Juno** ("Python IDE & Jupyter – Juno", Rational Matter, v4.3.2, updated **2026-10-03** — [App Store](https://apps.apple.com/us/app/python-ide-jupyter-juno/id1462586500)) is the most modern of the local environments: "Run Jupyter notebooks, Python scripts, and whole projects locally with the built-in **Python 3.13** interpreter and a full suite of up-to-date, preinstalled scientific and data packages — entirely offline", plus an AI coding assistant, with code execution gated behind a one-time Pro purchase; the sibling **Juno Connect** is a remote-kernel Jupyter client. Neither documents Shortcuts automation of arbitrary scripts (**NOT VERIFIED**). Verdict (both): **Real shell/subprocess: NO (in-process interpreter) — Shortcuts-drivable: NOT VERIFIED / no documented actions.**

**Remote box + phone-as-UI** — run the harness on a Linux server or home machine and use the iPhone only as a terminal/client. This is the only option that removes *every* constraint above (real processes, real `git`/`ffmpeg`, unlimited memory, real cron) at the cost of requiring network reachability. Blink Shell is the strongest client (SSH + mosh + a local build environment; v18.7.1, updated **2026-09-17** — [App Store](https://apps.apple.com/us/app/blink-shell-build-code/id1594898306), [blink.sh](https://blink.sh/)); Termius is the cross-platform alternative (v7.9.0, **2026-09-30** — [App Store](https://apps.apple.com/us/app/termius-modern-ssh-client/id549039908)). Whether Blink/Termius expose Shortcuts actions or x-callback-url entry points for "run this command and return the output" is **NOT VERIFIED** from a primary source in this research pass. Verdict: **Real shell/subprocess: YES (on the remote host) — Shortcuts-drivable: NOT VERIFIED.**

---

## 10. Q9 — Pyto's own AI/LLM examples

**There are none.** I grepped the full source tree (case-insensitive) for `openai`, `chatgpt`, `gpt-`, `copilot`, `anthropic`, `llm` across `.py`, `.swift`, `.md` and `.rst`: no LLM client, no chat example, no Copilot integration. The shipped sample set is [Pyto/Samples/Examples](https://github.com/ColdGrub1384/Pyto/tree/main/Pyto/Samples/Examples): `breakpoints/`, `clipboard_manager.py`, `console_output.py`, `get_location.py`, `gyroscope.py`, `html.py`, `music_library.py`, **`open_shortcut.py`**, `third_party/` (Astropy, Matplotlib, Numpy, OpenCV, Pandas, PyNaCl, SciKit-Image, SciKit-Learn, SciPy, Statsmodels, Turtle), `ui/`, **`web_server.py`**, `widgets/`, `wireless_chat.py`. Project templates (`_project_template`, `_c_project_template`, `_cpp_project_template`) exist for `setuptools`/C/C++ packaging ([projects docs](https://pyto.readthedocs.io/en/latest/projects.html)).

What Pyto *does* give you for API calls is generic and sufficient: `urllib`/`http.client` from the stdlib, `requests` bundled as a vendored pure-Python dependency ([third_party list](https://pyto.readthedocs.io/en/latest/third_party.html) — "requests 2.27.1"), plus `curl` as a command ([App Store](https://apps.apple.com/us/app/pyto-ide/id1436650069)). The docs have no "calling APIs" tutorial; the only how-to-ish pages are Terminal, Automation, Projects, External files, FAQ, and the (404) LLVM page. There is **no official example repo and no official wiki** — the public artifacts are the [documentation](https://pyto.readthedocs.io/en/latest/), the [source repo](https://github.com/ColdGrub1384/Pyto), the vendor site [pyto.app](https://pyto.app/), and the [r/PytoIDE](https://www.reddit.com/r/PytoIDE) subreddit. The in-app onboarding text is [Pyto/Welcome to Pyto.md](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Welcome%20to%20Pyto.md) (60 lines: getting started, debugging, installing modules).

**Implication:** the harness's HTTP+SSE client, tool schema, session store and prompt/context management are all yours to write and maintain.

---

## 11. The honest "cannot do" list

1. ❌ Cannot spawn, supervise, kill or isolate a child process. `subprocess.Popen` is synchronous and in-process; `kill`/`terminate` are no-ops; `os.fork` is a silent no-op; `os.waitpid` returns `(-1,0)`. No `multiprocessing`.
2. ❌ Cannot enforce a hard timeout on a blocking call by killing it (no process to kill; thread interruption only lands between bytecodes).
3. ❌ Cannot `pip install` anything with a compiled component, and cannot use numpy/pandas/scipy/matplotlib/cv2 unless the user buys the Full Version IAP.
4. ❌ Cannot update Python: frozen at 3.10 in a binary the user cannot change.
5. ❌ Cannot run a real `sh`/`bash`/`git`/`ffmpeg`/`wget`/`make`; the "shell" is a Python re-implementation plus in-process `ios_system` commands.
6. ❌ Cannot run reliably in the background *by default*: needs `background.BackgroundTask` (silent audio) or the widget-in-app intent. `request_background_fetch` is explicitly "pretty unreliable" and capped at 30 s.
7. ❌ Cannot read the clipboard in the background.
8. ❌ Cannot create Shortcuts automations programmatically; the user must configure them, and only the triggers Apple lists can run without confirmation.
9. ❌ Cannot use Reminders, HealthKit, CoreBluetooth, motion-activity/pedometer or speech recognition out of the box (missing Info.plist usage strings / entitlements). Workaround: a user-built Shortcut.
10. ❌ Cannot ship secrets safely in `userkeys` (plain `NSUserDefaults` JSON) — there is no first-class Keychain module; only raw `Security` framework calls.
11. ❌ Cannot run code in a *script file* through the x-callback URL — only `code=` (use `runpy` from an embedded snippet).
12. ❌ Cannot rely on `pyto-run://` — the scheme is declared but I found no handler (NOT VERIFIED).
13. ❌ Cannot read arbitrary other-app files without a one-time user grant (security-scoped bookmark).
14. ❌ Cannot count on the docs for the very features that matter most: `library/background.html` is empty, `django.html` and `llvm.html` are 404.
15. ❌ No LLM/API examples, no official example repo, no wiki.
16. ⚠️ Cannot assume a fixed memory ceiling number: Apple publishes no jetsam figure; Pyto's own guard trips at ~500 MB of *available* memory (500 MB being Pyto's constant, not Apple's). Pyto holds `increased-memory-limit`, so it is better off than a stock app — but its extension (widgets, `Get Script Output`) is not.

---

## 12. Open / unverified items (do not design against these)

| Claim | Status |
|---|---|
| `pyto-run://` URL scheme behaviour | **NOT VERIFIED** — declared in `Info.plist`, handler not found in source |
| `multiprocessing` failure mode on device | **INFERRED** from the `os.fork` no-op stub |
| Ability to build a non-trivial C extension in-app | **NOT VERIFIED**; docs claim it works, the Welcome doc says Pyto "cannot compile modules", and the published page 404s |
| App Store review text / Reddit threads about Pyto | **NOT VERIFIED** — Apple's review RSS returned 0 entries and Reddit's JSON endpoint refused this client; the GitHub issue tracker is disabled |
| Exact runtime behaviour when importing HealthKit/CoreBluetooth without permission strings (crash vs. denial) | **NOT VERIFIED** — the absence of the keys is verified |
| Whether Shortcuts `Run Script` executes while the device is locked | Partially evidenced (both `IntentsRestrictedWhileLocked` and `IntentsRestrictedWhileProtectedDataUnavailable` are empty arrays) but **NOT VERIFIED** end-to-end |
| Blink Shell / Termius Shortcuts actions or x-callback entry points | **NOT VERIFIED** |
| Carnets / Juno Shortcuts automation of arbitrary scripts | **NOT VERIFIED** |
| Pythonista's total lack of subprocess support | **NOT VERIFIED** as a single authoritative statement (strongly implied by its in-process extension design) |

---

## 13. One-paragraph design recommendation

Build the harness as a **single long-lived in-process Python program inside Pyto** that (a) is started either manually or by a **Shortcuts Personal Automation → `Run Script` with `Show Console` off**, (b) immediately acquires `background.BackgroundTask(id="…")` so it survives backgrounding and screen lock, (c) owns its own cooperative scheduler on threads (never `subprocess`, never `multiprocessing`, never a killable timer), (d) keeps all state in `~/Documents/agent/` as JSONL with rotation and hard size caps because a hidden watchdog will `MemoryError`-stop every script when free memory falls to ~500 MB, (e) treats `sys.stdin` (Shortcuts) and `~/Documents` queue files as its input channels and stdout/`console.txt`/notifications as its output channels, (f) uses `xcallback.open_url("shortcuts://x-callback-url/run-shortcut?…")` as the escape hatch for any iOS capability Pyto lacks (Reminders, Health, anything not in `Info.plist`), (g) stays stdlib-only and degrades gracefully when numpy/pandas are unavailable, and (h) instruments `os_proc_available_memory()` itself so the agent can compact context before Pyto's watchdog does it for you.

---

## 14. pyto-harness capability audit (2026-10-07)

The repository registers **46 fixed model-facing tools** in `harness/tools_ios.py`. Their
stable IDs, input JSON Schemas, model-visible output shape, effects, prerequisites,
retry metadata and source evidence are recorded in
[`capability-contracts.json`](capability-contracts.json). Every fixed entry is marked
`implemented` with a source line; none is marked device-tested. The active Pyto workspace
can also load user-authored custom tools dynamically. Their names and schemas are
instance-specific and must be inspected with `custom_tool_list` in that installation; the
manifest includes a family-level contract template, while no user-specific custom source
is part of this repository snapshot.

The proposed capability names from the extension roadmap mostly already map to registered
tools: clipboard get/set, workspace file read/write/list, notifications, URL opening,
Shortcut calls, and calendar read/add. There is no model-facing location read, contacts,
image OCR, language detection, general scheduler, Shortcut enumeration, or generic App
Intent call. `device_capabilities` reports feature availability; it does not return a
location. `keepalive_start` keeps a task running temporarily; it is not a general
scheduler. These distinctions prevent the next capability work from duplicating an
existing tool or assuming an integration that is not registered.

### Shortcut handoff versus Shortcut result

Pyto's published xcallback API and sample describe `xcallback.open_url(url)` as returning
Shortcut output (§6.3 above). The repository's current `shortcut_run_wait` adapter calls
that function but discards its return value; it reports the handoff URL and a callback
description instead. Therefore, a successful harness tool result currently means that an
opener accepted the handoff, not that the Shortcut completed or its semantic result was
received. A Shortcut may return ordinary text that means its own operation failed, so
transport state and semantic result must remain separate.

The existing registry applies a 30-second timeout to this tool, but synchronous handlers
run in worker threads and a timeout does not kill a blocked thread (`harness/tools.py`,
`ToolRegistry` contract). A user-provided Pyto run dated 2026-10-07 observed A20: the direct
`xcallback.open_url` call returned a `str` containing the fixture's semantic-failure marker
with `transport_state: ok`. This is one narrow device observation, not verification of the
model-facing `shortcut_run_wait` handler, which still discards the callback return value.
Cancellation, size limits, name handling, repeated-call stability and recovery remain
unverified; see the dated record in [`shortcut-bridge.md`](shortcut-bridge.md). If return or
recovery behavior proves unreliable, downstream integrations that need returned data must
be scoped accordingly; this audit does not add a workaround.

### Data-flow prototype

[`harness/handles.py`](../harness/handles.py) and
[`examples/handle_pipeline.py`](../examples/handle_pipeline.py) demonstrate a local
workspace-file pipeline. Handles expose an opaque ID, media type, shape, byte size,
preview policy, workspace-session scope and lifetime. Text and binary content stays in a
private temporary store; binary files are typed as media artifacts. The prototype caps
an individual handle at 8 MiB and text transforms at 1 MiB. Operation outcomes are
separate from the handle metadata. The example uses the same `Workspace` path jail as
the registered file tools, transforms text locally, writes the output, and prints only
handle metadata plus an output hash comparison. This is a prototype, not a new registered
capability, a security sandbox, or an enforcement mechanism that prevents direct Pyto API
use.

The contract's `effects` and `data_egress` fields are declarative metadata. They neither
enforce policy nor stop generated Python from importing Pyto modules or using other
available APIs. The current LLM provider, prompt construction, Web UI and session-log
format are outside this audit.
