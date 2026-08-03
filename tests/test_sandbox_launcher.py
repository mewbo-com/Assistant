"""The launcher shim that confines servers Mewbo configures but does not spawn.

Five contracts, each of which is the reason a line of the shim exists:

1. **It always execs.** No Landlock, no scope in the environment, malformed
   JSON — every one of them still runs the real command, because a server that
   fails to start is a worse outcome than one that starts unconfined.
2. **When it does confine, the confinement survives the exec.** Proven by
   reading a denied file from the exec'd process, not by inspecting a ruleset.
3. **Only a spawn is wrapped.** An MCP server reached over HTTP starts no
   process here, so its config must come back untouched.
4. **The pool keeps the ORIGINAL config**, or every refresh would see the
   launch form differ from the configured form and reconnect the whole fleet.
5. **Gate off ⇒ byte-identical.** Both wiring sites return exactly what they
   would have returned before this existed.

The confinement tests exec REAL processes: the claim is about what the kernel
does across ``execve``, and a mocked spawn would prove nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.config import reset_config, set_config_override
from mewbo_tools.integration.landlock import ShellScope
from mewbo_tools.integration.lsp.manager import LSPServerManager
from mewbo_tools.integration.lsp.servers import ServerDef
from mewbo_tools.integration.mcp_pool import MCPConnectionPool
from mewbo_tools.integration.sandbox_launcher import SandboxLauncher
from test_shell_landlock import requires_landlock

_SHIM = (sys.executable, "-m", "mewbo_tools.integration.sandbox_launcher")


@pytest.fixture
def workspaces(tmp_path):
    """Two real checkouts, each holding a file worth not leaking."""
    made = {}
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "secret.env").write_text(f"SECRET_{name.upper()}=value\n")
        made[name] = str(d)
    return made


@pytest.fixture
def sandbox_on(workspaces):
    """Both gates on, with alpha and beta configured as projects."""
    set_config_override(
        {
            "agent": {"shell_sandbox": True, "server_sandbox": True},
            "projects": {k: {"path": v} for k, v in workspaces.items()},
        }
    )
    yield workspaces
    reset_config()


def _shim_run(argv: list[str], env_scope: str | None) -> subprocess.CompletedProcess:
    """Run the shim over *argv*, with ``MEWBO_SANDBOX_SCOPE`` set to *env_scope*."""
    env = dict(os.environ)
    env.pop(SandboxLauncher.ENV_VAR, None)
    if env_scope is not None:
        env[SandboxLauncher.ENV_VAR] = env_scope
    return subprocess.run(
        [*_SHIM, *argv], capture_output=True, text=True, timeout=120, env=env
    )


def _read_command(path: str) -> list[str]:
    """A command that prints a file, exiting non-zero when it cannot."""
    return [sys.executable, "-c", f"print(open({path!r}).read())"]


class TestItAlwaysExecs:
    """(1) Every degrade path still runs the real command."""

    def test_no_scope_in_the_environment_execs_unconfined(self):
        result = _shim_run([sys.executable, "-c", "print('EXECD')"], None)
        assert result.returncode == 0, result.stderr
        assert "EXECD" in result.stdout

    def test_unreadable_scope_execs_and_says_so_once(self):
        result = _shim_run([sys.executable, "-c", "print('EXECD')"], "{not json")
        assert result.returncode == 0, result.stderr
        assert "EXECD" in result.stdout
        assert result.stderr.count("mewbo-sandbox-exec:") == 1

    def test_a_scope_that_is_not_an_object_execs(self):
        result = _shim_run([sys.executable, "-c", "print('EXECD')"], '["nope"]')
        assert result.returncode == 0, result.stderr
        assert "EXECD" in result.stdout

    def test_an_empty_scope_execs(self):
        result = _shim_run([sys.executable, "-c", "print('EXECD')"], "{}")
        assert result.returncode == 0, result.stderr
        assert "EXECD" in result.stdout

    def test_no_command_at_all_refuses_rather_than_hanging(self):
        result = _shim_run([], None)
        assert result.returncode == 2
        assert "usage" in result.stderr

    def test_a_command_that_does_not_exist_reports_it(self):
        result = _shim_run(["mewbo-no-such-binary-anywhere"], None)
        assert result.returncode == 127
        assert "cannot execute" in result.stderr

    def test_stdout_carries_nothing_but_the_command_s_own_output(self):
        """stdout IS the protocol channel for both server kinds it launches."""
        result = _shim_run([sys.executable, "-c", "print('ONLY')"], "{not json")
        assert result.stdout == "ONLY\n"


class TestTheConfinementSurvivesTheExec:
    """(2) The exec'd process inherits the ruleset the shim applied to itself."""

    @requires_landlock
    def test_a_denied_file_is_unreadable_after_the_exec(self, workspaces):
        scope = json.dumps({"denied": [workspaces["beta"]], "allowed": []})
        result = _shim_run(_read_command(f"{workspaces['beta']}/secret.env"), scope)
        assert result.returncode != 0
        assert "SECRET_BETA" not in result.stdout

    @requires_landlock
    def test_everything_not_denied_stays_readable(self, workspaces):
        scope = json.dumps({"denied": [workspaces["beta"]], "allowed": []})
        result = _shim_run(_read_command(f"{workspaces['alpha']}/secret.env"), scope)
        assert result.returncode == 0, result.stderr
        assert "SECRET_ALPHA" in result.stdout

    @requires_landlock
    def test_an_allowed_path_under_a_denial_is_readable(self, workspaces, tmp_path):
        """The worktree shape: a grant beneath a denied directory still stands."""
        nested = tmp_path / "beta" / "worktree"
        nested.mkdir()
        (nested / "ok.txt").write_text("REACHABLE\n")
        scope = json.dumps({"denied": [workspaces["beta"]], "allowed": [str(nested)]})
        result = _shim_run(_read_command(f"{nested}/ok.txt"), scope)
        assert result.returncode == 0, result.stderr
        assert "REACHABLE" in result.stdout

    @requires_landlock
    def test_the_scope_variable_does_not_reach_the_server(self, workspaces):
        """It has done its job; the kernel is what every descendant inherits."""
        scope = json.dumps({"denied": [workspaces["beta"]], "allowed": []})
        result = _shim_run(
            [sys.executable, "-c", "import os;print(os.environ.get('MEWBO_SANDBOX_SCOPE'))"],
            scope,
        )
        assert result.stdout.strip() == "None"

    @requires_landlock
    def test_a_descendant_of_the_exec_d_process_is_confined_too(self, workspaces):
        """Confinement is inherited, so a server's own helpers are covered."""
        scope = json.dumps({"denied": [workspaces["beta"]], "allowed": []})
        inner = (
            "import subprocess,sys;"
            f"r=subprocess.run([sys.executable,'-c',\"open({workspaces['beta']!r}"
            "+'/secret.env').read()\"]);"
            "print('CHILD_EXIT', r.returncode)"
        )
        result = _shim_run([sys.executable, "-c", inner], scope)
        assert "CHILD_EXIT 0" not in result.stdout


