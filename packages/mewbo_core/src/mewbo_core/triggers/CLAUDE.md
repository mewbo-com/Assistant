> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `triggers/` — the durable wake contract

Scope: `spec.py` (the typed kind classes) · `policy.py` (admission ceilings) ·
`store.py` / `store_mongo.py` · `session_tool.py` (the agent-facing surface).

**Triggers are the hypervisor's DURABLE peer.** The hypervisor is the ephemeral
intra-session governor: one run, children resolved to an absorbing `AgentStatus`. A
`TriggerSpec` survives process death in a store and re-engages a session later — it
is to reverse-invocations what the run registry is to runs. Both are
absorbing-state subsystems, which is why the hard-termination cascade disarms a dead
session's triggers (`session/CLAUDE.md`).

**Trigger behaviour is DECLARED via typed kind classes, NEVER an LLM-authored
script.** The agent picks a `kind` and fills its fields; it never authors scheduling
or matching logic. That guardrail *is* the subsystem — agent self-scheduling via
model-written code is precisely what this refuses to ship — so never add a
`custom`/eval kind.

This package is also the reference implementation of the repository's
discriminated-union principle. **If you are about to write an `if kind ==` switch
anywhere in the tree, read `spec.py` first.**

## Data-owned kinds, zero dispatch

**Five kind classes, each owning its own validators and due-ness/match method
(`spec.py`):** `TimeAtTrigger` · `CronTrigger` · `CiWorkflowTrigger` ·
`ForgePrTrigger` · `WebhookTrigger`. `next_fire_at` is the clock method, `matches`
the payload method, `verify` the webhook-signature method; the base `next_fire_at`
returns `None`, since event kinds never poll the clock. A single
`Field(discriminator="kind")` union plus `parse_trigger()` (= `TriggerSpec.parse`)
is the ONE parse seam — any service-side `if kind ==` drifts out of sync the moment
a kind gains a field, so there is none, anywhere.

**Models never import I/O:** the current time, a webhook's headers/body, a
normalized CI/PR payload all arrive as method ARGS (also why tests inject a fixed
`NOW` instead of patching a clock).

**`fired` is a transition, not a status:** two resting statuses (`armed` ⇄ `paused`,
freely interchangeable) and four absorbing terminals
(`completed`/`failed`/`cancelled`/`expired`); `transition()` guards illegal moves
and terminals absorb; `is_due` polls positive only for `armed`.

**`record_fire` never auto-fails.** An errored fire bumps `fires` + records
`last_error` but leaves the trigger `armed` — retry/backoff is the SERVICE layer's
call (the app-side watcher reads `last_error` to re-arm/cancel/leave), never the
model's. It auto-completes only when `fires` reaches `max_fires`.

**Cron missed-fire is anchored, not herd-prone:** `CronTrigger.next_fire_at` anchors
on `last_fired_at`/`created_at`, NOT `now`, so after downtime a due trigger fires
exactly ONCE and re-anchors on that fire — no thundering-herd catch-up of every
missed slot.

## Admission ceilings

**`TriggerPolicy` (`policy.py`) is the ONE home of admission ceilings** —
`max_armed_per_session`, `max_fires_cap`, `default_expiry`,
`cron_min_interval_seconds`, `webhook_payload_max_bytes`, every one config-tunable
per deployment, NEVER hardcoded in behaviour code. `admit()` validates AND stamps
defaults (currently `expires_at`) in one step, and **never mutates** the spec — a
stamped default returns a `model_copy`. The app builds the instance from `AppConfig`
and DI-hands it to the tool; this module owns no config I/O and no store access.

## The agent-facing surface

**`schedule_trigger` SessionTool (`session_tool.py`)** is the agent's
arm/list/cancel surface — **terminal-free like `update_todos`**
(`should_terminate_run()` always `False`; arming is a normal step, not a plan-mode
exit). Built through the ordinary `SessionToolRegistry` plugin path (constructor
gets `session_id` + `event_logger`), so `agent_id` is an OPTIONAL kwarg —
inline-attach to get live agent attribution on the `trigger_armed` event.

