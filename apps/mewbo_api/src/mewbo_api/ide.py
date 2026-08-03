"""Per-session "Open in Web IDE" orchestration.

Single module containing four cleanly separated sections:

- ``IdeInstance``: immutable dataclass describing one running container.
- ``IdeStore``: thin MongoDB wrapper (the single source of truth for state).
- ``IdeContainerBackend`` + its two implementations: the one seam through
  which privileged container work happens.
- ``IdeManager``: lifecycle policy (spawn, extend, stop, health probe).

Mongo is the single source of truth. Container labels are written for debug /
disaster recovery only and are never read back to hydrate state. Drift between
Mongo and Docker is reconciled lazily in ``get()``.

Containers self-terminate: a watchdog started inside the entrypoint polls the
bind-mounted ``/mewbo/deadline`` file and sends ``kill 1`` once the wall
clock passes the stored epoch. Extending a session just overwrites that file.

Reaching the docker socket is a whole-host privilege, so it is confined to the
ONE collaborator injected into ``IdeManager``. ``DockerContainerBackend`` talks
to the daemon directly; ``BrokerContainerBackend`` sends coordinates to a
separate broker service that holds the socket instead. Everything above that
seam — TTL policy, the extension counter, Mongo writes, drift reconciliation,
the readiness probe — is identical either way and needs no daemon to test.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

from loguru import logger
from mewbo_core.config import WebIdeConfig
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only so this module never imports the broker client at runtime: the
    # client raises ``DockerUnavailable`` from here, and an import in both
    # directions would cycle. The composition root constructs it and injects it.
    from mewbo_api.ide_broker import IdeBrokerClient

try:
    from docker.errors import APIError, DockerException, NotFound

    # Its own block because the repo root holds a ``docker/`` directory, so the
    # import sorter reads the bare package name as first-party.
    from docker import from_env as docker_from_env  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - the optional ``docker`` extra is absent
    # The API image ships without the SDK once a broker holds the socket. The
    # module must still import — every unrelated route mounts from the same
    # app — so the absence surfaces where a caller can act on it:
    # ``DockerContainerBackend`` refuses to construct.
    class _DockerSdkMissing(Exception):
        """Stand-in so this module's ``except`` clauses stay valid without the SDK."""

    docker_from_env = None
    APIError = DockerException = NotFound = _DockerSdkMissing

UTC = timezone.utc
SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
IdeStatus = Literal["pending", "starting", "ready"]

# Container names are DERIVED from the session id, never accepted from a caller.
CONTAINER_NAME_PREFIX = "mewbo-ide-"

DOCKER_SDK_MISSING = (
    "the docker SDK is not installed; install the 'docker' extra of mewbo-api, "
    "or point agent.web_ide.broker_url at an IDE broker"
)

# Default editor theme, seeded into every container's user settings rather
# than left to code-server's stock default. Verified against a real
# ``codercom/code-server:latest`` container, not assumed: its bundled
# ``theme-monokai`` extension contributes exactly one theme, id ``"Monokai"``
# (``extensions/theme-monokai/package.json``).
DEFAULT_COLOR_THEME = "Monokai"

# Where code-server reads user settings from, inside the container. Verified
# by running the image and reading its own log line
# (``Using user-data-dir /home/coder/.local/share/code-server``), then the
# VS Code convention of ``<user-data-dir>/User/settings.json`` underneath it —
# confirmed by starting the container and observing the ``User/`` directory
# created on first HTTP request.
CODE_SERVER_SETTINGS_PATH = "/home/coder/.local/share/code-server/User/settings.json"

# Container watchdog: the only ``sh -c`` string in the feature.
# ``{sid}`` is interpolated from a value that MUST be regex-validated to
# ``^[a-f0-9]{32}$`` before reaching this code path.
WATCHDOG_CMD = (
    "(while [ $(date +%s) -lt $(cat /mewbo/deadline) ]; do sleep 15; done; "
    "kill 1) & "
    "exec /usr/bin/entrypoint.sh --auth password --bind-addr 0.0.0.0:8080 "
    "--disable-telemetry --disable-update-check "
    "--abs-proxy-base-path /ide/{sid} /home/coder/project"
)

