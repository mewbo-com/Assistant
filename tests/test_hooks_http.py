"""Tests for HTTP hook type (fire-and-forget POST to external URLs).

Every dispatch is joined through ``HOOK_DISPATCH.wait`` rather than slept out.
A sleep here is not merely slow: it asserts on whichever side of the race the
machine happened to land, so on a loaded box the assertion is WRONG, not late.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from mewbo_core.classes import ActionStep
from mewbo_core.config import HookEntry, HooksConfig
from mewbo_core.hooks import HOOK_DISPATCH, HookDispatch, HookManager

# Generous, because it bounds a JOIN and not a poll — a correct run leaves it
# untouched, and only a genuinely stuck dispatch ever spends it.
_JOIN_TIMEOUT_S = 5.0


def _step(tool_id: str = "read_file", operation: str = "get") -> ActionStep:
    return ActionStep(tool_id=tool_id, operation=operation, tool_input={})


def _settle() -> None:
    """Block until every dispatched hook has landed."""
    assert HOOK_DISPATCH.wait(timeout=_JOIN_TIMEOUT_S), "a hook dispatch never landed"


class TestHttpHookFactory:
    """Test _make_http_hook and related factories."""

    def test_pre_tool_http_hook_posts_json(self) -> None:
        config = HooksConfig(pre_tool_use=[HookEntry(type="http", url="http://example.com/hook")])
        manager = HookManager.load_from_config(config)
        assert len(manager.pre_tool_use) == 1

        with patch("mewbo_core.hooks._post_json") as mock_post:
            step = _step()
            result = manager.run_pre_tool_use(step)
            assert result is step
            _settle()
            mock_post.assert_called_once()
            args = mock_post.call_args[0]
            assert args[0] == "http://example.com/hook"
            payload = args[1]
            assert payload["event"] == "pre_tool_use"
            assert payload["tool_id"] == "read_file"
            assert payload["operation"] == "get"

    def test_pre_tool_http_hook_respects_matcher(self) -> None:
        config = HooksConfig(
            pre_tool_use=[HookEntry(type="http", url="http://example.com/hook", matcher="shell_*")]
        )
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_pre_tool_use(_step("read_file"))
            _settle()
            mock_post.assert_not_called()

            manager.run_pre_tool_use(_step("shell_exec"))
            _settle()
            mock_post.assert_called_once()

    def test_post_tool_http_hook(self) -> None:
        config = HooksConfig(post_tool_use=[HookEntry(type="http", url="http://example.com/post")])
        manager = HookManager.load_from_config(config)

        mock_result = MagicMock()
        mock_result.content = "some output"

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_post_tool_use(_step(), mock_result)
            _settle()
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert payload["event"] == "post_tool_use"
            assert payload["result_preview"] == "some output"

    def test_session_start_http_hook(self) -> None:
        config = HooksConfig(
            on_session_start=[HookEntry(type="http", url="http://example.com/start")]
        )
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_on_session_start("sess-123")
            _settle()
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert payload["event"] == "session_start"
            assert payload["session_id"] == "sess-123"

    def test_session_end_http_hook_with_error(self) -> None:
        config = HooksConfig(on_session_end=[HookEntry(type="http", url="http://example.com/end")])
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_on_session_end("sess-456", error="timeout")
            _settle()
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert payload["event"] == "session_end"
            assert payload["session_id"] == "sess-456"
            assert payload["error"] == "timeout"

    def test_dispatch_is_joinable_while_the_post_is_still_running(self) -> None:
        """The seam a caller waits on is the dispatch, not a guess about timing.

        The stubbed POST blocks on a gate held by this test, so "has it landed"
        has a definite answer at each step regardless of how fast the machine
        is: no, then yes. Without a joinable dispatch the only available
        question is "has enough wall-clock passed", which is a different
        question with a different answer under load.
        """
        gate = threading.Event()
        landed: list[str] = []

        def blocked_post(url: str, payload: dict, headers: dict, timeout: int) -> None:
            assert gate.wait(_JOIN_TIMEOUT_S), "the gate was never released"
            landed.append(url)

        config = HooksConfig(
            on_session_start=[HookEntry(type="http", url="http://example.com/slow")]
        )
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json", blocked_post):
            manager.run_on_session_start("sess-slow")
            # Dispatched, demonstrably not done — and the join says so rather
            # than a sleep asserting whichever side of the race it landed on.
            assert landed == []
            assert HOOK_DISPATCH.wait(timeout=0.2) is False
            gate.set()
            _settle()
            assert landed == ["http://example.com/slow"]

    def test_a_finished_dispatch_stops_being_tracked(self) -> None:
        """The set holds what is in flight and nothing else.

        This is the property the ``O(1)`` submit rests on: nothing sweeps the
        set, so if a finished thread did not remove itself the set would grow
        with every hook the process ever dispatched. Asserted structurally
        rather than by timing — a wall-clock assertion on a shared machine
        proves nothing either way.
        """
        gate = threading.Event()
        dispatch = HookDispatch()

        def blocked() -> None:
            assert gate.wait(_JOIN_TIMEOUT_S), "the gate was never released"

        for _ in range(3):
            dispatch.submit(blocked)
        # Reaching into the private set on purpose: the tracking IS the subject
        # here, and a public accessor with one caller would be surface built
        # for this test alone.
        assert len(dispatch._threads) == 3
        gate.set()
        assert dispatch.wait(timeout=_JOIN_TIMEOUT_S)
        assert dispatch._threads == set()

    def test_mixed_command_and_http_hooks(self) -> None:
        config = HooksConfig(
            pre_tool_use=[
                HookEntry(type="command", command="echo hello"),
                HookEntry(type="http", url="http://example.com/hook"),
            ]
        )
        manager = HookManager.load_from_config(config)
        assert len(manager.pre_tool_use) == 2


class TestSessionEnvEnrichment:
    """Test that session hooks pass env vars."""

    def test_session_start_passes_session_id(self) -> None:
        config = HooksConfig(
            on_session_start=[HookEntry(type="command", command="echo $MEWBO_SESSION_ID")]
        )
        manager = HookManager.load_from_config(config)

        with patch("subprocess.run") as mock_run:
            manager.run_on_session_start("test-session-id")
            mock_run.assert_called_once()
            env = mock_run.call_args[1]["env"]
            assert env["MEWBO_SESSION_ID"] == "test-session-id"

    def test_session_end_passes_session_id_and_error(self) -> None:
        config = HooksConfig(on_session_end=[HookEntry(type="command", command="echo test")])
        manager = HookManager.load_from_config(config)

        with patch("subprocess.run") as mock_run:
            manager.run_on_session_end("sess-789", error="failed")
            mock_run.assert_called_once()
            env = mock_run.call_args[1]["env"]
            assert env["MEWBO_SESSION_ID"] == "sess-789"
            assert env["MEWBO_ERROR"] == "failed"
