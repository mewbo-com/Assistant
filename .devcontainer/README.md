# Dev container

> [!IMPORTANT]
> **This is a standard Dev Container configuration, and
> [Grove](https://github.com/bearlike/Grove) drives coding agents through it.**
>
> Open the repo in VS Code with the Dev Containers extension and you get the
> stack described below. Grove reads the same configuration, runs a coding agent
> inside that container, and manages the agent runtime and lifecycle from there.
> Grove did not invent a format. It adapted the one your editor already reads.
>
> Install Grove if you want the agent side of that. Everything below stands on
> its own without it.

A self-contained dev stack for this monorepo. The goal is that an engineer,
human or agent, can do full stack work from inside it without reaching for
anything on the host.

It is a **compose stack**, not a single container:

| Service | What it is |
|---|---|
| `workspace` | where you work. Runs its **own** Docker daemon (docker-in-docker) |
| `mongo` | `mongo:7`, this stack's own database |

---

## The one thing that comes from outside

Egress to the LLM gateway and the tracing service. Nothing else.

Those are reached over ordinary LAN DNS from the default bridge network, so no
external docker network is joined and no hostname is committed to this repo.
The real hostnames live in the gitignored `.grove/config.local.json`. If you do
need this stack on a specific external network, put that in a gitignored
`docker-compose.override.yml` rather than in the committed file.

Everything else is inside: the database, the Docker daemon, the toolchain.

---

## Why docker-IN-docker, not the host socket

The workspace runs its own `dockerd`. Containers you start from inside are
**children of this container**, not siblings on the host. From in here,
`docker ps` shows your containers and nothing of the host's.

That is the point. Image builds, the demo stack, and the Web IDE all work
without the container being able to see or disturb anything on the host.

It also removes a trap the host-socket approach carries: with a mounted host
socket, every bind path you hand a container is resolved against the *host*
filesystem, so a relative bind like `./configs` silently resolves somewhere that
does not exist. Here the daemon and the files share one filesystem, so paths
mean what they say.

Cost: the service must run `privileged`. That is inherent to a nested daemon,
and is why this is a dev-only configuration.

---

## Your production data is not at risk

Mongo in this stack lands in `assistant_devcontainer_mongo-data`. That is a
different volume from the `assistant_mongo-data` your real compose stack uses.
The dev container never opens the production database.

That is also why the mongo volume is deliberately left **unnamed** in
`docker-compose.yml`: compose scopes it per project, so concurrent worktrees
each get their own. Giving it an explicit name would make every workspace share
one volume, and two `mongod` processes over one volume corrupt it.

---

## The tier model, and what forces a rebuild

Dependencies change far more often than toolchains, so they are kept out of the
image.

| Tier | Where | Cost | Put here |
|---|---|---|---|
| 0 | `Dockerfile` | minutes, rare | OS packages, Python, uv |
| 1 | `features` | cached layers | docker-in-docker, node, gh, claude-code |
| 2 | `postCreateCommand` → `bootstrap.sh create` | once per container | `uv sync`, `npm ci` |
| 3 | `postStartCommand` → `bootstrap.sh start` | every start, seconds | CA trust, git identity, app config |

> [!TIP]
> Adding a dependency should almost never touch the Dockerfile. Add a step to
> `init.d/` instead.

`init.d/` holds numbered, idempotent scripts that `bootstrap.sh` runs in order:

```
00-09  trust / certificates
10-19  git / identity
20-39  language dependencies
40-59  app-specific config
60-79  tooling and CLIs
80-99  checks (never fatal)
```

Each one phase-guards itself (`[ "$GROVE_INIT_PHASE" = "create" ] || exit 0`),
is safe to run twice, and skips with a logged reason rather than failing when an
optional resource is absent.

---

## Things that will bite you

**`moby: false` on the docker-in-docker feature is required.** The `moby-cli`
packages do not exist for Debian trixie, and the feature aborts the build rather
than degrading. The trixie base is itself fixed by a glibc 2.41 requirement, so
upstream Docker CE is the only way through. Do not flip it back without also
moving the base image.

**`init.d` scripts run as subprocesses, not sourced.** An `export` in one does
not reach the shell you later attach to. Environment that must persist belongs
in `/etc/environment` or `containerEnv`.

**The image needs `iptables` and `iproute2`.** The nested daemon needs them to
build its bridge network, and the workspace supervisor's egress allowlist aborts
container start without them. On this base they also need `/usr/sbin` on the
non-root `PATH`, which the Dockerfile sets explicitly.

**Under the supervisor's egress allowlist, only the workspace's own outbound
traffic is filtered.** Traffic from nested child containers is forwarded rather
than locally generated, so it does not traverse the same chain. Treat the
allowlist as a guard on what *you* reach, not as a boundary around the whole
nested stack.

**Teardown by the workspace supervisor is currently imperfect for compose-based
configs.** It recognises a compose project it did not mint but does not label
every service, so cleanup falls back to removing labelled containers
individually and may leave this stack's volumes behind. `docker compose -p
assistant_devcontainer down -v` clears them by hand.
