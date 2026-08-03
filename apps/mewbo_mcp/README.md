# Mewbo MCP Server

Standalone [MCP](https://modelcontextprotocol.io) server that wraps the Mewbo REST API as a set of tools, so external agents (Claude Code and other MCP-compatible coding agents) can create and control Mewbo sessions, read session history at tiered detail levels, query the Agentic Wiki, and run Mewbo Search across saved multi-source workspaces.

- No components are explicitly tested for safety or security. Use with caution in production.
- For full deployment setup, see `docs/getting-started.md`.

## Run

```bash
uv sync --extra mcp      # or: uv sync --all-extras
uv run mewbo-mcp
```

The server starts a Streamable-HTTP MCP endpoint at `http://<MEWBO_MCP_HOST>:<MEWBO_MCP_PORT>/mcp`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MEWBO_API_URL` | `http://localhost:5124` | Base URL of the Mewbo REST API. The default targets local dev (`uv run mewbo-api` binds `0.0.0.0:5124`). In Docker the API runs under gunicorn on `5125`, and the compose services use `network_mode: host`, so the override sets `MEWBO_API_URL=http://localhost:5125`. |
| `MEWBO_MCP_HOST` | `127.0.0.1` | Bind host for the MCP server. Set to `0.0.0.0` in Docker. |
| `MEWBO_MCP_PORT` | `5127` | Bind port for the MCP server. Deliberately not `5125` (the API's gunicorn port in Docker) so both can run together without a port clash. |
| `MEWBO_MASTER_API_TOKEN` | `msk-strong-password` | Break-glass token; must match the API's master token |
| `MEWBO_HOME` | `~/.mewbo` | Data directory used by the file-driver KeyStore |
| `MEWBO_MONGODB_URI` | *(unset)* | When set, selects the Mongo KeyStore driver |
| `MEWBO_MCP_EXPOSED_GROUPS` | `sessions,wiki` | Comma-separated allowlist of tool groups to register (see "Tool exposure" below). An unknown name fails startup. |
| `MEWBO_MCP_EXPOSED_TIERS` | `read,navigate,ask,drive` | Comma-separated allowlist of effect tiers to register. An unknown name fails startup. |

**Important:** `MEWBO_HOME` (or `MEWBO_MONGODB_URI`) must point to the same storage as the Mewbo API so that keys issued by `POST /api/keys` are valid on the MCP server.

Authenticate tool calls with an issued API key (or the master token) as a `Bearer` token in the `Authorization` header.

## Tool exposure

Every tool is gated on two axes at registration time: a **group** (which subsystem)
and an **effect tier** (`read` returns stored data with no model call; `navigate` walks
a stored graph; `ask` starts a model run; `drive` creates, steers, or kills a session).
A tool is only registered when both its group and its tier are allowed.

**By default only the `sessions` and `wiki` groups are exposed** (all four tiers open) —
`integrations`, `search`, `structured`, and `triggers` are withheld. Set
`MEWBO_MCP_EXPOSED_GROUPS=sessions,wiki,integrations,triggers,search,structured` to
restore the full surface, or narrow `MEWBO_MCP_EXPOSED_TIERS` (e.g. `read,navigate`)
for a facade that answers no questions and starts no runs.

This is curation, not access control: an issued key is full-power against the REST
API directly. Withholding a group or tier removes it from this facade's surface only.

## Tools

### A. Sessions — create & control (`drive`)
- `create_session` — create and start a session; auto-provisions a fresh git worktree + branch by default
- `send_followup` — send a steering message into a running session
- `interrupt_session` — interrupt the current step of a running session (idle is a no-op)
- `terminate_session` — permanently and irreversibly end a session; cascades to cancel every armed trigger

### B. Sessions — discover & read (`read`)
- `list_sessions` — list sessions with optional project / status / since filters
- `get_session_history` — read session history at four detail tiers: `overview`, `turns`, `steps`, `full`
- `get_agent_tree` — return the session's sub-agent tree with lifecycle state

### C. Wiki — query & ask
- `list_wiki_projects` (`read`) — list indexed Agentic Wiki projects
- `read_wiki_structure` (`read`) — return a project's knowledge graph
- `read_wiki_page` (`read`) — fetch a single wiki page
- `list_wiki_pages` (`read`) — list a project's page index
- `graph_neighbors` (`navigate`) — directed, kind-filtered, multi-hop graph traversal ("what calls X" / "what does X contain"), no model call
- `submit_insight` (`ask`) — suggest a memory note for a project's multiplex code–memory–docs graph (condensed into atomic, auto-anchored claims)
- `ask_wiki` (`ask`) — ask a natural-language question and receive a cited answer
- `get_wiki_answer` (`read`) — re-fetch a stored answer snapshot by id

### D. Integrations / capability discovery (withheld by default)
- `list_integrations` — list available tools and plugins for capability discovery
- `list_projects` — list registered projects (the discovery surface for `create_session`'s `project`)

### E. Mewbo Search (withheld by default)
- `list_search_workspaces` — list saved multi-source search workspaces (compact: id, name, sources, recent-query count)
- `search` — run a search across a workspace (resolved by id or name) and return the cited answer + results; `detail` is `answer` (default) or `full`
- `get_search_run` — fetch a prior run by id (replay / deep-link, or poll an async run)

### F. Structured query (withheld by default)
- `structured_query` — run an agentic session that emits a JSON-Schema-validated object, routing graph-first through a mapped Search workspace
- `get_structured_run` — fetch a prior structured run by id

### G. Triggers (withheld by default)
- `list_triggers` — list armed reverse-invocation triggers with server-side filters
- `cancel_trigger` — idempotently cancel a trigger

## Docker Compose

The `mewbo-mcp` service is defined in the root `docker-compose.yml`. It shares the `api-data` volume with the `api` service so both see the same KeyStore.

```bash
docker compose up --build mewbo-mcp
```

[Link to GitHub Repository](https://github.com/bearlike/Assistant)
