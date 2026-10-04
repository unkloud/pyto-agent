#!/usr/bin/env python3
"""pyto-harness entry point.

    python run.py "rename my screenshots by date"      one task, then exit
    python run.py                                       interactive terminal chat
    python run.py --ui                                  Pyto chat window
    python run.py --resume <session.jsonl> "and again"  continue a previous chat
    python run.py --dry-run "..."                       print the request, send nothing
    python run.py --capabilities                        what this device can do
    python run.py --doctor                              diagnose this installation
    python run.py --doctor --fix                        repair what a machine can
    python run.py --repair "<what is broken>"           gated edit of the harness source
    python run.py --backups                             list source snapshots
    python run.py --restore <backup_id>                 put a snapshot back

Nothing outside the workspace is written, and nothing outside the configured API host
is contacted.  The first normal run of a day also runs a fast local health pass and
prints one line about it; `--no-doctor` or PYTO_HARNESS_NO_DOCTOR=1 turns that off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# Allow `python run.py` from anywhere: the harness package sits next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import __version__, doctor, home, ios, repair  # noqa: E402
from harness.config import (  # noqa: E402
    Config,
    ConfigError,
    default_config_path,
    default_sessions_dir,
    default_workspace,
    describe,
    enforce_config_mode,
    ensure_workspace,
    load_config,
    redact_key,
    write_sample_config,
)
from harness.errors import HarnessError, UnsupportedCapability  # noqa: E402
from harness.loop import (  # noqa: E402
    LoopOptions,
    build_system_prompt,
    client_from_config,
    make_policy,
    prompter_is_interactive,
    run_turn,
)
from harness.session import SessionLog  # noqa: E402
from harness.tools import ToolRegistry  # noqa: E402
from harness.tools_ios import build_registry, default_context  # noqa: E402
from harness.ui import Printer, TerminalApprover, open_session, run_turn_sync  # noqa: E402

#: Tools the ``--repair`` turn may use without an approval prompt.
#:
#: The user asked for exactly this repair, so asking again would be theatre -- but only
#: these seven are pre-approved.  Sharing, URLs and Shortcuts still go through the normal
#: policy, and in a headless run they are denied as usual.
REPAIR_ALLOWED_TOOLS = (
    "diagnose",
    "selftest",
    "read_source",
    "self_edit",
    "apply_fix",
    "list_backups",
    "restore_backup",
)


def resolve_config_lenient(args: argparse.Namespace) -> Tuple[Config, str]:
    """Resolve configuration for the doctor/repair commands.

    Those commands have to work *when the configuration is the problem*: a config file
    that is invalid JSON must not stop ``--doctor`` from telling you so.  If the normal
    loader raises, this builds the same defaults from the CLI flags and environment and
    hands back the message so the doctor can report it.
    """
    try:
        return resolve_config(args), ""
    except ConfigError as exc:
        config = Config()
        environ = os.environ
        if args.api_base:
            config.api_base = args.api_base
        elif environ.get("PYTO_HARNESS_API_BASE"):
            config.api_base = environ["PYTO_HARNESS_API_BASE"]
        if args.model:
            config.model = args.model
        elif environ.get("PYTO_HARNESS_MODEL"):
            config.model = environ["PYTO_HARNESS_MODEL"]
        if args.workspace:
            config.workspace = args.workspace
        elif environ.get("PYTO_HARNESS_WORKSPACE"):
            config.workspace = environ["PYTO_HARNESS_WORKSPACE"]
        if args.max_turns:
            config.max_turns = args.max_turns
        if args.api_key:
            config.api_key = args.api_key
        else:
            for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY"):
                if environ.get(name):
                    config.api_key = environ[name]
                    break
        config.stream = not args.no_stream
        config.yolo = bool(args.yolo)
        config.compact = not args.no_compact
        config.allow_unattended_programs = bool(getattr(args, "allow_unattended_programs", False))
        try:
            config.workspace = config.workspace or default_workspace()
            config.sessions_dir = environ.get("PYTO_HARNESS_SESSIONS_DIR") or default_sessions_dir()
        except ConfigError:
            # No writable home anywhere: leave the paths empty so the doctor can report
            # the actionable message (PYTO_HARNESS_HOME) instead of crashing here.
            pass
        config.spill_dir = os.path.join(config.workspace, "tool-output") if config.workspace else ""
        return config, str(exc)


def run_doctor_command(args: argparse.Namespace, migration: str = "") -> int:
    """``--doctor`` and ``--doctor --fix``.  Returns the exit code the spec asks for.

    ``migration`` is what :func:`migrate_state_directory` already did in :func:`main`
    *before* the config was resolved (resolving it can create the state directory, so it
    cannot be left to the doctor).  It is carried into the report header, so a move or a
    merge is always named; the doctor's own home check still warns about a leftover folder.
    """
    config, config_error = resolve_config_lenient(args)
    if config_error:
        print("configuration error: {} (diagnosing anyway)".format(config_error), file=sys.stderr)
    ctx = doctor.DoctorContext.for_config(
        config,
        network=not args.no_network,
        deep=bool(args.deep or args.deep_tests),
        deep_tests=bool(args.deep_tests),
        persist=True,
        workspace=args.workspace or None,
    )
    ctx.config_error = config_error
    if migration:
        ctx.state_migration = migration
    before = doctor.run_checks(ctx)
    if args.fix:
        after, outcomes = doctor.apply_fixes(ctx, before, safe_only=False)
        print(doctor.before_after_report(before, after, outcomes, ctx=ctx))
    else:
        after = before
        print(doctor.format_report(before, ctx=ctx, title="doctor"))
    doctor.save_health(ctx, after)
    return doctor.exit_code(after)


def run_backups_command(args: argparse.Namespace) -> int:
    backups = repair.list_backups()
    if not backups:
        print("no backups yet; they are written to {}".format(repair.default_backups_dir()))
        return 0
    print("{} backup(s) in {}".format(len(backups), repair.default_backups_dir()))
    for entry in backups:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((entry.get("created_at") or 0) / 1000))
        line = "  {}  {}  {} file(s), {} bytes".format(entry["id"], when, entry.get("files", 0), entry.get("bytes", 0))
        if entry.get("label"):
            line += "  [{}]".format(entry["label"])
        if entry.get("error"):
            line += "  (unreadable: {})".format(entry["error"])
        print(line)
    return 0


def run_restore_command(args: argparse.Namespace) -> int:
    result = repair.restore(args.restore)
    print(result.render(with_diff=False))
    if result.warnings and any("LOUD" in warning for warning in result.warnings):
        print("\nThe restore completed, but the harness is still not healthy.", file=sys.stderr)
    return 0 if result.ok else 1


class RepairPrinter(Printer):
    """Printer that also remembers the gated repair calls, for the closing summary."""

    REPAIR_TOOLS = ("self_edit", "restore_backup", "apply_fix")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.repair_calls: List[Dict[str, Any]] = []

    def handle(self, event: Any) -> None:
        if event.kind == "tool.completed" and event.data.get("name") in self.REPAIR_TOOLS:
            self.repair_calls.append(dict(event.data))
        super().handle(event)


def run_repair_command(args: argparse.Namespace) -> int:
    """``--repair``: one turn with the repair tools pre-approved and the gate in place."""
    config, config_error = resolve_config_lenient(args)
    if config_error:
        print("configuration error: {} (repairing anyway)".format(config_error), file=sys.stderr)
    warn_about_plain_http(config)
    if not config.has_api_key:
        print(
            "No API key found, and --repair asks the model for the change.\n"
            "Set one with:  export DEEPSEEK_API_KEY=sk-...\n"
            "or fix the setup first:  python run.py --doctor --fix",
            file=sys.stderr,
        )
        return 2
    try:
        workspace = ensure_workspace(config)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2

    context = default_context(workspace, config.spill_dir, config=config)
    context.full_gate = bool(args.deep_tests or args.deep)
    registry = build_registry(context)
    registry.policy = make_policy(
        yolo=config.yolo,
        prompter=None if config.yolo else TerminalApprover(),
        allow=REPAIR_ALLOWED_TOOLS,
        unattended_programs=bool(getattr(config, "allow_unattended_programs", False)),
        workspace=workspace,
    )
    registry.lock_policy()
    system_prompt = build_system_prompt(config, workspace, extra=repair.REPAIR_INSTRUCTIONS)
    client = client_from_config(config)
    printer = RepairPrinter(verbose=args.verbose)
    try:
        session = open_session(config, label="repair")
    except (OSError, HarnessError) as exc:
        print("could not open the session: {}".format(exc), file=sys.stderr)
        client.close()
        return 2
    options = LoopOptions(
        client=client,
        registry=registry,
        session=session,
        system_prompt=system_prompt,
        max_turns=config.max_turns,
        spill_dir=config.spill_dir,
        stream=config.stream,
        compact=bool(getattr(config, "compact", True)),
        stop=threading.Event(),
    )
    print("repairing: {}\n{}".format(args.repair, "-" * 72))
    try:
        done = run_turn_sync(
            options,
            "Repair this problem in the harness itself:\n\n{}\n\n"
            "Diagnose first, keep the change as small as you can, and let the offline tests "
            "decide whether it stays.".format(args.repair),
            printer,
        )
    finally:
        session.close()
        client.close()
    print("-" * 72)
    if not printer.repair_calls:
        print("[repair] no gated change was made (the model did not call self_edit/restore_backup/apply_fix)")
        _print_footer(done, session)
        return 1
    promoted = False
    for call in printer.repair_calls:
        metadata = call.get("metadata") or {}
        print(
            "[repair] {}: {} (tests: {})".format(
                call.get("name"),
                metadata.get("decision") or ("applied" if metadata.get("applied") else "failed"),
                _tests_brief(metadata.get("tests")),
            )
        )
        promoted = promoted or bool(metadata.get("ok"))
    if promoted:
        print("[repair] a change was kept; restart the harness to load it (backups: `--backups`)")
    else:
        print("[repair] nothing was kept; the tree is unchanged (see the tool output above)")
    _print_footer(done, session)
    return 0 if promoted else 1


def _tests_brief(tests: Any) -> str:
    if not isinstance(tests, dict) or not tests:
        return "not run"
    return "{} ran, {} failure(s), {} error(s)".format(
        tests.get("ran"), tests.get("failures"), tests.get("errors")
    )


def maybe_first_run_doctor(args: argparse.Namespace, config: Config) -> None:
    """One line of health information on the first normal run of a day.

    Never blocks the run, never prints the key, and only applies the fixes that cannot
    surprise anyone: create a missing directory, tighten the config file mode, cut a torn
    final session line.  ``--no-doctor`` and ``PYTO_HARNESS_NO_DOCTOR=1`` disable it.
    """
    if args.no_doctor or os.environ.get("PYTO_HARNESS_NO_DOCTOR"):
        return
    try:
        line = doctor.first_run(config, persist=True)
    except Exception:  # noqa: BLE001 - a diagnostic must never take the run down
        return
    if line:
        print(line)


def task_from_environment(argv: Optional[List[str]] = None) -> Optional[str]:
    """Recover a task passed by URL or environment, for the Shortcuts wiring.

    ``pyto://python/<path to run.py>?task=rename%20my%20screenshots`` launches the script
    with the query appended to ``sys.argv``; Pyto does not turn query parameters into
    flags, so they are read here.  ``PYTO_HARNESS_TASK`` is the environment equivalent
    for a Shortcut that prefers to set a variable.
    """
    for candidate in list(argv if argv is not None else sys.argv)[1:]:
        if candidate.startswith("task="):
            value = urllib.parse.unquote(candidate[len("task=") :].replace("+", " "))
            if value.strip():
                return value.strip()
    value = os.environ.get("PYTO_HARNESS_TASK", "")
    return value.strip() or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="A stdlib-only LLM agent harness for Pyto on iOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python run.py \"write a script that renames my screenshots by date\"\n"
            "  python run.py --dry-run \"summarise my notes folder\"\n"
            "  python run.py --resume ~/pyto_harness/sessions/latest.jsonl \"do the same for July\"\n"
            "  python run.py --doctor --fix\n"
            "  python run.py --repair \"the api_base keeps 404ing\"\n"
            "  python run.py --ui\n"
        ),
    )
    parser.add_argument("task", nargs="*", help="what the user wants done; omit for a chat REPL")
    parser.add_argument("--resume", metavar="SESSION", help="session .jsonl file (or a directory of them)")
    parser.add_argument("--model", help="override the model name")
    parser.add_argument("--api-base", help="override the API base URL")
    parser.add_argument(
        "--api-key",
        help="override the API key; WARNING: it is visible in `ps` and your shell history "
        "(prefer the config file or DEEPSEEK_API_KEY)",
    )
    parser.add_argument(
        "--yolo", action="store_true", help="skip approval for sharing, URLs, Shortcuts and the like"
    )
    parser.add_argument(
        "--allow-unattended-programs",
        action="store_true",
        help="allow run_program in a run with no interactive approver (Shortcut/headless); "
        "narrower than --yolo",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the request that would be sent, contact nothing")
    parser.add_argument("--ui", action="store_true", help="open the Pyto chat window instead of the terminal")
    parser.add_argument("--workspace", help="workspace directory (default ~/pyto_harness_workspace)")
    parser.add_argument("--max-turns", type=int, help="model round trips per task (default 8)")
    parser.add_argument("--no-stream", action="store_true", help="use the non-streaming request path")
    parser.add_argument("--verbose", action="store_true", help="show reasoning, usage and turn numbers")
    parser.add_argument(
        "--no-compact",
        action="store_true",
        help="never trim the session log (by default it is compacted near 4000 events / 4 MB)",
    )
    parser.add_argument("--capabilities", action="store_true", help="print the device capability report and exit")
    parser.add_argument("--tools", action="store_true", help="list the tools and exit")
    parser.add_argument("--init", action="store_true", help="write a starter config file and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --init: overwrite an existing config file (refused by default)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="diagnose this installation; exit 0 healthy, 1 broken but fixable, 2 needs a human",
    )
    parser.add_argument("--fix", action="store_true", help="with --doctor: apply the repairs a machine can do")
    parser.add_argument("--deep", action="store_true", help="with --doctor: also run the offline test suite")
    parser.add_argument(
        "--deep-tests", action="store_true", help="with --doctor --deep: run every test module, not the fast subset"
    )
    parser.add_argument("--no-network", action="store_true", help="with --doctor: skip DNS/TLS/auth checks")
    parser.add_argument(
        "--no-doctor",
        action="store_true",
        help="skip the one-line first-run health pass (also PYTO_HARNESS_NO_DOCTOR=1)",
    )
    parser.add_argument(
        "--repair", metavar="PROBLEM", help="ask the model for a gated source edit that fixes PROBLEM"
    )
    parser.add_argument("--backups", action="store_true", help="list the source snapshots and exit")
    parser.add_argument("--restore", metavar="BACKUP_ID", help="put a source snapshot back and exit")
    parser.add_argument("--version", action="version", version="pyto-harness {}".format(__version__))
    return parser


def migrate_state_directory() -> str:
    """Move a legacy hidden state directory into the open, once, before anything reads it.

    The state directory dropped its leading dot so the iOS Files app can show it.  An
    install from a previous release still has the hidden one: move it here, at the top of a
    run, and hand the message to the caller so the user knows where their config and
    sessions went.  Returns ``""`` when there was nothing to do (the usual case, and the
    only case after the first run).

    The homes are *guessed* (:func:`harness.home.candidate_homes`), never resolved: the
    resolver proves a candidate by creating ``<home>/pyto_harness``, and an empty new
    directory on disk is what used to turn this move into a silent no-op.  Calling this
    before the config is resolved is therefore the whole point.  ``--doctor`` gets the same
    treatment in :func:`main`; the message is carried into its report header.
    """
    try:
        return home.migrate_candidate_homes()
    except Exception:  # noqa: BLE001 - housekeeping must never stop a run
        return ""


def resolve_config(args: argparse.Namespace, *, use_env: bool = True) -> Config:
    overrides: Dict[str, Any] = {
        "model": args.model,
        "api_base": args.api_base,
        "api_key": args.api_key,
        "workspace": args.workspace,
        "max_turns": args.max_turns,
    }
    if args.no_stream:
        overrides["stream"] = False
    if args.yolo:
        overrides["yolo"] = True
    if args.no_compact:
        overrides["compact"] = False
    if getattr(args, "allow_unattended_programs", False):
        overrides["allow_unattended_programs"] = True
    return load_config(overrides=overrides, use_env=use_env)


def warn_about_argv_key(args: argparse.Namespace) -> None:
    """``--api-key`` puts the credential in ``ps`` and the shell history.  Say so, once."""
    if not getattr(args, "api_key", None):
        return
    print(
        "[warning] --api-key is visible in `ps` and your shell history. Prefer the config file "
        "(mode 0600) or the DEEPSEEK_API_KEY environment variable.",
        file=sys.stderr,
    )


def warn_about_plain_http(config: Config) -> None:
    """A plain ``http://`` base sends the key and every prompt in cleartext."""
    if not config.api_base.lower().startswith("http://") or not config.has_api_key:
        return
    print(
        "[warning] api_base uses plain http:// -- the API key and everything you send travel in "
        "cleartext on this network. Use https:// unless this is a local server you control.",
        file=sys.stderr,
    )


