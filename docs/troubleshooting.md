# Troubleshooting

## When something goes wrong

Common failures, symptom first. For session level debugging, see the
[debug methodology in CLAUDE.md](repo:CLAUDE.md).

## LLM connectivity

**Symptom.** A session starts, then immediately errors because an LLM call failed.

Checks.

1. Verify `llm.api_key` in [`configs/app.json`](repo:configs/app.example.json) is set and correct.
2. Behind a proxy, set `llm.api_base` and verify `llm.proxy_model_prefix` matches what the proxy expects.
3. Test connectivity directly.

```bash
curl -sk https://api.anthropic.com/v1/messages \
  -H "x-api-key: $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-haiku-4-5","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
```

## Tool not available

**Symptom.** The transcript shows a `Tool not available` error.

Causes.

- The allowlist or denylist filtered the tool out.
- `tool_id` mismatched between what the LLM was told and what the registry holds.
- The MCP server never connected. See [MCP server not found](#mcp-server-not-found) below.

Fix. Check [`GET /api/tools`](endpoint:GET /api/tools) from the API, or run `/mcp` in the CLI, to see what tools are registered.

## MCP server not found

**Symptom.** The error reads `MCP server 'X' not found in config`.

Causes.

- `configs/mcp.json` doesn't match the container mount. In Docker, the path must be identical between host and container.
- The project `.mcp.json` never merged, because the CWD wasn't set correctly in the request.

Fix. Run `/mcp` in the CLI to see which servers are loaded, and verify the config path with the `--config` flag. [MCP Tools](features-mcp.md) covers the file schema and the per-project merge.

## Shell/file tool errors

**Symptom.** A shell or file tool returns `result: null, success: false`.

Causes.

- The CWD is missing in the container, because the volume was never mounted or was mounted at a different path than the host uses.
- The `root` parameter wasn't injected into the tool call.

Fix. In `docker-compose.override.yml`, verify the project directory is mounted at the same path as on the host.

## Docker issues

**Symptom.** Services won't start, or the console can't reach the API.

Checks.

1. Host networking means a port clash stops a service. Verify nothing else holds 5125 or 3001.
2. `MEWBO_HOST_UID` and `MEWBO_HOST_GID` must match your actual user. Run `id` to find them.
3. Volume paths must match exactly between host and container.

## Session stuck / not completing

**Symptom.** A session runs for a long time or appears to stall.

Notes.

- Mewbo runs a natural completion loop. It continues until the LLM emits text with no tool calls, and there is no hard step limit enforced at runtime.
- Budget warnings arrive as injected messages as the context window fills.
- If a session appears stuck, use mid session steering.

```bash
POST /api/sessions/{id}/interrupt
```

In the CLI, use `/terminate` instead.

Stall detection in the hypervisor fires after repeated identical tool calls and injects a warning message.

## MongoDB connection

**Symptom.** The API starts, but the Web IDE or storage fails with MongoDB connection errors in the logs.

Checks.

1. `MEWBO_STORAGE_DRIVER=mongodb` is set in the environment.
2. `MEWBO_MONGODB_URI` uses the format `mongodb://user:pass@host:27017/dbname?authSource=admin`.
3. In Docker, MongoDB must sit on the same network or be reachable through host networking.
4. Port `27017` is production and `27018` reaches the dev environment directly.

## Getting logs

**CLI verbose mode.**

```bash
uv run mewbo -v    # debug
uv run mewbo -vv   # trace (very verbose)
```

**Docker API logs.**

```bash
docker compose logs -f mewbo-api
```

**Langfuse traces**, when enabled. Session traces group by `session_id`, and `done_reason: "error"` marks failures.
Standard path. `fetch_traces` → `fetch_trace(include_observations=true)` → `fetch_observation` on GENERATION nodes.

## Common config mistakes

| Mistake | Fix |
|---------|-----|
| Model name without provider prefix | Use `anthropic/model`, not just `model` |
| `MEWBO_MASTER_API_TOKEN` left as default | Change before exposing to the network |
| `MEWBO_VITE_API_KEY` doesn't match `MEWBO_MASTER_API_TOKEN` | They must be identical |
