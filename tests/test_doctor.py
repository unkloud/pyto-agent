"""Doctor tests: every check's ok/warn/fail path, the fixes, and the secrets rule.

Everything here is offline.  The network checks talk to ``tests/mock_provider.py`` on
127.0.0.1, or to a closed local port when a failure is the point.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from harness import doctor, ios
from harness.session import SessionLog

from .mock_provider import MockProvider, error_response, text_response
from .support import ROOT, TempDirTestCase

OK_JSON = {"json": {"id": "chatcmpl-mock", "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}]}}


def ok_spec(_body: Dict[str, Any]) -> Dict[str, Any]:
    return OK_JSON


def _path_spec(provider: MockProvider, ok_prefix: str = "/v1/"):
    """Answer 200 only for paths under ``ok_prefix``: a base probe that must find /v1."""

    def spec(_index: int, _body: Dict[str, Any]) -> Dict[str, Any]:
        with provider.lock:
            path = provider.requests[-1]["path"] if provider.requests else ""
        return OK_JSON if path.startswith(ok_prefix) else error_response(404, "not found")

    return spec


def model_spec(body: Dict[str, Any]) -> Dict[str, Any]:
    """Accept exactly one model name; reject everything else the way a provider would."""
    if body.get("model") == "mock-model":
        return OK_JSON
    return error_response(400, "model '{}' not found".format(body.get("model")))


class DoctorTestCase(TempDirTestCase):
    """A temp-dir context with no environment leakage."""

    def make_ctx(self, *, config: Any = None, **overrides: Any) -> doctor.DoctorContext:
        config = config or self.make_config(api_base="http://127.0.0.1:9", api_key=None)
        values: Dict[str, Any] = {
            "env": {},
            "config_path": self.path("config.json"),
            "root": ROOT,
            "state": self.path("state"),
            "workspace": self.path("workspace"),
            "sessions_dir": self.path("sessions"),
            "network": False,
            "persist": False,
        }
        values.update(overrides)
        ctx = doctor.DoctorContext.for_config(config, **values)
        return ctx

    def write_config(self, payload: Any, *, mode: int = 0o600) -> str:
        path = self.path("config.json")
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)
        os.chmod(path, mode)
        return path

    def check(self, check_id: str, **ctx_kwargs: Any) -> doctor.CheckResult:
        ctx = self.make_ctx(**ctx_kwargs)
        return doctor.run_one(ctx, check_id)

    def free_port(self) -> int:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        return port

    def mini_tree(self, modules: Dict[str, str], *, with_audit: bool = False) -> str:
        """A tiny standalone harness: enough for the import/audit checks to bite."""
        root = self.path("miniroot")
        os.makedirs(os.path.join(root, "harness"), exist_ok=True)
        with open(os.path.join(root, "harness", "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write("")
        for name, source in modules.items():
            with open(os.path.join(root, "harness", name), "w", encoding="utf-8") as handle:
                handle.write(source)
        if with_audit:
            shutil.copyfile(os.path.join(ROOT, "stdlib_audit.py"), os.path.join(root, "stdlib_audit.py"))
        return root

    def copy_tree(self, *, tests: bool = False) -> str:
        """A real copy of the harness next to this test's temp dir."""
        target = self.path("copy")
        shutil.copytree(
            ROOT,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".scratch"),
            dirs_exist_ok=False,
        )
        return target

    def make_session_log(self, name: str = "s.jsonl", events: int = 3) -> str:
        path = self.path(name)
        log = SessionLog.create(path, workspace=self.workspace_dir)
        for index in range(events):
            log.append("turn.started", {"turn": index})
        log.close()
        return path


# --------------------------------------------------------------------------------------
# interpreter / importability / stdlib
# --------------------------------------------------------------------------------------


class TestInterpreter(DoctorTestCase):
    def test_reports_this_interpreter(self) -> None:
        item = self.check("interpreter")
        self.assertIn(item.status, ("ok", "warn"))
        self.assertEqual(item.evidence["python"], "{}.{}.{}".format(*sys.version_info[:3]))

    def test_old_python_fails_with_a_human_action(self) -> None:
        with mock.patch.object(doctor, "python_version_info", return_value=(3, 9, 7)):
            item = self.check("interpreter")
        self.assertEqual(item.status, "fail")
        self.assertFalse(item.fixable)
        self.assertIn("3.10", item.human_action or "")

    def test_a_crashing_check_becomes_a_fail_not_an_exception(self) -> None:
        def explode(_ctx: Any) -> doctor.CheckResult:
            raise RuntimeError("boom")

        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "interpreter", explode)
        self.assertEqual(item.status, "fail")
        self.assertIn("RuntimeError", item.detail)
        self.assertIn("boom", item.evidence["traceback"])


class TestImportability(DoctorTestCase):
    def test_this_tree_imports(self) -> None:
        item = self.check("importability")
        self.assertEqual(item.status, "ok", item.detail)
        self.assertGreaterEqual(item.evidence["files"], 14)

    def test_syntax_error_names_the_module(self) -> None:
        root = self.mini_tree({"good.py": "X = 1\n", "broken.py": "def f(:\n"})
        item = self.check("importability", root=root)
        self.assertEqual(item.status, "fail")
        self.assertIn("harness.broken", item.detail)
        self.assertIn("syntax", json.dumps(item.evidence["problems"]))

    def test_import_time_crash_names_the_module(self) -> None:
        root = self.mini_tree({"good.py": "X = 1\n", "boom.py": "raise RuntimeError('nope')\n"})
        item = self.check("importability", root=root)
        self.assertEqual(item.status, "fail")
        self.assertIn("harness.boom", item.detail)
        self.assertIn("RuntimeError", json.dumps(item.evidence["problems"]))

    def test_a_backup_makes_the_failure_fixable(self) -> None:
        root = self.mini_tree({"broken.py": "def f(:\n"})
        os.makedirs(os.path.join(self.path("state", "backups", "20260101-000000-demo")))
        item = self.check("importability", root=root, state=self.path("state"))
        self.assertEqual(item.status, "fail")
        self.assertTrue(item.fixable)
        self.assertEqual(item.fix_id, "import.restore_backup")

    def test_missing_package_is_reported(self) -> None:
        item = self.check("importability", root=self.path("nowhere"))
        self.assertEqual(item.status, "fail")
        self.assertIn("no harness/*.py", item.detail)


