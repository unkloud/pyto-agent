# Self-repair

`pyto-harness` can look at itself, repair part of what it finds, and — when the fault is in
its own Python source — rewrite that source **under a test gate** that reverts anything the
offline suite rejects. This file explains exactly what that means, what is deliberately out
of reach, and how to read the output.

```
python run.py --doctor            # what is wrong
python run.py --doctor --fix      # fix what a machine can
python run.py --repair "the api_base keeps 404ing"   # last resort: gated source edit
python run.py --backups           # what can I go back to?
python run.py --restore <id>      # go back
```

The security model this gate sits inside — what is enforced versus what is only
policy-level, and why a program the harness runs can defeat the path jail — is in
[SECURITY.md](SECURITY.md). Read §4 of that file before relying on the gate on a phone.

---

## 1. The three tiers

Every fault the harness can see falls into exactly one tier, and the doctor says which.

### Tier 1 — state and configuration (auto-fixable)

These are machine repairs. `--doctor --fix` applies them and then re-runs the checks, so the
"after" report is a measurement, not a promise. The **safe** column marks the subset the
first-run pass applies on its own, without being asked.

| Check | What it inspects | Fix (`fix_id`) | Safe |
|---|---|---|---|
| `interpreter` | Python ≥ 3.10, Pyto bridges | — (report only) | |
| `importability` | every `harness/*.py` compiles and imports | `import.restore_backup` (only when a snapshot exists) | |
| `stdlib_only` | import-hook audit + static scan for non-stdlib imports | — (a person or `--repair` must change the code) | |
| `config_present` | config file exists | `config.create` (starter file, mode 0600, **no key**) | |
| `config_parses` | the file is valid JSON | — (never rewritten: it may hold the key) | |
| `config_schema` | expected keys, unknown keys, value types | `config.schema_repair` (bad value → the value the harness would use) | |
| `config_permissions` | mode is `0600` on POSIX, **including `config.json.bak` and every other copy** | `config.chmod` | ✅ |
| `file_permissions` | sessions dir/logs, `memory.json`, spill files, `capabilities.json`, the state and workspace directories are owner-only | `permissions.tighten` | |
| `api_key_present` | a key is set | — **human only** | |
| `api_key_shape` | not a placeholder, plausible length | — **human only** | |
| `network_reachable` | DNS → TCP → TLS to the api_base host | — (names the failing layer) | |
| `api_auth` | one 1-token request, status classified | `api_base.rewrite` on 404 (probes `/v1`-shaped variants) | |
| `model_accepted` | the configured model is usable | `model.rewrite` (ordered fallback list) | |
| `workspace` | exists, writable, free space | `workspace.create` | ✅ |
| `sessions` | dir + integrity scan of the newest logs | `session.repair_safe` (torn tail) / `session.repair_full` (quarantine, compact) | ✅ / |
| `memory_headroom` | `os_proc_available_memory()` vs 800/500 MB | — (a doctor cannot free memory) | |
| `ios_modules` | which Pyto bridges import | — | |
| `ios_signatures` | `inspect`-only discovery of fragile call shapes → `capabilities.json` | — (persists a cache) | |
| `shortcuts_wiring` | this install's `pyto://` URL, `SHORTCUTS.md` | `shortcuts.write_doc` | |
| `selftest` (`--deep`) | the offline suite | — (this is the gate for tier 2) | |

Data-safety rules in tier 1:

* A **torn** final session line is truncated to the last complete row, and the fragment is
  kept as `<log>.torn`. The rest of the log is untouched — there is a test that asserts the
  surviving event count.
* A log with a bad header or a corrupt middle row is **quarantined** to `<log>.corrupt`
  (never deleted). Those bytes are the user's conversation.
* An oversized log is compacted with the same budget logic the agent loop uses at turn
  boundaries (`harness/session.py: compact`), which only ever cuts at a user message.
* `config.schema_repair` writes a `.bak` copy first, preserves the key it cannot read, and
  never invents a credential.

### Tier 2 — model-authored source repair, gated

`--repair "<problem>"` gives the model one turn with the repair tools pre-approved and the
system prompt from `harness/repair.py: REPAIR_INSTRUCTIONS`. The model reads the file it
wants to change (`read_source`), makes the smallest change it can (`self_edit`), and the
gate decides.

