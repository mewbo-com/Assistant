# Web IDE

## Edit files in the browser

<video controls preload="metadata" width="1026" height="720" poster="../../assets/img/mewbo-console-01-front.png" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 0 auto;">
  <source src="../../assets/videos/mewbo-console-coder-demo-1.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Mewbo can launch a full IDE in your browser, running [code-server](https://github.com/coder/code-server) against a session's working directory. You get a real editor in the same tree the agent is working in.

One container runs per session, starting on demand and stopping itself after a configurable duration. Any session with a resolvable project qualifies, whether a project you configured, a workspace or worktree Mewbo manages, or a registered repository with a local checkout.

## Enabling

The feature is off until you turn it on in `configs/app.json`.

```json title="configs/app.json"
{
  "agent": {
    "web_ide": {
      "enabled": true
    }
  }
}
```

It needs MongoDB to persist container state across API restarts. See [Storage Backends](../deployment-storage.md) for the driver. The [Docker Compose](../deployment-docker.md) stack wires the rest of the plumbing for you.

## Launch an IDE

### From the console

An **Open in Web IDE** button appears on session cards once the feature is enabled. Click it to start the container and open the IDE in a new tab, with [`IdeLoader`](repo:apps/mewbo_console/src/components/IdeLoader.tsx) holding the screen while it boots.

### From the API

The console button calls the same endpoints you can drive directly.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/sessions/{session_id}/ide` | Create or reconnect to the IDE container |
| `GET` | `/api/sessions/{session_id}/ide` | Poll current container status |
| `DELETE` | `/api/sessions/{session_id}/ide` | Stop and remove the container |
| `POST` | `/api/sessions/{session_id}/ide/extend` | Extend the session lifetime |

The `POST` response includes a `password` field valid for one use. The `GET` response omits it. The IDE is reachable at `/ide/{session_id}/` behind the nginx proxy Mewbo ships with.

Create an IDE session.

```bash
curl -sk -X POST https://mewbo.example.com/api/sessions/abc123.../ide \
  -H "X-API-Key: your-token" | jq .
```

```json
{
  "session_id": "abc123...",
  "status": "starting",
  "url": "/ide/abc123.../",
  "password": "...",
  "expires_at": "2026-04-18T15:00:00+00:00",
  "remaining_seconds": 3600
}
```

Extend the deadline.

```bash
curl -sk -X POST https://mewbo.example.com/api/sessions/abc123.../ide/extend \
  -H "X-API-Key: your-token" \
  -H "Content-Type: application/json" \
  -d '{"hours": 2}'
```

The extend endpoint accepts either `hours`, an integer from 1 to 168, or an absolute `expires_at` ISO timestamp. Exactly one field is required. A request that would push the deadline past `max_lifetime_hours` is rejected with HTTP 409.

## Session lifetime

Every IDE session carries an expiry. When the wall clock passes `expires_at`, the container shuts itself down.

| Action | What happens |
|--------|--------------|
| Running | The container stays alive until `expires_at`. |
| Extend | Pushing the deadline out takes effect within about 15 seconds. |
| Stop | The container tears down immediately. |
| Reconnect | Reconnecting returns the current URL and password. If the container exited unexpectedly, Mewbo respawns it. |

## Configuration

Every key nests under `agent.web_ide` in `configs/app.json`. The [Configuration Reference](../configuration.md#agent) lists all of them with their defaults, including the container lifetime, the CPU, memory and PID ceilings, and the Docker network the proxy has to share with each container.

Restrict resources and pin the image.

```json title="configs/app.json"
{
  "agent": {
    "web_ide": {
      "enabled": true,
      "image": "codercom/code-server:4.95.3",
      "default_lifetime_hours": 2,
      "max_lifetime_hours": 4,
      "cpus": 0.5,
      "memory": "512m"
    }
  }
}
```

The session's project directory is mounted into the container, so your edits and the agent's land in the same tree and each sees the other immediately.

## Next steps

- [Sessions](sessions.md). The composer that starts a session the IDE can attach to.
- [Docker Compose](../deployment-docker.md). The stack that wires the IDE plumbing.
- [REST API Reference](../rest-api.md). The full IDE endpoint contract.
