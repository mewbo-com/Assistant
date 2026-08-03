"""Tests for ``resolve_safe_path`` symlink handling.

A symlink living inside an allowed project root must be honored even when
its target sits outside every root — a workspace symlink like
``<project>/shared -> /mnt/external`` is legitimate. Resolution therefore
cannot lean on ``Path.resolve()`` alone, which follows the link and lands
outside every root. ``../`` escape attempts must still be rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_core.config import reset_config, set_config_override
from mewbo_tools.core import resolve_safe_path


@pytest.fixture(autouse=True)
def _tmp_path_is_a_configured_project(tmp_path: Path):
    """Register ``tmp_path`` as a configured project — deliberately NOT a flag pin.

    Every sibling module that broke the same way pins
    ``path_scope_to_active_project`` OFF, because its subject is tool semantics
    and the path guard is incidental. This module's subject IS
    ``resolve_safe_path``, so pinning the axis off would leave the SHIPPED shape
    untested in the one file that most needs to test it — a test that pins away
    the default it exists to exercise is a test that has stopped being able to
    fail.

    Making ``tmp_path`` a configured project keeps scoping ON and puts the roots
    these cases pass inside the allowed union legitimately, so the caller's
    ``root`` NARROWS rather than widens. The symlink and traversal assertions
    then run against the real gate instead of around it.
    """
    set_config_override({"projects": {"tmpfixture": {"path": str(tmp_path)}}})
    yield
    reset_config()


def test_accepts_symlink_inside_root_pointing_outside(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "config.yml").write_text("hello\n", encoding="utf-8")

    (project / "shared").symlink_to(external)

    resolved = resolve_safe_path("shared/config.yml", root=str(project))

    assert resolved.read_text(encoding="utf-8") == "hello\n"
    # The file is readable — the exact returned path may be the physical
    # (resolved) location, since /tmp is itself an allowed root.


def test_accepts_absolute_path_through_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "note.txt").write_text("data", encoding="utf-8")

    (project / "link").symlink_to(external)

    target = str(project / "link" / "note.txt")
    resolved = resolve_safe_path(target, root=str(project))

    assert resolved.read_text(encoding="utf-8") == "data"


def test_rejects_dot_dot_escape_to_system_path(tmp_path: Path) -> None:
    # Both tmp_path siblings sit under /tmp, which is allowed. Escaping to a
    # system path well outside /tmp must still be blocked.
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="outside all allowed project roots"):
        resolve_safe_path("/etc/passwd", root=str(project))


def test_rejects_direct_path_outside_all_tmp_and_roots(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    # /etc is outside both the project root and /tmp — must still be rejected.
    with pytest.raises(ValueError, match="outside all allowed project roots"):
        resolve_safe_path("/etc/hostname", root=str(project))


def test_rejection_error_names_the_allowed_roots(tmp_path: Path) -> None:
    """A rejection must tell the model what IS allowed, not only what isn't.

    A bare "resolves outside all allowed project roots" leaves the model no
    way to self-correct: it retries via shell workarounds instead of
    restaging under an allowed root. Naming the roots turns the denial into
    a one-turn recovery.
    """
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="outside all allowed project roots") as excinfo:
        resolve_safe_path("/etc/hostname", root=str(project))

    message = str(excinfo.value)
    assert str(project) in message  # the caller-supplied root is named
    assert "/tmp/mewbo" in message  # the scratch root is named


def test_accepts_when_root_itself_traverses_symlink(tmp_path: Path) -> None:
    """If the configured root is reached via a symlink, paths under it must work.

    Example: user configures ``projects.foo.path = /var/foo`` where
    ``/var -> /private/var``. ``Path.resolve(root)`` returns
    ``/private/var/foo`` while the user passes ``/var/foo/file``. The
    logical-view fallback accepts this.
    """
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    (real_root / "file.txt").write_text("ok", encoding="utf-8")

    link_root = tmp_path / "link_root"
    link_root.symlink_to(real_root)

    target = str(link_root / "file.txt")
    resolved = resolve_safe_path(target, root=str(link_root))

    assert resolved.read_text(encoding="utf-8") == "ok"


def test_rejects_path_outside_all_roots_when_no_symlink_involved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="outside all allowed project roots"):
        resolve_safe_path("/etc/hostname", root=str(project))


def test_read_tool_reads_through_symlink(tmp_path: Path) -> None:
    """Integration: ReadFileTool must read a file via a symlink inside root."""
    from mewbo_core.classes import ActionStep
    from mewbo_tools.integration.aider_file_tools import ReadFileTool

    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "litellm-config.yml").write_text("model_list: []\n", encoding="utf-8")
    (project / "shared").symlink_to(external)

    # The link's target itself holds a directory, so the traversal crosses
    # the symlink boundary and then descends.
    nested = external / "litellm"
    nested.mkdir()
    (nested / "litellm-config.yml").write_text("model_list: []\n", encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={
            "path": "shared/litellm/litellm-config.yml",
            "root": str(project),
        },
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict), f"expected dict, got error: {payload!r}"
    assert payload.get("kind") == "file"
    assert "model_list" in payload.get("text", "")