What the gate does, in order:

1. **Path jail.** Only `harness/*.py` and `run.py`. Absolute paths, `..`, symlinks (file or
   parent directory), anything under `tests/`, `.git/` or a backups directory, and
   non-`.py` files are refused. `tests/` matters most: an edit that could weaken the tests
   would make the gate worthless.
2. **The gate cannot edit itself.** `harness/repair.py` is refused by `apply_source_edit`
   (it *can* be restored from a snapshot — going back is safe, going forward is not).
3. **Static guards before any write.** `ast.parse(feature_version=(3, 10))` (so 3.11+ syntax
   is caught on the phone, not at the next launch) and the stdlib-only import scan from
   `stdlib_audit.py`. Empty sources and byte-identical sources are refused too.
4. **Snapshot.** `harness/` + `run.py` are copied to `~/.pyto_harness/backups/<timestamp>-<label>/`
   with a `manifest.json` of SHA-256 hashes, **signed with an HMAC keyed by a per-install
   secret** (`~/.pyto_harness/backup.key`, mode 0600, never inside the backup). A restore
   refuses a manifest that is missing, unsigned, modified or does not match the payload
   hashes, and writes nothing in that case. The previous bytes are also held in memory.
5. **Write, then run the offline test suite.**
6. **Promote or revert.** Green suite → the edit stays and the result carries a unified
   diff, the test counts and the backup id. Red suite → the previous bytes are written back
   and verified byte-for-byte, and the failure output is handed over **verbatim**. There is
   a warning telling the model not to weaken the tests to get a change through.
7. **Restore is gated the same way**, and it snapshots the current tree first
   (`pre-restore-…`), so a restore is itself undoable. If the suite still fails afterwards
   the result says so loudly (`restored-with-failing-tests`).

**How long the gate takes.** By default it is *bounded*: the fast base subset
(`test_schema`, `test_config`, `test_session`, `test_tools`, `test_ios`, `test_tools_ios`)
plus the modules that cover the file being edited (`harness/doctor.py: GATE_COVERAGE`).
That is ~3 s and ~227 tests on a laptop — a phone should budget perhaps 3-4× that. A 30 s
gate on every edit would be a footgun, and Pyto's watchdog agrees.

The whole suite is opt-in:

* `python run.py --repair --deep-tests "<problem>"`,
* `python run.py --doctor --deep --deep-tests`,
* `PYTO_HARNESS_REPAIR_GATE=full`,
* `PYTO_HARNESS_REPAIR_TEST_MODULES=test_a,test_b` (explicit list; a test seam).

Nested suite runs are capped at depth 3 (`PYTO_HARNESS_SELFTEST_DEPTH`): a suite that runs
the suite that runs the suite is a fork bomb with extra steps, so the third level refuses.

### Tier 3 — human-only, and that is the honest answer

Some faults have no machine fix on iOS. The doctor marks them `unfixable` and gives a
`human_action` string that the agent is instructed to repeat to the user instead of
inventing a workaround:

* **The API key.** Missing, expired, out of quota or lacking access to a model. The harness
  never prints it, never stores it outside the config file, and never writes a new one.
* **iOS permissions** (Photos, Calendar, Microphone/Speech, Notifications) and **entitlements**.
  No source edit can grant them; the first call shows the system prompt.
* **Anything needing App Review**: a real `subprocess`, a background daemon, Reminders,
  HealthKit, Bluetooth.
* **Compiled dependencies**: Pyto cannot `pip install`, so numpy/pandas (a paid in-app
  purchase) and any C extension are out of reach.
* **Provider-side failures** (5xx, rate limits) and **network/DNS/TLS interception**.
* **Disk and memory pressure** — the doctor can say what to delete, not delete for you.

---

## 2. Worked example

A copy of the harness with three injected faults: a world-readable config, a session log
whose last line was cut off by a simulated iOS kill, and a workspace that does not exist.