# Readiness-probe path, appended to the configurable ide-proxy base URL
# (``WebIdeConfig.proxy_url``). The base varies with the API's network
# topology, so it is never hardcoded here.
PROBE_PATH = "/ide/{sid}/healthz"


class MaxLifetimeReached(Exception):
    """Raised when an extension would exceed ``max_deadline``."""

    def __init__(self, max_deadline: datetime) -> None:
        """Store the hard cap that blocked the extension."""
        super().__init__(f"max lifetime reached: {max_deadline.isoformat()}")
        self.max_deadline = max_deadline


class DockerUnavailable(Exception):
    """Raised when the docker daemon is unreachable."""


# ---------------------------------------------------------------------------
# IdeInstance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdeInstance:
    """State of a single running code-server container for one session.

    Resource limits are deliberately NOT recorded here. Under
    ``BrokerContainerBackend`` the broker builds them from its OWN environment
    (``MEWBO_IDE_CPUS`` / ``MEWBO_IDE_MEMORY``) and nothing reconciles the two,
    so stamping ``WebIdeConfig``'s values onto the record persisted and served
    numbers that need not describe the running container. The limits live where
    they are applied, on whichever backend built the container.
    """

    session_id: str
    status: IdeStatus
    project_name: str
    project_path: str
    password: str
    created_at: datetime
    expires_at: datetime
    max_deadline: datetime
    extensions: int

    @property
    def url(self) -> str:
        """Return the browser-facing URL behind ``ide-proxy``."""
        return f"/ide/{self.session_id}/"

    @staticmethod
    def name_for(session_id: str) -> str:
        """Return the deterministic docker container name for a session id.

        The ONE home for that derivation: the backend needs it without holding
        an instance, and a second copy of the rule is how the two drift.
        """
        return f"{CONTAINER_NAME_PREFIX}{session_id}"

    @property
    def container_name(self) -> str:
        """Return the deterministic docker container name."""
        return self.name_for(self.session_id)

    @property
    def remaining_seconds(self) -> int:
        """Return seconds until ``expires_at``, floored at zero."""
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))

    def to_dict(self, *, include_password: bool = False) -> dict[str, object]:
        """Serialize to the JSON shape returned by the API routes.

        ``password`` is included only on ``POST`` responses so the secret
        isn't re-sent on every 30s status poll from the session page.
        """
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "status": self.status,
            "url": self.url,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "max_deadline": self.max_deadline.isoformat(),
            "remaining_seconds": self.remaining_seconds,
            "extensions": self.extensions,
        }
        if include_password:
            payload["password"] = self.password
        return payload


# ---------------------------------------------------------------------------
# IdeStore
# ---------------------------------------------------------------------------


