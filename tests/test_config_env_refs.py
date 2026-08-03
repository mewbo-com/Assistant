"""Contract tests for environment-variable references in ``config.py``.

A config value may name an environment variable instead of holding a literal,
so a secret can live in the environment and out of ``app.json``. The properties
worth pinning are the two limits that keep it a naming convention rather than a
template language, and the refusal that keeps a missing variable from turning
into a puzzling failure much later.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mewbo_core import config as config_module
from mewbo_core.config import AppConfig, EnvRef, RetiredKey


class TestEnvRefParse:
    @pytest.mark.parametrize(
        "value,variable",
        [("${TOKEN}", "TOKEN"), ("${A_B_2}", "A_B_2"), ("  ${TOKEN}  ", "TOKEN")],
    )
    def test_a_whole_value_reference_is_recognised(self, value: str, variable: str) -> None:
        parsed = EnvRef.parse(value)

        assert parsed is not None
        assert parsed.variable == variable

    @pytest.mark.parametrize(
        "value",
        [
            "prefix-${TOKEN}",  # the whole-value rule: not a reference
            "${TOKEN}-suffix",
            "pa$$word",  # a literal that happens to contain the sigil
            "${2BAD}",  # not an identifier
            "${}",
            "$TOKEN",
            "",
            42,
            None,
        ],
    )
    def test_anything_else_is_a_literal(self, value: object) -> None:
        assert EnvRef.parse(value) is None


class TestEnvRefResolution:
    def test_a_reference_resolves_to_the_variables_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEWBO_TEST_TOKEN", "from-the-environment")

        cfg = AppConfig.model_validate({"api": {"master_token": "${MEWBO_TEST_TOKEN}"}})

        assert cfg.api.master_token == "from-the-environment"

    def test_an_unset_variable_is_refused_naming_both_it_and_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MEWBO_TEST_ABSENT", raising=False)

        with pytest.raises(ValueError) as excinfo:
            AppConfig.model_validate({"api": {"master_token": "${MEWBO_TEST_ABSENT}"}})

        message = str(excinfo.value)
        assert "MEWBO_TEST_ABSENT" in message
        assert "api.master_token" in message

    def test_a_variable_set_to_empty_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The way to say a value is deliberately blank, distinct from absent."""
        monkeypatch.setenv("MEWBO_TEST_BLANK", "")

        cfg = AppConfig.model_validate({"api": {"apps_token_secret": "${MEWBO_TEST_BLANK}"}})

        assert cfg.api.apps_token_secret == ""

    def test_a_literal_containing_the_sigil_survives_untouched(self) -> None:
        literal = "pa$$w0rd-${not-a-name"

        cfg = AppConfig.model_validate({"api": {"master_token": literal}})

        assert cfg.api.master_token == literal

    def test_a_reference_is_resolved_inside_a_nested_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEWBO_TEST_MODEL", "some/model")

        cfg = AppConfig.model_validate(
            {"llm": {"fallback": {"models": ["${MEWBO_TEST_MODEL}", "literal/model"]}}}
        )

        assert cfg.llm.fallback.models == ["some/model", "literal/model"]

    def test_a_document_with_no_reference_is_not_copied(self) -> None:
        """The common case must not rebuild the caller's document."""
        payload = {"api": {"allow_external_cwd": True}}

        assert EnvRef.resolve_document(payload, os.environ) is payload

    def test_the_callers_document_is_never_mutated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEWBO_TEST_TOKEN", "from-the-environment")
        document = {"api": {"master_token": "${MEWBO_TEST_TOKEN}"}}

        AppConfig.model_validate(document)

        assert document == {"api": {"master_token": "${MEWBO_TEST_TOKEN}"}}


class TestEnvRefOrdering:
    def test_a_reference_under_a_retired_key_is_never_resolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key that exists only to be ignored must not be able to refuse a boot."""
        monkeypatch.delenv("MEWBO_TEST_ABSENT", raising=False)
        monkeypatch.setattr(
            config_module,
            "_RETIRED_KEYS",
            (RetiredKey(path=("compaction", "caveman_mode"), guidance="gone"),),
        )

        cfg = AppConfig.model_validate(
            {"compaction": {"caveman_mode": "${MEWBO_TEST_ABSENT}"}}
        )

        assert cfg.compaction.caveman_mode is False


class TestConfigDirPin:
    """``MEWBO_CONFIG_DIR`` is the only redirect a spawned process inherits."""

    def test_the_pin_wins_over_the_cwd_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pinned = tmp_path / "pinned"
        (pinned).mkdir()
        (pinned / "app.json").write_text("{}\n")
        monkeypatch.setenv("MEWBO_CONFIG_DIR", str(pinned))

        assert config_module._resolve_config_path("app.json") == pinned / "app.json"

    def test_without_the_pin_the_walk_still_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MEWBO_CONFIG_DIR", raising=False)
        project = tmp_path / "project"
        (project / "configs").mkdir(parents=True)
        (project / "configs" / "app.json").write_text("{}\n")
        monkeypatch.chdir(project)

        assert config_module._resolve_config_path("app.json") == project / "configs" / "app.json"

    def test_a_subprocess_inherits_the_pin(self, tmp_path: Path) -> None:
        """The property an in-process override cannot have, and the reason this exists."""
        pinned = tmp_path / "pinned"
        pinned.mkdir()
        (pinned / "app.json").write_text('{"runtime": {"log_level": "CRITICAL"}}\n')
        probe = (
            "from mewbo_core.config import get_app_config_path; print(get_app_config_path())"
        )

        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "MEWBO_CONFIG_DIR": str(pinned)},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(pinned / "app.json")

    def test_a_pin_at_a_missing_directory_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse, because the silent alternative is the worst outcome available.

        An unreadable config path loads as an empty document, so a wrong pin
        would bring the process up healthy on built-in defaults — reaching no
        gateway and holding no operator setting, with nothing saying why.
        """
        monkeypatch.setenv("MEWBO_CONFIG_DIR", str(tmp_path / "nope"))

        with pytest.raises(ValueError, match="MEWBO_CONFIG_DIR"):
            config_module._resolve_config_path("app.json")

    def test_a_missing_FILE_in_a_real_directory_is_fine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh install scaffolds its config; that is not a misconfiguration."""
        monkeypatch.setenv("MEWBO_CONFIG_DIR", str(tmp_path))

        assert config_module._resolve_config_path("app.json") == tmp_path / "app.json"
