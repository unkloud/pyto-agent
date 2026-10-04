"""Shared test scaffolding: path setup, environment isolation and temp workspaces."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import types
import unittest
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Never let a test spawn a browser: the capability adapters are exercised for their
# "unsupported" path, and `webbrowser.open` on a headless box can block.
os.environ["PYTO_HARNESS_NO_BROWSER"] = "1"

# Hermetic home for the whole suite.  The resolver *probes* for real, and on a machine
# where $HOME is not writable (a build sandbox, or Pyto itself) the next candidate is the
# current working directory — which would leave a `.pyto_harness` directory inside the
# checkout.  `PYTO_HARNESS_HOME` is the documented escape hatch, so the suite uses it;
# tests that exercise the resolution order pass their own `environ=` mapping.
_HARNESS_HOME = tempfile.mkdtemp(prefix="pyto-harness-home-")
atexit.register(shutil.rmtree, _HARNESS_HOME, True)
os.environ["PYTO_HARNESS_HOME"] = _HARNESS_HOME

from harness import ios, session as session_mod  # noqa: E402
from harness.config import Config, load_config  # noqa: E402
from harness.llm import LLMClient, LLMConfig, RetryPolicy  # noqa: E402
from harness.loop import LoopOptions, make_policy  # noqa: E402
from harness.session import SessionLog  # noqa: E402
from harness.tools import ToolRegistry  # noqa: E402
from harness.tools_ios import build_registry, default_context  # noqa: E402

from .mock_provider import MockProvider  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    """A test case with a private temp directory, cleaned up afterwards."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="pyto-harness-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.workspace_dir = os.path.join(self.tmp, "workspace")

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def make_config(self, **overrides: Any) -> Config:
        values: Dict[str, Any] = {
            "workspace": self.workspace_dir,
            "sessions_dir": self.path("sessions"),
            "spill_dir": self.path("workspace", "tool-output"),
            "api_key": "sk-test-key-not-real",
            "api_base": "http://127.0.0.1:1",
        }
        values.update(overrides)
        return Config(**values)

    def make_registry(self, **overrides: Any) -> Any:
        context = default_context(self.workspace_dir, self.path("workspace", "tool-output"))
        registry = build_registry(context)
        registry.policy = make_policy(yolo=True)
        for key, value in overrides.items():
            setattr(context, key, value)
        registry.context = context  # type: ignore[attr-defined]
        return registry

    def make_session(self, name: str = "session.jsonl") -> SessionLog:
        session = SessionLog.create(self.path(name), workspace=self.workspace_dir)
        # Closed automatically: an unclosed log hides real leaks under -W error.
        self.addCleanup(session.close)
        return session

    def install_bridge(self, name: str) -> Any:
        """Insert a fake Pyto bridge module, removed again at cleanup.

        Linux has no ``pasteboard`` module, so the native path of an adapter can only be
        exercised by injecting one.  That keeps the "works on device" branch tested
        without a device.
        """
        module = types.ModuleType(name)
        module.calls = []  # type: ignore[attr-defined]

        def record(*args: Any, **kwargs: Any) -> None:
            module.calls.append((args, kwargs))  # type: ignore[attr-defined]

        module.record = record  # type: ignore[attr-defined]
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        return module


def make_client(provider: MockProvider, **overrides: Any) -> LLMClient:
    values: Dict[str, Any] = {
        "api_base": provider.api_base,
        "model": "mock-model",
        "api_key": "sk-test",
        "timeout": 10.0,
    }
    values.update(overrides)
    if "retry" not in values:
        values["retry"] = RetryPolicy(max_retries=2, initial_delay=0.01, max_delay=0.05, jitter_ratio=0.0)
    return LLMClient(LLMConfig(**values))


def make_options(
    client: LLMClient,
    registry: ToolRegistry,
    session: SessionLog,
    **overrides: Any,
) -> LoopOptions:
    values: Dict[str, Any] = {
        "client": client,
        "registry": registry,
        "session": session,
        "system_prompt": "You are a test agent.",
        "max_turns": 5,
        "spill_dir": "",
    }
    values.update(overrides)
    return LoopOptions(**values)