```
$ python3 run.py --doctor --no-network --workspace /tmp/demo/ws
pyto-harness doctor
========================================================================
root       : /tmp/demo/harness-copy
python     : 3.12.3 (Linux)
config     : /tmp/demo/config.json
workspace  : /tmp/demo/ws
sessions   : /tmp/demo/sessions
state      : /tmp/demo/state
network    : not checked (fast pass / --no-network)
------------------------------------------------------------------------
fail (2)
  workspace          /tmp/demo/ws does not exist
                     fix: workspace.create (run --doctor --fix)
                     you: run `--doctor --fix` to create it, or point --workspace somewhere else.
  sessions           1 log(s) need repair (torn) -- 20260101-000000-demo.jsonl: the final line
                     was not written completely (48 bytes); the rest of the log is fine
                     fix: session.repair_safe (run --doctor --fix)

warn (3)
  interpreter        Python 3.12.3 ... pyto: absent -- Pyto's iOS bridges are not importable here
                     you: the device tools only work inside Pyto on iOS ...
  config_permissions /tmp/demo/config.json is mode 0o644 -- readable by other accounts on this device
                     fix: config.chmod (run --doctor --fix)
  shortcuts_wiring   /tmp/demo/ws/SHORTCUTS.md is missing (the workspace does not exist yet);
                     URL: pyto://python//tmp/demo/harness-copy/run.py?task=summarise+my+notes+folder
                     fix: shortcuts.write_doc (run --doctor --fix)

ok (7)   importability, stdlib_only, config_present, config_parses, config_schema,
         api_key_present, api_key_shape
skipped (7)  network_reachable, api_auth, model_accepted, memory_headroom,
             ios_modules, ios_signatures, selftest
------------------------------------------------------------------------
doctor: 17 ok, 0 fixed, 2 need you (run --doctor for details)

$ echo $?
1

$ python3 run.py --doctor --fix --no-network --workspace /tmp/demo/ws
pyto-harness doctor (before)
========================================================================
... the same report as above ...

fixes applied
------------------------------------------------------------------------
config.chmod: applied
  set /tmp/demo/config.json to mode 0o600
workspace.create: applied
  created /tmp/demo/ws
session.repair_safe: applied
  20260101-000000-demo.jsonl: truncated 48 torn byte(s); the fragment is in
  20260101-000000-demo.jsonl.torn (torn)
shortcuts.write_doc: applied
  wrote /tmp/demo/ws/SHORTCUTS.md

pyto-harness doctor (after)
========================================================================
warn (1)
  interpreter        ... (a laptop is not a phone; nothing to fix)
fixed (4)
  config_permissions set /tmp/demo/config.json to mode 0o600 -- mode 0o600
  workspace          created /tmp/demo/ws -- /tmp/demo/ws is writable (31030 MB free)
  sessions           20260101-000000-demo.jsonl: truncated 48 torn byte(s) ... -- 1 log(s),
                     newest 1 scanned cleanly
  shortcuts_wiring   wrote /tmp/demo/ws/SHORTCUTS.md -- ... describes this installation
ok (7) / skipped (7) ...
------------------------------------------------------------------------
doctor: 15 ok, 4 fixed, 0 need you (run --doctor for details)

before: doctor: 17 ok, 0 fixed, 2 need you (run --doctor for details)
after : 4 fixed, 0 need you, 0 failed

$ echo $?
0
```

Healed state, measured afterwards: config mode `0o600`; the log is 549 → 501 bytes with the
48-byte fragment kept in `….jsonl.torn` and **all 4 events preserved**; the workspace and
`SHORTCUTS.md` exist.

Now a `--repair` turn where the model proposes a patch that breaks the tests:

```
$ python3 run.py --repair "the model default looks wrong" --api-base http://127.0.0.1:PORT --yolo
repairing: the model default looks wrong
------------------------------------------------------------------------
  -> self_edit(path='harness/config.py', new_source='"""Configuration: file, then environment...')
  <- [!!] self_edit (177 ms)
     | not ok: reverted (harness/config.py)
     | reason: the offline test suite failed, so the edit was reverted
     | backup: 20261004-194158-pre-edit-config-py-3856b4
     | tests: 31 ran, 1 failure(s), 0 error(s) (subprocess)
     | warning: the file is byte-for-byte what it was before the edit (verified identical)
     | warning: the test output above is verbatim; do not weaken the tests to make an edit pass
     | error:
     | FAIL: test_defaults_when_nothing_is_set (tests.test_config.TestPrecedence...)
     |   self.assertEqual(config.model, "deepseek-chat")
     | AssertionError: 'broken-model' != 'deepseek-chat'
     | Ran 31 tests in 0.003s
     | FAILED (failures=1)
     | diff:
     | --- harness/config.py (before)
     | +++ harness/config.py (after)
     | -DEFAULT_MODEL = "deepseek-chat"
     | +DEFAULT_MODEL = "broken-model"

[repair] self_edit: reverted (tests: 31 ran, 1 failure(s), 0 error(s))
[repair] nothing was kept; the tree is unchanged (see the tool output above)
$ echo $?
1
```

