"""The manifest round-trip must not drop a ``ToolSpec`` field.

A deployment with an MCP config loads its registry from a cached manifest
rather than from the direct registration, so a field the rebuild forgets is
not defaulted deliberately — it is LOST, and only on the path most real
deployments take. That is invisible three ways at once: the direct
registration still reads correctly, a test that builds the registry in
process never touches the manifest, and the resulting cap is a plausible
number rather than an error.

The measured instance: ``max_result_chars`` was absent from the rebuild, so
every tool ran at the 2000-char default. A file read came back with its
middle removed on files well under the tool's own line window, and the model
argued with a tool whose schema had told it no such limit existed.
"""

from __future__ import annotations

import json
from pathlib import Path

from mewbo_core.tooling.tool_registry import (
    ToolSpec,
    _built_in_manifest_entries,
    load_registry,
)

# Tools whose cap is a deliberate decision rather than the default, and the
# value each one declares. A cap that silently reverts to the default is the
# defect this module exists to catch, so the numbers are stated here rather
# than read back out of the source under test.
DECLARED_CAPS = {
    "read_file": 200_000,
    "aider_shell_tool": 30_000,
    "shell_session_tool": 30_000,
    # Both are BULK payloads with no paging affordance — a directory listing
    # reached the model as its first ~45 paths, a whole-home state dump as its
    # first dozen entities, and neither cut can be asked for again.
    "aider_list_dir_tool": 30_000,
    "home_assistant_tool": 30_000,
}


def _write_manifest(tmp_path: Path) -> str:
    path = tmp_path / "tool-manifest.json"
    entries = _built_in_manifest_entries()
    path.write_text(json.dumps({"tools": entries}, default=str), encoding="utf-8")
    return str(path)


class TestManifestCarriesDeclaredCaps:
    """The WRITER half: an entry must state a cap it means to keep."""

    def test_every_declared_cap_is_present_in_the_manifest(self) -> None:
        entries = {
            entry["tool_id"]: entry
            for entry in _built_in_manifest_entries()
            if isinstance(entry, dict) and "tool_id" in entry
        }
        for tool_id, expected in DECLARED_CAPS.items():
            assert entries[tool_id].get("max_result_chars") == expected, (
                f"{tool_id} declares a cap in its direct registration but its "
                "manifest entry omits one, so the manifest path silently "
                "reverts it to the default"
            )


class TestRebuildPreservesDeclaredFields:
    """The READER half: what the manifest states must survive the rebuild."""

    def test_caps_survive_a_manifest_round_trip(self, tmp_path: Path) -> None:
        registry = load_registry(_write_manifest(tmp_path))
        by_id = {spec.tool_id: spec for spec in registry.list_specs(include_disabled=True)}
        for tool_id, expected in DECLARED_CAPS.items():
            assert by_id[tool_id].max_result_chars == expected

    def test_a_tool_declaring_no_cap_still_gets_the_default(self, tmp_path: Path) -> None:
        """Absence must read as the default, not as zero (which means unlimited)."""
        registry = load_registry(_write_manifest(tmp_path))
        undeclared = [
            spec
            for spec in registry.list_specs(include_disabled=True)
            if spec.tool_id not in DECLARED_CAPS
        ]
        assert undeclared, "expected at least one tool without a declared cap"
        assert all(spec.max_result_chars == ToolSpec.max_result_chars for spec in undeclared)

    def test_the_file_reader_outranks_the_shell(self, tmp_path: Path) -> None:
        """A regression guard stated as the relationship, not the numbers.

        The defect that motivated this module left the file reader capped an
        order of magnitude BELOW shell output, so a model could see more of a
        command's stdout than of the file it was editing.
        """
        registry = load_registry(_write_manifest(tmp_path))
        by_id = {spec.tool_id: spec for spec in registry.list_specs(include_disabled=True)}
        assert by_id["read_file"].max_result_chars > by_id["aider_shell_tool"].max_result_chars
