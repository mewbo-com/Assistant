#!/usr/bin/env python3
"""MongoDB-backed repository-registry driver."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_core.config import get_config_value
from mewbo_core.workspaces.repositories import Repository
from mewbo_core.workspaces.repository_store import RepositoryStoreBase


class MongoRepositoryStore(RepositoryStoreBase):
    """MongoDB-backed storage for the repository registry.

    One ``repositories`` collection keyed by the slug (stored as ``_id``, so the
    idempotent upsert is enforced by the database rather than by a read-then-
    write race). Connection and config handling mirror ``MongoKeyStore``.
    """

    def __init__(self, *, uri: str | None = None, database: str | None = None) -> None:
        """Initialize the MongoDB connection."""
        if uri is None:
            uri = get_config_value("storage", "mongodb", "uri", default="mongodb://localhost:27017")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="mewbo")

        self._client: MongoClient = MongoClient(
            uri, maxPoolSize=10, minPoolSize=2, serverSelectionTimeoutMS=5000
        )
        self._db: Database = self._client[cast("str", database)]

        # Fail fast: verify MongoDB is reachable before continuing.
        try:
            self._client.admin.command("ping")
        except Exception as exc:
            raise ConnectionError(
                f"MongoDB is unreachable at the configured URI. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc

    def _col(self) -> Collection:
        """Return the repositories collection."""
        return self._db["repositories"]

    def _read(self, slug: str) -> Repository | None:
        doc = self._col().find_one({"_id": slug})
        return self._parse(doc) if doc is not None else None

    def _read_all(self) -> list[Repository]:
        return self._parse_all(self._col().find({}))

    def _write(self, repository: Repository) -> None:
        payload = repository.model_dump(mode="json")
        self._col().replace_one(
            {"_id": repository.slug}, {"_id": repository.slug, **payload}, upsert=True
        )

    def _remove(self, slug: str) -> bool:
        return self._col().delete_one({"_id": slug}).deleted_count > 0

    @staticmethod
    def _parse(doc: Mapping[str, Any]) -> Repository | None:
        """Drop the ``_id`` mirror, then apply the base's rehydration policy.

        ``_id`` is dropped rather than mapped to a field: it is a duplicate of
        ``slug`` written for the unique-key upsert, and ``Repository`` is
        ``extra="forbid"``, so leaving it in would fail every read.
        """
        payload = {key: value for key, value in doc.items() if key != "_id"}
        return RepositoryStoreBase._parse(payload)


__all__ = ["MongoRepositoryStore"]
