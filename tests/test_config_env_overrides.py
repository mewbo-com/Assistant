"""Contract tests for the declarative environment-variable overrides.

Distinct from ``test_config_env_refs.py``: an ``EnvRef`` is an operator writing
``${VAR}`` as a value and is refused when the variable is unset, whereas an
override is a FIELD declaring a variable that wins over whatever the file says,
where unset means "not overridden". These pin that direction, the declaration
reaching the JSON schema, and each migrated field individually — the four used
to be hand-written validators, so a silent regression would look exactly like a
deployment that never exported the variable.
"""

from __future__ import annotations

from typing import Any

import pytest
from mewbo_core.config import (
    AgentConfig,
    EnvOverridable,
    MongoDBConfig,
    StorageConfig,
    WebIdeConfig,
)
from pydantic import Field

# (model, field, variable, value the env sets, what the file says instead)
MIGRATED: list[tuple[type[EnvOverridable], str, str, str, Any]] = [
    (WebIdeConfig, "broker_url", "MEWBO_IDE_BROKER_URL", "http://broker:5128", "http://file:1"),
    (AgentConfig, "llm_call_timeout", "MEWBO_AGENT_LLM_CALL_TIMEOUT", "900", 30.0),
    (MongoDBConfig, "uri", "MEWBO_MONGODB_URI", "mongodb://env:27017", "mongodb://file:27017"),
    (MongoDBConfig, "database", "MEWBO_MONGODB_DATABASE", "envdb", "filedb"),
    (StorageConfig, "driver", "MEWBO_STORAGE_DRIVER", "mongodb", "json"),
]

IDS = [f"{model.__name__}.{name}" for model, name, *_ in MIGRATED]


def _expected(model: type[EnvOverridable], name: str, raw: str) -> Any:
    """The env string as the field's own validators render it."""
    return model.model_validate({name: raw}).model_dump()[name]


@pytest.mark.parametrize("model,name,variable,raw,from_file", MIGRATED, ids=IDS)
class TestMigratedFields:
    def test_the_variable_wins_over_the_file_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: type[EnvOverridable],
        name: str,
        variable: str,
        raw: str,
        from_file: Any,
    ) -> None:
        monkeypatch.setenv(variable, raw)

        loaded = model.model_validate({name: from_file})

        assert getattr(loaded, name) == _expected(model, name, raw)

    def test_the_variable_wins_when_the_key_is_absent_entirely(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: type[EnvOverridable],
        name: str,
        variable: str,
        raw: str,
        from_file: Any,
    ) -> None:
        # The common `model_validate({})` path — an operator who never wrote the
        # key still gets the override, which is the whole point of deploying one.
        monkeypatch.setenv(variable, raw)

        loaded = model.model_validate({})

        assert getattr(loaded, name) == _expected(model, name, raw)

    def test_an_unset_variable_leaves_the_file_value_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: type[EnvOverridable],
        name: str,
        variable: str,
        raw: str,
        from_file: Any,
    ) -> None:
        monkeypatch.delenv(variable, raising=False)

        loaded = model.model_validate({name: from_file})

        assert getattr(loaded, name) == _expected(model, name, str(from_file))

    def test_an_unset_variable_leaves_the_default_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: type[EnvOverridable],
        name: str,
        variable: str,
        raw: str,
        from_file: Any,
    ) -> None:
        monkeypatch.delenv(variable, raising=False)

        assert getattr(model.model_validate({}), name) == model.model_fields[name].default

    def test_an_empty_variable_reads_as_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: type[EnvOverridable],
        name: str,
        variable: str,
        raw: str,
        from_file: Any,
    ) -> None:
        # An exported-but-blank variable is how a shell says nothing.
        monkeypatch.setenv(variable, "")

        loaded = model.model_validate({name: from_file})

        assert getattr(loaded, name) == _expected(model, name, str(from_file))

    def test_the_declaration_reaches_the_json_schema(
        self,
        model: type[EnvOverridable],
        name: str,
        variable: str,
        raw: str,
        from_file: Any,
    ) -> None:
        node = model.model_json_schema()["properties"][name]

        assert node["x-env-var"] == variable


class TestTheSeam:
    def test_a_field_that_declares_nothing_is_never_touched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEWBO_STORAGE_DRIVER", "mongodb")

        assert MongoDBConfig.model_validate({"database": "kept"}).database == "kept"

    def test_a_declaration_on_a_plain_model_does_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The base class is what applies a declaration; this is the trap the
        # annotation-contract comment warns about, pinned rather than described.
        class Plain(EnvOverridable):
            pass

        assert "x-env-var" not in str(Plain.model_json_schema())

    def test_the_overridden_value_still_runs_the_field_validators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bad variable is refused exactly as a bad file value is, rather than
        # bypassing validation on its way in.
        monkeypatch.setenv("MEWBO_STORAGE_DRIVER", "cassandra")

        with pytest.raises(ValueError, match="Unknown storage driver"):
            StorageConfig.model_validate({"driver": "json"})

    def test_adding_an_override_is_a_declaration_not_new_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Section(EnvOverridable):
            knob: str = Field("default", json_schema_extra={"x-env-var": "MEWBO_TEST_KNOB"})

        monkeypatch.setenv("MEWBO_TEST_KNOB", "from-env")
        assert Section.model_validate({"knob": "from-file"}).knob == "from-env"

        monkeypatch.delenv("MEWBO_TEST_KNOB")
        assert Section.model_validate({"knob": "from-file"}).knob == "from-file"
        assert Section.model_json_schema()["properties"]["knob"]["x-env-var"] == "MEWBO_TEST_KNOB"

    def test_a_non_dict_payload_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEWBO_MONGODB_DATABASE", "envdb")
        original = MongoDBConfig.model_validate({})

        assert MongoDBConfig.model_validate(original).database == original.database
