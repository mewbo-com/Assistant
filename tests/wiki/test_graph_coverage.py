"""Per-language construct-family coverage for the tree-sitter extractor.

These tests exist because a 19% Python symbol gap survived the rest of the
suite. `test_graph.py` asserts that *some* class, function and method come out
of a small fixture, which every one of the missing families also satisfies —
the gap was invisible because nothing enumerated the families themselves.

Two complementary gates, and the second is the one that matters:

* `test_<lang>_extracts_every_construct_family` pins a hand-written list of
  symbols per language. It catches a family someone thought of and broke.
* `test_python_extraction_matches_ast_ground_truth` compares the extractor
  against Python's own `ast` over the same fixture. It catches a family
  **nobody thought of**, which is precisely how the original gap was found and
  the only kind of test that would have caught it in the first place.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest
from mewbo_graph.wiki.graph import GraphIndex

FIXTURES = Path(__file__).parent / "fixtures" / "coverage"

# Symbol names each language's fixture must yield. Names encode their family,
# so a failure reads as "the decorated-def pattern regressed", not "count 41 !=
# 43". Kept as sets: the extractor is free to find MORE than this.
EXPECTED: dict[str, set[str]] = {
    "families.py": {
        # plain module level
        "module_fn", "module_async_fn", "ModuleClass",
        "plain_method", "async_method",
        # decorated — a decorated def is wrapped in `decorated_definition`
        "decorated_fn", "multi_decorated_fn", "DecoratedClass",
        "decorated_property", "decorated_staticmethod", "decorated_classmethod",
        # nested
        "outer_fn", "nested_fn", "NestedInFunction", "method_of_nested_class",
        "OuterClass", "NestedClass", "nested_class_method",
        "method_holding_a_fn", "fn_inside_method",
        # conditional / guarded
        "conditional_fn", "ConditionalClass",
        "ClassWithGuardedBody", "conditional_method",
    },
    "families.ts": {
        "ModuleClass", "plainMethod", "staticMethod", "getterMethod",
        "AbstractClass", "abstractMethod", "DerivedClass",
        "moduleFn", "generatorFn", "ModuleInterface",
        "ExportedAlias", "LocalAlias", "GenericAlias", "ModuleEnum",
        "arrowConst", "unexportedArrowConst", "asyncArrowConst",
        "fnExpressionConst",
        "FieldArrowClass", "fieldArrowMethod", "staticFieldArrowMethod",
        "consumer",
    },
    "families.tsx": {
        "ArrowComponent", "useHook", "FunctionComponent",
        "ComponentProps", "PropsAlias", "ComponentClass", "renderBody",
        "trailingArrow", "trailingFn",
    },
    "families.js": {
        "ModuleClass", "plainMethod", "staticMethod",
        "moduleFn", "generatorFn",
        "arrowConst", "unexportedArrowConst", "asyncArrowConst",
        "fnExpressionConst",
        "FieldArrowClass", "fieldArrowMethod", "consumer",
    },
}


@pytest.fixture
def graph() -> GraphIndex:
    return GraphIndex()


def _symbols(graph: GraphIndex, fixture: str) -> set[str]:
    """Every non-File symbol name the live extractor yields for *fixture*."""
    result = graph.parse_file(
        slug="x/y", file_path=FIXTURES / fixture, repo_root=FIXTURES
    )
    return {n.name for n in result.nodes if n.type != "File"}


@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_extracts_every_construct_family(graph, fixture):
    missing = EXPECTED[fixture] - _symbols(graph, fixture)
    assert not missing, f"{fixture}: extractor missed {sorted(missing)}"


def _ast_ground_truth(node: ast.AST, *, in_class: bool) -> Counter[tuple[str, str]]:
    """Every def `ast` sees below *node*, keyed by nearest enclosing SCOPE.

    `if`, `try`, `with` and `for` create no scope in Python, so a def guarded by
    one still belongs to the class or function around it — hence the recursive
    descent that carries `in_class` through them, rather than an `ast.walk` that
    reads a def's immediate parent. Reading the immediate parent calls a guarded
    method a plain function, which is a claim about the language, not about the
    extractor.

    Returns a MULTISET, not a set. A `set` of `(kind, name)` collapses two
    genuinely distinct defs that happen to share a kind and name — e.g. two
    classes in the same file each defining `__init__` — into one entry, and
    both sides of the comparison would collapse identically, so the test would
    stay green even if the extractor dropped one of the two nodes outright.
    Counting occurrences is what a multiset buys that a set cannot.
    """
    found: Counter[tuple[str, str]] = Counter()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[("Method" if in_class else "Function", child.name)] += 1
            found += _ast_ground_truth(child, in_class=False)
        elif isinstance(child, ast.ClassDef):
            found[("Class", child.name)] += 1
            found += _ast_ground_truth(child, in_class=True)
        else:
            found += _ast_ground_truth(child, in_class=in_class)
    return found


def test_python_extraction_matches_ast_ground_truth(graph):
    """The extractor must agree with Python's own parser, in both directions.

    Ground truth comes from `ast`, never from tree-sitter — the two have to be
    independent or this only asserts the extractor agrees with itself. The
    reverse direction matters just as much: an extractor that invented symbols
    would inflate the graph as surely as one that misses them.
    """
    fixture = FIXTURES / "families.py"
    truth = _ast_ground_truth(ast.parse(fixture.read_bytes()), in_class=False)

    result = graph.parse_file(
        slug="x/y", file_path=fixture, repo_root=FIXTURES
    )
    extracted: Counter[tuple[str, str]] = Counter(
        (n.type, n.name) for n in result.nodes if n.type != "File"
    )

    missing = truth - extracted
    extra = extracted - truth
    assert not missing, f"ast found defs the extractor missed: {sorted(missing.elements())}"
    assert not extra, f"extractor invented defs ast does not see: {sorted(extra.elements())}"


def test_tsx_is_parsed_by_a_jsx_aware_grammar(graph):
    """A `.tsx` file's symbols must survive its JSX.

    The TypeScript grammar reads `<div>` as a type assertion and fills the rest
    of the tree with ERROR nodes, so symbols positioned after the first JSX
    expression vanish. `trailingArrow`/`trailingFn` sit at the bottom of the
    fixture for exactly this reason — they are the ones that disappear when
    `.tsx` is routed to the wrong grammar.
    """
    names = _symbols(graph, "families.tsx")
    assert {"trailingArrow", "trailingFn"} <= names


def test_symbol_kinds_are_not_collapsed(graph):
    """Methods stay Methods and functions stay Functions across the split.

    Python routes every `function_definition` through ONE capture family and
    splits it back into Function vs Method by scope; this pins that the split
    still discriminates rather than labelling everything one kind.
    """
    result = graph.parse_file(
        slug="x/y", file_path=FIXTURES / "families.py", repo_root=FIXTURES
    )
    by_name = {n.name: n.type for n in result.nodes if n.type != "File"}
    assert by_name["module_fn"] == "Function"
    assert by_name["plain_method"] == "Method"
    assert by_name["decorated_property"] == "Method"
    assert by_name["nested_fn"] == "Function"
    assert by_name["fn_inside_method"] == "Function"
    assert by_name["nested_class_method"] == "Method"
    assert by_name["NestedClass"] == "Class"