class TestTheGate:
    """(5) Nothing changes until BOTH switches are on."""

    def teardown_method(self):
        reset_config()

    def test_off_by_default(self, workspaces):
        set_config_override({"projects": {"beta": {"path": workspaces["beta"]}}})
        assert SandboxLauncher.for_root(workspaces["alpha"]) is None

    def test_server_sandbox_alone_does_nothing(self, workspaces):
        set_config_override(
            {
                "agent": {"shell_sandbox": False, "server_sandbox": True},
                "projects": {"beta": {"path": workspaces["beta"]}},
            }
        )
        assert SandboxLauncher.for_root(workspaces["alpha"]) is None

    def test_shell_sandbox_alone_does_nothing(self, workspaces):
        set_config_override(
            {
                "agent": {"shell_sandbox": True, "server_sandbox": False},
                "projects": {"beta": {"path": workspaces["beta"]}},
            }
        )
        assert SandboxLauncher.for_root(workspaces["alpha"]) is None

    def test_both_on_scopes_to_the_served_root(self, sandbox_on):
        launcher = SandboxLauncher.for_root(sandbox_on["alpha"])
        assert launcher is not None
        assert sandbox_on["beta"] in launcher.scope.denied
        assert sandbox_on["alpha"] not in launcher.scope.denied

    def test_a_server_with_no_root_is_denied_every_project(self, sandbox_on):
        launcher = SandboxLauncher.for_root(None)
        assert launcher is not None
        # Every configured project, plus whatever the harness denies of itself.
        assert set(sandbox_on.values()) <= set(launcher.scope.denied)

    def test_the_environment_carries_exactly_the_two_scope_fields(self, sandbox_on):
        launcher = SandboxLauncher.for_root(sandbox_on["alpha"])
        assert launcher is not None
        payload = json.loads(launcher.environ({"KEEP": "me"})[SandboxLauncher.ENV_VAR])
        assert set(payload) == {"denied", "allowed"}
        assert payload["denied"] == list(launcher.scope.denied)

    def test_the_base_environment_is_copied_not_mutated(self, sandbox_on):
        launcher = SandboxLauncher.for_root(sandbox_on["alpha"])
        assert launcher is not None
        base = {"KEEP": "me"}
        merged = launcher.environ(base)
        assert base == {"KEEP": "me"}
        assert merged["KEEP"] == "me"


