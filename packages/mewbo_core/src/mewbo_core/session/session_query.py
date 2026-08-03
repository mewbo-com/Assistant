#!/usr/bin/env python3
"""The predicate a session listing is narrowed by, owned by one model.

``SessionRuntime.list_sessions`` builds each row by projecting that session's
transcript. So the only filter that costs nothing is one decided BEFORE that
read — which is what this model is for. Every field here is answerable from the
session DOCUMENT alone (the ``sessions`` collection on Mongo, ``index.json`` on
the filesystem driver), so a session the query rejects is never opened.

That is also what lets a filter compose with a PAGE. ``list_session_digests``
resolves this query first and slices the page out of what it admitted, so
``pinned=True`` with a limit returns the newest N pinned sessions rather than the
pinned sessions among the newest N of everything.

**That boundary is the design, not an implementation detail.** ``origin`` and
``status`` are deliberately absent: both are DERIVED at read time and never
stored (see ``session/CLAUDE.md`` and ``loop/CLAUDE.md``), which is what lets a
classifier gain an arm and reclassify existing sessions with no migration.
Materialising either one here to make it filterable would trade that property
away. They stay post-summarisation filters at the consumer, where they are free
— the summary row already carries them.

Pinning is likewise not a filter by default. It is an ORDERING: a pinned session
is still subject to whatever filters are active, so a surface that hides an
origin keeps hiding it when the row is pinned. ``pinned`` exists here only for a
caller that genuinely wants the pinned set alone.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mewbo_core.workspaces.project_identity import ProjectIdentity


class SessionQuery(BaseModel):
    """A narrowing over the session record set, decidable without any transcript.

    Carries its own translation to each backend rather than letting the drivers
    each restate the rule: :meth:`to_mongo` for the Mongo driver,
    :meth:`matches_record` for the filesystem index. Two backends, one definition
    of what the query MEANS.
    """

    model_config = ConfigDict(extra="forbid")

    owner: str | None = Field(
        default=None,
        description=(
            "Narrow to this subject's sessions plus every UNOWNED one. ``None`` "
            "lists everything. The unowned arm is the migration semantic, not a "
            "hole — see SessionStoreBase.list_sessions."
        ),
    )
    include_archived: bool = Field(
        default=False,
        description="Include archived sessions. Archived rows are excluded by default.",
    )
    pinned: bool | None = Field(
        default=None,
        description=(
            "``True`` selects only pinned sessions, ``False`` only unpinned, "
            "``None`` (the default) does not filter on pinning at all. Pinning is "
            "normally an ORDERING rather than a filter."
        ),
    )
    projects: list[str] = Field(
        default_factory=list,
        description=(
            "Match a session that has worked in ANY of these projects. A session "
            "accumulates every project its context has bound to, so an "
            "auto-select session that switched mid-task matches each one."
        ),
    )

    @field_validator("projects", mode="after")
    @classmethod
    def _normalize_projects(cls, values: list[str]) -> list[str]:
        """Normalize requested names the same way stored identities were.

        Validating AT DEFINITION means a caller may pass ``managed:<uuid>`` and
        still match the bare identity that was recorded from it, and that a name
        which normalizes to nothing (blank, or the ``auto`` sentinel) is dropped
        here rather than becoming a clause that can never match.
        """
        seen: list[str] = []
        for value in values:
            identity = ProjectIdentity.normalize(value)
            if identity is not None and identity not in seen:
                seen.append(identity)
        return seen

    @property
    def is_unfiltered(self) -> bool:
        """Whether this query narrows nothing beyond the archived default."""
        return self.owner is None and self.pinned is None and not self.projects

    def to_mongo(self) -> dict[str, Any]:
        """Compile to a MongoDB filter document over the ``sessions`` collection.

        Equality to ``None`` is load-bearing in two clauses: Mongo matches a
        MISSING field that way too, so both arms select documents that carry no
        such field. Without it, adding a field silently disappears every session
        lacking it from its own owner's list.
        """
        query: dict[str, Any] = {}
        if self.owner is not None:
            query["$or"] = [{"owner_subject": self.owner}, {"owner_subject": None}]
        if not self.include_archived:
            query["archived_at"] = None
        if self.pinned is True:
            query["pinned_at"] = {"$ne": None}
        elif self.pinned is False:
            query["pinned_at"] = None
        if self.projects:
            query["projects"] = {"$in": self.projects}
        return query

    def matches_record(
        self,
        *,
        owner: str | None,
        archived: bool,
        pinned: bool,
        projects: list[str],
    ) -> bool:
        """Whether one session's document-level facts satisfy this query.

        The filesystem driver's half of :meth:`to_mongo`. Facts arrive as
        arguments rather than being read here, so the predicate stays pure and a
        test can exercise every combination without a store.
        """
        if self.owner is not None and owner is not None and owner != self.owner:
            return False
        if archived and not self.include_archived:
            return False
        if self.pinned is not None and pinned != self.pinned:
            return False
        if self.projects and not any(p in self.projects for p in projects):
            return False
        return True
