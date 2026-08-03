"""Landlock scoping for the wiki git seam — ``run_git_with_chain`` + ``_run_local_git``.

Proves the fix added to ``plugins/wiki/clone.py`` for a spawn seam that
bypassed workspace scoping: both spawn sites pass the directory the call
legitimately works in (the clone/
checkout target) as the active root, so it stays reachable even when it
coincides with a DENIED configured project — the ``RepositoryCheckout`` shape
("clones straight into the project's own path") — while every OTHER
configured project stays denied. Style mirrors ``tests/test_shell_landlock.py``:
real processes, skipped when Landlock is unavailable.

``run_git_with_chain``'s own argv is caller-supplied (``build_argv``), so the
refusal/flag-off tests hand it a plain python read-probe instead of a real git
invocation — isolating the Landlock composition from git's own behaviour,
which is unnecessary here since ``build_argv`` never validates what it
returns (unlike ``PipelineSpec.check_exec_allowed`` — see
``tests/apps/test_apps_pipeline_runner_landlock.py`` for why THAT seam needs
the split).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from mewbo_core.config import reset_config, set_config_override
from mewbo_graph.plugins.wiki.clone import (
    _run_local_git,
    clone_with_fallback,
    run_git_with_chain,
)
from mewbo_tools.integration.landlock import LandlockAbi

_HAS_LANDLOCK = sys.platform.startswith("linux") and LandlockAbi.probe().available
requires_landlock = pytest.mark.skipif(
    not _HAS_LANDLOCK, reason="Landlock is a Linux-only kernel feature and is unavailable here"
)


def _read_probe(path: Path) -> list[str]:
    """argv for a python subprocess that reads *path* and reports the outcome.

    Prints the file's content on success; on any ``OSError`` (Landlock's
    denial surfaces as ``PermissionError``, a subclass) writes to stderr and
    exits 1.
    """
    return [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "try:\n"
            "    sys.stdout.write(open(sys.argv[1]).read())\n"
            "except OSError as exc:\n"
            "    sys.stderr.write(str(exc))\n"
            "    sys.exit(1)\n"
        ),
        str(path),
    ]


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare git repo with one commit — the clone source, entirely offline."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=seed, check=True)
    (seed / "README.md").write_text("hello\n")
    env = ["-c", "user.email=a@b.c", "-c", "user.name=a"]
    subprocess.run(["git", *env, "add", "README.md"], cwd=seed, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "x"], cwd=seed, check=True)
    subprocess.run(
        ["git", "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True
    )
    return remote


@pytest.fixture
def beta(tmp_path: Path) -> Path:
    d = tmp_path / "beta"
    d.mkdir()
    (d / "secret.env").write_text("SECRET_BETA=value\n")
    return d


def _configure(*, checkout: Path, beta: Path, shell_sandbox: bool = True) -> None:
    set_config_override(
        {
            "agent": {"shell_sandbox": shell_sandbox},
            "projects": {
                "checkout": {"path": str(checkout)},
                "beta": {"path": str(beta)},
            },
        }
    )


class TestCloneLandlockScope:
    def teardown_method(self):
        reset_config()

    @requires_landlock
    def test_the_clone_target_stays_reachable_even_though_it_is_a_denied_project(
        self, tmp_path, bare_remote, beta
    ):
        # `checkout` is deliberately ALSO a registered configured project —
        # the RepositoryCheckout shape. Without threading `active_root=target`
        # through to `run_git_with_chain`, it would sit in `ShellScope`'s
        # denied set like every other configured project and the clone could
        # never write into its own destination.
        target = tmp_path / "checkout"
        _configure(checkout=target, beta=beta)
        outcome = clone_with_fallback(
            f"file://{bare_remote}", target, ref=None, store=None, slug="acme/checkout"
        )
        assert outcome.ok, outcome.stderr
        assert (target / "README.md").exists()

    @requires_landlock
    def test_a_different_configured_project_is_refused(self, tmp_path, beta):
        target = tmp_path / "checkout"
        target.mkdir()
        _configure(checkout=target, beta=beta)
        outcome = run_git_with_chain(
            None,
            "acme/checkout",
            "unused://url",
            lambda _authed: _read_probe(beta / "secret.env"),
            timeout=30,
            active_root=target,
        )
        assert not outcome.ok
        assert "Permission" in outcome.stderr_redacted
        assert "SECRET_BETA" not in outcome.stdout

    @requires_landlock
    def test_flag_off_leaves_the_spawn_unscoped(self, tmp_path, beta):
        target = tmp_path / "checkout"
        target.mkdir()
        _configure(checkout=target, beta=beta, shell_sandbox=False)
        outcome = run_git_with_chain(
            None,
            "acme/checkout",
            "unused://url",
            lambda _authed: _read_probe(beta / "secret.env"),
            timeout=30,
            active_root=target,
        )
        assert outcome.ok, outcome.stderr_redacted
        assert "SECRET_BETA" in outcome.stdout

    @requires_landlock
    def test_run_local_git_scopes_to_the_clone_dir(self, tmp_path, beta):
        # `_run_local_git` is the SECOND spawn site in this module —
        # `clone_at_sha`'s local `init`/`checkout FETCH_HEAD` calls. Its own
        # `clone_dir` argument IS the active root, so the only property it can
        # prove is the positive one: a registered-and-otherwise-denied project
        # stays usable for the local git calls that build it.
        target = tmp_path / "checkout"
        target.mkdir()
        _configure(checkout=target, beta=beta)
        assert _run_local_git(target, ["init", "-q"]) == ""
        assert _run_local_git(target, ["rev-parse", "--is-inside-work-tree"]) == "true"
