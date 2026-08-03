"""Regression coverage for the app_data `query` result truncation bound.

Prior behaviour: `AppDataTool._query` returned an unbounded Python-repr
string for its payload, and the LOOP's generic per-tool character cap
(`DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS`, 200_000) sliced it wherever it
happened to land — mid-record, with no truncation marker and no flag. A
result like that fails `ast.literal_eval`, which is how the loop's own
envelope parser reads a session tool's output, so the model received text
it could not even parse as the structure it asked for.

The fix makes the TOOL bound its own payload honestly: `_query` now drops
WHOLE documents from the tail once the serialized result would exceed
`_MAX_QUERY_RESULT_CHARS` (a ceiling strictly below the loop's generic cap,
so the loop's cut is structurally unreachable on this path), and reports
the drop via `output_truncated` / `documents_omitted` / `result_char_limit`
rather than silently narrowing.

A second, independent gap in the SAME issue: `count: 1000` against
`limit: 1000` is indistinguishable from `count: 1000` against a collection of
5,970 — a `limit`-bound query has ZERO discriminating power over whether more
matching documents exist. `_query` now asks the store for `limit + 1` (mirroring
`AppsRoutesController.read_data`'s own `limit + 1` probe) and reports
`more_available` when the store held more than `limit`, without ever widening
what `AppDataArgs.limit` accepts (`le=_MAX_QUERY_LIMIT` still refuses at the
boundary). `more_available` and `output_truncated` are independent axes: one
says the FETCH stopped short, the other says the already-fetched rows didn't
FIT the response.

Uses the same FakeAppStore/FakeDataStore/FakeRunStore shape as
`test_apps_plugin_data.py` (Protocol satisfiers, no network, no LLM).
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone

from mewbo_api.apps.models import AppDataDoc, AppFrontend, AppSpec, CollectionSpec, WorkspaceRef
from mewbo_api.apps.plugin.app_data import _MAX_QUERY_RESULT_CHARS, AppDataTool
from mewbo_core.classes import ActionStep

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "s1"
APP_ID = "app1"
COLLECTION = "offerings"


def _app() -> AppSpec:
    return AppSpec(
        app_id=APP_ID,
        title="App One",
        owner_session_id="owner",
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(files={"app.py": "import streamlit as st"}),
        collections=[CollectionSpec(name=COLLECTION, json_schema={"type": "object"})],
        maintainer_session_id=SESSION_ID,
    )


class FakeAppStore:
    def __init__(self, *apps: AppSpec) -> None:
        self._apps = {a.app_id: a for a in apps}

    def get(self, app_id: str) -> AppSpec | None:
        return self._apps.get(app_id)


class FakeDataStore:
    """Mirrors A's store: `query` returns at most `limit` of the seeded rows —
    same head-slice behaviour as the real Json/Mongo stores, which is what
    makes the tool's `limit + 1` probe (for `more_available`) meaningful here."""

    def __init__(self, *, rows: list[AppDataDoc] | None = None) -> None:
        self._rows = rows or []

    def upsert(self, *args, **kwargs) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def query(self, app_id, collection, *, filter=None, limit=100, sort=None):  # noqa: A002
        return list(self._rows)[:limit]

    def delete(self, *args, **kwargs) -> bool:  # pragma: no cover - unused here
        raise NotImplementedError


class FakeRunStore:
    def get_open(self, app_id: str, pipeline_name: str):
        return None

    def save(self, run) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


def _tool(data_store: FakeDataStore) -> AppDataTool:
    return AppDataTool(
        session_id=SESSION_ID,
        app_store=FakeAppStore(_app()),
        data_store=data_store,
        run_store=FakeRunStore(),
    )


