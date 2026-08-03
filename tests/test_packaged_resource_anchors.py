"""Packaged assets are anchored on their package, never on a module's own position.

The second defect class a module move creates, and the one an import gate cannot
catch by construction: an import graph is about imports, and this is a filesystem
path.

A template directory belonging to a PACKAGE was once resolved with
``Path(__file__).parent`` from a module inside it. Moving that module into a
subpackage took the search path along with it, and the templates stopped being
found — **54 test errors from one line**, surfacing at render time rather than at
import time, so nothing about the move looked wrong until something tried to read
a template. ``project_store.py`` carries the cure and the reasoning:
``resources.files("mewbo_core") / "templates"``.

The rule asserted here is deliberately blunt: **a module may not reach for its own
location.** A package's ``__init__.py`` is the one exception, and it is exempt by
definition rather than by concession — its directory IS the package directory, so
``Path(__file__).parent`` there denotes exactly what ``resources.files(package)``
denotes, and the only move that relocates it is a move of the whole package.

A bare ``__file__`` is what counts. ``logging.__file__`` — another module's
attribute, read to identify a stack frame — is a different expression and is not
this defect; ``test_the_rule_reads_the_expression_not_the_word`` pins that
distinction against source written to contain both.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

# Sibling reuse: `tests/` is on sys.path under pytest, and the package list is
# already stated once there. Importing it means a package added to the import
# gate is covered by this one too, with no second list to keep in step.
from test_package_imports import DISTRIBUTED_PACKAGES, REPO_ROOT

# A floor, not a count. Every assertion below is over what the scan found, and
# "no module reaches for its own location" is trivially true of no modules.
MINIMUM_FILES_SCANNED = 350


class PackagedResourceAnchors:
    """Every site in packaged source that reads the module's own ``__file__``.

    State: the sites found, split by whether the file holding them is a package
    ``__init__.py``. Behaviour: the scan itself, and rendering a site as
    ``path:line`` for a failure message.

    Parsed, never imported — the same reason ``test_core_import_graph.py`` parses:
    importing packaged modules executes real import-time side effects, and this
    question is answerable from the syntax alone.
    """

    def __init__(self, roots: tuple[Path, ...]) -> None:
        """Scan every ``.py`` file under *roots* for bare ``__file__`` reads."""
        self.roots = roots
        self.scanned: list[Path] = []
        self.package_anchors: list[str] = []
        self.module_relative: list[str] = []
        for root in roots:
            for path in sorted(root.rglob("*.py")):
                self.scanned.append(path)
                for lineno in self._own_file_sites(path):
                    site = f"{self._display(path)}:{lineno}"
                    if path.name == "__init__.py":
                        self.package_anchors.append(site)
                    else:
                        self.module_relative.append(site)

    @classmethod
    def build(cls) -> PackagedResourceAnchors:
        """Scan the source root of every distributed package."""
        return cls(tuple(package.src_root for package in DISTRIBUTED_PACKAGES))

    @staticmethod
    def _display(path: Path) -> str:
        """*path* relative to the repository root, or absolute if it lies outside.

        A site is only useful in a failure message if it is clickable, and the
        real scan is always inside the tree. The fallback exists because the
        classifier is also driven against packages built under ``tmp_path``.
        """
        try:
            return str(path.relative_to(REPO_ROOT))
        except ValueError:
            return str(path)

    @staticmethod
    def _own_file_sites(path: Path) -> list[int]:
        """Line numbers where *path* reads a bare ``__file__``.

        ``ast.Name`` and not ``ast.Attribute`` is the whole discrimination.
        ``__file__`` names THIS module's location — the thing that moves when
        this module moves. ``other.__file__`` is an attribute read off some
        other module and has nothing to do with where this file sits, which is
        why a textual search for the word cannot answer this question.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "__file__"
        ]


