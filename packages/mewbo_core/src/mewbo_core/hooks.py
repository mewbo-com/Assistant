#!/usr/bin/env python3
"""Hook manager for orchestration lifecycle events."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.contracts.types import EventRecord
from mewbo_core.permissions import PermissionDecision

if TYPE_CHECKING:
    from mewbo_core.agents.hypervisor import AgentHandle
    from mewbo_core.config import HookEntry, HooksConfig

logger = get_logger(name="core.hooks")

# Shared bound for any hook payload value derived from tool/event content —
# a tool result or an event field can be arbitrarily large (a file read, a
# big context payload); this caps it the same way everywhere it's exposed to
# a hook, whether via env var, HTTP preview, or a full event record.
_HOOK_VALUE_CAP = 2000

# Minimal env a command-hook subprocess needs to run a shell command at all.
# Command hooks execute unsandboxed on the host (see HookEntry.command's
# docstring); inheriting the FULL process environment would leak every
# secret the API/CLI process holds (LLM keys, DB URIs, proxy tokens) into a
# hook the operator merely pointed at a trusted script. Only these plus the
# documented MEWBO_* payload variables (added by each env builder below) are
# passed through.
_ALLOWED_ENV_KEYS = ("PATH", "HOME", "LANG", "TERM")


# An outcome-assertion token is an identifier and a detail is one line beside a
# status pill — both are clamped rather than rejected, mirroring ``RunError``:
# a hook reporting an over-long detail must still get its assertion through,
# because dropping it would silently restore the very laundering it exists to
# report.
_ASSERTION_TOKEN_MAX_CHARS = 64
_ASSERTION_DETAIL_MAX_CHARS = 500


class OutcomeAssertion(BaseModel):
    """A session-end hook's report that the session's PURPOSE was not achieved.

    The return channel a session-end hook needs in order to CONTRADICT a clean
    terminal. A session can end with no exception, no halt and no blocked
    envelope — every signal the loop owns says success — while the job it
    existed to perform never reached its terminal state. Nothing the loop can
    see distinguishes that from a real completion. Only the hook holding the
    owning job can, and until this existed it had no way to say so: the return
    value of ``run_on_session_end`` was discarded, so the one component able to
    evaluate a session's real outcome could write a warning to its own job log
    and nothing more.

    **Absence is not an assertion.** A hook that returns ``None`` — every
    command hook, every http hook, and any python hook that declines to judge —
    reports nothing, which is why the contract is an OPTIONAL RETURN and not a
    boolean: "did not report" and "reported success" must never collapse into
    each other, or the channel would invent an assertion for every hook that
    does not use it.

    ``reason`` is a PRODUCT-OWNED token and deliberately not a ``Literal``:
    core would otherwise have to learn every product's vocabulary before that
    product could tell the truth about itself. It never drives dispatch — the
    status this produces comes from the TYPE of this object, never from parsing
    the string — so it stays data, not a magic string.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        description="Short product-owned token naming the outcome that was not met.",
    )
    detail: str = Field(
        default="",
        description="One-line factual detail. Reaches clients, so it is bounded.",
    )
    source: str = Field(
        default="",
        description="Which hook asserted; stamped by the manager for forensics.",
    )

    @model_validator(mode="before")
    @classmethod
    def _bound_fields(cls, data: object) -> object:
        """Normalize and CLAMP at definition, so no call site can over-run a cap.

        Clamping rather than rejecting is the ``RunError`` precedent: these
        values are bounded because they are persisted and replayed to clients,
        but an assertion is an honesty signal and refusing an over-long one
        would discard the report entirely.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key, cap in (
            ("reason", _ASSERTION_TOKEN_MAX_CHARS),
            ("source", _ASSERTION_TOKEN_MAX_CHARS),
            ("detail", _ASSERTION_DETAIL_MAX_CHARS),
        ):
            raw = data.get(key)
            if not isinstance(raw, str):
                continue
            # A token is one bare word; a detail is one line.
            joiner = "_" if key != "detail" else " "
            data[key] = joiner.join(raw.split())[:cap]
        return data


def _scrubbed_env() -> dict[str, str]:
    """Build the minimal base env for a command-hook subprocess.

    Keeps ``PATH``/``HOME``/``LANG``/``TERM`` and any ``LC_*`` locale
    variable from the current process env; drops everything else. Callers
    layer their own ``MEWBO_*`` payload variables on top.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_ENV_KEYS or key.startswith("LC_")
    }