Per-kind validation is DELIBERATELY not duplicated in `ScheduleTriggerArgs` (it
holds only the per-operation "arm needs kind+wake_prompt" guard) — kind requirements
belong to `parse_trigger` and the concrete subclass alone. Errors return the shared
`{"error":{code,message}}` envelope (codes `validation`/`policy`/`not_found`) that
`_session_tool_error_envelope` reclassifies as a failed step; a foreign-session
`trigger_id` reads `not_found` uniformly, so there is no existence leak.
`provenance.step` is ALWAYS `None` — no per-step turn counter exists anywhere in the
engine.

**Delivery rides the ordinary `SessionToolRegistry`, NOT `extra_session_tools`.**
Root-only `extra_session_tools` injection is structurally unreachable by a SPAWNED
sub-agent (`_build_child_loop` forwards only `session_tool_registry=`), so an
app-builder AgentDef listing `schedule_trigger` in its `tools:` would never get it.
The app pushes its store+policy ONCE via `register_schedule_trigger_provider`
(down-only, mirroring `register_app_submitter`/`register_builtin_root`; gated on
`triggers.enabled`), and every `Orchestrator` registers the resulting
`SessionToolFactory(unconditional=True)`.

**`unconditional` is a THIRD gate on `SessionToolFactory`** beside
`requires_capabilities`, and **its ceiling is `strict_tool_scope`, NOT bare
`has_explicit_scope`.** `build_for`/`ids_for` take `strict_tool_scope`; an
unconditional tool is capped by a non-empty allowlist ONLY when the scope is STRICT
(an authoritative AgentDef `tools:`). Under a PERMISSIVE scope — the FE default,
where `context.mcp_tools` (139 entries on a real console/Aura session) is a ceiling
over MCP tools ONLY and never lists built-ins — the unconditional tool STILL
surfaces. **Getting this wrong silently breaks every mobile alarm/reminder flow**
(Aura "set an alarm in 10 min" arms a time trigger through `schedule_trigger` on a
permissive root). So: app-builder AgentDef (strict, names it) ✓, differently-scoped
strict child ✗, permissive FE root (mcp_tools, no `schedule_trigger`) ✓, plain root
✓. The capability gate keeps the plain `has_explicit_scope` ceiling — the relaxation
is unconditional-only.

**The two flags COMPOSE: `unconditional` sets the CEILING, `requires_capabilities`
sets the GATE.** `unconditional` relaxes the allowlist ceiling to strict-scope-only;
it does NOT waive the capability. Setting both means "default-on, but only for a
session that advertised the capability" — the ONLY way to reach a console/Aura ROOT,
which always sends some `context.mcp_tools` (`[]` at create) and so can never
satisfy the capability gate on its own. `present_ui`
(`builtin_plugins/generative_ui`) is the first tool shaped this way; without the
capability half it would bind its eleven component schemas on every headless drive
that can render nothing. `schedule_trigger` declares NO capabilities, so it is
untouched — asserted against its REAL factory, not a fixture, in
`tests/test_session_tools.py`.

**Both `build_for` (the loop) AND `ids_for` (the operator's `{{ tools }}` catalog
via `_resolve_instruction_tools`) must be passed the SAME `strict_tool_scope`, or
the catalog drifts from what the agent holds.** `ask_user_question` stays on
`extra_session_tools` — root-only is its documented intent.

**Test trap:** `configs/app.json` enables triggers, so importing `backend.py` (any
apps test, any order) sets that process global permanently, and `schedule_trigger`
then leaks into EVERY permissive/un-scoped core `Orchestrator` session's tools. An
autouse fixture in `tests/conftest.py` (`_reset_schedule_trigger_provider`) resets
`_TRIGGER_TOOL_PROVIDER` per test so exact-tool-set assertions stay hermetic; a test
that WANTS it registers it in its own body.
