# mewbo-ide — the Web IDE container broker

`mewbo-ide` is a small Fastify service whose entire job is to be **the only process
holding `/var/run/docker.sock`**. It creates, inspects, extends and removes the
per-session `code-server` containers behind Mewbo's "Open in Web IDE" feature, on
behalf of the Python API — which no longer has the socket at all.

## Why it exists

The API process runs an agent, and that agent runs arbitrary shell. Mounting the
docker socket into that container makes every one of those shells a root shell on
the host: `docker run -v /:/host` is not an exploit, it is the documented behaviour
of the socket. The Web IDE feature needs about six docker calls; the socket grants
unlimited ones.

So the socket moves to a process that does nothing else. The broker has no LLM, no
tool loop, no plugins, no database and no user input beyond three fields on four
routes. Whoever compromises the API now gets a client of this HTTP API, not a
daemon.

**A generic socket proxy would not have worked.** Filtering by HTTP verb and path
(`POST /containers/create`) still lets the caller choose the request BODY — and the
body is where `Binds`, `Privileged`, `Image` and `NetworkMode` live. Restricting
those means understanding the docker API's payload semantics, which is a
domain-specific decision, not a proxy rule. The broker takes the other side of that
trade: it never accepts a spec, so there is nothing to filter.

## The rule everything else follows

**The broker CONSTRUCTS the container spec. It never accepts one.**

Image, binds, network, memory, CPU, PID limits, entrypoint, labels and the container
NAME all come from the broker's own configuration. Exactly three caller-supplied
values reach a container:

| Value | Where the broker puts it | Why it is safe |
|---|---|---|
| `password` | the `PASSWORD` env var | an opaque value in a field the broker chooses; the API already stores it and hands it to the browser |
| `workspace_path` | the bind SOURCE, after realpath + allowlist | resolved in the broker's own mount namespace and refused if it escapes |
| `ttl_seconds` | an epoch in the deadline file | an integer within fixed bounds |

Every request body is parsed with a Zod `.strict()` object, so a caller smuggling an
`image` or a `binds` list gets a clean `400` rather than a field that is silently
ignored.

## Configuration

Environment only, validated at boot. An invalid value stops the process with a
message naming the variable — it never degrades into a running broker with a
permissive default.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MEWBO_IDE_BROKER_TOKEN` | **yes** | — | shared secret, minimum 16 chars. Distinct from `MEWBO_MASTER_API_TOKEN`. |
| `MEWBO_IDE_ALLOWED_ROOTS` | **yes** | — | colon-separated ABSOLUTE paths. Empty or unset and the process refuses to start. |
| `MEWBO_IDE_BROKER_HOST` | no | `0.0.0.0` | the address INSIDE the container — see below |
| `MEWBO_IDE_BROKER_PORT` | no | `5128` | |
| `MEWBO_IDE_STATE_DIR` | no | `/tmp/mewbo-ide` | where deadline files live |
| `MEWBO_IDE_IMAGE` | no | `codercom/code-server:latest` | |
| `MEWBO_IDE_NETWORK` | no | `mewbo-ide` | |
| `MEWBO_IDE_MEMORY` | no | `1g` | matches `^\d+[mgMG]$` |
| `MEWBO_IDE_CPUS` | no | `1.0` | |
| `MEWBO_IDE_PIDS_LIMIT` | no | `512` | |
| `DOCKER_SOCKET` | no | `/var/run/docker.sock` | |

`MEWBO_IDE_BROKER_HOST` defaults to `0.0.0.0` because it is the address the broker
binds **inside its container**, where loopback is a private namespace: docker
forwards a published port to the container's bridge IP and never to its loopback.
Binding `127.0.0.1` there leaves the broker listening where nothing can reach it —
and the healthcheck, which runs inside the container, still passes. What keeps the
broker off the network is the compose publish (`127.0.0.1:5128:5128`) and the shared
secret on every `/v1` route, not this address.

The allowlist is **required rather than defaulted** because a default of
"everything" is a fail-open filter written once in the callee. Refusing to boot is
diagnosable; silently accepting every path on the host is not.

The broker must also **mount the allowed roots at identical paths**. It resolves
`workspace_path` with `realpath` in its own mount namespace, so a path it cannot see
cannot be validated — and that resolution is what makes the symlink-escape check
real rather than a string prefix test.

## Routes

Base `http://127.0.0.1:5128`. Every `/v1` route requires `X-Broker-Token`; missing
or wrong is `401`. `session_id` must match `^[a-f0-9]{32}$` or the answer is `400`.

