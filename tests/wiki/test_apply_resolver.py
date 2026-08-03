"""``_apply_resolver`` — merge of tree-sitter edges with faithful resolver edges.

Drives the merge helper directly with an INJECTED fake resolver, so no scip
binaries are required: the unit asserts the exact edge/node bookkeeping the
graph-build seam performs when the Python resolver supersedes tree-sitter's
name-matched edges, and that an unavailable resolver leaves the parse untouched.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mewbo_graph.plugins.wiki import build_graph as build_graph_mod
from mewbo_graph.plugins.wiki.build_graph import _apply_resolver
from mewbo_graph.wiki.graph import GraphParseResult
from mewbo_graph.wiki.resolve import ResolutionResult, ResolutionStats
from mewbo_graph.wiki.types import (
    GraphEdge,
    GraphNode,
    GraphResolution,
    make_graph_node,
)

SLUG = "host/org/repo"


def _node(node_id: str, type_: str, name: str, file: str) -> GraphNode:
    return make_graph_node(
        slug=SLUG, node_id=node_id, type=type_, name=name, file=file, range=(0, 1)
    )


def _edge(source: str, target: str, type_: str, target_name: str | None = None) -> GraphEdge:
    return GraphEdge(
        slug=SLUG, source=source, target=target, type=type_, target_name=target_name
    )


def _fixture() -> GraphParseResult:
    """A two-file (one Python, one Go) parse with CONTAINS + synthetic edges."""
    nodes = [
        _node("pyfile", "File", "pkg/a.py", "pkg/a.py"),
        _node("pyfunc", "Function", "helper", "pkg/a.py"),
        _node("pycls", "Class", "Widget", "pkg/a.py"),
        _node("gofile", "File", "pkg/b.go", "pkg/b.go"),
        _node("gofunc", "Function", "GoHelper", "pkg/b.go"),
    ]
    edges = [
        # Structural — all languages — must always survive.
        _edge("pyfile", "pyfunc", "CONTAINS"),
        _edge("pyfile", "pycls", "CONTAINS"),
        _edge("gofile", "gofunc", "CONTAINS"),
        # Python synthetic name-matched edges — the resolver supersedes these.
        _edge("pyfile", "ext-os", "IMPORTS", target_name="os"),
        _edge("pyfile", "ext-h", "CALLS", target_name="other"),
        _edge("pycls", "ext-base", "EXTENDS", target_name="Base"),
        # Go synthetic CALLS — kept (no Go backend yet).
        _edge("gofile", "ext-go", "CALLS", target_name="GoHelper"),
    ]
    return GraphParseResult(nodes=nodes, edges=edges, skipped=["README.md"])


class _FakeResolver:
    """Returns a canned ``ResolutionResult`` regardless of inputs."""

    def __init__(self, result: ResolutionResult) -> None:
        self._result = result

    def resolve(self, repo_root: Path, nodes: object) -> ResolutionResult:
        return self._result


def _resolver_result() -> ResolutionResult:
    """One precise in-repo edge + one synthesized External node (available=True)."""
    precise = _edge("pyfunc", "pycls", "REFERENCES")  # real ids, target_name=None
    external = _node("ext-desc", "External", "requests.get", "")
    return ResolutionResult(
        edges=[precise],
        externals=[external],
        stats=ResolutionStats(indexed_roots=1),
    )


def test_apply_resolver_supersedes_python_edges_keeps_rest() -> None:
    parsed = _fixture()
    fake = _FakeResolver(_resolver_result())
    with patch.object(build_graph_mod, "_resolver_available", return_value=True), \
         patch.object(build_graph_mod, "_make_resolver", return_value=fake):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)

    edge_keys = {(e.source, e.target, e.type) for e in out.edges}

    # Python synthetic IMPORTS/CALLS/EXTENDS dropped.
    assert ("pyfile", "ext-os", "IMPORTS") not in edge_keys
    assert ("pyfile", "ext-h", "CALLS") not in edge_keys
    assert ("pycls", "ext-base", "EXTENDS") not in edge_keys
    # Go synthetic CALLS kept (non-Python source).
    assert ("gofile", "ext-go", "CALLS") in edge_keys
    # CONTAINS kept for every language.
    assert ("pyfile", "pyfunc", "CONTAINS") in edge_keys
    assert ("pyfile", "pycls", "CONTAINS") in edge_keys
    assert ("gofile", "gofunc", "CONTAINS") in edge_keys
    # Resolver's precise edge appended.
    assert ("pyfunc", "pycls", "REFERENCES") in edge_keys

    # External node added to the persisted node set.
    node_ids = {n.node_id for n in out.nodes}
    assert "ext-desc" in node_ids
    assert node_ids.issuperset({"pyfile", "pyfunc", "pycls", "gofile", "gofunc"})
    # skipped passes through untouched.
    assert out.skipped == ["README.md"]


def _partial_resolver_result() -> ResolutionResult:
    """A pass that indexed 6 of the 9 roots it discovered.

    ``available`` is still True (a root DID index), so this is exactly the shape
    that a repo-wide supersede would ruin — the three unindexed roots losing
    their name-matched edges and gaining nothing in return.
    """
    precise = _edge("pyfunc", "pycls", "REFERENCES")
    external = _node("ext-desc", "External", "requests.get", "")
    return ResolutionResult(
        edges=[precise],
        externals=[external],
        stats=ResolutionStats(project_roots=9, indexed_roots=6),
    )


def test_apply_resolver_partial_pass_drops_no_name_matched_edge() -> None:
    """A partial pass ADDS its exact edges and supersedes NOTHING.

    Superseding is all-or-nothing repo-wide, so a pass that covered six of nine
    roots would delete the name-matched Python edges of all nine — leaving the
    three it never reached with no cross-file edges at all. The
    exact edges it *did* produce are additive and correct, so they still land.
    """
    parsed = _fixture()
    fake = _FakeResolver(_partial_resolver_result())
    with patch.object(build_graph_mod, "_resolver_available", return_value=True), \
         patch.object(build_graph_mod, "_make_resolver", return_value=fake):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)

    edge_keys = {(e.source, e.target, e.type) for e in out.edges}

    # Every name-matched Python edge the faithful path supersedes is KEPT.
    assert ("pyfile", "ext-os", "IMPORTS") in edge_keys
    assert ("pyfile", "ext-h", "CALLS") in edge_keys
    assert ("pycls", "ext-base", "EXTENDS") in edge_keys
    # …alongside everything the faithful path also keeps.
    assert ("gofile", "ext-go", "CALLS") in edge_keys
    assert ("pyfile", "pyfunc", "CONTAINS") in edge_keys
    # The resolved edge is still contributed — withholding it would punish the
    # roots that DID index for the ones that did not.
    assert ("pyfunc", "pycls", "REFERENCES") in edge_keys
    # Nothing was dropped: the four original edges the faithful path would have
    # cut are all still here, so the count is the input plus the resolved one.
    assert len(out.edges) == len(parsed.edges) + 1

    node_ids = {n.node_id for n in out.nodes}
    assert "ext-desc" in node_ids  # synthesized External still added
    assert out.skipped == ["README.md"]


def test_apply_resolver_partial_pass_reports_that_it_withheld_superseding() -> None:
    """The counters alone read as a successful pass — say what was withheld."""
    reports = _reports_for(available=True, result=_partial_resolver_result())
    # Two lines: the pass's own counters, then the consequence.
    assert len(reports) == 2
    assert "6/9 project roots indexed" in reports[0]
    assert "Keeping every name-matched Python edge" in reports[1]
    assert "covered part of the repository" in reports[1]


def test_apply_resolver_reports_its_outcome_on_every_branch() -> None:
    """``on_outcome`` fires for all three branches, including the early returns."""
    outcomes: list[GraphResolution] = []

    def _run(*, available: bool, result: ResolutionResult | None) -> None:
        fake = _FakeResolver(result) if result is not None else None
        with patch.object(build_graph_mod, "_resolver_available", return_value=available), \
             patch.object(build_graph_mod, "_make_resolver", return_value=fake):
            _apply_resolver(SLUG, Path("/repo"), _fixture(), on_outcome=outcomes.append)

    _run(available=False, result=None)
    _run(available=True, result=ResolutionResult(edges=[], externals=[], stats=ResolutionStats()))
    _run(available=True, result=_partial_resolver_result())
    _run(available=True, result=_resolver_result())

    assert [o.available for o in outcomes] == [False, True, True, True]
    # ``faithful`` is the property the supersede decision is taken on — only the
    # complete pass may drop an edge.
    assert [o.faithful for o in outcomes] == [False, False, False, True]
    assert outcomes[2].roots_indexed == 6
    assert outcomes[2].roots_discovered == 9
    assert outcomes[2].resolved_edges == 1


def test_apply_resolver_no_binaries_is_identity() -> None:
    parsed = _fixture()
    with patch.object(build_graph_mod, "_resolver_available", return_value=False):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)
    # Resolver unavailable → the parse is returned untouched (byte-identical).
    assert out is parsed


def test_apply_resolver_unavailable_index_is_identity() -> None:
    parsed = _fixture()
    # available is False whenever no root was indexed (default stats).
    empty = ResolutionResult(edges=[], externals=[], stats=ResolutionStats())
    assert empty.available is False
    fake = _FakeResolver(empty)
    with patch.object(build_graph_mod, "_resolver_available", return_value=True), \
         patch.object(build_graph_mod, "_make_resolver", return_value=fake):
        out = _apply_resolver(SLUG, Path("/repo"), parsed)
    assert out is parsed


# ── The degradation must be legible, not merely correct ─────────────────────
#
# Each branch below returns a graph that is fully populated and passes
# validation, so "did the faithful resolver run" is NOT answerable from the
# result — only from what was reported. A deployment whose image lacked the
# scip binaries degraded every index it ran without one observable signal.
# These assert the SIGNAL, which is the part that was missing.


def _reports_for(*, available: bool, result: ResolutionResult | None = None) -> list[str]:
    """Collect everything ``_apply_resolver`` reports for one configured branch."""
    seen: list[str] = []
    fake = _FakeResolver(result) if result is not None else None
    with patch.object(build_graph_mod, "_resolver_available", return_value=available), \
         patch.object(build_graph_mod, "_make_resolver", return_value=fake):
        _apply_resolver(SLUG, Path("/repo"), _fixture(), on_report=seen.append)
    return seen


def test_missing_binaries_report_names_the_cause_and_the_consequence() -> None:
    reports = _reports_for(available=False)
    assert len(reports) == 1
    message = reports[0]
    # The cause has to be actionable — an operator must learn WHICH binaries.
    assert "scip" in message and "scip-python" in message
    # …and the consequence, since the resulting graph looks healthy either way.
    assert "name matching" in message


def test_indexing_no_root_reports_rather_than_passing_silently() -> None:
    empty = ResolutionResult(edges=[], externals=[], stats=ResolutionStats())
    reports = _reports_for(available=True, result=empty)
    assert len(reports) == 1
    # Distinct from "binaries absent": here they RAN and indexed nothing, which
    # is the one-run-per-project-root failure and needs a different repair.
    assert "indexed no project root" in reports[0]
    assert "0/0 project roots indexed" in reports[0]


def test_successful_pass_reports_its_counters() -> None:
    reports = _reports_for(available=True, result=_resolver_result())
    assert len(reports) == 1
    # A success that reported nothing would be indistinguishable from a no-op
    # in the job log, which is the whole failure this reporting exists to end.
    assert "1/0 project roots indexed" in reports[0]


def test_stats_describe_separates_healthy_drops_from_a_dead_pass() -> None:
    """``dropped_unmodelled`` is routine; ``indexed_roots=0`` is an outage."""
    healthy = ResolutionStats(
        project_roots=11,
        indexed_roots=11,
        resolved_exact=76430,
        dropped_unmodelled=174250,
        dropped_ambiguous=0,
    )
    text = healthy.describe()
    assert "11/11 project roots indexed" in text
    assert "76430 exact" in text
    assert "174250 unmodelled" in text
    assert "0 ambiguous" in text
    # The dead pass is legible as such from the same one line.
    assert "0/11 project roots indexed" in ResolutionStats(project_roots=11).describe()
