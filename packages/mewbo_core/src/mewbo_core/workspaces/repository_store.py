#!/usr/bin/env python3
"""Repository registry storage — a ``RepositoryStoreBase`` ABC + two drivers.

Mirrors ``key_store.py``/``triggers/store.py``: the whole PUBLIC surface
(``get``/``list``/``register``/``patch``/``delete``) is composed on the base from
four persistence primitives every driver implements, so slug normalization, the
idempotent-registration merge and the patch round-trip are written ONCE and no
backend can drift from another. The Mongo driver lives in its own module and is
imported lazily by the factory, exactly as the session/key/trigger stores do —
a ``storage.driver=json`` deployment must never import ``pymongo``.

**Registration is inert.** ``register`` normalizes, validates, dedupes and
persists. It does not clone, index, resolve a credential, or touch the network;
a repository is simply KNOWN to Mewbo, and consumers opt in later.
"""

from __future__ import annotations

import abc
import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.config import get_config_value
from mewbo_core.workspaces.repositories import Repository, RepositoryPatch, RepositoryRef

logging = get_logger(name="core.repository_store")


class RepositoryStoreBase(abc.ABC):
    """Abstract interface for repository-registry storage backends.

    Subclasses implement persistence only, keyed by an ALREADY-NORMALIZED slug
    (the base normalizes every inbound slug through :class:`RepositoryRef`, so a
    lookup for ``GitHub.com/O/R.git`` finds the row written as
    ``github.com/O/R``). The registry semantics live here.
    """

    # -- persistence primitives (backend-specific) ----------------------------

    @abc.abstractmethod
    def _read(self, slug: str) -> Repository | None:
        """Return the record stored under the normalized *slug*, or ``None``."""

    @abc.abstractmethod
    def _read_all(self) -> list[Repository]:
        """Return every stored record, in any order."""

    @abc.abstractmethod
    def _write(self, repository: Repository) -> None:
        """Upsert *repository* under its own slug."""

    @abc.abstractmethod
    def _remove(self, slug: str) -> bool:
        """Delete the normalized *slug*; return ``True`` if a row was removed."""

    # -- rehydration (one policy, shared by every backend) --------------------

    @staticmethod
    def _parse(record: Mapping[str, Any]) -> Repository | None:
        """Rehydrate one stored record, skipping (with a warning) a malformed one.

        A row that no longer validates is skipped rather than raised, so one bad
        record cannot make the whole registry unreadable — the same stance
        ``CredentialStore._decode`` takes on a malformed credential blob. It
        lives on the base for the same reason the merge does: a driver that
        raised here instead would make one backend stricter than another.
        """
        try:
            return Repository.model_validate(dict(record))
        except Exception:
            logging.warning("Skipping malformed repository record.")
            return None

    @classmethod
    def _parse_all(cls, records: Iterable[Mapping[str, Any]]) -> list[Repository]:
        """Rehydrate *records*, dropping every one that no longer validates."""
        parsed = (cls._parse(record) for record in records)
        return [repository for repository in parsed if repository is not None]

    # -- public surface (shared by every backend) -----------------------------

    @staticmethod
    def normalize(slug: str) -> str | None:
        """Normalize a slug/URL to the store key, or ``None`` if it isn't one.

        Tolerant on purpose — every read path takes whatever slug a route or a
        job happens to carry, and a malformed one must read as "no such
        repository" rather than blow up a request. The fail-fast lives at the
        WRITE boundary, where :class:`Repository` parses its slug strictly.
        """
        ref = RepositoryRef.coerce(slug)
        return ref.slug if ref is not None else None

    def get(self, slug: str) -> Repository | None:
        """Return the registered repository for *slug*, or ``None``."""
        key = self.normalize(slug)
        return self._read(key) if key is not None else None

    def list(self) -> list[Repository]:
        """Return every registered repository, ordered by slug.

        Sorted at the data source, not in a caller: storage enumeration order
        is undefined on both drivers, so leaving it to the UI would make the
        list reshuffle between reads.
        """
        return sorted(self._read_all(), key=lambda repository: repository.slug)

    def register(self, repository: Repository) -> Repository:
        """Persist *repository*, idempotently on its slug. Returns the stored row.

        Registering a slug that is already on file is a MERGE, not a replace —
        :meth:`Repository.merged_with` owns that rule (set fields win,
        ``created_at``/``origin`` are immutable). So a wiki index registering a
        repository a person added by hand enriches the row instead of rewriting
        who added it, and a repeated registration converges rather than
        duplicating.
        """
        now = utc_now_iso()
        existing = self._read(repository.slug)
        if existing is None:
            stored = repository.model_copy(
                update={"created_at": repository.created_at or now, "updated_at": now}
            )
        else:
            stored = existing.merged_with(repository).model_copy(update={"updated_at": now})
        self._write(stored)
        return stored

    def patch(self, slug: str, patch: RepositoryPatch) -> Repository | None:
        """Apply *patch* to the stored repository. ``None`` when it isn't registered.

        Only the patch's SET fields move (see :meth:`RepositoryPatch.apply`), so
        an untouched field is never echoed back as a change and an explicit
        ``null`` genuinely clears.
        """
        existing = self.get(slug)
        if existing is None:
            return None
        updated = patch.apply(existing).model_copy(update={"updated_at": utc_now_iso()})
        self._write(updated)
        return updated

    def delete(self, slug: str) -> bool:
        """Unregister *slug*. Returns ``True`` if a row was removed.

        Deregistration is inert too: it forgets the repository, and touches
        nothing a consumer built from it (an existing wiki index, a managed
        project, a stored credential all survive and keep their own lifecycle).
        """
        key = self.normalize(slug)
        return self._remove(key) if key is not None else False