class TestMCPWiring:
    """(3) and (4): only stdio is wrapped, and the stored config stays original."""

    def teardown_method(self):
        reset_config()

    def test_a_stdio_server_is_routed_through_the_shim(self, sandbox_on):
        config = {"transport": "stdio", "command": "my-server", "args": ["--flag"]}
        wrapped = MCPConnectionPool._sandboxed(config)
        assert wrapped is not config
        assert [wrapped["command"], *wrapped["args"]][-2:] == ["my-server", "--flag"]
        assert SandboxLauncher.ENV_VAR in wrapped["env"]

    def test_a_transport_inferred_from_a_bare_command_is_wrapped(self, sandbox_on):
        wrapped = MCPConnectionPool._sandboxed({"command": "my-server"})
        assert wrapped["args"][-1:] == ["my-server"]

    @pytest.mark.parametrize(
        "config",
        [
            {"transport": "streamable_http", "url": "https://example.invalid/mcp"},
            {"transport": "sse", "url": "https://example.invalid/sse"},
            {"transport": "stdio", "command": ["not", "a", "string"]},
        ],
    )
    def test_a_server_that_spawns_nothing_here_is_untouched(self, sandbox_on, config):
        assert MCPConnectionPool._sandboxed(config) is config

    def test_the_gate_off_leaves_the_config_identical(self, workspaces):
        set_config_override({"projects": {"beta": {"path": workspaces["beta"]}}})
        config = {"transport": "stdio", "command": "my-server", "args": ["--flag"]}
        assert MCPConnectionPool._sandboxed(config) is config

    def test_a_server_s_own_cwd_is_the_scope(self, sandbox_on):
        wrapped = MCPConnectionPool._sandboxed(
            {"transport": "stdio", "command": "s", "cwd": sandbox_on["alpha"]}
        )
        payload = json.loads(wrapped["env"][SandboxLauncher.ENV_VAR])
        assert sandbox_on["beta"] in payload["denied"]
        assert sandbox_on["alpha"] not in payload["denied"]

    def test_the_config_s_own_env_survives(self, sandbox_on):
        wrapped = MCPConnectionPool._sandboxed(
            {"transport": "stdio", "command": "s", "env": {"TOKEN": "abc"}}
        )
        assert wrapped["env"]["TOKEN"] == "abc"

    def test_the_pool_stores_the_configured_form_not_the_launch_form(self, sandbox_on):
        """Otherwise every refresh sees a difference and reconnects the fleet."""
        config = {"transport": "stdio", "command": "my-server", "args": []}
        pool = MCPConnectionPool()
        seen: dict[str, dict] = {}

        class _Client:
            def __init__(self, servers):
                seen.update(servers)

            async def get_tools(self, server_name):
                return []

        with patch("langchain_mcp_adapters.client.MultiServerMCPClient", _Client):
            state = asyncio.run(pool._connect_single("s", config))

        assert state.config == config
        assert seen["s"]["command"] != "my-server"


