"""The config ceiling on a memory note's length must match the model's own.

``WikiMemoryConfig.max_insight_chars`` carries ``le=200`` as a literal, because
``config.py`` lives in core and the canonical constant lives in graph, which
sits above it. Only a test can hold the two together, and this is it: core
cannot import graph, but a test may import both.

Without the bound the field is worse than dead. A value above the model's
declared length passes config validation, passes the ingestor's own length
check, and then fails as the note is written — turning an operator's setting
into an error at the far end of a pipeline rather than at the file they edited.
"""

from __future__ import annotations

import pytest
from mewbo_core.config import WikiMemoryConfig
from mewbo_graph.wiki.memory_types import MAX_INSIGHT_CHARS


class TestMaxInsightCharsCeiling:
    def test_the_config_bound_equals_the_models_declared_length(self) -> None:
        bound = WikiMemoryConfig.model_fields["max_insight_chars"].metadata

        ceilings = [getattr(m, "le", None) for m in bound]
        assert MAX_INSIGHT_CHARS in ceilings, (
            f"WikiMemoryConfig.max_insight_chars must cap at MAX_INSIGHT_CHARS "
            f"({MAX_INSIGHT_CHARS}); found {ceilings}. The two are mirrored by "
            "value because core cannot import graph — move them together."
        )

    def test_a_value_above_the_ceiling_is_refused_at_the_config_boundary(self) -> None:
        # Refused where the operator can see it, rather than accepted here and
        # raised later while a note is being written.
        with pytest.raises(ValueError):
            WikiMemoryConfig.model_validate({"max_insight_chars": MAX_INSIGHT_CHARS + 1})

    def test_lowering_is_allowed_because_that_is_the_direction_that_works(self) -> None:
        cfg = WikiMemoryConfig.model_validate({"max_insight_chars": 80})

        assert cfg.max_insight_chars == 80

    def test_zero_is_refused(self) -> None:
        with pytest.raises(ValueError):
            WikiMemoryConfig.model_validate({"max_insight_chars": 0})
