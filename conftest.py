"""Repository-wide pytest configuration."""
# ruff: noqa: E402, I001

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Pin MEWBO_HOME to a throwaway dir before any test module is imported.
# apps/mewbo_api/src/mewbo_api/backend.py constructs its session/key/project
# stores as MODULE-LEVEL singletons (``key_store = create_key_store()`` and
# friends), so the first import of that module -- which happens during
# collection, before any fixture (autouse or not) gets a chance to run --
# resolves MEWBO_HOME once, for the life of the process. A per-test fixture is
# structurally too late for this: `pytest --collect-only` alone (zero
# fixtures, zero test bodies executed) was enough to write into the
# developer's real ~/.mewbo. This module-scope statement is the earliest seam
# that can reach it. ``setdefault`` respects an operator/CI-supplied value.
_pytest_home = tempfile.mkdtemp(prefix="mewbo-pytest-home-")
atexit.register(shutil.rmtree, _pytest_home, ignore_errors=True)
os.environ.setdefault("MEWBO_HOME", _pytest_home)

ROOT = os.path.abspath(os.path.dirname(__file__))
SOURCE_PATHS = [
    ROOT,
    os.path.join(ROOT, "packages", "mewbo_core", "src"),
    os.path.join(ROOT, "packages", "mewbo_tools", "src"),
    os.path.join(ROOT, "apps", "mewbo_cli", "src"),
    os.path.join(ROOT, "apps", "mewbo_api", "src"),
    os.path.join(ROOT, "apps", "mewbo_mcp", "src"),
    os.path.join(ROOT, "apps"),
]
for path in SOURCE_PATHS:
    if path not in sys.path:
        sys.path.insert(0, path)

# Wiki test fixtures embed a fake repo (``tests/wiki/fixtures/tiny_repo``) that
# ships its own ``tests/test_main.py`` as *data* for the scanner — it is not a
# Mewbo test. Excluding it from collection avoids a module-name collision
# (``tests.test_main``) that pytest's prepend import mode hits once another
# top-level ``tests`` package is on sys.path.
collect_ignore_glob = ["tests/wiki/fixtures/*"]

# ``mongomock`` — the Mongo double every store suite runs against — lags
# pymongo's operation models. pymongo's ``UpdateOne`` unconditionally forwards a
# ``sort`` argument into the bulk builder, and mongomock's ``add_update`` has no
# such parameter, so ANY ``bulk_write`` of updates raises ``TypeError`` against
# the double while working against a real server. Absorb the argument once,
# here, rather than letting a driver method avoid ``bulk_write`` to keep a test
# double happy. A non-``None`` sort would change what the operation means, so it
# is refused instead of dropped — mongomock cannot honour it either way.
try:
    from mongomock.collection import BulkOperationBuilder as _MongomockBulk
except Exception:  # pragma: no cover - mongomock is a dev-only dependency
    pass
else:
    import inspect as _inspect

    if "sort" not in _inspect.signature(_MongomockBulk.add_update).parameters:
        _mongomock_add_update = _MongomockBulk.add_update

        def _add_update_absorbing_sort(self, *args, sort=None, **kwargs):
            """Drop pymongo's unsupported ``sort`` before mongomock sees it."""
            if sort is not None:
                raise NotImplementedError("mongomock cannot apply a sorted bulk update")
            return _mongomock_add_update(self, *args, **kwargs)

        _MongomockBulk.add_update = _add_update_absorbing_sort

import pytest

from mewbo_core.config import (
    AppConfig,
    reset_config,
    set_app_config_path,
    set_mcp_config_path,
)


_pytest_configs = Path(_pytest_home) / "configs"
_pytest_configs.mkdir(parents=True, exist_ok=True)
(_pytest_configs / "app.json").write_text("{}\n")
os.environ["MEWBO_CONFIG_DIR"] = str(_pytest_configs)
set_app_config_path(_pytest_configs / "app.json")
# Pin the config DIRECTORY at import time, for the same reason MEWBO_HOME is
# pinned above and not by a fixture: ``mewbo_api.backend`` calls
# ``get_config()`` at MODULE level, so the first import of it during collection
# resolves a config before any fixture can redirect one. Left alone, the chain
# walks up from CWD and finds the DEVELOPER'S OWN ``configs/app.json`` — a live,
# secret-bearing file the suite has no business reading, and one whose values a
# test would then silently assert against.
#
# It is set as an ENV VAR and not only via ``set_app_config_path`` because a
# large part of this suite probes behaviour in SUBPROCESSES, and an in-process
# override cannot reach them: each child re-runs the walk from its own CWD —
# the repo root — and finds that same config again. The env var is the only
# form of the redirect a child inherits.
#
# It ASSIGNS rather than ``setdefault``s, and the difference is the whole point
# of the pin. ``setdefault`` lets an inherited value win, so the one environment
# the isolation exists to defend against — a developer's shell, already carrying
# a redirect at their own live config — is exactly the one where it silently did
# nothing. Nothing supplies this variable ahead of a run: no workflow and no
# compose file sets it for pytest, and ``scripts/ci/generate_openapi_spec.py``
# sets it in-process long after this line, save-and-restore, so it is unaffected.
# The deliberate redirects both still work: ``--config`` on the CLI, and
# ``monkeypatch.setenv`` inside a test that means to probe the chain itself.


@pytest.fixture(autouse=True)
def app_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a fresh app config file and point the loader at it.

    Also pins ``MEWBO_HOME`` to a throwaway dir *before* constructing
    ``AppConfig()``. ``RuntimeConfig``'s ``config_dir``/``cache_dir``/
    ``session_dir`` fields default to ``""`` and a ``field_validator`` resolves
    that to ``resolve_mewbo_home()`` at construction time — the resolved
    absolute path is what gets baked into the JSON this fixture writes, not a
    placeholder that re-resolves later. A test that sets ``MEWBO_HOME`` itself
    inside its own body does so too late for anything reading `config_dir`
    (every ``_JsonCollectionStore`` subclass in ``mewbo_iam.stores``) — without
    this, those stores fall back to the developer's real ``~/.mewbo``.
    """
    monkeypatch.setenv("MEWBO_HOME", str(tmp_path / "home"))
    reset_config()
    config_path = tmp_path / "app.json"
    AppConfig().write(config_path)
    set_app_config_path(config_path)
    set_mcp_config_path(tmp_path / "mcp.json")
    yield config_path
    reset_config()