class InMemoryRepositoryStore(RepositoryStoreBase):
    """Process-local repository store — no persistence, no I/O.

    The in-process driver: tests drive the real registry semantics through it
    without a file or a database, and an embedded caller that wants a scratch
    registry can use it directly. It is deliberately NOT what
    :func:`create_repository_store` returns for a non-Mongo deployment — the
    default ``storage.driver=json`` gets the file-backed driver, because a
    registry that forgets every repository on restart is not a registry.
    """

    def __init__(self) -> None:
        """Start with an empty registry."""
        self._rows: dict[str, Repository] = {}
        self._lock = threading.Lock()

    def _read(self, slug: str) -> Repository | None:
        with self._lock:
            return self._rows.get(slug)

    def _read_all(self) -> list[Repository]:
        with self._lock:
            return list(self._rows.values())

    def _write(self, repository: Repository) -> None:
        with self._lock:
            self._rows[repository.slug] = repository

    def _remove(self, slug: str) -> bool:
        with self._lock:
            return self._rows.pop(slug, None) is not None


class JsonRepositoryStore(RepositoryStoreBase):
    """JSON file-backed repository store — one flat file, list-of-records.

    Mirrors ``JsonTriggerStore``: repositories are an independent flat
    collection, so a single JSON array guarded by a ``threading.Lock`` is
    enough for the API's request threads to share one instance.
    """

    def __init__(self, data_file: str | Path | None = None) -> None:
        """Initialize the store, defaulting the file under the config dir."""
        if data_file is None:
            config_dir = get_config_value(
                "runtime", "config_dir", default=str(Path.home() / ".mewbo")
            )
            data_file = Path(config_dir) / "repositories.json"
        self._data_file = Path(data_file)
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self._data_file.exists():
            return []
        try:
            records = json.loads(self._data_file.read_text())
        except Exception:
            logging.warning("Repository store file unreadable; treating as empty.", exc_info=True)
            return []
        return records if isinstance(records, list) else []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        self._data_file.write_text(json.dumps(records, indent=2))

    def _read(self, slug: str) -> Repository | None:
        for repository in self._read_all():
            if repository.slug == slug:
                return repository
        return None

    def _read_all(self) -> list[Repository]:
        with self._lock:
            records = self._load()
        return self._parse_all(records)

    def _write(self, repository: Repository) -> None:
        with self._lock:
            records = self._load()
            payload = repository.model_dump(mode="json")
            for index, record in enumerate(records):
                if record.get("slug") == repository.slug:
                    records[index] = payload
                    break
            else:
                records.append(payload)
            self._save(records)

    def _remove(self, slug: str) -> bool:
        with self._lock:
            records = self._load()
            remaining = [record for record in records if record.get("slug") != slug]
            if len(remaining) == len(records):
                return False
            self._save(remaining)
            return True


def create_repository_store(data_file: str | Path | None = None) -> RepositoryStoreBase:
    """Return the configured repository store driver (json or mongodb).

    Reads the same ``storage.driver`` the session/key/trigger stores read, so
    the registry follows the deployment's one backend. ``data_file`` is honored
    only by the json driver.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.workspaces.repository_store_mongo import (
            MongoRepositoryStore,  # noqa: PLC0415
        )

        try:
            return MongoRepositoryStore()
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return JsonRepositoryStore(data_file=data_file)


__all__ = [
    "InMemoryRepositoryStore",
    "JsonRepositoryStore",
    "RepositoryStoreBase",
    "create_repository_store",
]
