"""Tests for the shell tool integration.

Every case drives the tool's PUBLIC surface with a real command. The previous
version patched a module-private ``_run_command``, which made that private name
an implicit contract — renaming it silently turned two of these tests into
no-ops rather than failing them.
"""

from __future__ import annotations

import time

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.config import reset_config, set_config_override
from mewbo_tools.integration.aider_shell_tool import AiderShellTool


@pytest.fixture(autouse=True)
def _unpinned_path_scope():
    """Pin the path-scope axis OFF: these exercise SHELL-TOOL semantics.

    What this module is about is the plain envelope, exit codes, merged stderr,
    a closed stdin, the pager-safe env and process-group reaping — against an
    explicit caller-supplied ``root``, which every case here spells as a bare
    ``tmp_path``. ``path_scope_to_active_project`` ships ON and refuses a
    ``root`` argument that widens beyond the session's scope, so leaving it at
    its default would fail all of these for a reason none of them is testing.
    That axis has its own coverage in ``tests/test_path_guard_scope_parity.py``.

    It also keeps ``test_shell_tool_blocks_escape`` HONEST. With the root
    dropped, ``cwd="../"`` is refused because no root was admitted at all — the
    assertion passes without the escape check ever running.

    Resets afterwards: ``set_config_override`` is process-global and nothing in
    ``conftest.py`` clears it, so an unreset override leaks into every later
    module.
    """
    set_config_override({"agent": {"path_scope_to_active_project": False}})
    yield
    reset_config()


def run_shell(**tool_input: object) -> object:
    """Invoke the shell tool once and return whatever it put on the wire."""
    step = ActionStep(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input=tool_input,
    )
    return AiderShellTool().set_state(step).content


def test_shell_tool_runs_command(tmp_path):
    """A foreground command returns its output in the plain envelope."""
    payload = run_shell(command="echo hello", root=str(tmp_path))
    assert isinstance(payload, dict)
    assert payload.get("kind") == "shell"
    assert payload.get("exit_code") == 0
    assert payload.get("stdout") == "hello\n"


def test_shell_tool_reports_a_failing_exit_code(tmp_path):
    """A non-zero exit is reported as itself, never flattened to 1."""
    payload = run_shell(command="exit 7", root=str(tmp_path))
    assert isinstance(payload, dict)
    assert payload.get("exit_code") == 7


def test_shell_tool_merges_stderr_into_stdout(tmp_path):
    """stderr reaches the model; the envelope has always carried it in stdout."""
    payload = run_shell(command="echo oops >&2", root=str(tmp_path))
    assert isinstance(payload, dict)
    assert "oops" in str(payload.get("stdout"))


def test_shell_tool_resolves_cwd(tmp_path):
    """Resolve cwd within the project root, and actually run there."""
    (tmp_path / "subdir").mkdir()
    payload = run_shell(command="pwd", root=str(tmp_path), cwd="subdir")
    assert isinstance(payload, dict)
    assert payload.get("cwd") == str(tmp_path / "subdir")
    assert str(tmp_path / "subdir") in str(payload.get("stdout"))


def test_shell_tool_blocks_escape(tmp_path):
    """Reject cwd that escapes the root."""
    content = run_shell(command="pwd", root=str(tmp_path), cwd="../")
    assert isinstance(content, str)
    assert "resolves outside all allowed project roots" in content


def test_shell_tool_requires_command(tmp_path):
    """Reject missing command input."""
    content = run_shell(command="", root=str(tmp_path))
    assert isinstance(content, str)
    assert "command is required" in content


def test_shell_tool_rejects_unknown_argument(tmp_path):
    """An unknown field is refused at the boundary, never silently ignored."""
    content = run_shell(command="echo hi", root=str(tmp_path), nonsense=True)
    assert isinstance(content, str)
    assert "Invalid shell arguments" in content


def test_shell_tool_rejects_invalid_payload_type():
    """Reject invalid tool input types."""
    step = ActionStep.model_construct(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input=123,
    )
    content = AiderShellTool().set_state(step).content
    assert isinstance(content, str)
    assert "Tool input must be a string command" in content


def test_shell_tool_accepts_a_bare_string_command():
    """Several models send a bare string; it still means "run this"."""
    step = ActionStep(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input="echo bare",
    )
    payload = AiderShellTool().set_state(step).content
    assert isinstance(payload, dict)
    assert payload.get("stdout") == "bare\n"


def test_shell_tool_nonexistent_cwd_returns_an_error_not_an_exception(tmp_path):
    """A bad working directory is a readable result, never a raised OSError."""
    payload = run_shell(
        command="echo hello",
        root=str(tmp_path),
        cwd=str(tmp_path / "does_not_exist"),
    )
    # The path guard rejects a non-existent child before the spawn is attempted;
    # either way the model gets prose it can act on rather than a traceback.
    if isinstance(payload, dict):
        assert payload.get("exit_code") != 0
    else:
        assert isinstance(payload, str) and payload


def test_shell_tool_closes_stdin(tmp_path):
    """A command that reads stdin (e.g. a pager) never blocks — stdin is closed."""
    payload = run_shell(command="cat", root=str(tmp_path))
    assert isinstance(payload, dict)
    assert payload.get("exit_code") == 0
    assert payload.get("stdout") == ""


def test_shell_tool_pager_safe_env(tmp_path):
    """GIT_PAGER (and friends) are forced to non-interactive values."""
    payload = run_shell(command='printf "%s" "$GIT_PAGER"', root=str(tmp_path))
    assert isinstance(payload, dict)
    assert payload.get("stdout") == "cat"


def test_shell_tool_timeout_reaps_process_group(tmp_path):
    """A hung command is killed on timeout, and its process group dies with it."""
    marker = tmp_path / "reached.txt"
    started = time.monotonic()
    payload = run_shell(
        command=f"sleep 60 && touch {marker}",
        root=str(tmp_path),
        timeout=0.5,
    )
    elapsed = time.monotonic() - started

    assert isinstance(payload, dict)
    assert payload.get("exit_code") == 1
    assert "timed out" in str(payload.get("stdout"))
    assert elapsed < 10  # returned promptly, not after the full sleep
    # Give a killed-but-not-yet-reaped descendant a moment, then confirm it
    # never ran to completion (would prove it survived as an orphan).
    time.sleep(1)
    assert not marker.exists()


def test_default_timeout_under_core_tool_timeout():
    """The tool's own timeout stays under ToolUseLoop's default tool timeout.

    If this ever inverted, the outer asyncio.wait_for would cancel the awaiting
    task before the subprocess had a chance to reap itself.
    """
    from mewbo_core.tooling.tool_registry import ToolSpec
    from mewbo_tools.integration.shell_session import DEFAULT_TIMEOUT_S

    assert DEFAULT_TIMEOUT_S < ToolSpec.timeout
