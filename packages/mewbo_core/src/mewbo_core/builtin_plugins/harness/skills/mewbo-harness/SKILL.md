---
name: mewbo-harness
description: Use when you need to know how this harness itself behaves rather than what a tool's arguments are — why a tool result was truncated and how to page past it, what a tool timeout does and does not mean, which directory a relative path resolves against, how to reach a tool whose schema was not bound, how spawned sub-agents finish, and how to present a structured UI panel. Read this before reading the harness implementation source.
---

# How this harness behaves

Runtime semantics that no tool schema can carry. Everything here is a property of
the ENGINE; anything specific to your session or your project is not stated here
because it would be a guess — get those from the tool that owns them.

## Tool results are capped, and a capped result is windowed

Every tool result is fitted to a per-tool character cap before you see it.

| Population | Cap when the tool declares none |
|---|---|
| registry tools (`ToolSpec.max_result_chars`) | **2000 characters** |
| session tools (plugin/per-session tools) | **200000 characters** |
| shell tools | **30000 characters** (declared, not the default) |

When a result exceeds its cap it is **windowed, not head-truncated**: the head and
the tail are both kept and the middle is replaced by a marker of the form
`[... N characters omitted ...]`. So:

- **A marker means you are missing the middle** — not the end. The verdict of a
  command (traceback, exit banner, failure summary) survives.
- **Do not treat a windowed result as complete.** Narrow the read instead of
  re-issuing the same call: `grep` for the line you need, or page.

**Paging.** `read_file` is line-windowed — pass `offset` (0-based start line) and
`limit` (max lines, default 2000) to walk a large file instead of re-reading it.
A tool that pages says so in its own schema; when it does not, narrow the query.

## A timeout bounds the WAIT, not the work

Tool execution sits under a ceiling — **120 seconds** unless the tool declares its
own. A tool that hits it comes back as a timed-out call.

**A timeout is not evidence that nothing happened.** The wait was abandoned; a
subprocess, a background shell, a spawned agent or a remote job may still be
running and may still complete. Before retrying, **check state** — re-read the
file, list the shell session, poll the run id — rather than assuming a clean slate.
Re-issuing a side-effecting call after a timeout can double the effect.

## Paths resolve against the session's active root

Relative paths and the subprocess sandbox both resolve against the **active
project root** the loop publishes around every tool call — never against a `root`
argument you supply, because a model-chosen root would be a model-chosen sandbox.

**A session with no project bound has no active root**, and file/shell tools then
fall back to the process working directory — which is the server's, not yours.
So when a path matters: **do not assume the directory, get it from the tool that
resolves it** (the tool result, or the introspection call the owning surface
provides). A path you inferred from a doc is a hypothesis; a path a result handed
you is a fact.

## Not every tool you can use is bound

Tool schemas are deferred by default: MCP tools and tools marked deferred are
present by NAME only, listed under `<available-deferred-tools>` /
`<available-mcp-servers>`. **Calling a deferred tool without fetching its schema
fails** — there are no parameters to validate against.

Fetch first with `tool_search`:

- `select:Read,Edit` — exact names you already saw in the listing.
- `notebook jupyter` — keyword search when you do not know the name.

Fetching cannot widen your surface: search sees exactly what your agent is allowed
to bind, so a name that never appears is a name you cannot call.

## Delegation: spawn, steer, collect

`spawn_agent` / `spawn_agents` admit sub-agents; `check_agents` polls them and
`steer_agent` sends a mid-flight correction (both are root-only — a sub-agent
cannot manage its own children this way).

An agent occupies one of six states: `submitted` and `running` are live; the four
**terminal** states are `completed`, `failed`, `cancelled`, `rejected`.
`rejected` means it was declined at admission and **never ran** — a capacity or
policy refusal, not a failed attempt; re-spawning it unchanged repeats it.
Poll with `check_agents` rather than assuming; a spawn returns an id, not a result.

## Presenting a UI panel

`present_ui` (when bound) renders a small structured panel inline. The tree is
built from a fixed component vocabulary:

`Text` · `Heading` · `Card` · `Stack` · `Badge` · `KeyValue` · `Table` ·
`CodeBlock` · `Alert` · `Divider` · `Link`

`Card` and `Stack` are the only containers (they take `children`). One example:

```json
{
  "root": [
    {"component": "Heading", "value": "Index run"},
    {"component": "Card", "children": [
      {"component": "Badge", "label": "green", "status": "success"},
      {"component": "KeyValue", "items": [
        {"label": "Files", "value": "1204"},
        {"label": "Duration", "value": "38s"}
      ]}
    ]}
  ]
}
```

Presenting a panel is an ordinary step — it does not end the run, so write your
closing message as usual.
