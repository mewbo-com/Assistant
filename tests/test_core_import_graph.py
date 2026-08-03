"""Structural invariants for ``mewbo_core``'s internal import graph.

This is NOT a feature test. It pins the one property the package reorganization
must preserve at every step: **the runtime import graph is a DAG**. Nothing here
asserts what any module does — only the shape of how the modules reach each
other.

Why a test rather than a convention. The graph is already acyclic, but only
because a handful of modules deliberately break their cycle-closing edge, with
``if TYPE_CHECKING:`` or with a function-local import. Those guards look like
stylistic noise in a diff: promoting one to a plain module-top import is a
one-line change that reads as a tidy-up and silently re-closes a cycle. The
failure mode is not an ``ImportError`` at the edit site; it is an
``ImportError`` in whichever consumer happens to import the two modules in the
unlucky order, which may be a different package entirely. Which guards are
load-bearing is derived from the source rather than asserted from memory — see
``test_declared_breakers_are_exactly_the_load_bearing_guards``.

The classification is the load-bearing part, and a first attempt at it got the
answer wrong — it reported two cycles that do not exist, because it tagged
statements by walking flat and asking "is this node inside some
``if TYPE_CHECKING:`` body", which mis-answers for anything nested. The cure is
:meth:`CoreImportGraph._classify`: a recursive descent that carries the kind
DOWN from enclosing scopes, so an import's kind is decided by where it sits in
the tree and by nothing else. ``test_import_kinds_are_decided_by_enclosing_scope``
pins that behaviour against hand-written source.

Everything is parsed, never imported. Two reasons: importing a core module
executes real import-time side effects (``get_logger`` reaches the config
loader; ``mewbo_core.llm.llm`` pulls in LiteLLM, which reads a ``.env`` off disk),
and at least one file under ``builtin_plugins/`` is a Streamlit example script
that calls ``sys.exit()`` in its module body.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = REPO_ROOT / "packages" / "mewbo_core" / "src" / "mewbo_core"
PACKAGE = "mewbo_core"

ROOT_GROUP = "ROOT"

# Every guarded edge that would close a runtime cycle if it were promoted to a
# module-top import, as (importer, imported, kind).
#
# This list is NOT taken on trust:
# ``test_declared_breakers_are_exactly_the_load_bearing_guards`` recomputes it
# from the source by promoting each guarded edge in turn and asking whether the
# graph goes cyclic. Writing it down as well is what turns a five-module
# strongly-connected component in a failure message into a single named import.
#
# Stated as triples rather than ``file:line`` so an unrelated edit above one of
# them does not fail the test; the line numbers go in the failure message.
#
# The two kinds are not interchangeable. A ``type_checking`` guard never
# executes. A ``deferred`` (function-local) one DOES execute, at call time, and
# is safe only because the imported module's import-time side effects cannot
# re-enter the caller — a per-site argument, not a general licence. The comment
# at the ``build_chat_model`` call site in ``classes.py`` is what one of those
# arguments has to look like.
DECLARED_CYCLE_BREAKERS: frozenset[tuple[str, str, str]] = frozenset(
    {
        # --- type-only back-references within the delegation subsystem -------
        # Both partners import the hypervisor's types at runtime, so the
        # hypervisor's references back to them can only be type-only.
        ("mewbo_core.agents.hypervisor", "mewbo_core.agents.attestation", "type_checking"),
        ("mewbo_core.agents.hypervisor", "mewbo_core.agents.spawn_agent", "type_checking"),
        # --- config's own upward imports ------------------------------------
        # config is imported by nearly all of core, so it may not import a
        # consumer at module top.
        #
        # Four guards in the source are deliberately NOT listed here —
        # `llm_resilience` and `verification` each deferring `config`,
        # `llm_resilience` type-guarding `tool_registry`, and `verification`
        # deferring `hooks`. `contracts/defaults.py` owns config's default
        # constants, which severs `config -> llm_resilience` and
        # `config -> verification`, so none of the four holds the graph
        # acyclic. Listing them would be a claim the derivation below
        # contradicts. They stay in the source: removing them is a behaviour
        # change, not a cleanup.
        ("mewbo_core.config", "mewbo_core.common", "deferred"),
        ("mewbo_core.config", "mewbo_core.tooling.plugins", "deferred"),
        ("mewbo_core.common", "mewbo_core.llm.prompt_registry", "deferred"),
        # --- the lazy-Mongo store pattern -----------------------------------
        # Each Mongo driver subclasses the base store, so the base can only
        # reach its driver at call time. That deferral is also what keeps a
        # `storage.driver=json` deployment from needing pymongo at all, which
        # is why these are guards worth naming rather than incidental.
        ("mewbo_core.secrets.key_store", "mewbo_core.secrets.key_store_mongo", "deferred"),
        ("mewbo_core.session.session_store", "mewbo_core.session.session_store_mongo", "deferred"),
        (
            "mewbo_core.workspaces.repository_store",
            "mewbo_core.workspaces.repository_store_mongo",
            "deferred",
        ),
        (
            "mewbo_core.system_instructions.store",
            "mewbo_core.system_instructions.store_mongo",
            "deferred",
        ),
        ("mewbo_core.triggers.store", "mewbo_core.triggers.store_mongo", "deferred"),
    }
)

# The package-level cycles that exist. Now empty: the extraction is complete and
# the package graph is a clean DAG.
#
# This constant was not always empty, and the history is the reason it is stated
# as an exact set rather than a bound. Mid-extraction the graph was genuinely
# cyclic, always in the same shape: a module destined for some subpackage was
# still sitting at ROOT, importing a package that imported ROOT back. Two such
# components existed — `orchestrator` against `system_instructions`, and eleven
# root modules against `llm/`. Both dissolved with no edit to either side, purely
# by the modules reaching their assigned packages.
#
# Keeping it exact is what makes both directions loud. A NEW package cycle fails
# here, which is the point. But so does this list going stale: if a future change
# dissolves or introduces an edge and nobody updates the constant, the test
# reports the discrepancy instead of absorbing it. An assertion widened to
# "cycles are fine" would have stopped detecting the thing worth detecting at
# exactly the moment the reorganization made it detectable.
KNOWN_PACKAGE_CYCLES: list[list[str]] = []


class CoreImportGraph:
    """The internal import graph of ``mewbo_core``, classified by execution kind.

    State: the parsed modules and, for each, the ``mewbo_core`` imports it
    declares tagged ``runtime`` / ``type_checking`` / ``deferred``. Behaviour:
    projecting that to a module-level or package-level edge set and finding the
    cycles in either.

    A *runtime* import is one that executes when the module is imported — the
    module body, a class body at module scope, a decorator, a default-argument
    expression. Those are the only edges that can deadlock an import. The other
    two kinds are edges to mypy and to the reader, never to the interpreter.
    """

    def __init__(self, modules: dict[str, Path]) -> None:
        """Parse every module in *modules* and classify its internal imports."""
        self.modules = modules
        # (importer, imported, kind, lineno)
        self.imports: list[tuple[str, str, str, int]] = []
        for name, path in sorted(modules.items()):
            for target, kind, lineno in self._classify(path, name):
                self.imports.append((name, target, kind, lineno))

    @classmethod
    def build(cls, root: Path = CORE_SRC) -> CoreImportGraph:
        """Build the graph for the package rooted at *root*."""
        return cls({cls._module_name(p, root): p for p in sorted(root.rglob("*.py"))})

    @staticmethod
    def _module_name(path: Path, root: Path) -> str:
        """Dotted module name for *path* (``__init__.py`` names its package)."""
        parts = list(path.relative_to(root).parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].removesuffix(".py")
        return ".".join([PACKAGE, *parts])

    @staticmethod
    def _is_type_checking(test: ast.expr) -> bool:
        """True for ``TYPE_CHECKING`` and ``typing.TYPE_CHECKING`` tests."""
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    @classmethod
    def _classify(cls, path: Path, self_name: str) -> list[tuple[str, str, int]]:
        """Return ``(imported, kind, lineno)`` for each ``mewbo_core`` import.

        Recursive descent, carrying the kind down from enclosing scopes. An
        import's kind is a property of WHERE THE STATEMENT SITS, so it is
        decided once, on the way down, and never re-derived by asking whether
        some ancestor happens to be a guard — the mistake that produced two
        phantom cycles the first time this graph was measured.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[tuple[str, str, int]] = []

        def imported_modules(node: ast.stmt) -> list[str]:
            if isinstance(node, ast.Import):
                return [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom):
                if node.level:  # `from . import x` / `from .x import y`
                    base = self_name.rsplit(".", node.level)[0]
                    return [f"{base}.{node.module}" if node.module else base]
                return [node.module] if node.module else []
            return []

        def walk(node: ast.AST, kind: str) -> None:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name in imported_modules(node):
                    if name == PACKAGE or name.startswith(f"{PACKAGE}."):
                        found.append((name, kind, node.lineno))
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # A function BODY runs at call time. Its decorators and default
                # expressions do not — they are evaluated where the `def` sits,
                # so they keep the enclosing kind.
                for expr in [
                    *getattr(node, "decorator_list", []),
                    *node.args.defaults,
                    *[d for d in node.args.kw_defaults if d is not None],
                ]:
                    walk(expr, kind)
                for stmt in ast.iter_child_nodes(node):
                    if stmt is node.args or stmt in getattr(node, "decorator_list", []):
                        continue
                    walk(stmt, "deferred")
                return
            if isinstance(node, ast.If) and cls._is_type_checking(node.test):
                # Only the TRUE branch is type-only; `else:` is what actually
                # runs. A guard nested inside a function body stays deferred —
                # never executed either way, and `deferred` is the stronger
                # statement about the interpreter.
                for stmt in node.body:
                    walk(stmt, "type_checking" if kind == "runtime" else kind)
                for stmt in node.orelse:
                    walk(stmt, kind)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, kind)

        for stmt in tree.body:
            walk(stmt, "runtime")
        return found

    def edges(self, *, kinds: tuple[str, ...] = ("runtime",)) -> dict[str, set[str]]:
        """Module-level edge set restricted to *kinds*, self-edges dropped.

        Targets that name no module in this graph are DROPPED here and reported
        by :meth:`unresolved` — see that method for why the split matters.
        """
        out: dict[str, set[str]] = {name: set() for name in self.modules}
        for importer, imported, kind, _ in self.imports:
            if kind in kinds and imported in self.modules and imported != importer:
                out[importer].add(imported)
        return out

    def unresolved(self) -> list[str]:
        """Internal imports naming a module that does not exist.

        Dropping an unresolvable target from the edge set is the right thing for
        cycle-finding — but doing it SILENTLY once cost a real bug. A module
        moved into a subpackage left one `from .old_name import X` behind; the
        relative form still resolved to a `mewbo_core.*` name, that name matched
        nothing, the edge vanished, and the DAG assertion went green over a tree
        that could not be imported at all. The suite only caught it three files
        later, at a conftest.

        So the drop is still a drop, and it is also an assertion.
        """
        return [
            f"{self.modules[importer].relative_to(REPO_ROOT)}:{lineno} -> {imported}"
            for importer, imported, _, lineno in self.imports
            if imported not in self.modules
        ]

    def packages(self, root: Path = CORE_SRC) -> dict[str, str]:
        """Module → subpackage name, or ``ROOT`` for a module at the package root.

        Derived from the file's DIRECTORY, not from its dotted name. A
        subpackage's ``__init__.py`` is ``mewbo_core.triggers`` — one dot, same
        as a root module — so a name-based rule files the package's own
        ``__init__`` under ROOT and then reports the package importing itself as
        a ROOT ↔ package cycle. That is a phantom, and it was produced by the
        first version of this helper.
        """
        out: dict[str, str] = {}
        for name, path in self.modules.items():
            parts = path.relative_to(root).parts
            out[name] = parts[0] if len(parts) > 1 else ROOT_GROUP
        return out

    def package_edges(
        self, group_of: dict[str, str], *, kinds: tuple[str, ...] = ("runtime",)
    ) -> dict[str, set[str]]:
        """Project the module edges onto packages via *group_of*, dropping self-edges.

        This is the seam each later phase of the reorganization extends: hand it
        the module→package assignment the phase creates and it answers whether
        that grouping keeps the package graph acyclic.
        """
        out: dict[str, set[str]] = {group: set() for group in group_of.values()}
        for importer, targets in self.edges(kinds=kinds).items():
            src = group_of[importer]
            for target in targets:
                dst = group_of[target]
                if dst != src:
                    out.setdefault(src, set()).add(dst)
        return out

    @staticmethod
    def cycles(edges: dict[str, set[str]]) -> list[list[str]]:
        """Every strongly-connected component of *edges* with more than one node.

        Iterative Tarjan — the module graph is deep enough that the recursive
        form needs its own recursion-limit bump, and a test that has to raise
        the interpreter's limit to answer a question is one edit away from a
        confusing ``RecursionError`` instead of a clear assertion failure.
        """
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        counter = 0
        found: list[list[str]] = []

        for start in edges:
            if start in index:
                continue
            work: list[tuple[str, list[str]]] = [(start, sorted(edges.get(start, ())))]
            index[start] = low[start] = counter
            counter += 1
            stack.append(start)
            on_stack.add(start)
            while work:
                node, pending = work[-1]
                if pending:
                    nxt = pending.pop()
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, sorted(edges.get(nxt, ()))))
                    elif nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: list[str] = []
                    while True:
                        popped = stack.pop()
                        on_stack.discard(popped)
                        component.append(popped)
                        if popped == node:
                            break
                    if len(component) > 1:
                        found.append(sorted(component))
        return found

    def describe(self, importer: str, imported: str) -> str:
        """``file:line`` for every statement importing *imported* from *importer*."""
        sites = [
            f"{self.modules[importer].relative_to(REPO_ROOT)}:{lineno} [{kind}]"
            for src, dst, kind, lineno in self.imports
            if src == importer and dst == imported
        ]
        return ", ".join(sites) or "<no such import>"


