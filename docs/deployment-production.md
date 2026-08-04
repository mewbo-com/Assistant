# Production Setup

## Production hardening

The default Compose stack ships a public example token, a wildcard CORS origin and default MongoDB credentials. Close those first.

## Security Checklist

1. **Rotate `MEWBO_MASTER_API_TOKEN`**. The example value is public. Generate a strong random token.
   ```bash
   openssl rand -hex 32
   ```
   Set it in `.env` as both `MEWBO_MASTER_API_TOKEN` and `MEWBO_VITE_API_KEY`, which must match.

2. **Restrict `CORS_ORIGIN`**. The default `*` allows any origin. In production, set it to your actual domain.
   ```dotenv title=".env"
   CORS_ORIGIN=https://mewbo.example.com
   ```

3. **Use TLS**. Terminate TLS at a reverse proxy such as nginx, Caddy, or Traefik. Never expose the API or console ports directly on a public interface.

4. **Set `GITHUB_TOKEN` in `.env`**. If you mount git repositories, [`10-git-setup.sh`](repo:docker/init.d/10-git-setup.sh) uses this token to configure `gh CLI` authentication, so `git fetch`, `push` and `pull` run without prompts.

5. **Change MongoDB credentials**. Update `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD` in `.env` from their defaults, then update `MEWBO_MONGODB_URI` to match.

6. **Consider per user accounts**. A shared master token cannot tell one caller from another. For per user identity, roles or an audit trail, see [Authentication & Access](authentication.md). It is opt in.

## Secrets management (optional)

Secrets do not have to sit in a plaintext `.env` on disk. `MEWBO_ENV_FILE` points the stack at a file rendered elsewhere, for example by a secrets manager writing into a private location at deploy time.

[`Makefile`](repo:Makefile) ships `ssm-bootstrap` and `redeploy` targets as one working example. A checkout binds to a secrets project once, then every `make redeploy` resolves secrets fresh and rebuilds the stack, with nothing sensitive committed to the tree. The load bearing piece is `MEWBO_ENV_FILE`, so swap in whatever secrets manager you already use.

## TLS with nginx

The repository ships a working nginx reverse proxy config. Install it on the host, outside Docker, after setting your `server_name`, `ssl_certificate` and `ssl_certificate_key`.

```bash
sudo ln -s /path/to/mewbo/docker/nginx-reverse-proxy.conf \
           /etc/nginx/sites-enabled/mewbo
sudo nginx -t && sudo systemctl reload nginx
```

[`docker/nginx-reverse-proxy.conf`](repo:docker/nginx-reverse-proxy.conf) already routes the console, the API,
the wiki event streams and the Web IDE. These are the settings that break Mewbo when a hand written
config omits them.

### Key nginx settings for Mewbo

| Setting | Why |
|---------|-----|
| `proxy_buffering off` on `/api/sessions/` | Server-Sent Events (SSE) must stream to the browser in real time; buffering breaks the stream. |
| `proxy_read_timeout 300s` on `/api/sessions/` | Sessions can run for minutes; the default 60s timeout would kill long-running agents. |
| Same SSE settings on the wiki `/stream` regex block | Wiki indexing and Q&A push progress over SSE too; without this block those streams stall behind nginx buffering. |
| `proxy_http_version 1.1` + `Connection ''` on SSE | Required for HTTP/1.1 keepalive on SSE endpoints. |
| `proxy_read_timeout 3600s` + WebSocket headers on `/ide/` | code-server uses WebSockets; upgrade headers and a long timeout are required. |
| `proxy_buffering off` on `/ide/` | Prevents nginx from interfering with the WebSocket connection. |

The API and console both use host networking, so nginx on the host reaches them at `127.0.0.1`.

## Observability with Langfuse

Langfuse gives you LLM level tracing. A multi turn session appears as one trace group carrying every tool call, every model response and every error.

Add Langfuse config to [`configs/app.json`](repo:configs/app.example.json).

```json title="configs/app.json"
{
  "langfuse": {
    "enabled": true,
    "public_key": "pk-lf-...",
    "secret_key": "sk-lf-...",
    "host": "https://cloud.langfuse.com"
  }
}
```

For a self hosted Langfuse instance, set `host` to your deployment URL.

