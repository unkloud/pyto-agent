"""Local browser front-end tests with loopback HTTP and the scripted provider."""

from __future__ import annotations

import json
import io
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import unittest
from contextlib import redirect_stderr
from unittest import mock
from typing import Any, Dict, Optional, Tuple

import run as runner
from harness import programs
from harness.loop import make_policy
from harness.session import (
    SessionHeader,
    SessionLog,
    assistant_message_event,
    new_session_path,
    user_message_event,
)
from harness.tools import ToolDef
from harness.ui import UIApprover
from harness.web import LocalWebServer, WebController, WebState, _BackgroundKeepalive, run_web

from .mock_provider import MockProvider, text_response, tool_response
from .support import TempDirTestCase, make_client, make_options


class TestWebFrontEnd(TempDirTestCase):
    def make_controller(self, responses: Optional[list] = None) -> Tuple[WebController, MockProvider, Any]:
        provider = MockProvider(responses or [text_response("web reply")], strict_tool_protocol=True)
        self.addCleanup(provider.close)
        client = make_client(provider)
        self.addCleanup(client.close)
        registry = self.make_registry()
        approver = UIApprover()
        registry.policy = make_policy(prompter=approver, interactive=True, workspace=self.workspace_dir)
        registry.lock_policy()
        session = self.make_session()

        def options_factory(selected_session):
            return make_options(client, registry, selected_session, stop=threading.Event())

        options_factory.context = registry.context
        options_factory.registry = registry
        controller = WebController(
            options_factory=options_factory,
            session=session,
            approver=approver,
        )
        self.addCleanup(controller.close)
        return controller, provider, registry

    def start_server(self, controller: WebController) -> LocalWebServer:
        server = LocalWebServer(controller)
        server.start()
        self.addCleanup(server.close)
        return server

    def request(
        self,
        server: LocalWebServer,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[bytes] = None,
        token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        host: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        request_headers = dict(headers or {})
        request_headers["X-Pyto-Harness-Token"] = token if token is not None else server.token
        if host is not None:
            request_headers["Host"] = host
        request = urllib.request.Request(
            server.url + path.lstrip("/"),
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()

    def post_json(self, server: LocalWebServer, path: str, payload: Any) -> Tuple[int, Dict[str, Any]]:
        status, _headers, body = self.request(
            server,
            path,
            method="POST",
            payload=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return status, json.loads(body.decode("utf-8"))

    def wait_for(self, server: LocalWebServer, predicate: Any, timeout: float = 5.0) -> Tuple[Dict[str, Any], list]:
        cursor = 0
        collected = []
        deadline = time.monotonic() + timeout
        latest: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            status, _headers, body = self.request(server, "api/state?since={}".format(cursor))
            self.assertEqual(status, 200)
            latest = json.loads(body.decode("utf-8"))
            collected.extend(latest.get("events", []))
            cursor = latest.get("next_id", cursor)
            if predicate(latest, collected):
                return latest, collected
            time.sleep(0.025)
        self.fail("web state did not reach the expected condition; latest={!r}, events={!r}".format(latest, collected))

    def test_page_and_api_are_tokenized_loopback_with_origin_checks(self) -> None:
        controller, _provider, _registry = self.make_controller()
        server = self.start_server(controller)

        status, headers, body = self.request(server, "")
        self.assertEqual(status, 200)
        self.assertIn(b"Pyto Harness", body)
        self.assertIn(b"background keepalive", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(server.server.server_address[0], "127.0.0.1")
        for asset, mime in (("app.js", "text/javascript"), ("style.css", "text/css")):
            asset_status, asset_headers, asset_body = self.request(server, "asset/" + asset)
            self.assertEqual(asset_status, 200)
            self.assertIn(mime, asset_headers["Content-Type"])
            self.assertTrue(asset_body)
            if asset == "app.js":
                self.assertIn(b"document.createTextNode", asset_body)
                self.assertIn(b"safeMarkdownHref", asset_body)
                self.assertNotIn(b"innerHTML", asset_body)

        status, _headers, body = self.request(server, "api/state", token="wrong-token")
        self.assertEqual(status, 401)
        self.assertIn(b"Unauthorized", body)

        port = server.server.server_address[1]
        status, _headers, _body = self.request(server, "api/state", host="localhost:{}".format(port))
        self.assertEqual(status, 403)

        status, _headers, _body = self.request(
            server,
            "api/state",
            headers={"Origin": "http://example.test"},
        )
        self.assertEqual(status, 403)

        oversized = b"{" + b" " * 65536 + b"}"
        status, _headers, body = self.request(
            server,
            "api/chat",
            method="POST",
            payload=oversized,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"65536 bytes", body)

    def test_session_picker_lists_safe_metadata_and_resumes_by_server_resolved_id(self) -> None:
        controller, _provider, _registry = self.make_controller()
        controller.session.close()
        controller.session = None
        sessions_dir = self.path("session-choices")
        saved_path = os.path.join(sessions_dir, "saved.jsonl")
        saved = SessionLog.create(
            saved_path,
            header=SessionHeader(id="saved-session", workspace=self.workspace_dir, model="mock-model"),
        )
        saved.append("message.user", user_message_event("Please remember this request"))
        saved.append(
            "message.assistant",
            assistant_message_event({"role": "assistant", "content": "I have restored it."}),
        )
        saved.close()
        selected_paths = []

        def session_factory(path):
            selected_paths.append(path)
            return SessionLog.resume(path) if path else SessionLog.create(
                new_session_path(sessions_dir), workspace=self.workspace_dir
            )

        controller.sessions_dir = sessions_dir
        controller.session_factory = session_factory
        self.addCleanup(lambda: controller.session.close() if controller.session is not None else None)
        server = self.start_server(controller)

        status, _headers, body = self.request(server, "api/session")
        self.assertEqual(status, 200)
        available = json.loads(body.decode("utf-8"))
        self.assertFalse(available["selected"])
        self.assertEqual([row["id"] for row in available["sessions"]], ["saved-session"])
        self.assertNotIn(saved_path, body.decode("utf-8"))

        status, result = self.post_json(server, "api/session", {"id": "../saved.jsonl"})
        self.assertEqual(status, 400)
        self.assertEqual(selected_paths, [])

        status, result = self.post_json(server, "api/session", {"id": "saved-session"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["selected"])
        self.assertEqual(selected_paths, [saved_path])
        self.assertEqual(
            [(message["role"], message["text"]) for message in result["messages"]],
            [("user", "Please remember this request"), ("assistant", "I have restored it.")],
        )
        self.assertNotIn(saved_path, json.dumps(result))

    def test_session_picker_starts_an_independent_new_log(self) -> None:
        controller, _provider, _registry = self.make_controller()
        controller.session.close()
        controller.session = None
        sessions_dir = self.path("session-choices")
        old_path = os.path.join(sessions_dir, "old.jsonl")
        old = SessionLog.create(old_path, header=SessionHeader(id="old-session"))
        old.append("message.user", user_message_event("Keep this session"))
        old.close()
        controller.sessions_dir = sessions_dir
        controller.session_factory = lambda path: SessionLog.create(
            new_session_path(sessions_dir), workspace=self.workspace_dir
        )
        self.addCleanup(lambda: controller.session.close() if controller.session is not None else None)
        server = self.start_server(controller)

        status, result = self.post_json(server, "api/session", {"id": "new"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["selected"])
        self.assertNotEqual(controller.session.path, old_path)
        self.assertEqual(controller.session.project(), [])
        reopened = SessionLog.resume(old_path)
        self.assertEqual(reopened.project()[0]["content"], "Keep this session")
        reopened.close()
        self.assertEqual(len([name for name in os.listdir(sessions_dir) if name.endswith(".jsonl")]), 2)

    def test_chat_runs_through_provider_and_outputs_events(self) -> None:
        controller, provider, _registry = self.make_controller([text_response("Hello from the mock")])
        server = self.start_server(controller)

        status, result = self.post_json(server, "api/chat", {"prompt": "say hello"})
        self.assertEqual(status, 202, result)
        _state, events = self.wait_for(
            server,
            lambda state, events: any(item["type"] == "operation_finished" for item in events),
        )
        self.assertTrue(any(item["type"] == "user" and item["data"]["text"] == "say hello" for item in events))
        assistant = next(
            item for item in events
            if item["type"] == "output" and "Hello from the mock" in item["data"]["text"]
        )
        self.assertEqual(assistant["data"]["format"], "markdown")
        self.assertEqual(
            assistant["data"]["markdown"],
            [["paragraph", [["text", "Hello from the mock"]]]],
        )
        with provider.lock:
            self.assertEqual(len(provider.requests), 1)

    def test_assistant_markdown_keeps_hostile_markup_as_text_and_links_inert(self) -> None:
        response_text = '<img src=x onerror="bad()"> **bold** [unsafe](javascript:bad())'
        controller, _provider, _registry = self.make_controller([text_response(response_text)])
        server = self.start_server(controller)

        status, result = self.post_json(server, "api/chat", {"prompt": "show formatting"})
        self.assertEqual(status, 202, result)
        _state, events = self.wait_for(
            server,
            lambda state, collected: any(
                item["type"] == "output" and item["data"].get("format") == "markdown"
                for item in collected
            ),
        )
        assistant = next(item for item in events if item["type"] == "output" and item["data"].get("format") == "markdown")
        nodes = assistant["data"]["markdown"][0][1]
        self.assertEqual(nodes[0], ["text", '<img src=x onerror="bad()"> '])
        self.assertEqual(nodes[1][0], "strong")
        self.assertEqual(nodes[2][0], "text")
        self.assertEqual(nodes[3][0], "link")
        self.assertIsNone(nodes[3][1])

    def test_oversized_markdown_tree_falls_back_to_bounded_plain_text(self) -> None:
        state = WebState()
        text = "**x** " * 2000
        state.publish("output", {"text": text, "format": "markdown"})
        event = state.snapshot(0)["events"][0]
        self.assertEqual(event["data"]["text"], text)
        self.assertNotIn("format", event["data"])
        self.assertNotIn("markdown", event["data"])

    def test_stop_turn_cancels_a_provider_request(self) -> None:
        controller, provider, _registry = self.make_controller([{"delay": 2.0}])
        server = self.start_server(controller)

        status, result = self.post_json(server, "api/chat", {"prompt": "wait for a slow reply"})
        self.assertEqual(status, 202, result)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with provider.lock:
                if provider.requests:
                    break
            time.sleep(0.01)
        else:
            self.fail("mock provider did not receive the request")
        status, result = self.post_json(server, "api/stop-turn", {})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["stopped"])
        latest, events = self.wait_for(
            server,
            lambda state, events: not state["busy"] and any(item["type"] == "operation_finished" for item in events),
            timeout=3,
        )
        self.assertFalse(latest["busy"])
        self.assertTrue(any(item["type"] == "turn_finished" and item["data"]["stop"] == "cancelled" for item in events))

    def test_approval_uses_the_shared_approver_and_executes_after_allow(self) -> None:
        controller, provider, registry = self.make_controller(
            [
                tool_response(("external_action", {"value": "kept"})),
                text_response("Action completed"),
            ]
        )
        performed = []
        registry.register(
            ToolDef(
                name="external_action",
                description="A test action that requires approval.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                handler=lambda value: performed.append(value) or "done",
                timeout=2,
            )
        )
        server = self.start_server(controller)

        status, result = self.post_json(server, "api/chat", {"prompt": "perform the test action"})
        self.assertEqual(status, 202, result)
        state, _events = self.wait_for(server, lambda latest, _events: latest.get("pending_approval") is not None)
        approval = state["pending_approval"]
        self.assertEqual(approval["tool"], "external_action")
        status, result = self.post_json(server, "api/approval", {"id": approval["id"], "allow": True})
        self.assertEqual(status, 200, result)
        _state, events = self.wait_for(
            server,
            lambda latest, events: any(item["type"] == "operation_finished" for item in events),
        )
        self.assertEqual(performed, ["kept"])
        self.assertTrue(any(item["type"] == "output" and "Action completed" in item["data"]["text"] for item in events))
        with provider.lock:
            self.assertEqual(len(provider.requests), 2)

    def test_saved_program_schema_is_rendered_and_values_reach_the_managed_runner(self) -> None:
        controller, _provider, _registry = self.make_controller()
        workspace = controller.options_factory.context.workspace
        os.makedirs(os.path.join(self.workspace_dir, "scripts"), exist_ok=True)
        source_path = os.path.join(self.workspace_dir, "scripts", "count.py")
        with open(source_path, "w", encoding="utf-8") as handle:
            handle.write("def main(inputs):\n    print('count=' + str(inputs['count']))\n")
        record = programs.register(
            workspace,
            title="Count input",
            purpose="Print the submitted number.",
            entry_file="scripts/count.py",
            mode="batch",
            input_schema=[
                {"name": "count", "label": "Count", "type": "number", "required": True, "integer": True}
            ],
        )
        server = self.start_server(controller)

        status, _headers, body = self.request(server, "api/programs")
        self.assertEqual(status, 200)
        listed = json.loads(body.decode("utf-8"))["programs"]
        self.assertEqual(listed[0]["id"], record["id"])
        self.assertEqual(listed[0]["input_schema"][0]["type"], "number")

        status, result = self.post_json(server, "api/program/run", {"id": record["id"], "values": {"count": "7"}})
        self.assertEqual(status, 202, result)
        _state, events = self.wait_for(
            server,
            lambda latest, events: any(item["type"] == "operation_finished" for item in events),
        )
        result_events = [item for item in events if item["type"] == "program_result"]
        self.assertEqual(len(result_events), 1)
        self.assertFalse(result_events[0]["data"]["is_error"])
        self.assertIn("count=7", result_events[0]["data"]["content"])

    def test_stop_session_closes_server_and_drains_run_web(self) -> None:
        controller, _provider, _registry = self.make_controller()
        ready = threading.Event()
        result = []
        url_holder = []

        def on_ready(url: str) -> None:
            url_holder.append(url)
            ready.set()

        thread = threading.Thread(
            target=lambda: result.append(run_web(controller, open_browser=False, keep_alive=False, on_ready=on_ready)),
            name="web-lifecycle-test",
        )
        thread.start()
        self.assertTrue(ready.wait(3), "server did not start")
        url = url_holder[0]
        request = urllib.request.Request(
            url + "api/stop",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Pyto-Harness-Token": url.split("/")[-2],
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "run_web did not stop its worker/server")
        self.assertEqual(result, [0])
        with self.assertRaises((OSError, urllib.error.URLError)):
            urllib.request.urlopen(url, timeout=1)

    def test_cli_exposes_web_and_rejects_the_removed_ui_flag(self) -> None:
        self.assertTrue(runner.build_parser().parse_args(["--web"]).web)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            runner.build_parser().parse_args(["--ui"])
        self.assertEqual(raised.exception.code, 2)

    def test_background_task_lifecycle_runs_and_stops_on_a_helper_thread(self) -> None:
        created = []

        class FakeBackgroundTask:
            def __init__(self, *, id: str) -> None:
                self.id = id
                self.started_on = None
                self.stopped_on = None
                self.reminder_notifications = True

            def start(self) -> None:
                self.started_on = threading.current_thread()

            def stop(self) -> None:
                self.stopped_on = threading.current_thread()

        background = types.ModuleType("background")

        def create(*, id: str) -> FakeBackgroundTask:
            task = FakeBackgroundTask(id=id)
            created.append(task)
            return task

        background.BackgroundTask = create  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"background": background}):
            keepalive = _BackgroundKeepalive()
            keepalive.start()
            keepalive.stop()

        self.assertEqual(len(created), 1)
        self.assertIsNot(created[0].started_on, threading.current_thread())
        self.assertIsNot(created[0].stopped_on, created[0].started_on)
        self.assertFalse(keepalive._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
