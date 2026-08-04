# Get Started

## Installing Mewbo

Run the server, then pick a client. Setup for each client lives on its own page, and this page routes you there.

## Run the server {#run-the-server}

One backend powers the web console, the Android client, the REST API, and every integration. Pick one path.

### Docker (recommended) {#docker-quickstart}

Prebuilt images on GHCR, and the fastest path to a stack ready for production.

```bash
# 1. Create your environment file and edit the three required vars
cp .env.example .env

# 2. Pull and start
docker compose pull && docker compose up -d
```

Required variables in `.env`.

| Variable | Purpose |
|----------|---------|
| `MEWBO_MASTER_API_TOKEN` | API authentication token |
| `MEWBO_VITE_API_KEY` | Frontend key (must match `MEWBO_MASTER_API_TOKEN`) |
| `MEWBO_HOST_UID` / `MEWBO_HOST_GID` | Host user and group IDs (run `id` to find yours) |

[Docker Compose](deployment-docker.md) covers volume mounts, project directories, the reverse proxy, and runtime config.

### From source with uv {#api-setup}

Install [uv](https://docs.astral.sh/uv/), which manages Python 3.10+ for you. Then sync the API extra and start the server.

```bash
uv sync --extra api
uv run mewbo-api
```

The API listens on `127.0.0.1:5124` by default, where the Docker stack publishes it on host port 5125 instead. Swap `--extra api` for `--all-extras --all-groups` to pull in the wiki, MCP, and Home Assistant components plus the dev tooling.

Before your first run, set a provider key and a default model in `configs/app.json`. See [LLM Setup](llm-setup.md). Every other key has a default, listed in the [Configuration Reference](configuration.md).

## Pick your client {#pick-your-client}

One engine, the same behaviour and tools everywhere. Only the mode of access changes.

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="../terminal/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:terminal" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Terminal</span>
  <span class="ms-card__body">Ships with the base install and runs the engine locally, so it needs no separate server.</span>
</a>

<a class="ms-card" href="../web/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:app-window" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Web console</span>
  <span class="ms-card__body">A browser console over the REST API. Review sessions side by side and drive scheduled runs.</span>
</a>

<a class="ms-card" href="../android/">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:android" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Android</span>
  <span class="ms-card__body">Aura, the native Android client. Start and steer sessions from your phone.</span>
</a>

<a class="ms-card" href="../api/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:braces" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">API</span>
  <span class="ms-card__body">The REST API behind every surface. Drive sessions from CI, cron, or your own app.</span>
</a>

</div>

## More ways to connect {#integrations}

- [Home Assistant](clients-home-assistant.md): voice control across every exposed sensor and device.
- [Email](clients-email.md): send a normal email and get a styled reply minutes later.
- [Nextcloud Talk](clients-nextcloud-talk.md): a chat adapter for the channels your team already uses.
- [MCP server](clients-mcp.md): expose Mewbo to Claude Code, Codex, Cursor, or another agent fleet over MCP.

## Project instructions {#project-instructions}

Place a `CLAUDE.md`, `AGENTS.md`, or `.claude/rules/*.md` at your project root and it loads at session start. Both the Claude Code and AGENTS.md conventions work unchanged. [Project Configuration](project-configuration.md) has the full loading strategy, including nested packages and on-demand instruction files.