@pytest.fixture(scope="module")
def graph() -> CoreImportGraph:
    """The parsed import graph of the whole ``mewbo_core`` package."""
    return CoreImportGraph.build()


def test_core_has_no_runtime_import_cycles(graph: CoreImportGraph) -> None:
    """`mewbo_core`'s runtime import graph is a DAG — the reorganization tripwire.

    This is the property every phase of the package reorganization must
    preserve. It holds today, and the whole point of pinning it BEFORE any
    module moves is that a cycle found afterwards is otherwise indistinguishable
    from one the moves caused.

    A cycle here does not usually announce itself at the edit. Python tolerates
    a cycle whenever the first module to be imported happens to finish binding
    the names the second one needs before handing control over, so the same
    cyclic pair works from one entry point and raises
    ``ImportError: cannot import name ... (most likely due to a circular
    import)`` from another. That makes it a latent failure keyed to import
    ORDER, which is exactly what moving 57 modules perturbs.
    """
    found = graph.cycles(graph.edges())
    assert found == [], (
        "mewbo_core gained a runtime import cycle:\n"
        + "\n".join(f"  {' -> '.join(component)}" for component in found)
    )


def test_every_internal_import_names_a_real_module(graph: CoreImportGraph) -> None:
    """No `mewbo_core.*` import points at a module that does not exist.

    A REGRESSION test, and the one that would have caught the reorganization's
    first real breakage a phase earlier. Moving six modules into `contracts/`
    rewrote every absolute `mewbo_core.<name>` reference, but
    `transcript_timeline.py` reached one of them through a RELATIVE
    `from .run_error import ...` — the only relative import in the package, and
    invisible to a search for the absolute form.

    The DAG test stayed green over that, because an edge to a nonexistent
    module is dropped before the cycle search ever sees it, and a missing edge
    can only make a graph MORE acyclic. The package was unimportable and the
    structural test said the structure was fine.

    So: resolvability is asserted separately from acyclicity, because a
    reference to nothing is a different failure from a reference in a circle,
    and only one of them is visible in the shape of the graph.
    """
    assert graph.unresolved() == [], (
        "these imports name a module that does not exist:\n"
        + "\n".join(f"  {entry}" for entry in graph.unresolved())
    )


