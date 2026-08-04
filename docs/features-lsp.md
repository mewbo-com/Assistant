# LSP Code Intelligence

## See errors and find symbols

Mewbo ships a native Language Server Protocol tool. Servers are discovered on your `PATH` and start
lazily on first use.

## Built-in language servers

| Language | Server | Install |
|----------|--------|---------|
| Python | `pyright` (pyright-langserver) | `pip install pyright` |
| TypeScript / JavaScript | `typescript-language-server` | `npm install -g typescript-language-server typescript` |
| Go | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer` |

Nothing beyond the binary on your `PATH` is required. A server that is not installed is silently
skipped.

## Operations

| Operation | Required fields | What it returns |
|-----------|----------------|-----------------|
| `diagnostics` | `file_path` | Errors and warnings in a file (capped at 50, severity ≥ warning) |
| `definition` | `file_path`, `line`, `character` | File path(s) where the symbol is defined |
| `references` | `file_path`, `line`, `character` | All reference locations including the declaration |
| `hover` | `file_path`, `line`, `character` | Type signature and documentation for the symbol |

`line` and `character` both count from 0. This call jumps to the definition at line 42, column 12.

```json
{
  "operation": "definition",
  "file_path": "/home/user/project/main.py",
  "line": 41,
  "character": 12
}
```

## Passive diagnostics

After every file edit the LSP tool runs and appends type errors and lint warnings to the session
context, with no explicit tool call. Set `agent.lsp.enabled` to `false` to turn it off.

## Workspace root

Each server picks a workspace root by walking up from the working directory looking for marker
files.

| Server | Root markers |
|--------|-------------|
| pyright | `pyproject.toml`, `setup.py`, `setup.cfg`, `pyrightconfig.json` |
| typescript-language-server | `tsconfig.json`, `package.json` |
| gopls | `go.mod` |
| rust-analyzer | `Cargo.toml` |

If no marker is found nearby, the current working directory is used as the root.

## Configuration

### Enable / disable

LSP is enabled by default. Disable it entirely.

```json title="configs/app.json"
{
  "agent": {
    "lsp": {
      "enabled": false
    }
  }
}
```

### Disable a specific server

```json title="configs/app.json"
{
  "agent": {
    "lsp": {
      "servers": {
        "pyright": { "disabled": true }
      }
    }
  }
}
```

### Add a custom language server

Define a custom server under `agent.lsp.servers`.

```json title="configs/app.json"
{
  "agent": {
    "lsp": {
      "servers": {
        "my-lsp": {
          "command": ["my-language-server", "--stdio"],
          "extensions": [".mylang", ".ml"],
          "root_markers": ["my-project.toml"],
          "language_id": "mylang"
        }
      }
    }
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `command` | Yes | List of binary and arguments (must be on `PATH`). |
| `extensions` | Yes | List of file extensions this server handles (e.g. `[".py"]`). |
| `root_markers` | No | Filenames that indicate a workspace root. |
| `language_id` | No | LSP `languageId` string (defaults to the server's key name). |
| `disabled` | No | Set `true` to disable a built-in server. |

/// table-caption
The fields a custom server definition accepts.
///

### Full config key reference

Every key is nested under `agent.lsp` in [`configs/app.json`](repo:configs/app.example.json) and
documented with its default in [Configuration](configuration.md#agent).

## Installation

The LSP client library `pygls` ships with the base install, so there is no separate install step.
If it is ever absent, the LSP tool is silently disabled, with no crash and no startup error.

---

> [!NOTE] How it works internally
> See [Architecture Overview → LSP tool](core-orchestration.md#lsp).
