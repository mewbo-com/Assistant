"""Webhook routes, shared message pipeline, and channel system init.

Registers a single Flask Blueprint with a generic
``POST /api/webhooks/<platform>`` endpoint that delegates to the
appropriate :class:`ChannelAdapter`.  Non-webhook channels (e.g. email
via IMAP polling) share the same processing pipeline through
:func:`_process_inbound`.

The bot only processes messages that contain its trigger keyword
(e.g. ``@Mewbo``).  Non-mentioned messages are silently
acknowledged — no events are appended, no LLM runs are started.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

from flask import Blueprint, Flask, request
from mewbo_core.common import get_logger
from mewbo_core.permissions import auto_approve
from mewbo_core.session.token_budget import get_token_budget
from mewbo_core.tooling.exit_plan_mode import session_temp_dir
from mewbo_core.workspaces.project_catalog import ProjectCatalog, ProjectEntry

from mewbo_api.auth.guard_registry import guard
from mewbo_api.channels.base import (
    ChannelRegistry,
    DeduplicationGuard,
    InboundMessage,
)
from mewbo_api.channels.nextcloud_talk import NextcloudTalkAdapter

if TYPE_CHECKING:
    from mewbo_core.config import AppConfig
    from mewbo_core.contracts.types import EventRecord
    from mewbo_core.hooks import HookManager
    from mewbo_core.loop.session_runtime import SessionRuntime

logger = get_logger(name="channels.routes")

channel_bp = Blueprint("channels", __name__)

# Module-level references injected by ``init_channels()``.
_runtime: SessionRuntime | None = None
_hook_manager: HookManager | None = None
_registry: ChannelRegistry = ChannelRegistry()
_dedup: DeduplicationGuard = DeduplicationGuard()

# Matches ``/command`` or ``/command args`` after the trigger keyword.
_COMMAND_RE = re.compile(r"/([\w-]+)[^\S\n]*(.*)")


@final
class ChannelProjectContext:
    """A channel session's project: how it is listed, switched, and read back.

    One atomic class because the three must agree on the KEYS. The shared reader
    every other surface goes through (``backend._resolve_session_cwd`` — the diff
    endpoints, ``/message`` re-engage, a console ``/query`` into the same
    session) reads ``cwd``/``project``, so ``/switch-project`` writes exactly
    those. A channel-private pair would leave a room that switched project
    reporting the wrong directory everywhere except its own next message, and a
    console follow-up running the turn somewhere else entirely — which is why
    ``active_project``/``active_project_cwd`` survive only as a READ fallback for
    rooms whose transcripts already carry them.

    Both collaborators are injected because both live one layer up: the catalog
    is composed at the API's composition root, and the shared cwd resolution is
    ``backend``'s — which imports this module, so reaching back for either would
    cycle. Both arrive as CALLABLES rather than as the objects themselves, the
    same late-binding the session-spec store uses: the suite swaps the runtime
    and the project store wholesale, so a catalog captured at wiring time would
    keep answering from stores the process has already replaced. With neither
    bound (a test wiring only the adapters) every method degrades to the
    behaviour of a deployment with no projects, never a raise.
    """

    def __init__(
        self,
        *,
        catalog_source: Callable[[], ProjectCatalog] | None = None,
        resolve_session_cwd: Callable[[str], str | None] | None = None,
    ) -> None:
        """Bind the project catalog source and the shared session-cwd resolution."""
        self.catalog_source = catalog_source
        self.resolve_session_cwd = resolve_session_cwd

    def choices(self) -> tuple[ProjectEntry, ...]:
        """Every project a room can switch to — the ones on disk right now."""
        if self.catalog_source is None:
            return ()
        return tuple(entry for entry in self.catalog_source().entries() if entry.runnable)

    def find(self, key: str) -> ProjectEntry | None:
        """The entry a room named, or ``None``. Never raises."""
        return self.catalog_source().find(key) if self.catalog_source is not None else None

    def format_list(self, header: str) -> str:
        """Render *header* followed by the switchable projects as a markdown list.

        Lists each project by its catalog KEY rather than a display name, because
        the key is what ``/switch-project`` takes back — a managed project shown
        as its bare name would read as switchable and then not resolve.
        """
        entries = self.choices()
        lines = [header, "", "**Available projects:**"]
        lines.extend(
            f"- `{entry.key}`" + (f" — {entry.description}" if entry.description else "")
            for entry in entries
        )
        if not entries:
            lines.append("- _(none configured)_")
        return "\n".join(lines)

    def cwd_for(
        self,
        session_id: str,
        load_transcript: Callable[[], Sequence[Mapping[str, Any]]],
    ) -> str | None:
        """The session's working directory: the SHARED resolution, then ``active_project_cwd``.

        Read both spellings, write only the shared one — a room whose transcript
        carries only ``active_project_cwd`` keeps resolving off what it already
        has, with nothing to migrate. The transcript arrives as a LAZY loader so
        the second leg costs a read only when the shared one misses.
        """
        if self.resolve_session_cwd is not None:
            shared = self.resolve_session_cwd(session_id)
            if shared:
                return shared
        return self._legacy_cwd(load_transcript())

    @staticmethod
    def _legacy_cwd(transcript: Sequence[Mapping[str, Any]]) -> str | None:
        """The channel-private ``active_project_cwd`` key; newest event wins."""
        for event in reversed(transcript):
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("active_project_cwd"):
                return str(payload["active_project_cwd"])
        return None


# Injected by ``init_channels()`` alongside ``_runtime``; see the class docstring
# for why the catalog and the shared resolution are handed in rather than reached
# for.
_projects = ChannelProjectContext()


# ------------------------------------------------------------------
# Command registry
# ------------------------------------------------------------------


@dataclass(frozen=True)
class CommandContext:
    """Everything a slash-command handler needs."""

    session_id: str
    args: str
    message: InboundMessage
    tag: str


# command_name → (handler, one-line description)
_COMMANDS: dict[str, tuple[Callable[[CommandContext], str], str]] = {}


def command(name: str, description: str) -> Callable:
    """Decorator that registers a slash command."""

    def decorator(fn: Callable[[CommandContext], str]) -> Callable[[CommandContext], str]:
        _COMMANDS[name] = (fn, description)
        return fn

    return decorator


def _dispatch_command(cmd: str, ctx: CommandContext) -> str | None:
    """Run a registered command.  Returns response text, or None."""
    entry = _COMMANDS.get(cmd.lower())
    return entry[0](ctx) if entry else None


def _build_help_text() -> str:
    """Auto-generate help from the command registry."""
    lines = [
        "**Mewbo** — AI assistant with a conversation state machine and agent hypervisor",
        "",
        "**Commands** (use after @mention):",
    ]
    for name, (_, desc) in _COMMANDS.items():
        lines.append(f"- `/{name}` — {desc}")
    lines += ["", "**Usage:** `@Mewbo <your question or task>`"]
    return "\n".join(lines)


# ---- Registered commands ----


@command("help", "Show available commands")
def _cmd_help(ctx: CommandContext) -> str:
    return _build_help_text()


@command("usage", "Show session context usage and token budget")
def _cmd_usage(ctx: CommandContext) -> str:
    assert _runtime is not None  # noqa: S101
    events = _runtime.session_store.load_transcript(ctx.session_id)
    summary = _runtime.session_store.load_summary(ctx.session_id)
    budget = get_token_budget(events, summary, model_name=None)
    return (
        f"**Session context usage**\n"
        f"- Events: {len(events)}\n"
        f"- Tokens used: ~{budget.total_tokens:,}\n"
        f"- Context window: {budget.context_window:,}\n"
        f"- Utilization: {budget.utilization:.0%}\n"
        f"- Auto-compact threshold: {budget.threshold:.0%}\n"
        f"- Status: {'⚠️ Compaction needed' if budget.needs_compact else '✅ Healthy'}"
    )


@command("new", "Start a fresh conversation")
def _cmd_new(ctx: CommandContext) -> str:
    assert _runtime is not None  # noqa: S101
    # Deliberately mint-then-RE-POINT rather than ``resolve_session(session_tag=)``:
    # resolving the tag would hand back the very conversation this command exists
    # to leave behind. The tag follows the new session; the old one keeps its
    # transcript and simply stops being what the room resolves to.
    new_id = _runtime.resolve_session()
    _runtime.tag_session(new_id, ctx.tag)
    _runtime.append_context_event(
        new_id,
        {
            "source_platform": ctx.message.platform,
            "channel_id": ctx.message.channel_id,
            "thread_id": ctx.message.thread_id,
            "sender": ctx.message.sender_name,
            "room": ctx.message.room_name,
        },
    )
    return "Fresh conversation started. Previous context cleared."


@command("switch-project", "Switch project context (`<name>`)")
def _cmd_switch_project(ctx: CommandContext) -> str:
    assert _runtime is not None  # noqa: S101
    if not ctx.args:
        return _projects.format_list("Usage: `/switch-project <name>`")
    entry = _projects.find(ctx.args)
    if entry is None or not entry.path or not entry.available:
        return _projects.format_list(f"Unknown project **{ctx.args}**.")
    _runtime.session_store.append_event(
        ctx.session_id,
        {
            "type": "context",
            "payload": {
                "source_platform": ctx.message.platform,
                # The SHARED keys — the ones every other surface's cwd
                # resolution reads. The room-local ``active_project``/
                # ``active_project_cwd`` pair is invisible outside this module, so
                # writing only that pair leaves a switched room reporting the
                # wrong directory on the diff endpoints and running a console
                # follow-up in the wrong place. That pair is still READ
                # (:meth:`ChannelProjectContext.cwd_for`); it is never written.
                "project": entry.key,
                "cwd": entry.path,
            },
        },
    )
    return f"Switched to project **{entry.key}** (`{entry.path}`)."


# ------------------------------------------------------------------
# Webhook endpoint
# ------------------------------------------------------------------


def _process_inbound(
    adapter: Any,
    message: InboundMessage,
) -> tuple[dict[str, str], int]:
    """Shared message processing pipeline.

    Called by the webhook endpoint **and** non-webhook pollers (e.g.
    email IMAP).  Handles dedup → mention gate → trigger strip →
    session resolution → command dispatch → LLM invocation.
    """
    assert _runtime is not None  # noqa: S101
    platform = message.platform

    dedup_key = f"{platform}:{message.message_id}"
    if _dedup.is_duplicate(dedup_key):
        return {}, 200  # Replay

    # --- Gate: ignore messages that don't mention the bot ---
    if not _is_mentioned(message):
        return {}, 200

    # --- Strip trigger keyword from the user's text ---
    user_text = _strip_trigger(message)

    # --- Session resolution: thread-scoped > room-scoped ---
    if message.thread_id:
        tag = f"{platform}:thread:{message.channel_id}:{message.thread_id}"
    else:
        tag = f"{platform}:room:{message.channel_id}"

    # ``resolve_session`` is the one resolve-or-create seam. The pre-read stays
    # because only a JUST-MINTED session gets the channel-identity context event:
    # re-writing it per message would let a later sender/room silently overwrite
    # the pair the conversation was opened with.
    existing = _runtime.session_store.resolve_tag(tag)
    session_id = _runtime.resolve_session(session_tag=tag)
    if existing is None:
        _runtime.append_context_event(
            session_id,
            {
                "source_platform": platform,
                "channel_id": message.channel_id,
                "thread_id": message.thread_id,
                "sender": message.sender_name,
                "room": message.room_name,
            },
        )

    # --- Gate: a permanently terminated session accepts no new work ---
    # A room/thread tag can outlive its session's termination (the user keeps
    # chatting in the same channel). Don't create a run; reply once so the
    # sender knows why nothing happens, then acknowledge.
    if _runtime.is_terminated(session_id):
        logger.info(
            "Ignoring channel message for terminated session {} (tag {}).",
            session_id,
            tag,
        )
        adapter.send_response(
            channel_id=message.channel_id,
            text="This conversation has been permanently terminated and can no longer respond.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return {}, 200

    # --- Check for slash commands (no LLM needed) ---
    cmd_match = _COMMAND_RE.match(user_text.strip())
    if cmd_match:
        ctx = CommandContext(
            session_id=session_id,
            args=cmd_match.group(2).strip(),
            message=message,
            tag=tag,
        )
        response = _dispatch_command(cmd_match.group(1), ctx)
        if response is not None:
            adapter.send_response(
                channel_id=message.channel_id,
                text=response,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return {}, 200
        # Unknown command — fall through to LLM

    # --- Store reply target for the completion hook ---
    _runtime.session_store.append_event(
        session_id,
        {
            "type": "context",
            "payload": {
                "source_platform": platform,
                "reply_to_message_id": message.message_id,
                "channel_id": message.channel_id,
                "thread_id": message.thread_id,
            },
        },
    )

    # --- Acknowledge receipt with a reaction (if adapter supports it) ---
    if hasattr(adapter, "send_reaction"):
        adapter.send_reaction(
            channel_id=message.channel_id,
            emoji="\N{EYES}",
            reply_to=message.message_id,
            thread_id=message.thread_id,
        )

    # --- Steer running session or start new run ---
    if _runtime.is_running(session_id):
        _runtime.enqueue_message(session_id, user_text)
        return {}, 200

    runtime = _runtime
    project_cwd = _projects.cwd_for(
        session_id, lambda: runtime.session_store.load_transcript(session_id)
    ) or session_temp_dir(session_id)
    client_ctx = getattr(adapter, "system_context", None)

    _runtime.start_async(
        session_id=session_id,
        user_query=user_text,
        hook_manager=_hook_manager,
        approval_callback=auto_approve,
        cwd=project_cwd,
        skill_instructions=client_ctx,
        source_platform=platform,
    )
    return {}, 200


@channel_bp.route("/api/webhooks/<platform>", methods=["POST"])
@guard.public(
    "platform webhook; a webhook-capable adapter's own HMAC signature check is "
    "the proof. Platforms with no push surface (e.g. email, polled via IMAP) "
    "carry supports_webhook=False and 404 here before either adapter method runs."
)
def webhook_receive(platform: str) -> tuple[dict[str, str], int]:
    """Receive an inbound webhook from a chat platform.

    Authenticates and parses via the platform adapter, then delegates
    to :func:`_process_inbound` for the shared processing pipeline.
    """
    if _runtime is None:
        return {"error": "Channel system not initialised"}, 500

    adapter = _registry.get(platform)
    if not adapter or not adapter.supports_webhook:
        return {"error": "Unknown platform"}, 404

    body = request.get_data()
    headers = {k: v for k, v in request.headers}

    if not adapter.verify_request(headers, body):
        return {"error": "Unauthorized"}, 401

    message = adapter.parse_inbound(headers, body)
    if message is None:
        return {}, 200  # Non-message event — acknowledge silently

    return _process_inbound(adapter, message)


# ------------------------------------------------------------------
# Mention detection & text cleanup
# ------------------------------------------------------------------


def _is_mentioned(message: InboundMessage) -> bool:
    """Return True if the message should be processed.

    Adapters may expose a ``requires_mention(message)`` method to
    dynamically decide whether the trigger keyword must appear.  For
    example, the email adapter skips mention gating for 1-to-1 emails
    but requires ``@Mewbo`` in multi-party threads.
    """
    adapter = _registry.get(message.platform)
    if adapter is None:
        return True
    # Let adapter opt out of mention gating per message
    if hasattr(adapter, "requires_mention"):
        if not adapter.requires_mention(message):
            return True  # Adapter says no mention needed
    keyword: str = getattr(adapter, "trigger_keyword", "")
    if not keyword:
        return True  # Empty keyword → respond to all
    return keyword.lower() in message.text.lower()


def _strip_trigger(message: InboundMessage) -> str:
    """Remove the trigger keyword from the message text."""
    adapter = _registry.get(message.platform)
    keyword: str = getattr(adapter, "trigger_keyword", "") if adapter else ""
    if not keyword:
        return message.text
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    return pattern.sub("", message.text, count=1).strip()


# ------------------------------------------------------------------
# Completion callback (on_session_end hook)
# ------------------------------------------------------------------


def _channel_completion_hook(session_id: str, error: str | None = None) -> None:
    """Send the final answer back to the originating chat thread."""
    if _runtime is None:
        return

    events = _runtime.session_store.load_transcript(session_id)
    ctx = _find_channel_context(events)
    if not ctx:
        return  # Not a channel session

    adapter = _registry.get(ctx.get("source_platform", ""))
    if not adapter:
        return

    outcome = _runtime.summarize_session(session_id, events=events)
    final_text = extract_final_answer(events, error, outcome=outcome)
    if not final_text:
        return

    channel_id = ctx.get("channel_id", "")
    thread_id = ctx.get("thread_id")
    reply_to = ctx.get("reply_to_message_id") or thread_id
    adapter.send_response(
        channel_id=channel_id,
        text=final_text,
        thread_id=thread_id,
        reply_to=reply_to,
    )


def _find_channel_context(
    events: list[EventRecord],
) -> dict[str, Any] | None:
    """Find the most recent context event with ``source_platform``."""
    for event in reversed(events):
        if event.get("type") != "context":
            continue
        payload = event.get("payload", {})
        if "source_platform" in payload:
            return dict(payload)
    return None


# Presentation-only labels for a completion's ``blocked_code``. The CODE itself
# is derived exactly once, in core's ``session_runtime._completion_status``
# table — this only prettifies a known value for a chat/CI reply; an
# unrecognised one (a future core addition) falls back to its raw spelling
# rather than a lookup failure, so this can never mask a wall it doesn't
# recognise yet.
_BLOCKED_CODE_LABELS: dict[str, str] = {
    "repo_access": "repository access",
    "network": "a network problem",
    "forbidden": "a permission restriction",
    "quota_exceeded": "a usage quota",
}


def _frame_blocked_reply(outcome: Mapping[str, object], final_text: str) -> str:
    """Reframe a blocked run's reply so it reads as blocked, not finished."""
    code = outcome.get("blocked_code")
    label = _BLOCKED_CODE_LABELS.get(str(code), str(code)) if code else "an unresolved wall"
    header = f"⚠️ This run was blocked on {label} before it could finish."
    return f"{header}\n\n{final_text}" if final_text else header


