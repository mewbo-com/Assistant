#!/usr/bin/env python3
"""Core data models and tool abstractions for Mewbo orchestration."""

from __future__ import annotations

import abc
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from langchain_community.document_loaders import JSONLoader
from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mewbo_core.common import MockSpeaker, get_logger, get_mock_speaker, get_unique_timestamp
from mewbo_core.components import build_langfuse_handler
from mewbo_core.config import get_config_value, get_version
from mewbo_core.contracts.types import ActionStepPayload, ToolInput

logging = get_logger(name="core.classes")
AVAILABLE_TOOLS: list[str] = ["home_assistant_tool"]

# Tool ids ``ToolUseLoop`` injects directly into ``bind_tools`` (session
# tools + inline agent-management/skill schemas) rather than sourcing from
# ``ToolRegistry`` — so they never reach ``set_available_tools``. Every
# executed tool call is converted to an ``ActionStep`` for ``TaskQueue``
# compatibility (``tool_use_loop.py:_tool_call_to_action_step``), so these
# ids must count as valid here or every such step logs a
# false-positive "not a valid Assistant tool" error. Defined here (classes.py
# is the base of the module, imported by all of them) rather than imported,
# to avoid a circular import. Kept in lockstep with where each is defined:
# ``exit_plan_mode.py``, ``update_todos.py``, ``spawn_agent.py``,
# ``skills.py``, ``structured_response.py``. ``tool_search`` is excluded —
# it IS registered through ``ToolRegistry`` (see ``tool_registry.py``).
INTERNAL_TOOL_IDS: frozenset[str] = frozenset(
    {
        "exit_plan_mode",
        "update_todos",
        "spawn_agent",
        "spawn_agents",
        "check_agents",
        "steer_agent",
        "activate_skill",
        "emit_result",
    }
)


@dataclass
class ToolResult:
    """Structured tool execution result."""

    content: str
    success: bool = True
    error: str | None = None
    truncated: bool = False
    original_length: int | None = None


def set_available_tools(tool_ids: list[str]) -> None:
    """Update available tool IDs for validation."""
    global AVAILABLE_TOOLS
    AVAILABLE_TOOLS = tool_ids


