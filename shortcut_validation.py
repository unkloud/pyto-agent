"""Manual, no-LLM Shortcut bridge checks for Pyto.

Run this script in Pyto and select one case at a time. It calls only explicitly named
``pyto-harness-test-`` fixtures. The default path never runs stress or hang-recovery
cases. It uses Python's standard library and Pyto's shipped ``xcallback`` module only.

This is an observation harness, not a reliability guarantee. A blocking x-callback call
cannot be forcibly stopped by this script; use A6/A8 only when prepared to stop or restart
the Pyto run manually.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from harness import __version__ as KIT_VERSION
except Exception:
    KIT_VERSION = "unknown"


PREFIX = "pyto-harness-test-"
ECHO = PREFIX + "echo"
STATIC_OK = PREFIX + "return"
ERROR = PREFIX + "error"
CANCEL = PREFIX + "cancel"
WAIT = PREFIX + "wait"
SEMANTIC_FAILURE = PREFIX + "semantic-failure"
PERMISSION = PREFIX + "permission"
REPORT_PREFIX = "shortcut-validation-"

STRESS_CASES = frozenset(("A5", "A13"))
RECOVERY_CASES = frozenset(("A6", "A7", "A8"))

CASES: Dict[str, Dict[str, str]] = {
    "A1": {"kind": "call", "title": "Successful x-callback return type", "fixture": STATIC_OK},
    "A2": {"kind": "call", "title": "x-error behavior", "fixture": ERROR},
    "A3": {"kind": "call", "title": "User cancellation behavior", "fixture": CANCEL},
    "A4": {"kind": "unicode", "title": "Unicode, emoji, and newline round trip", "fixture": ECHO},
    "A5": {"kind": "stress_output", "title": "Returned output size limits", "fixture": ECHO},
    "A6": {"kind": "call", "title": "x-callback behavior with a 60-second Shortcut wait", "fixture": WAIT},
    "A7": {"kind": "manual", "title": "Call after timeout or cancellation"},
    "A8": {"kind": "manual", "title": "Recovery after a blocked run"},
    "A9": {"kind": "repeat", "title": "Ten repeated echo calls", "fixture": ECHO},
    "A10": {"kind": "echo", "title": "Plain text input", "fixture": ECHO},
    "A11": {"kind": "echo_json", "title": "JSON text input", "fixture": ECHO},
    "A12": {"kind": "path", "title": "File path passed as Shortcut input", "fixture": PREFIX + "path"},
    "A13": {"kind": "stress_input", "title": "Input size limits", "fixture": ECHO},
    "A14": {"kind": "missing", "title": "Nonexistent Shortcut name"},
    "A15": {"kind": "names", "title": "Spaces, Chinese, and emoji in names", "fixture": ECHO},
    "A16": {"kind": "manual", "title": "Duplicate Shortcut names"},
    "A17": {"kind": "manual", "title": "Shortcut enumeration"},
    "A18": {"kind": "permission", "title": "Permission prompt behavior", "fixture": PERMISSION},
    "A19": {"kind": "manual", "title": "Foreground versus background behavior"},
    "A20": {"kind": "semantic", "title": "Semantic failure returned as text", "fixture": SEMANTIC_FAILURE},
}

MANUAL_INSTRUCTIONS = {
    "A7": "The standalone script cannot impose or kill an xcallback timeout. To test the harness worker, use a disposable harness session to call the registered shortcut_run_wait on pyto-harness-test-wait (registry timeout: 30 s); then call A1 in a fresh invocation and record whether it succeeds. A timed-out worker may still be alive.",
    "A8": "Use only a disposable harness session whose JSONL path you can identify. Start the wait fixture, force-quit Pyto only if needed, then resume that session with the documented --resume option. Record whether it resumes and whether any fixture side effect completed. Do not use a personal session.",
    "A16": "Check whether the Shortcuts app permits two fixtures with the same name. If it does, run both only if you can distinguish their harmless outputs. Otherwise record that duplicate names are prevented by the UI.",
    "A17": "Record whether a documented API in this Pyto build lists Shortcut names. Do not probe undocumented URL schemes or personal data. A manual fixture directory is an acceptable result.",
    "A19": "Run the same harmless fixture once with Pyto foregrounded and once through a user-created Shortcuts automation with Show Console off. Record whether the x-callback call succeeds, prompts, suspends, or resumes.",
}


def _runtime() -> Dict[str, str]:
    return {
        "python_version": platform.python_version(),
        "python_build": sys.version.replace("\n", " "),
        "platform": sys.platform,
        "system": platform.platform(),
        "pyto_runtime": "present" if _has_module("xcallback") else "not_detected",
        "pyto_app_version": "unknown; no stable stdlib API exposes it",
    }


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _report_path() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
    return os.path.join(base, REPORT_PREFIX + stamp + ".jsonl")


def _emit(record: Dict[str, Any], report_path: str) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    print("RESULT " + line)
    try:
        with open(report_path, "a", encoding="utf-8") as output:
            output.write(line + "\n")
    except OSError as exc:
        print("REPORT_WRITE_FAILED {}".format(type(exc).__name__))


def _result(
    case_id: str,
    status: str,
    observation: str,
    report_path: str,
    **fields: Any,
) -> None:
    payload = {
        "case_id": case_id,
        "title": CASES[case_id]["title"],
        "status": status,
        "observation": observation,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "kit_version": KIT_VERSION,
        "runtime": _runtime(),
        "evidence_status": "requires_manual_device_review",
    }
    payload.update(fields)
    _emit(payload, report_path)


def _shortcut_url(name: str, input_text: Optional[str] = None) -> str:
    # Match Pyto's documented open_shortcut example: xcallback.open_url manages its own
    # callback, so pass the Shortcut action URL without adding x-success ourselves.
    query: List[Tuple[str, str]] = [("name", name)]
    if input_text is not None:
        query.extend((("input", "text"), ("text", input_text)))
    return "shortcuts://x-callback-url/run-shortcut?" + urllib.parse.urlencode(query)


def _safe_value_summary(value: Any) -> Dict[str, Any]:
    if isinstance(value, bytes):
        raw = value
        preview = value[:80].decode("utf-8", "replace")
    elif isinstance(value, str):
        raw = value.encode("utf-8")
        preview = value[:80]
    elif value is None:
        raw = b""
        preview = ""
    else:
        rendered = repr(value)
        raw = rendered.encode("utf-8", "replace")
        preview = rendered[:80]
    return {
        "type": type(value).__name__,
        "utf8_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "preview": preview,
        "preview_truncated": len(preview) >= 80,
    }


def _matches_text(value: Any, expected: str) -> bool:
    accepted = (expected, expected.rstrip("\n"))
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return isinstance(value, str) and value in accepted


def _safe_error_message(exc: Exception) -> str:
    """Return a short diagnostic with URL contents removed."""
    message = str(exc).strip()
    message = re.sub(
        r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+",
        "<redacted-url>",
        message,
    )
    return (message or "<empty>")[:240]


def _call(
    name: str,
    input_text: Optional[str],
    *,
    include_error_message: bool = False,
) -> Tuple[str, Any, str, float, str]:
    """Return transport state, value, error class, elapsed time, and optional detail."""
    try:
        import xcallback
    except Exception as exc:
        detail = _safe_error_message(exc) if include_error_message else ""
        return "failed", None, "xcallback_import_{}".format(type(exc).__name__), 0.0, detail

    started = time.monotonic()
    try:
        value = xcallback.open_url(_shortcut_url(name, input_text))
        return "ok", value, "", time.monotonic() - started, ""
    except SystemExit:
        return "cancelled", None, "SystemExit", time.monotonic() - started, ""
    except TimeoutError:
        return "indeterminate", None, "TimeoutError", time.monotonic() - started, ""
    except Exception as exc:
        detail = _safe_error_message(exc) if include_error_message else ""
        return "failed", None, type(exc).__name__, time.monotonic() - started, detail


def _confirm_fixture(case_id: str, fixture: str) -> bool:
    return _confirm_fixtures(case_id, (fixture,))


def _confirm_fixtures(case_id: str, fixtures: Sequence[str]) -> bool:
    if not fixtures or any(not fixture.startswith(PREFIX) for fixture in fixtures):
        print("Refusing to call a Shortcut outside the test prefix.")
        return False
    print("This case will call only these explicitly named test fixtures:")
    for fixture in fixtures:
        print("  - {}".format(fixture))
    answer = input("Type RUN to continue, or press Return to skip: ").strip()
    return answer == "RUN"


def _manual(case_id: str, report_path: str) -> None:
    print("MANUAL {} — {}".format(case_id, MANUAL_INSTRUCTIONS[case_id]))
    notes = input("Observation (test data only; leave blank if not run): ").strip()
    status = "observed" if notes else "unknown"
    _result(case_id, status, notes or "No manual observation recorded.", report_path)


def _run_call(case_id: str, *, fixture: str, input_text: Optional[str] = None) -> Dict[str, Any]:
    if not _confirm_fixture(case_id, fixture):
        return {"status": "not_run", "state": None, "shortcut": fixture, "value": None}
    print("CALLING {} at {} UTC".format(fixture, datetime.now(timezone.utc).isoformat()))
    state, value, error_type, elapsed, error_message = _call(
        fixture,
        input_text,
        include_error_message=case_id == "A1",
    )
    summary = _safe_value_summary(value) if state == "ok" else None
    return {
        "status": "observed",
        "transport_state": state,
        "shortcut": fixture,
        "input_utf8_bytes": len(input_text.encode("utf-8")) if input_text is not None else 0,
        "returned": summary,
        "return_type": type(value).__name__ if state == "ok" else None,
        "error_type": error_type or None,
        "error_message": error_message or None,
        "elapsed_seconds": round(elapsed, 3),
        "value": value,
    }


def _record_call(case_id: str, report_path: str, call: Dict[str, Any], observation: str) -> None:
    public_fields = {key: value for key, value in call.items() if key not in ("value", "status")}
    state = call.get("transport_state")
    public_fields.setdefault("transport_state", state or "unknown")
    _result(
        case_id,
        call.get("status", "unknown"),
        observation,
        report_path,
        **public_fields,
    )

def _run_case(case_id: str, report_path: str, *, stress: bool, recovery: bool) -> None:
    case = CASES[case_id]
    kind = case["kind"]
    if kind == "manual":
        _manual(case_id, report_path)
        return
    if case_id in STRESS_CASES and not stress:
        _result(case_id, "not_run", "Opt-in stress suite required.", report_path)
        return
    if case_id in RECOVERY_CASES and not recovery:
        _result(case_id, "not_run", "Opt-in recovery suite required.", report_path)
        return

    if kind == "call":
        fixture = case["fixture"]
        if case_id == "A6":
            print("A6 can block for up to the fixture's configured wait. There is no enforced timeout here.")
        if case_id in ("A2", "A3", "A6"):
            print("Confirm the fixture is harmless and behaves as documented before running it.")
        call = _run_call(case_id, fixture=fixture)
        observation = "{} after {}s".format(call.get("transport_state", "not run"), call.get("elapsed_seconds", "n/a"))
        if call.get("error_type"):
            observation += " ({})".format(call["error_type"])
        _record_call(case_id, report_path, call, observation)
        return

    if kind == "unicode":
        value = "中文🙂\nline-two\n"
        call = _run_call(case_id, fixture=case["fixture"], input_text=value)
        returned = call.get("value")
        exact = _matches_text(returned, value)
        public_fields = {key: item for key, item in call.items() if key not in ("value", "status")}
        observation = (
            "Unicode round trip {}.".format("matched" if exact else "did not match")
            if call.get("status") == "observed"
            else "Fixture invocation was skipped; no round trip was observed."
        )
        _result(case_id, call.get("status", "unknown"), observation, report_path, **public_fields, exact_match=exact)
        return

    if kind in ("echo", "echo_json"):
        value = "pyto-harness-test-input-42" if kind == "echo" else '{"fixture":"echo","n":7,"text":"中文🙂"}'
        call = _run_call(case_id, fixture=case["fixture"], input_text=value)
        returned = call.get("value")
        exact = _matches_text(returned, value)
        public_fields = {key: item for key, item in call.items() if key not in ("value", "status")}
        observation = (
            "Input round trip {}.".format("matched" if exact else "did not match")
            if call.get("status") == "observed"
            else "Fixture invocation was skipped; no round trip was observed."
        )
        _result(case_id, call.get("status", "unknown"), observation, report_path, **public_fields, exact_match=exact)
        return

    if kind == "path":
        fixture = case["fixture"]
        probe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PREFIX + "path-probe.txt")
        try:
            with open(probe_path, "w", encoding="utf-8") as probe:
                probe.write("PYTO_HARNESS_PATH_PROBE")
        except OSError as exc:
            _result(case_id, "failed", "Could not create the harmless path probe: {}.".format(type(exc).__name__), report_path)
            return
        print("The script created a harmless probe file at: {}".format(probe_path))
        call = _run_call(case_id, fixture=fixture, input_text=probe_path)
        returned = call.get("value")
        exact = _matches_text(returned, "PYTO_HARNESS_PATH_PROBE")
        try:
            os.unlink(probe_path)
        except OSError:
            pass
        public_fields = {key: item for key, item in call.items() if key not in ("value", "status")}
        _result(
            case_id,
            call.get("status", "unknown"),
            (
                "Shortcut returned the file marker: {}. The path probe is synthetic and can be deleted after the run.".format(exact)
                if call.get("status") == "observed"
                else "Fixture invocation was skipped; no path result was observed."
            ),
            report_path,
            **public_fields,
            exact_match=exact,
        )
        return

    if kind == "stress_output":
        fixture = case["fixture"]
        if not _confirm_fixture(case_id, fixture):
            _result(case_id, "not_run", "User skipped fixture invocation.", report_path, shortcut=fixture)
            return
        samples = []
        for size in (1024, 100 * 1024, 1024 * 1024):
            payload = "x" * size
            state, returned, error_type, elapsed, _error_message = _call(fixture, payload)
            if state != "ok":
                samples.append({"requested_input_bytes": size, "transport_state": state, "error_type": error_type, "elapsed_seconds": round(elapsed, 3)})
                break
            returned_bytes = returned if isinstance(returned, bytes) else str(returned).encode("utf-8")
            samples.append({
                "requested_input_bytes": size,
                "returned_bytes": len(returned_bytes),
                "exact_match": returned_bytes == payload.encode("utf-8"),
                "returned_sha256": hashlib.sha256(returned_bytes).hexdigest(),
                "elapsed_seconds": round(elapsed, 3),
            })
        _result(case_id, "observed", "Progressive returned-size samples recorded.", report_path, shortcut=fixture, samples=samples)
        return

    if kind == "stress_input":
        fixture = case["fixture"]
        if not _confirm_fixture(case_id, fixture):
            _result(case_id, "not_run", "User skipped fixture invocation.", report_path, shortcut=fixture)
            return
        samples = []
        for size in (1024, 16 * 1024, 100 * 1024):
            payload = "i" * size
            state, returned, error_type, elapsed, _error_message = _call(fixture, payload)
            if state != "ok":
                samples.append({"requested_input_bytes": size, "transport_state": state, "error_type": error_type, "elapsed_seconds": round(elapsed, 3)})
                break
            returned_bytes = returned if isinstance(returned, bytes) else str(returned).encode("utf-8")
            samples.append({
                "requested_input_bytes": size,
                "returned_bytes": len(returned_bytes),
                "exact_match": returned_bytes == payload.encode("utf-8"),
                "elapsed_seconds": round(elapsed, 3),
            })
        _result(case_id, "observed", "Progressive input-size samples recorded.", report_path, shortcut=fixture, samples=samples)
        return

    if kind == "repeat":
        fixture = case["fixture"]
        successes = 0
        failures = 0
        if not _confirm_fixture(case_id, fixture):
            _result(case_id, "not_run", "User skipped fixture invocation.", report_path, shortcut=fixture)
            return
        started = time.monotonic()
        for index in range(10):
            state, value, error_type, _elapsed, _error_message = _call(fixture, "repeat-{}".format(index + 1))
            if state == "ok" and _matches_text(value, "repeat-{}".format(index + 1)):
                successes += 1
            else:
                failures += 1
                print("A9 stopped at call {}: {} {}".format(index + 1, state, error_type))
                break
        _result(
            case_id,
            "observed",
            "{} calls succeeded; {} failed.".format(successes, failures),
            report_path,
            requested_calls=10,
            successful_calls=successes,
            failed_calls=failures,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return

    if kind == "missing":
        missing_name = PREFIX + "missing-" + str(int(time.time()))
        call = _run_call(case_id, fixture=missing_name)
        _record_call(case_id, report_path, call, "Nonexistent fixture lookup recorded.")
        return

    if kind == "names":
        names = (PREFIX + "space fixture", PREFIX + "中文", PREFIX + "emoji-🧪")
        if not _confirm_fixtures(case_id, names):
            _result(case_id, "not_run", "User skipped fixture invocation.", report_path, variants=list(names))
            return
        observations = []
        for name in names:
            state, value, error_type, elapsed, _error_message = _call(name, "name-check")
            observations.append({"shortcut": name, "transport_state": state, "return_type": type(value).__name__ if state == "ok" else None, "error_type": error_type or None, "elapsed_seconds": round(elapsed, 3)})
        _result(case_id, "observed", "Name variants attempted; compare returned values and error shapes.", report_path, variants=observations)
        return

    if kind == "permission":
        print("If this permission was already granted, revoke it in Settings before this check to observe the prompt.")
        call = _run_call(case_id, fixture=case["fixture"])
        notes = input("Did an iOS permission prompt appear? Enter yes/no/already-granted/unknown: ").strip().lower()
        public_fields = {key: value for key, value in call.items() if key not in ("value", "status")}
        public_fields.setdefault("transport_state", call.get("transport_state") or "unknown")
        _result(case_id, call.get("status", "unknown"), "Permission prompt observation recorded.", report_path, permission_prompt=notes or "unknown", **public_fields)
        return

    if kind == "semantic":
        call = _run_call(case_id, fixture=case["fixture"])
        returned = call.get("value")
        semantic_failure = _matches_text(returned, "PYTO_HARNESS_SEMANTIC_FAILURE")
        public_fields = {key: value for key, value in call.items() if key not in ("value", "status")}
        public_fields.setdefault("transport_state", call.get("transport_state") or "unknown")
        _result(
            case_id,
            call.get("status", "unknown"),
            "Transport success is separate from the returned failure string."
            if call.get("status") == "observed"
            else "Fixture invocation was skipped; semantic result remains unknown.",
            report_path,
            **public_fields,
            semantic_marker_seen=semantic_failure,
            semantic_result="fixture_failure_text" if semantic_failure else "unclassified",
        )
        return

    _result(case_id, "unknown", "No runner is defined for this case.", report_path)


def _usage() -> None:
    print("Pyto Shortcut validation — no LLM or network calls")
    print("Use --case A1 (A1–A20), --suite basic, stress, recovery, all, --list, or q.")
    print("Stress and recovery suites require explicit selection. Calls use only the test prefix.")
    print("A report is written beside this script when the folder is writable.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    report_path = _report_path()
    _usage()
    print("Runtime: {}".format(json.dumps(_runtime(), ensure_ascii=False, sort_keys=True)))
    if args and args[0].lower() in ("--case", "--suite"):
        if len(args) < 2:
            print("Expected a value after {}.".format(args[0]))
            return 2
        selection = args[1].strip().upper()
    elif args and args[0].lower() in ("--list", "list"):
        selection = "LIST"
    elif args:
        selection = args[0].strip().upper()
    else:
        selection = input("Select case or suite: ").strip().upper()
    if selection in ("Q", "QUIT", "EXIT", ""):
        return 0
    if selection == "LIST":
        for case_id, case in CASES.items():
            print("{}  {} [{}]".format(case_id, case["title"], case["kind"]))
        return 0

    if selection in CASES:
        _run_case(selection, report_path, stress=selection in STRESS_CASES, recovery=selection in RECOVERY_CASES)
        return 0

    suites = {
        "BASIC": ["A1", "A2", "A3", "A4", "A9", "A10", "A11", "A14", "A15", "A18", "A20"],
        "STRESS": ["A5", "A13"],
        "RECOVERY": ["A6", "A7", "A8"],
        "ALL": list(CASES),
    }
    if selection not in suites:
        print("Unknown selection. Choose A1–A20, basic, stress, recovery, list, or q.")
        return 2
    if selection in ("STRESS", "RECOVERY", "ALL"):
        expected = "RUN " + selection
        if input("This suite can send large inputs or block Pyto. Type {} to continue: ".format(expected)).strip() != expected:
            print("Suite skipped.")
            return 0
    for case_id in suites[selection]:
        _run_case(
            case_id,
            report_path,
            stress=selection in ("STRESS", "ALL"),
            recovery=selection in ("RECOVERY", "ALL"),
        )
    print("Report: {}".format(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
