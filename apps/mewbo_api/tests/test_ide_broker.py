"""Contract tests for the IDE broker client and the two container backends.

No network, no docker daemon, no Mongo. ``urllib.request.urlopen`` is replaced
by ``FakeBroker``, an in-memory implementation of the broker's wire contract, so
the client is exercised against the real request/response shapes rather than a
mock that agrees with whatever it is handed.

Also covers ``DockerContainerBackend`` directly: the docker mechanics it now
owns are the container-lifecycle operations, not ``IdeManager``'s bookkeeping.
"""

# mypy: ignore-errors
# ruff: noqa: D101, D102, D103, D107
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

# DockerContainerBackend (mewbo_api.ide) is dev/CI-only — the deployed api
# never imports it (see apps/mewbo_api/CLAUDE.md, "Web IDE"). A lean install
# without the `docker` extra must still collect the rest of the suite.
pytest.importorskip("docker")
from docker.errors import APIError, DockerException, NotFound
from mewbo_api.ide import (
    CONTAINER_NAME_PREFIX,
    BrokerContainerBackend,
    DockerContainerBackend,
    DockerUnavailable,
    IdeInstance,
    IdeManager,
)
from mewbo_api.ide_broker import (
    BROKER_TOKEN_HEADER,
    CreateRequest,
    ExtendRequest,
    IdeBrokerClient,
    WorkspaceDenied,
)
from mewbo_core.config import WebIdeConfig
from pydantic import ValidationError

UTC = timezone.utc
VALID_SID = "a" * 32
BASE_URL = "http://127.0.0.1:5128"
TOKEN = "broker-secret-token"


# ---------------------------------------------------------------------------
# HTTP stub
# ---------------------------------------------------------------------------


class RecordedCall:
    """One outbound request, captured as the client actually built it."""

    def __init__(self, request: Any) -> None:
        self.method = request.get_method()
        self.url = request.full_url
        # urllib capitalizes header names on the way in; normalize so an
        # assertion can name the header the way the contract spells it.
        self.headers = {k.lower(): v for k, v in request.header_items()}
        self.body = json.loads(request.data) if request.data else None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _http_error(status: int, code: str, reason: str, *, retryable: bool = False) -> Any:
    body = json.dumps({"error": {"code": code, "reason": reason, "retryable": retryable}})
    return urllib.error.HTTPError(
        url=BASE_URL, code=status, msg=reason, hdrs=None, fp=_ByteFp(body.encode())
    )