class TestStdlibOnly(DoctorTestCase):
    def test_this_tree_is_stdlib_only(self) -> None:
        item = self.check("stdlib_only")
        self.assertEqual(item.status, "ok", item.detail)
        self.assertEqual(item.evidence["third_party_modules"], [])

    def test_a_third_party_import_is_flagged(self) -> None:
        root = self.mini_tree({"dep.py": "import requests\n"}, with_audit=True)
        item = self.check("stdlib_only", root=root)
        self.assertEqual(item.status, "unfixable")
        self.assertIn("requests", json.dumps(item.evidence["static_suspicious"]))
        self.assertIn("pip", item.human_action or "")

    def test_missing_audit_script_is_skipped(self) -> None:
        item = self.check("stdlib_only", root=self.path("nowhere"))
        self.assertEqual(item.status, "skipped")


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


class TestConfigChecks(DoctorTestCase):
    def test_missing_config_warns_and_is_fixable(self) -> None:
        item = self.check("config_present")
        self.assertEqual(item.status, "warn")
        self.assertTrue(item.fixable)
        self.assertEqual(item.fix_id, "config.create")

    def test_create_fix_writes_a_keyless_config_0600(self) -> None:
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "config_present")
        outcome = doctor.apply_fix(ctx, "config.create", item)
        self.assertTrue(outcome.ok, outcome.error)
        with open(ctx.config_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["api_key"], "")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(ctx.config_path).st_mode), 0o600)

    def test_invalid_json_is_unfixable_and_explains_itself(self) -> None:
        self.write_config("{not json")
        item = self.check("config_parses")
        self.assertEqual(item.status, "unfixable")
        self.assertIn("valid JSON", item.detail)
        self.assertIn("fix the JSON", item.human_action or "")

    def test_unknown_and_missing_keys_warn(self) -> None:
        self.write_config({"model": "m", "api_base": "http://x", "mystery": 1})
        item = self.check("config_schema")
        self.assertEqual(item.status, "warn")
        self.assertEqual(item.evidence["unknown"], ["mystery"])
        self.assertIn("api_key", item.evidence["missing"])

    def test_bad_type_fails_and_the_fix_repairs_the_value(self) -> None:
        self.write_config(
            {
                "model": "m",
                "api_base": "http://x",
                "api_key": "k",
                "max_turns": "lots",
                "timeout": 60,
                "workspace": self.workspace_dir,
            }
        )
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "config_schema")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.fix_id, "config.schema_repair")
        outcome = doctor.apply_fix(ctx, "config.schema_repair", item)
        self.assertTrue(outcome.ok, outcome.error)
        with open(ctx.config_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["max_turns"], ctx.config.max_turns)
        self.assertEqual(payload["api_key"], "k", "the repair must not touch a valid key")
        self.assertEqual(doctor.run_one(ctx, "config_schema").status, "ok")

    def test_clean_config_is_ok(self) -> None:
        self.write_config(
            {
                "api_base": "http://127.0.0.1:9",
                "model": "m",
                "api_key": "sk-test",
                "max_turns": 8,
                "timeout": 60,
                "workspace": self.workspace_dir,
            }
        )
        item = self.check("config_schema")
        self.assertEqual(item.status, "ok", item.detail)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_world_readable_config_warns_then_chmods(self) -> None:
        self.write_config({"model": "m"}, mode=0o644)
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "config_permissions")
        self.assertEqual(item.status, "warn")
        self.assertEqual(item.fix_id, "config.chmod")
        outcome = doctor.apply_fix(ctx, "config.chmod", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(stat.S_IMODE(os.stat(ctx.config_path).st_mode), 0o600)
        self.assertEqual(doctor.run_one(ctx, "config_permissions").status, "ok")


class TestApiKeyChecks(DoctorTestCase):
    def test_no_key_is_unfixable(self) -> None:
        item = self.check("api_key_present", config=self.make_config(api_key=None))
        self.assertEqual(item.status, "unfixable")
        self.assertIn("DEEPSEEK_API_KEY", item.human_action or "")

    def test_key_present_is_ok_and_redacted(self) -> None:
        item = self.check("api_key_present", config=self.make_config(api_key="sk-canary-abcdefghijklmnop"))
        self.assertEqual(item.status, "ok")
        self.assertNotIn("sk-canary-abcdefghijklmnop", json.dumps(item.to_dict()))
        self.assertIn("<set:26 chars", item.evidence["key"])

    def test_placeholder_key_is_unfixable(self) -> None:
        item = self.check("api_key_shape", config=self.make_config(api_key="sk-REPLACE-ME"))
        self.assertEqual(item.status, "unfixable")
        self.assertIn("placeholder", item.detail)

    def test_short_key_warns(self) -> None:
        item = self.check("api_key_shape", config=self.make_config(api_key="shortkey"))
        self.assertEqual(item.status, "warn")

    def test_plausible_key_is_ok(self) -> None:
        item = self.check("api_key_shape", config=self.make_config(api_key="sk-" + "a1b2c3d4" * 4))
        self.assertEqual(item.status, "ok")

    def test_no_key_skips_the_shape_check(self) -> None:
        item = self.check("api_key_shape", config=self.make_config(api_key=None))
        self.assertEqual(item.status, "skipped")


# --------------------------------------------------------------------------------------
# network and provider
# --------------------------------------------------------------------------------------


class TestNetworkChecks(DoctorTestCase):
    def test_reachable_host_is_ok(self) -> None:
        with MockProvider([text_response("hi")]) as provider:
            ctx = self.make_ctx(config=self.make_config(api_base=provider.api_base, api_key="sk-test"), network=True)
            item = doctor.run_one(ctx, "network_reachable")
        self.assertEqual(item.status, "ok", item.detail)
        self.assertEqual(item.evidence["host"], "127.0.0.1")

    def test_closed_port_is_a_refusal_with_its_own_action(self) -> None:
        ctx = self.make_ctx(
            config=self.make_config(api_base="http://127.0.0.1:{}".format(self.free_port())), network=True
        )
        item = doctor.run_one(ctx, "network_reachable")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.evidence["failure"], "refused")
        self.assertIn("nothing is listening", item.human_action or "")

    def test_https_to_a_plain_http_server_is_a_tls_failure(self) -> None:
        with MockProvider([text_response("hi")]) as provider:
            base = "https://127.0.0.1:{}".format(provider.port)
            ctx = self.make_ctx(config=self.make_config(api_base=base), network=True)
            item = doctor.run_one(ctx, "network_reachable")
        self.assertEqual(item.status, "fail")
        self.assertIn(item.evidence["failure"], ("tls", "timeout"))
        self.assertNotEqual(item.human_action, doctor.network_action("refused"))

    def test_dns_failure_has_a_distinct_action(self) -> None:
        ctx = self.make_ctx(config=self.make_config(api_base="https://no-such-host.invalid"), network=True)
        with mock.patch.object(
            doctor, "probe_connect", side_effect=doctor.ProbeFailure("dns", "name resolution failed")
        ):
            item = doctor.run_one(ctx, "network_reachable")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.evidence["failure"], "dns")
        self.assertIn("DNS", item.human_action or "")
        self.assertNotEqual(doctor.network_action("dns"), doctor.network_action("timeout"))
        self.assertNotEqual(doctor.network_action("tls"), doctor.network_action("timeout"))

    def test_network_checks_are_skipped_when_off(self) -> None:
        item = self.check("network_reachable", network=False)
        self.assertEqual(item.status, "skipped")
        self.assertIn("--no-network", item.detail)


