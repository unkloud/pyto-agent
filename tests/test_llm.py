"""LLM client tests: SSE framing, chunk reassembly, retries, fallback, cancellation.

Every test runs against :class:`tests.mock_provider.MockProvider` on 127.0.0.1, so the
suite is fully offline and no test depends on an external service.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
import time
import unittest

from harness.errors import (
    AuthError,
    BadRequestError,
    CancelledError,
    MalformedResponseError,
    RateLimitedError,
    TransportError,
)
from harness.llm import LLMClient, LLMConfig, RetryPolicy, SseParser, AssistantStream, ToolCall

from .mock_provider import MockProvider, error_response, text_response, tool_response
from .support import make_client


class TestSseParser(unittest.TestCase):
    def test_single_frame(self) -> None:
        parser = SseParser()
        events = parser.feed(b'data: {"a": 1}\n\n')
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].data), {"a": 1})

    def test_frame_split_across_chunks(self) -> None:
        parser = SseParser()
        self.assertEqual(parser.feed(b'data: {"a"'), [])
        self.assertEqual(parser.feed(b": 1}"), [])
        events = parser.feed(b"\n\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].data), {"a": 1})

    def test_crlf_framing(self) -> None:
        parser = SseParser()
        events = parser.feed(b'data: {"a": 1}\r\n\r\n')
        self.assertEqual(len(events), 1)

    def test_split_crlf_is_not_two_lines(self) -> None:
        parser = SseParser()
        self.assertEqual(parser.feed(b'data: {"a": 1}\r'), [])
        events = parser.feed(b"\n\r\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].data), {"a": 1})

    def test_multiple_data_lines_are_joined(self) -> None:
        parser = SseParser()
        events = parser.feed(b"data: one\ndata: two\n\n")
        self.assertEqual(events[0].data, "one\ntwo")

    def test_comments_ignored(self) -> None:
        parser = SseParser()
        events = parser.feed(b": keep-alive\n\ndata: x\n\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(parser.comments, 1)

    def test_unterminated_tail_is_not_an_event(self) -> None:
        parser = SseParser()
        self.assertEqual(parser.feed(b"data: incomplete"), [])
        parser.finish()
        self.assertEqual(parser.feed(b""), [])

    def test_multibyte_split_across_chunks(self) -> None:
        parser = SseParser()
        payload = 'data: {"t": "caf\u00e9"}\n\n'.encode("utf-8")
        cut = payload.index(b"\xc3") + 1
        self.assertEqual(parser.feed(payload[:cut]), [])
        events = parser.feed(payload[cut:])
        self.assertEqual(json.loads(events[0].data)["t"], "caf\u00e9")


class TestStreamAssembly(unittest.TestCase):
    def test_text_stream_and_usage(self) -> None:
        with MockProvider([text_response("hello world")]) as provider:
            client = make_client(provider)
            deltas = []
            stream = client.stream_sync(
                [{"role": "user", "content": "hi"}],
                on_delta=lambda kind, text: deltas.append((kind, text)),
            )
            client.close()
        self.assertEqual(stream.text, "hello world")
        self.assertEqual(stream.finish_reason, "stop")
        self.assertEqual("".join(t for _, t in deltas), "hello world")
        self.assertEqual(stream.usage.total_tokens, 18)
        self.assertEqual(stream.usage.prompt_tokens, 11)

    def test_tool_call_arguments_reassembled_across_chunks(self) -> None:
        arguments = {"path": "a.py", "source": "print(1)\n" * 40, "purpose": "demo"}
        with MockProvider([tool_response(("write_program", arguments))]) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertEqual(len(stream.tool_calls), 1)
        call = stream.tool_calls[0]
        self.assertEqual(call.name, "write_program")
        self.assertEqual(call.arguments(), arguments)
        self.assertGreater(stream.chunk_count, 3, "arguments should have arrived in several chunks")
        self.assertEqual(stream.finish_reason, "tool_calls")

    def test_multiple_tool_calls_keep_indexes(self) -> None:
        with MockProvider(
            [tool_response(("list_files", {"pattern": "*.py"}), ("memory_read", {}))]
        ) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertEqual([c.name for c in stream.tool_calls], ["list_files", "memory_read"])
        self.assertEqual(stream.tool_calls[0].arguments(), {"pattern": "*.py"})
        self.assertEqual(stream.tool_calls[1].arguments(), {})

    def test_run_name_is_stored(self) -> None:
        with MockProvider([text_response("x")]) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertEqual(stream.model, "mock-model")

    def test_request_shape_streaming(self) -> None:
        with MockProvider([text_response("x")]) as provider:
            client = make_client(provider)
            client.stream_sync(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}}],
            )
            client.close()
            bodies = provider.request_bodies()
        self.assertEqual(bodies[0]["stream"], True)
        self.assertEqual(bodies[0]["model"], "mock-model")
        self.assertEqual(bodies[0]["stream_options"], {"include_usage": True})
        self.assertEqual(bodies[0]["tool_choice"], "auto")
        self.assertEqual(len(bodies[0]["tools"]), 1)

    def test_authorization_header_sent_and_not_leaked_in_preview(self) -> None:
        with MockProvider([text_response("x")]) as provider:
            client = make_client(provider)
            preview = client.request_preview([{"role": "user", "content": "hi"}])
            client.stream_sync([{"role": "user", "content": "hi"}])
            client.close()
            headers = provider.requests[0]["headers"]
        self.assertEqual(headers["authorization"], "Bearer sk-test")
        self.assertEqual(preview["headers"]["authorization"], "<redacted>")

    def test_tool_call_to_message_round_trip(self) -> None:
        with MockProvider([tool_response(("t", {"a": 1}))]) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        message = stream.to_message()
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "t")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"a": 1})

    def test_nameless_tool_call_is_rejected(self) -> None:
        stream = AssistantStream()
        stream.tool_calls.append(ToolCall(id="x", name="", arguments_raw="{}"))
        with self.assertRaises(MalformedResponseError):
            stream.to_message()

    def test_malformed_arguments_raise_on_access(self) -> None:
        call = ToolCall(id="x", name="t", arguments_raw='{"a": ')
        with self.assertRaises(MalformedResponseError):
            call.arguments()

    def test_empty_arguments_are_an_empty_dict(self) -> None:
        self.assertEqual(ToolCall(id="x", name="t", arguments_raw="").arguments(), {})


class TestNonStreamingFallback(unittest.TestCase):
    def test_stream_false_uses_the_json_path(self) -> None:
        payload = {
            "id": "chatcmpl-1",
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "non-streamed answer",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "list_files", "arguments": '{"pattern": "*.md"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
        with MockProvider([{"json": payload}]) as provider:
            client = make_client(provider, stream=False)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            sent = provider.request_bodies()[0]
        self.assertEqual(stream.text, "non-streamed answer")
        self.assertEqual(stream.tool_calls[0].name, "list_files")
        self.assertEqual(stream.tool_calls[0].arguments(), {"pattern": "*.md"})
        self.assertEqual(stream.usage.total_tokens, 7)
        self.assertEqual(sent["stream"], False)
        self.assertNotIn("stream_options", sent)

    def test_stream_false_accepts_non_sse_content_type(self) -> None:
        with MockProvider([{"json": {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}}]) as provider:
            client = make_client(provider, stream=False)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertEqual(stream.text, "hi")

    def test_non_streaming_provider_error_is_raised(self) -> None:
        with MockProvider([{"json": {"error": {"message": "no such model"}}}]) as provider:
            client = make_client(provider, stream=False)
            with self.assertRaises(MalformedResponseError) as caught:
                client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertIn("no such model", str(caught.exception))


class TestRetries(unittest.TestCase):
    def test_429_then_success(self) -> None:
        script = [error_response(429, "slow down", **{"retry-after": "0"}), text_response("recovered")]
        with MockProvider(script) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            attempts = len(provider.requests)
        self.assertEqual(stream.text, "recovered")
        self.assertEqual(attempts, 2, "the first 429 should be retried exactly once")

    def test_500_then_success(self) -> None:
        script = [error_response(500), error_response(503), text_response("third time lucky")]
        with MockProvider(script) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertEqual(stream.text, "third time lucky")

    def test_retry_exhaustion_raises_rate_limited(self) -> None:
        with MockProvider([error_response(429, "nope")]) as provider:
            client = make_client(provider, retry=RetryPolicy(max_retries=1, initial_delay=0.01, jitter_ratio=0.0))
            with self.assertRaises(RateLimitedError):
                client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            self.assertEqual(len(provider.requests), 2, "1 initial + 1 retry")

    def test_401_is_not_retried(self) -> None:
        with MockProvider([error_response(401, "bad key")]) as provider:
            client = make_client(provider)
            with self.assertRaises(AuthError):
                client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            self.assertEqual(len(provider.requests), 1)

    def test_400_is_not_retried(self) -> None:
        with MockProvider([error_response(400, "bad request")]) as provider:
            client = make_client(provider)
            with self.assertRaises(BadRequestError):
                client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            self.assertEqual(len(provider.requests), 1)

    def test_backoff_delay_is_exponential_and_jittered(self) -> None:
        policy = RetryPolicy(initial_delay=1.0, max_delay=8.0, jitter_ratio=0.0)
        self.assertEqual(policy.delay_for(0), 1.0)
        self.assertEqual(policy.delay_for(1), 2.0)
        self.assertEqual(policy.delay_for(2), 4.0)
        self.assertEqual(policy.delay_for(9), 8.0, "must clamp at max_delay")
        jittered = RetryPolicy(initial_delay=1.0, jitter_ratio=0.5)
        values = {round(jittered.delay_for(0, rand=lambda: r), 3) for r in (0.0, 0.5, 1.0)}
        self.assertEqual(values, {0.5, 1.0, 1.5})

    def test_retry_after_header_is_honoured(self) -> None:
        policy = RetryPolicy(initial_delay=99.0, max_delay=8.0)
        self.assertEqual(policy.delay_for_error(RateLimitedError("x", retry_after=0.25), 0), 0.25)

    def test_no_retry_after_tokens_were_emitted(self) -> None:
        """A stream that dies mid-flight must not be replayed: the user saw those tokens."""
        script = [
            {"sse": [{"content": "partial"}], "raw_tail": "data: {not json}\n\n", "usage": False},
            text_response("should never be sent"),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            deltas = []
            with self.assertRaises(MalformedResponseError):
                client.stream_sync(
                    [{"role": "user", "content": "go"}], on_delta=lambda k, t: deltas.append(t)
                )
            client.close()
            self.assertEqual(len(provider.requests), 1, "must not retry after emitting a delta")
        self.assertEqual("".join(deltas), "partial")

    def test_zero_data_frames_is_malformed(self) -> None:
        with MockProvider([{"sse": [], "usage": False, "raw_tail": ": nothing here\n\n"}]) as provider:
            client = make_client(provider)
            with self.assertRaises(MalformedResponseError) as caught:
                client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertIn("no SSE data frames", str(caught.exception))

    def test_connection_failure_is_wrapped_as_transport_error(self) -> None:
        client = LLMClient(LLMConfig(api_base="http://127.0.0.1:9", model="m", timeout=0.5, retry=RetryPolicy(max_retries=0)))
        with self.assertRaises(TransportError):
            client.stream_sync([{"role": "user", "content": "go"}])
        client.close()

    def test_content_free_finish_still_returns_a_stream(self) -> None:
        """finish_reason with no content is legal (e.g. a length-limited reply)."""
        with MockProvider([{"sse": [], "usage": False, "finish": "length"}]) as provider:
            client = make_client(provider)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
        self.assertTrue(stream.is_empty())
        self.assertEqual(stream.finish_reason, "length")


class TestUrlResolution(unittest.TestCase):
    def test_path_is_appended_to_the_base(self) -> None:
        client = LLMClient(LLMConfig(api_base="https://api.deepseek.com"))
        self.assertEqual(client.candidate_urls()[0], "https://api.deepseek.com/chat/completions")
        self.assertIn("https://api.deepseek.com/v1/chat/completions", client.candidate_urls())
        client.close()

    def test_explicit_v1_path_has_no_fallback(self) -> None:
        client = LLMClient(LLMConfig(api_base="https://api.openai.com/v1"))
        self.assertEqual(client.candidate_urls(), ["https://api.openai.com/v1/chat/completions"])
        client.close()

    def test_bad_scheme_raises(self) -> None:
        client = LLMClient(LLMConfig(api_base="ftp://example.com"))
        with self.assertRaises(ValueError):
            client.candidate_urls()
        client.close()

    def test_404_on_first_candidate_falls_through_to_v1(self) -> None:
        with MockProvider([error_response(404), text_response("v1 worked")]) as provider:
            client = make_client(provider, api_base=provider.base_url)
            stream = client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            paths = [record["path"] for record in provider.requests]
        self.assertEqual(stream.text, "v1 worked")
        self.assertEqual(paths, ["/chat/completions", "/v1/chat/completions"])

    def test_v1_base_hits_v1_path_directly(self) -> None:
        with MockProvider([text_response("ok")]) as provider:
            client = make_client(provider, api_base=provider.api_base_v1)
            client.stream_sync([{"role": "user", "content": "go"}])
            client.close()
            paths = [record["path"] for record in provider.requests]
        self.assertEqual(paths, ["/v1/chat/completions"])


class TestAsyncAndCancellation(unittest.TestCase):
    def test_async_stream_returns_the_same_result(self) -> None:
        with MockProvider([text_response("async hello")]) as provider:
            client = make_client(provider)

            async def go() -> str:
                stream = await client.stream([{"role": "user", "content": "hi"}])
                return stream.text

            text = asyncio.run(go())
            client.close()
        self.assertEqual(text, "async hello")

    def test_stream_events_generator_yields_deltas(self) -> None:
        with MockProvider([text_response("one two three")]) as provider:
            client = make_client(provider)
            kinds = [event["kind"] for event in client.stream_events([{"role": "user", "content": "hi"}])]
            client.close()
        self.assertIn("delta", kinds)
        self.assertIn("event", kinds)

    def test_cancellation_stops_promptly(self) -> None:
        """A stalled stream must abort on `cancel()`, not wait for the socket deadline."""
        script = [
            {
                "sse": [{"content": "a"}, {"content": "b"}],
                "hold_open": 20.0,
                "usage": False,
            }
        ]
        with MockProvider(script, repeat_last=False) as provider:
            client = make_client(provider, timeout=30.0)
            stop = threading.Event()
            outcome = {}

            def worker() -> None:
                try:
                    client.stream_sync([{"role": "user", "content": "go"}], stop=stop)
                    outcome["result"] = "completed"
                except BaseException as exc:  # noqa: BLE001
                    outcome["result"] = type(exc).__name__

            thread = threading.Thread(target=worker, daemon=True)
            started = time.monotonic()
            thread.start()
            time.sleep(0.4)
            client.cancel()
            thread.join(timeout=5)
            elapsed = time.monotonic() - started

        self.assertFalse(thread.is_alive(), "the worker must not stay parked")
        self.assertLess(elapsed, 3.0, "cancellation should not wait for the 30s read timeout")
        self.assertEqual(outcome.get("result"), "CancelledError")
        client.close()

    def test_stop_event_already_set_short_circuits(self) -> None:
        with MockProvider([text_response("never")]) as provider:
            client = make_client(provider)
            stop = threading.Event()
            stop.set()
            with self.assertRaises(CancelledError):
                client.stream_sync([{"role": "user", "content": "go"}], stop=stop)
            client.close()
            self.assertEqual(len(provider.requests), 0)


if __name__ == "__main__":
    unittest.main()