class _ByteFp:
    """Minimal file-like for ``HTTPError``, which reads its body once."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self, *_a: Any) -> bytes:
        raw, self._raw = self._raw, b""
        return raw

    def close(self) -> None:
        return None


class FakeBroker:
    """In-memory implementation of the broker's four ``/v1`` routes.

    Stands in for ``urllib.request.urlopen``. Holds the same state the real
    broker does — a container status and a deadline per session — so a full
    manager lifecycle exercises the same transitions.
    """

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self.containers: dict[str, str] = {}
        self.deadlines: dict[str, datetime] = {}
        #: Queued exceptions/responses that pre-empt the routing table.
        self.script: list[Any] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        call = RecordedCall(request)
        self.calls.append(call)
        if self.script:
            nxt = self.script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return _FakeResponse(nxt)
        return _FakeResponse(self._route(call))

    def _route(self, call: RecordedCall) -> dict[str, Any]:
        path = call.url[len(BASE_URL) :]
        if path.endswith("/extend"):
            sid = path[len("/v1/ide/") : -len("/extend")]
            self._set_deadline(sid, call.body["ttl_seconds"])
            return {}
        sid = path[len("/v1/ide/") :]
        if call.method == "POST":
            self.containers[sid] = "running"
            self._set_deadline(sid, call.body["ttl_seconds"])
            return {}
        if call.method == "GET":
            return {"status": self.containers.get(sid, "absent")}
        removed = self.containers.pop(sid, None) is not None
        removed = self.deadlines.pop(sid, None) is not None or removed
        return {"removed": removed}

    def _set_deadline(self, sid: str, ttl_seconds: int) -> None:
        self.deadlines[sid] = datetime.now(UTC) + timedelta(seconds=ttl_seconds)


class InMemoryStore:
    """Mimics ``IdeStore`` using a dict (same shape the other IDE suites use)."""

    def __init__(self) -> None:
        self._docs: dict[str, IdeInstance] = {}

    def get(self, session_id: str) -> IdeInstance | None:
        src = self._docs.get(session_id)
        return None if src is None else replace(src, status="pending")

    def insert(self, instance: IdeInstance) -> None:
        if instance.session_id in self._docs:
            from pymongo.errors import DuplicateKeyError

            raise DuplicateKeyError("dup")
        self._docs[instance.session_id] = instance

    def update_expiry(self, session_id: str, *, expires_at: datetime, extensions: int) -> None:
        self._docs[session_id] = replace(
            self._docs[session_id], expires_at=expires_at, extensions=extensions
        )

    def delete(self, session_id: str) -> bool:
        return self._docs.pop(session_id, None) is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def project_path() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def cfg(state_dir: str) -> WebIdeConfig:
    return WebIdeConfig(
        enabled=True,
        image="codercom/code-server:latest",
        default_lifetime_hours=1,
        max_lifetime_hours=8,
        pids_limit=512,
        network="mewbo-ide",
        state_dir=state_dir,
    )


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def client(broker: FakeBroker) -> IdeBrokerClient:
    with patch("urllib.request.urlopen", broker):
        yield IdeBrokerClient(BASE_URL, TOKEN)


# ---------------------------------------------------------------------------
# The four routes: header + exact body
# ---------------------------------------------------------------------------


def test_create_sends_token_and_exact_body(client: IdeBrokerClient, broker: FakeBroker) -> None:
    client.create(
        VALID_SID,
        workspace_path="/srv/projects/demo",
        ttl_seconds=3600,
        password="abcdefghijklmnop",
    )

    call = broker.calls[0]
    assert call.method == "POST"
    assert call.url == f"{BASE_URL}/v1/ide/{VALID_SID}"
    assert call.header(BROKER_TOKEN_HEADER) == TOKEN
    assert call.header("Content-Type") == "application/json"
    # Coordinates only — no image, no mounts, no network, no container name.
    assert call.body == {
        "workspace_path": "/srv/projects/demo",
        "ttl_seconds": 3600,
        "password": "abcdefghijklmnop",
    }


def test_status_sends_token_and_no_body(client: IdeBrokerClient, broker: FakeBroker) -> None:
    result = client.status(VALID_SID)

    call = broker.calls[0]
    assert call.method == "GET"
    assert call.url == f"{BASE_URL}/v1/ide/{VALID_SID}"
    assert call.header(BROKER_TOKEN_HEADER) == TOKEN
    assert call.body is None
    assert result.status == "absent"


def test_extend_sends_token_and_exact_body(client: IdeBrokerClient, broker: FakeBroker) -> None:
    client.extend(VALID_SID, ttl_seconds=7200)

    call = broker.calls[0]
    assert call.method == "POST"
    assert call.url == f"{BASE_URL}/v1/ide/{VALID_SID}/extend"
    assert call.header(BROKER_TOKEN_HEADER) == TOKEN
    assert call.body == {"ttl_seconds": 7200}


def test_delete_sends_token(client: IdeBrokerClient, broker: FakeBroker) -> None:
    result = client.delete(VALID_SID)

    call = broker.calls[0]
    assert call.method == "DELETE"
    assert call.url == f"{BASE_URL}/v1/ide/{VALID_SID}"
    assert call.header(BROKER_TOKEN_HEADER) == TOKEN
    assert result.removed is False


def test_base_url_trailing_slash_does_not_double(broker: FakeBroker) -> None:
    with patch("urllib.request.urlopen", broker):
        IdeBrokerClient(f"{BASE_URL}/", TOKEN).status(VALID_SID)
    assert broker.calls[0].url == f"{BASE_URL}/v1/ide/{VALID_SID}"


# ---------------------------------------------------------------------------
# Request bodies are refused at definition, before any HTTP happens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workspace_path": "relative/path", "ttl_seconds": 3600, "password": "a" * 16},
        {"workspace_path": "/srv/demo", "ttl_seconds": 59, "password": "a" * 16},
        {"workspace_path": "/srv/demo", "ttl_seconds": 604801, "password": "a" * 16},
        {"workspace_path": "/srv/demo", "ttl_seconds": 3600, "password": "short"},
        {"workspace_path": "/srv/demo", "ttl_seconds": 3600, "password": "has spaces here!!"},
    ],
)
def test_create_request_refuses_bad_coordinates(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CreateRequest(**kwargs)


def test_request_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ExtendRequest(ttl_seconds=3600, image="attacker/image")


def test_bad_coordinates_never_reach_the_wire(client: IdeBrokerClient, broker: FakeBroker) -> None:
    with pytest.raises(ValidationError):
        client.create(VALID_SID, workspace_path="rel", ttl_seconds=3600, password="a" * 16)
    assert broker.calls == []


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        TimeoutError("slow"),
        ConnectionResetError("reset"),
    ],
)
def test_transport_failure_surfaces_as_docker_unavailable(
    client: IdeBrokerClient, broker: FakeBroker, failure: Exception
) -> None:
    broker.script = [failure]
    with pytest.raises(DockerUnavailable):
        client.status(VALID_SID)


def test_workspace_denied_is_distinguishable(client: IdeBrokerClient, broker: FakeBroker) -> None:
    """A 403 workspace_denied must not be swallowed into the generic 503 path."""
    broker.script = [_http_error(403, "workspace_denied", "/etc is outside the allowed roots")]
    with pytest.raises(WorkspaceDenied) as excinfo:
        client.create(
            VALID_SID, workspace_path="/etc", ttl_seconds=3600, password="a" * 16
        )
    assert "outside the allowed roots" in str(excinfo.value)
    # It is a ValueError, so the extend route's existing arm renders it as a 400.
    assert isinstance(excinfo.value, ValueError)
    assert not isinstance(excinfo.value, DockerUnavailable)


def test_other_403_is_not_mistaken_for_workspace_denied(
    client: IdeBrokerClient, broker: FakeBroker
) -> None:
    broker.script = [_http_error(403, "forbidden", "nope")]
    with pytest.raises(DockerUnavailable):
        client.status(VALID_SID)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "unauthorized"),
        (400, "bad_request"),
        (502, "docker_error"),
        (503, "docker_unavailable"),
    ],
)
def test_other_broker_errors_map_to_docker_unavailable(
    client: IdeBrokerClient, broker: FakeBroker, status: int, code: str
) -> None:
    broker.script = [_http_error(status, code, "broker said no")]
    with pytest.raises(DockerUnavailable) as excinfo:
        client.status(VALID_SID)
    # The code and reason survive into the message, so the log is diagnosable.
    assert code in str(excinfo.value)
    assert "broker said no" in str(excinfo.value)


def test_error_without_an_envelope_still_raises_diagnosably(
    client: IdeBrokerClient, broker: FakeBroker
) -> None:
    """A proxy in front of the broker can return HTML; that must not crash the mapping."""
    broker.script = [
        urllib.error.HTTPError(
            url=BASE_URL, code=502, msg="Bad Gateway", hdrs=None, fp=_ByteFp(b"<html>nope</html>")
        )
    ]
    with pytest.raises(DockerUnavailable):
        client.status(VALID_SID)


# ---------------------------------------------------------------------------
# Responses are refused, never coerced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "confused"},  # not in the closed set
        {},  # missing the one field the caller reads
        {"status": "running", "surprise": 1},  # extra="forbid"
        # Fields the wire deliberately no longer carries: the caller derives the
        # container name and the URL, and owns expires_at authoritatively. A
        # broker that starts sending them again is a divergence, not a courtesy.
        {"status": "running", "container": "mewbo-ide-x"},
        {"status": "running", "url": f"/ide/{VALID_SID}/"},
        {"status": "running", "expires_at": "2026-01-01T00:00:00+00:00"},
    ],
)
def test_malformed_status_response_is_refused(
    client: IdeBrokerClient, broker: FakeBroker, payload: dict[str, Any]
) -> None:
    broker.script = [payload]
    with pytest.raises(DockerUnavailable) as excinfo:
        client.status(VALID_SID)
    assert "unusable" in str(excinfo.value)


@pytest.mark.parametrize("payload", [{"container": "mewbo-ide-x"}, {"expires_at": "2026-01-01"}])
def test_command_responses_carry_no_body(
    client: IdeBrokerClient, broker: FakeBroker, payload: dict[str, Any]
) -> None:
    """Create and extend answer with ``{}``; anything else is refused, not ignored."""
    broker.script = [payload]
    with pytest.raises(DockerUnavailable):
        client.extend(VALID_SID, ttl_seconds=3600)


def test_non_json_response_is_refused(client: IdeBrokerClient, broker: FakeBroker) -> None:
    class _Garbage:
        def read(self) -> bytes:
            return b"not json at all"

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    with patch("urllib.request.urlopen", return_value=_Garbage()):
        with pytest.raises(DockerUnavailable):
            client.delete(VALID_SID)


# ---------------------------------------------------------------------------
# BrokerContainerBackend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,running",
    [("running", True), ("absent", False), ("exited", False)],
)
def test_is_running_only_for_running(
    client: IdeBrokerClient, broker: FakeBroker, status: str, running: bool
) -> None:
    backend = BrokerContainerBackend(client)
    broker.script = [{"status": status}]
    assert backend.is_running(VALID_SID) is running


def test_the_backend_addresses_the_session_id(
    client: IdeBrokerClient, broker: FakeBroker
) -> None:
    """The protocol is session-keyed end to end; the broker derives the container name."""
    BrokerContainerBackend(client).is_running(VALID_SID)
    assert broker.calls[0].url == f"{BASE_URL}/v1/ide/{VALID_SID}"


def test_teardown_is_one_request_and_never_raises(
    client: IdeBrokerClient, broker: FakeBroker
) -> None:
    """One DELETE covers the container AND the deadline, and a dead broker is not fatal."""
    broker.script = [urllib.error.URLError("down")]
    assert BrokerContainerBackend(client).teardown(VALID_SID) is False
    assert len(broker.calls) == 1


def test_is_running_propagates_unavailability(client: IdeBrokerClient, broker: FakeBroker) -> None:
    """Unlike teardown(), a failed status check must NOT read as 'not running'.

    Swallowing it would let a transient broker outage destroy live Mongo state
    through the drift-reconciliation path in ``get()``.
    """
    broker.script = [urllib.error.URLError("down")]
    with pytest.raises(DockerUnavailable):
        BrokerContainerBackend(client).is_running(VALID_SID)


def test_full_lifecycle_over_the_broker_never_touches_the_docker_sdk(
    cfg: WebIdeConfig, broker: FakeBroker, project_path: str
) -> None:
    """ensure -> get -> extend -> stop, driven entirely through the broker."""
    store = InMemoryStore()

    def _explode(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the broker path must never reach the docker SDK")

    with patch("urllib.request.urlopen", broker), patch(
        "mewbo_api.ide.docker_from_env", _explode
    ), patch.object(IdeManager, "_probe_ready", return_value=True):
        manager = IdeManager(
            cfg, store, backend=BrokerContainerBackend(IdeBrokerClient(BASE_URL, TOKEN))
        )

        instance, created = manager.ensure(VALID_SID, "demo", project_path)
        assert created is True
        assert instance.status == "ready"
        assert broker.containers[VALID_SID] == "running"

        fetched = manager.get(VALID_SID)
        assert fetched is not None
        assert fetched.session_id == VALID_SID

        extended = manager.extend(VALID_SID, hours=2)
        assert extended.extensions == 1
        assert extended.expires_at > instance.expires_at

        # One teardown is ONE round trip. The broker's DELETE already removes
        # the container and unlinks the deadline, so a second call could only
        # ever report that it found nothing left — a wasted trip and a
        # misleading log line.
        before = len(broker.calls)
        assert manager.stop(VALID_SID) is True
        assert [c.method for c in broker.calls[before:]] == ["DELETE"]

    assert store.get(VALID_SID) is None
    assert broker.containers == {}
    assert broker.deadlines == {}
    # The API process wrote no deadline file of its own — the broker owns them.
    assert os.listdir(cfg.state_dir) == []


def test_ensure_rolls_back_when_the_broker_refuses_the_workspace(
    cfg: WebIdeConfig, broker: FakeBroker, project_path: str
) -> None:
    """A refused launch must leave no Mongo row — the same end state as a docker failure."""
    store = InMemoryStore()
    broker.script = [_http_error(403, "workspace_denied", "outside the allowed roots")]

    with patch("urllib.request.urlopen", broker):
        manager = IdeManager(
            cfg, store, backend=BrokerContainerBackend(IdeBrokerClient(BASE_URL, TOKEN))
        )
        with pytest.raises(WorkspaceDenied):
            manager.ensure(VALID_SID, "demo", project_path)

    assert store.get(VALID_SID) is None


def test_extend_below_the_broker_minimum_is_refused_locally(
    cfg: WebIdeConfig, broker: FakeBroker, project_path: str
) -> None:
    """A sub-minimum TTL is a ValueError, which the extend route renders as a 400."""
    store = InMemoryStore()
    with patch("urllib.request.urlopen", broker), patch.object(
        IdeManager, "_probe_ready", return_value=False
    ):
        manager = IdeManager(
            cfg, store, backend=BrokerContainerBackend(IdeBrokerClient(BASE_URL, TOKEN))
        )
        manager.ensure(VALID_SID, "demo", project_path)
        with pytest.raises(ValueError):
            manager.extend(VALID_SID, expires_at=datetime.now(UTC) + timedelta(seconds=5))


# ---------------------------------------------------------------------------
# DockerContainerBackend — the docker mechanics, at their new address
# ---------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, name: str, store: FakeContainers, status: str = "running") -> None:
        self.name = name
        self._store = store
        self.removed = False
        self.status = status

    def remove(self, force: bool = False) -> None:
        self._store._by_name.pop(self.name, None)
        self.removed = True


class FakeContainers:
    def __init__(self) -> None:
        self._by_name: dict[str, FakeContainer] = {}
        self.run_calls: list[dict[str, Any]] = []
        self.get_should_raise: Exception | None = None

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(kwargs)
        container = FakeContainer(kwargs["name"], self)
        self._by_name[kwargs["name"]] = container
        return container

    def get(self, name: str) -> FakeContainer:
        if self.get_should_raise is not None:
            raise self.get_should_raise
        if name not in self._by_name:
            raise NotFound(f"container {name} not found")
        return self._by_name[name]


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()


@pytest.fixture
def docker_backend(cfg: WebIdeConfig) -> DockerContainerBackend:
    return DockerContainerBackend(cfg, FakeDockerClient())


def test_docker_backend_wraps_connection_failure(cfg: WebIdeConfig) -> None:
    backend = DockerContainerBackend(cfg, docker_client=None)
    with patch("mewbo_api.ide.docker_from_env", side_effect=DockerException("no sock")):
        with pytest.raises(DockerUnavailable):
            backend._docker()


def test_docker_backend_refuses_to_construct_without_the_sdk(cfg: WebIdeConfig) -> None:
    """With the optional extra absent the module still imports; this is where it fails."""
    with patch("mewbo_api.ide.docker_from_env", None):
        with pytest.raises(DockerUnavailable) as excinfo:
            DockerContainerBackend(cfg)
    assert "docker" in str(excinfo.value)


def test_ide_module_imports_without_the_docker_sdk() -> None:
    """An ImportError at module scope would take every other API route down with it."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name == 'docker' or name.startswith('docker.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        return self.find_module(name, path)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import mewbo_api.ide as ide\n"
        "assert ide.docker_from_env is None\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_docker_backend_teardown_swallows_api_error_on_get(
    docker_backend: DockerContainerBackend,
) -> None:
    docker_backend._client.containers.get_should_raise = APIError("daemon glitch")
    assert docker_backend.teardown(VALID_SID) is False


