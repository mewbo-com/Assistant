# Skills

## Instructions loaded on need

A skill is one instruction file that gives Mewbo a specific way of working. Each lives in its own
directory as a `SKILL.md` file, with YAML frontmatter and a markdown body. Mewbo pulls that body into
context only when the skill activates, so dozens of installed skills cost nothing until one runs.

> [!TIP] Drop-in compatible with Claude Code
> Skills follow the [Agent Skills standard](https://docs.claude.com/en/api/agent-skills), also
> published as the open [`agentskills.io`](https://agentskills.io) spec. Mewbo reads the same
> directories, the same `SKILL.md` frontmatter, the same `allowed-tools` scoping and the same
> `/skill-name` invocation. Any skill written for Claude Code works unchanged in Mewbo.

---

## Writing a skill

Create a directory at `.claude/skills/<your-skill-name>/SKILL.md`. The file starts with a YAML
frontmatter block and the instruction body follows it.

```markdown title=".claude/skills/<your-skill-name>/SKILL.md"
---
name: code-reviewer
description: Review code changes for correctness, style, and test coverage. Use when asked to review a diff, PR, or commit.
allowed-tools: read_file aider_list_dir_tool aider_shell_tool
---

# Code Review

1. Read the diff using `read_file`.
2. Run `aider_shell_tool` with `git diff HEAD~1` to verify context.
3. Check for missing tests.
4. Report each finding as **Issue**, **Severity**, **Suggestion**.
```

### Frontmatter reference

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Lowercase letters and digits, single hyphens only between them, max 64 characters. Must match `^[a-z0-9](?:[a-z0-9]\|-(?=[a-z0-9])){0,62}[a-z0-9]?$` |
| `description` | string | Yes | Used for auto-invocation matching (max 1024 chars) |
| `requires-capabilities` | string or list | No | Capability ids this skill needs; space-delimited string or YAML list. The skill is hidden from sessions that do not advertise all of them on `X-Mewbo-Capabilities`. See [Plugins & Marketplace → Capability gating](features-plugins.md#capability-gating). |
| `allowed-tools` | string or list | No | Tool IDs the skill scopes to; space-delimited string or YAML list |
| `disable-model-invocation` | boolean | No | Set `true` to hide this skill from the auto-invocation catalog (user `/skill-name` still works) |
| `user-invocable` | boolean | No | Set `false` to prevent explicit `/skill-name` invocation |
| `context` | string | No | Set `"fork"` to run the skill in a forked context |
| `agent` | string | No | Run skill inside a registered agent type |
| `model` | string | No | Model override when the skill activates |

/// table-caption
Every `SKILL.md` frontmatter key. Only `name` and `description` are required.
///

---

## Tool scoping

Set `allowed-tools` to narrow the tool set for as long as the skill is active, using the same tool
IDs as anywhere else in Mewbo.

```yaml
allowed-tools: read_file aider_list_dir_tool
```

Omit it and the skill inherits the session's full tool set.

---

## Capability gating

The client advertises what it can render on the `X-Mewbo-Capabilities` header, and
[SkillRegistry](repo:packages/mewbo_core/src/mewbo_core/tooling/skills.py) filters against it. A
gated skill reaches neither the model nor an explicit `/skill-name` call.

The built-in `widget-builder` plugin works this way. Its `st-widget-builder` skill declares
`requires-capabilities: [stlite]`, so it never appears on a CLI session that does not advertise stlite.

```yaml title="SKILL.md"
---
name: st-widget-builder
description: Build an interactive stlite widget rendered inline in the console.
requires-capabilities: [stlite]
---
```

Declare `requires-capabilities` once in `plugin.json` and every skill under that plugin inherits it.
See [Plugins & Marketplace → Capability gating](features-plugins.md#capability-gating).

---

## Shell preprocessing

Skills embed shell commands with the `` !`command` `` syntax. Each matched command runs at activation
time and its standard output is substituted inline before the model sees the instructions.

**Example.** Inject the current git branch name.

```markdown
You are reviewing code on branch: !`git rev-parse --abbrev-ref HEAD`
```

A command times out after 30 seconds. On error the placeholder becomes `[ERROR: ...]`.

---

## Where skills live

| Path | Scope | Priority |
|---|---|---|
| `~/.claude/skills/<name>/SKILL.md` | User-global (all projects) | Lowest |
| `.claude/skills/<name>/SKILL.md` | Project-local (CWD) | Overrides personal |
| `<subdir>/.claude/skills/<name>/SKILL.md` | Subtree (nested inside the project) | Does not override above |

/// table-caption
The three directories skills are discovered from. A skill in the project wins a name collision.
///

Plugins ship skills in the same `SKILL.md` format. A plugin skill never overrides a personal or a
project skill of the same name.

---

## Invoking skills

Two paths trigger a skill.

- **Automatically.** Mewbo reads the skill catalogue at the start of every session and can activate a
  matching skill from your request.
- **Explicitly.** Type `/skill-name` in the CLI or console. Arguments after the name arrive as
  `$ARGUMENTS` inside the skill body, and individual tokens as `$0`, `$1` and so on.

Sub-agents carry the same catalogue in their system prompt, so naming a skill in a delegated task
description works too.

---

## Hot-reload

A changed `SKILL.md` is picked up automatically, and a new skill directory on the next scan. No
restart is required.

---

> [!NOTE] How it works internally
> See [Architecture Overview → Skill loading](core-orchestration.md#skill-loading).
