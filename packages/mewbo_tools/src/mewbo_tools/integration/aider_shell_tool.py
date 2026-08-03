#!/usr/bin/env python3
"""Shell execution tool — foreground by default, backgroundable on request."""

from __future__ import annotations

import os
import time

from mewbo_core.classes import AbstractTool, ActionStep
from mewbo_core.common import MockSpeaker, get_mock_speaker
from pydantic import ValidationError

from mewbo_tools.core import resolve_safe_path
from mewbo_tools.integration.shell_session import (
    SHELL_SESSIONS,
    ShellSession,
    ShellStartArgs,
)


def _parse_shell_request(action_step: ActionStep | None) -> tuple[ShellStartArgs, str]:
    """Validate the model's arguments and resolve the confined working directory.

    Returns the parsed arguments alongside the resolved ``cwd``. A bare string
    ``tool_input`` is still accepted — several models send one — and means the
    command with every other field defaulted.
    """
    if action_step is None:
        raise ValueError("Action step is required.")
    argument = action_step.tool_input
    if isinstance(argument, str):
        args = ShellStartArgs(command=argument)
        return args, os.getcwd()
    if isinstance(argument, dict):
        args = ShellStartArgs.model_validate(argument)
        root = args.root or os.getcwd()
        cwd = str(resolve_safe_path(args.cwd or root, root=root))
        return args, cwd
    raise ValueError("Tool input must be a string command or an object payload.")


class AiderShellTool(AbstractTool):
    """Run a shell command, optionally leaving it running in the background.

    Always non-interactive unless the call asks for a TTY: the vendored Aider
    ``run_cmd`` picks a pexpect PTY whenever the PARENT has a tty, which let a
    paginated command like ``git log`` block forever on ``less`` and leaked the
    interactive rc-file banner into captured output. ``tty`` is therefore opt-in
    per call — the model asks for a terminal when it intends to drive one, and
    never inherits it by accident. Codex reaches the same default
    (``default_tty() -> false``); Claude Code and opencode allocate no PTY at
    all.
    """

    def __init__(self) -> None:
        """Initialize the shell execution tool."""
        super().__init__(
            name="Aider Shell",
            description="Run shell commands, foreground or background.",
            use_llm=False,
        )

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Execute a shell command and return its output or a session handle."""
        speaker = get_mock_speaker()
        try:
            args, cwd = _parse_shell_request(action_step)
        except ValidationError as exc:
            return speaker(content=f"Invalid shell arguments: {exc.errors()[0]['msg']}")
        except ValueError as exc:
            return speaker(content=str(exc))

        started = time.monotonic()
        try:
            session = SHELL_SESSIONS.create(args, cwd)
        except ValueError as exc:
            return speaker(content=str(exc))

        if args.run_in_background:
            return speaker(content=self._background_payload(session, args))
        return speaker(content=self._foreground_payload(session, args, started))

    def _background_payload(
        self, session: ShellSession, args: ShellStartArgs
    ) -> dict[str, object]:
        """Describe a session left running, naming how to reach it again."""
        return {
            "kind": "shell",
            "command": args.command,
            "cwd": session.cwd,
            "shell_id": session.shell_id,
            "status": session.status,
            "stdout": "",
            "stderr": "",
            "note": (
                f"Started in the background as {session.shell_id}. Read its output with "
                f"shell_session_tool {{'operation': 'read', 'shell_id': "
                f"'{session.shell_id}'}}, send it input with 'write', and stop it with "
                f"'kill'."
            ),
        }

    def _foreground_payload(
        self, session: ShellSession, args: ShellStartArgs, started: float
    ) -> dict[str, object]:
        """Wait for the command and render the historical one-shot envelope.

        A timed-out command is KILLED with its whole process group and reported
        with whatever it managed to emit — abandoning the wait would leave the
        command (and any child it spawned) running with nothing holding its
        handle, which is the orphan this bound exists to prevent.
        """
        exited = session.wait(args.timeout)
        read = session.read()
        output = str(read.get("output") or "")
        if not exited:
            session.kill()
            SHELL_SESSIONS.discard(session.shell_id)
            message = (
                f"[shell] command timed out after {args.timeout}s and was killed. "
                f"Re-run it with run_in_background=true to keep it alive and poll "
                f"its output instead."
            )
            output = f"{output}\n{message}" if output else message
            exit_code = 1
        else:
            SHELL_SESSIONS.discard(session.shell_id)
            exit_code = session.exit_code if session.exit_code is not None else 1
        return {
            "kind": "shell",
            "command": args.command,
            "cwd": session.cwd,
            "exit_code": exit_code,
            "stdout": output,
            "stderr": "",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }


__all__ = ["AiderShellTool"]
