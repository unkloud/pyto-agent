"""Tool registry tests: validation, approval, timeouts, and real parallel overlap."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

from harness.errors import ToolError
from harness.tools import Decision, ToolDef, ToolRegistry, ToolResult, build_policy

from .support import TempDirTestCase


def run(coro):
    return asyncio.run(coro)


class TestRegistration(unittest.TestCase):
    def test_decorator_registers_and_builds_a_schema(self) -> None:
        registry = ToolRegistry()

        @registry.tool("echo", "Echo text back.", {"type": "object", "properties": {"t": {"type": "string"}}})
        def echo(t: str) -> ToolResult:
            return ToolResult.ok(t)

        self.assertIn("echo", registry)
        self.assertEqual(len(registry), 1)
        schema = registry.definitions()[0]
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "echo")
        self.assertEqual(schema["function"]["parameters"]["properties"]["t"]["type"], "string")

    def test_duplicate_registration_is_refused(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolDef(name="a", description="d", parameters={}, handler=lambda: None))
        with self.assertRaises(ValueError):
            registry.register(ToolDef(name="a", description="d", parameters={}, handler=lambda: None))

    def test_empty_description_is_refused(self) -> None:
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register(ToolDef(name="a", description="", parameters={}, handler=lambda: None))

    def test_definitions_can_be_filtered(self) -> None:
        registry = ToolRegistry()
        for name in ("a", "b", "c"):
            registry.register(ToolDef(name=name, description="d", parameters={}, handler=lambda: None))
        self.assertEqual([t["function"]["name"] for t in registry.definitions(["a", "c"])], ["a", "c"])


class TestInvoke(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()

    def test_success_round_trip(self) -> None:
        @self.registry.tool("add", "Add two integers.", {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        })
        def add(a: int, b: int) -> ToolResult:
            return ToolResult.ok(str(a + b))

        result = run(self.registry.invoke("add", {"a": 1, "b": 2}))
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "3")
        self.assertGreaterEqual(result.duration_ms, 0.0)

    def test_unknown_tool_is_a_result_not_an_exception(self) -> None:
        result = run(self.registry.invoke("nope", {}))
        self.assertTrue(result.is_error)
        self.assertIn("unknown tool", result.content)

    def test_validation_failure_lists_the_expected_schema(self) -> None:
        @self.registry.tool("f", "d", {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        })
        def f(n: int) -> str:
            return str(n)

        result = run(self.registry.invoke("f", {"n": "abc"}))
        self.assertTrue(result.is_error)
        self.assertIn("Expected arguments", result.content)
        self.assertTrue(result.metadata.get("validation"))

    def test_bool_is_rejected_where_an_integer_is_required(self) -> None:
        @self.registry.tool("f", "d", {"type": "object", "properties": {"n": {"type": "integer"}}})
        def f(n: int) -> str:
            return str(n)

        result = run(self.registry.invoke("f", {"n": True}))
        self.assertTrue(result.is_error)
        self.assertIn("expected integer", result.content)

    def test_handler_exception_becomes_a_tool_error(self) -> None:
        @self.registry.tool("boom", "Always fails.", {"type": "object", "properties": {}})
        def boom() -> str:
            raise RuntimeError("kaboom")

        result = run(self.registry.invoke("boom", {}))
        self.assertTrue(result.is_error)
        self.assertIn("kaboom", result.content)
        self.assertTrue(result.metadata.get("handler_crash"))

    def test_plain_string_return_is_wrapped(self) -> None:
        @self.registry.tool("s", "d", {"type": "object", "properties": {}})
        def s() -> str:
            return "just text"

        self.assertEqual(run(self.registry.invoke("s", {})).content, "just text")

    def test_tool_error_is_reported_with_its_code(self) -> None:
        @self.registry.tool("te", "d", {"type": "object", "properties": {}})
        def te() -> str:
            raise ToolError("something specific")

        result = run(self.registry.invoke("te", {}))
        self.assertTrue(result.is_error)
        self.assertIn("TOOL_ERROR", result.content)


class TestApproval(unittest.TestCase):
    def _registry(self, policy) -> ToolRegistry:
        registry = ToolRegistry(policy=policy)

        @registry.tool("read_thing", "d", {"type": "object", "properties": {}})
        def read_thing() -> str:
            return "read"

        return registry

    def test_allow(self) -> None:
        registry = self._registry(lambda name, args: True)
        self.assertFalse(run(registry.invoke("read_thing", {})).is_error)
        self.assertEqual(registry.approvals, [("read_thing", True, "")])

    def test_deny_blocks_the_handler(self) -> None:
        calls = []
        registry = ToolRegistry(policy=lambda name, args: Decision.deny("not on a weekday"))

        @registry.tool("act", "d", {"type": "object", "properties": {}})
        def act() -> str:
            calls.append(1)
            return "done"

        result = run(registry.invoke("act", {}))
        self.assertTrue(result.is_error)
        self.assertIn("denied by policy", result.content)
        self.assertIn("not on a weekday", result.content)
        self.assertEqual(result.metadata["policy_code"], "APPROVAL_DENIED")
        self.assertEqual(calls, [], "a denied tool must not run")

    def test_policy_exception_fails_closed(self) -> None:
        def broken(name, args):
            raise RuntimeError("policy is broken")

        registry = self._registry(broken)
        result = run(registry.invoke("read_thing", {}))
        self.assertTrue(result.is_error)
        self.assertIn("denied", result.content)

    def test_approval_happens_after_validation(self) -> None:
        seen = []
        registry = ToolRegistry(policy=lambda name, args: seen.append(dict(args)) or True)

        @registry.tool("n", "d", {"type": "object", "properties": {"k": {"type": "integer"}}})
        def n(k: int) -> str:
            return str(k)

        run(registry.invoke("n", {"k": "not an int"}))
        self.assertEqual(seen, [], "invalid arguments should never reach the policy")

    def test_build_policy_denies_unknown_tools(self) -> None:
        policy = build_policy(allow=["safe"], deny=["bad"])
        self.assertTrue(policy("safe", {}).allowed)
        self.assertFalse(policy("bad", {}).allowed)
        self.assertFalse(policy("mystery", {}).allowed, "an unknown tool must not default to allowed")

    def test_build_policy_uses_the_prompter(self) -> None:
        asked = []

        def prompter(name, args, danger):
            asked.append(name)
            return True

        policy = build_policy(prompter=prompter)
        self.assertTrue(policy("share_text", {"text": "hi"}).allowed)
        self.assertEqual(asked, ["share_text"])


class TestTimeout(unittest.TestCase):
    def test_slow_tool_times_out(self) -> None:
        registry = ToolRegistry()

        @registry.tool("slow", "d", {"type": "object", "properties": {}}, timeout=0.2)
        def slow() -> str:
            time.sleep(2.0)
            return "finished"

        started = time.monotonic()
        result = run(registry.invoke("slow", {}))
        elapsed = time.monotonic() - started
        self.assertTrue(result.is_error)
        self.assertTrue(result.metadata.get("timeout"))
        self.assertIn("timed out", result.content)
        self.assertLess(elapsed, 1.5, "the timeout must fire near its deadline")

    def test_fast_tool_is_unaffected(self) -> None:
        registry = ToolRegistry()

        @registry.tool("quick", "d", {"type": "object", "properties": {}}, timeout=5.0)
        def quick() -> str:
            return "fast"

        self.assertFalse(run(registry.invoke("quick", {})).is_error)

    def test_timeout_none_disables_the_bound(self) -> None:
        registry = ToolRegistry()

        @registry.tool("patient", "d", {"type": "object", "properties": {}}, timeout=None)
        def patient() -> str:
            time.sleep(0.05)
            return "ok"

        self.assertFalse(run(registry.invoke("patient", {})).is_error)


class TestConcurrency(TempDirTestCase):
    """Regression tests for the bug the reference spike found: `parallel` was serial."""

    def test_parallel_tools_actually_overlap(self) -> None:
        registry = ToolRegistry()
        peak = {"value": 0}
        active = {"value": 0}
        lock = threading.Lock()

        @registry.tool("wait", "Sleep, then return.", {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        })
        def wait(seconds: float) -> str:
            with lock:
                active["value"] += 1
                peak["value"] = max(peak["value"], active["value"])
            time.sleep(seconds)
            with lock:
                active["value"] -= 1
            return "slept {}".format(seconds)

        calls = [("c{}".format(i), "wait", {"seconds": 0.3}) for i in range(4)]
        started = time.monotonic()
        results = run(registry.invoke_batch(calls, max_parallel=4))
        elapsed = time.monotonic() - started

        self.assertEqual(len(results), 4)
        self.assertTrue(all(not result.is_error for _, result in results))
        self.assertGreaterEqual(peak["value"], 2, "sync handlers must run off the event loop")
        self.assertLess(elapsed, 4 * 0.3 * 0.8, "4 x 0.3s of work must not take 1.2s")

    def test_resource_reads_can_overlap(self) -> None:
        registry = ToolRegistry()
        rendezvous = threading.Barrier(2)

        for name in ("read_a", "read_b"):
            @registry.tool(
                name,
                "Read one workspace resource.",
                {"type": "object", "properties": {}},
                resource_reads=("workspace",),
            )
            def read() -> str:
                rendezvous.wait(timeout=1.0)
                return "read"

        results = run(
            registry.invoke_batch([("a", "read_a", {}), ("b", "read_b", {})], max_parallel=2)
        )
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not result.is_error for _, result in results))

    def test_resource_write_precedes_a_later_read(self) -> None:
        registry = ToolRegistry()
        state = {"ready": False}

        @registry.tool(
            "write",
            "Prepare a workspace resource.",
            {"type": "object", "properties": {}},
            resource_writes=("workspace",),
        )
        def write() -> str:
            time.sleep(0.05)
            state["ready"] = True
            return "written"

        @registry.tool(
            "read",
            "Read the workspace resource.",
            {"type": "object", "properties": {}},
            resource_reads=("workspace",),
        )
        def read() -> str:
            return "ready" if state["ready"] else "not ready"

        results = run(registry.invoke_batch([("w", "write", {}), ("r", "read", {})], max_parallel=2))
        self.assertEqual([result.content for _, result in results], ["written", "ready"])

    def test_batch_preserves_model_order(self) -> None:
        registry = ToolRegistry()

        @registry.tool("echo", "d", {"type": "object", "properties": {"t": {"type": "string"}}})
        def echo(t: str) -> str:
            time.sleep(0.05 if t == "first" else 0.0)
            return t

        results = run(
            registry.invoke_batch([("1", "echo", {"t": "first"}), ("2", "echo", {"t": "second"})])
        )
        self.assertEqual([call_id for call_id, _ in results], ["1", "2"])
        self.assertEqual([result.content for _, result in results], ["first", "second"])

    def test_max_parallel_is_respected(self) -> None:
        registry = ToolRegistry()
        active = {"value": 0}
        peak = {"value": 0}
        lock = threading.Lock()

        @registry.tool("wait", "d", {"type": "object", "properties": {}})
        def wait() -> str:
            with lock:
                active["value"] += 1
                peak["value"] = max(peak["value"], active["value"])
            time.sleep(0.1)
            with lock:
                active["value"] -= 1
            return "ok"

        run(registry.invoke_batch([("c{}".format(i), "wait", {}) for i in range(6)], max_parallel=2))
        self.assertLessEqual(peak["value"], 2)

    def test_a_failing_call_does_not_sink_the_batch(self) -> None:
        registry = ToolRegistry()

        @registry.tool("boom", "d", {"type": "object", "properties": {}})
        def boom() -> str:
            raise RuntimeError("nope")

        @registry.tool("fine", "d", {"type": "object", "properties": {}})
        def fine() -> str:
            return "ok"

        results = dict(run(registry.invoke_batch([("1", "boom", {}), ("2", "fine", {})])))
        self.assertTrue(results["1"].is_error)
        self.assertFalse(results["2"].is_error)

    def test_inline_tools_run_on_the_loop(self) -> None:
        registry = ToolRegistry()

        @registry.tool("instant", "d", {"type": "object", "properties": {}}, inline=True)
        def instant() -> str:
            return "inline"

        self.assertFalse(run(registry.invoke("instant", {})).is_error)


if __name__ == "__main__":
    unittest.main()