class ActionStep(BaseModel):
    """Action step with validation metadata."""

    title: str | None = Field(
        default=None,
        description="Short header summarizing the task for this step.",
    )
    objective: str | None = Field(
        default=None,
        description="Brief objective explaining why this step is needed.",
    )
    execution_checklist: list[str] = Field(
        default_factory=list,
        description="Short checklist of execution details for this step.",
    )
    expected_output: str | None = Field(
        default=None,
        description="Optional description of what success looks like.",
    )
    tool_id: str = Field(
        description=(
            "Specify the tool_id that should execute the action. "
            "Use only tool IDs listed under Available tools."
        )
    )
    operation: str = Field(description="Specify the execution type (get/set or execute).")
    tool_input: ToolInput = Field(
        description=(
            "Provide details for the action. If 'task', specify the task to perform. "
            "If 'talk', include the message to speak to the user."
        )
    )
    result: object | None = Field(
        alias="_result",
        default=None,
        description="Private field to persist the action status and other data.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class PlanStep(BaseModel):
    """High-level plan step produced by the planner."""

    title: str = Field(description="Short title for the step.")
    description: str = Field(description="One-paragraph description of the step.")


class Plan(BaseModel):
    """Plan with human-readable steps."""

    human_message: str | None = Field(
        alias="_human_message",
        default=None,
        description="Human message associated with the plan.",
    )
    steps: list[PlanStep] = Field(default_factory=list)


class TaskQueue(BaseModel):
    """Queue of executed tool steps and results."""

    human_message: str | None = Field(
        alias="_human_message",
        default=None,
        description="Human message associated with the task queue.",
    )
    plan_steps: list[PlanStep] = Field(default_factory=list)
    action_steps: list[ActionStep] = Field(default_factory=list)
    task_result: str | None = Field(
        alias="_task_result", default=None, description="Store the result for the entire task queue"
    )
    last_error: str | None = Field(
        alias="_last_error",
        default=None,
        description="Short description of the most recent tool failure.",
    )

    @field_validator("action_steps")
    @classmethod
    def validate_actions(cls, field: list[ActionStep]) -> list[ActionStep]:
        """Normalize and validate action steps."""
        for action in field:
            action.tool_id = action.tool_id.lower()
            action.operation = action.operation.lower()
            error_msg_list = []

            if action.tool_id not in AVAILABLE_TOOLS and action.tool_id not in INTERNAL_TOOL_IDS:
                error_msg_list.append(f"`{action.tool_id}` is not a valid Assistant tool.")

            if action.operation not in ["get", "set", "execute"]:
                error_msg = f"`{action.operation}` is not a valid operation."
                error_msg_list.append(error_msg)

            if action.tool_input is None:
                error_msg_list.append("Tool input cannot be None.")

            if error_msg_list:
                for msg in error_msg_list:
                    logging.error(msg)

        return field


ActionStep.model_rebuild()


class OrchestrationState(BaseModel):
    """State for the orchestration loop."""

    goal: str
    session_id: str | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    done: bool = False
    done_reason: str | None = None
    summary: str | None = None
    plan_approved: bool = False
    plan_path: str | None = None
    # Verifier-gated completion outcome. ``None`` = the gate never ran (no
    # spec, or inactive); ``True``/``False`` = a
    # ground-truth check passed/exhausted its retries. ``verify_attempts``
    # counts how many verifier runs this task drove (0 when the gate was
    # never active). Both survive to the ``AgentResult`` so a spawner sees an
    # honest done-claim rather than an invisible null.
    verified: bool | None = None
    verify_attempts: int = 0
    # The tool-envelope error code of a blocked-class condition the run never
    # recovered from — credentials, reachability, permission, quota. ``None``
    # (the overwhelming majority) means no such condition stood at the end.
    # Deliberately NOT folded into ``done_reason``: that vocabulary is a wire
    # contract every client already switches on, whereas this is an additive
    # fact the status layer reads to tell a run that FAILED from one that is
    # BLOCKED on something a user can go fix. Set by the loop, projected onto
    # the completion event, mapped to a status downstream.
    blocked_code: str | None = None

    def terminal_status(self) -> Literal["completed", "failed", "cancelled"]:
        """Project this settled state onto the terminal a spawner is told.

        ``done`` answers "did the loop stop", never "did the task succeed", and
        reading it as the latter is what let a doom-looped or budget-spent child
        report ``completed`` to its parent. A run that stopped short is
        ``failed``; only a genuine natural completion whose ground-truth check
        did not fail is ``completed``.

        ``verified is False`` is checked in its own right rather than trusted to
        the reason: it is the authoritative record that a ground-truth check ran
        and did not pass, and a claim contradicted by ground truth must not
        depend on a second field spelling it the same way. It is checked BEFORE
        cancellation because a contradicted claim is a substantive failure,
        whereas a stop is only a stop.

        A cancelled run is neither: nobody claimed the goal was reached and
        nothing contradicted a claim, so folding it into ``failed`` would report
        an error that never happened while ``completed`` reports a success that
        never happened. It gets the ``AgentStatus`` member that means what
        occurred. A ``completed``/``failed`` projection cannot express the one
        terminal a user causes directly, which is why there is a third member.

        The returned values are members of the hypervisor's ``AgentStatus``
        vocabulary — this is a NARROWING of that authority, not a vocabulary of
        its own, and it is exactly ``SettledStatus``. A halt reports as
        ``failed`` because ``AgentStatus`` has no "stopped short" arm; the
        precise reason is not lost — it rides the ``stop`` event's ``detail``
        and the attestation's ``done_reason``.

        Lives on the model rather than on whichever service happens to settle a
        run: the projection reads nothing but this state's own fields, so a
        second copy at another call site could only ever drift from this one.
        """
        if not self.done:
            return "failed"
        if self.verified is False:
            return "failed"
        reason = self.done_reason
        if isinstance(reason, str) and reason in CANCELLED_DONE_REASONS:
            return "cancelled"
        if isinstance(reason, str) and reason in UNACHIEVED_DONE_REASONS:
            return "failed"
        return "completed"


# ``OrchestrationState.done_reason`` values meaning the run STOPPED WITHOUT
# REACHING ITS GOAL. Every one of them sets ``done=True``, so none of them
# raises and none leaves a sticky error string of its own. Shared by the
# orchestrator's own completion path (attaching a structured diagnostic to a
# completion that would otherwise reach the store bare) and by
# :meth:`OrchestrationState.terminal_status` (a halted/budget-spent/
# ground-truth-failed child must report ``failed`` to its parent, never
# ``completed``) — a reason added to one consumer's vocabulary must reach the
# other, so it lives in ONE place rather than two independently-maintained
# copies.
UNACHIEVED_DONE_REASONS: frozenset[str] = frozenset(
    {
        "unmet_goal",
        "halted_no_progress",
        "verification_failed",
        "max_steps_reached",
        "max_iterations_reached",
        "budget_exhausted",
        "halted_agent_budget",
        "safety_blocked",
    }
)

# ``done_reason`` values meaning the run was STOPPED BY SOMEONE — deliberately
# NOT members of ``UNACHIEVED_DONE_REASONS`` above, because the two answer
# different questions and one consumer reads each: a cancelled run did not fall
# short of its goal, it was never allowed to pursue it. Kept beside that set for
# the same reason that one has a single home — a reason added to either
# vocabulary must be visible to whoever reads the other.
#
# Both spellings are accepted because both exist in the tree: the loop mints the
# one-L American ``canceled`` while the lifecycle vocabulary spells the status
# ``cancelled``, and a projection that recognised only one spelling would report
# a clean success the first time the other was minted.
CANCELLED_DONE_REASONS: frozenset[str] = frozenset({"canceled", "cancelled"})


class AbstractTool(abc.ABC):
    """Base tool with shared initialization helpers."""

    def __init__(
        self,
        name: str,
        description: str,
        model_name: str | None = None,
        use_llm: bool = True,
    ) -> None:
        """Initialize tool configuration."""
        tool_model = get_config_value("llm", "tool_model")
        default_model = get_config_value("llm", "default_model", default="gpt-5.2")
        self.model_name = cast(
            str,
            model_name or tool_model or default_model,
        )
        self.name = name
        self.description = description
        self.use_llm = use_llm
        self._id = f"{name.lower().replace(' ', '_')}_tool"
        session_id = f"{self._id}-tool-id-{get_unique_timestamp()}"
        logging.info(f"Tool created <name={name}; session_id={session_id};>")
        self.langfuse_handler = build_langfuse_handler(
            user_id=f"mewbo-{name}",
            session_id=session_id,
            trace_name=f"mewbo-{self._id}",
            version=get_version(),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        self.model = None
        if self.use_llm:
            # Imported at the call site, not at module top: this module sits
            # below `llm` in the layering everywhere else, and a module-top
            # import here is the edge that pins the whole LLM stack to the
            # package root.
            #
            # Safe HERE specifically, and the argument is per-site rather than
            # general (a lazy import is only safe when the imported module's
            # import-time side effects cannot re-enter the caller). `llm` pulls
            # in LiteLLM, whose module body reads a `.env` and populates
            # `os.environ`; this line runs long after import, and the config
            # this constructor depends on is already built by the time control
            # reaches it — `get_config_value` above and `get_logger` at module
            # scope both force it.
            from mewbo_core.llm.llm import build_chat_model

            self.model = build_chat_model(model_name=self.model_name)
        root_cache_dir = get_config_value("runtime", "cache_dir", default=".cache")
        if not root_cache_dir:
            raise ValueError("runtime.cache_dir is not set.")
        self.cache_dir = os.path.abspath(os.path.join(str(root_cache_dir), self._id))
        logging.debug("{} cache directory is {}.", self._id, self.cache_dir)

    def _save_json(self, data: object, filename: str) -> None:
        """Persist JSON data under the cache directory."""
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        filename = os.path.join(self.cache_dir, filename)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        logging.info(f"Data saved to {filename}.")

    def _load_rag_json(self, filename: str) -> list[Document]:
        """Load JSON content as documents."""
        logging.debug("RAG directory is {}.", self.cache_dir)
        logging.info(f"Loading `{filename}` as JSON.")
        filename = os.path.join(self.cache_dir, filename)
        filename = os.path.abspath(filename)
        loader = JSONLoader(file_path=filename, jq_schema=".", text_content=False)
        data = loader.load()
        return data

    def _load_rag_documents(self, filenames: list[str]) -> list[Document]:
        """Load and concatenate multiple JSON files."""
        rag_documents: list[Document] = []
        for rag_file in filenames:
            data = self._load_rag_json(rag_file)
            rag_documents.extend(data)
        return rag_documents

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Perform a state-changing action."""
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content="Not implemented yet.")

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Perform a read-only action."""
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content="Not implemented yet.")

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Execute the action based on the operation."""
        if action_step.operation == "set":
            return self.set_state(action_step)
        if action_step.operation == "get":
            return self.get_state(action_step)
        raise ValueError(f"Invalid operation: {action_step.operation}")


def create_task_queue(
    action_data: list[ActionStepPayload] | None = None,
    is_example: bool = True,
) -> TaskQueue:
    """Create a TaskQueue from serialized action data."""
    if action_data is None:
        raise ValueError("Action data cannot be None.")

    action_steps = [ActionStep(**action) for action in action_data]
    task_queue = TaskQueue(action_steps=action_steps)
    if is_example:
        del task_queue.human_message
    return task_queue


def create_plan(
    step_data: list[dict[str, str]] | None = None,
    is_example: bool = True,
) -> Plan:
    """Create a Plan from serialized step data."""
    if step_data is None:
        raise ValueError("Step data cannot be None.")
    steps = [PlanStep(**step) for step in step_data]
    plan = Plan(steps=steps)
    if is_example:
        del plan.human_message
    return plan


def get_task_master_examples(
    example_id: int = 0,
    available_tools: Sequence[str] | None = None,
) -> str:
    """Return serialized example plan data."""
    if available_tools is None:
        available_tools = AVAILABLE_TOOLS
    include_home_assistant = "home_assistant_tool" in available_tools
    if include_home_assistant:
        examples: list[list[dict[str, str]]] = [
            [
                {
                    "title": "Turn on strip lights",
                    "description": "Use Home Assistant to switch on the strip lights.",
                },
                {
                    "title": "Turn on heater",
                    "description": "Use Home Assistant to switch on the heater.",
                },
            ],
            [
                {
                    "title": "Check weather",
                    "description": "Use Home Assistant to retrieve today's weather details.",
                },
            ],
        ]
    else:
        examples = [[], []]
    if example_id not in range(0, len(examples)):
        raise ValueError(f"Invalid example ID: {example_id}")

    return create_plan(step_data=examples[example_id], is_example=True).model_dump_json()
