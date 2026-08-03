> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `agents/` — admission, delegation, and the agent lifecycle

Scope: `hypervisor.py` · `spawn_agent.py` · `agent_context.py` ·
`attestation.py` · `agent_registry.py` · `built_in_agents.py`. A closed
subsystem: these modules reference each other's types and are meaningless apart.

Owns **automaton A** of the three (parent file → "Three automata, not one"): the
agent lifecycle, from admission to an absorbing terminal.

## The lifecycle vocabulary is SIX members, four of them terminal

`AgentStatus` (`hypervisor.py`) is `submitted · running · completed · failed ·
cancelled · rejected`, sourced from A2A v1.0; the last four are absorbing.
`ACTIVE_STATUSES` = `{submitted, running}` is the non-terminal set, and every
hypervisor-side site (`cleanup`, `cancel_pending`, `collect_running`) reads that
constant rather than comparing against one literal. A2A spells the active state
`working`; this codebase spells it `running` — do not assume the two are
interchangeable when reading A2A material against this code.

**Do NOT add a seventh member for "accepted but not started".** A2A has no
`queued`/`pending`, and `SUBMITTED` is specified as *"task acknowledged and
accepted"*, which already IS that state. The vocabulary is mirrored in three
clients (console `agentStatus.ts`, CLI `agent_transcript_hub.py`, Aura
`TranscriptReducer.kt`), so a new member costs five surfaces and buys a synonym.
`agentStatusAlignment.test.ts` pins the console mirror and is meant to fail if you
change the vocabulary.

Three RESULT vocabularies sit beside the lifecycle one:

| Vocabulary | Members | Answers |
|---|---|---|
| `AgentStatus` | 6 | where a live agent IS |
| `AgentResultStatus` | 5 | what the child REPORTED |
| `SpawnOutcomeStatus` | 6 | how one spawn ATTEMPT ended |

**`AgentResultStatus.partial` is minted nowhere.** It is the one member able to
express "advanced the work but did not finish it", and no producer sets it —
`OrchestrationState.terminal_status()` returns `completed`/`failed`/`cancelled` by
declared type. So a budget-spent child and a crashed child reach the parent under
the same word, and the difference survives only in the `stop` event's `detail` and
the attestation's `done_reason`. `SpawnOutcomeStatus` deliberately is NOT a
superset of `AgentResultStatus`: it drops that dead member and reports the terminal
it SETTLED (`SettledStatus`). Minting `partial` for the budget-exhausted subset, or
deleting it, is a change to `AgentResultStatus` AND three client mirrors, not to an
alias. `tests/test_child_settlement_parity.py` pins the containment, so widening
any of these vocabularies fails there rather than silently changing what a spawn
can report.

## Communication Units — what the envelope guarantees

`AgentResult` is modelled on the Communication Unit idea from the multi-agent
literature. The envelope transferred; the guarantee the name implies did not.

- **The ENVELOPE is real and richer than the paper's.** `AgentResult` carries
  `content`, `status`, `steps_used`, `artifacts`, `warnings`, `summary`,
  `attempts`, `summary_kind`, `attestation_hash`, `verified` and `verify_attempts`,
  so a parent reads `verified=False` or `attempts=3` without parsing prose.
- **The COMPRESSION is not.** `summary` is the child's own final text — or its
  compaction summary when that text is empty — hard-truncated at 500 characters,
  with no compression pass anywhere on that path. **A Communication Unit is earned
  by a compression step, not conferred by a field name; a truncation is not a
  summary.** The one place the engine genuinely produces one is the forced closing
  turn a depth > 0 child takes when its final response is empty (`loop/CLAUDE.md` →
  "No agent halts into a stump").
- **`summary_kind` is DECLARED, never enforced.** Stamped from the spawner's
  optional declaration; nothing checks that a unit marked `evidence` contains
  evidence, and nothing instructs the child to produce that shape. It describes
  what the caller wanted, not what arrived.
- **Mewbo is a TREE; the chain guarantee belongs to a CHAIN.** `spawn_agent` fans
  out siblings that never see one another's unit; results converge only at the
  parent, and tree-structured communication is exactly the topology that cannot
  carry a multi-hop chain, because hop two needs what hop one holds. The
  compensation is hub-and-spoke — the root's system prompt carries a tree rendered
  ONCE before the loop starts, and `check_agents` re-reads `render_agent_tree()`
  live to collect results and re-dispatch — which routes every hop through the
  parent's context window, the cost a chain exists to avoid. **Do not build a
  fan-out that expects chain semantics.**

