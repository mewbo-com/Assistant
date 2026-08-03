"""Wiki code-graph module.

Two atomic classes share the same domain (the code knowledge graph) at
different phases:

* ``GraphIndex`` runs at indexing time — tree-sitter-driven AST extraction
  yielding flat ``GraphNode`` + ``GraphEdge`` lists that the store persists.
* ``KnowledgeGraphView`` runs at view time — loads a slug's persisted nodes
  + edges, computes lightweight stats, and serialises a Cytoscape-friendly
  wire shape for the ``/v1/wiki/projects/<slug>/graph`` endpoint.

Keeping both in one module avoids splitting the same domain across files;
each class owns its own state and behaviour over that state.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .folder_tree import FolderTree
from .structure_provider import entity_key_for_node
from .types import (
    ClassNode,
    CommitScope,
    ExternalNode,
    FileNode,
    FunctionNode,
    GraphEdge,
    GraphNode,
    InterfaceNode,
    MethodNode,
    ObjectNode,
    PropertyNode,
)

if TYPE_CHECKING:
    from mewbo_graph.entities.types import Entity, EntityRelation

    from .memory_types import MemoryEdge, MemoryNode
    from .store import WikiStoreBase


@dataclass(frozen=True)
class LanguageSpec:
    """One tree-sitter-backed language the code-graph extractor supports.

    ``query_file`` defaults to ``<name>.scm`` — set it only when a language's
    query file diverges from its language name, as ``tsx`` does: it is a
    SEPARATE grammar from ``typescript`` (the two disagree about whether
    ``<T>`` opens a type assertion or a JSX element) but the node types the
    graph captures are identical, so one query file serves both.
    """

    name: str
    extensions: tuple[str, ...]
    query_file: str | None = None

    @property
    def query_filename(self) -> str:
        """Resolved query file name — ``query_file`` override, else ``<name>.scm``."""
        return self.query_file or f"{self.name}.scm"


# Every tree-sitter-backed language GraphIndex supports. ``.kts`` (Kotlin
# build/script files) is deliberately excluded — it's build config, not
# application code the wiki graph should model.
#
# ``.tsx`` MUST route to the ``tsx`` grammar, not ``typescript``. The
# TypeScript grammar reads ``<div>`` as a type assertion, so a file containing
# JSX parses into a tree studded with ERROR nodes and most of its captures are
# lost — measured at 320 of 321 ``.tsx`` files in this repository under
# ``typescript`` versus 19 under ``tsx``. The failure is silent: the file still
# parses, still yields a File node, and simply reports almost no symbols.
_LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", (".py",)),
    LanguageSpec("javascript", (".js", ".jsx")),
    LanguageSpec("typescript", (".ts",)),
    LanguageSpec("tsx", (".tsx",), query_file="typescript.scm"),
    LanguageSpec("go", (".go",)),
    LanguageSpec("rust", (".rs",)),
    LanguageSpec("kotlin", (".kt",)),
    LanguageSpec("java", (".java",)),
)

# Derived extension → language-name map — the single source of truth both
# `parse_file`'s hot path and `prefetch.py`'s language list read from.
_LANG_BY_EXT: dict[str, str] = {
    ext: spec.name for spec in _LANGUAGES for ext in spec.extensions
}
_SPEC_BY_NAME: dict[str, LanguageSpec] = {spec.name: spec for spec in _LANGUAGES}

# Directory segments that mark checked-in THIRD-PARTY code — exact match only
# (never substring: a hypothetical ``distributed/`` must not match ``dist``),
# mirroring the ``_ALWAYS_EXCLUDE_DIRS`` idiom in ``plugins/wiki/scan.py``.
# ``vendor``/``vendored`` is this repository's own convention
# (``packages/mewbo_tools/.../vendor/aider/``, see its ``VENDOR.md``); the rest
# are the common names other repositories use for the same thing.
_VENDORED_DIR_SEGMENTS: frozenset[str] = frozenset({
    "vendor",
    "vendored",
    "third_party",
    "third-party",
    "dist",
    "build",
    ".venv",
    "site-packages",
    "external",
    "generated",
    "__generated__",
})

# A minified file collapses onto one (or a handful of) enormous lines.
# Measured across this repository's own first-party source (1613 files, the
# 9 extensions GraphIndex parses): the longest legitimate line is 815 chars
# (an inlined SVG path in a `.tsx` component); `docs/assets/scalar.standalone
# .min.js` — the one real offender on record — carries a 3.59-million-char
# line. 1000 clears every measured real file with headroom and sits three
# orders of magnitude below the measured minified bundle. A bare byte-size
# threshold was tried and rejected: the tightest cutoff that still caught the
# bundle also caught real first-party modules (a 364KB `backend.py`, a 213KB
# `tool_use_loop.py`) — size doesn't distinguish "big" from "minified" the way
# line shape does.
_MINIFIED_MAX_LINE_CHARS = 1000


def _is_vendored_path(rel_path: Path) -> bool:
    """True if *rel_path* sits under a checked-in third-party directory."""
    return any(part in _VENDORED_DIR_SEGMENTS for part in rel_path.parts[:-1])


def _is_minified(rel_path: Path, source: bytes) -> bool:
    """True if *rel_path*/*source* looks minified.

    Two independent signals, either sufficient on its own: the conventional
    ``*.min.*`` basename marker, and a single line far longer than any real
    source line this repository contains (minification's actual mechanical
    signature — see ``_MINIFIED_MAX_LINE_CHARS``).
    """
    if ".min." in rel_path.name:
        return True
    return max((len(line) for line in source.split(b"\n")), default=0) > (
        _MINIFIED_MAX_LINE_CHARS
    )


@dataclass(frozen=True)
class GraphParseResult:
    """Output of a single-file or repo parse."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    skipped: list[str]  # files whose extension isn't supported

    def __add__(self, other: GraphParseResult) -> GraphParseResult:
        """Merge two results by concatenating their node, edge, and skipped lists."""
        return GraphParseResult(
            nodes=self.nodes + other.nodes,
            edges=self.edges + other.edges,
            skipped=self.skipped + other.skipped,
        )


class GraphIndex:
    """AST-graph extractor.

    Constructed once per wiki indexing job. Caches loaded languages and
    compiled queries so per-file parse is a hot-loop friendly call.
    """

    def __init__(self) -> None:
        """Initialise caches and verify that the wiki extras are installed."""
        self._lang_cache: dict[str, object] = {}   # name → tree_sitter.Language
        self._query_cache: dict[str, object] = {}  # name → tree_sitter.Query
        # Anchored on the PACKAGE, not on this file's depth: ``graph_queries/``
        # belongs to ``mewbo_graph.wiki``, so a __file__-relative path would
        # silently follow this module if it ever moves and fail at query-load
        # time rather than at import.
        self._queries_dir = resources.files("mewbo_graph.wiki") / "graph_queries"
        # Defensive import so missing extras give a clean error.
        try:
            import tree_sitter  # noqa: F401
            import tree_sitter_language_pack  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "GraphIndex requires the 'wiki' extras: install with "
                "`uv sync --extra wiki` or `pip install mewbo-api[wiki]`."
            ) from exc

    def parse_file(
        self, slug: str, file_path: Path, *, repo_root: Path
    ) -> GraphParseResult:
        """Parse a single file.

        Returns an empty result — the path lands in ``skipped``, same as an
        unsupported extension — for a file with no supported extension, one
        under a vendored directory, or one that looks minified. This is the
        ONE seam every caller funnels through (``parse_repo``, the agent-driven
        ``wiki_build_graph`` tool, and the zero-LLM ``GraphOnlyIndexer`` each
        build their own file list independently and never share a walk), so
        gating it here is what makes the exclusion apply everywhere rather
        than needing to be re-applied at each caller's own file-collection
        site.
        """
        ext = file_path.suffix.lower()
        lang_name = _LANG_BY_EXT.get(ext)
        rel_path = file_path.relative_to(repo_root)
        if lang_name is None or _is_vendored_path(rel_path):
            return GraphParseResult(nodes=[], edges=[], skipped=[str(rel_path)])

        from tree_sitter import Parser

        source = file_path.read_bytes()
        if _is_minified(rel_path, source):
            return GraphParseResult(nodes=[], edges=[], skipped=[str(rel_path)])
        lang = self._lang_for(lang_name)
        tree = Parser(lang).parse(source)
        query = self._query_for(lang_name, lang)
        from tree_sitter import QueryCursor

        cursor = QueryCursor(query)
        # ``captures()`` groups nodes per capture NAME but the per-name lists are
        # NOT mutually index-aligned (order varies once matches nest), so
        # ``_extract`` re-aligns each parallel ``.def``/``.name`` pair by start
        # byte via ``_by_position`` before zipping — see that helper.
        captures: dict[str, list] = cursor.captures(tree.root_node)

        rel = str(file_path.relative_to(repo_root))
        return _extract(slug, rel, source, captures)

    def parse_repo(
        self,
        slug: str,
        repo_root: Path,
        files: list[Path],
        *,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> GraphParseResult:
        """Parse every file. Files outside the supported set go to ``skipped``.

        ``on_progress(done, total, path)`` is called after each file when
        supplied. It is INJECTED rather than written here because progress is
        persisted against an indexing job, which this parser knows nothing about
        — and because the loop is the only place that knows how far along it is.
        The throttling is the callee's business (see ``PhaseProgress``); this
        loop reports every file and never decides what is worth writing.
        """
        result = GraphParseResult(nodes=[], edges=[], skipped=[])
        total = len(files)
        for done, fp in enumerate(files, start=1):
            result += self.parse_file(slug, fp, repo_root=repo_root)
            if on_progress is not None:
                on_progress(done, total, str(fp.relative_to(repo_root)))
        return result

    # ── helpers ───────────────────────────────────────────────────────────────

    def _lang_for(self, lang_name: str) -> Any:
        """Return a cached ``tree_sitter.Language`` for *lang_name*."""
        if lang_name not in self._lang_cache:
            self._lang_cache[lang_name] = _load_ts_language(lang_name)
        return self._lang_cache[lang_name]

    def _query_for(self, lang_name: str, lang: Any) -> Any:
        """Return a cached compiled ``tree_sitter.Query`` for *lang_name*."""
        if lang_name not in self._query_cache:
            from tree_sitter import Query

            filename = _SPEC_BY_NAME[lang_name].query_filename
            scm = (self._queries_dir / filename).read_text(encoding="utf-8")
            self._query_cache[lang_name] = Query(lang, scm)
        return self._query_cache[lang_name]


def _load_ts_language(lang_name: str):
    """Return a ``tree_sitter.Language`` for *lang_name* from the language pack.

    ``get_language`` is the library's supported API. It does NOT return a
    grammar bundled in the wheel — on first call for a given language it
    downloads that parser over the network and caches it under
    ``tree_sitter_language_pack.cache_dir()`` (verified empirically; a prior
    docstring here claimed the 1.x line bundles every parser, which is wrong
    for the resolved wheel). Production images pre-warm that cache at BUILD
    time via ``python -m mewbo_graph.wiki.prefetch`` (see that module) so no
    container ever pays the download on its first real parse. Never
    reintroduce the manual ``ctypes`` + ``cache_dir()`` + ``download()``
    loading path — it broke once the pack's ``download()`` became a no-op in
    1.10.x: it returned successfully but wrote nothing, so the manual
    ``ctypes.LoadLibrary`` then failed with "cannot open shared object file"
    on a path that was never created.
    """
    import tree_sitter_language_pack as tlp

    return tlp.get_language(lang_name)


def _stable_id(slug: str, kind: str, name: str, file: str, byte_start: int) -> str:
    """Deterministic node id over (slug, kind, name, file, byte_start)."""
    h = hashlib.sha1(
        f"{slug}|{kind}|{name}|{file}|{byte_start}".encode()
    ).hexdigest()
    return h[:16]


def _by_position(nodes: list) -> list:
    """Order capture nodes by start byte so parallel captures zip correctly.

    ``QueryCursor.captures()`` groups matches per capture NAME, but the per-name
    lists are NOT guaranteed to be mutually index-aligned — their order varies
    run-to-run, so a naive ``zip(captures["x.def"], captures["x.name"])`` can pair
    a def with the WRONG name (silently attaching a node's name to another node's
    byte range, which corrupts the graph and any downstream resolver matching on
    name). Sorting BOTH lists by start byte lines up the i-th def with the i-th
    name regardless of capture order.

    Document order is a sound join key even though captured defs NEST (a def
    inside a class, a class inside a function), because in every grammar here a
    def's own name token precedes its body: for defs ``d1 < d2`` by start byte,
    ``name(d1) < d2.start <= name(d2)`` when d2 is nested in d1, and
    ``name(d1) < d1.end <= d2.start <= name(d2)`` when they are siblings. Both
    orderings agree, so the zip holds. What would break it is a family whose
    def can contain NO name at all — that one pairs by containment instead, via
    ``_pair_defs_with_names``.
    """
    return sorted(nodes, key=lambda n: n.start_byte)


def _pair_defs_with_names(defs: list, names: list) -> list[tuple[Any, Any | None]]:
    """Pair each def with the name node CONTAINED in its byte range.

    Unlike the strict positional ``zip(_by_position(defs), _by_position(names))``
    the Class/Interface/Function/Method families use (every def there has
    exactly one name, so sort-then-zip is sound), a newer family may have a
    def with NO name at all — e.g. Kotlin's anonymous ``companion object`` has
    no ``type_identifier`` token to capture.

    Assigns each name token to the TIGHTEST (smallest-span) containing def —
    the same algorithm ``_subkinds_for`` uses, for the same reason. A def of
    this family can NEST inside another (an `object` declared inside an
    anonymous `companion object`'s body): the outer def's wider range also
    contains the inner def's own name token. A single left-to-right pointer
    into ``names`` (the prior implementation) assigns that name to whichever
    def it reaches FIRST by position — the OUTER one — silently swapping
    names between two real nodes (the outer anonymous companion ends up named
    after the inner object; the inner object falls back to "Companion").
    Scanning every def for each name and keeping the smallest match is what
    correctly routes the name to its OWN (innermost) def regardless of
    nesting; a def with no name token anywhere inside it is paired with
    ``None`` — the caller decides the fallback (or to skip it).
    """
    defs = _by_position(defs)
    names = _by_position(names)
    assigned: dict[int, Any] = {}  # def index → its name node
    for name_node in names:
        best_idx: int | None = None
        best_span: int | None = None
        for i, d in enumerate(defs):
            if d.start_byte <= name_node.start_byte < d.end_byte:
                span = d.end_byte - d.start_byte
                if best_span is None or span < best_span:
                    best_idx, best_span = i, span
        if best_idx is not None:
            assigned[best_idx] = name_node
    return [(d, assigned.get(i)) for i, d in enumerate(defs)]


def _split_callables_by_scope(defs: list, class_defs: list) -> dict[int, bool]:
    """Map each callable def's ``start_byte`` → is it a Method (else Function).

    Some grammars cannot express "a def anywhere inside a class". A
    tree-sitter pattern matches a FIXED ancestor shape and the language has no
    descendant axis, so Python's three original patterns had to enumerate
    parent shapes — module, class body — and consequently missed every shape
    nobody listed: a decorated def (wrapped in ``decorated_definition``), a def
    nested in another def, a def under an ``if``/``try`` guard. Enumerating
    harder does not converge; the split has to happen here instead, over byte
    ranges, where "anywhere inside" is a question that can actually be asked.

    A def's kind follows its NEAREST enclosing definition, which is the rule a
    reader applies: a def whose closest container is a class is a method, and a
    def nested inside a method is a plain function again. Hence tightest
    (smallest-span) containment rather than "is any class an ancestor" — the
    same trap ``_subkinds_for`` documents, in a different disguise. Callables
    are compared against each other as well as against classes, since a class
    only wins when no closer callable stands between it and *d*.
    """
    class_spans = {(c.start_byte, c.end_byte) for c in class_defs}
    is_method: dict[int, bool] = {}
    for d in defs:
        best_span: int | None = None
        best_is_class = False
        for other in (*defs, *class_defs):
            if other.start_byte == d.start_byte and other.end_byte == d.end_byte:
                continue  # itself
            if other.start_byte <= d.start_byte and d.end_byte <= other.end_byte:
                span = other.end_byte - other.start_byte
                if best_span is None or span < best_span:
                    best_span = span
                    best_is_class = (other.start_byte, other.end_byte) in class_spans
        is_method[d.start_byte] = best_is_class
    return is_method


def _subkinds_for(captures: dict[str, list], family: str, defs: list) -> dict[int, str]:
    """Map a def's ``start_byte`` → subkind string via the capture convention.

    A capture named ``@<family>.subkind.<value>`` (e.g.
    ``@class.subkind.data_class``) mints a new subkind purely by naming it in
    a language's ``.scm`` file — no Python change required. This scans every
    capture key carrying the ``.subkind.`` infix whose head matches *family*,
    and assigns ``<value>`` to whichever *defs* entry byte-contains the
    captured token (the token may be the def itself, e.g. a whole
    ``companion_object``, or a modifier keyword nested inside it).

    Picks the TIGHTEST (smallest-span) containing def, not merely the first
    one found — a data class nested inside a sealed class both belong to the
    "class" family, so the sealed class's wider range also technically
    contains the nested data class's "data" token; without this, the nested
    def's own subkind is silently lost to its outer container.
    """
    result: dict[int, str] = {}
    for key, tokens in captures.items():
        head, marker, value = key.partition(".subkind.")
        if not marker or head != family:
            continue
        for tok in tokens:
            best = None
            for d in defs:
                if d.start_byte <= tok.start_byte < d.end_byte and (
                    best is None or (d.end_byte - d.start_byte) < (best.end_byte - best.start_byte)
                ):
                    best = d
            if best is not None:
                result[best.start_byte] = value
    return result


def _dedupe_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    """Collapse duplicate ``node_id``s, keeping the variant carrying a subkind.

    Defensive backstop: tree-sitter's own ``captures()`` already merges
    identical (capture-name, node) pairs within one compiled query, so a
    generic pattern (e.g. ``class.def`` matching every ``class_declaration``
    with a "class" keyword) layered under a subkind-specific one (e.g.
    Kotlin's "enum class" pattern) matching the SAME node does not actually
    re-emit a second ``GraphNode`` in practice — verified empirically. This
    fold exists so that invariant never has to be re-verified per language:
    ``CodeGraph``'s node-id-uniqueness validator would otherwise reject the
    whole graph outright if a future query combination ever did produce two
    nodes for one id.
    """
    best: dict[str, GraphNode] = {}
    order: list[str] = []
    for n in nodes:
        prev = best.get(n.node_id)
        if prev is None:
            order.append(n.node_id)
            best[n.node_id] = n
        elif prev.subkind is None and n.subkind is not None:
            best[n.node_id] = n
    return [best[nid] for nid in order]


def _extract(
    slug: str,
    rel_path: str,
    source: bytes,
    captures: dict[str, list],
) -> GraphParseResult:
    """Translate tree-sitter captures into GraphNode + GraphEdge.

    ``captures`` is a dict of ``{capture_name: [Node, ...]}`` as returned by
    ``QueryCursor.captures()`` in tree-sitter 0.25+. Nodes within each list
    are in document (byte) order, which means zipping parallel capture names
    (e.g. ``class.def`` with ``class.name``) is safe for non-overlapping
    patterns that produce exactly one sibling capture per match.
    """
    # File node — always emitted; contains all nodes inside the file.
    file_node = FileNode(
        slug=slug,
        node_id=_stable_id(slug, "File", rel_path, rel_path, 0),
        name=rel_path,
        file=rel_path,
        range=(0, len(source)),
        docstring=None,
    )
    nodes: list[GraphNode] = [file_node]
    edges: list[GraphEdge] = []

    # Classes. ``class.subkind.<value>`` captures (e.g. Kotlin's
    # "data"/"sealed"/"enum" modifiers, Java's "enum"/"record") refine a subset
    # of these — see ``_subkinds_for``. A language may ALSO layer a
    # subkind-specific pattern that re-declares the same class.def/name pair
    # (Kotlin's shared ``class_declaration`` node needs this to discriminate
    # data/sealed/enum from a plain class); ``_dedupe_nodes`` folds those at
    # the end of this function.
    cls_defs = _by_position(captures.get("class.def", []))
    cls_names = _by_position(captures.get("class.name", []))
    cls_subkinds = _subkinds_for(captures, "class", cls_defs)
    for cls_def_node, cls_name_node in zip(cls_defs, cls_names):
        name = cls_name_node.text.decode()
        nid = _stable_id(slug, "Class", name, rel_path, cls_def_node.start_byte)
        nodes.append(
            ClassNode(
                slug=slug,
                node_id=nid,
                name=name,
                file=rel_path,
                range=(cls_def_node.start_byte, cls_def_node.end_byte),
                docstring=_extract_docstring(cls_def_node),
                subkind=cls_subkinds.get(cls_def_node.start_byte),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Interfaces (TypeScript, Go, Rust, Kotlin, Java — trait/interface → Interface
    # node). Carries subkinds for the same reason Class does: TypeScript's
    # `type X = …` declares a type the way an interface does and is
    # interchangeable with one at most call sites, so it maps to this kind with
    # `subkind="type_alias"` rather than earning a new structural kind.
    if_defs = _by_position(captures.get("interface.def", []))
    if_subkinds = _subkinds_for(captures, "interface", if_defs)
    for if_def_node, if_name_node in zip(
        if_defs,
        _by_position(captures.get("interface.name", [])),
    ):
        name = if_name_node.text.decode()
        nid = _stable_id(slug, "Interface", name, rel_path, if_def_node.start_byte)
        nodes.append(
            InterfaceNode(
                slug=slug,
                node_id=nid,
                name=name,
                file=rel_path,
                range=(if_def_node.start_byte, if_def_node.end_byte),
                docstring=None,
                subkind=if_subkinds.get(if_def_node.start_byte),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Objects (Kotlin `object` / `companion object`; Scala later). A companion
    # object's name is OPTIONAL in the grammar — an anonymous companion has no
    # name token to capture — so pairing uses byte-containment
    # (``_pair_defs_with_names``) instead of the strict positional zip the
    # families above use; a name-less def falls back to the literal
    # "Companion" (Kotlin's own implicit name for it).
    obj_defs = captures.get("object.def", [])
    obj_names = captures.get("object.name", [])
    obj_subkinds = _subkinds_for(captures, "object", _by_position(obj_defs))
    for obj_def_node, obj_name_node in _pair_defs_with_names(obj_defs, obj_names):
        name = obj_name_node.text.decode() if obj_name_node is not None else "Companion"
        nid = _stable_id(slug, "Object", name, rel_path, obj_def_node.start_byte)
        nodes.append(
            ObjectNode(
                slug=slug,
                node_id=nid,
                name=name,
                file=rel_path,
                range=(obj_def_node.start_byte, obj_def_node.end_byte),
                docstring=None,
                subkind=obj_subkinds.get(obj_def_node.start_byte),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Functions and methods. A language whose grammar CAN separate the two by
    # ancestor shape captures them as distinct families; one whose grammar
    # cannot (Python — see python.scm) emits a single unclassified `callable.*`
    # family instead, which `_split_callables_by_scope` divides here by byte
    # containment. Both routes converge on the same two node kinds, so nothing
    # downstream — including a node_id already minted for a symbol the old
    # patterns did reach — can tell which route produced a given node.
    fn_pairs = list(
        zip(
            _by_position(captures.get("function.def", [])),
            _by_position(captures.get("function.name", [])),
        )
    )
    m_pairs = list(
        zip(
            _by_position(captures.get("method.def", [])),
            _by_position(captures.get("method.name", [])),
        )
    )
    call_defs = _by_position(captures.get("callable.def", []))
    call_names = _by_position(captures.get("callable.name", []))
    call_is_method = _split_callables_by_scope(call_defs, cls_defs)
    for call_def_node, call_name_node in zip(call_defs, call_names):
        target = m_pairs if call_is_method[call_def_node.start_byte] else fn_pairs
        target.append((call_def_node, call_name_node))

    for fn_def_node, fn_name_node in fn_pairs:
        name = fn_name_node.text.decode()
        nid = _stable_id(slug, "Function", name, rel_path, fn_def_node.start_byte)
        nodes.append(
            FunctionNode(
                slug=slug,
                node_id=nid,
                name=name,
                file=rel_path,
                range=(fn_def_node.start_byte, fn_def_node.end_byte),
                docstring=_extract_docstring(fn_def_node),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Methods
    for m_def_node, m_name_node in m_pairs:
        name = m_name_node.text.decode()
        nid = _stable_id(slug, "Method", name, rel_path, m_def_node.start_byte)
        nodes.append(
            MethodNode(
                slug=slug,
                node_id=nid,
                name=name,
                file=rel_path,
                range=(m_def_node.start_byte, m_def_node.end_byte),
                docstring=_extract_docstring(m_def_node),
            )
        )
        # Method CONTAINS edge: the class that contains this method.
        # We attach it to the file node as a CONTAINS edge (class→method scoping
        # ships in Task 3.2 with cross-language support).
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Properties (fields / constants) — Kotlin `property_declaration`/
    # `class_parameter`, Java `field_declaration`. Every CURRENTLY CAPTURED
    # def/name pair is 1:1 (a Kotlin destructuring `val (a, b) = …` doesn't
    # match `property.def` at all — its container is `multi_variable_
    # declaration`, not `variable_declaration` — so it never reaches this
    # loop). Reuses the same containment helper as Object anyway: it's free
    # (no new code, no behaviour change for the 1:1 case) and one fewer
    # capture-shape assumption to re-verify if a future .scm pattern for this
    # family ever does produce a name-less def.
    prop_defs = captures.get("property.def", [])
    prop_names = captures.get("property.name", [])
    prop_subkinds = _subkinds_for(captures, "property", _by_position(prop_defs))
    for prop_def_node, prop_name_node in _pair_defs_with_names(prop_defs, prop_names):
        if prop_name_node is None:
            continue
        name = prop_name_node.text.decode()
        nid = _stable_id(slug, "Property", name, rel_path, prop_def_node.start_byte)
        nodes.append(
            PropertyNode(
                slug=slug,
                node_id=nid,
                name=name,
                file=rel_path,
                range=(prop_def_node.start_byte, prop_def_node.end_byte),
                docstring=None,
                subkind=prop_subkinds.get(prop_def_node.start_byte),
            )
        )
        edges.append(
            GraphEdge(slug=slug, source=file_node.node_id, target=nid, type="CONTAINS")
        )

    # Cross-file edges (IMPORTS/CALLS/EXTENDS) target a symbol *by name*. Their
    # real node_id is unknown at single-file parse time (the definition may live
    # in another file parsed later), so we keep a synthetic ``<external>`` target
    # id AND carry the raw ``target_name``. ``KnowledgeGraphView.for_slug`` then
    # resolves each name against the whole-repo node set: a hit re-points the
    # edge at the real node (connecting File-clusters through shared symbols), a
    # miss converges on one named ``External`` view-node. Keeping the synthetic
    # id here means the persisted ``wiki_graph_nodes`` table stays real-in-repo
    # symbols only.

    # Imports — IMPORTS edges from file to the imported module name.
    import_nodes = captures.get("import.module", []) + captures.get(
        "import.from_module", []
    )
    for mod_node in import_nodes:
        target_name = mod_node.text.decode()
        edges.append(
            GraphEdge(
                slug=slug,
                source=file_node.node_id,
                target=_stable_id(slug, "Module", target_name, "<external>", 0),
                type="IMPORTS",
                target_name=target_name,
            )
        )

    # Calls — CALLS edges from the file to the called name. Exact scope
    # resolution is out of v1's scope.
    for call_name_node in captures.get("call.name", []):
        callee = call_name_node.text.decode()
        edges.append(
            GraphEdge(
                slug=slug,
                source=file_node.node_id,
                target=_stable_id(slug, "Function", callee, "<external>", 0),
                type="CALLS",
                target_name=callee,
            )
        )

    # EXTENDS edges from subclass → superclass. ``_by_position`` keeps each
    # subclass paired with its own superclass (single inheritance); a class with
    # multiple bases is a separate, pre-existing limitation of the 1:1 zip.
    subs = _by_position(captures.get("subclass.name", []))
    sups = _by_position(captures.get("superclass.name", []))
    for sub_node, sup_node in zip(subs, sups):
        sub_name = sub_node.text.decode()
        sup_name = sup_node.text.decode()
        sub_id = _stable_id(slug, "Class", sub_name, rel_path, sub_node.start_byte)
        edges.append(
            GraphEdge(
                slug=slug,
                source=sub_id,
                target=_stable_id(slug, "Class", sup_name, "<external>", 0),
                type="EXTENDS",
                target_name=sup_name,
            )
        )

    # Extension functions (Kotlin `fun Receiver.name()`) — a REFERENCES edge
    # from the already-emitted Function node to a synthetic external Class
    # named after the receiver type, tagged so a consumer can tell it apart
    # from an ordinary cross-file reference.
    #
    # CRITICAL: the source id is recomputed here from the DEF node's
    # start_byte — the SAME (kind="Function", name, file, def.start_byte)
    # recipe the top-level function block above uses — NOT the name token's
    # byte. The EXTENDS block just above computes its subclass id from the
    # NAME capture's byte instead of the def's, so it matches no persisted
    # node and the view silently drops it (a known, out-of-scope bug — not
    # replicated here, and not fixed there either).
    ext_defs = _by_position(captures.get("extension.def", []))
    ext_names = _by_position(captures.get("extension.name", []))
    ext_receivers = _by_position(captures.get("extension.receiver", []))
    for ext_def_node, ext_name_node, ext_recv_node in zip(
        ext_defs, ext_names, ext_receivers
    ):
        ext_fn_name = ext_name_node.text.decode()
        receiver = ext_recv_node.text.decode()
        source_id = _stable_id(slug, "Function", ext_fn_name, rel_path, ext_def_node.start_byte)
        edges.append(
            GraphEdge(
                slug=slug,
                source=source_id,
                target=_stable_id(slug, "Class", receiver, "<external>", 0),
                type="REFERENCES",
                target_name=receiver,
                attributes={"kotlin.extension": True},
            )
        )

    return GraphParseResult(nodes=_dedupe_nodes(nodes), edges=edges, skipped=[])


_MEMORY_LABEL_CHARS = 60
_MEMORY_SNIPPET_CHARS = 120


@dataclass(frozen=True, slots=True)
class KnowledgeGraphView:
    """Slug-scoped projection of the persisted MULTIPLEX graph for the viewer.

    Three node layers share one viewer payload: the tree-sitter ``ast`` layer
    (File/Class/Function/… + synthesized ``External`` convergence nodes), the
    abstract ``entity`` layer, and the atomic-note ``memory`` layer. Edges carry
    a ``layer`` tag — ``ast`` (CONTAINS/IMPORTS/CALLS/EXTENDS/REFERENCES),
    ``entity`` (entity↔entity RELATES, open-vocab verb in ``label``), ``memory``
    (note RELATES) and ``cross`` (ANCHORS spanning layers).

    Construction is exclusively via ``for_slug`` so the invariant — every node +
    edge belongs to the same slug, and every emitted edge endpoint is a real
    node in the payload — stays enforced in one place. Once built, the instance
    is immutable; safe to share across requests and trivially cacheable upstream.
    """

    slug: str
    # AST layer (real in-repo nodes only) + synthesized External convergence
    # nodes (one per distinct unresolved cross-file symbol name).
    nodes: tuple[GraphNode, ...]
    external_nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]  # ast-layer edges (endpoints all in payload)
    # Entity layer.
    entity_nodes: tuple[Entity, ...]
    entity_edges: tuple[EntityRelation, ...]  # entity↔entity RELATES only
    # Memory layer.
    memory_nodes: tuple[MemoryNode, ...]
    memory_edges: tuple[MemoryEdge, ...]  # note↔note RELATES only
    # Cross-layer ANCHORS, pre-resolved to ``(source_node_id, target_node_id)``.
    cross_edges: tuple[tuple[str, str], ...]
    total_nodes: int  # full AST node count (pre-cap), for the "showing N of M" banner
    total_edges: int  # full AST edge count (pre-cap)
    # Externals the payload COULD have carried, before the cap took its share.
    # Distinct from ``total_nodes`` because externals are synthesized at read
    # time rather than stored, and the cap can drop them while leaving every
    # real AST node in place — without this the banner reports "showing N of N"
    # for a payload that silently lost nodes.
    total_externals: int = 0
    # Directory scaffold for the "hierarchy" wire mode — ``None`` in the default
    # mode, so ``to_wire`` stays byte-identical when hierarchy is off.
    folder_tree: FolderTree | None = None

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def _reanchor_entity_edges(
        cls,
        store: WikiStoreBase,
        slug: str,
        stranded: list[EntityRelation],
        live_nodes: list[GraphNode],
        payload_ast_ids: set[str],
    ) -> list[tuple[str, str]]:
        """Re-point entity ANCHORS that address a superseded generation's nodes.

        An entity anchors to a code node by RAW ``node_id``, and a node id
        embeds the symbol's ``start_byte`` (``_stable_id``). So every edit that
        shifts a symbol within its file re-keys it, and an anchor minted
        against an earlier index addresses an id the current generation does
        not contain. Unscoped that never showed, because the superseded node
        was still in the payload to point at — which is exactly the bug commit
        scoping fixes, and exactly why scoping alone would silently sever most
        of the entity→code bridge.

        The repair is by ``entity_key`` (``file#name``), which carries NO byte
        offset and so survives the edit that broke the id. Cost is bounded by
        the number of stranded anchors, not by the graph: the superseded nodes
        are fetched by explicit id rather than by reading the union.

        The memory layer needs none of this — a memory ANCHORS edge already
        stores an ``entity_key`` as its target rather than a node id, so it
        re-resolves against whatever generation is loaded.

        An anchor whose symbol genuinely no longer exists resolves to nothing
        and stays dropped. That is the correct outcome, not a loss: an entity
        pointing at deleted code is what this phase set out to stop rendering.
        """
        wanted = {rel.target_id for rel in stranded}
        if not wanted:
            return []
        live_by_key: dict[str, str] = {}
        for n in live_nodes:
            live_by_key.setdefault(entity_key_for_node(n), n.node_id)
        superseded = store.query_graph(
            slug, scope=CommitScope.every(), node_ids=wanted
        )
        repointed: dict[str, str] = {}
        for n in superseded:
            live_id = live_by_key.get(entity_key_for_node(n))
            if live_id is not None and live_id in payload_ast_ids:
                repointed[n.node_id] = live_id
        return [
            (rel.source_id, repointed[rel.target_id])
            for rel in stranded
            if rel.target_id in repointed
        ]

    @classmethod
    def for_slug(
        cls,
        store: WikiStoreBase,
        slug: str,
        *,
        node_limit: int | None = None,
        hierarchy: bool = False,
        scope: CommitScope | None = None,
    ) -> KnowledgeGraphView:
        """Load the full multiplex (ast + entity + memory layers) for *slug*.

        **Commit-scoped.** *scope* defaults to ``store.live_scope(slug)`` — the
        project's own commit — so the viewer shows the code as it is now. Before
        this the AST layer was read unscoped, which is why the payload was the
        union of every generation ever indexed: deleted code rendered alongside
        live code with nothing distinguishing them, and the node count grew
        monotonically with re-indexes rather than with the repository.

        Pass an explicit scope to read a specific generation; pass
        ``CommitScope.every()`` to restore the pre-scoping union (which the
        entity-anchor repair below relies on, and nothing else should).

        AST connectivity: a cross-file IMPORTS/CALLS/EXTENDS edge carries the
        raw ``target_name``; if that name resolves to a real in-repo node it is
        re-pointed there (genuinely connecting File-clusters through shared
        symbols), otherwise every reference to the same external name converges
        on ONE synthesized ``External`` view-node.

        ``node_limit`` (when set and exceeded) degree-prunes the **whole AST
        payload** — real nodes AND the view-synthesized ``External`` nodes
        together, not the persisted layer alone. The AST nodes are pruned
        first (unchanged from before: highest-degree kept, ties on
        ``node_id``); External nodes then get whatever budget is left,
        pruned by the same degree-then-``node_id`` rule over their surviving
        edges. A ``node_limit`` that the AST layer alone does not exceed can
        still cap externals down, and an AST layer that exhausts the whole
        budget leaves externals at zero — both are the cap "governing the
        whole payload" rather than only the persisted one. Entity + memory
        layers are always fully included (they're small) and never counted
        against this cap. ``total_nodes``/``total_edges`` always reflect the
        full AST graph so the wire response can honestly say "showing N of M".

        Cross-layer ANCHORS are reconciled to real node ids in O(nodes+edges):
        memory ANCHORS targets (``EntityKey`` / ``entity:<id>``) batch-resolve
        through the existing ``CodeStructureProvider`` + ``EntityAnchorResolver``;
        an anchor that resolves to nothing is dropped (no dangling edges).

        ``hierarchy`` (default off) synthesises a directory scaffold via
        ``FolderTree`` from the kept File nodes' paths — folder nodes + folder
        ``CONTAINS`` edges, and a single ``parentId`` per node — and stamps it
        onto the wire in ``to_wire``. Off ⇒ the wire is byte-identical to the
        default mode (the SCG / Agentic Search reuse path is undisturbed).
        """
        from mewbo_graph.entities.anchor import EntityAnchorResolver  # noqa: PLC0415

        from .structure_provider import CodeStructureProvider  # noqa: PLC0415

        # ── AST layer ────────────────────────────────────────────────────
        scope = store.live_scope(slug) if scope is None else scope
        all_nodes = store.query_graph(slug, scope=scope)
        all_edges = list(store.list_edges(slug, scope=scope))
        total_nodes = len(all_nodes)

        # Resolve cross-file edge targets by name → real in-repo node. Build the
        # name index once (O(nodes)); externals converge on one synthesized node.
        by_name: dict[str, str] = {}
        for n in all_nodes:
            by_name.setdefault(n.name, n.node_id)
        resolved_edges, external_nodes = cls._resolve_ast_edges(
            slug, all_edges, by_name, {n.node_id for n in all_nodes}
        )
        # ``total_edges`` is the FULL emittable AST edge count — measured AFTER
        # resolution, because that pass drops orphan structural edges (a
        # ``target_name=None`` edge with a missing endpoint). Using the raw
        # ``len(all_edges)`` would overstate "M" by the orphans that never reach
        # the payload. (Resolution adds External nodes but never adds/drops
        # cross-file edges, so this count is cap-independent.)
        total_edges = len(resolved_edges)

        # Degree over the full resolved edge set — needed by the AST prune
        # below (when it fires) and by the external cap that follows it, so
        # it's computed once whenever a limit is in play at all. Left EMPTY
        # when nothing is capped: both readers are behind the same
        # ``node_limit is not None`` guard, and a Counter answers 0 for an
        # absent key, so an unlimited read can never see a wrong rank.
        degree: Counter[str] = Counter()
        if node_limit is not None:
            for e in resolved_edges:
                degree[e.source] += 1
                degree[e.target] += 1

        # Degree-prune the AST layer (externals follow their surviving edge,
        # then get their own cap below).
        if node_limit is None or total_nodes <= node_limit:
            nodes = list(all_nodes)
        else:
            nodes = sorted(
                all_nodes, key=lambda n: (-degree[n.node_id], n.node_id)
            )[:node_limit]

        kept_ast_ids = {n.node_id for n in nodes}
        ext_by_id = {n.node_id: n for n in external_nodes}
        edges = [
            e
            for e in resolved_edges
            if e.source in kept_ast_ids
            and (e.target in kept_ast_ids or e.target in ext_by_id)
        ]
        # Keep only externals still referenced by a surviving edge.
        live_ext_ids = {e.target for e in edges if e.target in ext_by_id}

        # ``node_limit`` caps the WHOLE payload, not just the persisted AST
        # layer — a view-synthesized External node still counts against it.
        # Externals get whatever budget the AST prune above left behind (zero
        # when that prune already spent the full cap), ranked by the SAME
        # degree-then-``node_id`` rule so the tie-break policy is one rule,
        # not two. Final tuple is re-sorted by ``node_id`` for stable output,
        # matching the AST layer's own ordering guarantee.
        if node_limit is None:
            live_externals = [ext_by_id[i] for i in live_ext_ids]
        else:
            budget = max(0, node_limit - len(nodes))
            live_externals = sorted(
                (ext_by_id[i] for i in live_ext_ids),
                key=lambda n: (-degree[n.node_id], n.node_id),
            )[:budget]
        kept_externals = tuple(sorted(live_externals, key=lambda n: n.node_id))
        kept_ext_ids = {n.node_id for n in kept_externals}
        if kept_ext_ids != live_ext_ids:
            # The external cap dropped some of the externals ``edges`` above
            # was built against — drop the now-dangling edges pointing at
            # them so no edge survives whose target was capped away.
            edges = [
                e
                for e in edges
                if e.target in kept_ast_ids or e.target in kept_ext_ids
            ]
        payload_ast_ids = kept_ast_ids | kept_ext_ids

        # ── Entity layer ─────────────────────────────────────────────────
        entity_nodes = store.query_entities(slug)
        entity_ids = {e.id for e in entity_nodes}
        entity_rels: list[EntityRelation] = []
        entity_cross: list[tuple[str, str]] = []
        stranded: list[EntityRelation] = []
        for rel in store.list_entity_edges(slug):
            if rel.target_id in entity_ids and rel.source_id in entity_ids:
                # entity ↔ entity → RELATES (verb in label)
                entity_rels.append(rel)
            elif rel.source_id in entity_ids and rel.target_id in payload_ast_ids:
                # entity → AST node → cross-layer ANCHORS
                entity_cross.append((rel.source_id, rel.target_id))
            elif rel.source_id in entity_ids:
                # Anchored at an AST node that is not in this payload. Under a
                # commit scope that is usually not a dangling anchor but a
                # SUPERSEDED one — see the repair below.
                stranded.append(rel)
            # else: dangling (target absent from this payload) → dropped
        entity_cross.extend(
            cls._reanchor_entity_edges(store, slug, stranded, nodes, payload_ast_ids)
        )

        # ── Memory layer ─────────────────────────────────────────────────
        memory_nodes = store.query_memory(slug)
        memory_ids = {n.node_id for n in memory_nodes}
        mem_rels: list[MemoryEdge] = []
        mem_anchor_edges: list[MemoryEdge] = []
        for me in store.list_memory_edges(slug, include_invalidated=False):
            if me.type == "RELATES" and me.source in memory_ids:
                mem_rels.append(me)
            elif me.type == "ANCHORS" and me.source in memory_ids:
                mem_anchor_edges.append(me)

        # Batch-resolve memory ANCHORS targets to real node ids (one pass each).
        code_keys = [e.target for e in mem_anchor_edges if not e.target.startswith("entity:")]
        ent_keys = [e.target for e in mem_anchor_edges if e.target.startswith("entity:")]
        code_map = CodeStructureProvider(store).resolve_many(slug, code_keys)
        ent_map = EntityAnchorResolver(store).resolve_many(slug, ent_keys)
        memory_cross: list[tuple[str, str]] = []
        for me in mem_anchor_edges:
            if me.target.startswith("entity:"):
                ent = ent_map.get(me.target)
                tid = ent.id if ent is not None and ent.id in entity_ids else None
            else:
                node = code_map.get(me.target)
                tid = node.node_id if node is not None and node.node_id in payload_ast_ids else None
            if tid is not None:
                memory_cross.append((me.source, tid))

        # ── Directory scaffold (hierarchy wire mode only) ────────────────
        # Built over the KEPT AST layer so folder nodes only scaffold files
        # actually in the payload; the symbol→container parentId reads off the
        # same filtered CONTAINS edges the wire emits.
        folder_tree = (
            FolderTree.build(slug, nodes, edges, external_nodes=kept_externals)
            if hierarchy
            else None
        )

        return cls(
            slug=slug,
            nodes=tuple(nodes),
            external_nodes=kept_externals,
            edges=tuple(edges),
            entity_nodes=tuple(entity_nodes),
            entity_edges=tuple(entity_rels),
            memory_nodes=tuple(memory_nodes),
            memory_edges=tuple(mem_rels),
            cross_edges=tuple(entity_cross + memory_cross),
            total_nodes=total_nodes,
            total_edges=total_edges,
            total_externals=len(live_ext_ids),
            folder_tree=folder_tree,
        )

    @staticmethod
    def _resolve_ast_edges(
        slug: str,
        all_edges: list[GraphEdge],
        by_name: dict[str, str],
        node_ids: set[str],
    ) -> tuple[list[GraphEdge], list[GraphNode]]:
        """Re-point cross-file edges to real nodes; synthesize External nodes.

        An edge with a ``target_name`` (cross-file IMPORTS/CALLS/EXTENDS) is
        re-pointed at the in-repo node of that name when one exists; otherwise
        every reference to the same name converges on one synthesized
        ``External`` node (deterministic id over the name). Edges WITHOUT a
        ``target_name`` (CONTAINS etc.) pass through only when both endpoints are
        real nodes — orphan hygiene unchanged from the prior behaviour.
        """
        resolved: list[GraphEdge] = []
        externals: dict[str, GraphNode] = {}
        for e in all_edges:
            if e.target_name is None:
                # In-repo structural edge; keep only if both endpoints are real.
                if e.source in node_ids and e.target in node_ids:
                    resolved.append(e)
                continue
            real = by_name.get(e.target_name)
            if real is not None and real != e.source:
                resolved.append(e.model_copy(update={"target": real}))
            else:
                ext_id = _stable_id(slug, "External", e.target_name, "<external>", 0)
                externals.setdefault(
                    ext_id,
                    ExternalNode(
                        slug=slug,
                        node_id=ext_id,
                        name=e.target_name,
                        file="",
                        range=(0, 0),
                        docstring=None,
                    ),
                )
                resolved.append(e.model_copy(update={"target": ext_id}))
        return resolved, list(externals.values())

    # ── Derived state ───────────────────────────────────────────────────

    @property
    def node_count(self) -> int:
        """AST nodes in this view (real + External), drives the truncation banner."""
        return len(self.nodes) + len(self.external_nodes)

    @property
    def edge_count(self) -> int:
        """AST-layer edges in this view (post-filter)."""
        return len(self.edges)

    @property
    def kinds(self) -> dict[str, int]:
        """Per-kind histogram over EVERY node this view puts on the wire.

        Counts the Entity, Memory and Folder classes alongside the AST kinds
        because the FE derives its whole legend from this one map: it lists a
        kind only when the tally is positive, and sums the same map to decide
        which LAYERS to offer. Counting the AST layer alone therefore did not
        merely under-report — it drew those three classes on the canvas with no
        legend entry and no toggle, so a user could neither identify them nor
        turn them off.

        Folder is counted only in the hierarchy wire mode, which is the only
        mode that emits Folder nodes.
        """
        counts = (
            Counter(n.type for n in self.nodes)
            + Counter(n.type for n in self.external_nodes)
            + Counter({"Entity": len(self.entity_nodes)} if self.entity_nodes else {})
            + Counter({"Memory": len(self.memory_nodes)} if self.memory_nodes else {})
        )
        if self.folder_tree is not None and self.folder_tree.folder_nodes:
            counts += Counter({"Folder": len(self.folder_tree.folder_nodes)})
        return dict(counts)

    # ── Serialisation ───────────────────────────────────────────────────

    def to_wire(self) -> dict[str, Any]:
        """Wire shape: ``{slug, nodes, edges, stats}`` over all three layers.

        Each node/edge is Cytoscape-ready (``{data: {...}}``) and carries a
        ``layer`` tag so the FE can style/filter per layer. The FE hands the
        arrays straight to ``cy.add(elements)`` with no intermediate transform.

        When the view was built with ``hierarchy=True`` the directory scaffold
        is merged in: synthesised ``Folder`` nodes + their ``CONTAINS`` edges
        are appended, and every emitted node gains a ``parentId`` /
        ``folderPath`` (``folderCount`` lands in ``stats``). With hierarchy off
        the ``folder_tree`` is ``None`` and the payload is byte-identical to the
        default mode.
        """
        tree = self.folder_tree
        nodes = (
            [self._node_to_wire(n, "ast", tree) for n in self.nodes]
            + [self._node_to_wire(n, "ast", tree) for n in self.external_nodes]
            + [self._entity_node_to_wire(e, tree) for e in self.entity_nodes]
            + [self._memory_node_to_wire(m, tree) for m in self.memory_nodes]
        )
        edges = (
            [self._edge_to_wire(e, "ast") for e in self.edges]
            + [self._entity_edge_to_wire(e) for e in self.entity_edges]
            + [self._memory_edge_to_wire(e) for e in self.memory_edges]
            + [self._cross_edge_to_wire(s, t) for s, t in self.cross_edges]
        )
        if tree is not None:
            # Folder nodes carry their own parentId/folderPath off the tree.
            nodes += [self._node_to_wire(f, "ast", tree) for f in tree.folder_nodes]
            edges += [self._edge_to_wire(e, "ast") for e in tree.folder_edges]
        stats: dict[str, Any] = {
            # AST-only counters — the flat shape wire consumers read.
            "nodeCount": self.node_count,
            "edgeCount": self.edge_count,
            "kinds": self.kinds,
            # FULL multiplex counts ("M" in the FE "showing N of M" banner).
            # Only the AST layer is ever capped, so entity + memory + the
            # view-only External nodes contribute their in-view counts; the
            # pre-cap AST total is ``self.total_nodes``. Uncapped ⇒ M == N.
            "totalNodes": (
                self.total_nodes
                + self.total_externals
                + len(self.entity_nodes)
                + len(self.memory_nodes)
            ),
            "totalEdges": (
                self.total_edges
                + len(self.entity_edges)
                + len(self.memory_edges)
                + len(self.cross_edges)
            ),
            # ``truncated`` answers "did the cap drop anything the payload
            # would otherwise carry", and the cap governs BOTH populations —
            # so both are compared against their own pre-cap total. The two
            # counts stay separate rather than summed: summing lets a surplus
            # on one side mask a shortfall on the other, which is how a
            # payload that had lost a quarter of its nodes still reported
            # itself complete. Entity + memory layers are never capped, and
            # an edge dropped by orphan hygiene is not truncation.
            "truncated": (
                len(self.nodes) < self.total_nodes
                or len(self.external_nodes) < self.total_externals
            ),
            "perLayer": {
                "ast": len(self.nodes) + len(self.external_nodes),
                "entity": len(self.entity_nodes),
                "memory": len(self.memory_nodes),
            },
        }
        # ``folderCount`` only in hierarchy mode — the default wire stays
        # byte-identical (the SCG / Agentic Search reuse path is undisturbed).
        if tree is not None:
            stats["folderCount"] = len(tree.folder_nodes)
        return {
            "slug": self.slug,
            "nodes": nodes,
            "edges": edges,
            "stats": stats,
        }

    # ── Static helpers (per-record formatters) ──────────────────────────

    @staticmethod
    def _stamp_hierarchy(
        data: dict[str, Any], node_id: str, tree: FolderTree | None
    ) -> dict[str, Any]:
        """Add ``parentId``/``folderPath`` to a node's data when in hierarchy mode.

        No-op (identity) off-mode so the default wire keeps NEITHER key and
        stays byte-identical. ``folderPath`` is set only for Folder + File node
        ids (per the tree's map); every other node carries an explicit ``None``.
        """
        if tree is None:
            return data
        data["parentId"] = tree.parent_of(node_id)
        data["folderPath"] = tree.folder_path_of(node_id)
        return data

    @classmethod
    def _node_to_wire(
        cls, n: GraphNode, layer: str, tree: FolderTree | None = None
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": n.node_id,
            "label": n.name,
            "kind": n.type,
            "layer": layer,
            "file": n.file,
            "range": list(n.range),
            "docstring": n.docstring or "",
        }
        if n.subkind is not None:
            data["subkind"] = n.subkind
        return {"data": cls._stamp_hierarchy(data, n.node_id, tree)}

    @classmethod
    def _entity_node_to_wire(
        cls, e: Entity, tree: FolderTree | None = None
    ) -> dict[str, Any]:
        return {
            "data": cls._stamp_hierarchy(
                {
                    "id": e.id,
                    "label": e.name,
                    "kind": "Entity",
                    "layer": "entity",
                    "entityType": e.type,
                    "labels": list(e.labels),
                },
                e.id,
                tree,
            ),
        }

    @classmethod
    def _memory_node_to_wire(
        cls, m: MemoryNode, tree: FolderTree | None = None
    ) -> dict[str, Any]:
        content = m.content.strip()
        label = content[:_MEMORY_LABEL_CHARS]
        if len(content) > _MEMORY_LABEL_CHARS:
            label += "…"
        return {
            "data": cls._stamp_hierarchy(
                {
                    "id": m.node_id,
                    "label": label,
                    "kind": "Memory",
                    "layer": "memory",
                    "snippet": content[:_MEMORY_SNIPPET_CHARS],
                    "labels": list(m.labels),
                },
                m.node_id,
                tree,
            ),
        }

    @staticmethod
    def _edge_to_wire(e: GraphEdge, layer: str) -> dict[str, Any]:
        return {
            "data": {
                "id": f"{e.source}__{e.type}__{e.target}",
                "source": e.source,
                "target": e.target,
                "kind": e.type,
                "layer": layer,
            },
        }

    @staticmethod
    def _entity_edge_to_wire(e: EntityRelation) -> dict[str, Any]:
        # Open-vocab relation verb rides ``label`` (NOT ``kind``) so the FE's
        # closed kind-union stays closed — ``RELATES`` is the only entity kind.
        return {
            "data": {
                "id": f"{e.source_id}__RELATES__{e.target_id}__{e.type}",
                "source": e.source_id,
                "target": e.target_id,
                "kind": "RELATES",
                "layer": "entity",
                "label": e.type,
            },
        }

    @staticmethod
    def _memory_edge_to_wire(e: MemoryEdge) -> dict[str, Any]:
        return {
            "data": {
                "id": f"{e.source}__RELATES__{e.target}",
                "source": e.source,
                "target": e.target,
                "kind": "RELATES",
                "layer": "memory",
            },
        }

    @staticmethod
    def _cross_edge_to_wire(source: str, target: str) -> dict[str, Any]:
        return {
            "data": {
                "id": f"{source}__ANCHORS__{target}",
                "source": source,
                "target": target,
                "kind": "ANCHORS",
                "layer": "cross",
            },
        }


def _extract_docstring(node) -> str | None:
    """Pull the first string literal inside a function/class body.

    In the tree-sitter Python grammar, docstrings appear as ``string`` nodes
    that are direct children of the ``block`` node (not wrapped in
    ``expression_statement``). We also handle the ``expression_statement``
    wrapping as a fallback for compatibility.
    """
    body = next((c for c in node.children if c.type == "block"), None)
    if not body:
        return None
    for child in body.children:
        # Direct string child (tree-sitter 0.25 Python grammar)
        if child.type == "string":
            raw = child.text.decode()
            # Strip surrounding triple/single/double quotes and whitespace.
            return raw.strip("\"'").strip()
        # Fallback: expression_statement wrapping a string
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = sub.text.decode()
                    return raw.strip("\"'").strip()
    return None