class TestProviderChecks(DoctorTestCase):
    def test_200_is_ok_and_makes_no_fuss(self) -> None:
        with MockProvider([ok_spec]) as provider:
            ctx = self.make_ctx(config=self.make_config(api_base=provider.api_base, api_key="sk-test"), network=True)
            auth = doctor.run_one(ctx, "api_auth")
            model = doctor.run_one(ctx, "model_accepted")
        self.assertEqual(auth.status, "ok", auth.detail)
        self.assertEqual(auth.evidence["status"], 200)
        self.assertEqual(model.status, "ok", model.detail)

    def test_401_is_unfixable_and_never_echoes_the_key(self) -> None:
        script = [error_response(401, "invalid api key sk-canary-should-not-appear")]
        with MockProvider(script) as provider:
            ctx = self.make_ctx(
                config=self.make_config(api_base=provider.api_base, api_key="sk-canary-abcdefghijklmnop"),
                network=True,
            )
            item = doctor.run_one(ctx, "api_auth")
        self.assertEqual(item.status, "unfixable")
        self.assertIn("rejected the credentials", item.detail)
        self.assertNotIn("sk-canary-abcdefghijklmnop", json.dumps(item.to_dict()))
        self.assertNotIn("sk-canary-should-not-appear", json.dumps(item.to_dict()))

    def test_429_is_a_warning(self) -> None:
        with MockProvider([error_response(429, "slow down", **{"retry-after": "1"})]) as provider:
            ctx = self.make_ctx(config=self.make_config(api_base=provider.api_base, api_key="sk-test"), network=True)
            item = doctor.run_one(ctx, "api_auth")
        self.assertEqual(item.status, "warn")
        self.assertIn("429", item.detail)

    def test_5xx_is_a_provider_side_failure(self) -> None:
        with MockProvider([error_response(503, "down")]) as provider:
            ctx = self.make_ctx(config=self.make_config(api_base=provider.api_base, api_key="sk-test"), network=True)
            item = doctor.run_one(ctx, "api_auth")
        self.assertEqual(item.status, "fail")
        self.assertIn("provider's side", item.human_action or "")

    def test_timeout_is_classified(self) -> None:
        with MockProvider([{"delay": 2.0, "json": OK_JSON["json"]}]) as provider:
            ctx = self.make_ctx(config=self.make_config(api_base=provider.api_base, api_key="sk-test"), network=True)
            ctx.network_timeout = 0.3
            item = doctor.run_one(ctx, "api_auth")
        self.assertEqual(item.status, "fail")
        self.assertIn("timeout", item.evidence.get("failure", "") + item.detail)

    def test_404_triggers_the_variant_probe_and_finds_v1(self) -> None:
        with MockProvider([text_response("unused")]) as provider:
            provider._spec_for = _path_spec(provider)  # type: ignore[assignment]
            provider._httpd.spec_for = provider._spec_for  # type: ignore[attr-defined]
            config = self.make_config(api_base=provider.api_base, api_key="sk-test")
            ctx = self.make_ctx(config=config, network=True)
            item = doctor.run_one(ctx, "api_auth")
            self.assertEqual(item.status, "fail")
            self.assertTrue(item.fixable)
            self.assertEqual(item.fix_id, "api_base.rewrite")
            self.assertEqual(item.evidence["working_base"], provider.api_base + "/v1")
            outcome = doctor.apply_fix(ctx, "api_base.rewrite", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(ctx.config.api_base, provider.api_base + "/v1")
        with open(self.path("config.json"), encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(written["api_base"], provider.api_base + "/v1")

    def test_404_everywhere_is_unfixable(self) -> None:
        script = [error_response(404, "nope")]
        with MockProvider(script) as provider:
            ctx = self.make_ctx(config=self.make_config(api_base=provider.api_base, api_key="sk-test"), network=True)
            item = doctor.run_one(ctx, "api_auth")
        self.assertEqual(item.status, "unfixable")
        self.assertIn("no candidate endpoint answered", item.detail)

    def test_model_fallback_is_written_by_the_fix(self) -> None:
        with MockProvider([model_spec]) as provider:
            config = self.make_config(api_base=provider.api_base, api_key="sk-test", model="bad-model")
            ctx = self.make_ctx(config=config, network=True, models=("mock-model",))
            auth = doctor.run_one(ctx, "api_auth")
            self.assertEqual(auth.status, "fail")
            self.assertEqual(auth.fix_id, "model.rewrite")
            model = doctor.run_one(ctx, "model_accepted")
            self.assertEqual(model.status, "fail")
            self.assertEqual(model.evidence["working_model"], "mock-model")
            outcome = doctor.apply_fix(ctx, "model.rewrite", model)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(ctx.config.model, "mock-model")
        with open(self.path("config.json"), encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(written["model"], "mock-model")

    def test_no_model_is_accepted_reports_unfixable(self) -> None:
        with MockProvider([error_response(400, "model 'x' not found")]) as provider:
            config = self.make_config(api_base=provider.api_base, api_key="sk-test", model="bad-model")
            ctx = self.make_ctx(config=config, network=True, models=("also-bad",))
            item = doctor.run_one(ctx, "model_accepted")
        self.assertEqual(item.status, "unfixable")
        self.assertIn("model list", item.human_action or "")

    def test_auth_skipped_without_a_key(self) -> None:
        ctx = self.make_ctx(config=self.make_config(api_key=None), network=True)
        self.assertEqual(doctor.run_one(ctx, "api_auth").status, "skipped")

    def test_model_check_defers_to_a_broken_endpoint(self) -> None:
        ctx = self.make_ctx(
            config=self.make_config(api_base="http://127.0.0.1:{}".format(self.free_port()), api_key="sk-test"),
            network=True,
        )
        doctor.run_one(ctx, "api_auth")
        item = doctor.run_one(ctx, "model_accepted")
        self.assertEqual(item.status, "skipped")
        self.assertEqual(item.evidence.get("depends_on"), "api_auth")


# --------------------------------------------------------------------------------------
# local state
# --------------------------------------------------------------------------------------


class TestWorkspaceCheck(DoctorTestCase):
    def test_missing_workspace_fails_and_the_fix_creates_it(self) -> None:
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "workspace")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.fix_id, "workspace.create")
        outcome = doctor.apply_fix(ctx, "workspace.create", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(os.path.isdir(ctx.workspace))
        self.assertEqual(doctor.run_one(ctx, "workspace").status, "ok")

    def test_a_file_in_the_way_is_unfixable(self) -> None:
        with open(self.path("workspace"), "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        item = self.check("workspace")
        self.assertEqual(item.status, "unfixable")
        self.assertIn("not a directory", item.detail)

    def test_writable_workspace_is_ok(self) -> None:
        os.makedirs(self.path("workspace"))
        item = self.check("workspace")
        self.assertEqual(item.status, "ok", item.detail)


class TestSessionChecks(DoctorTestCase):
    def test_missing_sessions_dir_warns_and_is_fixable(self) -> None:
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "sessions")
        self.assertEqual(item.status, "warn")
        self.assertEqual(item.fix_id, "sessions.create")
        self.assertTrue(doctor.apply_fix(ctx, "sessions.create", item).ok)

    def test_healthy_log_is_ok(self) -> None:
        self.make_session_log()
        item = self.check("sessions", sessions_dir=self.path(""))
        self.assertEqual(item.status, "ok", item.detail)

    def test_torn_final_line_is_safe_to_repair_and_the_rest_survives(self) -> None:
        path = self.make_session_log(events=4)
        with open(path, "ab") as handle:
            handle.write(b'{"kind":"event","seq":4,"time":1,"type":"turn.st')
        ctx = self.make_ctx(sessions_dir=self.path(""))
        item = doctor.run_one(ctx, "sessions")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.evidence["problems"][0]["state"], "torn")
        self.assertEqual(item.fix_id, "session.repair_safe")
        outcome = doctor.apply_fix(ctx, "session.repair_safe", item)
        self.assertTrue(outcome.ok, outcome.error)
        log = SessionLog.resume(path, writable=False)
        try:
            self.assertEqual(len(log.events), 4)
        finally:
            log.close()
        self.assertTrue(os.path.exists(path + ".torn"), "the torn fragment must be kept")
        with open(path + ".torn", "rb") as handle:
            self.assertIn(b"turn.st", handle.read())
        self.assertEqual(doctor.run_one(ctx, "sessions").status, "ok")

    def test_a_complete_last_row_without_a_newline_is_not_torn(self) -> None:
        path = self.make_session_log(events=2)
        with open(path, "rb") as handle:
            raw = handle.read()
        with open(path, "wb") as handle:
            handle.write(raw.rstrip(b"\n"))
        ctx = self.make_ctx(sessions_dir=self.path(""))
        item = doctor.run_one(ctx, "sessions")
        self.assertEqual(item.status, "ok", item.detail)

    def test_corrupt_header_is_quarantined_not_deleted(self) -> None:
        path = self.path("bad.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("this is not a session log\n")
        ctx = self.make_ctx(sessions_dir=self.path(""))
        item = doctor.run_one(ctx, "sessions")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.evidence["problems"][0]["state"], "corrupt")
        self.assertEqual(item.fix_id, "session.repair_full")
        outcome = doctor.apply_fix(ctx, "session.repair_full", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".corrupt"))
        with open(path + ".corrupt", encoding="utf-8") as handle:
            self.assertIn("not a session log", handle.read())

    def test_oversized_log_is_compacted(self) -> None:
        from harness.session import user_message_event

        path = self.path("big.jsonl")
        log = SessionLog.create(path, workspace=self.workspace_dir)
        for index in range(6):
            log.append("message.user", user_message_event("task {}".format(index)))
        log.close()
        with mock.patch.object(doctor.budget, "MAX_SESSION_EVENTS", 4), mock.patch.object(
            doctor.budget, "KEEP_RECENT_EVENTS", 2
        ):
            ctx = self.make_ctx(sessions_dir=self.path(""))
            item = doctor.run_one(ctx, "sessions")
            self.assertEqual(item.status, "fail")
            self.assertEqual(item.evidence["problems"][0]["state"], "oversized")
            outcome = doctor.apply_fix(ctx, "session.repair_full", item)
            self.assertTrue(outcome.ok, outcome.error)
            self.assertIn("compacted", outcome.detail)
            self.assertEqual(doctor.run_one(ctx, "sessions").status, "ok")

    def test_a_corrupt_middle_row_is_not_truncated(self) -> None:
        path = self.make_session_log(events=3)
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        lines.insert(2, "{oops}")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        ctx = self.make_ctx(sessions_dir=self.path(""))
        item = doctor.run_one(ctx, "sessions")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.fix_id, "session.repair_full")
        self.assertEqual(item.evidence["problems"][0]["state"], "corrupt")


