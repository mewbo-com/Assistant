# Remote Sync

## Mirror sessions to a server

The terminal client runs local by default. Your session transcript is a JSONL file on your disk and it is the source of truth. Nothing leaves your machine unless you opt in.

Remote sync keeps the engine local and mirrors the session to a remote Mewbo deployment, so you can read your terminal sessions from another device. The seam lives in [`cli_remote.py`](repo:apps/mewbo_cli/src/mewbo_cli/cli_remote.py).

## The config block

One config block controls it, `cli.remote`. The model is defined in [`config.py`](repo:packages/mewbo_core/src/mewbo_core/config.py).

| Key | Purpose |
|-----|---------|
| `cli.remote.base_url` | The root URL of the remote Mewbo deployment. It serves the REST API under `/api` and the Mewbo MCP server under `/mcp`. Empty by default. |
| `cli.remote.token` | The API token presented to that deployment. It is write only and never returned once set. |

Only the CLI reads this block. Every other Mewbo surface ignores it.

```json title="configs/app.json"
{
  "cli": {
    "remote": {
      "base_url": "https://mewbo.example.com",
      "token": "${MEWBO_REMOTE_TOKEN}"
    }
  }
}
```

The token field expands environment references at use time, so the secret stays out of the file.

## What leaves your machine when enabled

!!! warning "Enabling remote sync sends your data to the configured server"
    When `cli.remote.base_url` is set, the client sends your session transcript and event data to that server. Turn it on only for a deployment you trust.

Two things reach the network once a base URL is set.

- The client mirrors each session event to [POST /api/sessions/{session_id}/events](endpoint:POST /api/sessions/{session_id}/events) without waiting for a response. It batches a burst of streaming deltas into one request and mirrors only the session you are working on. A failed post is logged and dropped.
- The client registers the Mewbo MCP server from that deployment, which surfaces the Mewbo product tools such as wiki and search in your session. Those tools then execute on the deployment, not on your machine.

The engine and the authoritative transcript never leave your host.

## The visible indicator

The header names the mode. A local session shows `local-only` and a synced session shows `remote:` followed by the base URL. A synced session also carries a `transcript_sink` chip on its trace, so you can filter synced work apart from local work.

## How to turn it off

Leave `cli.remote.base_url` empty, or remove the `cli.remote` block. The client then never mirrors and never registers the remote tools.

## Next steps

- [Configuration](configuration.md) covers flags, the config chain, and session recovery.
- [The Interface](interface.md) covers the live transcript and approval prompts.
- [REST API](../api/index.md) is the remote deployment your terminal sessions mirror to.