class TestLSPWiring:
    """The command pygls is handed, with the sandbox on and off."""

    def teardown_method(self):
        reset_config()

    def _sdef(self) -> ServerDef:
        return ServerDef(
            id="fake",
            extensions=(".fake",),
            command=("fake-langserver", "--stdio"),
            root_markers=("pyproject.toml",),
            language_id="fake",
        )

    def test_the_command_is_prefixed_and_the_environment_carried_over(self, sandbox_on):
        command, kwargs = LSPServerManager._launch(self._sdef(), sandbox_on["alpha"])
        assert command[-2:] == ["fake-langserver", "--stdio"]
        assert len(command) > 2
        assert SandboxLauncher.ENV_VAR in kwargs["env"]
        assert "PATH" in kwargs["env"]

    def test_the_served_root_is_the_scope(self, sandbox_on):
        _, kwargs = LSPServerManager._launch(self._sdef(), sandbox_on["alpha"])
        payload = json.loads(kwargs["env"][SandboxLauncher.ENV_VAR])
        assert sandbox_on["beta"] in payload["denied"]
        assert sandbox_on["alpha"] not in payload["denied"]

    def test_the_gate_off_spawns_byte_identically(self, workspaces):
        set_config_override({"projects": {"beta": {"path": workspaces["beta"]}}})
        sdef = self._sdef()
        assert LSPServerManager._launch(sdef, workspaces["alpha"]) == (
            list(sdef.command),
            {},
        )


class TestTheShimIsInvokable:
    """Whichever way it resolves, the argv it produces must actually run."""

    def teardown_method(self):
        SandboxLauncher._shim_argv.cache_clear()
        reset_config()

    def test_the_resolved_shim_execs_a_real_command(self, sandbox_on):
        launcher = SandboxLauncher.for_root(sandbox_on["alpha"])
        assert launcher is not None
        argv = launcher.command([sys.executable, "-c", "print('RESOLVED')"])
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            env=launcher.environ(os.environ),
        )
        assert result.returncode == 0, result.stderr
        assert "RESOLVED" in result.stdout

    def test_the_module_fallback_is_used_when_the_script_is_absent(self):
        with patch("mewbo_tools.integration.sandbox_launcher.shutil.which", return_value=None):
            SandboxLauncher._shim_argv.cache_clear()
            argv = SandboxLauncher(scope=ShellScope(denied=())).command(["x"])
        assert argv[:3] == [sys.executable, "-m", "mewbo_tools.integration.sandbox_launcher"]


def test_apply_from_environ_reports_when_nothing_was_declared(monkeypatch):
    """A caller must be able to tell "unconfined" from "unchanged"."""
    monkeypatch.delenv(SandboxLauncher.ENV_VAR, raising=False)
    assert SandboxLauncher.apply_from_environ() is False


def test_apply_from_environ_never_raises_on_a_broken_scope(monkeypatch, capsys):
    monkeypatch.setenv(SandboxLauncher.ENV_VAR, "{oops")
    assert SandboxLauncher.apply_from_environ() is False
    assert "unreadable" in capsys.readouterr().err


def test_a_magicmock_config_is_not_mistaken_for_a_command():
    """Guards the isinstance check: only a real string names a spawn."""
    config = {"transport": "stdio", "command": MagicMock()}
    assert MCPConnectionPool._sandboxed(config) is config
