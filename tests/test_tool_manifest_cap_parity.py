"""A cached tool manifest may never serve a spec that lost a declared cap.

The defect these pin is not a wrong value written down somewhere — it is a
*missing key*. A manifest written before a field existed carries no entry for
it, the loader defaults it, and the resulting spec is internally consistent,
loads clean, and is wrong. It cost a live deployment every declared result cap:
all 159 tools ran at the 2000-char class default while ``read_file`` declared
200_000, so a model reading a file received 5.6% of it and no honest signal.

So these drive the REAL ``load_registry`` against manifest files written to
disk, and the stale-manifest case is constructed BY HAND — a manifest produced
by current code can never reach the path only a legacy file takes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from mewbo_core.common import get_logger
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.tooling.tool_registry import (
    ToolSpec,
    _built_in_manifest_entries,
    _default_registry,
    load_registry,
)

# Every knob a built-in DECLARES and a manifest entry merely caches. Spelled out
# here rather than imported from ``ToolSpec`` — a test that reads its expectations
# out of the code under test cannot fail when that code is wrong, and this file
# must stay collectible against a tree that does not have the fix yet.
DECLARED_KNOBS = (
    "max_result_chars",
    "timeout",
    "concurrency_safe",
    "interrupt_behavior",
    "poll",
    "poll_when_args",
    "read_only",
    "capability",
)


def _write_manifest(path: Path, tools: list[dict[str, Any]]) -> str:
    """Write *tools* as a manifest file and return its path."""
    path.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    return str(path)


def _stale_entries() -> list[dict[str, Any]]:
    """The built-in entries as a manifest written BEFORE the caps existed.

    Every declared knob is stripped, which is exactly the shape measured on
    the deployed stack: ``0 of 159`` entries carried ``max_result_chars``.
    """
    return [
        {key: value for key, value in entry.items() if key not in DECLARED_KNOBS}
        for entry in _built_in_manifest_entries()
    ]


def _knobs(spec: ToolSpec) -> dict[str, object]:
    return {name: getattr(spec, name) for name in DECLARED_KNOBS}


class TestStaleManifestSelfHeals:
    """An on-disk manifest missing a declared cap must not be able to serve it."""

    def test_read_file_keeps_its_declared_cap_through_a_stale_manifest(
        self, tmp_path: Path
    ) -> None:
        """The measured defect, reproduced: a legacy manifest, a 2000-char read.

        Fails without the load-time overlay — the loader defaults the absent key
        to ``ToolSpec.max_result_chars`` (2000) and the 200_000 declared on the
        direct registration never reaches the deployment.
        """
        entries = _stale_entries()
        read_file_entry = next(e for e in entries if e["tool_id"] == "read_file")
        # Guard the fixture itself: if the key were still present the test would
        # pass for the wrong reason, proving nothing about the stale case.
        assert "max_result_chars" not in read_file_entry

        manifest = _write_manifest(tmp_path / "tool-manifest.json", entries)
        registry = load_registry(manifest)

        spec = registry.get_spec("read_file")
        assert spec is not None
        assert spec.max_result_chars == 200_000

    def test_every_built_in_resolves_its_directly_registered_knobs(
        self, tmp_path: Path
    ) -> None:
        """Parity over EVERY built-in: manifest-loaded knob == declared knob.

        The per-tool assertion is what makes this durable — a cap added to a new
        tool's direct registration is covered the day it is written, with no
        second list to keep in step.
        """
        manifest = _write_manifest(tmp_path / "tool-manifest.json", _stale_entries())
        loaded = load_registry(manifest)
        declared = _default_registry()

        for declared_spec in declared.list_specs(include_disabled=True):
            loaded_spec = loaded.get_spec(declared_spec.tool_id)
            assert loaded_spec is not None, declared_spec.tool_id
            assert _knobs(loaded_spec) == _knobs(declared_spec), declared_spec.tool_id

    def test_the_repair_is_loud(self, tmp_path: Path) -> None:
        """A silent repair leaves the stale file in place for the next reader.

        Captured through a loguru sink: ``caplog`` hooks stdlib logging and
        would pass vacuously against this module.
        """
        messages: list[str] = []
        sink_id = get_logger().add(lambda message: messages.append(str(message)), level="WARNING")
        try:
            manifest = _write_manifest(tmp_path / "tool-manifest.json", _stale_entries())
            load_registry(manifest)
        finally:
            get_logger().remove(sink_id)

        stale = [line for line in messages if "Tool manifest is stale" in line]
        assert stale, messages
        assert any("read_file" in line and "200000" in line.replace("_", "") for line in stale)


class TestFreshManifestNeedsNoRepair:
    """The WRITER and the direct registration must not drift apart either.

    The overlay repairs a stale file at load; it does not excuse the manifest
    writer from emitting what it declares. If these disagree, every freshly
    written manifest is born needing repair — which the overlay would then
    silently perform forever.
    """

    def test_written_entries_agree_with_the_direct_registration(
        self, tmp_path: Path
    ) -> None:
        manifest = _write_manifest(
            tmp_path / "tool-manifest.json", list(_built_in_manifest_entries())
        )
        loaded = load_registry(manifest)
        declared = _default_registry()

        for declared_spec in declared.list_specs(include_disabled=True):
            loaded_spec = loaded.get_spec(declared_spec.tool_id)
            assert loaded_spec is not None, declared_spec.tool_id
            assert _knobs(loaded_spec) == _knobs(declared_spec), declared_spec.tool_id

    def test_a_fresh_manifest_logs_no_staleness(self, tmp_path: Path) -> None:
        """The parity guard must stay quiet when there is nothing to repair."""
        messages: list[str] = []
        sink_id = get_logger().add(lambda message: messages.append(str(message)), level="WARNING")
        try:
            manifest = _write_manifest(
                tmp_path / "tool-manifest.json", list(_built_in_manifest_entries())
            )
            load_registry(manifest)
        finally:
            get_logger().remove(sink_id)

        assert not [line for line in messages if "Tool manifest is stale" in line], messages


class TestOverlayScope:
    """The overlay is authority for BUILT-INS only."""

    def test_an_mcp_entry_keeps_the_manifest_value(self, tmp_path: Path) -> None:
        """No built-in declares an MCP tool, so nothing may overwrite its entry.

        Discovery is the only authority for those, and a value it recorded must
        survive the load untouched.
        """
        entries = list(_built_in_manifest_entries())
        entries.append(
            {
                "tool_id": "mcp_probe_thing",
                "name": "thing",
                "description": "an MCP tool",
                "kind": "mcp",
                "server": "probe",
                "tool": "thing",
                "enabled": True,
                "max_result_chars": 12_345,
                "timeout": 7.0,
            }
        )
        manifest = _write_manifest(tmp_path / "tool-manifest.json", entries)

        spec = load_registry(manifest).get_spec("mcp_probe_thing")
        assert spec is not None
        assert spec.max_result_chars == 12_345
        assert spec.timeout == 7.0

    def test_the_overlay_touches_only_the_declared_knobs(
        self, tmp_path: Path
    ) -> None:
        """Identity, enablement and schema stay the manifest's to decide.

        A manifest records what discovery found — including a tool disabled
        because its server was unreachable. An overlay reaching past the four
        declared knobs would resurrect it.
        """
        entries = _stale_entries()
        for entry in entries:
            if entry["tool_id"] == "read_file":
                entry["enabled"] = False
                entry["description"] = "manifest-authored description"

        manifest = _write_manifest(tmp_path / "tool-manifest.json", entries)
        spec = load_registry(manifest).get_spec("read_file")

        assert spec is not None
        assert spec.enabled is False
        assert spec.description == "manifest-authored description"
        assert spec.max_result_chars == 200_000


class TestListingDegradesStructurally:
    """A cap the listing tool now declares must also be SPENDABLE on a listing.

    Filed here rather than in a suite of its own because it closes the same
    issue's other half: raising `aider_list_dir_tool`'s cap achieves nothing if
    an over-cap listing still collapses into the `{"truncated": true}` wrapper,
    losing every path as structure. `entries` is a LIST, and `_fitted_json`
    fitted only `str` fields — so naming it in `_WINDOWED_RESULT_FIELDS` was
    inert on its own.
    """

    @staticmethod
    def _build_loop() -> ToolUseLoop:
        from test_tool_use_loop import (  # noqa: PLC0415 — sibling test helpers
            _allow_all_policy,
            _make_agent_context,
            _make_hook_manager,
            _make_registry,
            _make_spec,
        )

        with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=_make_registry(_make_spec("aider_list_dir_tool", "List")),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

    def test_an_over_cap_listing_keeps_its_entries_as_entries(self) -> None:
        loop = self._build_loop()
        listing = {
            "kind": "dir",
            "path": "packages",
            "entries": [f"packages/mewbo_core/src/module_{n:04d}.py" for n in range(500)],
        }

        fitted = json.loads(loop._fitted_json(listing, 2000))

        # The envelope survives: still a listing, not a wrapper around a blob.
        assert fitted["kind"] == "dir"
        assert fitted["path"] == "packages"
        assert "truncated" not in fitted
        assert isinstance(fitted["entries"], list)
        # Whole paths, never a path cut mid-name — a truncated filename names a
        # file that does not exist, which is worse than an absent one.
        assert fitted["entries"][0] == listing["entries"][0]
        assert 1 < len(fitted["entries"]) < 500
        assert "entries omitted" in fitted["entries"][-1]

    def test_a_listing_that_fits_is_untouched(self) -> None:
        loop = self._build_loop()
        listing = {"kind": "dir", "path": "pkg", "entries": ["a.py", "b.py"]}

        assert json.loads(loop._fitted_json(listing, 2000)) == listing


class TestLimitMismatches:
    """The comparison itself — the only thing that can see this defect class."""

    def test_an_absent_key_reads_as_a_mismatch_not_as_a_choice(self) -> None:
        """A defaulted field and a deliberately-default field are the same value.

        Which is precisely why the comparison must be against the DECLARATION
        rather than against the manifest's own contents: nothing in the loaded
        spec records that a key was missing.
        """
        declared = _default_registry().get_spec("read_file")
        assert declared is not None
        defaulted = ToolSpec(
            tool_id="read_file",
            name=declared.name,
            description=declared.description,
            factory=declared.factory,
        )

        mismatches = defaulted.knob_mismatches(declared)
        assert mismatches["max_result_chars"] == (2000, 200_000)
        assert defaulted.with_declared_knobs(declared).max_result_chars == 200_000

    def test_the_overlay_covers_exactly_the_knobs_this_module_pins(self) -> None:
        """The local list and the class's must not drift apart.

        Spelling the knobs out above is what lets the rest of this file fail
        honestly; this is the one assertion that keeps the copy accurate, so a
        knob added to ``ToolSpec.DECLARED_KNOBS`` and not to this module fails
        HERE rather than silently dropping out of every parity assertion.
        """
        assert set(ToolSpec.DECLARED_KNOBS) == set(DECLARED_KNOBS)

    def test_an_agreeing_spec_reports_nothing(self) -> None:
        declared = _default_registry().get_spec("aider_shell_tool")
        assert declared is not None
        assert declared.knob_mismatches(declared) == {}