def print_request_preview(config: Config, system_prompt: str, task: str) -> int:
    """``--dry-run``: show exactly what would go on the wire.  Contacts nothing.

    A preview writes nothing, so it must not *require* a writable workspace: on a
    read-only or sandboxed filesystem the tool schemas are still worth printing.  The
    system prompt keeps the configured path, because that is what the model would see.
    """
    try:
        ensure_workspace(config)
        context = default_context(config.workspace, config.spill_dir, config=config)
    except (ConfigError, OSError) as exc:
        print("[note] the workspace is not writable here ({}); previewing anyway".format(exc))
        context = default_context(tempfile.mkdtemp(prefix="pyto-harness-preview-"), "")
    registry = build_registry(context)
    # A placeholder key keeps the preview shaped like a real request without ever
    # touching the real credential.
    preview_config = Config(**{**config.__dict__})
    preview_config.api_key = config.api_key or "sk-DRY-RUN-NO-KEY"
    client = client_from_config(preview_config)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    preview = client.request_preview(messages, tools=registry.definitions(), stream=config.stream)

    print("=" * 78)
    print("DRY RUN - nothing is sent, no network connection is made")
    print("=" * 78)
    print(describe(config))
    print("-" * 78)
    print("{} {}".format(preview["method"], preview["url"]))
    if preview["fallback_urls"]:
        print("fallbacks : {}".format(", ".join(preview["fallback_urls"])))
    for key, value in preview["headers"].items():
        print("{}: {}".format(key, value))
    print("-" * 78)
    print("body ({} bytes, {} tools):".format(preview["bytes"], len(registry.definitions())))
    print(json.dumps(preview["body"], indent=2, ensure_ascii=False)[:20000])
    print("-" * 78)
    print("tools offered to the model:")
    for tool in registry.tools():
        print("  {:<19} {}".format(tool.name, tool.description.split(". ")[0]))
    return 0


