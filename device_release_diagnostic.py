#!/usr/bin/env python3
"""Create a private, shareable Pyto/iOS acceptance report for release v1.0.15.

Save this file beside ``run.py`` in the installed pyto-agent folder, open it in Pyto,
and press Run. It uses only the standard library and the harness already in that folder.

Automatic checks make no network requests and apply no doctor fixes. The focused behavior
suite runs selected offline tests inside the installed Python process, using disposable
temporary folders. It does not use the user's workspace, call the configured model, or open
the Shortcuts, file picker, approval or other native UI. The built-in doctor
may make and remove a temporary workspace write-probe file and retains the app's normal
legacy state-directory migration. It reads configuration metadata and scans a bounded
number of recent session logs for integrity; log contents are not copied into the report.
On iOS, the script imports Foundation and UIKit, then probes each read-only UIDevice property
separately so an unsupported selector does not hide successful framework imports or other
property reads. It does not open permission prompts, read the clipboard/photos/calendar, or
run saved programs. Manual acceptance checks are always written as NOT RUN until a person
records results.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import importlib
import io
import os
import platform
import re
import shutil
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


RELEASE_VERSION = "1.0.15"

# These are deliberately narrow, offline checks. They run in Pyto's Python process against
# disposable test workspaces; they do not simulate or certify native UI behavior. Keep the
# case list explicit so a new test module cannot accidentally introduce network, subprocess,
# permission-prompt or user-workspace activity into this device diagnostic.
SCRIPTED_TEST_GROUPS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "03 · Managed program execution",
        "Run isolation, output capture, process-state restoration, concurrency and timeout recovery.",
        (
            "tests.test_tools_ios.TestRunProgram.test_arguments_are_passed",
            "tests.test_tools_ios.TestRunProgram.test_in_process_captures_stdout_and_exit_code",
            "tests.test_tools_ios.TestRunProgram.test_in_process_runs_do_not_overlap_or_mix_output",
            "tests.test_tools_ios.TestRunProgram.test_in_process_survivor_holds_the_lane_until_it_exits",
            "tests.test_tools_ios.TestRunProgram.test_in_process_waits_for_program_threads_and_restores_state_after_error",
        ),
    ),
    (
        "06 · Saved-program library",
        "Check persistence across a workspace reload and a saved batch run without a model client.",
        (
            "tests.test_programs.TestProgramLibrary.test_records_survive_workspace_reopen_and_keep_unicode_and_spaces",
            "tests.test_programs.TestProgramLibrary.test_explicit_saved_run_uses_registered_runner_without_model_client",
        ),
    ),
    (
        "07 · Program input validation",
        "Check typed values, validation failures and Unicode/equals-sign argument parsing; no real picker is opened.",
        (
            "tests.test_program_inputs.TestProgramInputSchema.test_values_are_typed_validated_and_not_saved_anywhere",
            "tests.test_program_inputs.TestProgramInputSchema.test_terminal_prompt_and_cli_values_parse_numbers_choices_and_path_picker",
            "tests.test_tools_ios.TestRunProgram.test_registered_input_program_uses_validated_main_contract",
        ),
    ),
    (
        "08 · Simulated interruption recovery",
        "Check that recorded-but-unfinished work is reconciled and unknown side effects are not replayed.",
        (
            "tests.test_session.TestInterruptedToolRecovery.test_started_action_without_saved_result_is_reported_as_unknown",
            "tests.test_session.TestInterruptedToolRecovery.test_intent_without_start_is_not_replayed_and_reconciliation_is_idempotent",
        ),
    ),
    (
        "09 · Project context persistence",
        "Check project-brief isolation and persistence through a resumed edit session.",
        (
            "tests.test_programs.TestProgramLibrary.test_project_briefs_are_isolated_and_show_live_files_and_verification",
            "tests.test_programs.TestProgramLibrary.test_project_requirement_survives_compaction_and_a_new_edit_session",
        ),
    ),
    (
        "12 · Shortcuts saved-run contract",
        "Check saved-run opt-in, typed arguments and refusals without invoking the Shortcuts app.",
        (
            "tests.test_shortcut_saved_run.TestShortcutSavedRun.test_shortcut_arguments_parse_without_shell_or_url_decoding",
            "tests.test_shortcut_saved_run.TestShortcutSavedRun.test_shortcut_requires_explicit_unattended_opt_in_before_execution",
            "tests.test_shortcut_saved_run.TestShortcutSavedRun.test_shortcut_runs_trusted_batch_program_with_validated_unicode_input_without_api_key",
            "tests.test_shortcut_saved_run.TestShortcutSavedRun.test_shortcut_rejects_invalid_or_missing_input_before_running_code",
            "tests.test_shortcut_saved_run.TestShortcutSavedRun.test_shortcut_does_not_open_file_picker_for_at_pick",
            "tests.test_shortcut_saved_run.TestShortcutSavedRun.test_shortcut_rejects_interactive_apps_without_running_them",
        ),
    ),
)

MANUAL_CHECKS: Tuple[Tuple[str, str, str], ...] = (
    (
        "01 · Readable output",
        "In Pyto chat, request a Pyto API lookup, then repeat with verbose mode and with streaming disabled. Trigger one harmless program error and deny one protected action.",
        "Normal output stays concise; verbose details are available; each answer appears once; errors and denials are understandable; credentials stay redacted.",
    ),
    (
        "02 · Chat lifecycle",
        "Open the GUI, complete two turns, Stop a slow request, then close during another request and reopen the session.",
        "The window remains responsive; Stop permits another turn; close/reopen leaves no stale callback or write to a closed view/session.",
    ),
    (
        "03 · Managed program execution",
        "Run sequential programs with arguments. Try a program that changes cwd/environment and raises. Start a 4-second sleeper with a 0.5-second limit, then request a conflicting run while it is still active.",
        "Output and process state are restored; the live worker is reported and conflicting work is refused until it exits; later runs recover. Do not infer sandboxing or guaranteed thread termination.",
    ),
    (
        "04 · In-app approvals",
        "Request share_text and open_url. Deny each, then allow each once. Queue two protected actions and try Stop/Close with an approval visible.",
        "The visible prompt identifies the action and arguments; denied actions do not run; allowed actions run once; stale taps, Stop and Close cannot approve another request.",
    ),
    (
        "05 · Interactive preview",
        "Open the counter/form example, leave it open for more than 30 seconds, exercise controls, trigger a visible validation/callback error, close it, and rerun after an edit.",
        "An open preview is not treated as a timed-out batch; updates/errors are visible; close and rerun work and surviving work is reported honestly.",
    ),
    (
        "06 · Saved-program library",
        "Save a reusable batch program and an interactive app. List Programs, run the batch with `/run ID`, edit it with `/edit ID`, and reopen the library after restarting Pyto.",
        "Titles, ids, entry files and verification state persist; the selected program supplies edit context; batch runs can be reused without an unnecessary model request.",
    ),
    (
        "07 · Program inputs and pickers",
        "Exercise text, number, choice and boolean inputs, plus file/folder selection and cancellation. Include Unicode, spaces and `=` in a value; try invalid and missing values.",
        "Values arrive unchanged; invalid or missing fields stop before source execution; picker cancellation is clear; headless/Shortcut `@pick` requests are refused before execution.",
    ),
    (
        "08 · Interruption and recovery",
        "Interrupt a task after a side effect may have started, force-quit Pyto, reopen the session and resume. Use a harmless marker file or disposable test data.",
        "An outcome that cannot be known is reported as unknown and is not replayed automatically; the user gets a clear reconciliation step.",
    ),
    (
        "09 · Durable project context",
        "Save a project instruction/memory, close and reopen the same project, then open another project and ask what context is available.",
        "The intended context persists and is available in its project; unrelated project context does not leak across projects.",
    ),
    (
        "10 · Phone layout and history",
        "On a small phone, focus the keyboard, send a long request, inspect older history, rotate the device, and repeat with an approval visible.",
        "Send/Stop/Allow/Deny stay reachable; transcript updates remain usable; older details can be inspected; rotation and keyboard do not hide controls or corrupt the session.",
    ),
    (
        "11 · Agent workflow and Objective-C recipes",
        "Ask for a one-time action, a reusable batch tool and an interactive app. Run `examples/objc_framework_recipes.py` from the harness folder.",
        "The agent chooses the right flow and reports what it actually verified. Foundation/NSBundle and UIKit/UIDevice recipe results are read-only; framework import success does not prove unrelated selectors, permissions or entitlements.",
    ),
    (
        "12 · Shortcuts handoff",
        "Run a reviewed saved batch program from Pyto's Run Script action. Pass the program id and each input as separate arguments, including Unicode, spaces and `=`. Also try omitting the unattended opt-in and a required input.",
        "Valid output returns without an LLM request; missing opt-in/inputs, invalid values, pickers and app-mode entries stop before execution. Record foreground/background and cancellation behavior.",
    ),
    (
        "13 · Complete novice workflow",
        "In a disposable folder, build/save/reopen/run the clipboard notebook twice and edit it; preview/apply/undo an organizer plan; reopen a persistent counter/form; test Photos denial and recovery; then exercise interruption recovery.",
        "Record completion for each journey, source/path edits, repeated explanations and rerun time. Organizer changes are restored by Undo; permission denial never reports a false save. Keep real files and personal photos out of this test.",
    ),
)


def _is_ios_runtime() -> bool:
    return sys.platform == "ios" or "ios" in platform.platform().lower()


def _candidate_roots(script_path: Path, explicit_root: Optional[str]) -> Iterable[Path]:
    if explicit_root:
        yield Path(explicit_root).expanduser()
    for start in (script_path.parent, Path.cwd()):
        yield start
        yield from start.parents


def find_harness_root(script_path: Path, explicit_root: Optional[str]) -> Optional[Path]:
    seen = set()
    for candidate in _candidate_roots(script_path, explicit_root):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "run.py").is_file() and (resolved / "harness" / "doctor.py").is_file():
            return resolved
    return None


def _device_probe() -> Dict[str, str]:
    if not _is_ios_runtime():
        return {
            "status": "SKIPPED",
            "detail": "This interpreter does not identify as iOS; Foundation/UIKit are not probed on desktop.",
            "model": "",
            "system": "",
            "ios_version": "",
        }
    try:
        from Foundation import NSBundle  # noqa: F401 - verifies the documented module/class import
    except Exception as exc:  # A device report should survive an unavailable bridge.
        return {
            "status": "FAIL",
            "detail": "Foundation.NSBundle import failed with {}.".format(type(exc).__name__),
            "model": "",
            "system": "",
            "ios_version": "",
        }
    try:
        from UIKit import UIDevice
    except Exception as exc:
        return {
            "status": "FAIL",
            "detail": "Foundation.NSBundle imported; UIKit.UIDevice import failed with {}.".format(type(exc).__name__),
            "model": "",
            "system": "",
            "ios_version": "",
        }

    try:
        current_device = UIDevice.currentDevice
        device = current_device() if callable(current_device) else current_device
    except Exception as exc:
        return {
            "status": "WARN",
            "detail": "Foundation.NSBundle and UIKit.UIDevice imports passed; UIDevice.currentDevice access failed with {}.".format(type(exc).__name__),
            "model": "",
            "system": "",
            "ios_version": "",
        }

    values: Dict[str, str] = {"model": "", "system": "", "ios_version": ""}
    successful = []
    failures = []
    for attribute, key in (("model", "model"), ("systemName", "system"), ("systemVersion", "ios_version")):
        try:
            values[key] = str(getattr(device, attribute))
            successful.append(attribute)
        except Exception as exc:
            failures.append("{} ({})".format(attribute, type(exc).__name__))

    if failures:
        detail = "Framework imports and UIDevice.currentDevice access passed; read {}. Failed: {}.".format(
            ", ".join(successful) if successful else "no UIDevice properties",
            ", ".join(failures),
        )
        status = "WARN"
    else:
        detail = "Foundation.NSBundle and UIKit.UIDevice imported; UIDevice.currentDevice access and all three read-only properties passed."
        status = "PASS"
    return {"status": status, "detail": detail, **values}


def _skipped_behavior_checks(reason: str) -> List[Dict[str, str]]:
    checks = [
        {"title": title, "status": "SKIPPED", "detail": reason}
        for title, _scope, _cases in SCRIPTED_TEST_GROUPS
    ]
    checks.insert(
        5,
        {
            "title": "11 · Read-only Objective-C recipes",
            "status": "SKIPPED",
            "detail": reason,
        },
    )
    return checks


def _run_test_group(title: str, scope: str, cases: Sequence[str]) -> Dict[str, str]:
    started = time.monotonic()
    try:
        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        for case_name in cases:
            suite.addTests([loader.loadTestsFromName(case_name)])
        output = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
    except Exception as exc:
        return {
            "title": title,
            "status": "ERROR",
            "detail": "The scripted checks could not start ({}). {}".format(type(exc).__name__, scope),
        }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if result.testsRun == 0:
        status = "ERROR"
        detail = "No scripted cases ran. {}".format(scope)
    elif result.failures or result.errors:
        status = "FAIL"
        failed = [test.id().rsplit(".", 1)[-1] for test, _message in result.failures + result.errors]
        detail = "{} case(s) failed or errored: {}. Failure text omitted to avoid recording local paths. {}".format(
            len(failed), ", ".join(failed), scope
        )
    elif result.skipped:
        status = "WARN"
        detail = "{} case(s) ran; {} skipped in {} ms. {}".format(
            result.testsRun, len(result.skipped), elapsed_ms, scope
        )
    else:
        status = "PASS"
        detail = "{} case(s) passed in {} ms. {}".format(result.testsRun, elapsed_ms, scope)
    return {"title": title, "status": status, "detail": detail}


def _run_objc_recipe(root: Path) -> Dict[str, str]:
    title = "11 · Read-only Objective-C recipes"
    recipe_path = root / "examples" / "objc_framework_recipes.py"
    if not recipe_path.is_file():
        return {"title": title, "status": "SKIPPED", "detail": "The Objective-C recipe file is not in this installation."}
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(root))
        module = importlib.import_module("examples.objc_framework_recipes")
        bundle_path = module.app_bundle_path()
        device = module.device_summary()
        if not bundle_path or not all(device.get(key) for key in ("model", "system", "version")):
            raise ValueError("a read-only recipe returned an empty value")
    except Exception as exc:
        return {
            "title": title,
            "status": "FAIL",
            "detail": "A read-only Foundation/UIKit recipe failed with {}. Returned device values and paths are withheld.".format(
                type(exc).__name__
            ),
        }
    finally:
        sys.path[:] = original_path
    return {
        "title": title,
        "status": "PASS",
        "detail": "Foundation bundle path and UIKit device properties were read. Values and paths are withheld; no permission or entitlement was tested.",
    }


def run_device_behavior_checks(root: Optional[Path], source_version: str) -> List[Dict[str, str]]:
    """Run focused, offline behavior checks only in a matching Pyto installation."""
    if not _is_ios_runtime():
        return _skipped_behavior_checks(
            "Not run: this report was generated outside iOS/Pyto, so it cannot count as device evidence."
        )
    if root is None:
        return _skipped_behavior_checks("Not run: the installed harness source folder was not found.")
    if source_version != RELEASE_VERSION:
        return _skipped_behavior_checks(
            "Not run: diagnostic v{} requires matching harness source; found v{}. Update the installed source first.".format(
                RELEASE_VERSION, source_version or "unknown"
            )
        )
    if not (root / "tests").is_dir():
        return _skipped_behavior_checks(
            "Not run: the focused test files are missing. Use the complete release installation or source ZIP."
        )

    original_env = dict(os.environ)
    original_cwd = os.getcwd()
    original_argv = sys.argv
    original_argv_value = list(sys.argv)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_path = list(sys.path)
    original_dont_write_bytecode = sys.dont_write_bytecode
    checks: List[Dict[str, str]] = []
    try:
        sys.path.insert(0, str(root))
        sys.dont_write_bytecode = True
        for title, scope, cases in SCRIPTED_TEST_GROUPS:
            checks.append(_run_test_group(title, scope, cases))
        checks.insert(5, _run_objc_recipe(root))
    except Exception as exc:
        skipped = _skipped_behavior_checks(
            "The behavior suite stopped with {}.".format(type(exc).__name__)
        )
        completed_titles = {item["title"] for item in checks}
        checks.extend(item for item in skipped if item["title"] not in completed_titles)
    finally:
        support = sys.modules.get("tests.support")
        test_home = getattr(support, "_HARNESS_HOME", "") if support else ""
        if test_home:
            shutil.rmtree(test_home, ignore_errors=True)
        try:
            os.chdir(original_cwd)
        except OSError:
            pass
        os.environ.clear()
        os.environ.update(original_env)
        original_argv[:] = original_argv_value
        sys.argv = original_argv
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_dont_write_bytecode
    return checks


def _redact(text: str, ctx: Any) -> str:
    try:
        from harness import doctor

        text = doctor.scrub_secrets(text, ctx)
    except Exception:
        pass
    replacements = (
        (getattr(ctx, "root", ""), "<harness folder>"),
        (getattr(ctx, "home", ""), "<home>"),
        (getattr(ctx, "config_path", ""), "<config file>"),
        (getattr(ctx, "state", ""), "<state folder>"),
        (getattr(ctx, "workspace", ""), "<workspace>"),
        (getattr(ctx, "sessions_dir", ""), "<sessions folder>"),
    )
    for value, replacement in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        if value:
            text = text.replace(str(value), replacement)
    api_base = getattr(getattr(ctx, "config", None), "api_base", "")
    if api_base:
        text = text.replace(str(api_base), "<API endpoint>")
    text = re.sub(r"https?://[^\s)\]}>,]+", "<URL>", text)
    text = re.sub(r"(?<![A-Za-z0-9:])/(?:[^/\s]+/)*[^/\s,;)]*", "<path>", text)
    text = re.sub(r"[A-Za-z]:\\(?:[^\\\s]+\\?)+", "<path>", text)
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def run_local_doctor(root: Optional[Path]) -> Dict[str, Any]:
    if root is None:
        return {
            "status": "NOT RUN",
            "summary": "Harness source not found. Save this script beside run.py and harness/ to run local checks.",
            "rows": [],
            "migration": "",
        }
    try:
        sys.path.insert(0, str(root))
        harness_run = importlib.import_module("run")
        harness = importlib.import_module("harness")
        doctor = importlib.import_module("harness.doctor")
        parser = harness_run.build_parser()
        args = parser.parse_args(["--doctor", "--no-network"])
        config, config_error = harness_run.resolve_config_lenient(args)
        ctx = doctor.DoctorContext.for_config(
            config,
            root=str(root),
            network=False,
            deep=False,
            deep_tests=False,
            persist=False,
            workspace=args.workspace or None,
        )
        ctx.config_error = config_error
        results = doctor.run_checks(ctx)
        rows: List[Dict[str, str]] = []
        for item in results:
            if item.id in ("api_key_present", "api_key_shape"):
                detail = "Credential check recorded; credential contents and fingerprint withheld."
                action = ""
            else:
                detail = _redact(item.detail, ctx)
                action = _redact(item.human_action, ctx) if item.human_action else ""
            rows.append(
                {
                    "id": str(item.id),
                    "status": str(item.status).upper(),
                    "detail": detail or "(no detail)",
                    "action": action,
                    "fix_id": str(item.fix_id or ""),
                }
            )
        needs_help = doctor.needs_attention(results)
        summary = doctor.summary_line(results)
        return {
            "status": "PASS" if needs_help == 0 else "NEEDS ATTENTION",
            "summary": _redact(summary, ctx),
            "rows": rows,
            "migration": _redact(getattr(ctx, "state_migration", ""), ctx),
            "version": str(getattr(harness, "__version__", "unknown")),
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "summary": "Local doctor setup failed with {}. Check that the script is beside the release source, then run `python run.py --doctor --no-network` for the local error.".format(type(exc).__name__),
            "rows": [],
            "migration": "",
        }


def _safe_markdown(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def build_report(script_path: Path, explicit_root: Optional[str]) -> str:
    root = find_harness_root(script_path, explicit_root)
    device = _device_probe()
    doctor_result = run_local_doctor(root)
    behavior_checks = run_device_behavior_checks(root, doctor_result.get("version", ""))
    generated = _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat()
    runtime = "iOS / Pyto candidate" if _is_ios_runtime() else "non-iOS runtime"
    version = doctor_result.get("version", "unknown")

    lines = [
        "# pyto-agent v{} device diagnostic".format(RELEASE_VERSION),
        "",
        "> Automatic checks below are not native acceptance. Manual items remain **NOT RUN** until a person performs and records them on the target device.",
        "",
        "## Run details",
        "",
        "- Generated (UTC): `{}`".format(generated),
        "- Python: `{}`".format(_safe_markdown(sys.version.split()[0])),
        "- Runtime: `{}`".format(_safe_markdown(runtime)),
        "- Harness source version: `{}`".format(_safe_markdown(version)),
        "- Device model: `{}`".format(_safe_markdown(device.get("model") or "fill in if not detected")),
        "- System: `{}`".format(_safe_markdown(device.get("system") or "fill in if not detected")),
        "- iOS version: `{}`".format(_safe_markdown(device.get("ios_version") or "fill in if not detected")),
        "- Pyto app version: `FILL IN`",
        "- Harness version matches this report: `{}`".format("yes" if version == RELEASE_VERSION else "no / source not detected"),
        "",
        "## Automatic checks",
        "",
        "### Objective-C bridge probe",
        "",
        "- **{}** — {}".format(device["status"], _safe_markdown(device["detail"])),
        "- Probe scope: documented `Foundation.NSBundle` / `UIKit.UIDevice` imports and isolated read-only device properties. It does not test permissions, entitlements, or arbitrary selectors.",
        "",
        "### Built-in offline doctor",
        "",
        "- **{}** — {}".format(doctor_result["status"], _safe_markdown(doctor_result["summary"])),
        "- Network checks: **disabled**. Fixes: **not applied**. Diagnostic health snapshot: **not written by this script**.",
        "- Doctor initialization follows the harness's normal legacy state-directory migration behavior.{}".format(
            " Migration noted: {}".format(_safe_markdown(doctor_result["migration"]))
            if doctor_result.get("migration")
            else ""
        ),
        "- The doctor reads configuration metadata and checks recent session-log integrity. It may create and remove a temporary workspace probe file; session contents are not copied into this report.",
    ]
    if doctor_result["rows"]:
        lines.extend(["", "| Check | Status | Result |", "|---|---|---|"])
        for row in doctor_result["rows"]:
            result_text = row["detail"]
            if row["action"]:
                result_text += " Action: " + row["action"]
            if row["fix_id"]:
                result_text += " Fix id: " + row["fix_id"] + " (not applied)"
            lines.append(
                "| `{}` | **{}** | {} |".format(
                    _safe_markdown(row["id"]),
                    _safe_markdown(row["status"]),
                    _safe_markdown(result_text),
                )
            )
    lines.extend(
        [
            "",
            "### Focused on-device behavior scripts",
            "",
            "These seven scripted checks run only when this diagnostic matches the installed harness version and the runtime identifies as iOS. Test programs use disposable temporary folders. They make no provider/network requests and do not open the Shortcuts app, pickers, approval prompts or other native UI. Some cases use test fixtures to check harness logic; a scripted PASS does not establish native UI acceptance, force-quit behavior or real Shortcuts handoff.",
            "",
            "| Goal | Status | Result |",
            "|---|---|---|",
        ]
    )
    for item in behavior_checks:
        lines.append(
            "| {} | **{}** | {} |".format(
                _safe_markdown(item["title"]),
                _safe_markdown(item["status"]),
                _safe_markdown(item["detail"]),
            )
        )
    lines.extend(
        [
            "",
            "Scripted results do not change the manual checklist statuses below. Update those only after performing the listed on-device interaction and recording evidence.",
            "",
            "## Device acceptance checklist",
            "",
            "Use a disposable workspace and sample files. After each check, replace `NOT RUN` with `PASS`, `FAIL`, or `BLOCKED`, then add brief evidence and timing. Do not use desktop/mock results to mark a device check passed.",
            "",
        ]
    )
    for title, steps, expected in MANUAL_CHECKS:
        lines.extend(
            [
                "### {}".format(title),
                "",
                "**Status:** `NOT RUN`",
                "",
                "**Steps:** {}".format(steps),
                "",
                "**Expected:** {}".format(expected),
                "",
                "**Evidence / notes:**",
                "",
                "",
            ]
        )
    lines.extend(
        [
            "## Sharing this report",
            "",
            "The report omits API key contents, environment variables, session logs, test output and diagnostic evidence payloads. The focused checks may create and remove disposable temporary test folders. Review your manually added notes before sharing; do not paste credentials, private URLs, copied task text, or personal file contents.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_report(report: str, requested_path: Optional[str], script_path: Path) -> Path:
    timestamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = "pyto-agent-v{}-device-report-{}.md".format(RELEASE_VERSION, timestamp)
    candidates = [Path(requested_path).expanduser()] if requested_path else [script_path.parent / filename, Path.cwd() / filename]
    last_error: Optional[Exception] = None
    for target in candidates:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report, encoding="utf-8")
            try:
                os.chmod(str(target), 0o600)
            except OSError:
                pass
            return target
        except OSError as exc:
            last_error = exc
    raise OSError("could not save the report: {}".format(last_error))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", help="path to the installed harness folder (the folder containing run.py)")
    parser.add_argument("--output", help="report Markdown path; by default save next to this script")
    args = parser.parse_args(list(argv) if argv is not None else None)
    script_path = Path(__file__).resolve()
    try:
        report = build_report(script_path, args.harness)
        output = _write_report(report, args.output, script_path)
    except Exception as exc:
        print("Diagnostic could not save its report: {}: {}".format(type(exc).__name__, exc))
        return 1
    print("pyto-agent v{} diagnostic report saved.".format(RELEASE_VERSION))
    print("Report: {}".format(output))
    print("Manual device acceptance remains NOT RUN until you complete and update the checklist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