def _frame_unmet_goal_reply(outcome: Mapping[str, object], final_text: str) -> str:
    """Reframe a run that never reached its goal so it reads as incomplete."""
    reason = outcome.get("unmet_goal_reason") or outcome.get("done_reason")
    header = "⚠️ This run ended without completing its goal."
    if reason:
        header = f"{header} ({reason})"
    return f"{header}\n\n{final_text}" if final_text else header


def extract_final_answer(
    events: list[EventRecord],
    error: str | None,
    *,
    outcome: Mapping[str, object] | None = None,
) -> str:
    """Walk the transcript backwards to find the final answer text.

    Shared by every reply-capable inbound surface (chat channels here,
    ``vcs_pickup`` for CI) — the "what do we send back" rule must not fork.

    ``outcome`` is the caller's own ``SessionRuntime.summarize_session()``
    result — core's ONE status derivation (see ``session_runtime.py``), never
    re-derived here. When supplied and the derived ``status`` is ``blocked``
    or ``unmet_goal``, the reply is reframed so a stopped run can never read
    as a clean success; omitted, a caller gets exactly today's behavior.
    """
    if error:
        return f"Session ended with an error: {error}"
    final_text = ""
    for event in reversed(events):
        etype = event.get("type", "")
        payload = event.get("payload", {})
        if etype == "completion":
            result = payload.get("task_result")
            if result:
                final_text = str(result)
                break
        if etype == "assistant":
            text = payload.get("text")
            if text:
                final_text = str(text)
                break
    if outcome is None:
        return final_text
    status = outcome.get("status")
    if status == "blocked":
        return _frame_blocked_reply(outcome, final_text)
    if status == "unmet_goal":
        return _frame_unmet_goal_reply(outcome, final_text)
    return final_text


