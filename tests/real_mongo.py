"""The containerized real-MongoDB test tier (tier 2).

Tier 1 — the default ``pytest`` run — is in-process and mongomock-backed. It
stays the common loop and needs no Docker. Tier 2 is small and marked
``realmongo``: it exists for the properties tier 1 structurally cannot observe.
mongomock has no query planner, so an N+1 write, a missing index and a table
scan all cost nothing there; and it accepts ``event_listeners=`` while
implementing none of PyMongo's monitoring API, so no command can be COUNTED.

The substrate is ``tests/docker-compose.test.yml`` (``make test-mongo``).
Absence of the container is a SKIP, never an error: a developer running plain
``pytest`` must not need Docker, and a tier that errors when it is missing
would make the default run red for everyone.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from pymongo import MongoClient, monitoring

#: What a client built here hands back from a collection — pymongo's own
#: default document class. Named once because it appears in every signature
#: below; a bare ``MongoClient`` is unparameterised and infers ``Any`` again,
#: which is the thing this file exists not to do.
TierClient = MongoClient[dict[str, Any]]


class CommandCounter(monitoring.CommandListener):
    """Counts the commands a PyMongo client actually issues, by command name.

    This is the instrument tier 2 exists for. Register it with
    ``MongoClient(..., event_listeners=[counter])`` and the round-trip count of
    a store method becomes an assertable number — which is what
    ``tests/CLAUDE.md`` means by "assert READ COUNTS, never wall-clock": a
    timing assertion is flaky on a loaded machine and passes for the wrong
    reason on a fast one, while a command count is exact.

    State and behaviour together; nothing is injected because a listener owns
    only its own tally.
    """

    #: Command names that carry a write. A per-document loop issues one of
    #: these per document; a batched write issues one per BATCH, which is the
    #: whole difference an N+1 defect hides in.
    WRITE_COMMANDS = frozenset({"insert", "update", "delete", "findAndModify"})

    def __init__(self) -> None:
        """Start with an empty tally."""
        self.counts: Counter[str] = Counter()

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        """Record one issued command."""
        self.counts[event.command_name] += 1

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        """Ignored — a command is counted when it is ISSUED, not when it lands."""

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        """Ignored — a failed round trip still cost a round trip."""

    @property
    def writes(self) -> int:
        """How many write commands were issued."""
        return sum(n for name, n in self.counts.items() if name in self.WRITE_COMMANDS)

    @property
    def total(self) -> int:
        """How many commands of any kind were issued."""
        return sum(self.counts.values())

    def reset(self) -> None:
        """Clear the tally, so one test can measure several call sites."""
        self.counts.clear()


class RealMongoTier:
    """Connection facts for the tier-2 MongoDB, and the rule for skipping it.

    Collaborators are injected as fields: the URI and the client factory both
    arrive as arguments, so the skip rule can be exercised without a container
    and a caller can point the tier at a Mongo it started itself.
    """

    #: Overrides the URI. ``make test-mongo`` publishes on the compose file's
    #: own port; a developer with Mongo somewhere else sets this instead.
    URI_ENV = "MEWBO_TEST_MONGODB_URI"
    #: Matches tests/docker-compose.test.yml's loopback publish.
    DEFAULT_URI = "mongodb://127.0.0.1:27019"
    #: One database for the whole tier. The container is ephemeral (no volume),
    #: so nothing survives a `down`; individual tests still scope their own
    #: writes by slug/id so they do not read each other's rows.
    DATABASE = "mewbo_test_tier"
    #: Short on purpose — the answer when the container is absent is "skip",
    #: and a developer without Docker should not wait 30s per test for it.
    SELECTION_TIMEOUT_MS = 1500

    def __init__(
        self,
        uri: str | None = None,
        *,
        client_factory: Callable[..., TierClient] | None = None,
    ) -> None:
        """Bind the tier to *uri*, defaulting to the env var then the compose port."""
        self.uri = uri or os.environ.get(self.URI_ENV) or self.DEFAULT_URI
        self.client_factory: Callable[..., TierClient] = client_factory or MongoClient

    def connect(
        self, *, event_listeners: Sequence[monitoring.CommandListener] | None = None
    ) -> TierClient:
        """Open a client and prove the server answers, or raise.

        The ``ping`` is what makes absence detectable at all: PyMongo connects
        lazily, so a client built against a dead address looks healthy until the
        first operation — which would surface as a 30s stall inside whichever
        assertion happened to run first.
        """
        client = self.client_factory(
            self.uri,
            serverSelectionTimeoutMS=self.SELECTION_TIMEOUT_MS,
            event_listeners=list(event_listeners or []),
        )
        client.admin.command("ping")
        return client

    def client_or_skip(
        self, *, event_listeners: Sequence[monitoring.CommandListener] | None = None
    ) -> TierClient:
        """``connect()``, turning an unreachable server into a pytest skip."""
        import pytest

        try:
            return self.connect(event_listeners=event_listeners)
        except Exception as exc:  # noqa: BLE001 — every failure mode means "absent"
            pytest.skip(
                f"tier-2 MongoDB not reachable at {self.uri} ({type(exc).__name__}); "
                "start it with `make test-mongo`"
            )
