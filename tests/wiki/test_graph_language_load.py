"""``_load_ts_language`` loads bundled grammars via the language pack.

Reading a per-language ``.so`` out of ``tree_sitter_language_pack.cache_dir()``
and relying on ``download()`` to populate it does not work: on the 1.10.x line
``download()`` is a no-op that returns successfully and writes nothing, so a
manual ``ctypes.LoadLibrary`` fails with "cannot open shared object file" on a
path that was never created — taking both graph-only and the LLM indexer down.
The supported ``get_language`` API is what this uses: the 1.x line bundles every
grammar in the wheel, so there is no download, no cache, and no ``.so`` to
locate. These drive the real loader + a real parse, so they fail if the
production body regresses (not namesake tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from mewbo_graph.wiki.graph import GraphIndex, _load_ts_language  # noqa: E402


def test_load_ts_language_returns_usable_language() -> None:
    from tree_sitter import Language, Parser

    lang = _load_ts_language("python")
    assert isinstance(lang, Language)
    # A broken loader (missing .so) raises before this point, so a successful
    # parse is the end-to-end regression guard.
    tree = Parser(lang).parse(b"def f(x):\n    return x\n")
    assert tree.root_node.type == "module"


def test_graph_index_parses_python_without_network(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text("def hello(x):\n    return x + 1\n", encoding="utf-8")

    result = GraphIndex().parse_file("demo", src, repo_root=tmp_path)

    assert not result.skipped
    assert result.nodes  # the grammar parsed — the missing-.so bug raised here
