# Storage Backends

## Choose where sessions are kept

Mewbo keeps session transcripts, compaction summaries, titles and metadata in a pluggable backend. The default JSON store needs no dependencies and works straight after install. Switch to MongoDB for multiple API workers, persistence across container restarts, or the [Web IDE](web/ide.md), which keeps container state in MongoDB.

## JSON (Default)

The JSON driver writes one file per session under `$MEWBO_HOME/sessions/`, defaulting to `~/.mewbo/sessions/`.

```bash
# These two are equivalent. JSON is the default
MEWBO_STORAGE_DRIVER=json
# or simply leave it unset
```

There is no cross process locking, so this fits a single instance, local development and CLI sessions. It does not fit several API workers running at once, or any deployment that needs the Web IDE.

## MongoDB

```bash title=".env"
MEWBO_STORAGE_DRIVER=mongodb
MEWBO_MONGODB_URI=mongodb://mewbo:mewbo@localhost:27018/mewbo?authSource=admin
MEWBO_MONGODB_DATABASE=mewbo
```

> [!IMPORTANT] Required for the Web IDE
> The [Web IDE](web/ide.md) feature needs MongoDB to persist container state across API restarts. Without MongoDB, the Web IDE button stays disabled.

### Adding MongoDB to the Docker Compose Stack

MongoDB is already defined as a service in [`docker-compose.yml`](repo:docker-compose.yml). Point the driver at it from `.env`, or from `docker-compose.override.yml`.

```dotenv title=".env"
# .env
MEWBO_STORAGE_DRIVER=mongodb
MEWBO_MONGODB_URI=mongodb://mewbo:mewbo@127.0.0.1:27018/mewbo?authSource=admin
MEWBO_MONGODB_DATABASE=mewbo
```

The MongoDB service publishes `ports: ["${MEWBO_MONGO_PORT:-27018}:27017"]` on the host. The API container runs with `network_mode: host`, so `127.0.0.1:27018` reaches MongoDB from inside it. The same two variables work as an `environment:` block on the `api` service if you prefer keeping them in the override file.

### External MongoDB

For Atlas or another hosted service, set `MEWBO_MONGODB_URI` to your connection string. Include the `authSource` parameter where your provider requires it.

```dotenv title=".env"
MEWBO_MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/mewbo
MEWBO_MONGODB_DATABASE=mewbo
```

## Switching Between Drivers

Nothing migrates between drivers. Sessions written by the old store stay unreachable from the new one, in either direction.

Export what you want to keep before you switch.

1. Export sessions via [`GET /api/sessions/{session_id}/export`](endpoint:GET /api/sessions/{session_id}/export).
2. Change `MEWBO_STORAGE_DRIVER` and restart.

## Configuration Reference

| Variable / Config key | Source | Default | Description |
|----------------------|--------|---------|-------------|
| `MEWBO_STORAGE_DRIVER` | Env var | `json` | Storage driver: `json` or `mongodb`. Env var takes precedence over [`configs/app.json`](repo:configs/app.example.json). |
| `MEWBO_MONGODB_URI` | Env var | `mongodb://localhost:27017` | Full MongoDB connection URI. |
| `MEWBO_MONGODB_DATABASE` | Env var | `mewbo` | MongoDB database name. |
| `MEWBO_HOME` | Env var | `~/.mewbo` | Data root for the JSON driver. In Docker, set to `/app/data` (mapped to the `api-data` named volume). |
| `storage.driver` | [`configs/app.json`](repo:configs/app.example.json) | `json` | Config file equivalent of `MEWBO_STORAGE_DRIVER` (env var wins). |
| `storage.mongodb.uri` | [`configs/app.json`](repo:configs/app.example.json) | `mongodb://localhost:27017` | Config file equivalent of `MEWBO_MONGODB_URI` (env var wins). |
| `storage.mongodb.database` | [`configs/app.json`](repo:configs/app.example.json) | `mewbo` | Config file equivalent of `MEWBO_MONGODB_DATABASE` (env var wins). |
