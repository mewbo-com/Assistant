# Building a Client

## Drive a session over REST

Every Mewbo client drives the same engine over the same REST surface. Build against these endpoints and your client behaves like the console, the CLI, and the mobile app.

The other option is to embed the core engine in your own Python process instead of going over HTTP. That path is covered [at the end](#embedding-the-engine-in-process).

The examples below use `$MEWBO_API_KEY` and `$MEWBO_API_URL`. [Get Started](index.md) covers the key and the base URL.

## The session lifecycle

A session is the core resource. It holds one conversation and its full event history. Create a session, send it a query, follow the run, then keep asking or steer as it goes.

### Create a session

[POST /api/sessions](endpoint:POST /api/sessions) creates an empty session and returns its id.

```bash
curl -X POST "$MEWBO_API_URL/api/sessions" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

```json
{ "session_id": "9e2d47c1a0b34f12" }
```

The body is optional. Four fields earn their place.

| Field | Purpose |
|---|---|
| `session_tag` | A stable lookup key. Resolve or reuse a session by tag instead of tracking the id yourself. |
| `project` | Bind the session to a configured project, which sets its working directory, tools, and skills. |
| `context` | Initial context to persist, such as `model`. |
| `fork_from` | Create the session as a branch of an existing session's history. Pair with `fork_at_ts` to branch at a point. |

An explicit `cwd` is also accepted behind the `api.allow_external_cwd` flag. It exists for external workspace managers that anchor a session in their own worktree.

### Send a query

[POST /api/sessions/{session_id}/query](endpoint:POST /api/sessions/{session_id}/query) starts a run. Only `query` is required.

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/query" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize the open pull requests."}'
```

The run is asynchronous. The endpoint returns straight away and the work continues on the server.

| Status | Meaning |
|---|---|
| `202` | A run started. Follow it via the events or stream endpoints. |
| `200` | An inline slash command (`/status`) was handled without a run. |
| `409` | A run is already active. A session executes one turn at a time. |
| `400` | The `query` field was missing. |

`context.model` picks the model for the turn, and `context.fallback_models` takes a ladder to escalate down when the primary keeps failing. `mode` takes `plan` or `act`. Inline `@file`, `@dir`, `@diff`, and `@url` references expand into bounded context before the run, so local context needs no separate read step.

### Follow the run

**Poll** [GET /api/sessions/{session_id}/events](endpoint:GET /api/sessions/{session_id}/events) with `after` set to the timestamp of the last event you processed, so only newer ones come back. `truncate=1` caps large free-text payloads. Alongside the events the response carries `status`, `running`, `done_reason` and `recoverable`, so run state is never reconstructed from the timeline.

**Stream** [GET /api/sessions/{session_id}/stream](endpoint:GET /api/sessions/{session_id}/stream) to get each event the instant it is appended, after one replay of the stored backlog. Pass `after` to reconnect without downloading the transcript again.

```bash
curl -N "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/stream?api_key=$MEWBO_API_KEY&after=1720000000.0"
```

The bound is inclusive, so a reconnect may replay one event you already hold. Deduplicate by comparing the event itself rather than skipping whatever shares the cursor's timestamp.

The stream also pushes a `session_state` frame carrying the polling response's run state plus `title`, `terminated`, and `terminated_at`. It arrives twice, once after the replay and again just before the connection closes, so a client reading only that frame still knows where the run stands.

A terminal `stream_end` event marks the run finished. Drain the stream through it, because the final `completion` event lands just before.

### Event kinds

Each event has a `type` and a `payload`. These are the kinds a client handles.

| Event | When it fires |
|---|---|
| `action_plan` | A plan was generated. Steps are `{title, description}`. |
| `tool_result` | A tool ran. Carries `tool_id`, `operation`, `tool_input`, and `result`. |
| `permission` | An approval was requested or a decision was recorded. |
| `sub_agent` | A sub-agent changed state. |
| `step_reflection` | The reflector asked for a revision. |
| `assistant` | Final assistant output for the turn. |
| `completion` | The run reached a terminal state. Carries the final `status`. |

`tool_id`, `operation` and `tool_input` are a stable contract.

### Send a follow-up

Send another query to the same session id. The new turn inherits the session's history and context. Wait for the previous run to reach a terminal state first. A query sent while a run is still active returns `409`. Steer the run instead if you need to add input mid flight.

### Steer or interrupt a run

[POST /api/sessions/{session_id}/message](endpoint:POST /api/sessions/{session_id}/message) enqueues a steering message into an active run. The `text` field is required.

```bash
curl -X POST "$MEWBO_API_URL/api/sessions/9e2d47c1a0b34f12/message" \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Focus on the auth module only."}'
```

The same endpoint does double duty. On an active run the text steers it and the call returns `202`. On an idle or finished session it starts a fresh run with that text as the query and returns `200` with a new `run_id`. Only a terminated session rejects the message.

[POST /api/sessions/{session_id}/interrupt](endpoint:POST /api/sessions/{session_id}/interrupt) signals the current tool step to pause. Interrupting an idle session is a no-op that returns `200`.

### Terminate a session

Interrupting stops a run and leaves the session steerable. Terminating ends the session itself.

[POST /api/sessions/{session_id}/terminate](endpoint:POST /api/sessions/{session_id}/terminate) is irreversible. No call undoes it.

```json
{
  "session_id": "9e2d47c1a0b34f12",
  "status": "terminated",
  "terminated_at": "2026-07-13T18:24:10.882001+00:00",
  "cancelled_triggers": 2
}
```

`cancelled_triggers` counts the [triggers](triggers.md) armed on the session. The same call cancels them, so a terminated session can never be woken later. The call is idempotent, and terminating an already terminated session returns the same original values.

After termination, every mutating route on that session returns `410 Gone`. That covers a new query, a steering message, recovery, forking, and plan approval.

```json
{ "error": { "code": "session_terminated", "reason": "Session is permanently terminated", "retryable": false } }
```

Termination is not deletion. The transcript stays fully readable, and `GET /api/sessions/{session_id}/events`, the stream, export, and share all keep working as before. What is gone is the ability to make the session do anything else.

## Client capability negotiation

A client advertises the UI primitives it can render with the `X-Mewbo-Capabilities` request header. The value is a comma separated list of capability ids such as `stlite`. The API writes that list onto the session's context event, and the orchestrator filters which agent types, skills, and session tools the model sees.

Omit the header and no capability-gated surface appears. Without `X-Mewbo-Capabilities: stlite`, for instance, a session exposes no widget building agent type, skill or tool.

`generative_ui` is the one most clients want. It lays an answer out as a [panel](../web/panels.md) of eleven components, emitted as a `generative_ui` transcript event. The event carries the component tree and a plain text rendering computed on the server. Printing that text is enough, and it is what the terminal and the MCP server do.

Do not advertise the capability and then drop the event. The capability is what binds the tool at all, so a claim with no surfacing spends a step on output nobody sees.

Send the header on every request that creates or drives a session, not only on create. The orchestrator reads capabilities from the most recent context event, so the grant tracks the latest one. Session recovery injects that context again on the server, so a recovered session keeps its grant.

## Session utilities

Fork a session from any point to branch a conversation.

```
POST /api/sessions/{session_id}/share     returns { token }
GET  /api/share/{token}                    fetch shared session data (no auth)
GET  /api/sessions/{session_id}/export     download the full payload
POST /api/sessions/{session_id}/archive    archive; DELETE unarchives
```

List and inspect what a project exposes with [GET /api/projects](endpoint:GET /api/projects), [GET /api/tools](endpoint:GET /api/tools), and [GET /api/skills](endpoint:GET /api/skills). Upload files to inject into a session's context with [POST /api/sessions/{session_id}/attachments](endpoint:POST /api/sessions/{session_id}/attachments).

## Embedding the engine in-process

The core is a library, so you can run the engine inside your own Python process and skip the network hop.

```python
from mewbo_core.common import get_logger
from mewbo_core.permissions import approval_callback_from_config, load_permission_policy
from mewbo_core.loop.session_runtime import SessionRuntime, parse_core_command
from mewbo_core.session.session_store import SessionStore
from mewbo_core.tooling.tool_registry import load_registry

logger = get_logger("client")

session_store = SessionStore()
tool_registry = load_registry()
runtime = SessionRuntime(session_store=session_store)

session_id = runtime.resolve_session(session_tag="client")
user_text = "Hello from the client"
command = parse_core_command(user_text)
if command:
    logger.info("Handled command: {}", command)
else:
    result = runtime.run_sync(
        session_id=session_id,
        user_query=user_text,
        tool_registry=tool_registry,
        permission_policy=load_permission_policy(),
        approval_callback=approval_callback_from_config(),
    )
    logger.info("Task result: {}", result.task_result)
```

Two things the sample does not show. `parse_core_command()` handles the core slash commands `/compact`, `/status` and `/terminate`, so a client never implements them. `run_sync()` runs a turn synchronously, and `start_async()` with `load_events(after=...)` gives the polling flow instead.

The pieces live in [`session_store.py`](repo:packages/mewbo_core/src/mewbo_core/session/session_store.py), [`session_runtime.py`](repo:packages/mewbo_core/src/mewbo_core/loop/session_runtime.py), [`tool_registry.py`](repo:packages/mewbo_core/src/mewbo_core/tooling/tool_registry.py) and [`permissions.py`](repo:packages/mewbo_core/src/mewbo_core/permissions.py). The [Architecture Overview](../core-orchestration.md) and [Session Runtime](../session-runtime.md) cover the layout and how to add a local tool or a channel adapter.

## The full reference

Every route, with its full parameter list, response shapes, and a runnable request sample, lives in the generated [REST API Reference](../rest-api.md).

## Next steps

- [Structured Outputs](structured-outputs.md) returns schema-validated objects instead of prose.
- [Device Tool Bridge](device-tools.md) lets your client expose tools the agent can call on the device.
- [Automation](automation.md) drives issue pickup and PR workflows through the API.