## Privilege attenuation narrows through ONE seam

`AgentContext._narrow(parent, requested, rank_table)` is the single narrowing
primitive, shared by `capability_mode` and `workspace_mode`; `atomic` is a plain
OR. Narrowing is monotonic down the tree — a descendant can never re-widen an
ancestor's ceiling — and that property is what the delegation firebreak rests on. A
third narrowable axis gets a rank table and goes through `_narrow`; it does not get
its own comparison.

`_build_child_loop` forwards `session_tool_registry=` and takes no
`extra_session_tools` parameter at all. That is the structural reason
`ask_user_question` is root-only (`tooling/CLAUDE.md`): adding the parameter would
hand a depth ≥ 1 agent a direct line to the human principal, which is exactly the
authority a sub-agent is not meant to hold.

## Admission laws (`hypervisor.py` + `spawn_agent.py`)

**`rejected` may ONLY be returned for a decision that is identical on retry.** A2A
defines REJECTED as *"the agent has decided to not perform the task"*, and it is
ABSORBING — spending it on transient capacity makes a shortage permanent by
definition. Unknown `agent_type`, unresolvable project, unavailable model:
permanent, `rejected`. **No free slot is `EAGAIN`, not `EINVAL`** — it defers, it
never rejects. `SpawnRefusalCode` names `capacity` precisely to draw that line:
every other code is permanent, and re-issuing the same call unchanged fails
identically.

**A refusal is a FAILED step.** The spawn branch must set `session_tool_error`; a
refusal that records `success=True` is invisible to the failure nudge, to doom-loop
tracking, and to `permanence`. `MockSpeaker` is a one-field NamedTuple, so a status
cannot ride on the return value — the signal travels via `success=False`, not by
widening that type (which touches every tool).

**A deferred unit MUST be registered.** `collect_running` is the gate that stops a
root finishing with outstanding children, and it can only see the registry. An
unregistered unit is invisible to the one mechanism built to catch exactly this,
and `check_agents(wait=true)` has no concept of an expected total — a clean 20/20
tree satisfies the wait while six units were never attempted.

**Waiting is free; only RUNNING consumes a slot.** Depth ≥ 1 spawns hold their slot
while blocked on a child, so naive blocking admission deadlocks: N parents each
awaiting a slot the others hold. The answer is the kernel's — a task blocked on I/O
leaves the runqueue. `release()` is the dispatch pump, it is already the completion
path, and it must fire EXACTLY ONCE per acquired slot. NOT `hooks.on_agent_stop`,
which fires while the slot is still held; NOT the session event bus, which is
drop-oldest on overflow and a dropped wakeup wedges the queue forever.

**Acceptance is atomic; dispatch is staggered.** A fan-out is a gang: accept every
task or none, then start them as capacity allows. That is what makes a terminal
step's precondition satisfiable by construction rather than by luck.

**SEMANTICS AND ITS DESCRIPTIONS MOVE TOGETHER.** Changing admission semantics
means changing `loop.yaml`, both spawn schemas, every fan-out AgentDef, the console
renderer and `docs/core-orchestration.md` in the SAME change — otherwise the
prompts confidently instruct the model to do something the code no longer does,
and every one of those surfaces reads as correct in isolation.

## Delegation governance

- **Contracts must be cashable.** `DelegationContract` (`hypervisor.py`, beside
  `AgentResult`) carries only fields with a live in-process signal — steps + wall
  enforced, `max_tokens` advisory (usage_metadata unreliable), cost deliberately
  absent. Never add a contract field whose enforcement signal doesn't exist yet.
- **Attestation rides the event pipeline, or nowhere.** `type:"attestation"` events
  through the terminated-gated `append_event` — never a new writer or uploader. It
  is tamper-EVIDENCE (in-place edits of retained records), not tamper-proof: suffix
  deletion is undetectable and there is no signing.
- **Progress signalling never feeds its own verdict back** — `WriteProgressSignal`
  is observe-only telemetry; its two-gate arming lives beside the class in
  `llm/CLAUDE.md`.
- A child's budget wrap-up and its terminal projection belong to the driver:
  `loop/CLAUDE.md` → "No agent halts into a stump" and "`state.done` means the loop
  stopped".

## The spawn bridge

`hypervisor.py:AgentHypervisor` is the only place sub-agents register, cancel, get
budget warnings, and resolve to their `AgentResult`. `spawn_agent.py` bridges the
LLM-facing tool schema to the hypervisor. **Schema fields stay
backwards-compatible** — removing a field breaks every running agent that already
learned it.

