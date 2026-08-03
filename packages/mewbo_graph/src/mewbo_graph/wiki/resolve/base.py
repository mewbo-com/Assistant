"""The faithful-resolution seam: ``SymbolResolver`` + its result type.

A :class:`SymbolResolver` turns a repo's already-extracted tree-sitter
:class:`~mewbo_graph.wiki.types.GraphNode` list into PRECISE relationship edges
— each edge's ``target`` is a real ``node_id`` (an in-repo symbol the references
actually point at, or a synthesized ``External`` node for a genuinely
out-of-repo symbol), never a name-match guess. This is the protocol per-language
resolvers (``scip-python`` for Python today; ``scip-typescript`` /
``rust-analyzer`` tomorrow) plug into, so the indexer can swap exact resolution
in behind one interface.

It lives BELOW the indexer and the view: it imports only ``mewbo_graph`` domain
types + stdlib, never an app, and is wired in separately.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..types import GraphEdge, GraphNode


@dataclass(frozen=True, slots=True)
class ResolutionStats:
    """Provenance of a resolution pass — every reference is accounted for.

    The counters are honest about WHY a reference produced no in-repo edge, so
    the pipeline-wiring step (and validation) can audit faithfulness rather than
    trust a single resolution percentage:

    * ``resolved_exact`` — linked by full symbol (a same-project reference).
    * ``resolved_stitched`` — linked by descriptor across projects (the full
      symbol missed on its ``<pkg> <ver>`` tokens but the descriptor was unique).
    * ``external`` — reference occurrences pointed at a synthesized External node.
    * ``dropped_unmodelled`` — the target IS an in-repo definition but not a kind
      our tree-sitter graph models (a bare attribute, a parameter) → dropped, NOT
      mislinked and NOT faked as external.
    * ``dropped_ambiguous`` — a descriptor matched 2+ in-repo definitions across
      projects → dropped rather than guess one (this is what keeps "never
      mislinks" true).
    * ``dropped_unresolved`` — the reference SITE could not be mapped back to a
      node: an unreadable file, a range outside the file's bytes, or no
      enclosing def to hang the edge's source on. A site failure, not a target
      one — the remaining counters all describe what a reference pointed AT.
    """

    project_roots: int = 0
    indexed_roots: int = 0
    def_sites_mapped: int = 0
    resolved_exact: int = 0
    resolved_stitched: int = 0
    external: int = 0
    extends: int = 0
    dropped_unmodelled: int = 0
    dropped_ambiguous: int = 0
    dropped_unresolved: int = 0

    def describe(self) -> str:
        """One line naming what a pass resolved and what it deliberately dropped.

        Lives on the stats rather than at the call site because every consumer
        that reports a pass wants the same sentence, and the counters only mean
        something together: a high ``dropped_unmodelled`` beside a zero
        ``dropped_ambiguous`` is a HEALTHY pass (attributes and parameters are
        not modelled kinds), while a zero ``indexed_roots`` is a pass that never
        happened. Reading those apart is what made a resolver that contributed
        nothing look, from the outside, exactly like one that ran.
        """
        return (
            f"{self.indexed_roots}/{self.project_roots} project roots indexed, "
            f"{self.def_sites_mapped} definitions mapped; "
            f"{self.resolved_exact} exact + {self.resolved_stitched} stitched "
            f"references, {self.extends} inheritance links, "
            f"{self.external} external; dropped "
            f"{self.dropped_unmodelled} unmodelled / "
            f"{self.dropped_ambiguous} ambiguous / "
            f"{self.dropped_unresolved} unresolved"
        )


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Output of a :class:`SymbolResolver` pass.

    ``edges`` carry REAL ``node_id`` targets: an in-repo target is an existing
    node from the input, an out-of-repo target is one of ``externals`` (both are
    concrete ids — there is no name-match left for a downstream pass to do). Edge
    types are ``REFERENCES`` (a resolved reference occurrence) and ``EXTENDS``
    (a SCIP ``is_implementation`` relationship). ``target_name`` is ``None`` on
    every edge: resolution is by id, so the view's name-based fallback is bypassed
    entirely. ``externals`` are the synthesized ``External`` nodes (one per
    distinct out-of-repo descriptor) the edges point at.
    """

    edges: list[GraphEdge] = field(default_factory=list)
    externals: list[GraphNode] = field(default_factory=list)
    stats: ResolutionStats = field(default_factory=ResolutionStats)

    @property
    def available(self) -> bool:
        """True when the underlying resolver actually ran (a root was indexed)."""
        return self.stats.indexed_roots > 0


@runtime_checkable
class SymbolResolver(Protocol):
    """Resolve a repo's tree-sitter nodes into precise relationship edges."""

    def resolve(
        self, repo_root: Path, nodes: Sequence[GraphNode]
    ) -> ResolutionResult:
        """Return precise ``REFERENCES``/``EXTENDS`` edges + synthesized externals.

        ``repo_root`` is the checkout the *nodes* were extracted from (the
        resolver re-reads source bytes to map SCIP line:col positions onto the
        nodes' byte ranges). An unavailable backend (missing binaries, no
        indexable project) returns an empty, ``available is False`` result —
        never raises — so the indexer degrades cleanly to its prior behaviour.
        """
        ...