def test_the_guards_are_what_makes_the_graph_acyclic(graph: CoreImportGraph) -> None:
    """Treating every import as runtime DOES produce cycles — the guards earn their keep.

    Non-vacuity for the test above, and the reason it is worth having. An
    "acyclic" result computed over an empty or near-empty edge set would assert
    nothing, and a reader who never sees the counterfactual has no way to tell
    whether the ``TYPE_CHECKING`` and function-local guards are load-bearing or
    merely decorative. They are load-bearing: fold them back in and the graph
    collapses into strongly-connected components.

    Both halves are asserted — that the scanner sees a substantial runtime
    graph at all, and that the naive reading of the same source is cyclic.
    """
    runtime = graph.edges()
    assert len(graph.modules) > 50, f"the scanner found only {len(graph.modules)} modules"
    assert sum(len(t) for t in runtime.values()) > 100, "the runtime edge set is implausibly small"

    naive = graph.edges(kinds=("runtime", "type_checking", "deferred"))
    assert graph.cycles(naive), (
        "ignoring the import guards produced an acyclic graph — either the guards "
        "have been removed or the classifier is not distinguishing them"
    )


@pytest.mark.parametrize(
    ("importer", "imported", "kind"),
    sorted(DECLARED_CYCLE_BREAKERS),
)
def test_declared_cycle_breakers_stay_guarded(
    graph: CoreImportGraph, importer: str, imported: str, kind: str
) -> None:
    """Each edge that would close a cycle is still guarded, and guarded the same way.

    ``test_core_has_no_runtime_import_cycles`` says the graph is acyclic;
    these say WHY, one edge at a time, so a failure names the single import
    that has to change rather than handing back a component of five modules.

    The two kinds are not interchangeable. A ``TYPE_CHECKING`` guard never
    executes, so it is unconditionally safe. A function-local import does
    execute, and is safe only because the imported module's import-time side
    effects cannot re-enter the caller — ``tool_registry`` reads config values
    at call time for exactly that reason. Downgrading a ``TYPE_CHECKING`` guard
    to a function-local one is therefore a real change, not a refactor, which is
    why the kind is asserted and not just the absence of a runtime edge.
    """
    kinds = {
        found_kind
        for src, dst, found_kind, _ in graph.imports
        if src == importer and dst == imported
    }
    assert kinds, (
        f"{importer} no longer imports {imported} at all — if the dependency is "
        "genuinely gone, drop this entry from DECLARED_CYCLE_BREAKERS"
    )
    assert "runtime" not in kinds, (
        f"{importer} now imports {imported} at module top: "
        f"{graph.describe(importer, imported)} — this closes a cycle"
    )
    assert kind in kinds, (
        f"{importer} -> {imported} changed guard style, expected {kind!r}, got "
        f"{sorted(kinds)}: {graph.describe(importer, imported)}"
    )


