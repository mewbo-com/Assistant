# Docker Compose Deployment

## Run the whole stack locally

Docker Compose is the recommended way to run Mewbo persistently. Prebuilt images ship from GHCR, and one `docker compose up` starts every service listed below.

## Quick Start

Copy the template, set three values in it, then pull and start.

```bash
cp .env.example .env
```

```dotenv title=".env"
MEWBO_MASTER_API_TOKEN=your-strong-random-token
MEWBO_VITE_API_KEY=your-strong-random-token   # must match MEWBO_MASTER_API_TOKEN
MEWBO_HOST_UID=1000                            # output of `id -u`
```

```bash
docker compose pull && docker compose up -d
```

The console runs at `http://localhost:3001`, the API at `:5125`, and the MCP server at `:5127`.

## Environment Variables

Every compose service reads `.env` through `env_file: ${MEWBO_ENV_FILE:-.env}`. The `MEWBO_` prefix stops a collision with a same named variable in your shell or CI. The two `MONGO_INITDB_` names stay bare because the official `mongo` image reads them, not Mewbo.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MEWBO_MASTER_API_TOKEN` | Yes | _(none)_ | API authentication token. Set a strong random value. |
| `MEWBO_VITE_API_KEY` | Yes | _(none)_ | Frontend API key. Must match `MEWBO_MASTER_API_TOKEN`. |
| `MEWBO_HOST_UID` | Yes | `1000` | UID the API container runs as. Run `id -u` to find yours. |
| `MEWBO_HOST_GID` | Yes | `1000` | GID the API container runs as. Run `id -g` to find yours. |
| `MEWBO_API_PORT` | No | `5125` | Host port for the API. |
| `CORS_ORIGIN` | No | `*` | CORS allowed origin. Set to your domain in production. |
| `MEWBO_VITE_API_BASE_URL` | No | _(empty)_ | API URL the browser sends requests to. Leave empty behind the nginx proxy, which proxies `/api/` internally. |
| `MEWBO_DOCKER_GID` | No | `999` | GID of the host `docker` group, needed by the `mewbo-ide` broker. Run `stat -c '%g' /var/run/docker.sock`. |
| `MEWBO_MONGO_PORT` | No | `27018` | Host port MongoDB is exposed on. |
| `MONGO_INITDB_ROOT_USERNAME` | No | `mewbo` | MongoDB root username. |
| `MONGO_INITDB_ROOT_PASSWORD` | No | `mewbo` | MongoDB root password. Change this in production. |
| `MEWBO_AGENTIC_SEARCH_SEED` | No | `1` | Agentic Search demo-data seeding. Set `0` so a production install lists only workspaces and sources you configured. |
| `MEWBO_IDE_BROKER_TOKEN` | Yes, to use Web IDE | _(none)_ | Shared secret the API and the `mewbo-ide` broker both read, distinct from `MEWBO_MASTER_API_TOKEN`. The broker refuses to boot without it. |
| `MEWBO_IDE_ALLOWED_ROOTS` | Yes, to use Web IDE | _(none)_ | Colon-separated absolute paths the broker will launch an IDE against. Each must also be mounted. See "Web IDE" below. |
| `MEWBO_IDE_BROKER_URL` | No | `http://127.0.0.1:5128` | Already set on the `api` service in [`docker-compose.yml`](repo:docker-compose.yml). Override only if you moved the broker. |

The console's host port is fixed at `3001`. It uses host networking rather than a port mapping, so there is no `PORT` variable.

LLM provider keys, Langfuse credentials and other runtime settings belong in `configs/app.json`, not `.env`. See [Configuration](configuration.md).

## Referencing `.env` from `configs/app.json`

A `configs/app.json` value can name an environment variable instead of embedding a literal. Write `${VARIABLE_NAME}` as the whole value, and the secret never has to be written into the file.

```json title="configs/app.json"
{ "api": { "master_token": "${MEWBO_MASTER_API_TOKEN}" } }
```

A named variable that is not set is refused at startup rather than silently read as empty. One set to the empty string resolves to empty, since it is set. Every key that accepts this is marked in [Configuration](configuration.md).

## Services

The [Compose file](repo:docker-compose.yml) defines six services.

| Service | Image | Default port | Purpose |
|---------|-------|-------------|---------|
| `api` | `ghcr.io/bearlike/mewbo-api` | `5125` | Gunicorn + Flask REST API. Runs as `MEWBO_HOST_UID:MEWBO_HOST_GID`. |
| `mewbo-mcp` | `ghcr.io/bearlike/mewbo-mcp` | `5127` | MCP server for external agents. A thin shim that proxies tool calls to the API. |
| `console` | `ghcr.io/bearlike/mewbo-console` | `3001` | nginx serving the React SPA. Proxies `/api/` to the API. |
| `mongo` | `mongo:7` | `27018` (host) | MongoDB for session storage and Web IDE state. |
| `mewbo-ide` | `ghcr.io/bearlike/mewbo-ide` | `127.0.0.1:5128` | Web IDE broker. The only service holding `/var/run/docker.sock`. Launches and reaps the per-session code-server containers. |
| `ide-proxy` | `nginx:1.27-alpine` | `127.0.0.1:5126` | nginx reverse proxy for per-session code-server containers. |

`api`, `mewbo-mcp` and `console` use host networking, so they share `127.0.0.1` with the host. That is how `api` reaches `mewbo-ide` at `http://127.0.0.1:5128`. `ide-proxy` sits on the separate `mewbo-ide` bridge network, loopback bound, to reach the code-server containers the broker attaches there.

