"""Backfill the packed ``vec_f32`` buffer onto pre-existing wiki embeddings.

``MongoWikiStore.vector_search`` scores a project as one NumPy matrix product by
reading a packed little-endian float32 buffer instead of the canonical BSON
``vector`` array — see ``store.py:_VEC_F32`` for the measurements that motivated
it. ``upsert_embeddings`` writes the buffer, but a row lacking it makes the
search path fall back to the pure-Python scan for the WHOLE project rather than
score only the packed subset — so a store with any unpacked row keeps the slower
path until this runs::

    python -m mewbo_graph.wiki.backfill_vectors            # every slug
    python -m mewbo_graph.wiki.backfill_vectors <slug> …   # only these

Additive and idempotent: it only ever ``$set``s ``vec_f32`` on rows that lack a
correctly-sized one, and never touches ``vector``. Re-running is a no-op, and
rolling the code back leaves the extra field harmless — nothing but the packed
read path looks at it.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

#: Documents per bulk write. Each carries one ~12 KB buffer, so this bounds the
#: in-flight batch at roughly 12 MB rather than the whole collection.
_BATCH = 1000


def backfill(slugs: list[str] | None = None) -> int:
    """Pack every embedding missing a usable ``vec_f32``; return rows written."""
    from pymongo import UpdateOne  # noqa: PLC0415

    from .store import _VEC_F32, MongoWikiStore, _pack_f32, get_wiki_store  # noqa: PLC0415

    store = get_wiki_store()
    if not isinstance(store, MongoWikiStore):
        logger.error("Not a Mongo-backed wiki store — nothing to backfill.")
        return 0

    col = store._col("wiki_embeddings")
    query: dict[str, object] = {} if not slugs else {"slug": {"$in": slugs}}
    written = 0
    pending: list[UpdateOne] = []

    # Project away ``vec_f32`` itself: re-reading a buffer only to confirm it is
    # already correct would make a no-op re-run as expensive as the first pass.
    for doc in col.find(query, {"_id": 1, "vector": 1, _VEC_F32: 1}):
        vector = doc.get("vector") or []
        if not vector:
            continue
        existing = doc.get(_VEC_F32)
        if isinstance(existing, bytes | bytearray) and len(existing) == len(vector) * 4:
            continue
        pending.append(
            UpdateOne({"_id": doc["_id"]}, {"$set": {_VEC_F32: _pack_f32(vector)}})
        )
        if len(pending) >= _BATCH:
            written += col.bulk_write(pending, ordered=False).modified_count
            pending = []
            logger.info("Packed %d embeddings…", written)
    if pending:
        written += col.bulk_write(pending, ordered=False).modified_count
    logger.info("Backfill complete — %d embeddings packed.", written)
    return written


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; extra args are slugs to restrict the backfill to."""
    args = sys.argv[1:] if argv is None else argv
    try:
        backfill(args or None)
    except Exception:
        logger.exception("Embedding backfill failed")
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
