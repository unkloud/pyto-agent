"""Session log tests: append, resume, projection equality, replay determinism, compaction."""

from __future__ import annotations

import json
import os
import unittest

from harness.errors import SessionFormatError
from harness.session import (
    SessionEvent,
    SessionHeader,
    SessionLog,
    assistant_message_event,
    completed_tool_event,
    list_session_summaries,
    project,
    read_log,
    redact,
    tool_message_event,
    user_message_event,
)

from .support import TempDirTestCase


class TestHeader(unittest.TestCase):
    def test_round_trip(self) -> None:
        header = SessionHeader(id="abc", workspace="/tmp/ws", model="m", api_base="https://x")
        restored = SessionHeader.from_wire(header.to_wire())
        self.assertEqual(restored.id, "abc")
        self.assertEqual(restored.workspace, "/tmp/ws")
        self.assertEqual(restored.model, "m")

    def test_missing_version_is_refused(self) -> None:
        with self.assertRaises(SessionFormatError):
            SessionHeader.from_wire({"id": "x"})

    def test_future_version_is_refused(self) -> None:
        with self.assertRaises(SessionFormatError):
            SessionHeader.from_wire({"version": 99, "id": "x"})

    def test_v0_is_migrated(self) -> None:
        header = SessionHeader.from_wire({"version": 0, "id": "old", "cwd": "/legacy"})
        self.assertEqual(header.version, 1)
        self.assertEqual(header.workspace, "/legacy")

    def test_config_is_redacted_on_disk(self) -> None:
        header = SessionHeader(config={"api_key": "sk-secret", "nested": {"token": "t"}})
        wire = header.to_wire()
        self.assertEqual(wire["config"]["api_key"], "<redacted>")
        self.assertEqual(wire["config"]["nested"]["token"], "<redacted>")

    def test_redact_walks_lists(self) -> None:
        self.assertEqual(redact({"items": [{"secret": "x"}]})["items"][0]["secret"], "<redacted>")