def make_options_factory(config: Config, *, prompter: Any, verbose: bool, ui: bool = False) -> Any:
    """Build the callable that turns a session into loop options.

    ``ui=True`` marks a Pyto-window session as interactive even when stdin is not a TTY:
    the user is in the app and can see the transcript, which is what ``run_program``'s AUTO
    status is for.  A headless/Shortcut run has neither a TTY nor a window, so there it is
    denied unless ``--allow-unattended-programs`` (or ``--yolo``) was asked for.
    """
    workspace = ensure_workspace(config)
    context = default_context(workspace, config.spill_dir, config=config)
    registry = build_registry(context)
    interactive = bool(ui) or prompter_is_interactive(prompter)
    registry.policy = make_policy(
        yolo=config.yolo,
        prompter=prompter,
        unattended_programs=bool(getattr(config, "allow_unattended_programs", False)),
        interactive=interactive,
        workspace=workspace,
    )
    registry.lock_policy()
    system_prompt = build_system_prompt(config, workspace)
    client = client_from_config(config)

    def factory(session: SessionLog) -> LoopOptions:
        return LoopOptions(
            client=client,
            registry=registry,
            session=session,
            system_prompt=system_prompt,
            max_turns=config.max_turns,
            spill_dir=config.spill_dir,
            stream=config.stream,
            compact=bool(getattr(config, "compact", True)),
            stop=threading.Event(),
        )

    factory.context = context  # type: ignore[attr-defined]
    factory.registry = registry  # type: ignore[attr-defined]
    factory.client = client  # type: ignore[attr-defined]
    factory.system_prompt = system_prompt  # type: ignore[attr-defined]
    return factory


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Query parameters arrive as bare `key=value` arguments; argparse would treat them as
    # the task text, so they are removed before parsing and read separately.
    cleaned = [item for item in raw_argv if not item.startswith("task=")]
    args = parser.parse_args(cleaned)
    url_task = task_from_environment(raw_argv) or ""
    warn_about_argv_key(args)

    # The state directory is visible now; a hidden one from an older release is moved
    # before *anything* resolves a path -- resolving a home probes it, and the probe
    # creates the new directory, which used to make this move a silent no-op.  Every run
    # path does it here, ``--doctor`` included; that one carries the message into its
    # report header instead of printing it twice.  On stderr: a notice, not agent output.
    migration = migrate_state_directory()
    if migration and not (args.doctor or args.fix):
        print(migration, file=sys.stderr)

    if args.capabilities:
        print(ios.capability_report())
        return 0
    if args.init:
        try:
            path = write_sample_config(force=bool(args.force))
        except ConfigError as exc:
            print("{}".format(exc), file=sys.stderr)
            return 2
        except OSError as exc:
            print("could not write the config: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
            return 2
        # Print the mode the file *has*, never the mode it was supposed to have.  The
        # helper chmods if it can and reports the result; a failure is not swallowed.
        mode = enforce_config_mode(path)
        if mode == "0o600":
            print("Wrote {} (mode 0600). Put your key in it or export DEEPSEEK_API_KEY.".format(path))
        else:
            print(
                "Wrote {} but its mode is {} -- it is readable by other accounts. "
                "Run `chmod 600 {}` before putting a key in it.".format(path, mode, path),
                file=sys.stderr,
            )
            return 1
        return 0
    if args.doctor or args.fix:
        return run_doctor_command(args, migration)
    if args.repair:
        return run_repair_command(args)
    if args.backups:
        return run_backups_command(args)
    if args.restore:
        return run_restore_command(args)

    try:
        config = resolve_config(args)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2

    if args.tools:
        try:
            context = default_context(ensure_workspace(config), config.spill_dir, config=config)
        except (ConfigError, OSError):
            # Listing tools must work even where the workspace is not writable.
            import tempfile

            context = default_context(os.path.join(tempfile.gettempdir(), "pyto-harness-tools"), config=config)
        registry = build_registry(context)
        print("{} tools:".format(len(registry)))
        for tool in registry.tools():
            print("\n{}  (timeout={}s)".format(tool.name, tool.timeout))
            print("  {}".format(tool.description))
        return 0

    task = " ".join(args.task).strip()
    if not task:
        # A Shortcut launched us with `?task=...` or PYTO_HARNESS_TASK; that is the task.
        task = url_task

    warn_about_plain_http(config)

    if not args.dry_run:
        # A preview writes nothing, so it must not run the first-run health pass either.
        maybe_first_run_doctor(args, config)

    if args.dry_run:
        # Before the workspace is created: a preview writes nothing, so a read-only
        # filesystem must not stop it.  `print_request_preview` reports the situation.
        system_prompt = build_system_prompt(config, config.workspace)
        return print_request_preview(config, system_prompt, task or "<no task given>")

    try:
        workspace = ensure_workspace(config)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2
    system_prompt = build_system_prompt(config, workspace)

    if not config.has_api_key:
        try:
            config_path = default_config_path()
        except ConfigError:
            config_path = "<no writable home: use --init once PYTO_HARNESS_HOME is set>"
        print(
            "No API key found.\n"
            "Set one with:  export DEEPSEEK_API_KEY=sk-...\n"
            "or put it in {} (run `python run.py --init` to create the file).".format(config_path),
            file=sys.stderr,
        )
        return 2

    try:
        session = open_session(config, resume=args.resume, label=(task[:24] or "chat"))
    except (OSError, HarnessError) as exc:
        print("could not open the session: {}".format(exc), file=sys.stderr)
        return 2

    prompter = None if config.yolo else TerminalApprover()
    factory = make_options_factory(config, prompter=prompter, verbose=args.verbose, ui=bool(args.ui))

    approvals_line = (
        "bypassed (--yolo)"
        if config.yolo
        else "ask before sharing / URLs / Shortcuts"
        + ("; run_program allowed unattended" if config.allow_unattended_programs else "")
    )
    banner = (
        "pyto-harness {}\n{}\nworkspace: {}\nsession  : {}\napprovals: {}".format(
            __version__,
            ios.platform_label(),
            workspace,
            session.path,
            approvals_line,
        )
    )

    try:
        if args.ui:
            from harness.ui import run_ui

            try:
                run_ui(options_factory=factory, session=session)
            except UnsupportedCapability as exc:
                print("--ui is not available: {}".format(exc.message), file=sys.stderr)
                print(banner)
                return 3
            return 0

        printer = Printer(verbose=args.verbose)
        if task:
            printer.write(banner)
            printer.write("")
            done = run_turn_sync(factory(session), task, printer)
            _print_footer(done, session)
            return 0 if not done.get("errors") else 1

        printer.write(banner)
        from harness.ui import terminal_repl

        return terminal_repl(options_factory=factory, session=session, printer=printer, banner="")
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\ninterrupted", file=sys.stderr)
        return 130
    except HarnessError as exc:
        print("{}: {}".format(exc.code, exc.message), file=sys.stderr)
        return 1
    finally:
        session.close()
        factory.client.close()  # type: ignore[attr-defined]


def _print_footer(done: Dict[str, Any], session: SessionLog) -> None:
    if done.get("errors"):
        print("\n[the turn ended with errors: {}]".format("; ".join(done["errors"])), file=sys.stderr)
    print(
        "\n[{} turn(s), {} tool call(s), {} ms, stop={}]".format(
            done.get("turns", 0),
            done.get("tool_calls", 0),
            done.get("duration_ms", 0),
            done.get("stop", "?"),
        )
    )
    print("[session: {}]".format(session.path))


if __name__ == "__main__":
    raise SystemExit(main())