def test_declared_breakers_are_exactly_the_load_bearing_guards(graph: CoreImportGraph) -> None:
    """``DECLARED_CYCLE_BREAKERS`` is recomputed from source, never taken on trust.

    A hand-maintained list of "the imports that must stay guarded" is a claim
    about how other code behaves, and this one was wrong when written from an
    earlier analysis: it named five function-local ``tool_registry -> config``
    imports as cycle-breakers, but `tool_registry` imports `config` at module
    top anyway, so those five break nothing. It also missed nine guards that
    genuinely are load-bearing, including the four lazy-Mongo store imports.

    So the set is derived: promote each guarded edge to a runtime edge, one at a
    time, and keep the ones that make the graph cyclic. Asserting the derived
    set EQUALS the declared one catches drift in both directions — a guard that
    stops mattering (so the comment explaining it is now misleading) as loudly
    as a new one that nobody wrote down.
    """
    runtime = graph.edges()
    guarded = {
        (src, dst, kind)
        for src, dst, kind, _ in graph.imports
        if kind != "runtime" and dst in graph.modules and dst != src
    }
    load_bearing = set()
    for src, dst, kind in guarded:
        trial = {node: set(targets) for node, targets in runtime.items()}
        trial[src].add(dst)
        if graph.cycles(trial):
            load_bearing.add((src, dst, kind))

    assert load_bearing == DECLARED_CYCLE_BREAKERS, (
        "the set of guards holding the graph acyclic changed.\n"
        f"  newly load-bearing: {sorted(load_bearing - DECLARED_CYCLE_BREAKERS)}\n"
        f"  no longer needed:   {sorted(DECLARED_CYCLE_BREAKERS - load_bearing)}"
    )