class TestAppendAndResume(TempDirTestCase):
    def test_header_is_the_first_row(self) -> None:
        log = self.make_session()
        path = log.path
        log.close()
        with open(path, encoding="utf-8") as handle:
            first = json.loads(handle.readline())
        self.assertEqual(first["kind"], "header")
        self.assertEqual(first["version"], 1)

    def test_seq_is_contiguous(self) -> None:
        log = self.make_session()
        for index in range(5):
            event = log.append("turn.started", {"turn": index})
            self.assertEqual(event.seq, index)
        log.close()

    def test_resume_continues_the_sequence(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("one"))
        log.close()
        resumed = SessionLog.resume(self.path("session.jsonl"))
        self.assertEqual(resumed.append("message.user", user_message_event("two")).seq, 1)
        resumed.close()

    def test_resume_does_not_duplicate_the_header(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("one"))
        log.close()
        resumed = SessionLog.resume(self.path("session.jsonl"))
        resumed.append("message.user", user_message_event("two"))
        resumed.close()
        with open(self.path("session.jsonl"), encoding="utf-8") as handle:
            kinds = [json.loads(line)["kind"] for line in handle if line.strip()]
        self.assertEqual(kinds.count("header"), 1)

    def test_open_or_create_resumes_an_existing_file(self) -> None:
        log = SessionLog.create(self.path("s.jsonl"), workspace=self.workspace_dir)
        log.append("message.user", user_message_event("kept"))
        log.close()
        again = SessionLog.open_or_create(self.path("s.jsonl"))
        self.assertEqual(len(again.events), 1)
        again.close()

    def test_torn_tail_is_dropped_with_a_warning(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("complete"))
        path = log.path
        log.close()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"kind":"event","seq":1,"type":"mess')  # killed mid-write
        resumed = SessionLog.resume(path)
        self.assertEqual(len(resumed.events), 1)
        self.assertTrue(any("torn tail" in warning for warning in resumed.warnings))
        resumed.close()

    def test_missing_header_is_refused(self) -> None:
        path = self.path("bad.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"kind":"event","seq":0,"type":"x","data":{}}\n')
        with self.assertRaises(SessionFormatError):
            read_log(path)

    def test_append_on_a_read_only_log_raises(self) -> None:
        log = self.make_session()
        log.close()
        read_only = SessionLog.resume(log.path, writable=False)
        with self.assertRaises(SessionFormatError):
            read_only.append("message.user", user_message_event("x"))


class TestSessionDiscovery(TempDirTestCase):
    def test_lists_valid_sessions_newest_first_without_exposing_paths(self) -> None:
        sessions_dir = self.path("sessions")
        os.makedirs(sessions_dir)
        records = [
            ("older", 1000, "First request"),
            ("newer", 2000, "Most recent request"),
        ]
        for name, created_at, message in records:
            path = os.path.join(sessions_dir, name + ".jsonl")
            log = SessionLog.create(
                path,
                header=SessionHeader(
                    id=name,
                    created_at=created_at,
                    workspace=self.workspace_dir,
                    model="mock-model",
                ),
            )
            log.append("message.user", user_message_event(message))
            log.close()
            os.utime(path, (created_at / 1000, created_at / 1000))

        with open(os.path.join(sessions_dir, "invalid.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("not a session log\n")

        summaries = list_session_summaries(sessions_dir)
        self.assertEqual([summary.id for summary in summaries], ["newer", "older"])
        self.assertEqual(summaries[0].model, "mock-model")
        self.assertEqual(summaries[0].preview, "Most recent request")
        self.assertNotIn("path", summaries[0].public())
        self.assertNotIn(summaries[0].path, str(summaries[0].public()))

    def test_ignores_duplicate_session_ids(self) -> None:
        sessions_dir = self.path("sessions")
        os.makedirs(sessions_dir)
        for name in ("one", "two"):
            path = os.path.join(sessions_dir, name + ".jsonl")
            SessionLog.create(path, header=SessionHeader(id="duplicate")).close()
        self.assertEqual(list_session_summaries(sessions_dir), [])


class TestProjection(TempDirTestCase):
    def test_projection_shape(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("hello"))
        log.append(
            "message.assistant",
            assistant_message_event(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}
                    ],
                }
            ),
        )
        log.append(
            "message.tool",
            tool_message_event({"role": "tool", "tool_call_id": "c1", "name": "list_files", "content": "[]"}),
        )
        messages = log.project()
        log.close()
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool"])
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "c1")


