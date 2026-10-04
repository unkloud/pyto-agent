"""Configuration tests: precedence, coercion, and never leaking the key."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from harness import home
from harness.config import (
    Config,
    ConfigError,
    default_config_path,
    default_home,
    default_sessions_dir,
    default_spill_dir,
    default_state_dir,
    default_workspace,
    describe,
    ensure_workspace,
    load_config,
    load_config_file,
    redact_key,
    write_sample_config,
)

from .support import TempDirTestCase


class TestPrecedence(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_path = self.path("config.json")

    def write_config(self, payload) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_defaults_when_nothing_is_set(self) -> None:
        config = load_config(config_path=self.config_path, env={})
        self.assertEqual(config.api_base, "https://api.deepseek.com")
        self.assertEqual(config.model, "deepseek-chat")
        self.assertIsNone(config.api_key)
        self.assertTrue(config.workspace.endswith("pyto_harness_workspace"))
        self.assertFalse(config.yolo)

    def test_file_overrides_defaults(self) -> None:
        self.write_config({"model": "from-file", "api_base": "https://file.example"})
        config = load_config(config_path=self.config_path, env={})
        self.assertEqual(config.model, "from-file")
        self.assertEqual(config.api_base, "https://file.example")
        self.assertEqual(config.sources["model"], "file")

    def test_env_overrides_file(self) -> None:
        self.write_config({"model": "from-file", "api_base": "https://file.example"})
        config = load_config(
            config_path=self.config_path,
            env={"PYTO_HARNESS_MODEL": "from-env", "PYTO_HARNESS_API_BASE": "https://env.example"},
        )
        self.assertEqual(config.model, "from-env")
        self.assertEqual(config.api_base, "https://env.example")
        self.assertEqual(config.sources["model"], "env:PYTO_HARNESS_MODEL")

    def test_cli_overrides_env_and_file(self) -> None:
        self.write_config({"model": "from-file"})
        config = load_config(
            config_path=self.config_path,
            env={"PYTO_HARNESS_MODEL": "from-env"},
            overrides={"model": "from-cli"},
        )
        self.assertEqual(config.model, "from-cli")
        self.assertEqual(config.sources["model"], "cli")

    def test_none_overrides_are_ignored(self) -> None:
        self.write_config({"model": "from-file"})
        config = load_config(config_path=self.config_path, env={}, overrides={"model": None, "api_base": ""})
        self.assertEqual(config.model, "from-file")

    def test_deepseek_key_is_read_from_the_environment(self) -> None:
        config = load_config(config_path=self.config_path, env={"DEEPSEEK_API_KEY": "sk-deep"})
        self.assertEqual(config.api_key, "sk-deep")
        self.assertEqual(config.sources["api_key"], "env:DEEPSEEK_API_KEY")

    def test_openai_key_is_the_second_choice(self) -> None:
        config = load_config(config_path=self.config_path, env={"OPENAI_API_KEY": "sk-openai"})
        self.assertEqual(config.api_key, "sk-openai")

    def test_deepseek_key_wins_over_openai(self) -> None:
        config = load_config(
            config_path=self.config_path, env={"DEEPSEEK_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-b"}
        )
        self.assertEqual(config.api_key, "sk-a")

    def test_empty_env_values_are_ignored(self) -> None:
        self.write_config({"model": "from-file"})
        config = load_config(config_path=self.config_path, env={"PYTO_HARNESS_MODEL": ""})
        self.assertEqual(config.model, "from-file")

    def test_use_env_false_skips_the_environment(self) -> None:
        config = load_config(config_path=self.config_path, env={"DEEPSEEK_API_KEY": "sk-x"}, use_env=False)
        self.assertIsNone(config.api_key)


class TestCoercion(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_path = self.path("config.json")

    def test_numeric_strings_from_env(self) -> None:
        config = load_config(
            config_path=self.config_path,
            env={"PYTO_HARNESS_MAX_TURNS": "3", "PYTO_HARNESS_TIMEOUT": "12.5"},
        )
        self.assertEqual(config.max_turns, 3)
        self.assertEqual(config.timeout, 12.5)

    def test_booleans_from_strings(self) -> None:
        for raw, expected in (("1", True), ("true", True), ("no", False), ("off", False)):
            config = load_config(config_path=self.config_path, env={"PYTO_HARNESS_STREAM": raw})
            self.assertEqual(config.stream, expected, raw)

    def test_bad_integer_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(config_path=self.config_path, env={"PYTO_HARNESS_MAX_TURNS": "many"})

    def test_bad_boolean_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(config_path=self.config_path, env={"PYTO_HARNESS_STREAM": "maybe"})

    def test_zero_turns_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(config_path=self.config_path, env={}, overrides={"max_turns": 0})

    def test_non_positive_timeout_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(config_path=self.config_path, env={}, overrides={"timeout": 0})

    def test_unknown_file_keys_are_ignored(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"model": "m", "not_a_setting": 1}, handle)
        self.assertEqual(load_config(config_path=self.config_path, env={}).model, "m")

    def test_broken_json_is_a_config_error(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{oops")
        with self.assertRaises(ConfigError):
            load_config_file(self.config_path)

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_config_file(self.path("absent.json")), {})

    def test_extra_headers_merge(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"headers": {"X-A": "1"}}, handle)
        config = load_config(config_path=self.config_path, env={})
        self.assertEqual(config.extra_headers, {"X-A": "1"})


class TestSecrecy(TempDirTestCase):
    def test_redact_key_shows_only_the_tail(self) -> None:
        rendered = redact_key("sk-abcdefghijklmnop")
        self.assertIn("...mnop", rendered)
        self.assertNotIn("sk-abcdefgh", rendered)

    def test_redact_key_handles_missing_and_short(self) -> None:
        self.assertEqual(redact_key(None), "<unset>")
        self.assertEqual(redact_key(""), "<unset>")
        self.assertNotIn("abc", redact_key("abc"))

    def test_public_never_contains_the_key(self) -> None:
        config = Config(api_key="sk-super-secret-value")
        dumped = json.dumps(config.public())
        self.assertNotIn("sk-super-secret-value", dumped)
        self.assertIn("set", dumped)

    def test_describe_never_contains_the_key(self) -> None:
        config = Config(api_key="sk-super-secret-value")
        self.assertNotIn("sk-super-secret-value", describe(config))

    def test_describe_reports_missing_key(self) -> None:
        self.assertIn("MISSING", describe(Config()))

    def test_endpoint_has_no_key(self) -> None:
        config = Config(api_base="https://api.deepseek.com", api_key="sk-x")
        self.assertEqual(config.chat_completions_url, "https://api.deepseek.com/chat/completions")

    def test_sample_config_is_written_with_restrictive_mode(self) -> None:
        path = write_sample_config(self.path("cfg", "config.json"))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["api_base"], "https://api.deepseek.com")
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "a file holding an API key must not be world readable")


class TestPaths(TempDirTestCase):
    def test_default_workspace_is_under_home(self) -> None:
        self.assertIn("pyto_harness_workspace", default_workspace())
        self.assertTrue(os.path.isabs(default_workspace()))

    def test_spill_dir_defaults_inside_the_workspace(self) -> None:
        config = load_config(
            config_path=self.path("nope.json"), env={}, overrides={"workspace": self.workspace_dir}
        )
        self.assertTrue(config.spill_dir.startswith(self.workspace_dir))

    def test_sessions_dir_has_a_default(self) -> None:
        config = load_config(config_path=self.path("nope.json"), env={})
        self.assertTrue(config.sessions_dir.endswith("sessions"))


class TestHomeDerivedPaths(TempDirTestCase):
    """Every ``~``-derived default is a real, absolute path (see tests/test_home.py)."""

    def setUp(self) -> None:
        super().setUp()
        home.reset_home_cache()
        self.addCleanup(home.reset_home_cache)

    def test_defaults_follow_the_escape_hatch(self) -> None:
        hatch = self.path("hatch")
        with mock.patch.dict(
            os.environ,
            {"PYTO_HARNESS_HOME": hatch, "PYTO_HARNESS_CONFIG": "", "PYTO_HARNESS_STATE_DIR": ""},
        ):
            self.assertEqual(default_home(), hatch)
            derived = {
                "config": default_config_path(),
                "state": default_state_dir(),
                "sessions": default_sessions_dir(),
            }
            self.assertEqual(derived["config"], os.path.join(hatch, home.STATE_DIR_NAME, "config.json"))
            self.assertEqual(derived["state"], os.path.join(hatch, home.STATE_DIR_NAME))
            self.assertEqual(derived["sessions"], os.path.join(hatch, home.STATE_DIR_NAME, "sessions"))
            self.assertEqual(default_workspace(), os.path.join(hatch, "pyto_harness_workspace"))
            self.assertEqual(default_spill_dir(), os.path.join(hatch, "pyto_harness_workspace", "tool-output"))
            # Nothing the Files app has to show may be hidden: no dot-prefixed segment.
            for label, path in derived.items():
                for part in os.path.relpath(path, hatch).split(os.sep):
                    self.assertFalse(part.startswith("."), "{} is hidden: {}".format(label, path))
        self.assertTrue(os.path.isdir(hatch), "the escape hatch is created before it is used")

    def test_state_dir_follows_a_portable_config_file(self) -> None:
        portable = self.path("portable", "config.json")
        with mock.patch.dict(os.environ, {"PYTO_HARNESS_CONFIG": portable, "PYTO_HARNESS_STATE_DIR": ""}):
            self.assertEqual(default_state_dir(), self.path("portable"))

    def test_an_unexpandable_workspace_is_refused(self) -> None:
        real = os.path.expanduser

        def broken(path):
            return path if str(path).startswith("~") else real(path)

        with mock.patch.object(home.os.path, "expanduser", side_effect=broken):
            with self.assertRaises(ConfigError) as caught:
                ensure_workspace(Config(workspace="~/pyto_harness_workspace"))
            with self.assertRaises(ConfigError):
                load_config_file("~/config.json")
        self.assertIn("absolute path", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "~")))


if __name__ == "__main__":
    unittest.main()