def test_docker_backend_teardown_swallows_api_error_on_remove(
    docker_backend: DockerContainerBackend,
) -> None:
    class ErrorContainer:
        name = f"{CONTAINER_NAME_PREFIX}{VALID_SID}"
        status = "running"

        def remove(self, force: bool = False) -> None:
            raise APIError("cannot remove")

    docker_backend._client.containers._by_name[ErrorContainer.name] = ErrorContainer()
    assert docker_backend.teardown(VALID_SID) is False


def test_docker_backend_teardown_swallows_not_found_on_remove(
    docker_backend: DockerContainerBackend,
) -> None:
    class VanishingContainer:
        name = f"{CONTAINER_NAME_PREFIX}{VALID_SID}"
        status = "running"

        def remove(self, force: bool = False) -> None:
            raise NotFound("already gone")

    docker_backend._client.containers._by_name[VanishingContainer.name] = VanishingContainer()
    assert docker_backend.teardown(VALID_SID) is False


def test_docker_backend_teardown_returns_false_when_daemon_unreachable(cfg: WebIdeConfig) -> None:
    backend = DockerContainerBackend(cfg, docker_client=None)
    with patch("mewbo_api.ide.docker_from_env", side_effect=DockerException("no sock")):
        assert backend.teardown(VALID_SID) is False