@pytest.fixture(scope="module")
def anchors() -> PackagedResourceAnchors:
    """The scan across every distributed package's source root."""
    return PackagedResourceAnchors.build()


def test_no_packaged_module_resolves_a_path_from_its_own_file(
    anchors: PackagedResourceAnchors,
) -> None:
    """The gate. Only a package ``__init__.py`` may read its own ``__file__``.

    A failure here is not a style complaint. It marks a path that will follow
    its module the next time that module moves, and will report the breakage as
    a missing file at render time — far from the edit, long after the move, and
    with nothing in the diff that looked like a path change.

    The fix is to name the package that OWNS the asset:
    ``resources.files("package.subpackage") / "asset"``.
    """
    assert anchors.module_relative == [], (
        "these modules resolve a path from their own location, so the path "
        "follows the module if it ever moves and fails at read time rather "
        "than at import:\n"
        + "\n".join(f"  {site}" for site in anchors.module_relative)
        + "\nanchor on the package that owns the asset instead: "
        'resources.files("package.subpackage") / "asset"'
    )


def test_the_scan_reached_a_real_tree_and_found_the_anchors_it_permits(
    anchors: PackagedResourceAnchors,
) -> None:
    """Non-vacuity: the scanner parses a real tree and does see ``__file__``.

    Both halves matter. A scanner pointed at a mistyped root returns no
    violations and no anchors, and the gate above passes over nothing at all.
    Asserting that the permitted ``__init__.py`` anchors are still FOUND is what
    proves the detector works, since those are the only sites it is expected to
    see — the failing case has no examples left in the tree by design.
    """
    assert len(anchors.scanned) >= MINIMUM_FILES_SCANNED, (
        f"scanned only {len(anchors.scanned)} files across "
        f"{len(anchors.roots)} package roots — discovery is broken, so the "
        "gate is asserting nothing"
    )
    assert anchors.package_anchors, (
        "the scanner found no `__file__` reads at all, not even the package "
        "`__init__.py` anchors that are known to exist — it is not detecting "
        "the thing it claims to detect"
    )


def test_the_rule_reads_the_expression_not_the_word(tmp_path: Path) -> None:
    """The classifier separates a bare ``__file__`` from another module's attribute.

    Every case below is one the real tree contains. The sharp one is
    ``logging.__file__``: a textual search for the word flags it, and it is not
    this defect — the module is reading somebody else's location to identify a
    stack frame, and no move of this file changes what it resolves to.
    """
    source = textwrap.dedent(
        '''
        """Fixture module covering every classification case."""
        import logging
        from pathlib import Path

        TEMPLATES = Path(__file__).parent / "templates"
        SIBLING = Path(__file__).with_name("email_template.html.j2")


        def caller(frame):
            return frame.f_code.co_filename == logging.__file__


        def deferred():
            return Path(__file__).resolve()
        '''
    )
    path = tmp_path / "fixture.py"
    path.write_text(source, encoding="utf-8")

    assert PackagedResourceAnchors._own_file_sites(path) == [6, 7, 15]


def test_a_package_init_is_the_one_permitted_anchor(tmp_path: Path) -> None:
    """An ``__init__.py`` site is recorded as an anchor, the same site elsewhere is not.

    The exemption is the load-bearing half of the rule, so it is driven rather
    than assumed: identical source is written to two files whose only difference
    is the name, and the classification has to follow the name.
    """
    body = 'from pathlib import Path\n\nROOT = Path(__file__).resolve().parent\n'
    root = tmp_path / "fixturepkg"
    root.mkdir()
    (root / "__init__.py").write_text(body, encoding="utf-8")
    (root / "helper.py").write_text(body, encoding="utf-8")

    anchors = PackagedResourceAnchors((root,))

    assert [site.rsplit("/", 1)[-1] for site in anchors.package_anchors] == ["__init__.py:3"]
    assert [site.rsplit("/", 1)[-1] for site in anchors.module_relative] == ["helper.py:3"]