def test_import_kinds_are_decided_by_enclosing_scope(tmp_path: Path) -> None:
    """The classifier tags each import by its own position, including when nested.

    A REGRESSION test for the bug that made the first measurement of this graph
    report two cycles that do not exist. The broken version answered "is this
    import inside a ``TYPE_CHECKING`` block" by looking at block membership
    rather than by carrying the kind down the tree, so it mis-tagged anything
    one level in.

    Every case below is one the real package contains, and the ``else:`` branch
    is the sharp one: an import there is a plain runtime import that happens to
    sit inside a statement whose other branch is type-only, so a classifier
    keyed on the enclosing ``if`` alone would exempt a genuinely cycle-closing
    edge.
    """
    source = textwrap.dedent(
        '''
        """Fixture module covering every classification case."""
        from typing import TYPE_CHECKING

        from mewbo_core.plain import Runtime

        if TYPE_CHECKING:
            from mewbo_core.guarded import Typed
            if True:
                from mewbo_core.nested_guarded import AlsoTyped
        else:
            from mewbo_core.else_branch import ReallyRuntime

        class Holder:
            from mewbo_core.class_body import AtClassScope

        def caller(default=None):
            from mewbo_core.local import Deferred
            if TYPE_CHECKING:
                from mewbo_core.local_guarded import StillDeferred

        async def async_caller():
            from mewbo_core.async_local import AlsoDeferred
        '''
    )
    path = tmp_path / "fixture.py"
    path.write_text(source, encoding="utf-8")

    classified = {
        target: kind
        for target, kind, _ in CoreImportGraph._classify(path, "mewbo_core.fixture")
    }

    assert classified == {
        "mewbo_core.plain": "runtime",
        "mewbo_core.guarded": "type_checking",
        "mewbo_core.nested_guarded": "type_checking",
        "mewbo_core.else_branch": "runtime",
        "mewbo_core.class_body": "runtime",
        "mewbo_core.local": "deferred",
        "mewbo_core.local_guarded": "deferred",
        "mewbo_core.async_local": "deferred",
    }