class TestInterruptedToolRecovery(TempDirTestCase):
    @staticmethod
    def assistant_with_calls(*call_ids_and_names):
        return assistant_message_event(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                    for call_id, name in call_ids_and_names
                ],
            }
        )

    def test_intent_without_start_is_not_replayed_and_reconciliation_is_idempotent(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("do the action"))
        log.append("message.assistant", self.assistant_with_calls(("c1", "write_program")))
        log.append("tool.intent", {"id": "c1", "name": "write_program", "arguments": {"path": "x.py"}})

        recovered = log.reconcile_tool_calls()
        self.assertEqual([row["state"] for row in recovered], ["not_started"])
        projected = log.project()
        self.assertEqual([message["role"] for message in projected], ["user", "assistant", "tool"])
        self.assertIn("before this operation was launched", projected[-1]["content"])
        self.assertEqual(log.events[-1].data["recovery"]["state"], "not_started")
        log.close()

        resumed = SessionLog.resume(log.path)
        self.assertEqual(resumed.reconcile_tool_calls(), [])
        self.assertEqual(resumed.project(), projected)
        resumed.close()

    def test_started_action_without_saved_result_is_reported_as_unknown(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("do the action"))
        log.append("message.assistant", self.assistant_with_calls(("c1", "calendar_add_event")))
        log.append("tool.intent", {"id": "c1", "name": "calendar_add_event"})
        log.append("tool.started", {"id": "c1", "name": "calendar_add_event"})

        recovered = log.reconcile_tool_calls()
        self.assertEqual([row["state"] for row in recovered], ["unknown"])
        self.assertIn("not run again", recovered[0]["message"])
        self.assertIn("Outcome unknown", log.project()[-1]["content"])
        log.close()

    def test_partial_batch_projects_saved_results_and_marks_only_missing_result_unknown(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("run two actions"))
        log.append(
            "message.assistant",
            self.assistant_with_calls(("c1", "write_program"), ("c2", "run_program")),
        )
        log.append("tool.intent", {"id": "c1", "name": "write_program"})
        log.append("tool.started", {"id": "c1", "name": "write_program"})
        log.append(
            "tool.completed",
            completed_tool_event(
                {"role": "tool", "tool_call_id": "c1", "name": "write_program", "content": "saved file"},
                id="c1",
                name="write_program",
                is_error=False,
            ),
        )
        log.append("tool.intent", {"id": "c2", "name": "run_program"})
        log.append("tool.started", {"id": "c2", "name": "run_program"})

        recovered = log.reconcile_tool_calls()
        self.assertEqual([(row["tool"], row["state"]) for row in recovered], [("run_program", "unknown")])
        projected = log.project()
        self.assertEqual([message["role"] for message in projected], ["user", "assistant", "tool", "tool"])
        self.assertEqual([message["tool_call_id"] for message in projected[2:]], ["c1", "c2"])
        self.assertEqual(projected[2]["content"], "saved file")
        self.assertIn("Outcome unknown", projected[3]["content"])
        log.close()

    def test_telemetry_never_projects(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("hi"))
        log.append("turn.started", {"turn": 1})
        log.append("tool.completed", {"name": "x"})
        log.append("delta", {"text": "partial"})
        log.append("usage", {"total_tokens": 5})
        self.assertEqual(len(log.project()), 1)
        log.close()

    def test_orphan_tool_message_is_pruned(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("hi"))
        log.append(
            "message.tool",
            tool_message_event({"role": "tool", "tool_call_id": "ghost", "name": "x", "content": "y"}),
        )
        self.assertEqual([m["role"] for m in log.project()], ["user"])
        log.close()

    def test_projection_is_a_copy(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("hi"))
        first = log.project()
        first[0]["content"] = "mutated"
        self.assertEqual(log.project()[0]["content"], "hi")
        log.close()

    def test_projection_of_a_resumed_log_equals_the_original(self) -> None:
        """The resume requirement: a chat killed and reopened must look identical."""
        log = self.make_session()
        log.append("message.user", user_message_event("first"))
        log.append("message.assistant", assistant_message_event({"role": "assistant", "content": "hi"}))
        before = log.project()
        digest_before = log.replay()
        log.close()
        resumed = SessionLog.resume(log.path)
        self.assertEqual(resumed.project(), before)
        self.assertEqual(resumed.replay(), digest_before)
        resumed.close()

    def test_projection_up_to_a_seq(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("one"))
        log.append("message.user", user_message_event("two"))
        self.assertEqual(len(log.project(up_to=0)), 1)
        log.close()

    def test_projection_is_a_pure_function(self) -> None:
        events = [
            SessionEvent(seq=0, type="message.user", time=1, data=user_message_event("a")),
            SessionEvent(seq=1, type="turn.started", time=2, data={}),
            SessionEvent(seq=2, type="message.assistant", time=3, data=assistant_message_event(
                {"role": "assistant", "content": "b"}
            )),
        ]
        self.assertEqual(project(events), project(events))
        self.assertEqual(len(project(events)), 2)