class IdeStore:
    """Thin MongoDB wrapper for the ``ide_instances`` collection."""

    def __init__(self, db: Database) -> None:
        """Bind to a database and ensure the unique index exists."""
        self._col: Collection = db["ide_instances"]
        self._col.create_index("session_id", unique=True, name="ux_ide_session_id")

    def get(self, session_id: str) -> IdeInstance | None:
        """Return the stored instance or ``None`` if absent."""
        doc = self._col.find_one({"session_id": session_id})
        if doc is None:
            return None
        return self._from_doc(doc)

    def insert(self, instance: IdeInstance) -> None:
        """Insert a new instance. Raises ``DuplicateKeyError`` on race."""
        # Build the document explicitly so the persisted schema is obvious
        # and cannot accidentally pick up new fields that belong in-memory only.
        self._col.insert_one(
            {
                "session_id": instance.session_id,
                "project_name": instance.project_name,
                "project_path": instance.project_path,
                "password": instance.password,
                "created_at": instance.created_at,
                "expires_at": instance.expires_at,
                "max_deadline": instance.max_deadline,
                "extensions": instance.extensions,
            }
        )

    def update_expiry(self, session_id: str, *, expires_at: datetime, extensions: int) -> None:
        """Atomically bump ``expires_at`` and the extension counter."""
        self._col.update_one(
            {"session_id": session_id},
            {"$set": {"expires_at": expires_at, "extensions": extensions}},
        )

    def delete(self, session_id: str) -> bool:
        """Delete the doc and return whether one was actually removed."""
        return self._col.delete_one({"session_id": session_id}).deleted_count > 0

    @staticmethod
    def _from_doc(doc: dict[str, object]) -> IdeInstance:
        """Hydrate an ``IdeInstance`` from a Mongo document (status defaults to pending)."""
        return IdeInstance(
            session_id=str(doc["session_id"]),
            status="pending",
            project_name=str(doc["project_name"]),
            project_path=str(doc["project_path"]),
            password=str(doc["password"]),
            created_at=_as_utc(doc["created_at"]),
            expires_at=_as_utc(doc["expires_at"]),
            max_deadline=_as_utc(doc["max_deadline"]),
            extensions=int(doc.get("extensions") or 0),  # type: ignore[call-overload]
        )


def _as_utc(value: object) -> datetime:
    """Coerce a Mongo datetime (possibly naive) to a UTC-aware datetime."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# IdeContainerBackend — the one privileged seam
# ---------------------------------------------------------------------------


class IdeContainerBackend(Protocol):
    """Every operation that needs container privileges, and nothing else.

    **Keyed by SESSION ID throughout.** The container name is derived from the
    session id everywhere it appears, so a protocol that spoke names would force
    a session-keyed backend to un-derive one on the way back out — a round trip
    through a vocabulary only one implementation uses.

    ``launch`` and ``teardown`` each merge the deadline file with the container
    because for a backend that owns neither the filesystem nor the daemon
    locally they are one indivisible step: the broker does both inside a single
    request. Splitting them here would make a teardown two round trips, the
    second of which could only ever report that it found nothing left to remove.
    """

    def launch(self, instance: IdeInstance) -> None:
        """Write the instance's deadline and start its container."""
        ...

    def is_running(self, session_id: str) -> bool:
        """Return True iff the session's container exists AND is running."""
        ...

    def write_deadline(self, session_id: str, expires_at: datetime) -> None:
        """Rewrite the deadline the container's watchdog polls."""
        ...

    def teardown(self, session_id: str) -> bool:
        """Force-remove the container AND drop its deadline.

        Returns whether either existed. Best-effort: never raises.
        """
        ...