def _run(tool: AppDataTool, tool_input: dict):
    step = ActionStep(tool_id="app_data", operation="execute", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


def _row(key: str, *, data: str = "") -> AppDataDoc:
    return AppDataDoc(
        app_id=APP_ID, collection=COLLECTION, key=key, doc={"data": data}, updated_at=NOW
    )


def _query(tool: AppDataTool, *, limit: int = 1000):
    return _run(
        tool,
        {"operation": "query", "app_id": APP_ID, "collection": COLLECTION, "limit": limit},
    )


# ---------------------------------------------------------------------------
# The regression test — a pathologically large collection
# ---------------------------------------------------------------------------


def test_pathological_collection_is_dropped_not_amputated():
    # Each row's "data" field alone is 2_000 chars; 500 of them serialize to
    # ~1,000,000+ chars — well over both _MAX_QUERY_RESULT_CHARS (180_000) and
    # the loop's generic 200_000 cap, so this reproduces the reported shape.
    rows = [_row(f"k{i:04d}", data="x" * 2_000) for i in range(500)]
    tool = _tool(FakeDataStore(rows=rows))

    result = _query(tool)

    # (a) valid Python literal — this is exactly what fails today: the loop's
    # blind character slice lands mid-record and ast.literal_eval raises.
    payload = ast.literal_eval(result.content)

    assert payload["output_truncated"] is True
    assert payload["documents_omitted"] > 0
    assert payload["count"] == len(payload["documents"])
    assert payload["documents_omitted"] + payload["count"] == 500
    assert len(result.content) <= _MAX_QUERY_RESULT_CHARS
    assert payload["result_char_limit"] == _MAX_QUERY_RESULT_CHARS


# ---------------------------------------------------------------------------
# The honest-complete case — nothing to drop
# ---------------------------------------------------------------------------


def test_small_collection_reports_complete_and_untruncated():
    rows = [_row("k1", data="a"), _row("k2", data="b"), _row("k3", data="c")]
    tool = _tool(FakeDataStore(rows=rows))

    payload = ast.literal_eval(_query(tool).content)

    assert payload["output_truncated"] is False
    assert payload["documents_omitted"] == 0
    assert payload["count"] == 3
    assert len(payload["documents"]) == 3


# ---------------------------------------------------------------------------
# Boundary — fit is EXACT, not off by the marker/envelope width
# ---------------------------------------------------------------------------


def _worst_case_envelope_len(total: int) -> int:
    """Independently re-derive the envelope reservation the tool computes.

    Mirrors the tool's own worst-case measurement (``count``/``documents_omitted``
    at the pre-drop total, ``output_truncated`` at its longer spelling) so the
    boundary fixture below can target the computed budget exactly, without
    calling the tool's private method.
    """
    envelope = {
        "operation": "query",
        "app_id": APP_ID,
        "collection": COLLECTION,
        "count": total,
        "documents": [],
        "more_available": False,
        "output_truncated": False,
        "documents_omitted": total,
        "result_char_limit": _MAX_QUERY_RESULT_CHARS,
    }
    return len(str(envelope))


def test_boundary_row_exactly_at_budget_is_kept_next_is_dropped():
    total_rows = 2
    budget = _MAX_QUERY_RESULT_CHARS - _worst_case_envelope_len(total_rows)

    # The skeleton row (empty "data") tells us the fixed overhead; pad "data"
    # so str(row) lands EXACTLY on the budget.
    skeleton = {"key": "k0000", "doc": {"data": ""}, "updated_at": NOW.isoformat()}
    base_len = len(str(skeleton))
    assert budget > base_len, "test fixture assumption: budget must exceed the empty-row overhead"
    pad_len = budget - base_len
    big_row = _row("k0000", data="x" * pad_len)
    big_row_serialized = {
        "key": big_row.key,
        "doc": big_row.doc,
        "updated_at": big_row.updated_at.isoformat(),
    }
    assert len(str(big_row_serialized)) == budget

    small_row = _row("k0001", data="y")  # any nonzero addition now overflows the budget
    tool = _tool(FakeDataStore(rows=[big_row, small_row]))

    payload = ast.literal_eval(_query(tool).content)

    assert payload["count"] == 1
    assert payload["documents"][0]["key"] == "k0000"
    assert payload["documents_omitted"] == 1
    assert payload["output_truncated"] is True
    assert len(_query(tool).content) <= _MAX_QUERY_RESULT_CHARS


# ---------------------------------------------------------------------------
# more_available — count against limit alone has ZERO discriminating power;
# this is the fact #473's own "Compounding" section calls out.
# ---------------------------------------------------------------------------


def test_more_available_true_when_store_holds_more_than_limit():
    # The exact "zero discriminating power" shape: 1_500 stored, limit=1_000.
    # Without more_available, count:1000 reads identically whether storage
    # holds 1,000 or 1,500 — this is what discriminates them.
    rows = [_row(f"k{i:04d}") for i in range(1_500)]
    tool = _tool(FakeDataStore(rows=rows))

    payload = ast.literal_eval(_query(tool, limit=1_000).content)

    assert payload["count"] == 1_000
    assert payload["more_available"] is True
    # the char-budget axis is untouched by this: these rows are tiny.
    assert payload["output_truncated"] is False


def test_more_available_false_at_exactly_limit_rows():
    # The off-by-one the `limit + 1` probe exists to get right: exactly
    # `limit` matching rows means nothing was left behind.
    rows = [_row(f"k{i:04d}") for i in range(1_000)]
    tool = _tool(FakeDataStore(rows=rows))

    payload = ast.literal_eval(_query(tool, limit=1_000).content)

    assert payload["count"] == 1_000
    assert payload["more_available"] is False


def test_more_available_false_when_fewer_than_limit():
    rows = [_row("k1"), _row("k2")]
    tool = _tool(FakeDataStore(rows=rows))

    payload = ast.literal_eval(_query(tool, limit=1_000).content)

    assert payload["count"] == 2
    assert payload["more_available"] is False


def test_more_available_and_output_truncated_are_independent_axes():
    # A collection with MORE matching rows than limit (more_available) whose
    # already-fetched rows ALSO overflow the char budget (output_truncated) —
    # both fire, and neither is derived from the other.
    rows = [_row(f"k{i:04d}", data="x" * 2_000) for i in range(1_500)]
    tool = _tool(FakeDataStore(rows=rows))

    payload = ast.literal_eval(_query(tool, limit=1_000).content)

    assert payload["more_available"] is True  # 1_500 stored, limit=1_000
    assert payload["output_truncated"] is True  # even the 1_000 fetched don't fit
    assert payload["documents_omitted"] > 0
    assert payload["count"] == len(payload["documents"])
    assert len(_query(tool, limit=1_000).content) <= _MAX_QUERY_RESULT_CHARS
