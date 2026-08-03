#!/usr/bin/env python3
"""Generate JSON Schema from the AppConfig Pydantic model.

Performs an AST pre-check to verify that the AppConfig class exists in the
source file before importing it.  Writes the schema to ``configs/app.schema.json``.

Exit codes (matched with ``generate_openapi_spec.py``):

- ``0`` — the artifact is already current, OR it was rewritten. Rewriting is
  this script's job, so it is success. Which of the two happened is reported on
  stdout, where a human or a log reader can see it.
- non-zero — a genuine error, surfacing as an uncaught traceback.

Conflating "regenerated" with "failed" under one exit code forces every caller
to mask this script with ``|| true``, which then also swallows real breakage.

``--check`` writes nothing and exits non-zero when the committed artifact would
change — a local check to run before opening a change, rather than an enforced
gate. Its non-zero is coarse on purpose: "stale" and "the generator failed"
share it, so fail closed on it rather than branching on it.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_MODULE_PATH = (
    REPO_ROOT / "packages" / "mewbo_core" / "src" / "mewbo_core" / "config.py"
)
SCHEMA_OUTPUT_PATH = REPO_ROOT / "configs" / "app.schema.json"
SCHEMA_ID = "https://thekrishna.in/Assistant/latest/app.schema.json"


# ---------------------------------------------------------------------------
# 1. AST guard – ensure AppConfig class still exists in the source
# ---------------------------------------------------------------------------
def _ast_check() -> None:
    source = CONFIG_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONFIG_MODULE_PATH))
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    if "AppConfig" not in class_names:
        raise RuntimeError(
            f"AppConfig class not found in {CONFIG_MODULE_PATH}. "
            "The config model may have been renamed or removed — update this script."
        )


# ---------------------------------------------------------------------------
# 2. Generate schema via native Pydantic mechanism
# ---------------------------------------------------------------------------
def _generate_schema() -> str:
    from mewbo_core.config import AppConfig

    schema = AppConfig.model_json_schema()
    # Place $id first for readability; JSON Schema processors
    # are order-agnostic but humans read top-down.
    ordered = {"$id": SCHEMA_ID, **schema}
    return json.dumps(ordered, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 3. Compare and write
# ---------------------------------------------------------------------------
def main() -> int:
    """Generate the AppConfig JSON schema and write it to disk."""
    parser = argparse.ArgumentParser(
        description="Generate configs/app.schema.json from the AppConfig model."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Write nothing; exit non-zero if the committed schema is stale.",
    )
    args = parser.parse_args()

    # Errors below are deliberately left to propagate: an uncaught traceback is
    # the non-zero exit that tells a caller the artifact was never produced.
    _ast_check()
    new_schema = _generate_schema()

    if SCHEMA_OUTPUT_PATH.exists():
        old_schema = SCHEMA_OUTPUT_PATH.read_text(encoding="utf-8")
        if old_schema == new_schema:
            print(f"Schema unchanged: {SCHEMA_OUTPUT_PATH}")
            return 0

    if args.check:
        print(f"Schema STALE: {SCHEMA_OUTPUT_PATH}")
        print("Regenerate with: uv run python scripts/ci/generate_config_schema.py")
        return 1

    SCHEMA_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_OUTPUT_PATH.write_text(new_schema, encoding="utf-8")
    print(f"Schema updated: {SCHEMA_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