Every non-2xx carries the same envelope:

```json
{ "error": { "code": "workspace_denied", "reason": "human sentence", "retryable": false } }
```

**Success bodies carry only what the caller cannot work out for itself.** Create and
extend answer with `{}` — the status code is the whole answer. The container name and
the browser URL are derived from the session id on both sides, and the API owns
`expires_at` authoritatively in its own store, so echoing any of them back would be a
second channel for a fact that already has one. The caller parses responses with
`extra="forbid"`, which means an unread field is not free: it is a field that has to
stay in lockstep across two languages for nobody's benefit.

### `GET /healthz` — unauthenticated

`200 {"status":"ok"}`. Liveness only: it touches neither the daemon nor the
filesystem, so it keeps answering while docker is down. It is unauthenticated
because a health probe that needs the shared secret cannot be wired into compose.

### `POST /v1/ide/:session_id` — create or replace

Body `{ workspace_path, ttl_seconds, password }`, where `ttl_seconds` is 60..604800
and `password` matches `^[A-Za-z0-9_-]{16,128}$`.

Work happens in this order, and the order is load-bearing: validate the body,
resolve and allowlist-check the workspace, **write the deadline file**, force-remove
any existing `mewbo-ide-<sid>`, then create and start. The deadline file must exist
before the container does, because docker materializes a missing bind source as a
directory — `cat /mewbo/deadline` would then fail forever.

`201 {}`

Errors: `400` bad body or session id · `401` · `403` `workspace_denied` · `502`
`docker_error` · `503` `docker_unavailable`.

### `GET /v1/ide/:session_id` — status

`200 { "status": "absent" | "running" | "exited" }`

**It never 404s — `absent` is a status, not an error.** A 404 would force the caller
to distinguish "no container" from "the broker is broken", and the cheap reading of
that ambiguity treats both as "not running", hiding an outage behind a normal-looking
empty state.

### `POST /v1/ide/:session_id/extend`

Body `{ ttl_seconds }`. Rewrites the deadline file and answers `200 {}`.

It deliberately does **not** require the container to exist. Rewriting the file IS
the extension: the watchdog inside the container re-reads it on its own 15s poll, so
nothing needs to be signalled or restarted.

### `DELETE /v1/ide/:session_id`

Force-removes the container if present and unlinks the deadline file.
`200 { "removed": boolean }` — true if EITHER happened, so a partially-created
session still reports that something was cleaned up. This is the API's ONLY teardown
call: its container backend has a single `teardown` operation, because splitting the
container from the deadline would make one teardown two round trips here, the second
of which could only ever report that it found nothing left.

## How a container stops itself

The broker sets an entrypoint that starts a watchdog beside `code-server`:

```sh
(while [ $(date +%s) -lt $(cat /mewbo/deadline) ]; do sleep 15; done; kill 1) & exec /usr/bin/entrypoint.sh ...
```

`/mewbo/deadline` is the broker's per-session file, bind-mounted read-only. It holds
bare ASCII epoch seconds with **no trailing newline** — `sh` refuses an integer
comparison against a token it cannot parse, so a stray newline would kill the
container on its first tick.

## Startup sweep

On boot the broker lists containers (including stopped ones) filtered by the
`mewbo.kind=web-ide` **label** and force-removes any that are not running, have no
deadline file, or are past their deadline.

**Filtering by label rather than by the `mewbo-ide-` name prefix is not a
preference.** The nginx reverse proxy is named `mewbo-ide-proxy` and matches that
prefix; reaping it takes the whole feature down. The label is also re-checked per
item rather than trusted to the daemon-side filter, because that particular mistake
is unrecoverable.

A sweep failure logs and carries on — a broker that refuses to serve `/healthz`
because it could not tidy up is harder to diagnose than one that says so and answers.

## Development

```bash
npm install
npm run dev        # tsx watch
npm run typecheck
npm run lint
npm test           # watch
npm run test:ci    # single run, what CI gates on
npm run build && npm start
```

The tests inject a fake dockerode-shaped client, so none of them need a live daemon.
The allowlist tests create real symlinks in a temporary directory rather than
stubbing `realpath` — the escape they guard against is a filesystem fact, and a
stubbed resolver would only ever prove the stub.
