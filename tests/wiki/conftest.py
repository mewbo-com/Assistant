"""Shared test doubles for the wiki refresh subsystem."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from mewbo_graph.wiki.graph import GraphParseResult
from mewbo_graph.wiki.types import Embedding


@pytest.fixture(autouse=True)
def _no_ambient_git_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the DEV MACHINE's real git credentials during wiki tests.

    ``resolve_chain`` consults ``ambient_credential`` (a real ``git credential
    fill`` subprocess) as its penultimate candidate — so any test that reaches
    clone/finalize/freshness/branch resolution would otherwise read (and inject)
    the developer's ACTUAL host credentials. That is both non-deterministic and a
    secret-leak risk. Default it to "no ambient credential" for every wiki test; a
    test that exercises the ambient tier overrides this with its own patch inside
    the test body (the local monkeypatch wins over this autouse setup).
    """
    monkeypatch.setattr(
        "mewbo_graph.wiki.credentials.ambient_credential", lambda host: None
    )


class FakeParser:
    """Stub GraphIndex: returns a canned GraphParseResult per relative path."""

    def __init__(self, results: dict[str, GraphParseResult]) -> None:
        self.results = results

    def parse_file(
        self, slug: str, file_path: Path, *, repo_root: Path
    ) -> GraphParseResult:
        rel = str(file_path.relative_to(repo_root))
        return self.results.get(rel, GraphParseResult(nodes=[], edges=[], skipped=[rel]))


class FakeEmbedder:
    """Deterministic stand-in for ``Embedder`` — satisfies ``EmbedderProtocol``.

    Vectors are derived from the embedded TEXT, so two different texts never
    collide and a test can tell "re-embedded after an edit" from "kept the old
    vector". Every call is recorded in :attr:`calls`, which is what lets a test
    assert WHAT was handed to the embedder rather than only that something was.

    ``fail=True`` raises on both entry points, driving a caller's degrade path
    (a real embedding backend is a network call and this is how it fails).
    """

    model = "fake-embedding"

    def __init__(self, *, dim: int = 4, fail: bool = False) -> None:
        """Configure the vector width and whether every call raises."""
        self.dim = dim
        self.fail = fail
        self.calls: list[list[tuple[str, str]]] = []

    def embed_nodes(
        self, items: list[tuple[str, str]], *, slug: str = ""
    ) -> list[Embedding]:
        """Record the ``(node_id, text)`` pairs and vectorise them."""
        self.calls.append(list(items))
        if self.fail:
            raise RuntimeError("embedding backend unavailable")
        return [
            Embedding(
                slug=slug,
                node_id=node_id,
                vector=self._vector(text),
                model=self.model,
                dim=self.dim,
            )
            for node_id, text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        """Vectorise one query string."""
        if self.fail:
            raise RuntimeError("embedding backend unavailable")
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i] / 255.0 for i in range(self.dim)]
