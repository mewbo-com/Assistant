"""EntityMinter — the DRY write core for abstract entities.

normalize → resolve (shared ladder) → upsert-with-provenance. The deterministic
id makes every write an UPSERT, so a re-index converges and never duplicates —
and a candidate whose id is already stored resolves to ITSELF, skipping the
ladder entirely (see :meth:`EntityMinter.upsert`), which is what makes a
replayed mint land on the same node with the same provenance.
A ``merge`` decision adds the surface name as an alias + appends a mention; a
``flag`` decision writes a new entity with ``status=needs_review`` plus a SAME_AS
edge to the flagged neighbour. All collaborators are injected.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from mewbo_graph._util import utc_now_iso

from .resolver import EntityResolver, LadderDecision
from .types import Entity, EntityEmbedding, EntityMention, EntityRelation

if TYPE_CHECKING:
    from mewbo_graph.wiki.embedder import EmbedderProtocol
    from mewbo_graph.wiki.store import WikiStoreBase


class EntityMinter:
    """Upsert extracted entities into the multiplex with resolution + provenance."""

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: EmbedderProtocol,
        resolver: EntityResolver,
        clock: Callable[[], str] | None = None,
        commit_sha: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Wire collaborators (all injected); ``clock`` is overridable for tests.

        ``commit_sha``/``job_id`` attribute every entity this minter writes to the
        index that ran it — the enrich phase passes the owning job's, so a
        completed re-index supersedes the prior commit's entities. Both default
        ``None``: a Q&A session also mints entities, and those carry no job, so
        they stay commit-less and supersede preserves them as accretive memory.
        """
        self._store = store
        self._embedder = embedder
        self._resolver = resolver
        self._clock = clock or utc_now_iso
        self._commit_sha = commit_sha
        self._job_id = job_id

    def upsert(
        self,
        extracted: Entity,
        *,
        source: str,
        slug: str,
        insight_id: str | None = None,
    ) -> Entity:
        """Resolve *extracted* and upsert it (create / merge / flag).

        A surface whose deterministic id is ALREADY in the store resolves to
        ITSELF and never reaches the ladder. That short-circuit is what makes a
        replay safe: the ladder scores against CURRENT store state and excludes
        the candidate's own id from its block set, so a second identical mint
        could score some neighbour minted in between above ``auto_merge`` and
        write a DIFFERENT entity for a byte-identical call. The id is the
        convergence guarantee — a candidate that already exists cannot be a
        duplicate of anything else.

        So resolution is deterministic under replay only for an entity already
        in the store; a genuinely novel entity's resolution through the ladder
        is not, for exactly the reason above.
        """
        now = self._clock()
        mention = EntityMention(
            source=source, insight_id=insight_id, ts=now, surface_name=extracted.name
        )
        prior = self._store.get_entity(slug, extracted.id)
        if prior is not None:
            return self._apply_new(slug, extracted, mention, status="active", prior=prior)

        decision = self._resolver.resolve(slug, extracted)
        if decision.action == "merge" and decision.target_id:
            return self._apply_merge(slug, extracted, decision, mention)
        if decision.action == "flag" and decision.target_id:
            return self._apply_flag(slug, extracted, decision, mention)
        return self._apply_new(slug, extracted, mention, status="active", prior=None)

    def _apply_new(
        self,
        slug: str,
        extracted: Entity,
        mention: EntityMention,
        *,
        status: str,
        prior: Entity | None,
    ) -> Entity:
        # Deterministic id ⇒ a re-mint of the SAME surface is an idempotent
        # convergence, NOT a fresh node: fold the mention into the existing
        # record so provenance accumulates instead of being overwritten. Every
        # list-valued field UNIONS on this path exactly as it does on the merge
        # path — a field unioned on only one of the two silently regresses on
        # the second pass, since a deterministic id means re-index ALWAYS
        # re-resolves through here.
        if prior is not None:
            aliases = list(dict.fromkeys([*prior.aliases, *extracted.aliases]))
            labels = list(dict.fromkeys([*prior.labels, *extracted.labels]))
            entity = prior.model_copy(
                update={"aliases": aliases, "labels": labels}
            ).with_mention(mention)
        else:
            entity = extracted.model_copy(update={"status": status}).with_mention(mention)
        self._store.upsert_entities(
            slug, [entity], commit_sha=self._commit_sha, job_id=self._job_id
        )
        self._embed(slug, entity)
        return entity

    def _apply_merge(
        self,
        slug: str,
        extracted: Entity,
        decision: LadderDecision,
        mention: EntityMention,
    ) -> Entity:
        target = self._store.get_entity(slug, decision.target_id or "")
        if target is None:  # raced/absent — fall back to a fresh insert
            return self._apply_new(slug, extracted, mention, status="active", prior=None)
        aliases = list(dict.fromkeys([*target.aliases, extracted.name, *extracted.aliases]))
        labels = list(dict.fromkeys([*target.labels, *extracted.labels]))
        survivor = target.model_copy(
            update={
                "aliases": aliases,
                "labels": labels,
                "description": target.description or extracted.description,
            }
        ).with_mention(mention)
        self._store.upsert_entities(
            slug, [survivor], commit_sha=self._commit_sha, job_id=self._job_id
        )
        self._embed(slug, survivor)
        return survivor

    def _apply_flag(
        self,
        slug: str,
        extracted: Entity,
        decision: LadderDecision,
        mention: EntityMention,
    ) -> Entity:
        entity = self._apply_new(
            slug, extracted, mention, status="needs_review", prior=None
        )
        if decision.target_id:
            self._store.upsert_entity_edges(
                slug,
                [
                    EntityRelation(
                        source_id=entity.id, target_id=decision.target_id, type="SAME_AS"
                    )
                ],
                commit_sha=self._commit_sha,
                job_id=self._job_id,
            )
        return entity

    def _embed(self, slug: str, entity: Entity) -> None:
        try:
            rows = self._embedder.embed_nodes([(entity.id, entity.name)], slug=slug)
        except Exception:
            return
        if rows:
            vector = list(rows[0].vector)
            self._store.upsert_entity_embeddings(
                slug,
                [
                    EntityEmbedding(
                        slug=slug,
                        entity_id=entity.id,
                        vector=vector,
                        model=getattr(self._embedder, "model", ""),
                        dim=len(vector),
                    )
                ],
                commit_sha=self._commit_sha,
                job_id=self._job_id,
            )


__all__ = ["EntityMinter"]
