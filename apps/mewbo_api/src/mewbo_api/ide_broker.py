"""HTTP client for the privileged IDE broker.

The broker is the sole holder of the docker socket. This process sends it only
COORDINATES — a workspace path, a TTL and a password — and the broker builds the
container spec from its own configuration: image, mounts, network, limits,
entrypoint, labels and the container name are never accepted off the wire, and
the workspace path is realpath-resolved against the broker's own allowlist.

Both directions cross a trust boundary, so both are pydantic models with
``extra="forbid"``: an unknown field in a request body is refused by the broker
rather than silently dropped, and an unexpected response shape is refused here
rather than coerced into something the caller would act on.

Failure mapping is chosen so the existing routes need no change:

- unreachable, refused or timed out  -> ``DockerUnavailable`` (routes: 503)
- any other non-2xx                  -> ``DockerUnavailable`` (routes: 503)
- ``403 workspace_denied``           -> ``WorkspaceDenied`` (a ``ValueError``)

``workspace_denied`` is the ONE broker code this client branches on, and it is
deliberately NOT folded into the 503: it means the broker's allowlist does not
cover the workspace, which is an operator misconfiguration a 503 would hide
behind "daemon unreachable". It is logged at error level with the broker's own
sentence, and the extend route — which already renders ``ValueError`` as a 400 —
surfaces that sentence to the caller.

Every OTHER code collapses onto ``DockerUnavailable``, so the broker's own
``docker_error`` (502) vs ``docker_unavailable`` (503) split is DIAGNOSTIC here,
not behavioural: the code and reason are carried into the exception message so a
log names what happened, and nothing downstream reads them apart.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Literal, NoReturn, TypeVar

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_api.ide import DockerUnavailable

#: Header carrying the shared secret. Distinct from the API's own master token:
#: the broker is a separate trust domain and must not accept an API credential.
BROKER_TOKEN_HEADER = "X-Broker-Token"

#: Bounds the broker enforces on ``ttl_seconds``. Mirrored here so a request it
#: would refuse is refused locally, with a message naming the field.
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 604800

#: The broker mints nothing: this is the shape it accepts for a password the API
#: has already persisted and handed to the browser.
PASSWORD_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"

_ResponseT = TypeVar("_ResponseT", bound=BaseModel)


class WorkspaceDenied(ValueError):
    """The broker refused the workspace path (outside its allowlist, or not a directory).

    A ``ValueError`` so the extend route's existing ``except ValueError`` arm
    renders the broker's reason as a 400 rather than an opaque 503.
    """


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class BrokerRequest(BaseModel):
    """Base for every request body the broker parses with a strict schema."""

    model_config = ConfigDict(extra="forbid")


class BrokerResponse(BaseModel):
    """Base for every response body, refused rather than coerced when unexpected."""

    model_config = ConfigDict(extra="forbid")


class CreateRequest(BrokerRequest):
    """Body of ``POST /v1/ide/<session_id>``."""

    workspace_path: str = Field(min_length=1)
    ttl_seconds: int = Field(ge=MIN_TTL_SECONDS, le=MAX_TTL_SECONDS)
    password: str = Field(pattern=PASSWORD_PATTERN)

    @field_validator("workspace_path")
    @classmethod
    def _must_be_absolute(cls, value: str) -> str:
        """Refuse a relative path here: it cannot mean the same thing in two namespaces."""
        if not os.path.isabs(value):
            raise ValueError("workspace_path must be an absolute path")
        return value


class ExtendRequest(BrokerRequest):
    """Body of ``POST /v1/ide/<session_id>/extend``."""

    ttl_seconds: int = Field(ge=MIN_TTL_SECONDS, le=MAX_TTL_SECONDS)


class CommandResponse(BrokerResponse):
    """Body of a route whose whole answer is its status code.

    Deliberately empty. Everything the broker could echo back about a container
    it just created or extended is a fact this side already holds: the container
    name and the browser URL are DERIVED from the session id
    (``IdeInstance.name_for`` / ``.url``), and ``expires_at`` is owned
    authoritatively in Mongo. Sending them would be a second channel for a fact
    that already has one — and a second channel is what eventually disagrees.

    ``extra="forbid"`` is inherited, so this is not a shrug at the response
    shape: an unexpected field is still refused rather than ignored.
    """


class StatusResponse(BrokerResponse):
    """Body of the status probe. ``absent`` is a status, never a 404."""

    status: Literal["absent", "running", "exited"]


class DeleteResponse(BrokerResponse):
    """Body of a delete. ``removed`` covers the container OR the deadline file."""

    removed: bool


class BrokerErrorBody(BrokerResponse):
    """The broker's error object.

    ``retryable`` is declared but never read: ``_decode_error`` returns only
    ``(code, reason)``. It is RESERVED rather than dead — declaring it is what
    lets the envelope parse at all. ``extra="forbid"`` is inherited, and the
    broker puts ``retryable`` on every error body, so dropping the field would
    make ``model_validate`` reject a well-formed envelope; ``_decode_error``
    would then fall to its unstructured arm and lose the ``code`` that the
    ``workspace_denied`` branch depends on. Read it if a caller ever needs to
    decide whether to retry; do not remove it to tidy up.
    """

    code: str
    reason: str
    retryable: bool


class BrokerErrorEnvelope(BrokerResponse):
    """The envelope wrapping every non-2xx body."""

    error: BrokerErrorBody


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class IdeBrokerClient:
    """One method per broker route, over ``urllib`` — this app's existing HTTP idiom.

    Cost class: ``O(1)`` per call. The broker touches one container and one
    deadline file per request and never enumerates anything, so no method here
    scales with the number of live IDE instances.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        """Bind to a broker base URL and its shared secret."""
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- routes ------------------------------------------------------------

    def create(
        self,
        session_id: str,
        *,
        workspace_path: str,
        ttl_seconds: int,
        password: str,
    ) -> CommandResponse:
        """Create or replace the session's container, writing its deadline first."""
        body = CreateRequest(
            workspace_path=workspace_path,
            ttl_seconds=ttl_seconds,
            password=password,
        )
        return self._call("POST", f"/v1/ide/{session_id}", CommandResponse, body=body)

    def status(self, session_id: str) -> StatusResponse:
        """Return the container's status. Never 404s — absence is a status."""
        return self._call("GET", f"/v1/ide/{session_id}", StatusResponse)

    def extend(self, session_id: str, *, ttl_seconds: int) -> CommandResponse:
        """Rewrite the deadline file. Does not require the container to exist."""
        body = ExtendRequest(ttl_seconds=ttl_seconds)
        return self._call(
            "POST", f"/v1/ide/{session_id}/extend", CommandResponse, body=body
        )

    def delete(self, session_id: str) -> DeleteResponse:
        """Force-remove the container and unlink the deadline file."""
        return self._call("DELETE", f"/v1/ide/{session_id}", DeleteResponse)

    # -- transport ---------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        response_model: type[_ResponseT],
        *,
        body: BrokerRequest | None = None,
    ) -> _ResponseT:
        """Issue one request and parse the response, or raise a mapped failure."""
        raw = self._send(method, path, body)
        try:
            return response_model.model_validate_json(raw)
        except ValidationError as exc:
            # A response we cannot understand is a broker malfunction, not a
            # caller error — refuse it rather than acting on a partial parse.
            raise DockerUnavailable(
                f"ide broker returned an unusable {response_model.__name__}: {exc}"
            ) from exc

    def _send(self, method: str, path: str, body: BrokerRequest | None) -> bytes:
        """Perform the HTTP call, translating transport failures to ``DockerUnavailable``."""
        headers = {BROKER_TOKEN_HEADER: self.token, "Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = body.model_dump_json().encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            # Must precede URLError/OSError: HTTPError subclasses both.
            self._raise_for_status(exc)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise DockerUnavailable(f"ide broker unreachable at {self.base_url}: {exc}") from exc

    def _raise_for_status(self, exc: urllib.error.HTTPError) -> NoReturn:
        """Map a non-2xx response onto the failure the routes already handle."""
        code, reason = self._decode_error(exc)
        if exc.code == 403 and code == "workspace_denied":
            logger.error(
                "ide: broker refused the workspace — check its allowed roots: {}", reason
            )
            raise WorkspaceDenied(reason)
        if exc.code == 401:
            logger.error("ide: broker rejected the shared secret ({}): {}", code, reason)
        else:
            logger.warning("ide: broker returned {} {}: {}", exc.code, code, reason)
        raise DockerUnavailable(f"ide broker {exc.code} {code or 'error'}: {reason}")

    @staticmethod
    def _decode_error(exc: urllib.error.HTTPError) -> tuple[str, str]:
        """Return ``(code, reason)`` from the error envelope, degrading to the raw body.

        A broker that failed before it could render an envelope (a proxy in
        front of it, say) must still produce a diagnosable log line rather than
        a bare status number.
        """
        try:
            payload: Any = json.loads(exc.read() or b"")
        except (ValueError, OSError):
            return "", str(exc.reason)
        try:
            envelope = BrokerErrorEnvelope.model_validate(payload)
        except ValidationError:
            return "", str(payload)[:200]
        return envelope.error.code, envelope.error.reason


__all__ = [
    "BROKER_TOKEN_HEADER",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "BrokerErrorBody",
    "BrokerErrorEnvelope",
    "CommandResponse",
    "CreateRequest",
    "DeleteResponse",
    "ExtendRequest",
    "IdeBrokerClient",
    "StatusResponse",
    "WorkspaceDenied",
]