# ------------------------------------------------------------------
# Initialisation
# ------------------------------------------------------------------


def init_channels(
    app: Flask,
    runtime: SessionRuntime,
    hook_manager: HookManager,
    config: AppConfig,
    *,
    project_catalog: Callable[[], ProjectCatalog] | None = None,
    resolve_session_cwd: Callable[[str], str | None] | None = None,
) -> None:
    """Wire channel adapters into the Flask app.

    Called once at API startup.  No-ops if no channels are configured.

    *project_catalog* and *resolve_session_cwd* are the two project
    collaborators ``/switch-project`` needs; both are composed in ``backend``,
    which imports this module, so they arrive here rather than being reached
    for. Both are optional so a test may wire the adapters alone.
    """
    global _runtime, _hook_manager  # noqa: PLW0603
    _runtime = runtime
    _hook_manager = hook_manager
    _projects.catalog_source = project_catalog
    _projects.resolve_session_cwd = resolve_session_cwd

    nc_cfg = config.channels.get("nextcloud-talk", {})
    if nc_cfg.get("enabled") and nc_cfg.get("bot_secret"):
        adapter = NextcloudTalkAdapter(
            bot_secret=nc_cfg["bot_secret"],
            nextcloud_url=nc_cfg.get("nextcloud_url", ""),
            allowed_backends=nc_cfg.get("allowed_backends"),
            host_header=nc_cfg.get("nextcloud_host_header"),
            trigger_keyword=nc_cfg.get("trigger_keyword", "@Mewbo"),
        )
        _registry.register(adapter)
        logger.info("Nextcloud Talk channel adapter registered")

    # -- Email channel (IMAP polling + SMTP replies) --
    email_cfg = config.channels.get("email", {})
    if email_cfg.get("enabled"):
        from mewbo_api.channels.email_adapter import EmailAdapter, EmailPoller

        email_adapter = EmailAdapter(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=email_cfg.get("smtp_port", 587),
            smtp_ssl=email_cfg.get("smtp_ssl", False),
            smtp_starttls=email_cfg.get("smtp_starttls", True),
            username=email_cfg["username"],
            password=email_cfg["password"],
            from_address=email_cfg.get("from_address"),
            allowed_senders=email_cfg.get("allowed_senders"),
            allowed_recipients=email_cfg.get("allowed_recipients"),
        )
        _registry.register(email_adapter)

        poller = EmailPoller(
            adapter=email_adapter,
            imap_host=email_cfg["imap_host"],
            imap_port=email_cfg.get("imap_port", 993),
            imap_ssl=email_cfg.get("imap_ssl", True),
            username=email_cfg["username"],
            password=email_cfg["password"],
            mailbox=email_cfg.get("mailbox", "INBOX"),
            poll_interval=email_cfg.get("poll_interval_seconds", 30),
            process_fn=_process_inbound,
        )
        poller.start()
        logger.info(
            "Email channel registered (polling {} every {}s)",
            email_cfg["imap_host"],
            email_cfg.get("poll_interval_seconds", 30),
        )

    hook_manager.on_session_end.append(_channel_completion_hook)

    app.register_blueprint(channel_bp)
    logger.info(
        "Channel webhook routes registered (platforms: {})",
        _registry.platforms() or "none",
    )