class TestReplayDeterminism(TempDirTestCase):
    def test_replay_digest_is_stable(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("same"))
        first = log.replay()
        second = log.replay()
        log.close()
        self.assertEqual(first, second)
        self.assertEqual(first["events"], 1)
        self.assertEqual(first["messages"], 1)

    def test_replaying_into_a_new_file_preserves_timestamps_and_digest(self) -> None:
        original = self.make_session()
        original.append("message.user", user_message_event("keep me"), time=1000)
        original.append("message.assistant", assistant_message_event({"role": "assistant", "content": "ok"}), time=2000)
        digest = original.replay()["digest"]
        rows = original.to_jsonl()
        original.close()

        copy = SessionLog.create(self.path("copy.jsonl"), workspace=self.workspace_dir)
        source = SessionLog.resume(self.path("session.jsonl"), writable=False)
        for event in source.events:
            copy.append(event.type, event.data, time=event.time)
        self.assertEqual(copy.replay()["digest"], digest)
        self.assertEqual(copy.to_jsonl().count("\n"), rows.count("\n"))
        copy.close()

    def test_two_logs_with_the_same_events_digest_identically(self) -> None:
        first = SessionLog.create(self.path("a.jsonl"), header=SessionHeader(id="fixed", created_at=1))
        second = SessionLog.create(self.path("b.jsonl"), header=SessionHeader(id="fixed", created_at=1))
        for log in (first, second):
            log.append("message.user", user_message_event("x"), time=5)
        self.assertEqual(first.replay()["digest"], second.replay()["digest"])
        first.close()
        second.close()


class TestCompaction(TempDirTestCase):
    def _fill(self, turns: int = 40) -> SessionLog:
        log = self.make_session()
        for index in range(turns):
            log.append("message.user", user_message_event("ask {}".format(index)))
            log.append(
                "message.assistant",
                assistant_message_event(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c{}".format(index),
                                "type": "function",
                                "function": {"name": "list_files", "arguments": "{}"},
                            }
                        ],
                    }
                ),
            )
            log.append(
                "message.tool",
                tool_message_event(
                    {"role": "tool", "tool_call_id": "c{}".format(index), "name": "list_files", "content": "x"}
                ),
            )
        return log

    def test_forced_compaction_drops_old_events(self) -> None:
        log = self._fill()
        before = len(log.events)
        summary = log.compact(keep_recent=20, force=True)
        self.assertIsNotNone(summary)
        self.assertLess(len(log.events), before)
        self.assertGreater(summary["dropped_events"], 0)
        log.close()

    def test_compaction_starts_on_a_user_message(self) -> None:
        log = self._fill()
        log.compact(keep_recent=20, force=True)
        self.assertEqual(log.project()[0]["role"], "user")
        log.close()

    def test_compaction_never_orphans_a_tool_result(self) -> None:
        log = self._fill()
        log.compact(keep_recent=17, force=True)  # an awkward cut point inside a turn
        messages = log.project()
        declared = set()
        for message in messages:
            if message["role"] == "assistant":
                for call in message.get("tool_calls") or []:
                    declared.add(call["id"])
            if message["role"] == "tool":
                self.assertIn(message["tool_call_id"], declared, "tool result without its declaration")
        log.close()

    def test_compacted_log_reloads_identically(self) -> None:
        log = self._fill()
        log.compact(keep_recent=20, force=True)
        expected = log.project()
        log.close()
        resumed = SessionLog.resume(self.path("session.jsonl"))
        self.assertEqual(resumed.project(), expected)
        self.assertEqual(resumed.events[0].seq, 0)
        resumed.close()

    def test_compaction_is_a_no_op_below_the_budget(self) -> None:
        log = self.make_session()
        log.append("message.user", user_message_event("small"))
        self.assertIsNone(log.compact())
        log.close()

    def test_compaction_does_not_split_a_turn_between_sessions(self) -> None:
        log = self._fill()
        log.compact(keep_recent=6, force=True)
        self.assertEqual(log.project()[0]["role"], "user")
        log.close()


if __name__ == "__main__":
    unittest.main()