`mewbo-mcp` reads the same `.env` and mounts the same `api-data` volume, so it shares the master token and the key store. Keys issued via [`POST /api/keys`](endpoint:POST /api/keys) work on the MCP server too.

## Named Volumes

| Volume | Mounted at | Contains |
|--------|-----------|---------|
| `api-data` | `/app/data` | Session transcripts and summaries. Shared with `mewbo-mcp` for the key store. |
| `mongo-data` | `/data/db` (MongoDB) | MongoDB data files. |
| `plans-data` | `/tmp/mewbo/plans` | Plan-mode scratch files (survive restarts). |
| `wiki-clones` | `/tmp/mewbo/wiki/clones` | Git clones made by wiki indexing (survive restarts). |

All named volumes survive `docker compose down`. Only `docker compose down -v` or `docker volume rm` clears them.

## Mounting Project Directories

Mount your project directories through a `docker-compose.override.yml`, which Compose loads automatically.

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
# Edit to add your project paths
```

> [!IMPORTANT]
> Mount each project at the **same absolute path** as on the host. `configs/app.json` stores project paths, and they must match inside and outside the container.

**Example `docker-compose.override.yml`**

```yaml title="docker-compose.override.yml"
services:
  api:
    volumes:
      - ./docker/init.d:/app/docker/init.d:ro
      - /home/you/Projects/my-project:/home/you/Projects/my-project
      - /home/you/Projects/another-repo:/home/you/Projects/another-repo
```

## Post-Init Scripts

Scripts in [`docker/init.d/`](repo:docker/init.d) run inside the `api` container before Gunicorn starts, in filename order. They are sourced rather than executed, so they can export environment variables into the API process. A failing script logs a warning and lets startup continue.

| Script | Purpose |
|--------|---------|
| [`00-toolbox.sh`](repo:docker/init.d/00-toolbox.sh) | Defines the shared download and install helpers. Provisions nothing itself, and runs first so every later script inherits them. |
| [`10-git-setup.sh`](repo:docker/init.d/10-git-setup.sh) | With `GITHUB_TOKEN` set and `gh` installed, configures `git credential.helper` for non-interactive auth. Sets `safe.directory '*'` so volume-mounted repos are trusted regardless of file ownership. |
| [`12-agent-clis.sh`](repo:docker/init.d/12-agent-clis.sh) | Installs the forge CLIs `gh` and `tea` into the runtime toolbox as static binaries, needing neither apt nor root. Pin a version from `.env`. |
| [`13-models-cli.sh`](repo:docker/init.d/13-models-cli.sh) | Installs the `models` CLI for model pricing, benchmarks and provider status. It carries no credentials. |
| [`15-tea-setup.sh`](repo:docker/init.d/15-tea-setup.sh) | Derives `tea` logins from tokens already in the container. Runs after `12-`, which installs the binary it authenticates. A non-Gitea host is skipped with a warning rather than failing startup. |

The CLIs install here rather than in the Dockerfile because identity arrives at runtime, not at build time.

To add your own, mount [`docker/init.d/`](repo:docker/init.d) as shown above and drop `.sh` files in. No rebuild is needed.

A common addition trusts a private root CA. Drop the PEM at `/usr/local/share/ca-certificates/<name>.crt`, keeping the required `.crt` extension, run `update-ca-certificates`, then export `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` so `git clone` and Python HTTPS clients verify against it.

## Runtime Config Injection

The console image generates `runtime-config.js` at container startup from `MEWBO_VITE_API_BASE_URL` and `MEWBO_VITE_API_KEY`. Update `.env` and run `docker compose up -d` to change the API URL or key. No rebuild is required.

## Web IDE

The `api` container has no access to `/var/run/docker.sock` and no Docker tooling, because it runs the agent's arbitrary shell tool. Daemon access there would let any shell command reach the whole host as root through `docker run -v /:/host`.

Spawning the per session code-server container is delegated over HTTP to the `mewbo-ide` broker instead, which accepts only a workspace path, a TTL and a password, never an arbitrary container spec. The API takes this path once `MEWBO_IDE_BROKER_TOKEN` and `MEWBO_IDE_BROKER_URL` are set, already wired in `.env` and [`docker-compose.yml`](repo:docker-compose.yml). Without a broker it falls back to a local Docker daemon, for local development only.

Enable it in four steps.

1. Set `MEWBO_IDE_BROKER_TOKEN` in `.env` to a strong random secret.
2. Set `MEWBO_DOCKER_GID` in `.env` to the GID of the `docker` group on your host.
   ```bash
   stat -c '%g' /var/run/docker.sock
   ```
3. Set `MEWBO_IDE_ALLOWED_ROOTS` in `.env` to a colon separated list of absolute project paths.
4. Mount each of those paths read only into the `mewbo-ide` service at the identical path, following [`docker-compose.override.example.yml`](repo:docker-compose.override.example.yml). The broker resolves a requested path to its real path in its own mount namespace, so an unmounted path fails validation even when the string is allowlisted.

The broker binds to `127.0.0.1:5128`, loopback only, and is never published off host. Anyone who can reach it can ask the daemon to launch an arbitrary container as root.

Lifetimes, resource limits and the endpoint contract live on [Web IDE](web/ide.md).

## Building from Source

Build locally instead of pulling from GHCR.

```bash
docker compose up --build -d
```

## Images

The Services table above names the image for each running container. `ghcr.io/bearlike/mewbo-base` never runs on its own. It ships Python, Node and the core packages as the build arg for the API and MCP images.

For production TLS, CORS hardening, and observability setup, see [Production Setup](deployment-production.md).