@dataclass
class HookDispatch:
    """The background threads behind every fire-and-forget hook, joinable.

    A dispatch nobody can join is only observable by sleeping and hoping the
    thread won. That is a RACE, not a slow caller: on a loaded machine the
    observation is simply WRONG rather than late. Holding the handles here
    gives any caller that needs the outcome — a shutdown path, a test — a
    bounded ``wait`` to join on, while every hot-path caller keeps ignoring the
    return value and the fire-and-forget semantics are unchanged.

    Process-wide by construction (:data:`HOOK_DISPATCH`) rather than per
    manager: the threads are the process's, a plugin-translated hook is
    dispatched with no manager in reach of the factory, and "has every
    dispatched hook landed" is the only question a joiner actually has.

    Cost: ``O(1)`` per submit and ``O(1)`` per completion — a thread drops
    itself from the set when it ends, so nothing ever sweeps the set and the
    dispatch a hook pays for does not grow with how many are already in flight.
    Memory is bounded by what is genuinely in flight, for the same reason.
    """

    _threads: set[threading.Thread] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def submit(self, target: Callable[..., None], *args: Any) -> threading.Thread:
        """Run *target* on a daemon thread and return it, tracked until it ends."""
        thread = threading.Thread(target=self._run, args=(target, args), daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def _run(self, target: Callable[..., None], args: tuple[Any, ...]) -> None:
        """Run *target*, then drop this thread from the tracking set.

        The removal is the thread's OWN job because the alternative — sweeping
        the set for finished threads on the way into ``submit`` — is
        ``O(in-flight)`` work under a process-wide lock, so a burst of events
        makes every dispatch in the burst pay for the burst. Measured on one
        host, that sweep cost 49 µs at 200 live threads and 1.09 ms at 3000,
        against a lock-free ``Thread(...).start()`` before any of this existed.
        Discarding one member is ``O(1)``, and it happens off the hot path.

        The ``finally`` does not swallow anything: a raising hook still reaches
        ``threading.excepthook`` exactly as it did when the target ran as the
        thread's own callable.
        """
        try:
            target(*args)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def wait(self, timeout: float = 5.0) -> bool:
        """Join everything in flight within *timeout*; return whether all landed.

        Never raises and never re-raises a hook's own failure — a joiner is
        asking whether the work finished, not taking on responsibility for what
        it did. A timeout logs once and returns ``False``.

        Cost: ``O(in-flight dispatches)`` joins, bounded overall by *timeout*.
        No caller is on a hot path — production never joins; this is for a
        shutdown path or a test.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            pending = list(self._threads)
        for thread in pending:
            thread.join(max(0.0, deadline - time.monotonic()))
        still_running = [t for t in pending if t.is_alive()]
        if still_running:
            logger.warning(
                "{} hook dispatch(es) still in flight after {}s", len(still_running), timeout
            )
            return False
        return True


# The one dispatcher every fire-and-forget hook submits to. A joiner waits on
# this; the hooks themselves never consult it.
HOOK_DISPATCH = HookDispatch()


@dataclass
class HookManager:
    """Container for hook callbacks used during orchestration."""

    pre_tool_use: list[Callable[[ActionStep], ActionStep]] = field(default_factory=list)
    post_tool_use: list[Callable[[ActionStep, MockSpeaker], MockSpeaker]] = field(
        default_factory=list
    )
    permission_request: list[Callable[[ActionStep, PermissionDecision], PermissionDecision]] = (
        field(default_factory=list)
    )
    pre_compact: list[Callable[[list[EventRecord]], list[EventRecord]]] = field(
        default_factory=list
    )
    on_agent_start: list[Callable[[AgentHandle], None]] = field(default_factory=list)
    on_agent_stop: list[Callable[[AgentHandle], None]] = field(default_factory=list)
    on_session_start: list[Callable[[str], None]] = field(default_factory=list)
    # Returns an ``OutcomeAssertion`` to report an unmet purpose, or ``None``
    # to report nothing. Widened from ``None`` — every existing hook satisfies
    # the new signature unchanged, since returning nothing IS returning None.
    on_session_end: list[Callable[[str, str | None], OutcomeAssertion | None]] = field(
        default_factory=list
    )
    on_event: list[Callable[[str, EventRecord], None]] = field(default_factory=list)
    on_compact: list[Callable[..., None]] = field(default_factory=list)

    def run_pre_tool_use(self, action_step: ActionStep) -> ActionStep:
        """Apply pre-tool hooks to an action step.

        Args:
            action_step: Action step to process.

        Returns:
            Updated action step after hooks run.
        """
        for hook in self.pre_tool_use:
            try:
                action_step = hook(action_step)
            except Exception:
                logger.warning("Pre-tool hook failed", exc_info=True)
        return action_step

    def run_post_tool_use(self, action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        """Apply post-tool hooks to a tool result.

        Args:
            action_step: Action step that was executed.
            result: Result returned by the tool.

        Returns:
            Updated result after hooks run.
        """
        for hook in self.post_tool_use:
            try:
                result = hook(action_step, result)
            except Exception:
                logger.warning("Post-tool hook failed", exc_info=True)
        return result

    def run_permission_request(
        self, action_step: ActionStep, decision: PermissionDecision
    ) -> PermissionDecision:
        """Apply permission hooks to a decision outcome.

        Args:
            action_step: Action step under review.
            decision: Current decision to modify.

        Returns:
            Updated permission decision after hooks run.
        """
        for hook in self.permission_request:
            try:
                decision = hook(action_step, decision)
            except Exception:
                logger.warning("Permission hook failed", exc_info=True)
        return decision

    def run_pre_compact(self, events: Iterable[EventRecord]) -> list[EventRecord]:
        """Apply compaction hooks to events prior to summarization.

        Args:
            events: Iterable of event records.

        Returns:
            List of event records after hooks run.
        """
        event_list: list[EventRecord] = list(events)
        for hook in self.pre_compact:
            try:
                event_list = hook(event_list)
            except Exception:
                logger.warning("Pre-compact hook failed", exc_info=True)
        return event_list

    def run_on_agent_start(self, handle: AgentHandle) -> None:
        """Notify hooks that an agent has started."""
        for hook in self.on_agent_start:
            try:
                hook(handle)
            except Exception:
                logger.warning("on_agent_start hook failed", exc_info=True)

    def run_on_agent_stop(self, handle: AgentHandle) -> None:
        """Notify hooks that an agent has stopped."""
        for hook in self.on_agent_stop:
            try:
                hook(handle)
            except Exception:
                logger.warning("on_agent_stop hook failed", exc_info=True)

    def run_on_session_start(self, session_id: str) -> None:
        """Notify hooks that a session has started."""
        for hook in self.on_session_start:
            try:
                hook(session_id)
            except Exception:
                logger.warning("on_session_start hook failed", exc_info=True)

    def run_on_session_end(
        self, session_id: str, error: str | None = None
    ) -> list[OutcomeAssertion]:
        """Notify hooks a session ended; collect any outcome assertions they report.

        A hook MAY return an :class:`OutcomeAssertion` to report that the
        session's purpose was not achieved. Returning ``None`` asserts nothing
        — see that class for why absence must stay distinguishable from success.

        The return value is COLLECTED, not discarded — discarding it is how a
        job that never reached its terminal state still presents as a clean
        completion, with the hook able to SEE the failure and nowhere to put
        it.

        Failure-isolated per hook like every other lifecycle dispatch, and
        deliberately strict about what it accepts: a hook returning something
        that is not an assertion is logged and IGNORED rather than coerced,
        because a truthy stray return would otherwise invent a failure.
        """
        assertions: list[OutcomeAssertion] = []
        for hook in self.on_session_end:
            try:
                reported = hook(session_id, error)
            except Exception:
                logger.warning("on_session_end hook failed", exc_info=True)
                continue
            if reported is None:
                continue
            if not isinstance(reported, OutcomeAssertion):
                logger.warning(
                    "on_session_end hook returned {}, not an OutcomeAssertion; ignoring",
                    type(reported).__name__,
                )
                continue
            if not reported.source:
                # Re-CONSTRUCTED rather than ``model_copy``d: the stamp is a
                # function name of unbounded length, and model_copy skips the
                # validator that bounds it.
                reported = OutcomeAssertion(
                    reason=reported.reason,
                    detail=reported.detail,
                    source=getattr(hook, "__name__", "") or type(hook).__name__,
                )
            assertions.append(reported)
        return assertions

    def run_on_event(self, session_id: str, event: EventRecord) -> None:
        """Notify hooks that an event was appended to a session transcript.

        Runs on the event-append hot path (via the ``SessionEventBus``
        observer), so every registered hook is itself fire-and-forget; this
        loop only dispatches and is failure-isolated per hook.
        """
        for hook in self.on_event:
            try:
                hook(session_id, event)
            except Exception:
                logger.warning("on_event hook failed", exc_info=True)

    def run_on_compact(
        self,
        session_id: str,
        *,
        summary: str = "",
        tokens_before: int = 0,
        tokens_saved: int = 0,
        events_summarized: int = 0,
    ) -> None:
        """Notify hooks that compaction occurred."""
        for hook in self.on_compact:
            try:
                hook(
                    session_id,
                    summary=summary,
                    tokens_before=tokens_before,
                    tokens_saved=tokens_saved,
                    events_summarized=events_summarized,
                )
            except Exception:
                logger.warning("on_compact hook failed", exc_info=True)

    @classmethod
    def load_from_config(cls, hooks_config: HooksConfig) -> HookManager:
        """Create a HookManager with hooks loaded from config."""
        manager = cls()
        pre_map = {"command": _make_command_hook, "http": _make_http_hook}
        post_map = {"command": _make_post_tool_hook, "http": _make_http_post_tool_hook}
        start_map = {"command": _make_session_hook, "http": _make_http_session_hook}
        end_map = {"command": _make_session_end_hook, "http": _make_http_session_end_hook}
        event_map = {"command": _make_event_command_hook, "http": _make_http_event_hook}
        for entry in hooks_config.pre_tool_use:
            manager.pre_tool_use.append(pre_map.get(entry.type, _make_command_hook)(entry))
        for entry in hooks_config.post_tool_use:
            manager.post_tool_use.append(post_map.get(entry.type, _make_post_tool_hook)(entry))
        for entry in hooks_config.on_session_start:
            manager.on_session_start.append(start_map.get(entry.type, _make_session_hook)(entry))
        for entry in hooks_config.on_session_end:
            manager.on_session_end.append(end_map.get(entry.type, _make_session_end_hook)(entry))
        for entry in hooks_config.on_event:
            manager.on_event.append(event_map.get(entry.type, _make_event_command_hook)(entry))
        return manager


def _matches(matcher: str | None, tool_id: str) -> bool:
    """Check if a tool_id matches a hook's matcher pattern."""
    if matcher is None:
        return True
    return fnmatch.fnmatch(tool_id, matcher)


def _hook_env(action_step: ActionStep, result_content: str | None = None) -> dict[str, str]:
    """Build env vars to pass to command hooks (scrubbed base + MEWBO_* payload)."""
    env = _scrubbed_env()
    env["MEWBO_TOOL_ID"] = action_step.tool_id or ""
    env["MEWBO_OPERATION"] = action_step.operation or ""
    if result_content is not None:
        env["MEWBO_TOOL_RESULT"] = result_content[:_HOOK_VALUE_CAP]
    return env


def _session_env(session_id: str, error: str | None = None) -> dict[str, str]:
    """Build env vars for session lifecycle command hooks (scrubbed base + MEWBO_* payload)."""
    env = _scrubbed_env()
    env["MEWBO_SESSION_ID"] = session_id
    if error is not None:
        env["MEWBO_ERROR"] = error
    return env


# shell=True is intentional across every command-hook factory below: ``entry.command``
# is operator/plugin-author config (trusted, version-controlled scripts pointed at by
# app.json / a plugin's hooks.json), never runtime input. Untrusted tool/session data
# reaches the subprocess only through the scrubbed env (``_hook_env``/``_session_env``)
# and, for on_event, JSON on stdin — never spliced into the command string, so there is
# no interpolation surface to quote away.
def _make_command_hook(entry: HookEntry) -> Callable[[ActionStep], ActionStep]:
    """Create a pre-tool-use hook from a config entry."""

    def hook(action_step: ActionStep) -> ActionStep:
        if not _matches(entry.matcher, action_step.tool_id):
            return action_step
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_hook_env(action_step),
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Command hook timed out or failed: {}", entry.command)
        return action_step

    return hook


def _make_post_tool_hook(entry: HookEntry) -> Callable[[ActionStep, MockSpeaker], MockSpeaker]:
    """Create a post-tool-use hook from a config entry."""

    def hook(action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        if not _matches(entry.matcher, action_step.tool_id):
            return result
        content = getattr(result, "content", None)
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_hook_env(action_step, str(content) if content else None),
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Command hook timed out or failed: {}", entry.command)
        return result

    return hook


def _make_session_hook(entry: HookEntry) -> Callable[[str], None]:
    """Create a session lifecycle hook from a config entry."""

    def hook(session_id: str) -> None:
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_session_env(session_id),
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Session hook timed out or failed: {}", entry.command)

    return hook


def _make_session_end_hook(entry: HookEntry) -> Callable[[str, str | None], None]:
    """Create a session end hook from a config entry."""

    def hook(session_id: str, error: str | None = None) -> None:
        try:
            subprocess.run(
                entry.command,
                shell=True,
                timeout=entry.timeout,
                capture_output=True,
                text=True,
                env=_session_env(session_id, error),
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Session end hook timed out or failed: {}", entry.command)

    return hook


def _run_event_command(session_id: str, event: EventRecord, entry: HookEntry) -> None:
    """Run an ``on_event`` command hook subprocess (called on a daemon thread).

    The event is handed to the subprocess as JSON on stdin, with
    ``MEWBO_SESSION_ID`` and ``MEWBO_EVENT_TYPE`` in the environment.
    """
    env = _session_env(session_id)
    env["MEWBO_EVENT_TYPE"] = str(event.get("type", ""))
    try:
        subprocess.run(
            entry.command,
            shell=True,
            timeout=entry.timeout,
            capture_output=True,
            text=True,
            input=json.dumps(event),
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("Event hook timed out or failed: {}", entry.command)


def _make_event_command_hook(entry: HookEntry) -> Callable[[str, EventRecord], None]:
    """Create a non-blocking ``on_event`` command hook from a config entry.

    Fires the subprocess on a daemon thread so a slow hook never blocks the
    event-append hot path. The ``matcher`` fnmatches the event ``type``.
    """

    def hook(session_id: str, event: EventRecord) -> None:
        if not _matches(entry.matcher, str(event.get("type", ""))):
            return
        HOOK_DISPATCH.submit(_run_event_command, session_id, event, entry)

    return hook


# ---------------------------------------------------------------------------
# HTTP hook factories (fire-and-forget POST to external URLs)
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> None:
    """POST JSON payload to a URL. Called from a daemon thread."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
    except Exception:
        logger.warning("HTTP hook POST failed: {}", url, exc_info=True)


def _fire_http(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> None:
    """Submit the POST to :data:`HOOK_DISPATCH` without blocking the caller."""
    HOOK_DISPATCH.submit(_post_json, url, payload, headers, timeout)


def _make_http_hook(entry: HookEntry) -> Callable[[ActionStep], ActionStep]:
    """Create a pre-tool-use HTTP hook from a config entry."""

    def hook(action_step: ActionStep) -> ActionStep:
        if not _matches(entry.matcher, action_step.tool_id):
            return action_step
        payload = {
            "event": "pre_tool_use",
            "tool_id": action_step.tool_id,
            "operation": action_step.operation,
        }
        _fire_http(entry.url, payload, entry.headers, entry.timeout)
        return action_step

    return hook


def _make_http_post_tool_hook(
    entry: HookEntry,
) -> Callable[[ActionStep, MockSpeaker], MockSpeaker]:
    """Create a post-tool-use HTTP hook from a config entry."""

    def hook(action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        if not _matches(entry.matcher, action_step.tool_id):
            return result
        content = getattr(result, "content", None)
        payload: dict[str, Any] = {
            "event": "post_tool_use",
            "tool_id": action_step.tool_id,
            "operation": action_step.operation,
        }
        if content is not None:
            payload["result_preview"] = str(content)[:_HOOK_VALUE_CAP]
        _fire_http(entry.url, payload, entry.headers, entry.timeout)
        return result

    return hook


def _make_http_session_hook(entry: HookEntry) -> Callable[[str], None]:
    """Create a session start HTTP hook from a config entry."""

    def hook(session_id: str) -> None:
        _fire_http(
            entry.url,
            {"event": "session_start", "session_id": session_id},
            entry.headers,
            entry.timeout,
        )

    return hook


def _make_http_session_end_hook(entry: HookEntry) -> Callable[[str, str | None], None]:
    """Create a session end HTTP hook from a config entry."""

    def hook(session_id: str, error: str | None = None) -> None:
        payload: dict[str, Any] = {"event": "session_end", "session_id": session_id}
        if error is not None:
            payload["error"] = error
        _fire_http(entry.url, payload, entry.headers, entry.timeout)

    return hook


def _truncate_event_values(value: Any, cap: int = _HOOK_VALUE_CAP) -> Any:
    """Recursively cap string values inside an event record for HTTP hook payloads.

    An ``EventRecord`` can carry an arbitrarily large tool result or context
    payload; POSTing it unbounded to an operator-configured URL leaks as much
    as the largest value in the transcript. Only string VALUES are shortened
    — every key and every dict/list shape is preserved, so a receiver's
    parsing code never breaks on a truncated payload.
    """
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap]
    if isinstance(value, dict):
        return {key: _truncate_event_values(item, cap) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_event_values(item, cap) for item in value]
    return value


def _make_http_event_hook(entry: HookEntry) -> Callable[[str, EventRecord], None]:
    """Create a fire-and-forget ``on_event`` HTTP hook from a config entry.

    POSTs ``{"event": "session_event", "session_id": ..., "record": event}``,
    with the record's string values truncated to ``_HOOK_VALUE_CAP`` (mirrors
    the command variant's ``MEWBO_TOOL_RESULT`` bound). The ``matcher``
    fnmatches the event ``type``.
    """

    def hook(session_id: str, event: EventRecord) -> None:
        if not _matches(entry.matcher, str(event.get("type", ""))):
            return
        payload: dict[str, Any] = {
            "event": "session_event",
            "session_id": session_id,
            "record": _truncate_event_values(event),
        }
        _fire_http(entry.url, payload, entry.headers, entry.timeout)

    return hook


def default_hook_manager() -> HookManager:
    """Create a hook manager with no custom hooks registered.

    Returns:
        Empty HookManager instance.
    """
    return HookManager()


_PLUGIN_HOOK_MAP: dict[str, tuple[str, Callable]] = {
    "PreToolUse": ("pre_tool_use", _make_command_hook),
    "PostToolUse": ("post_tool_use", _make_post_tool_hook),
    "SessionStart": ("on_session_start", _make_session_hook),
    "SessionEnd": ("on_session_end", _make_session_end_hook),
    "SessionEvent": ("on_event", _make_event_command_hook),
}


def merge_plugin_hooks(
    manager: HookManager,
    hooks_json: dict[str, Any],
    plugin_root: str,
) -> None:
    """Translate Claude Code plugin hooks.json into HookManager callbacks.

    Supports: PreToolUse, PostToolUse, SessionStart, SessionEnd.
    Substitutes ${CLAUDE_PLUGIN_ROOT} in command strings.
    """
    from mewbo_core.config import HookEntry
    from mewbo_core.tooling.plugins import substitute_plugin_vars

    raw_hooks = hooks_json.get("hooks", {})
    for cc_event, entry_groups in raw_hooks.items():
        mapping = _PLUGIN_HOOK_MAP.get(cc_event)
        if mapping is None:
            continue
        slot_name, factory = mapping
        for group in entry_groups:
            matcher = group.get("matcher")
            for hook_def in group.get("hooks", []):
                if hook_def.get("type") != "command":
                    continue
                command = hook_def.get("command")
                if not command:
                    continue
                command = substitute_plugin_vars(command, plugin_root)
                entry = HookEntry(
                    type="command",
                    command=command,
                    matcher=matcher,
                    timeout=hook_def.get("timeout", 30),
                )
                getattr(manager, slot_name).append(factory(entry))


__all__ = [
    "HOOK_DISPATCH",
    "HookDispatch",
    "HookManager",
    "default_hook_manager",
    "merge_plugin_hooks",
]
