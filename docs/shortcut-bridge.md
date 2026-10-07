# Pyto Shortcut bridge: manual validation kit

**Status: device behavior is unverified.** No A1–A20 result is claimed until a person runs
the script in a real Pyto installation and keeps the dated JSONL run log. Pyto's published
[`xcallback.open_url` documentation](https://pyto.readthedocs.io/en/latest/library/xcallback.html)
describes a return value, and [Apple documents the x-callback URL behavior](https://support.apple.com/en-euro/guide/shortcuts/apdcd7f20a6f/ios),
but the harness wrapper currently discards the returned value. Documentation alone is not
evidence that this wrapper's round trip works on a particular device.

## Quick run from an installed release

The release installer downloads and extracts the complete source tree, including
`shortcut_validation.py` beside `run.py`. You do not need to download or copy a separate
script. In Pyto, open `pyto-agent/shortcut_validation.py` from your installed folder and
tap Run. If you installed to a custom directory, open the script beside that installation's
`run.py` instead.

For the smallest check, [install the harmless `pyto-harness-test-return` fixture from
iCloud](https://www.icloud.com/shortcuts/c3a68bc19a4c4efdae906603f5dc434b). It returns the
fixed text `PYTO_HARNESS_OK`. Apple requires you to tap **Get Shortcut** to add it. When the
script prompts, enter `A1`, then type `RUN` to confirm that exact fixture. A result with
`transport_state: "ok"` and the expected preview confirms the direct `xcallback.open_url`
return path on this device. The script writes a timestamped JSONL report beside itself.
The one-time shortcut import uses iCloud; the validation script itself makes no network
calls.
For A1, the script follows Pyto's documented example and does not add an `x-success`
parameter; `xcallback.open_url` manages that callback. The registered
`shortcut_run_wait` wrapper constructs its own `x-success` URL and is examined separately
by the manual A7 check.

## What the kit does

[`shortcut_validation.py`](../shortcut_validation.py) is a standard-library script that
uses only Pyto's bundled `xcallback` module for Shortcut calls. It makes no LLM request,
API call, network request, or third-party package import. The script prints one compact
JSON `RESULT` line per test case and appends the same record to a timestamped
`shortcut-validation-*.jsonl` file beside the script. Each record contains the case ID,
status, observation, UTC time, Python/platform runtime details, and
`evidence_status: requires_manual_device_review`.

Run the script in the Pyto foreground. With no arguments it presents a prompt. If the
launch method accepts script arguments, these are also available:

```text
--list
--setup
--case A1
--suite basic
--suite stress
--suite recovery
--suite all
```

When you run one case, every Shortcut call displays the exact fixture name and requires
typing `RUN`. The full `all` suite instead lists all required fixture names and asks for a
single `RUN ALL` confirmation; it then runs without per-case confirmations. The script
refuses names outside the `pyto-harness-test-` prefix. It never enumerates or invokes
personal Shortcuts. A random missing name is generated and shown immediately before A14.
You can always skip a single case by pressing Return. Use
`--setup` to print all fixture recipes in Pyto; when you run a specific case, the script
prints only the setup instructions for the fixtures that case needs before it asks you to
type `RUN`. In `all`, fixture setup recipes are available with `setup` before starting the
batch. The A1 import link is included in both places. Pyto cannot create or import
Shortcuts from Python, so each other fixture is a one-time setup in Apple's Shortcuts app.
Before `RUN ALL`, make sure each listed name belongs to the harmless test fixture you
created; the script cannot inspect Shortcut contents or distinguish a same-named personal
Shortcut before launching it.

The `all` batch includes stress checks A5/A13 and the 60-second A6 wait. The callback has
no script-enforced timeout, so a stalled Shortcut can block the run. A3 may require you to
cancel an iOS input prompt and A18 may show a permission prompt. A7, A8, A16, A17, and A19
cannot run end-to-end in this standalone script; the batch records them as `unknown` and
continues instead of waiting for manual notes.

## Create the harmless fixtures

In Apple's Shortcuts app, create only the fixtures you intend to test. Keep them local and
deterministic; do not add network actions, personal data, or actions that change user data.
Use the exact names below. The script's `--setup` output and per-case prompts provide the
step-by-step recipes, including the exact return text or input variable to use.

| Fixture name | Actions and expected behavior |
|---|---|
| `pyto-harness-test-return` | Install from the iCloud link above, or create manually. Returns the fixed text `PYTO_HARNESS_OK`. Used by A1. |
| `pyto-harness-test-echo` | Return the received Shortcut Input unchanged. Used by A4, A5, A9–A13 and A15. |
| `pyto-harness-test-error` | In a dedicated empty test folder, attempt to get one known-missing file. Keep it local. If the action asks for a file or cannot be made to fail safely, cancel and mark A2 unknown. |
| `pyto-harness-test-cancel` | Use Ask for Input and cancel it manually when prompted. Used by A3. |
| `pyto-harness-test-wait` | Wait 60 seconds, then return `PYTO_HARNESS_WAIT_DONE`. Used by A6–A8. |
| `pyto-harness-test-semantic-failure` | Return the fixed text `PYTO_HARNESS_SEMANTIC_FAILURE` without performing any other action. Used by A20. |
| `pyto-harness-test-permission` | Show a local notification with fixed, non-personal text. Used by A18. |
| `pyto-harness-test-path` | Receive a text path, try to read that exact file using a local Files action, and return its contents. Used by A12. |
| `pyto-harness-test-space fixture` | Echo Shortcut Input. Used by A15. |
| `pyto-harness-test-中文` | Echo Shortcut Input. Used by A15. |
| `pyto-harness-test-emoji-🧪` | Echo Shortcut Input. Used by A15. |

For A12, the script creates a temporary file named `pyto-harness-test-path-probe.txt`
beside itself containing only `PYTO_HARNESS_PATH_PROBE`, passes its full path to the
fixture, then attempts to remove the file after the callback returns. Observe whether the
Shortcuts file action accepts that path directly, prompts for a user-selected location,
or cannot read Pyto's container. If the callback blocks and you stop Pyto, remove the
probe file manually afterward.

For A2, a missing file in a folder created only for this test is the suggested local error
case. Do not use a web request or an action with external side effects to manufacture an
error. If iOS prompts, cancel rather than granting unexpected access.

## Cases A1–A20

| ID | Check | How to run / interpret |
|---|---|---|
| A1 | Successful return type | Run `--case A1`; uses Pyto's documented xcallback URL shape and records returned type, byte count, hash and a short synthetic fixture preview. On failure, records a short URL-redacted exception message. |
| A2 | x-error | Run `--case A2` with the local missing-file fixture. Records exception class and elapsed time; exact error text is not logged. |
| A3 | User cancellation | Run `--case A3`, then cancel Ask for Input. Records whether the call returns, raises `SystemExit`, or fails another way. |
| A4 | Unicode and newlines | Echoes Chinese, emoji and two newline-delimited lines; records whether returned text matches. |
| A5 | Output-size limits | Opt-in stress case. Echoes progressively 1 KiB, 100 KiB, then 1 MiB and stops at the first failure. |
| A6 | Built-in wait | Recovery-only case. The fixture waits 60 seconds. The standalone script has no enforced timeout; be prepared for a long wait. |
| A7 | Worker after timeout | Manual harness check. In a disposable harness session, call the registered `shortcut_run_wait` with the wait fixture (the registry timeout is 30 seconds), then run A1 in a fresh invocation. Record whether the second call succeeds and whether the first worker appears to remain active. The script cannot kill or inspect that worker itself. |
| A8 | Hang recovery | Manual, opt-in check with a disposable harness session only. If needed, force-quit Pyto and resume that exact session using the documented `--resume` option. Never use a personal session. |
| A9 | Repeated calls | Runs the echo fixture 10 times with distinct synthetic inputs and stops at the first mismatch or error. |
| A10 | Plain-text input | Sends one fixed synthetic string to the echo fixture and checks the response. |
| A11 | Structured text input | Sends a JSON string with synthetic fields; checks exact text round trip. |
| A12 | File path input | Creates and passes the harmless probe described above; checks for the marker response. |
| A13 | Input-size limits | Opt-in stress case. Sends 1 KiB, 16 KiB, then 100 KiB, stopping at the first failure. |
| A14 | Missing name | Opens a timestamped, prefixed name after you confirm it is absent. Skip if the name is present or you are unsure. |
| A15 | Name encoding | Calls the three explicitly listed echo fixtures with spaces, Chinese and emoji in their names. Create all three first. |
| A16 | Duplicate names | Manual check: see whether Shortcuts permits duplicate names. If it does, only run them if their harmless outputs distinguish them. |
| A17 | Shortcut enumeration | Manual documentation check. Do not probe undocumented URL schemes or inspect personal Shortcut names. Record whether this Pyto version offers a documented listing API. |
| A18 | Permission prompt | Run the local-notification fixture. If notification permission was previously granted, record that fact; revoke it in iOS Settings only if you intentionally want to observe the initial prompt. |
| A19 | Foreground/background | Manual check with the same harmless fixture while Pyto is foregrounded and through a user-created automation. Record prompts, suspension, return, or failure. |
| A20 | Semantic failure text | Returns the fixed failure marker. A transport success with this marker is still a successful transport and a semantic failure string; the harness must not infer success from transport alone. |

`basic` omits the stress and recovery cases. `stress` and `recovery` require explicit suite
confirmation. `all` requires explicit confirmation because it includes large inputs and a
60-second wait. High-size runs are progressive; stop at the first failure. No test has a
hard kill mechanism for an x-callback call blocked inside Pyto.

## Reading the results

Each `RESULT` line is JSON. The top-level `status` is the test harness status (`observed`,
`unknown`, `not_run`, or `failed`); `transport_state` records `ok`, `failed`, `cancelled`,
or `indeterminate` where a Shortcut call was made. A returned semantic-failure marker is
recorded separately. Records include `kit_version`. A1 also includes a short
`error_message` on failure, capped at 240 characters with URL contents redacted; other cases
record only the exception class. Successful return values are previewed up to 80 characters.
Use only the synthetic fixtures above; review and redact a report before sharing it if a
fixture returned anything unexpected.

Before updating a contract to `device-tested`, review the log and add a dated reference to
the real Pyto run. Record the Pyto app version from the device's About/settings screen as
a note alongside the JSONL report; Pyto does not expose a stable standard-library API for
that version, so the script records it as unknown. Do not treat a desktop run, Pyto
documentation, or an unreviewed JSONL file as device verification.

## If the bridge is unreliable

An A1–A20 failure is a valid result. If callback values are unavailable, do not design
downstream integrations that depend on Shortcuts returning structured data; the current
`shortcut_run_wait` contract is only a handoff request. If cancellation or timeout leaves
execution uncertain, do not retry automatically: a Shortcut may already have performed
side effects. If names cannot be enumerated, a future tool catalog needs user-maintained
names and schemas. If paths or sizes fail, record the observed boundary and scope the
downstream flow accordingly. This validation goal records those implications; it does not
add a bridge workaround.
