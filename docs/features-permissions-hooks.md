# Permissions & Hooks

## Approve or block tool calls

Mewbo runs every tool call through a **permission policy** before it executes, and fires **hooks** at session lifecycle events and around individual tool calls. Permissions gate what runs. Hooks are where notifications, audit logging, webhook fan out and external guardrails hang.

**Quick example.** Auto-approve all tool calls in the current session.

```
/automatic --yes
```

Or configure it globally.

```json title="configs/app.json"
"permissions": {
  "approval_mode": "allow"
}
```

---

## Permission policies

### The three outcomes

Every tool call carries a `tool_id`, such as `aider_shell_tool` or `mcp__my_server__my_tool`, and an `operation` such as `get` or `set`. Mewbo checks your policy against that pair and resolves it to one of three outcomes.

| Decision | Meaning |
|----------|---------|
| `allow` | Execute without prompting |
| `deny` | Block unconditionally |
| `ask` | Prompt the user. On the CLI this is an interactive prompt; in the console it is an approval card |

Rules are evaluated in order and the first match wins. With no match, Mewbo falls back to the per operation defaults. `get` resolves to `allow` and `set` resolves to `ask`. If neither default applies, the catch all `default_decision` decides.

### Rule syntax

Rules live in a JSON or TOML policy file named by `permissions.policy_path`. Each rule matches on `tool_id` and `operation` using `fnmatch` glob patterns.

```json title="configs/policy.json"
{
  "rules": [
    { "tool_id": "aider_shell_tool", "operation": "*", "decision": "ask" },
    { "tool_id": "read_file",        "operation": "*", "decision": "allow" },
    { "tool_id": "*",                "operation": "get", "decision": "allow" },
    { "tool_id": "*",                "operation": "*",   "decision": "ask" }
  ],
  "default_by_operation": {
    "get": "allow",
    "set": "ask"
  },
  "default_decision": "ask"
}
```

`*` matches any string and `?` matches a single character. The patterns work on MCP tool IDs too. `mcp__*` matches every tool sourced from MCP, and `mcp__my_server__*` scopes to one server.

### Approval modes

`permissions.approval_mode` applies for the whole session and overrides the rule file. Use it for a blanket policy without editing rules.

| Value | Aliases | Effect |
|-------|---------|--------|
| `allow` | `auto`, `approve`, `yes` | All tools run without prompting |
| `deny` | `never`, `no` | All tools are blocked |
| `ask` (default) | _(none)_ | Falls through to per-rule decisions |

### /automatic (CLI)

`/automatic` flips the current session into allow mode without touching your config file.

```
/automatic          # prompts for confirmation
/automatic --yes    # skip confirmation
/automatic off      # revert to policy-driven mode
```

### Config example

```json title="configs/app.json"
"permissions": {
  "policy_path": "./configs/policy.json",
  "approval_mode": "ask"
}
```

See [configuration.md](configuration.md#permissions) for field descriptions.

---

## Hooks

Hooks run custom code at specific moments in a session's life. Declare them in the `hooks` section of `configs/app.json`, starting from [`configs/app.example.json`](repo:configs/app.example.json). A failing hook is logged as a warning and never blocks execution, so a flaky endpoint cannot stall a session.

### When hooks fire

| Event | When it fires |
|-------|--------------|
| `on_session_start` | A new session begins |
| `on_session_end` | A session ends, whether it succeeded or errored |
| `pre_tool_use` | Just before a tool call executes |
| `post_tool_use` | Just after a tool call returns |
| `on_event` | Every time an event is appended to a session transcript |

`on_event` is the firehose. It sees every transcript record, not just tool calls. It sits on the append hot path, so both command and HTTP hooks fire without waiting and a slow hook never delays the session. Its `matcher` runs against the event **type**, for example `tool_result` or `context_compacted`, rather than a `tool_id`.

### Two hook types

#### Command hooks

A command hook runs a shell command. Mewbo waits up to `timeout` seconds, 30 by default, for the process to finish. Use it when the hook must complete before the session continues, such as prepping a workspace at session start.

Mewbo sets these environment variables on the subprocess.

| Variable | Available in | Value |
|----------|-------------|-------|
| `MEWBO_SESSION_ID` | `on_session_start`, `on_session_end` | Session identifier |
| `MEWBO_ERROR` | `on_session_end` (error cases only) | Error message string |
| `MEWBO_TOOL_ID` | `pre_tool_use`, `post_tool_use` | Tool identifier |
| `MEWBO_OPERATION` | `pre_tool_use`, `post_tool_use` | Operation name |
| `MEWBO_TOOL_RESULT` | `post_tool_use` | First 2 000 characters of the result |
| `MEWBO_EVENT_TYPE` | `on_event` | Event type of the appended record |

An `on_event` command hook also receives the full event record as JSON on stdin.

Example. Send a desktop notification when a session ends.

```json title="configs/app.json"
"hooks": {
  "on_session_end": [
    {
      "type": "command",
      "command": "notify-send 'Mewbo' \"Session $MEWBO_SESSION_ID done\"",
      "timeout": 5
    }
  ]
}
```

#### HTTP hooks

An HTTP hook posts a JSON body to a URL and never blocks. Mewbo does not wait for the response, and failures are logged rather than raised. Use it to feed session events into a webhook, an audit log or a chat integration without slowing the agent.

Payload for `on_session_end`.

```json
{
  "event": "session_end",
  "session_id": "abc123",
  "error": null
}
```

Payload for `post_tool_use`.

```json
{
  "event": "post_tool_use",
  "tool_id": "aider_shell_tool",
  "operation": "run",
  "result_preview": "stdout output (first 2000 chars)"
}
```

Payload for `on_event`. `record` is the transcript event with its string values truncated.

```json
{
  "event": "session_event",
  "session_id": "abc123",
  "record": { "type": "tool_result", "ts": "...", "...": "..." }
}
```

Example. Notify an external webhook.

```json title="configs/app.json"
"hooks": {
  "on_session_end": [
    {
      "type": "http",
      "url": "https://your-webhook.example.com/mewbo",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" },
      "timeout": 10
    }
  ]
}
```

### Scoping hooks to specific tools

Add a `matcher`, an `fnmatch` pattern, to any hook entry to restrict which tool calls fire it. That is the difference between logging every tool call and logging only shell commands.

```json title="configs/app.json"
"hooks": {
  "post_tool_use": [
    {
      "type": "command",
      "command": "echo 'Shell command ran' >> /tmp/shell.log",
      "matcher": "aider_shell_tool"
    }
  ]
}
```

`"matcher": "mcp__*"` scopes the hook to every MCP tool. Omit `matcher`, or set it to `null`, and the hook fires on every tool call.

---

## Reference

The `permissions` and `hooks` config keys are documented in [Configuration](configuration.md#permissions) and [Configuration → Hooks](configuration.md#hooks). Each entry in a hooks list accepts these fields.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | `"command"` | `"command"` or `"http"` |
| `command` | string | `""` | Shell command to run (`type=command`) |
| `url` | string | `""` | POST target (`type=http`) |
| `headers` | object | `{}` | Extra HTTP headers (`type=http`) |
| `matcher` | string \| null | `null` | fnmatch pattern on `tool_id` (on the event type for `on_event`); `null` matches all |
| `timeout` | integer | `30` | Max seconds to wait for the hook |

> [!NOTE] How it works internally
> See [Architecture Overview → Permission policy](core-orchestration.md#permission-policy) and [Hook manager](core-orchestration.md#hook-manager).
