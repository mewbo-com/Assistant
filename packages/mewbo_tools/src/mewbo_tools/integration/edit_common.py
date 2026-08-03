"""Shared utilities for file-editing tools.

Both the Aider-style SEARCH/REPLACE tool and the structured-patch tool
use these functions for path resolution, before-state reading, diff
computation, and result formatting.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from mewbo_core.contracts.diff_stat import DIFF_RESULT_KIND, DiffStat
from mewbo_core.contracts.errors import ToolInputError

from mewbo_tools.core import resolve_safe_path


def resolve_and_validate_path(file_path: str, root: str, *, write: bool = False) -> Path:
    """Resolve *file_path* under *root* and reject directory-traversal attempts.

    *write* must be ``True`` for a call that goes on to modify the file — it is
    what lets an active ``read_only`` containment refuse the edit rather than
    silently permitting it under the read rule.
    """
    try:
        return resolve_safe_path(file_path, root=root, write=write)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc


def read_file_contents(targets: dict[str, Path]) -> dict[str, str]:
    """Read current content of target files.  Missing files map to ``""``."""
    contents: dict[str, str] = {}
    for rel_path, abs_path in targets.items():
        if abs_path.exists():
            contents[rel_path] = abs_path.read_text(encoding="utf-8")
        else:
            contents[rel_path] = ""
    return contents


def build_unified_diff(before_map: dict[str, str], targets: dict[str, Path]) -> str:
    """Compute a unified diff between *before_map* snapshots and current disk state."""
    chunks: list[str] = []
    for rel_path, abs_path in targets.items():
        before = before_map.get(rel_path, "")
        after = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=rel_path,
            tofile=rel_path,
        )
        diff_text = "".join(diff)
        if diff_text:
            chunks.append(diff_text)
    return "".join(chunks)


def format_diff_result(
    diff_text: str,
    title: str,
    file_paths: list[str],
) -> dict[str, object]:
    """Build the canonical diff result dict consumed by the frontend DiffCard.

    The line counts ride ALONG with the diff rather than being left to each
    consumer: the event log is the durable record, and a reader that re-derives
    them later is reading a text the transcript may have truncated. Computed by
    the one :class:`~mewbo_core.contracts.diff_stat.DiffStat` so the producer and every
    renderer agree by construction.
    """
    stat = DiffStat.from_unified_diff(diff_text)
    return {
        "kind": DIFF_RESULT_KIND,
        "title": title,
        "text": diff_text,
        "files": file_paths,
        "additions": stat.additions,
        "deletions": stat.deletions,
    }


__all__ = [
    "build_unified_diff",
    "format_diff_result",
    "read_file_contents",
    "resolve_and_validate_path",
]