def test_package_projection_reports_a_cycle_a_grouping_would_create() -> None:
    """The package-level projection detects a cycle the module graph does not have.

    Each later phase of the reorganization asserts its new grouping keeps the
    PACKAGE graph acyclic, and that claim is only worth making if the projector
    can actually fail. Two module DAGs are projected here: one grouping that
    splits a chain across packages in dependency order, and one that
    interleaves it. The second is the shape the reorganization has to avoid —
    ``a -> b -> c`` is a fine module DAG, but filing ``a`` and ``c`` in one
    package and ``b`` in another makes the two packages import each other.
    """
    graph = CoreImportGraph({})
    graph.modules = {
        "mewbo_core.a": Path("a"),
        "mewbo_core.b": Path("b"),
        "mewbo_core.c": Path("c"),
    }
    graph.imports = [
        ("mewbo_core.a", "mewbo_core.b", "runtime", 1),
        ("mewbo_core.b", "mewbo_core.c", "runtime", 1),
    ]

    layered = {"mewbo_core.a": "top", "mewbo_core.b": "mid", "mewbo_core.c": "base"}
    assert graph.cycles(graph.package_edges(layered)) == []

    interleaved = {"mewbo_core.a": "one", "mewbo_core.b": "two", "mewbo_core.c": "one"}
    assert graph.cycles(graph.package_edges(interleaved)) == [["one", "two"]]


def test_todays_package_graph_has_exactly_the_known_cycle(graph: CoreImportGraph) -> None:
    """The package-level baseline, pinned as an EXACT set while extraction is in flight.

    The package graph is NOT acyclic mid-reorganization, and recording that
    honestly is the whole value of measuring it. A module that has not yet been
    extracted still sits at ROOT importing a package that imports ROOT back —
    `orchestrator` → `system_instructions`, eleven root modules → `llm/`. A
    phase that asserted a clean package DAG would fail on inherited state and
    read as "the reorganization broke the package graph".

    Equally, a phase that quietly widened the assertion to "cycles are fine"
    would stop detecting the thing worth detecting. So the baseline is exact,
    and it is expected to be EDITED by each phase — downward. Every member of
    the recorded component is a module with a pending move; when the last one
    lands this list is empty. Two failures are therefore both real: a component
    that acquires a member with no scheduled move (the grouping is wrong), and
    a component that shrinks without the constant being updated (a result to
    notice rather than absorb).
    """
    found = graph.cycles(graph.package_edges(graph.packages()))
    assert found == KNOWN_PACKAGE_CYCLES, (
        f"package-level cycles changed: expected {KNOWN_PACKAGE_CYCLES}, found {found}"
    )
