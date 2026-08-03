"""Contract tests for the retired-key tolerate-and-warn path in ``config.py``.

Retiring a config key has to survive the two OPPOSITE failure modes a deletion
produces, depending on which section owned the key: silent acceptance under an
``extra="ignore"`` section, and a refusal to boot under an ``extra="forbid"``
one. Both are exercised here against real ``AppConfig`` validation rather than
a stand-in model, because the pruning runs as a model validator and a stand-in
would not have one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from mewbo_core import config as config_module
from mewbo_core.config import AppConfig, RetiredKey, WikiRefreshConfig


@pytest.fixture(autouse=True)
def _forget_announcements() -> None:
    """Each test starts with nothing announced; the registry warns once per process."""
    AppConfig._warned_retired.clear()


@pytest.fixture
def retired(monkeypatch: pytest.MonkeyPatch) -> tuple[RetiredKey, ...]:
    """Two retired keys: one under an ``ignore`` section, one under a ``forbid`` one."""
    registry = (
        RetiredKey(path=("compaction", "caveman_mode"), guidance="Nothing ever read it."),
        RetiredKey(path=("wiki", "refresh", "default_mode"), guidance="Never threaded."),
    )
    monkeypatch.setattr(config_module, "_RETIRED_KEYS", registry)
    return registry


class TestRetiredKeyPrune:
    def test_a_key_under_a_forbid_section_no_longer_refuses_to_load(
        self, retired: tuple[RetiredKey, ...]
    ) -> None:
        # The premise: without pruning this exact payload is a hard failure.
        with pytest.raises(ValueError):
            WikiRefreshConfig.model_validate({"default_mode": "fast"})

        cfg = AppConfig.model_validate({"wiki": {"refresh": {"default_mode": "fast"}}})

        assert cfg.wiki.refresh is not None

    def test_a_key_under_an_ignore_section_loads_and_is_dropped(
        self, retired: tuple[RetiredKey, ...]
    ) -> None:
        cfg = AppConfig.model_validate({"compaction": {"caveman_mode": True}})

        # Pruned before validation, so the section falls back to its own default
        # rather than carrying the retired value forward.
        assert cfg.compaction.caveman_mode is False

    def test_the_callers_document_is_never_mutated(
        self, retired: tuple[RetiredKey, ...]
    ) -> None:
        # The settings PATCH path persists the very dict it validates, so a
        # prune-in-place would silently edit an operator's app.json.
        document = {"compaction": {"caveman_mode": True}, "api": {}}

        AppConfig.model_validate(document)

        assert document == {"compaction": {"caveman_mode": True}, "api": {}}

    def test_an_unrelated_key_in_the_same_section_survives(
        self, retired: tuple[RetiredKey, ...]
    ) -> None:
        cfg = AppConfig.model_validate(
            {"wiki": {"refresh": {"default_mode": "fast", "closure_max_depth": 7}}}
        )

        assert cfg.wiki.refresh.closure_max_depth == 7


class TestRetiredKeyAnnouncement:
    def test_setting_a_retired_key_warns_once_naming_the_key_and_the_guidance(
        self, retired: tuple[RetiredKey, ...], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="core.config"):
            AppConfig.model_validate({"compaction": {"caveman_mode": True}})
            AppConfig.model_validate({"compaction": {"caveman_mode": True}})

        warnings = [r for r in caplog.records if "compaction.caveman_mode" in r.getMessage()]
        assert len(warnings) == 1
        assert "Nothing ever read it." in warnings[0].getMessage()

    def test_a_config_that_sets_no_retired_key_says_nothing(
        self, retired: tuple[RetiredKey, ...], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="core.config"):
            AppConfig.model_validate({"compaction": {}})

        assert [r for r in caplog.records if "retired" in r.getMessage()] == []


class TestRetiredKeyRegistry:
    def test_every_declared_retired_key_is_absent_from_the_shipped_example(self) -> None:
        """A retired key left in ``app.example.json`` hands new installs a dead knob."""
        example = json.loads(
            (Path(__file__).resolve().parents[1] / "configs" / "app.example.json").read_text()
        )

        for key in config_module._RETIRED_KEYS:
            node = example
            for segment in key.path:
                if not isinstance(node, dict) or segment not in node:
                    node = None
                    break
                node = node[segment]
            assert node is None, f"{key.dotted} is retired but still shipped in app.example.json"

    def test_a_retired_key_must_declare_where_it_lived(self) -> None:
        with pytest.raises(ValueError):
            RetiredKey(path=(), guidance="nowhere")
