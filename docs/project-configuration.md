# Project Setup

## What a session loads at start

When you start a session, Mewbo injects your project's `CLAUDE.md`, and any parent files, into the system prompt. The same discovery handles MCP servers, skills and local overrides.

> [!TIP] Drop-in compatible with common agent conventions
> Mewbo reads the [Claude Code](https://docs.claude.com/en/docs/claude-code/memory) `CLAUDE.md` format and the open [`AGENTS.md`](https://agents.md) convention used by Codex, Aider and other agent frameworks. MCP servers follow the [Model Context Protocol](https://modelcontextprotocol.io) and accept both `servers` and `mcpServers`. Skills follow the [Agent Skills](https://docs.claude.com/en/api/agent-skills) standard. Your existing project files work unmodified.

---

## Choosing a workspace

Every session runs somewhere on disk, and there are three ways to pick where.

| Choice | Where the session starts | Pick this when |
|---|---|---|
| Temporary directory (default) | A fresh, empty scratch directory that exists only for the session | The task does not need an existing codebase: a one-off script, a calculation, a question with no repository behind it |
| A named project | The directory you choose before the session starts | You already know which project the work belongs to |
| Auto | A temporary directory at first, then any project the session moves into | The right project is not obvious up front, or the task genuinely touches more than one project |

The default and named choices are fixed for the life of the session. Auto starts the same way and can move later.

### What the model sees in auto mode

Auto mode binds two extra tools to the top level agent. `list_projects` enumerates every project Mewbo knows about, including repositories registered but never checked out. `switch_project` moves the session into one of them, so the working directory changes and the new project's instruction files load as described below.

Two constraints shape a switch, and both are deliberate.

- **A switch never grants more tools than the run started with.** The tool registry is rebuilt for the new directory, but the bound set narrows to what the agent already held, so a project's own `.mcp.json` servers are not admitted partway through a run. Otherwise moving between projects would be a way to acquire tools the caller never granted. Start a fresh session there and its servers resolve normally.
- **A sub-agent already running keeps the directory it started in.** The switch moves the session going forward, not agents already in flight. A parent can hand a sub-agent its own project at spawn time instead, which is what lets one session run a fleet in one repository while another fleet works in a second.

Both tools exist only for the top level agent, and only in auto mode. See [Built-in Tools](features-builtin-tools.md#list_projects) for parameters, results and the full switch semantics.

---

## Instruction file loading

### Upward pass: content injected at startup

At session start, Mewbo walks up from your working directory to the git root, or the filesystem root outside a repo, and loads every instruction file along the way. Each file's full text is concatenated into the system prompt under a heading naming its source.

| Priority | Path | Scope |
|----------|------|-------|
| 10 | `~/.claude/CLAUDE.md` | User-global, applies to every project |
| 20–29 | `CLAUDE.md` / `.claude/CLAUDE.md` walking up from CWD | Project hierarchy; CWD = 20, parent = 21, and so on |
| 30 | `.claude/rules/*.md` (all files, sorted) | Project-local rule set |
| 40 | `CLAUDE.local.md` | Machine-local override (gitignore this) |

Lower priority means lower precedence, so higher priority content wins on conflict. Where `CLAUDE.md` and `AGENTS.md` sit at the same path, `CLAUDE.md` takes precedence and `AGENTS.md` is treated as a fallback.

### Downward pass: context map for on-demand loading

Mewbo also scans *down* from your working directory to a maximum depth of 5, looking for `CLAUDE.md`, `AGENTS.md`, and `.claude/CLAUDE.md` in subdirectories. The content of these files is **not** injected. Only the file paths are collected and listed in the system prompt, shown below.

```
# Sub-package instruction files

The following instruction files exist in subdirectories.
Read them when working on the relevant package.

- packages/mewbo_core/CLAUDE.md
- apps/mewbo_console/CLAUDE.md
- apps/mewbo_api/AGENTS.md
```

Work starting in one of those directories reads the matching file with `read_file` first. That is what keeps a large monorepo manageable, since only the directly applicable instructions occupy the active context.

Some directories are never walked. `node_modules`, `__pycache__`, `.venv`, `venv`, and all dotfile directories such as `.git` and `.claude` are pruned.

### Noload marker

Add `<!-- mewbo:noload -->` as the very first line of any instruction file to exclude it from both passes. The loader checks the first line before reading the rest of the file.

Use this for shim files that redirect to another `CLAUDE.md`, so you do not get duplicate injection.

```markdown
<!-- mewbo:noload -->
See ../CLAUDE.md. This file exists only for tool compatibility.
```

### Git context

Mewbo can add the current git branch and status to the session context. Individual integrations opt in, including the CLI and specific skills, so it is not always injected.

---

## Project-level MCP configuration

### Config merge order

MCP server definitions come from four layers. Later layers win on key conflicts, using deep merge semantics where nested objects merge recursively rather than getting replaced wholesale.

| Layer | Source | Priority |
|-------|--------|----------|
| 1 | Plugin-contributed servers | Lowest |
| 2 | Global: `configs/mcp.json` or `$MEWBO_HOME/mcp.json` | Mid |
| 3 | Subtree `.mcp.json` files, deepest-first | Mid-high |
| 4 | CWD `.mcp.json` | Highest |

A project `.mcp.json` that adds a single server key therefore leaves every global server definition intact.

Mewbo reruns the merge whenever the MCP pool reconnects, so edits anywhere in the hierarchy are picked up automatically. Unchanged servers keep their connections, changed ones reconnect, and removed ones disconnect.

### Config normalization

All `.mcp.json` files are normalized before merging, so you can mix schemas freely.

| Input field | Normalized to | Notes |
|-------------|---------------|-------|
| `mcpServers` | `servers` | Claude Code / VS Code schema compatibility |
| `type` | `transport` | Both keys removed after normalization to avoid leaks |
| `http_headers` | `headers` | Direct rename |
| `transport: "http"` | `transport: "streamable_http"` | Accepted alias |
| `command` present, no `transport` | `transport: "stdio"` | Inferred |
| `${VAR}` / `$VAR` in values | Expanded from process environment | Unresolved vars left as-is |

### Example project `.mcp.json`

```json title=".mcp.json"
{
  "servers": {
    "project_db": {
      "transport": "stdio",
      "command": ["mcp-sqlite", "--db", "./dev.db"]
    }
  }
}
```

The Claude Code shape works too. Write `mcpServers` with a string `command` and an `args` array, and normalization folds it into the form above.

---

## Skills and project context

Skills follow the same layered discovery as instruction files. See [Skills](features-skills.md#where-skills-live) for the full path table. Project local skills at `.claude/skills/` take precedence over personal skills at `~/.claude/skills/`, and subtree skills fill gaps without overriding either.

---

## Summary: what goes into the context at startup

| How it enters | Components |
|---|---|
| Full text in the system prompt | User and project `CLAUDE.md` files, `.claude/rules/*.md`, `CLAUDE.local.md` |
| Index only, body read on demand | Subtree `CLAUDE.md` paths via `read_file`, skills via `activate_skill` |
| Bound to the LLM call | MCP tool schemas, remerged whenever the pool reconnects |

> [!NOTE] How it works internally
> See [Architecture Overview → Instruction loading](core-orchestration.md#instruction-loading) and [MCP connection pool](core-orchestration.md#mcp).
