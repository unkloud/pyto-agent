"""End-to-end tests: the real CLI against the mock provider, and `--dry-run` safety."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from harness import home
from harness.loop import LoopOptions, run_turn
from harness.session import SessionLog
from harness.tools_ios import build_registry, default_context

from .mock_provider import MockProvider, text_response, tool_response
from .support import ROOT, TempDirTestCase, make_client


class TestDryRun(TempDirTestCase):
    """`--dry-run` must print the request and contact nothing at all."""

    def run_cli(self, *args: str, env: dict = None):
        import run as runner

        stdout, stderr = io.StringIO(), io.StringIO()
        environment = dict(os.environ)
        environment["PYTO_HARNESS_CONFIG"] = self.path("absent.json")
        environment.update(env or {})
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(environment)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = runner.main(
                    list(args) + ["--workspace", self.workspace_dir]
                )
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_prints_the_request(self) -> None:
        code, out, err = self.run_cli("--dry-run", "rename my screenshots")
        self.assertEqual(code, 0, err)
        self.assertIn("DRY RUN", out)
        self.assertIn("POST https://api.deepseek.com/chat/completions", out)
        self.assertIn('"model": "deepseek-chat"', out)
        self.assertIn("rename my screenshots", out)
        self.assertIn("tools offered to the model", out)

    def test_dry_run_lists_the_tools_that_would_be_offered(self) -> None:
        _, out, _ = self.run_cli("--dry-run", "anything")
        for tool in ("write_program", "run_program", "finish", "shortcut_run", "memory_write"):
            self.assertIn(tool, out)

    def test_dry_run_never_leaks_the_key(self) -> None:
        _, out, _ = self.run_cli("--dry-run", "hello", env={"DEEPSEEK_API_KEY": "sk-canary-do-not-print"})
        self.assertNotIn("sk-canary-do-not-print", out)
        self.assertIn("<redacted>", out)

    def test_dry_run_makes_no_network_call(self) -> None:
        """Point the API base at a live mock and prove the mock saw nothing."""
        with MockProvider([text_response("should not happen")]) as provider:
            code, out, err = self.run_cli("--dry-run", "hello", "--api-base", provider.api_base)
            self.assertEqual(code, 0, err)
            self.assertIn(provider.api_base, out)
            with provider.lock:
                seen = len(provider.requests)
        self.assertEqual(seen, 0, "--dry-run contacted the network")

    def test_dry_run_works_with_an_unwritable_workspace(self) -> None:
        """A preview writes nothing, so a read-only workspace must not stop it."""
        import tempfile
        from harness.config import Config
        from harness.loop import build_system_prompt
        import run as runner

        unwritable = os.path.join(tempfile.gettempdir(), "pyto-harness-not-a-dir")
        with open(unwritable, "w", encoding="utf-8") as handle:
            handle.write("a file, so makedirs fails")
        self.addCleanup(os.unlink, unwritable)
        config = Config(workspace=unwritable, api_key="sk-test")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runner.print_request_preview(
                config, build_system_prompt(config, config.workspace), "hello"
            )
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", stdout.getvalue())
        self.assertIn("POST https://api.deepseek.com/chat/completions", stdout.getvalue())
        self.assertIn("not writable", stdout.getvalue())

    def test_capabilities_needs_no_key_and_no_workspace(self) -> None:
        code, out, _ = self.run_cli("--capabilities")
        self.assertEqual(code, 0)
        self.assertIn("platform:", out)

    def test_tools_listing(self) -> None:
        code, out, _ = self.run_cli("--tools")
        self.assertEqual(code, 0)
        self.assertIn("write_program", out)

    def test_missing_key_is_an_error_with_instructions(self) -> None:
        code, out, err = self.run_cli("do something", env={"DEEPSEEK_API_KEY": ""})
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", err)

    def test_task_can_come_from_a_url_query_parameter(self) -> None:
        """`pyto://python/run.py?task=...` is how a Shortcut starts the harness."""
        code, out, err = self.run_cli("--dry-run", "task=rename%20my%20screenshots")
        self.assertEqual(code, 0, err)
        self.assertIn('"content": "rename my screenshots"', out)

    def test_task_can_come_from_the_environment(self) -> None:
        code, out, err = self.run_cli("--dry-run", env={"PYTO_HARNESS_TASK": "summarise my notes"})
        self.assertEqual(code, 0, err)
        self.assertIn('"content": "summarise my notes"', out)

    def test_yolo_shows_in_the_preview(self) -> None:
        _, out, _ = self.run_cli("--dry-run", "--yolo", "hello")
        self.assertIn("bypassed (--yolo)", out)


class TestCliEndToEnd(TempDirTestCase):
    def test_two_turn_conversation_writes_and_runs_a_program(self) -> None:
        program = "print('renamed 3 files')\n"
        script = [
            tool_response(
                ("write_program", {"path": "rename_shots.py", "source": program, "purpose": "rename screenshots"})
            ),
            tool_response(
                ("run_program", {"path_or_source": "rename_shots.py"}),
                ("finish", {"message": "Wrote rename_shots.py and it ran: renamed 3 files."}),
            ),
        ]
        with MockProvider(script) as provider:
            code, out, err = self.run_cli_with_provider(provider, "rename my screenshots by date")
            session_path = self.session_path_from(out)
            with provider.lock:
                requests = len(provider.requests)
                second = provider.messages_sent(1)

        self.assertEqual(code, 0, err)
        self.assertEqual(requests, 2, "the task should need exactly two model turns")
        written = os.path.join(self.workspace_dir, "rename_shots.py")
        self.assertTrue(os.path.exists(written), "the program must exist in the workspace")
        with open(written, encoding="utf-8") as handle:
            self.assertIn("renamed 3 files", handle.read())
        self.assertIn("renamed 3 files", out)
        self.assertIn("Wrote rename_shots.py", out)
        self.assertTrue(session_path.endswith(".jsonl"))
        self.assertTrue(os.path.exists(session_path), "the CLI must leave a resumable session log")
        # The second request must replay the assistant tool call and the tool result.
        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "tool"])

    def test_a_resumed_cli_session_keeps_history(self) -> None:
        with MockProvider([tool_response(("write_file", {"path": "keep.txt", "content": "kept"})), text_response("saved")]) as provider:
            code, out, err = self.run_cli_with_provider(provider, "save a note")
            session_path = self.session_path_from(out)
        self.assertEqual(code, 0, err)

        with MockProvider([text_response("It is still saved.")]) as provider:
            code, out, err = self.run_cli_with_provider(
                provider, "is it saved?", extra=["--resume", session_path]
            )
            sent = provider.messages_sent(0)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [m["role"] for m in sent], ["system", "user", "assistant", "tool", "assistant", "user"]
        )
        self.assertEqual(sent[5]["content"], "is it saved?")

    def test_denied_tool_in_a_real_run_does_not_break_the_turn(self) -> None:
        script = [tool_response(("share_text", {"text": "leak me"})), text_response("I did not share it.")]
        with MockProvider(script) as provider:
            # No --yolo and no TTY: the terminal approver reads EOF, which means "no".
            code, out, err = self.run_cli_with_provider(provider, "share my notes")
        self.assertEqual(code, 0, err)
        self.assertIn("denied", out)
        self.assertIn("I did not share it.", out)

    def test_yolo_allows_a_share(self) -> None:
        script = [tool_response(("share_text", {"text": "share me"})), text_response("shared")]
        with MockProvider(script) as provider:
            code, out, err = self.run_cli_with_provider(provider, "share my notes", extra=["--yolo"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("denied", out)

    # -- helpers -------------------------------------------------------------------

    def run_cli_with_provider(self, provider: MockProvider, task: str, extra=None):
        import run as runner

        stdout, stderr = io.StringIO(), io.StringIO()
        saved = dict(os.environ)
        os.environ.update(
            {
                "PYTO_HARNESS_CONFIG": self.path("absent.json"),
                "DEEPSEEK_API_KEY": "sk-test-key",
                "PYTO_HARNESS_NO_BROWSER": "1",
                "PYTO_HARNESS_SESSIONS_DIR": self.path("sessions"),
            }
        )
        arguments = [
            task,
            "--workspace",
            self.workspace_dir,
            "--api-base",
            provider.api_base,
            "--model",
            "mock-model",
            "--max-turns",
            "4",
        ] + list(extra or [])
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = runner.main(arguments)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, stdout.getvalue(), stderr.getvalue()

    def session_path_from(self, output: str) -> str:
        for line in output.splitlines():
            if line.startswith("[session: "):
                return line[len("[session: ") : -1]
        self.fail("no session path in the CLI output:\n{}".format(output))


class TestSubprocessCli(TempDirTestCase):
    """One run through a real `python run.py`, to prove the entry point works."""

    def test_run_py_help_and_capabilities(self) -> None:
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "run.py"), "--capabilities"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("platform:", result.stdout)

    def test_run_py_dry_run_is_offline(self) -> None:
        env = dict(os.environ)
        env["DEEPSEEK_API_KEY"] = "sk-test-key"
        env["PYTO_HARNESS_CONFIG"] = self.path("absent.json")
        env["PYTO_HARNESS_NO_BROWSER"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "run.py"),
                "--dry-run",
                "--workspace",
                self.workspace_dir,
                "write a script that renames my screenshots by date",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertIn("POST https://api.deepseek.com/chat/completions", result.stdout)
        self.assertIn("renames my screenshots by date", result.stdout)


class TestDirectConversation(TempDirTestCase):
    """The same two-turn shape driven through the loop API, with full transcripts."""

    def test_transcript_of_write_then_run(self) -> None:
        program = "print('3 files renamed')\n"
        script = [
            tool_response(("write_program", {"path": "sh.py", "source": program, "purpose": "rename"})),
            tool_response(
                ("run_program", {"path_or_source": "sh.py"}),
                ("finish", {"message": "Ready. Run sh.py."}),
            ),
        ]
        transcript = []
        with MockProvider(script) as provider:
            client = make_client(provider)
            registry = build_registry(default_context(self.workspace_dir))
            session = SessionLog.create(self.path("s.jsonl"), workspace=self.workspace_dir)
            options = LoopOptions(
                client=client, registry=registry, session=session, system_prompt="sys", max_turns=4
            )

            async def go():
                async for event in run_turn(options, "rename my screenshots"):
                    transcript.append(event)

            asyncio.run(go())
            client.close()
            session.close()

        self.assertTrue(any(event.kind == "tool.completed" for event in transcript))
        self.assertTrue(os.path.exists(os.path.join(self.workspace_dir, "sh.py")))
        finish_events = [event for event in transcript if event.kind == "finished"]
        self.assertEqual(finish_events[0].data["message"], "Ready. Run sh.py.")


class TestUnexpandableWorkspace(TempDirTestCase):
    """``--workspace "~/x"`` on a device that cannot expand ``~``: refuse, never create."""

    def test_workspace_with_a_literal_tilde_is_refused(self) -> None:
        import run as runner

        cwd = self.path("cwd")
        os.makedirs(cwd, exist_ok=True)
        real = os.path.expanduser

        def broken(path):
            return path if str(path).startswith("~") else real(path)

        stdout, stderr = io.StringIO(), io.StringIO()
        saved_env = dict(os.environ)
        saved_cwd = os.getcwd()
        os.environ["PYTO_HARNESS_CONFIG"] = self.path("absent.json")
        os.environ["PYTO_HARNESS_HOME"] = self.path("hatch")
        os.chdir(cwd)
        try:
            with mock.patch.object(os.path, "expanduser", side_effect=broken):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = runner.main(["--workspace", "~/somewhere", "hello"])
        finally:
            os.chdir(saved_cwd)
            os.environ.clear()
            os.environ.update(saved_env)
        self.assertEqual(code, 2, stdout.getvalue() + stderr.getvalue())
        self.assertIn("configuration error", stderr.getvalue())
        self.assertIn("absolute path", stderr.getvalue())
        self.assertFalse(os.path.exists(os.path.join(cwd, "~")), "a directory named '~' was created")
        self.assertFalse(os.path.exists(os.path.join(cwd, home.STATE_DIR_NAME)))


if __name__ == "__main__":
    unittest.main()
