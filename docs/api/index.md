# Get Started

## Make your first API request

The REST API is Mewbo's programmable surface. The web console, the terminal CLI, the MCP server, and the mobile app all drive sessions through it, so your own client reaches the same orchestration loop they do. A session is the core resource.

## Base URL

Your own stack serves the API on port `5125` by default:

```
http://localhost:5125
```

`uv run mewbo-api` serves port `5124` instead, for local development. See [Docker Compose](../deployment-docker.md).

## Authentication

Every protected route requires an API key. Send it in the `X-API-KEY` request header:

```
X-API-KEY: <your-api-key>
```

The key is checked against your configured master token, `api.master_token`, or against a revocable key you mint with [POST /api/keys](endpoint:POST /api/keys). Prefer the minted key, since revoking is instant.

Server-Sent Events routes take the key as an `api_key` query parameter instead. Browser `EventSource` cannot set custom headers.

## Your first request

This walkthrough runs one query end to end. Export your key first to copy and paste the examples:

```bash
export MEWBO_API_KEY="<your-api-key>"
```

### 1. Create a session

A session holds one conversation. [POST /api/sessions](endpoint:POST /api/sessions) creates an empty one:

```bash
curl -X POST http://localhost:5125/api/sessions \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The response carries the new session id:

```json
{ "session_id": "9e2d47c1a0b34f12" }
```

The request body is optional. You can bind a project, apply a lookup `session_tag`, or persist initial context. [Building a Client](building-a-client.md) has the field list.

### 2. Send a query

[POST /api/sessions/{session_id}/query](endpoint:POST /api/sessions/{session_id}/query) starts a run. The body needs a `query` field:

```bash
curl -X POST http://localhost:5125/api/sessions/9e2d47c1a0b34f12/query \
  -H "X-API-KEY: $MEWBO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "List the Python files in this project."}'
```

The run is asynchronous. It returns `202 Accepted` right away and the work continues on the server:

```json
{ "session_id": "9e2d47c1a0b34f12", "accepted": true }
```

A session runs one turn at a time. A second query while one is active returns `409`.

### 3. Read the results

Poll for events, or attach to the live stream.

Poll with [GET /api/sessions/{session_id}/events](endpoint:GET /api/sessions/{session_id}/events). Pass `after` with the last event's timestamp to fetch only newer ones:

```bash
curl "http://localhost:5125/api/sessions/9e2d47c1a0b34f12/events" \
  -H "X-API-KEY: $MEWBO_API_KEY"
```

The response carries the event list plus the authoritative run state:

```json
{
  "session_id": "9e2d47c1a0b34f12",
  "status": "completed",
  "running": false,
  "done_reason": "completed",
  "events": [ { "type": "assistant", "payload": { "content": "..." } } ]
}
```

Or attach to the live stream with [GET /api/sessions/{session_id}/stream](endpoint:GET /api/sessions/{session_id}/stream). It uses Server-Sent Events, so the key goes in the query string:

```bash
curl -N "http://localhost:5125/api/sessions/9e2d47c1a0b34f12/stream?api_key=$MEWBO_API_KEY"
```

The stream replays the stored backlog once, then pushes each new event as it is appended. A terminal `stream_end` event marks the run finished. [Building a Client](building-a-client.md) covers reconnecting mid run.

## What is in this section

<div class="ms-grid ms-grid--5">

<a class="ms-card" href="building-a-client/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:code" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Building a Client</span>
  <span class="ms-card__body">The session lifecycle in full: queries, streaming, follow-ups, steering and termination. Start here.</span>
</a>

<a class="ms-card" href="structured-outputs/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:braces" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Structured Outputs</span>
  <span class="ms-card__body">Constrain a run to a JSON Schema and get a validated object back, with fast synthesis and token streaming.</span>
</a>

<a class="ms-card" href="automation/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:workflow" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Automation</span>
  <span class="ms-card__body">Assign a bot to an issue or pull request from CI and it picks up the work.</span>
</a>

<a class="ms-card" href="triggers/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:alarm-clock" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Triggers</span>
  <span class="ms-card__body">Arm a durable wake on a clock, a CI run, a pull request or a webhook instead of polling.</span>
</a>

<a class="ms-card" href="device-tools/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:smartphone" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Device Tool Bridge</span>
  <span class="ms-card__body">A connected client declares on-device tools the agent can call, such as sending an SMS.</span>
</a>

</div>

## The full reference

The complete endpoint catalog is the [REST API Reference](../rest-api.md), generated from the live server so it always matches the running code.

## Next steps

Write your own client against [Building a Client](building-a-client.md), which walks the session lifecycle in full.