- **`spawn_agent`/`spawn_agents` are injected via the loop's own
  `_spawn_agent_tool`, NOT through `filter_specs`, so they must re-check scope
  themselves.** The loop gates creation on `can_spawn` (depth) AND the agent's
  allowlist: an explicit `tools:` list omitting `spawn_agent` disables delegation,
  an ABSENT one (`None`) stays unrestricted — that allowlist is THREE-STATE and
  reading it by truthiness is fail-open (`tooling/CLAUDE.md`). Without the re-check
  a leaf agent whose allowlist is build+submit only could still delegate and recurse
  into copies of itself. **RULE: any tool injected outside `filter_specs` escapes
  the allowlist/denylist and must enforce scope at its own injection seam.**
- **The `sub_agent` lifecycle event carries two ADDITIVE keys beyond `detail`.**
  `detail` is only the `done_reason` on `stop`; the child's real result rides a
  capped `summary` key, so a consumer can project what the child answered.
  `agent_type` (the spawned AgentDef name) rides on BOTH `start` and `stop`, stored
  on the `AgentHandle` so the background `stop` from `_run_child_lifecycle` — which
  holds only the handle — can stamp it, and omitted entirely for an ad-hoc spawn
  with no `agent_type`. A trace projection needs the LANE identity, which a model
  name can never carry. Both keys are additive: a consumer reading only the
  pre-existing keys is untouched.
- **Batch fan-out (`spawn_agents`) is a thin wrapper, NOT a new engine.**
  `spawn_agent` admits one task per call, so a fast tier that serializes delegation
  across turns degrades an N-way fan-out into N round-trips. `run_async` and
  `run_batch_async` both funnel through one `_spawn_one(args, *, blocking_admit)`,
  so every entry takes the SAME per-task fields (validated at definition by
  `SpawnAgentTask`, `extra="forbid"`) and rides the EXISTING hypervisor admission +
  non-blocking root lifecycle. The whole fan-out is accepted in ONE atomic
  `accept_batch`; whatever does not fit waits at `submitted` and is dispatched by
  `release()` as siblings settle, so an over-subscribed call DEFERS its surplus
  rather than refusing it. `blocking_admit` names the CALLER SHAPE, not an admission
  mode: a single deliberate `spawn_agent` takes `critical` queue precedence and one
  entry of a wide batch takes `normal`, so a targeted delegation does not starve
  behind a 26-way fan-out. Slot ownership belongs to the lifecycle manager, which
  calls `release()` exactly once on the completion path.
- **Delegation-layer bounded retry complements the model-fallback ladder:** the
  ladder heals *within* one `ToolUseLoop` call; bounded retry re-delegates a whole
  child whose loop *died*. Opt-in `retry: {max, on, backoff}` on the spawn schema,
  **DEFAULT OFF** (`max == 0` ⇒ byte-identical single-attempt path).
  `spawn_agent.py:RetryPolicy` (atomic, validated via `from_value`, never raises) +
  `SpawnAgentTool._drive_with_retry` wrap the existing admit→run→resolve, shared by
  BOTH spawn paths. Four invariants:
  1. **Reuse, don't reinvent escalation** — each attempt builds a FRESH
     `ToolUseLoop` (`_build_child_loop`) so model-level recovery rides the fresh
     fallback ladder; `RetryPolicy.classify_cause` reuses `RetryStrategy.classify`'s
     taxonomy to bucket a failure into `timeout`/`failed`.
  2. **One slot for the whole sequence** — the semaphore slot acquired once in
     `run_async` is HELD across attempts (re-admission re-uses it), so concurrency
     stays bounded and there is no release/reacquire race.
  3. **`cancelled`/`rejected` are structurally unretryable** — `rejected` returns
     before the loop; `CancelledError` is caught and re-raised *before* the generic
     retry `except`.
  4. `attempts` is additive on `AgentResult`+`AgentHandle` (default 1) → surfaced in
     `check_agents` and the agent tree (`N attempts` marker only when >1).

  The driver keys off `ToolUseLoop.run` RAISING on resilience exhaustion rather than
  returning — `llm/CLAUDE.md` owns that terminal and names this consumer.

## Agent definitions

`agent_registry.py` is the agent-definition registry, loaded from built-in agents +
plugins. Wiki AgentDefs (`wiki-indexer`, `wiki-page-writer`, `wiki-qa`) are
capability-gated — they appear only when the session advertises
`client_capabilities: ["wiki"]`.