By default [`configs/app.json`](repo:configs/app.example.json) mounts read write, so editing it on the host and running `docker compose up -d` picks up the change with no rebuild. That writable mount is also what lets the console's Settings page and [PATCH /api/config](endpoint:PATCH /api/config) save back to disk. Mount it read only to keep configuration host controlled, and accept that every save then fails with a 500.

> [!IMPORTANT] Mount the directory, not a single file
> Mount the whole `configs/` directory, not `configs/app.json` as a standalone file. A configuration save replaces `app.json` with an atomic rename, and renaming over a path that is itself a mount point fails with `EBUSY`. This is the shape [`docker-compose.yml`](repo:docker-compose.yml) uses by default, so do not narrow it in your own override.

### Filtering traces by provenance

Every trace is tagged at run start, so you can slice the dashboard by which product is burning tokens or which client surface sent a bad request. Facets appear as `key:value` trace tags.

| Facet | Example values |
|-------|----------------|
| `origin` | `user`, `wiki`, `search`, `channel`, `structured`, `draft`, `mobile`, `apps` |
| `product` | `agent`, `wiki`, `search`, `channel`, `structured`, `draft`, `mobile`, `apps`, `vcs` |
| `session_type` | `chat`, `wiki_index`, `wiki_qa`, `wiki_act`, `wiki_maintain`, `search_run`, `scg_map`, `channel_msg`, `structured_run`, `structured_fast`, `draft_stream`, `mobile_<platform>`, `app_agent`, `vcs_pickup` |
| `surface` | `api`, plus whatever clients stamp (CLI, console, channel platforms) |
| `project` | The named project the session ran against. |
| `repo` | Repository, e.g. `owner/repo`. |
| `branch` | Git branch. |
| `workspace` | Structured-response or search workspace. |
| `model` | Model id the session was created with. |

Higher cardinality facets land in trace metadata instead of the tag list. Filter on metadata when you want one specific id.

API clients stamp their surface with an optional `X-Mewbo-Surface` header, defaulting to `api` when absent. It is already in the CORS allow list, so browser clients can send it cross origin. A path that never stamps a surface shows up as `surface:unknown` rather than untagged, which keeps uninstrumented clients findable.

See [Troubleshooting](troubleshooting.md) for the recommended sequence, from the MongoDB transcript to Langfuse traces to config to Docker env.

## Health Monitoring

The API exposes no dedicated health endpoint. For uptime checks and alerting, probe [`GET /api/tools`](endpoint:GET /api/tools) with your API key. It returns a populated list when the API is healthy.

```bash
curl -sk http://localhost:5125/api/tools -H "X-API-Key: your-token" | jq length
```

For logs and container state, see [Troubleshooting](troubleshooting.md).

## API Token Rotation

Rotate `MEWBO_MASTER_API_TOKEN` in two steps.

1. Update `.env` with the new value.
   ```dotenv title=".env"
   MEWBO_MASTER_API_TOKEN=new-strong-random-token
   MEWBO_VITE_API_KEY=new-strong-random-token
   ```
2. Apply without rebuilding.
   ```bash
   docker compose up -d
   ```

The console reads `MEWBO_VITE_API_KEY` from the injected `runtime-config.js` at startup, so no image rebuild is needed. Existing browser sessions get a 401 and prompt for the new key on the next request.

Where secrets are managed outside the tree, rotate the value at the source instead and redeploy. Editing a local `.env` does nothing once a deployment stops reading it.

## Resource Limits

Every service in [`docker-compose.yml`](repo:docker-compose.yml) ships with memory and CPU limits, so a runaway process cannot take down the host.

| Service | Memory limit | CPU limit |
|---------|-------------|-----------|
| `api` | `4G` | `4.0` |
| `mongo` | `1G` | `1.5` |
| `mewbo-mcp` | `1G` | `1.0` |
| `console` | `256M` | `0.5` |
| `ide-proxy` | `128M` | `0.5` |

The API gets the largest envelope because LLM orchestration, wiki indexing, sub-agent fan out and Web IDE management all run inside it. Raise its memory limit if you index very large repositories. Override any limit like this.

```yaml title="docker-compose.override.yml"
services:
  api:
    deploy:
      resources:
        limits:
          memory: 8G
          cpus: '6.0'
```