def test_docker_backend_is_running_swallows_api_error(
    docker_backend: DockerContainerBackend,
) -> None:
    docker_backend._client.containers.get_should_raise = APIError("oops")
    assert docker_backend.is_running(VALID_SID) is False


def test_docker_backend_teardown_reports_a_deadline_with_no_container(
    docker_backend: DockerContainerBackend,
) -> None:
    """The local backend still owns two subsystems; either half is enough to report True."""
    assert docker_backend.teardown(VALID_SID) is False
    docker_backend.write_deadline(VALID_SID, datetime.now(UTC) + timedelta(hours=1))
    assert docker_backend.teardown(VALID_SID) is True


def test_docker_backend_safe_remove_file_missing_returns_false() -> None:
    assert DockerContainerBackend._safe_remove_file("/nonexistent/path/file.txt") is False


def test_docker_backend_safe_remove_file_existing_returns_true() -> None:
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        path = fh.name
    assert DockerContainerBackend._safe_remove_file(path) is True
    assert not os.path.exists(path)


def test_docker_backend_launch_writes_the_deadline_then_runs(
    docker_backend: DockerContainerBackend, project_path: str
) -> None:
    now = datetime.now(UTC)
    instance = IdeInstance(
        session_id=VALID_SID,
        status="pending",
        project_name="demo",
        project_path=project_path,
        password="pw",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        max_deadline=now + timedelta(hours=8),
        extensions=0,
    )
    docker_backend.launch(instance)

    deadline_file = os.path.join(docker_backend._cfg.state_dir, f"{VALID_SID}.deadline")
    with open(deadline_file) as fh:
        assert int(fh.read()) == int(instance.expires_at.timestamp())
    call = docker_backend._client.containers.run_calls[0]
    assert call["name"] == f"{CONTAINER_NAME_PREFIX}{VALID_SID}"
    assert call["volumes"][deadline_file] == {"bind": "/mewbo/deadline", "mode": "ro"}


def test_ensure_rolls_back_mongo_when_the_deadline_write_fails(
    cfg: WebIdeConfig, project_path: str
) -> None:
    """The write now happens inside launch(); the rollback contract is unchanged."""
    store = InMemoryStore()
    backend = DockerContainerBackend(cfg, FakeDockerClient())
    manager = IdeManager(cfg, store, backend=backend)

    with patch.object(DockerContainerBackend, "_write_deadline", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            manager.ensure(VALID_SID, "demo", project_path)

    assert store.get(VALID_SID) is None
    assert backend._client.containers.run_calls == []


def test_manager_defaults_to_the_docker_backend(cfg: WebIdeConfig) -> None:
    """Omitting ``backend`` builds the Docker backend from (config, docker_client)."""
    client = FakeDockerClient()
    manager = IdeManager(cfg, InMemoryStore(), docker_client=client)
    assert isinstance(manager._backend, DockerContainerBackend)
    assert manager._backend._client is client