class TestMemoryCheck(DoctorTestCase):
    def _status(self, value: Optional[int]) -> str:
        with mock.patch.object(ios, "available_memory_bytes", return_value=value):
            return self.check("memory_headroom").status

    def test_low_memory_fails(self) -> None:
        self.assertEqual(self._status(400 * 1024 * 1024), "fail")

    def test_tight_memory_warns(self) -> None:
        self.assertEqual(self._status(600 * 1024 * 1024), "warn")

    def test_plenty_of_memory_is_ok(self) -> None:
        self.assertEqual(self._status(2 * 1024 * 1024 * 1024), "ok")

    def test_unsupported_interpreter_is_skipped(self) -> None:
        self.assertEqual(self._status(None), "skipped")

    def test_the_action_says_a_doctor_cannot_free_memory(self) -> None:
        with mock.patch.object(ios, "available_memory_bytes", return_value=100 * 1024 * 1024):
            item = self.check("memory_headroom")
        self.assertIn("cannot free memory", item.human_action or "")


# --------------------------------------------------------------------------------------
# iOS surface
# --------------------------------------------------------------------------------------


class TestIosChecks(DoctorTestCase):
    def test_modules_are_skipped_off_device(self) -> None:
        item = self.check("ios_modules")
        self.assertEqual(item.status, "skipped")
        self.assertEqual(item.evidence["counts"]["probed"], len(doctor.IOS_PROBE_MODULES))

    def test_an_injected_bridge_module_shows_up(self) -> None:
        self.install_bridge("share")
        item = self.check("ios_modules")
        self.assertIn("share", item.evidence["available"])

    def test_signatures_are_discovered_without_calling_anything(self) -> None:
        module = self.install_bridge("calendar_events")

        def save_event(title: str, start: str = "") -> None:  # pragma: no cover - never called
            raise AssertionError("the doctor must not call save_event")

        module.save_event = save_event  # type: ignore[attr-defined]
        calls_before = len(module.calls)  # type: ignore[attr-defined]
        item = self.check("ios_signatures")
        self.assertEqual(item.status, "ok", item.detail)
        signature = item.evidence["shapes"]["calendar_events.save_event"]
        for needle in ("title", "start", "="):
            self.assertIn(needle, signature)
        self.assertEqual(len(module.calls), calls_before)  # type: ignore[attr-defined]

    def test_a_missing_attribute_is_signature_drift(self) -> None:
        self.install_bridge("share")  # no `open` attribute
        item = self.check("ios_signatures")
        self.assertEqual(item.status, "warn")
        self.assertIn("share.open", item.detail)

    def test_capabilities_are_persisted_when_allowed(self) -> None:
        module = self.install_bridge("background")

        class BackgroundTask:
            def __init__(self, name: str = "") -> None:  # pragma: no cover - never called
                self.name = name

        module.BackgroundTask = BackgroundTask  # type: ignore[attr-defined]
        ctx = self.make_ctx(persist=True)
        item = doctor.run_one(ctx, "ios_signatures")
        self.assertEqual(item.status, "ok", item.detail)
        self.assertTrue(item.evidence["persisted"])
        with open(ctx.capabilities_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIn("background.BackgroundTask", payload["signatures"])
        self.assertIn("name", payload["signatures"]["background.BackgroundTask"]["signature"])

    def test_shortcuts_url_is_this_installation(self) -> None:
        item = self.check("shortcuts_wiring")
        urls = doctor.shortcuts_urls(self.make_ctx())
        self.assertEqual(item.status, "warn")
        self.assertTrue(urls["url"].startswith("pyto://python/"))
        self.assertIn("task=", urls["url"])
        self.assertIn(urls["url"], item.detail)

    def test_shortcuts_fix_writes_the_document(self) -> None:
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "shortcuts_wiring")
        outcome = doctor.apply_fix(ctx, "shortcuts.write_doc", item)
        self.assertTrue(outcome.ok, outcome.error)
        with open(ctx.shortcuts_doc, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(doctor.shortcuts_urls(ctx)["url"], text)
        self.assertIn("Run Script", text)
        self.assertEqual(doctor.run_one(ctx, "shortcuts_wiring").status, "ok")


# --------------------------------------------------------------------------------------
# the self-test check
# --------------------------------------------------------------------------------------


class TestSelftestCheck(DoctorTestCase):
    #: One real run of the documented fast subset, shared by the tests that need it:
    #: nested suite runs are bounded work, and this keeps the outer suite quick.
    fast_report: Optional[Dict[str, Any]] = None

    def test_skipped_without_deep(self) -> None:
        item = self.check("selftest")
        self.assertEqual(item.status, "skipped")
        self.assertIn("--deep", item.detail)

    def test_deep_runs_the_fast_subset(self) -> None:
        if TestSelftestCheck.fast_report is None:
            ctx = self.make_ctx(root=ROOT, deep=True)
            report = doctor.run_offline_tests(ctx, modules=doctor.FAST_TEST_MODULES)
            TestSelftestCheck.fast_report = report
        report = TestSelftestCheck.fast_report
        self.assertTrue(report["ok"], report.get("output_tail"))
        self.assertEqual(report["mode"], "subprocess")
        self.assertGreater(report["ran"], 100)
        self.assertLess(report["duration_ms"], 60000)

    def test_deep_check_reports_the_gate_failure(self) -> None:
        root = self.copy_tree()
        with open(os.path.join(root, "tests", "test_zz_broken.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "import unittest\n\n\nclass TestBroken(unittest.TestCase):\n"
                "    def test_fails(self):\n        self.assertEqual(1, 2)\n"
            )
        ctx = self.make_ctx(root=root, deep=True)
        with mock.patch.object(doctor, "FAST_TEST_MODULES", ("test_zz_broken",)):
            item = doctor.run_one(ctx, "selftest")
        self.assertEqual(item.status, "fail")
        self.assertEqual(item.evidence["failures"], 1)
        self.assertIn("assertEqual", item.evidence["output_tail"])

    def test_run_offline_tests_parses_counts(self) -> None:
        ctx = self.make_ctx(root=ROOT)
        report = doctor.run_offline_tests(ctx, modules=("test_config",))
        self.assertTrue(report["ok"], report.get("output_tail"))
        self.assertGreater(report["ran"], 10)
        self.assertEqual(report["failures"], 0)

    def test_nested_suites_are_capped(self) -> None:
        """A suite that runs the suite that runs the suite must stop, not fork-bomb."""
        ctx = self.make_ctx(root=ROOT, deep=True)
        with mock.patch.dict(os.environ, {doctor.SELFTEST_DEPTH_ENV: str(doctor.MAX_SELFTEST_DEPTH)}):
            report = doctor.run_offline_tests(ctx, modules=("test_config",))
        self.assertFalse(report["ok"])
        self.assertEqual(report["mode"], "refused")
        self.assertIn("fork bomb", report["error"])

    def test_the_gate_subset_is_bounded_and_covers_the_file(self) -> None:
        modules = doctor.gate_modules_for("harness/config.py")
        self.assertIn("test_config", modules)
        self.assertLess(len(modules), 10)
        repair_modules = doctor.gate_modules_for("harness/repair.py")
        self.assertIn("test_repair", repair_modules)
        self.assertIn("test_config", repair_modules)
        self.assertIn("test_doctor", doctor.gate_modules_for("harness/doctor.py"))


# --------------------------------------------------------------------------------------
# fixes, report, health file, first run
# --------------------------------------------------------------------------------------


class TestFixEngine(DoctorTestCase):
    def test_unknown_fix_is_an_error_value(self) -> None:
        ctx = self.make_ctx()
        outcome = doctor.apply_fix(ctx, "nope.nope")
        self.assertFalse(outcome.ok)
        self.assertIn("unknown fix id", outcome.error)

    def test_a_crashing_fix_is_an_error_value(self) -> None:
        ctx = self.make_ctx()

        def explode(_ctx: Any, _item: Any) -> Any:
            raise RuntimeError("bad fix")

        with mock.patch.dict(doctor.FIXES, {"x.y": doctor.Fix("x.y", "workspace", "boom", False, explode)}):
            outcome = doctor.apply_fix(ctx, "x.y")
        self.assertFalse(outcome.ok)
        self.assertIn("RuntimeError", outcome.error)

    def test_safe_only_applies_only_the_safe_fixes(self) -> None:
        ctx = self.make_ctx()
        results = doctor.run_checks(ctx)
        after, outcomes = doctor.apply_fixes(ctx, results, safe_only=True)
        applied = {outcome.fix_id for outcome in outcomes}
        self.assertIn("workspace.create", applied)
        self.assertIn("sessions.create", applied)
        self.assertNotIn("config.create", applied)
        statuses = {item.id: item.status for item in after}
        self.assertEqual(statuses["workspace"], "fixed")
        self.assertEqual(statuses["config_present"], "warn")

    def test_a_successful_fix_marks_the_check_fixed(self) -> None:
        ctx = self.make_ctx()
        results = doctor.run_checks(ctx)
        after, outcomes = doctor.apply_fixes(ctx, results)
        self.assertTrue(any(outcome.ok for outcome in outcomes))
        workspace = [item for item in after if item.id == "workspace"][0]
        self.assertEqual(workspace.status, "fixed")
        self.assertIn("fixed_by", workspace.evidence)


class TestReports(DoctorTestCase):
    def test_exit_codes(self) -> None:
        ok = [doctor.result("x", "x", "ok"), doctor.result("y", "y", "warn")]
        self.assertEqual(doctor.exit_code(ok), 0)
        broken = [doctor.result("x", "x", "fail", fixable=True, fix_id="workspace.create")]
        self.assertEqual(doctor.exit_code(broken), 1)
        human = [doctor.result("x", "x", "unfixable"), doctor.result("y", "y", "ok")]
        self.assertEqual(doctor.exit_code(human), 2)

    def test_summary_line_grammar(self) -> None:
        one = [doctor.result("a", "a", "ok"), doctor.result("b", "b", "unfixable")]
        self.assertIn("1 ok", doctor.summary_line(one))
        self.assertIn("1 needs you", doctor.summary_line(one))
        two = one + [doctor.result("c", "c", "fail")]
        self.assertIn("2 need you", doctor.summary_line(two))

    def test_fix_summary_line_shape(self) -> None:
        results = [doctor.result("a", "a", "fixed"), doctor.result("b", "b", "unfixable")]
        self.assertEqual(doctor.fix_summary_line(results), "1 fixed, 1 needs you, 0 failed")

    def test_report_groups_by_status_and_shows_actions(self) -> None:
        results = [
            doctor.result("a", "a", "ok", "fine"),
            doctor.result("b", "b", "fail", "broken", fixable=True, fix_id="workspace.create", human_action="do it"),
        ]
        text = doctor.format_report(results, title="doctor")
        self.assertIn("fail (1)", text)
        self.assertIn("ok (1)", text)
        self.assertIn("fix: workspace.create", text)
        self.assertIn("you: do it", text)

    def test_compact_report_is_capped(self) -> None:
        results = [doctor.result("check{}".format(i), "t", "fail", "x" * 200) for i in range(30)]
        text = doctor.compact_report(results, limit=500)
        self.assertLessEqual(len(text), 560)
        self.assertIn("truncated", text)


class TestHealthAndFirstRun(DoctorTestCase):
    def test_health_round_trip_and_age(self) -> None:
        ctx = self.make_ctx(persist=True)
        results = doctor.run_checks(ctx, only=("interpreter",))
        path = doctor.save_health(ctx, results)
        self.assertIsNotNone(path)
        payload = doctor.load_health(ctx.state)
        self.assertEqual(payload["counts"]["ok"] + payload["counts"]["warn"], 1)
        self.assertLess(doctor.health_age_seconds(ctx.state), 60)
        self.assertIsNone(doctor.health_age_seconds(self.path("nowhere")))

    def test_first_run_prints_once_then_goes_quiet(self) -> None:
        config = self.make_config(api_key=None)
        env = {"PYTO_HARNESS_STATE_DIR": self.path("state"), "PYTO_HARNESS_SESSIONS_DIR": self.path("sessions")}
        first = doctor.first_run(config, env=env)
        self.assertIsNotNone(first)
        self.assertTrue(first.startswith("doctor: "))
        self.assertIsNone(doctor.first_run(config, env=env), "a fresh health file must suppress the pass")
        self.assertIsNotNone(doctor.first_run(config, env=env, force=True))

    def test_first_run_honours_the_env_kill_switch(self) -> None:
        config = self.make_config()
        env = {"PYTO_HARNESS_NO_DOCTOR": "1"}
        self.assertIsNone(doctor.first_run(config, env=env))

    def test_first_run_applies_only_safe_fixes(self) -> None:
        config = self.make_config(api_key="sk-test")
        env = {"PYTO_HARNESS_STATE_DIR": self.path("state")}
        line = doctor.first_run(config, env=env)
        self.assertIn("fixed", line or "")
        self.assertTrue(os.path.isdir(self.workspace_dir))
        self.assertFalse(os.path.exists(self.path("config.json")), "creating a config is not a safe auto-fix")

    def test_first_run_never_raises(self) -> None:
        config = self.make_config()
        with mock.patch.object(doctor, "run_checks", side_effect=RuntimeError("everything is broken")):
            self.assertIsNone(doctor.first_run(config, env={}))

    def test_health_file_never_contains_the_key(self) -> None:
        config = self.make_config(api_key="sk-canary-abcdefghijklmnop")
        ctx = self.make_ctx(config=config, persist=True)
        results = doctor.run_checks(ctx)
        path = doctor.save_health(ctx, results)
        with open(path, encoding="utf-8") as handle:
            self.assertNotIn("sk-canary-abcdefghijklmnop", handle.read())


class TestHomeReporting(DoctorTestCase):
    """The doctor must name the resolved home and never crash while reporting it."""

    def test_report_shows_the_home_and_how_it_was_chosen(self) -> None:
        ctx = self.make_ctx()
        ctx.home = self.path("Documents")
        ctx.home_note = "from cwd; HOME was unusable"
        ctx.home_source = "cwd"
        report = doctor.format_report([doctor.result("home", "Home folder", "ok", "fine")], ctx=ctx)
        self.assertIn("home       : {} (from cwd; HOME was unusable)".format(ctx.home), report)

    def test_an_unresolved_home_is_printed_not_hidden(self) -> None:
        ctx = self.make_ctx()
        ctx.home = ""
        ctx.home_error = "cannot find a writable folder for ~/{}".format(doctor.home.STATE_DIR_NAME)
        report = doctor.format_report([doctor.result("home", "Home folder", "unfixable", "nope")], ctx=ctx)
        self.assertIn("home       : <unresolved", report)

    def test_home_check_ok_when_the_state_directory_is_writable(self) -> None:
        ctx = self.make_ctx()
        os.makedirs(ctx.state, exist_ok=True)
        item = doctor.run_one(ctx, "home")
        self.assertEqual(item.status, "ok", item.detail)
        self.assertIn(ctx.state, item.detail)

    def test_home_check_fails_with_the_workaround_on_an_unwritable_state_dir(self) -> None:
        ctx = self.make_ctx()
        os.makedirs(ctx.state, exist_ok=True)
        if os.geteuid() == 0:  # pragma: no cover - mode checks need a non-root user
            self.skipTest("running as root: every directory is writable")
        os.chmod(ctx.state, 0o500)
        self.addCleanup(os.chmod, ctx.state, 0o700)
        item = doctor.run_one(ctx, "home")
        self.assertTrue(item.failed(), item.detail)
        self.assertIn("PYTO_HARNESS_HOME", item.human_action or "")

    def test_home_check_reports_an_unresolvable_home_without_raising(self) -> None:
        ctx = self.make_ctx()
        ctx.home = ""
        ctx.home_error = (
            "cannot find a writable folder for ~/{}\n".format(doctor.home.STATE_DIR_NAME)
            + doctor.home.WORKAROUND
        )
        item = doctor.run_one(ctx, "home")
        self.assertTrue(item.failed())
        self.assertIn("PYTO_HARNESS_HOME", item.human_action or "")
        self.assertIn("PYTO_HARNESS_HOME", doctor.format_report([item], ctx=ctx))

    def test_for_config_survives_having_nowhere_writable(self) -> None:
        unwritable = self.path("readonly")
        os.makedirs(unwritable, exist_ok=True)
        if os.geteuid() == 0:  # pragma: no cover - mode checks need a non-root user
            self.skipTest("running as root: every directory is writable")
        os.chmod(unwritable, 0o500)
        self.addCleanup(os.chmod, unwritable, 0o700)
        real = os.path.expanduser

        def broken(path):
            return path if str(path).startswith("~") else real(path)

        env = {"PYTO_HARNESS_HOME": "", "HOME": unwritable}
        with mock.patch.object(doctor.home, "_cwd", return_value=unwritable), mock.patch.object(
            doctor.home, "entry_point_dirs", return_value=[]
        ), mock.patch.object(doctor.home, "_temp_root", return_value=os.path.join(unwritable, "tmp")), mock.patch.object(
            doctor.home.os.path, "expanduser", side_effect=broken
        ):
            ctx = doctor.DoctorContext.for_config(
                self.make_config(),
                env=env,
                config_path=self.path("config.json"),
                state=self.path("state"),
                workspace=self.workspace_dir,
                sessions_dir=self.path("sessions"),
            )
        self.assertEqual(ctx.home, "")
        self.assertIn("PYTO_HARNESS_HOME", ctx.home_error)
        results = [doctor.run_one(ctx, "home")]
        report = doctor.format_report(results, ctx=ctx)
        self.assertIn("<unresolved", report)
        self.assertIn("PYTO_HARNESS_HOME", report)

    def test_home_check_is_registered(self) -> None:
        self.assertIn("home", doctor.CHECK_FUNCTIONS)
        self.assertIn("home", doctor.CHECK_TITLES)
        self.assertIn("home", doctor.FIXES["home.create"].check_id)

    def test_for_config_moves_a_hidden_state_directory_and_reports_it(self) -> None:
        """``--doctor`` is a run start too: it migrates before a single check reads state."""
        home_dir = self.path("legacy-home")
        legacy = os.path.join(home_dir, doctor.home.LEGACY_STATE_DIR_NAME)
        os.makedirs(os.path.join(legacy, "sessions"), exist_ok=True)
        with open(os.path.join(legacy, "config.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(os.path.join(legacy, "sessions", "s.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")

        ctx = self.make_ctx(env={"PYTO_HARNESS_HOME": home_dir})

        new_state = os.path.join(home_dir, doctor.home.STATE_DIR_NAME)
        self.assertEqual(ctx.home, home_dir)
        self.assertEqual(ctx.state_migration, doctor.home.MIGRATED_MESSAGE)
        self.assertTrue(os.path.isfile(os.path.join(new_state, "config.json")))
        self.assertTrue(os.path.isfile(os.path.join(new_state, "sessions", "s.jsonl")))
        self.assertFalse(os.path.lexists(legacy), "the old hidden folder must be gone")
        report = doctor.format_report([doctor.result("home", "Home folder", "ok", "fine")], ctx=ctx)
        self.assertIn("migration  : {}".format(doctor.home.MIGRATED_MESSAGE), report)

    def test_a_leftover_hidden_state_directory_is_reported_as_a_warning(self) -> None:
        home_dir = self.path("both-homes")
        new_state = os.path.join(home_dir, doctor.home.STATE_DIR_NAME)
        legacy = os.path.join(home_dir, doctor.home.LEGACY_STATE_DIR_NAME)
        os.makedirs(new_state, exist_ok=True)
        os.makedirs(legacy, exist_ok=True)
        ctx = self.make_ctx(state=new_state)
        ctx.home = home_dir
        item = doctor.run_one(ctx, "home")
        self.assertEqual(item.status, "warn", item.detail)
        self.assertIn(new_state, item.detail)
        self.assertIn("in use", item.detail)
        self.assertIn("untouched", item.detail)
        self.assertIn(legacy, item.human_action or "")
        self.assertTrue(os.path.isdir(legacy), "the doctor must never delete it")

    def test_a_merge_that_skipped_files_names_them_in_the_warning(self) -> None:
        """The startup hook moved what it could; the doctor says exactly what stayed behind."""
        home_dir = self.path("merged-home")
        new_state = os.path.join(home_dir, doctor.home.STATE_DIR_NAME)
        legacy = os.path.join(home_dir, doctor.home.LEGACY_STATE_DIR_NAME)
        os.makedirs(new_state, exist_ok=True)
        os.makedirs(legacy, exist_ok=True)
        # No config.json in the new directory, so the entries are merged -- except the one
        # that is already there, which must be left alone *and* named.
        for directory, name, text in (
            (new_state, "notes.txt", "the new notes\n"),
            (legacy, "notes.txt", "the old notes\n"),
            (legacy, "memory.json", "the old memory\n"),
        ):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(text)

        ctx = self.make_ctx(env={"PYTO_HARNESS_HOME": home_dir}, state=new_state)
        item = doctor.run_one(ctx, "home")

        self.assertIn("merged", ctx.state_migration)
        self.assertEqual(item.status, "warn", item.detail)
        self.assertIn("notes.txt", item.detail, "the skipped file must be named")
        self.assertEqual(self.read_file(os.path.join(new_state, "memory.json")), "the old memory\n")
        self.assertEqual(self.read_file(os.path.join(new_state, "notes.txt")), "the new notes\n")
        self.assertEqual(self.read_file(os.path.join(legacy, "notes.txt")), "the old notes\n")
        self.assertTrue(os.path.isdir(legacy), "the non-empty old folder must survive")

    def read_file(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()


class TestSecretsNeverLeak(DoctorTestCase):
    def test_no_check_result_contains_the_key(self) -> None:
        canary = "sk-canary-abcdefghijklmnop0123456789"
        with MockProvider([error_response(401, "denied")]) as provider:
            config = self.make_config(api_base=provider.api_base, api_key=canary)
            ctx = self.make_ctx(config=config, network=True, persist=True)
            results = doctor.run_checks(ctx)
            doctor.save_health(ctx, results)
            rendered = json.dumps([item.to_dict() for item in results], default=str)
            report = doctor.format_report(results, ctx=ctx)
            compact = doctor.compact_report(results)
            with open(ctx.health_path, encoding="utf-8") as handle:
                health = handle.read()
        for blob in (rendered, report, compact, health):
            self.assertNotIn(canary, blob)

    def test_scrub_replaces_keys_in_foreign_text(self) -> None:
        ctx = self.make_ctx(config=self.make_config(api_key="sk-canary-abcdefghijklmnop"))
        text = doctor.scrub_secrets("authorization: Bearer sk-canary-abcdefghijklmnop and sk-other1234567890", ctx)
        self.assertNotIn("sk-canary", text)
        self.assertNotIn("sk-other", text)
        self.assertIn("<redacted>", text)


if __name__ == "__main__":
    unittest.main()
