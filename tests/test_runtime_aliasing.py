"""A harness reachable by a second path must not be able to stay quiet.

The control this guards is path-PREFIX based, and a bind mount gives the same
inodes a second name that ``realpath`` does not collapse. Measured on a real
deployment: the turn loop's source and the file holding the provider
credentials were refused through the harness path and resolved for WRITE
through the project path, same ``st_dev`` and ``st_ino`` both times.

The probe cannot fix that — the cure is topology — so what is asserted here is
that it SAYS so, with both names, rather than letting a control report healthy
while enforcing nothing.

Both directory readers are injected, so an aliased shape is described rather
than built: creating one for real would need a bind mount and privileges a test
has no business holding.
"""

from __future__ import annotations

import pytest
from mewbo_tools.integration.runtime_aliasing import RuntimeAlias, RuntimeAliasProbe


class _FakeTree:
    """A directory tree described as identities, with no filesystem behind it."""

    def __init__(self, children: dict[str, list[str]], identities: dict[str, tuple[int, int]]):
        self._children = children
        self._identities = identities

    def identify(self, path: str) -> tuple[int, int]:
        try:
            return self._identities[path]
        except KeyError:
            raise OSError(2, "No such file or directory", path) from None

    def list_dir(self, path: str) -> list[str]:
        return list(self._children.get(path, []))

    def is_walkable(self, _path: str) -> bool:
        """Everything listed here is a directory; `identify` rejects the rest."""
        return True


def _probe(
    tree: _FakeTree, harness: tuple[str, ...], projects: tuple[str, ...]
) -> RuntimeAliasProbe:
    return RuntimeAliasProbe(
        harness_roots=harness,
        project_roots=projects,
        identify=tree.identify,
        list_dir=tree.list_dir,
        is_walkable=tree.is_walkable,
    )


class TestAnAliasedRuntimeIsReported:
    """The deployed shape, reduced to the two names and the shared inode."""

    def test_a_bind_mounted_source_directory_is_found_by_identity(self):
        # `<runtime>/packages/pkg/src/pkg` and `<project>/packages/pkg/src/pkg`
        # are the same directory; every path above them differs, which is
        # exactly why a prefix denial on the runtime misses the project name.
        shared = (66307, 16148918)
        tree = _FakeTree(
            children={
                "/app/packages": ["pkg"],
                "/app/packages/pkg": ["src"],
                "/app/packages/pkg/src": ["pkg"],
                "/proj/acme": ["packages"],
                "/proj/acme/packages": ["pkg"],
                "/proj/acme/packages/pkg": ["src"],
                "/proj/acme/packages/pkg/src": ["pkg"],
            },
            identities={
                "/app/packages": (66307, 1),
                "/app/packages/pkg": (66307, 2),
                "/app/packages/pkg/src": (66307, 3),
                "/app/packages/pkg/src/pkg": shared,
                "/proj/acme": (66307, 10),
                "/proj/acme/packages": (66307, 11),
                "/proj/acme/packages/pkg": (66307, 12),
                "/proj/acme/packages/pkg/src": (66307, 13),
                "/proj/acme/packages/pkg/src/pkg": shared,
            },
        )
        found = _probe(tree, ("/app/packages",), ("/proj/acme",)).aliases()
        assert found == (
            RuntimeAlias(
                harness_path="/app/packages/pkg/src/pkg",
                project_path="/proj/acme/packages/pkg/src/pkg",
                device=66307,
                inode=16148918,
            ),
        )

    def test_a_directly_mounted_config_directory_is_found_at_the_root(self):
        """The config directory is mounted whole, so the roots themselves match."""
        shared = (66307, 15997214)
        tree = _FakeTree(
            children={},
            identities={"/app/configs": shared, "/proj/acme/configs": shared},
        )
        found = _probe(tree, ("/app/configs",), ("/proj/acme/configs",)).aliases()
        assert len(found) == 1
        assert found[0].inode == 15997214

    def test_the_report_names_both_paths_and_hands_them_back(self):
        shared = (1, 99)
        tree = _FakeTree(
            children={}, identities={"/app/configs": shared, "/proj/acme/configs": shared}
        )
        found = _probe(tree, ("/app/configs",), ("/proj/acme/configs",)).report()
        assert len(found) == 1, "the caller reads the finding, not only a log sink"
        assert "/app/configs" in found[0].describe()
        assert "/proj/acme/configs" in found[0].describe()


class TestAnUnaliasedDeploymentIsSilent:
    """A false alarm here trains everyone to ignore the real one."""

    def test_distinct_inodes_produce_nothing(self):
        tree = _FakeTree(
            children={},
            identities={"/app/configs": (66307, 1), "/proj/acme/configs": (66307, 2)},
        )
        assert _probe(tree, ("/app/configs",), ("/proj/acme/configs",)).aliases() == ()

    def test_no_configured_projects_produce_nothing(self):
        tree = _FakeTree(children={}, identities={"/app/configs": (66307, 1)})
        assert _probe(tree, ("/app/configs",), ()).aliases() == ()

    def test_a_harness_that_does_not_exist_produces_nothing(self):
        """Harness self-deny off, or a layout contributing no roots."""
        tree = _FakeTree(children={}, identities={"/proj/acme": (66307, 2)})
        assert _probe(tree, (), ("/proj/acme",)).aliases() == ()

    def test_an_unreadable_directory_is_skipped_not_fatal(self):
        """A directory this process cannot read is one it cannot alias through."""
        tree = _FakeTree(
            children={"/app/packages": ["locked"]},
            identities={"/app/packages": (66307, 1), "/proj/acme": (66307, 2)},
        )
        assert _probe(tree, ("/app/packages",), ("/proj/acme",)).aliases() == ()


class TestTheWalkIsBounded:
    """This runs at startup, so an unbounded walk of a project tree is a defect."""

    def test_nothing_below_max_depth_is_visited(self):
        shared = (1, 500)
        deep = "/app/a/b/c/d/e"
        tree = _FakeTree(
            children={
                "/app": ["a"],
                "/app/a": ["b"],
                "/app/a/b": ["c"],
                "/app/a/b/c": ["d"],
                "/app/a/b/c/d": ["e"],
                "/proj": ["x"],
                "/proj/x": [],
            },
            identities={
                "/app": (1, 1),
                "/app/a": (1, 2),
                "/app/a/b": (1, 3),
                "/app/a/b/c": (1, 4),
                "/app/a/b/c/d": (1, 5),
                deep: shared,
                "/proj": (1, 10),
                "/proj/x": shared,
            },
        )
        probe = RuntimeAliasProbe(
            harness_roots=("/app",),
            project_roots=("/proj",),
            max_depth=2,
            identify=tree.identify,
            list_dir=tree.list_dir,
            is_walkable=tree.is_walkable,
        )
        assert probe.aliases() == (), "a depth cap must actually cap the walk"


@pytest.mark.parametrize("depth", [0, 1, 4])
def test_the_root_itself_is_always_identified(depth):
    """Depth 0 still compares the roots, which is the config-mount shape."""
    shared = (7, 7)
    tree = _FakeTree(children={}, identities={"/app/configs": shared, "/proj/c": shared})
    probe = RuntimeAliasProbe(
        harness_roots=("/app/configs",),
        project_roots=("/proj/c",),
        max_depth=depth,
        identify=tree.identify,
        list_dir=tree.list_dir,
        is_walkable=tree.is_walkable,
    )
    assert len(probe.aliases()) == 1
