# Configuration

## Set launch flags and defaults

Flags set what one launch does, config files set the defaults, and slash commands manage the running session.

## Command-line flags

The parser is defined in [`cli_master.py`](repo:apps/mewbo_cli/src/mewbo_cli/cli_master.py).

| Flag | Purpose |
|------|---------|
| `--query "..."` | Run a single query and exit. This is the non-interactive path. |
| `--model MODEL` | Override the configured model for this run. |
| `--fallback-models LIST` | Comma-separated fallback model ids for this run. `--fallback-model` is a singular alias. |
| `--no-fallback` | Disable model fallback for this run. It overrides `--fallback-models`. |
| `--max-iters N` | Maximum orchestration iterations. Default is 3. |
| `--show-plan` | Show the action plan. This is the default. |
| `--no-plan` | Hide the action plan. |
| `-v`, `--verbose` | Increase log verbosity. `-v` is debug, `-vv` is trace. |
| `--session ID` | Resume an existing session by id. |
| `--tag TAG` | Resume or create a tagged session. |
| `--fork SOURCE` | Fork from another session id or tag. |
| `--session-dir PATH` | Override the session storage directory. |
| `--history-file PATH` | Override the CLI history file path. |
| `--no-color` | Disable ANSI color output. |
| `--auto-approve` | Approve every permission prompt automatically for the session. |
| `--log-file PATH` | Stream all logs to this file and keep the terminal output clean. |
| `--log-overwrite` | Truncate the log file at startup instead of appending. `--overwrite` is an alias. |
| `--log-console` | With a log file set, also keep logs on stderr. |
| `--config PATH` | Path to the app config file. The default is auto-discover. |

/// table-caption
Every flag `mewbo` accepts at launch.
///

## Model fallback

When a model fails during a run, the client retries it, then escalates down a ladder of alternate models. The ladder comes from the `llm.fallback` config. Two flags override it for one launch.

```bash
mewbo --fallback-models gpt-5.4,claude-sonnet-5
mewbo --no-fallback
```

`--no-fallback` wins when you pass both.

After each turn, the client replays the run's resilience events as short notices, a retry, a fallback, or a halt, so you never open a log to find one. When a fallback pins a new model for the rest of the run, the notice says so.

## The config chain

Mewbo loads config from the first directory that contains it, checked in this order.

1. `CWD/configs/`: config local to the project, highest priority.
2. `$MEWBO_HOME/`: a custom home directory, if you set one.
3. `~/.mewbo/`: the fallback in your home directory.

Run `/init` inside the app to scaffold both config files. The app config, [`configs/app.example.json`](repo:configs/app.example.json), holds runtime settings and LLM keys. The MCP config, `configs/mcp.json`, defines your MCP servers.

## Session recovery

A session is durable, so you can leave one and come back to it. Resume at launch with `--session`, `--tag` or `--fork`. Inside the app, press ++ctrl+s++ or type `/resume` to open the session switcher and pick one.

A killed or incomplete run is recoverable through `/continue` or `/retry`, both described in [Plan Mode](../features-plan-mode.md). Either path runs through the same engine, so a recovered run inherits the model fallback ladder like any other run.

## Next steps

- [The Interface](interface.md) covers the live transcript, the plan dock, and approval prompts.
- [Agent Fleet](agent-fleet.md) covers sub-agent orchestration and the fleet sidebar.
- [Remote Sync](remote-sync.md) covers the local default and the remote opt in.
- [Configuration Reference](../configuration.md) lists every config key on the site, with defaults.