class DockerContainerBackend:
    """Drive the docker daemon directly from this process.

    Requires a reachable socket, which grants whole-host root — so this is the
    path a developer takes against a local daemon, and the path a deployment
    takes only when it has no broker configured.
    """

    def __init__(self, config: WebIdeConfig, docker_client: Any = None) -> None:
        """Bind to the feature config and (optionally) a pre-built docker client."""
        if docker_from_env is None:
            raise DockerUnavailable(DOCKER_SDK_MISSING)
        self._cfg = config
        self._client = docker_client
        os.makedirs(self._cfg.state_dir, exist_ok=True)
        self._write_default_settings()

    # -- backend protocol --------------------------------------------------

    def launch(self, instance: IdeInstance) -> None:
        """Write the deadline file, then start the container that bind-mounts it."""
        self._write_deadline(self._deadline_file(instance.session_id), instance.expires_at)
        self._run_container(instance)

    def is_running(self, session_id: str) -> bool:
        """Return True iff the session's container exists AND is running.

        A container that has self-terminated via the watchdog is in the
        ``exited`` state but still resolvable by ``containers.get`` — we
        treat those as dead so the reconnect path in ``ensure()`` can
        force-remove and respawn instead of handing back stale state.

        Lets ``DockerUnavailable`` propagate so callers can 503 instead of
        silently destroying persistent Mongo state on a transient daemon blip.
        """
        container_name = IdeInstance.name_for(session_id)
        client = self._docker()
        try:
            container = client.containers.get(container_name)
        except NotFound:
            return False
        except APIError as exc:
            logger.warning("ide: docker API error while checking {}: {}", container_name, exc)
            return False
        return bool(container.status == "running")

    def write_deadline(self, session_id: str, expires_at: datetime) -> None:
        """Overwrite the deadline file the running container's watchdog polls."""
        self._write_deadline(self._deadline_file(session_id), expires_at)

    def teardown(self, session_id: str) -> bool:
        """Force-remove the container and unlink its deadline file.

        Two local operations against two different subsystems, which is why the
        protocol lets a backend own the pairing rather than making the caller
        sequence them: a docker outage must still leave the deadline file
        cleaned up, and vice versa.
        """
        removed_container = self._remove_container(IdeInstance.name_for(session_id))
        removed_deadline = self._safe_remove_file(self._deadline_file(session_id))
        return removed_container or removed_deadline

    # -- docker internals --------------------------------------------------

    def _remove_container(self, container_name: str) -> bool:
        """Force-remove a container, swallowing every error.

        Best-effort by contract: a docker outage here must not prevent the
        caller from also cleaning up Mongo and the deadline file.
        """
        try:
            client = self._docker()
        except DockerUnavailable:
            return False
        try:
            container = client.containers.get(container_name)
        except NotFound:
            return False
        except APIError as exc:
            logger.warning("ide: docker API error locating {}: {}", container_name, exc)
            return False
        try:
            container.remove(force=True)
            return True
        except NotFound:
            return False
        except APIError as exc:
            logger.warning("ide: failed to remove container {}: {}", container_name, exc)
            return False

    def _docker(self) -> Any:
        """Lazy-connect to the docker daemon; raise ``DockerUnavailable`` on failure."""
        if self._client is not None:
            return self._client
        try:
            self._client = docker_from_env()
        except DockerException as exc:
            raise DockerUnavailable(str(exc)) from exc
        return self._client

    def _run_container(self, instance: IdeInstance) -> None:
        """Invoke ``containers.run`` with all structured kwargs."""
        client = self._docker()
        labels = {
            "mewbo.kind": "web-ide",
            "mewbo.session_id": instance.session_id,
            "mewbo.project_name": instance.project_name,
            "mewbo.created_at": instance.created_at.isoformat(),
            "mewbo.max_deadline": instance.max_deadline.isoformat(),
        }
        volumes = {
            instance.project_path: {"bind": "/home/coder/project", "mode": "rw"},
            self._deadline_file(instance.session_id): {
                "bind": "/mewbo/deadline",
                "mode": "ro",
            },
        }
        volumes[self._settings_file()] = {
            "bind": CODE_SERVER_SETTINGS_PATH,
            "mode": "ro",
        }
        entrypoint = ["sh", "-c", WATCHDOG_CMD.format(sid=instance.session_id)]
        client.containers.run(
            image=self._cfg.image,
            name=instance.container_name,
            entrypoint=entrypoint,
            environment={"PASSWORD": instance.password},
            volumes=volumes,
            labels=labels,
            network=self._cfg.network,
            mem_limit=self._cfg.memory,
            nano_cpus=int(self._cfg.cpus * 1e9),
            pids_limit=self._cfg.pids_limit,
            detach=True,
            auto_remove=False,
        )

    # -- filesystem helpers ------------------------------------------------

    def _deadline_file(self, session_id: str) -> str:
        """Return the absolute path of the deadline bind-mount file."""
        return os.path.join(self._cfg.state_dir, f"{session_id}.deadline")

    def _settings_file(self) -> str:
        """Return the absolute path of the seeded user-settings bind-mount file.

        One file, shared by every container this backend starts — the theme
        is broker/backend configuration, not a per-session value, unlike the
        deadline file.
        """
        return os.path.join(self._cfg.state_dir, "default-settings.json")

    def _write_default_settings(self) -> None:
        """Write the seeded settings file once, at construction.

        A bind whose SOURCE is missing when the container is created
        materializes as a directory rather than a file, so this must exist
        before the first ``_run_container`` call — the same ordering
        constraint ``launch`` already respects for the deadline file.

        Read-only, and there is nowhere for a user's in-session theme change
        to land: code-server's own settings live in the container's writable
        layer, which is discarded on every force-remove-and-respawn. Picking
        a different theme in the IDE works for that container's lifetime and
        is lost on the next reopen — a known limitation, not a bug.
        """
        with open(self._settings_file(), "w", encoding="utf-8") as fh:
            json.dump({"workbench.colorTheme": DEFAULT_COLOR_THEME}, fh)

    @staticmethod
    def _write_deadline(path: str, expires_at: datetime) -> None:
        """Write the epoch seconds for ``expires_at`` to ``path``."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(int(expires_at.timestamp())))

    @staticmethod
    def _safe_remove_file(path: str) -> bool:
        """Unlink a file, swallowing ``FileNotFoundError``. Returns whether it existed."""
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("ide: failed to remove deadline file {}: {}", path, exc)
            return False


class BrokerContainerBackend:
    """Delegate every privileged operation to the IDE broker over HTTP.

    This process holds no docker socket and no deadline directory. It sends
    coordinates — workspace path, TTL, password — and the broker constructs the
    container spec from its OWN configuration, so a compromised API cannot name
    an image, a mount or a network. The broker also owns the deadline files,
    which is why ``launch`` and ``teardown`` are each a single request rather
    than a file operation paired with a container operation.
    """

    def __init__(self, client: IdeBrokerClient) -> None:
        """Bind to the broker client that carries the base URL and shared secret."""
        self._client = client

    # -- backend protocol --------------------------------------------------

    def launch(self, instance: IdeInstance) -> None:
        """Ask the broker to write the deadline and start the container."""
        self._client.create(
            instance.session_id,
            workspace_path=instance.project_path,
            ttl_seconds=self._ttl_seconds(instance.expires_at),
            password=instance.password,
        )

    def is_running(self, session_id: str) -> bool:
        """Return True only for a ``running`` status; ``absent``/``exited`` are dead."""
        return self._client.status(session_id).status == "running"

    def write_deadline(self, session_id: str, expires_at: datetime) -> None:
        """Extend the deadline broker-side.

        A deadline less than the broker's minimum TTL in the future is refused
        at this boundary rather than sent — pydantic's ``ValidationError`` is a
        ``ValueError``, which the extend route already renders as a 400.
        """
        self._client.extend(session_id, ttl_seconds=self._ttl_seconds(expires_at))

    def teardown(self, session_id: str) -> bool:
        """Ask the broker to remove the container and the deadline, swallowing every error.

        ONE request: the broker's ``DELETE`` already does both, and its
        ``removed`` is true if either half found something.
        """
        try:
            return self._client.delete(session_id).removed
        except Exception as exc:
            logger.warning("ide: broker failed to tear down {}: {}", session_id, exc)
            return False

    # -- coordinate translation --------------------------------------------

    @staticmethod
    def _ttl_seconds(expires_at: datetime) -> int:
        """Convert an absolute deadline into the relative TTL the broker takes."""
        return int((expires_at - datetime.now(UTC)).total_seconds())


# ---------------------------------------------------------------------------
# IdeManager
# ---------------------------------------------------------------------------


class IdeManager:
    """Lifecycle policy for per-session IDE containers.

    Owns the TTL rules, the lifetime ceiling, the extension counter, the Mongo
    writes, drift reconciliation and the readiness probe. Every privileged
    operation goes through the injected ``IdeContainerBackend``.
    """

    #: Probe results are cached this many seconds to cut blocking HTTP calls
    #: during tight polling loops (frontend loader polls at 1 Hz).
    _PROBE_TTL_SECONDS: float = 2.0

    def __init__(
        self,
        config: WebIdeConfig,
        store: IdeStore,
        *,
        docker_client: Any = None,
        backend: IdeContainerBackend | None = None,
    ) -> None:
        """Initialize with feature config, Mongo-backed store, and a container backend.

        Omitting ``backend`` builds a ``DockerContainerBackend`` from
        ``(config, docker_client)``, so a caller that passes only a docker
        client gets the Docker backend without naming it.
        """
        self._cfg = config
        self._store = store
        self._backend: IdeContainerBackend = (
            backend if backend is not None else DockerContainerBackend(config, docker_client)
        )
        self._probe_cache: dict[str, tuple[bool, float]] = {}

    # -- public API --------------------------------------------------------

    def ensure(
        self, session_id: str, project_name: str, project_path: str
    ) -> tuple[IdeInstance, bool]:
        """Create or reconnect to the container for a session.

        Returns ``(instance, created)`` where ``created`` is True if a brand
        new container was spawned (HTTP 201) and False on reconnect (HTTP 200).
        """
        self._validate_session_id(session_id)
        existing = self._store.get(session_id)
        if existing is not None:
            if self._backend.is_running(session_id):
                return replace(existing, status=self._probe_status(session_id)), False
            logger.warning(
                "ide: stale state for session {}; container gone or exited, recreating",
                session_id,
            )
            # Tear down before respawning: the container name is deterministic,
            # so a lingering exited one would 409 the subsequent launch.
            self._forget(session_id)

        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self._cfg.default_lifetime_hours)
        max_deadline = now + timedelta(hours=self._cfg.max_lifetime_hours)
        instance = IdeInstance(
            session_id=session_id,
            status="pending",
            project_name=project_name,
            project_path=project_path,
            password=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=expires_at,
            max_deadline=max_deadline,
            extensions=0,
        )

        # Write ordering: mongo -> launch (deadline + container). Rollback in
        # reverse, so a partial launch leaves no Mongo row, no deadline and no
        # container — whichever half of the launch failed.
        try:
            self._store.insert(instance)
        except DuplicateKeyError:
            # Lost the race with a concurrent spawn; read and return winner.
            winner = self._store.get(session_id)
            if winner is None:
                raise
            if self._backend.is_running(session_id):
                winner = replace(winner, status=self._probe_status(session_id))
            return winner, False

        try:
            self._backend.launch(instance)
        except Exception:
            self._forget(session_id)
            raise

        logger.info(
            "ide: spawned container {} for session {}",
            instance.container_name,
            session_id,
        )
        return replace(instance, status=self._probe_status(session_id)), True

    def get(self, session_id: str) -> IdeInstance | None:
        """Return current instance state, reconciling drift lazily.

        Raises ``DockerUnavailable`` if the daemon is unreachable so callers
        can return 503 without destroying persistent state on a transient
        daemon blip.
        """
        self._validate_session_id(session_id)
        instance = self._store.get(session_id)
        if instance is None:
            return None
        if not self._backend.is_running(session_id):
            logger.warning(
                "ide: container {} not running; cleaning up",
                instance.container_name,
            )
            # Tear down the exited container so a subsequent POST can respawn
            # under the same name without a 409 conflict.
            self._forget(session_id)
            return None
        return replace(instance, status=self._probe_status(session_id))

    def extend(
        self,
        session_id: str,
        *,
        hours: int | None = None,
        expires_at: datetime | None = None,
    ) -> IdeInstance:
        """Push the deadline forward. Exactly one of ``hours``/``expires_at`` required."""
        self._validate_session_id(session_id)
        if (hours is None) == (expires_at is None):
            raise ValueError("exactly one of hours / expires_at must be provided")
        instance = self._store.get(session_id)
        if instance is None:
            raise LookupError(f"no ide instance for session {session_id}")

        now = datetime.now(UTC)
        if hours is not None:
            new_expires_at = now + timedelta(hours=hours)
        else:
            assert expires_at is not None
            new_expires_at = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            new_expires_at = new_expires_at.astimezone(UTC)

        if new_expires_at <= now:
            raise ValueError("expires_at must be in the future")
        if new_expires_at > instance.max_deadline:
            raise MaxLifetimeReached(instance.max_deadline)

        new_extensions = instance.extensions + 1
        # Deadline file first, then Mongo. The file is what the container's
        # watchdog actually reads; if we write Mongo first and the file write
        # fails, the API would report an extended deadline while the container
        # self-terminates at the earlier one. File-then-Mongo reverses that risk:
        # if Mongo fails after a successful file write, the container simply
        # lives longer than Mongo records — strictly safer, and ``get()``
        # reconciles it on the next read.
        self._backend.write_deadline(session_id, new_expires_at)
        self._store.update_expiry(session_id, expires_at=new_expires_at, extensions=new_extensions)

        updated = replace(
            instance,
            expires_at=new_expires_at,
            extensions=new_extensions,
            status=(
                self._probe_status(session_id)
                if self._backend.is_running(session_id)
                else instance.status
            ),
        )
        return updated

    def stop(self, session_id: str) -> bool:
        """Remove the container, deadline file, and mongo doc. Returns whether anything existed."""
        self._validate_session_id(session_id)
        return self._forget(session_id)

    def _forget(self, session_id: str) -> bool:
        """Drop every trace of a session: container, deadline, Mongo doc, probe cache.

        Returns whether anything existed. An explicit ``stop()``, a failed
        launch's rollback and the two lazy drift reconciliations in ``ensure()``
        and ``get()`` all want exactly this set, so there is one implementation
        of it rather than four sequences that can fall out of step.
        """
        removed_backend = self._backend.teardown(session_id)
        removed_doc = self._store.delete(session_id)
        self._probe_cache.pop(session_id, None)
        return removed_backend or removed_doc

    # -- health probe ------------------------------------------------------

    def _probe_status(self, session_id: str) -> IdeStatus:
        """Return ``ready`` if the healthz probe passes, else ``starting``.

        Results are cached for ``_PROBE_TTL_SECONDS`` to avoid issuing a
        blocking HTTP request on every poll from the frontend loader.
        """
        now = time.monotonic()
        cached = self._probe_cache.get(session_id)
        if cached is not None and now - cached[1] < self._PROBE_TTL_SECONDS:
            ready = cached[0]
        else:
            ready = self._probe_ready(self._cfg.proxy_url, session_id)
            self._probe_cache[session_id] = (ready, now)
        return "ready" if ready else "starting"

    @staticmethod
    def _probe_ready(proxy_url: str, session_id: str) -> bool:
        """HTTP GET the ide-proxy healthz path with a 1s timeout.

        ``proxy_url`` is the ide-proxy base (``WebIdeConfig.proxy_url``). It
        is threaded in rather than hardcoded because the reachable address
        depends on the API's network topology: a host-networked API reaches
        the proxy on loopback, while a bridge-networked one must use the
        proxy's in-network DNS name.
        """
        url = proxy_url.rstrip("/") + PROBE_PATH.format(sid=session_id)
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return False

    # -- validation --------------------------------------------------------

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """Raise ``ValueError`` unless ``session_id`` matches the 32-hex regex."""
        if not SESSION_ID_RE.match(session_id):
            raise ValueError(f"invalid session_id: {session_id!r}")


__all__ = [
    "CONTAINER_NAME_PREFIX",
    "DOCKER_SDK_MISSING",
    "BrokerContainerBackend",
    "DockerContainerBackend",
    "DockerUnavailable",
    "IdeContainerBackend",
    "IdeInstance",
    "IdeManager",
    "IdeStore",
    "MaxLifetimeReached",
    "SESSION_ID_RE",
]
