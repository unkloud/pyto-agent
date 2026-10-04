"""End-to-end self-fix demonstrations, through the real CLI in a throwaway copy.

Four things are proved here, each with a real subprocess:

1. ``--doctor`` then ``--doctor --fix`` heals injected breakage (config mode, torn session
   line, missing workspace, missing Shortcuts guide) and exits 0.
2. A model-authored ``self_edit`` whose patch breaks the gate is **reverted byte for
   byte** and reported verbatim.
3. A good patch is promoted, listed by ``--backups`` and undone by ``--restore``.
4. The first-run doctor line appears once, is silenced by ``--no-doctor`` and
   ``PYTO_HARNESS_NO_DOCTOR``, and ``--dry-run`` still contacts nothing and writes no
   health file.

The gate is always the bounded subset (``PYTO_HARNESS_REPAIR_TEST_MODULES=test_config``),
so no test here ever runs the whole suite from inside the suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

from .mock_provider import MockProvider, text_response, tool_response
from .support import ROOT, TempDirTestCase

API_KEY = "sk-test-key-not-real"
GATE_ENV = {"PYTO_HARNESS_REPAIR_TEST_MODULES": "test_config"}


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SelfFixTestCase(TempDirTestCase):
    """A real copy of the harness, driven through ``python run.py``."""

    def setUp(self) -> None:
        super().setUp()
        self.copy = self.path("copy")
        shutil.copytree(
            ROOT,
            self.copy,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".scratch"),
        )
        self.state = self.path("state")
        self.config_path = self.path("config.json")
        self.sessions = self.path("sessions")
        self.workspace = self.path("ws")

    # -- helpers -------------------------------------------------------------------

    def env(self, **overrides: Any) -> Dict[str, str]:
        values = dict(os.environ)
        values.update(
            {
                "PYTO_HARNESS_STATE_DIR": self.state,
                "PYTO_HARNESS_CONFIG": self.config_path,
                "PYTO_HARNESS_SESSIONS_DIR": self.sessions,
                "PYTO_HARNESS_NO_BROWSER": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        for leaked in (
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "PYTO_HARNESS_API_KEY",
            # The suite may itself be running inside a repair gate, which sets these.
            "PYTO_HARNESS_NO_DOCTOR",
            "PYTO_HARNESS_REPAIR_GATE",
            "PYTO_HARNESS_REPAIR_TEST_MODULES",
            "PYTO_HARNESS_SELFTEST_DEPTH",
        ):
            values.pop(leaked, None)
        values.update({key: str(value) for key, value in overrides.items()})
        return values

    def run_cli(self, *args: str, env: Optional[Dict[str, str]] = None, timeout: float = 240.0) -> Tuple[int, str]:
        process = subprocess.run(
            [sys.executable, os.path.join(self.copy, "run.py")] + list(args),
            cwd=self.copy,
            capture_output=True,
            text=True,
            env=env or self.env(),
            timeout=timeout,
        )
        return process.returncode, (process.stdout or "") + (process.stderr or "")

    def write_config(self, payload: Dict[str, Any], *, mode: int = 0o600) -> str:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(self.config_path, mode)
        return self.config_path

    def healthy_config(self, **overrides: Any) -> Dict[str, Any]:
        payload = {
            "api_base": "http://127.0.0.1:9",
            "model": "mock-model",
            "api_key": API_KEY,
            "max_turns": 4,
            "timeout": 30,
            "workspace": self.workspace,
        }
        payload.update(overrides)
        return payload

    def make_torn_session(self, events: int = 4) -> str:
        from harness.session import SessionLog

        os.makedirs(self.sessions, exist_ok=True)
        path = os.path.join(self.sessions, "20260101-000000-torn.jsonl")
        log = SessionLog.create(path, workspace=self.workspace)
        for index in range(events):
            log.append("turn.started", {"turn": index})
        log.close()
        with open(path, "ab") as handle:
            handle.write(b'{"kind":"event","seq":9,"time":1,"type":"turn.st')
        return path

    def backup_ids(self) -> List[str]:
        code, out = self.run_cli("--backups")
        self.assertEqual(code, 0, out)
        ids = []
        for line in out.splitlines():
            stripped = line.strip()
            if "file(s)" in stripped and stripped.split():
                ids.append(stripped.split()[0])
        return ids

    def source(self, relative: str = "harness/config.py") -> str:
        with open(os.path.join(self.copy, relative), encoding="utf-8") as handle:
            return handle.read()


class TestDoctorCommand(SelfFixTestCase):
    def test_exit_codes_for_healthy_broken_and_human_only(self) -> None:
        os.makedirs(self.workspace)
        os.makedirs(self.sessions)
        self.write_config(self.healthy_config())
        code, out = self.run_cli("--doctor", "--fix", "--no-network", "--workspace", self.workspace)
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("--doctor", "--no-network", "--workspace", self.workspace)
        self.assertEqual(code, 0, out)
        self.assertIn("doctor:", out)

        shutil.rmtree(self.workspace)
        code, out = self.run_cli("--doctor", "--no-network", "--workspace", self.workspace)
        self.assertEqual(code, 1, out)
        self.assertIn("workspace.create", out)

        os.makedirs(self.workspace)
        self.write_config(self.healthy_config(api_key=""))
        code, out = self.run_cli("--doctor", "--no-network", "--workspace", self.workspace)
        self.assertEqual(code, 2, out)
        self.assertIn("api_key_present", out)
        self.assertIn("only you can supply the key", out)

    def test_doctor_fix_heals_injected_breakage(self) -> None:
        """The demonstration: broken copy -> before report -> --fix -> healed, exit 0."""
        self.write_config(self.healthy_config(), mode=0o644)
        torn = self.make_torn_session()
        before_events = len(self._events(torn))
        torn_bytes = os.path.getsize(torn)
        if os.path.isdir(self.workspace):
            shutil.rmtree(self.workspace)

        code, before = self.run_cli("--doctor", "--no-network", "--workspace", self.workspace)
        self.assertEqual(code, 1, before)
        self.assertIn("torn", before)
        self.assertIn("0o644", before)
        self.assertIn("does not exist", before)

        code, after = self.run_cli("--doctor", "--fix", "--no-network", "--workspace", self.workspace)
        self.assertEqual(code, 0, after)
        self.assertIn("fixed,", after)
        self.assertIn("0 failed", after)

        self.assertEqual(self._mode(self.config_path), 0o600)
        self.assertLess(os.path.getsize(torn), torn_bytes, "the torn fragment should be cut off")
        self.assertTrue(os.path.exists(torn + ".torn"), "the torn fragment must be kept")
        self.assertTrue(os.path.isdir(self.workspace))
        self.assertTrue(os.path.exists(os.path.join(self.workspace, "SHORTCUTS.md")))
        remaining = self._events(torn)
        self.assertEqual(len(remaining), before_events, "the rest of the log must survive")
        with open(torn, "r", encoding="utf-8") as handle:
            log_text = handle.read()
        self.assertNotIn('"seq":9', log_text)

    # -- helpers for the healing test ----------------------------------------------

    @staticmethod
    def _mode(path: str) -> int:
        import stat as stat_module

        return stat_module.S_IMODE(os.stat(path).st_mode)

    @staticmethod
    def _events(path: str) -> List[Any]:
        from harness.session import SessionLog

        log = SessionLog.resume(path, writable=False)
        try:
            return log.events
        finally:
            log.close()


class TestRepairCommand(SelfFixTestCase):
    def test_a_broken_patch_is_reverted_and_the_bytes_are_identical(self) -> None:
        self.write_config(self.healthy_config())
        before = sha256(os.path.join(self.copy, "harness", "config.py"))
        broken = self.source().replace('DEFAULT_MODEL = "deepseek-chat"', 'DEFAULT_MODEL = "broken-model"')
        script = [
            tool_response(
                ("self_edit", {"path": "harness/config.py", "new_source": broken, "reason": "pretend to fix"})
            ),
            text_response("I attempted a repair."),
        ]
        with MockProvider(script) as provider:
            code, out = self.run_cli(
                "--repair",
                "make the model name wrong",
                "--api-base",
                provider.api_base,
                "--api-key",
                API_KEY,
                "--model",
                "mock-model",
                "--workspace",
                self.workspace,
                env=self.env(**GATE_ENV),
            )
        self.assertEqual(code, 1, out)
        self.assertIn("reverted", out)
        self.assertIn("deepseek-chat", out, "the failing assertion must be shown verbatim")
        self.assertEqual(sha256(os.path.join(self.copy, "harness", "config.py")), before)
        ids = self.backup_ids()
        self.assertTrue(any("pre-edit" in backup_id for backup_id in ids), ids)

    def test_a_good_patch_is_promoted_listed_and_restored(self) -> None:
        self.write_config(self.healthy_config())
        good = self.source().replace(
            'DEFAULT_MODEL = "deepseek-chat"', 'DEFAULT_MODEL = "deepseek-chat"  # repaired by the gate'
        )
        script = [
            tool_response(
                ("self_edit", {"path": "harness/config.py", "new_source": good, "reason": "annotate the default"})
            ),
            text_response("Annotated the model default."),
        ]
        with MockProvider(script) as provider:
            code, out = self.run_cli(
                "--repair",
                "the model default is undocumented",
                "--api-base",
                provider.api_base,
                "--api-key",
                API_KEY,
                "--model",
                "mock-model",
                "--workspace",
                self.workspace,
                env=self.env(**GATE_ENV),
            )
        self.assertEqual(code, 0, out)
        self.assertIn("promoted", out)
        self.assertIn("repaired by the gate", self.source())

        ids = self.backup_ids()
        self.assertTrue(ids, "a promoted edit must leave a backup")

        # The key must not be in the turn's session log either.
        for name in os.listdir(self.sessions):
            if name.endswith(".jsonl"):
                with open(os.path.join(self.sessions, name), encoding="utf-8") as handle:
                    self.assertNotIn(API_KEY, handle.read(), "{} leaked the key".format(name))

        code, out = self.run_cli("--restore", ids[0], env=self.env(**GATE_ENV))
        self.assertEqual(code, 0, out)
        self.assertNotIn("repaired by the gate", self.source())

    def test_repair_without_a_key_explains_itself(self) -> None:
        code, out = self.run_cli("--repair", "something is broken", "--workspace", self.workspace)
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", out)


class TestFirstRunDoctor(SelfFixTestCase):
    def test_line_is_printed_once_then_suppressed(self) -> None:
        self.write_config(self.healthy_config(api_key=""))
        code, first = self.run_cli("do something", "--workspace", self.workspace)
        self.assertEqual(code, 2, first)
        self.assertIn("doctor: ", first)
        self.assertIn("--doctor for details", first)
        self.assertTrue(os.path.exists(os.path.join(self.state, "health.json")))

        code, second = self.run_cli("do something", "--workspace", self.workspace)
        self.assertEqual(code, 2)
        self.assertNotIn("doctor: ", second, "a fresh health file must silence the pass")

        state_file = os.path.join(self.state, "health.json")
        if os.path.exists(state_file):
            os.unlink(state_file)
        code, third = self.run_cli("do something", "--no-doctor", "--workspace", self.workspace)
        self.assertEqual(code, 2)
        self.assertNotIn("doctor: ", third)
        self.assertFalse(os.path.exists(state_file), "--no-doctor must not write the health file either")

        code, fourth = self.run_cli(
            "do something", "--workspace", self.workspace, env=self.env(PYTO_HARNESS_NO_DOCTOR="1")
        )
        self.assertEqual(code, 2)
        self.assertNotIn("doctor: ", fourth)

    def test_first_run_never_prints_the_key(self) -> None:
        canary = "sk-canary-abcdefghijklmnop0123456789"
        self.write_config(self.healthy_config(api_key=canary))
        code, out = self.run_cli("do something", "--workspace", self.workspace)
        self.assertNotIn(canary, out)

    def test_dry_run_still_contacts_nothing_and_writes_no_health_file(self) -> None:
        self.write_config(self.healthy_config())
        with MockProvider([text_response("should not happen")]) as provider:
            code, out = self.run_cli(
                "--dry-run",
                "hello",
                "--api-base",
                provider.api_base,
                "--api-key",
                API_KEY,
                "--workspace",
                self.workspace,
            )
            with provider.lock:
                seen = len(provider.requests)
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(seen, 0, "--dry-run contacted the network")
        self.assertFalse(
            os.path.exists(os.path.join(self.state, "health.json")),
            "a preview must not run the first-run doctor either",
        )


if __name__ == "__main__":
    unittest.main()
