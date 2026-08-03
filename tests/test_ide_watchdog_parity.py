"""The container watchdog exists in two languages and must not drift.

``WATCHDOG_CMD`` is declared twice — once in ``mewbo_api.ide`` (the local
Docker-SDK backend) and once in ``apps/mewbo_ide/src/containers.ts`` (the
broker that owns container creation in a deployed stack). Both strings become
the entrypoint of the SAME code-server image, so a change to one and not the
other produces two silently different IDE behaviours depending on which backend
happens to be configured.

Nothing else binds them: they are separate files in separate toolchains, and a
divergence compiles, type-checks and passes both suites. This test is the seam
— it parses both declarations and asserts they are byte-identical, following
the house rule that a contract shared by two surfaces gets RUN rather than
read.

The failure it guards is quiet: a shortened poll interval or a renamed flag on
one side would only surface as "the IDE dies early" or "the proxy 502s" on
whichever deployments use that backend.

⚠️ **A near-identical check exists in ``apps/mewbo_ide/src/__tests__/containers.test.ts``
and the pair is NOT redundant — deleting either opens a real gap.** The two CI
workflows are path-filtered to disjoint trees, so each test guards the direction
the other cannot see:

===========================================  ==================  ===============
Change                                       Workflow that runs  Test that fires
===========================================  ==================  ===============
only ``apps/mewbo_api/**`` (this side)       ``coverage.yml``    this one
only ``apps/mewbo_ide/**`` (the broker)      ``mewbo-ide.yml``   the vitest one
===========================================  ==================  ===============

``coverage.yml`` does not list ``apps/mewbo_ide/**``, and ``mewbo-ide.yml`` does
not list ``apps/mewbo_api/**``. So an edit to the Python constant alone never
runs the TypeScript check, and an edit to the TypeScript constant alone never
runs this one. Two tests, one fact, two triggers — that is the reason, and it
stops being true the moment either workflow's ``paths:`` list changes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_SOURCE = REPO_ROOT / "apps/mewbo_api/src/mewbo_api/ide.py"
TYPESCRIPT_SOURCE = REPO_ROOT / "apps/mewbo_ide/src/containers.ts"

#: Both declarations are a run of adjacent double-quoted fragments — implicit
#: concatenation in Python, ``+`` in TypeScript — so one fragment pattern reads
#: both once the surrounding declaration has been isolated.
_FRAGMENT_RE = re.compile(r'"([^"]*)"')

_PYTHON_DECL_RE = re.compile(r"^WATCHDOG_CMD = \(\n(?P<body>.*?)^\)", re.MULTILINE | re.DOTALL)
_TYPESCRIPT_DECL_RE = re.compile(
    r"^export const WATCHDOG_CMD =\n(?P<body>.*?);$", re.MULTILINE | re.DOTALL
)


def _extract(source: Path, declaration: re.Pattern[str]) -> str:
    """Return the concatenated watchdog string declared in ``source``.

    Fails loudly when the declaration cannot be found: a refactor that renames
    or reshapes it must not turn this test into a silent no-op, which is the
    usual way a parity check stops checking anything.
    """
    if not source.exists():
        pytest.fail(f"watchdog declaration source is missing: {source}")
    match = declaration.search(source.read_text(encoding="utf-8"))
    if match is None:
        pytest.fail(
            f"could not locate the WATCHDOG_CMD declaration in {source}. "
            "If its shape changed, update this test's parser rather than deleting it — "
            "the two declarations still have to agree."
        )
    fragments = _FRAGMENT_RE.findall(match.group("body"))
    if not fragments:
        pytest.fail(f"WATCHDOG_CMD in {source} declared no string fragments")
    return "".join(fragments)


def test_watchdog_command_is_byte_identical_across_languages() -> None:
    """The Python and TypeScript watchdog strings must match exactly."""
    python_cmd = _extract(PYTHON_SOURCE, _PYTHON_DECL_RE)
    typescript_cmd = _extract(TYPESCRIPT_SOURCE, _TYPESCRIPT_DECL_RE)

    assert python_cmd == typescript_cmd, (
        "The container watchdog has drifted between the Python backend and the "
        "IDE broker. Both are the entrypoint of the same image, so they must "
        "stay identical.\n"
        f"  python:     {python_cmd!r}\n"
        f"  typescript: {typescript_cmd!r}"
    )


def test_watchdog_command_keeps_the_contract_its_collaborators_depend_on() -> None:
    """Guard the three substrings other components read the watchdog through.

    Each is a cross-component coupling that a plausible edit would break:

    - ``/mewbo/deadline`` is the bind target both backends mount the deadline
      file at; renaming it here alone leaves the watchdog reading nothing.
    - ``--abs-proxy-base-path /ide/{sid}`` is the prefix ``nginx-ide-proxy.conf``
      strips before forwarding, so the two must agree or every asset 404s.
    - ``--bind-addr 0.0.0.0:8080`` is the port that proxy dials.
    """
    python_cmd = _extract(PYTHON_SOURCE, _PYTHON_DECL_RE)

    assert "/mewbo/deadline" in python_cmd
    assert "--abs-proxy-base-path /ide/{sid}" in python_cmd
    assert "--bind-addr 0.0.0.0:8080" in python_cmd