The SHA-256 of `harness/config.py` is identical before and after, and the pre-edit snapshot
is on disk. When the same turn proposes a *good* change, the output says `promoted`, the
file changes, and `--backups` / `--restore <id>` can undo it.

---

## 3. The agent's side of this

| Tool | Class | What it does |
|---|---|---|
| `diagnose(network=false, deep=false)` | auto | compact report: ids, statuses, actions. `network=true` adds DNS/TLS/auth (one tiny request). |
| `apply_fix(fix_id)` | **ask** | one named repair from the table above, then re-checks. |
| `selftest(full=false)` | auto | the offline suite, counts + first failures. |
| `read_source(path)` | auto | read `harness/*.py` / `run.py` (the workspace tools cannot reach them). |
| `self_edit(path, new_source / old_string+new_string, reason, full_tests=false)` | **ask** | the gated edit. |
| `list_backups()` | **ask** | snapshots, newest first. |
| `restore_backup(backup_id)` | **ask** | put one back, gated and undoable. |

The system prompt tells the model: when something fails twice for the same reason, run
`diagnose` first; if it names a fix, `apply_fix` it; only then consider `self_edit`, with the
smallest change and `selftest` first; and **never** edit files to work around a missing iOS
permission — report the `human_action` instead.

---

## 4. What self-repair cannot do on iOS — the honest list

* It cannot give itself a permission, an entitlement, or a capability Pyto does not expose.
* It cannot install a package, or make a compiled dependency work.
* It cannot run unattended: there is no daemon, and iOS kills the app.
* It cannot fix the network, the provider, or a wrong/expired API key.
* It cannot verify a change on the device beyond the offline suite — a green gate means
  "nothing the tests cover broke", not "this is correct". That is why the diff and the
  snapshot are always reported, and why the running process still holds the old module until
  you restart.
* It cannot be trusted to gate itself with a *weakened* suite: the gate refuses to touch
  `tests/`, and every revert tells the model not to edit tests to pass.
* **On a real device the gate may judge modules rather than the bytes just written.** There
  is no usable `subprocess` on Pyto, so the suite runs **in this process** with
  `unittest.TestLoader`; the tests import `harness.*` from `sys.modules`, i.e. the
  pre-edit code. A syntactically valid edit that only takes effect on the next launch can
  therefore pass a gate that never loaded it — the gate is not wrong about the tests, it is
  testing the *old* module. The gate is honest about the mode (`in-process` in the result)
  and the promoted result always carries "restart the harness to load the change", but do
  not read a green in-process gate as evidence about the bytes on disk.
* **The gate suite itself assumes desktop subprocess semantics**, so on the phone it can be
  red before any edit: the `run_program` tests in `tests/test_tools_ios.py` assert
  `mode == "subprocess"`, which is what a laptop has and what Pyto's `subprocess` shim is
  not. Those failures cascade (`self_edit` reverts every edit, `--repair` can never promote,
  `--doctor --deep` reports a permanent failure) and they say nothing about the edit under
  judgement. The suite size in the README (585) is the count on CPython 3.10/3.12 on a
  desktop; it is not a device-proven number. Treat the on-device gate as a smoke test, not
  as the verification story — and re-run the suite on a computer before trusting a change.

The security model behind those guardrails — what is enforced, what is only policy-level,
and why an in-process program can defeat the jail — is in [SECURITY.md](SECURITY.md).

If a repair loop ever goes wrong, the escapes are `--backups` (everything is a directory
with hashes), `--restore <id>`, and simply re-copying the folder: nothing in the harness
modifies anything outside `~/.pyto_harness/`, the workspace, and its own `harness/` + `run.py`.
