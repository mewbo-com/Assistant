#!/usr/bin/env python3
"""MCP tool runner for integrating MCP servers into Mewbo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Any

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger, get_mock_speaker
from mewbo_core.config import get_mcp_config_path

from mewbo_tools.integration.exception_unwrap import (
    classify_connect_failure,
    describe_exception_group,
)

logging = get_logger(name="tools.integration.mcp")
_LAST_DISCOVERY_FAILURES: dict[str, str] = {}


def _log_discovery_failure(server_name: str, exc: Exception) -> None:
    reason = classify_connect_failure(exc)
    logging.bind(mcp_server=server_name, reason=reason).warning(
        "Failed to discover MCP tools for {} [{}]: {}",
        server_name,
        reason,
        describe_exception_group(exc),
    )
    logging.opt(exception=exc).debug("MCP discovery traceback for {}", server_name)


def _log_runtime_failure(server_name: str, tool_name: str, exc: Exception) -> None:
    reason = classify_connect_failure(exc)
    logging.bind(mcp_server=server_name, reason=reason).warning(
        "MCP runtime error for {}.{} [{}]: {}",
        server_name,
        tool_name,
        reason,
        describe_exception_group(exc),
    )
    logging.opt(exception=exc).debug("MCP runtime traceback for {}.{}", server_name, tool_name)


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env_vars(config: dict[str, Any]) -> dict[str, Any]:
    """Recursively expand ``${VAR}`` and ``$VAR`` patterns in string values."""

    def _expand_str(value: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1) or match.group(2)
            resolved = os.environ.get(var_name)
            if resolved is None:
                logging.debug("Env var '{}' referenced in MCP config not found", var_name)
                return match.group(0)  # Leave unresolved
            return resolved

        return _ENV_VAR_PATTERN.sub(_replace, value)

    def _walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return _expand_str(obj)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return obj

    return _walk(config)


# Claude Code-only ``.mcp.json`` extensions that langchain-mcp-adapters'
# session factories don't accept and which would otherwise blow up as
# ``unexpected keyword argument`` when the adapter spreads the connection
# dict into the session constructor. Add new entries here as plugins
# surface them — see the ``type`` pop above for the same pattern.
_UNSUPPORTED_ADAPTER_KEYS = ("oauth",)

# Servers switched off via ``enabled: false``. They are removed from the active
# set, so nothing discovers or dials them — but remembered here so ``/mcp`` can
# report "configured but off" instead of the entry simply vanishing. An
# invisible disabled server is how someone spends an afternoon hunting a tool
# that was never going to load.
_DISABLED_SERVERS: set[str] = set()


def _server_enabled(server_config: dict[str, Any]) -> bool:
    """Read and REMOVE the on/off switch for one server.

    Removing it is load-bearing, not tidiness: a leftover key is spread into the
    adapter's session constructor and raises ``unexpected keyword argument`` —
    the same trap ``_UNSUPPORTED_ADAPTER_KEYS`` exists for. Adding a switch that
    broke the server it was meant to control would be a poor joke.

    Absent means enabled, so every existing config keeps working untouched.
    Only a literal JSON ``false`` disables: a typo (``"false"``, ``0``, ``null``)
    leaves the server ON rather than silently removing it, because the failure
    this whole area is being hardened against is servers disappearing without
    anyone being told.
    """
    return server_config.pop("enabled", True) is not False


def disabled_servers() -> frozenset[str]:
    """Server names switched off in config at the most recent load."""
    return frozenset(_DISABLED_SERVERS)


def _normalize_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize alternate MCP config key spellings for adapter compatibility."""
    config = _expand_env_vars(config)
    # Normalize top-level mcpServers → servers (Claude Code format)
    if "mcpServers" in config and "servers" not in config:
        config["servers"] = config.pop("mcpServers")
    elif "mcpServers" in config:
        config.pop("mcpServers")
    servers = config.get("servers", {})
    # Drop switched-off servers from the ACTIVE set here, at the one seam every
    # consumer passes through — the server dict is read at a dozen call sites
    # across discovery and the pool, and a filter re-spelled at each is a filter
    # that will be forgotten at one of them. Removed rather than flagged so a
    # disabled server costs nothing: nothing discovers it, nothing dials it.
    #
    # Scoped discard/add, NOT a blind clear()-then-rebuild: several callers
    # (e.g. ``MCPToolRunner._invoke_via_pool``) normalize a config and then
    # hand the ALREADY-normalized result to another normalizing call
    # (``MCPConnectionPool.refresh_if_config_changed``) — by the second pass
    # a disabled server is already gone from ``servers``, so a blind clear()
    # there would erase its membership and silently un-disable it in
    # ``status_snapshot()``. A name is only cleared when THIS pass sees it
    # again, enabled; a name simply absent from this pass (already stripped
    # upstream) is left exactly as it was.
    for name in list(servers.keys()):
        if _server_enabled(servers[name]):
            _DISABLED_SERVERS.discard(name)
        else:
            _DISABLED_SERVERS.add(name)
            servers.pop(name)
    for server_config in servers.values():
        if "http_headers" in server_config and "headers" not in server_config:
            server_config["headers"] = server_config.pop("http_headers")
        # Rename "type" → "transport" (Claude Code / VS Code .mcp.json schema).
        # Always pop "type" to avoid it leaking into **kwargs during session
        # creation (deep-merge can produce configs with both keys).
        if "type" in server_config:
            if "transport" not in server_config:
                server_config["transport"] = server_config.pop("type")
            else:
                server_config.pop("type")
        # Infer transport from config shape when neither key is present
        if "transport" not in server_config and "command" in server_config:
            server_config["transport"] = "stdio"
        if server_config.get("transport") == "http":
            server_config["transport"] = "streamable_http"
        for key in _UNSUPPORTED_ADAPTER_KEYS:
            server_config.pop(key, None)
    return config


# De-duped "the config FILE is broken" diagnostics -- distinct from a
# per-server connect failure, which is already well handled by the pool's
# classify/quarantine/backoff (see mcp_pool.py). Keyed by a hash of the raw
# file bytes so a hot tool-call path never re-warns while the file stays
# broken, but always re-fires the moment its bytes change: an operator's fix
# (confirmed by `_clear_config_error`, which also re-arms the warning for an
# identical re-break) or a fresh typo both show up on the very next load.
_WARNED_CONFIG_HASHES: dict[str, str] = {}
_LAST_CONFIG_ERROR: dict[str, str] | None = None


def _log_config_parse_failure(
    config_path: str, raw_text: str, exc: json.JSONDecodeError
) -> None:
    global _LAST_CONFIG_ERROR
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    detail = f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    _LAST_CONFIG_ERROR = {"path": config_path, "detail": detail}
    if _WARNED_CONFIG_HASHES.get(config_path) == content_hash:
        return
    _WARNED_CONFIG_HASHES[config_path] = content_hash
    logging.bind(mcp_config_path=config_path).warning(
        "MCP config at {} is not valid JSON ({}) -- every server it defines is"
        " unavailable until the file is fixed.",
        config_path,
        detail,
    )


def _clear_config_error(config_path: str) -> None:
    """Drop dedup + last-error state once *config_path* parses again.

    This both confirms the fix (``status_snapshot`` stops showing it) and
    re-arms the warning, so breaking the same file the same way again is
    not silently swallowed by the dedup cache.
    """
    global _LAST_CONFIG_ERROR
    _WARNED_CONFIG_HASHES.pop(config_path, None)
    if _LAST_CONFIG_ERROR is not None and _LAST_CONFIG_ERROR.get("path") == config_path:
        _LAST_CONFIG_ERROR = None


def get_last_config_error() -> dict[str, str] | None:
    """Return the most recent malformed-MCP-config diagnostic, if any.

    Read by ``MCPConnectionPool.status_snapshot`` so a broken config file is
    visible in ``/mcp`` alongside per-server connect states.
    """
    return dict(_LAST_CONFIG_ERROR) if _LAST_CONFIG_ERROR is not None else None


def _read_json_config(config_path: str) -> dict[str, Any]:
    """Read and parse one MCP config file.

    The single seam where a syntactically invalid MCP config is detected,
    regardless of which caller (merged discovery's single-file fallback, or
    an explicit path) triggered the load. Diagnoses the failure loudly --
    once per file until its content changes -- then re-raises unchanged, so
    ``_load_mcp_config``'s documented raise contract holds.
    """
    with open(config_path, encoding="utf-8") as handle:
        raw_text = handle.read()
    try:
        config = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        _log_config_parse_failure(config_path, raw_text, exc)
        raise
    _clear_config_error(config_path)
    return config


def _load_mcp_config(
    path: str | None = None,
    *,
    cwd: str | None = None,
    trust_cwd: bool = True,
) -> dict[str, Any]:
    """Load MCP server configuration from disk.

    When *cwd* is provided (or *path* is omitted), uses
    ``get_merged_mcp_config(cwd)`` to merge global config with any
    CWD-local ``.mcp.json``.  Falls back to single-file loading when
    an explicit *path* is given.

    *trust_cwd* rides straight through to ``get_merged_mcp_config``: pass
    ``False`` when *cwd* holds content this deployment did not author, and the
    directory-derived tiers contribute nothing. The fallback single-file load
    below is operator-owned config and is unaffected either way.

    Returns:
        Parsed MCP configuration dictionary.

    Raises:
        ValueError: If the MCP config path is not set.
        OSError: If the configuration file cannot be read.
        json.JSONDecodeError: If the configuration is invalid JSON.
    """
    if path is None or cwd is not None:
        # Use merged discovery: global + CWD .mcp.json
        from mewbo_core.config import get_merged_mcp_config

        merged = get_merged_mcp_config(cwd, trust_cwd=trust_cwd)
        if not merged or not merged.get("servers"):
            # Fall back to single-path for backward compat
            config_path = get_mcp_config_path()
            if not config_path:
                raise ValueError("MCP config path is not set.")
            config_path = os.path.abspath(config_path)
            if not os.path.exists(config_path):
                raise ValueError(f"MCP config not found at {config_path}.")
            merged = _read_json_config(config_path)
        return _normalize_mcp_config(merged)

    config_path = os.path.abspath(path)
    if not os.path.exists(config_path):
        raise ValueError(f"MCP config not found at {config_path}.")
    config = _read_json_config(config_path)
    return _normalize_mcp_config(config)


def save_mcp_config(config: dict[str, Any], path: str | None = None) -> None:
    """Persist an MCP configuration payload to disk.

    Args:
        config: MCP configuration payload to write.
        path: Optional explicit file path (defaults to the configured MCP path).
    """
    config_path = path or get_mcp_config_path()
    if not config_path:
        raise ValueError("MCP config path is not set.")
    config_path = os.path.abspath(config_path)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def _schema_from_args_schema(args_schema: Any) -> dict[str, Any] | None:
    """Extract a JSON Schema dict from an MCP tool's args_schema.

    Returns the schema as-is, stripping only Pydantic internals (``$defs``,
    ``definitions``) that reference resolved types.  All standard JSON Schema
    keywords (``type``, ``anyOf``, ``properties``, etc.) are preserved so that
    downstream LLM providers receive valid schemas.
    """
    if args_schema is None:
        return None
    if isinstance(args_schema, dict):
        schema = args_schema
    elif hasattr(args_schema, "model_json_schema"):
        schema = args_schema.model_json_schema()
    elif hasattr(args_schema, "schema"):
        schema = args_schema.schema()
    else:
        return None
    if not isinstance(schema, dict):
        return None
    # Strip Pydantic internals that only make sense alongside $ref.
    return {k: v for k, v in schema.items() if k not in ("$defs", "definitions")}


def _tool_schema_payload(tool: Any) -> dict[str, Any] | None:
    args_schema = getattr(tool, "args_schema", None)
    return _schema_from_args_schema(args_schema)


async def _list_server_tool_schemas_async(
    server_name: str, config: dict[str, Any]
) -> list[dict[str, Any]]:
    from mewbo_tools.integration.mcp_pool import get_mcp_pool

    pool = get_mcp_pool()
    try:
        await pool.refresh_if_config_changed(config)
        state = await pool.get_or_connect(server_name)
    except Exception as exc:
        raise RuntimeError(
            f"failed to introspect MCP server '{server_name}': {exc}"
        ) from exc
    tools: list[dict[str, Any]] = []
    for tool in state.tools:
        name = getattr(tool, "name", "")
        if not name:
            continue
        entry: dict[str, Any] = {"name": name}
        description = getattr(tool, "description", "") or ""
        if description:
            entry["description"] = description
        schema = _tool_schema_payload(tool)
        if schema is not None:
            entry["inputSchema"] = schema
        tools.append(entry)
    return sorted(tools, key=lambda t: t["name"])


def list_server_tool_schemas(
    server_name: str,
    *,
    cwd: str | None = None,
    trust_cwd: bool = True,
) -> list[dict[str, Any]]:
    """Return one configured server's live tool schemas via the shared pool.

    The public introspection seam for callers that need a server's advertised
    tool list (name / description / input schema) without binding the tools:
    loads the merged MCP config for *cwd*, refreshes the pool fingerprint, and
    connects on demand (the ``MCPToolRunner._invoke_via_pool`` pattern). Only
    schema-bearing attributes are read off each tool — never connection or
    auth material.

    Raises:
        LookupError: *server_name* has no entry in the merged MCP config.
        RuntimeError: the config could not be read, or the live introspection
            (pool connect / handshake) failed.
    """
    try:
        config = _load_mcp_config(cwd=cwd, trust_cwd=trust_cwd)
    except Exception as exc:
        raise RuntimeError(f"failed to read MCP config: {exc}") from exc
    servers = config.get("servers", {}) if isinstance(config, dict) else {}
    if server_name not in servers:
        raise LookupError(f"MCP server '{server_name}' is not configured.")
    return asyncio.run(_list_server_tool_schemas_async(server_name, config))


async def _discover_mcp_tool_details_with_failures_async(
    config: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Exception]]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("langchain-mcp-adapters is required for MCP tools.") from exc

    servers = config.get("servers", {})
    discovered: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, Exception] = {}
    for server_name, server_config in servers.items():
        try:
            client = MultiServerMCPClient({server_name: server_config})
            tools = await client.get_tools(server_name=server_name)
        except Exception as exc:
            _log_discovery_failure(server_name, exc)
            failures[server_name] = exc
            discovered[server_name] = []
            continue
        details: list[dict[str, Any]] = []
        for tool in tools:
            details.append(
                {
                    "name": tool.name,
                    "schema": _tool_schema_payload(tool),
                }
            )
        discovered[server_name] = sorted(details, key=lambda item: item.get("name", ""))
    return discovered, failures


async def _discover_mcp_tool_details_async(
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    details, _ = await _discover_mcp_tool_details_with_failures_async(config)
    return details


def discover_mcp_tools(config: dict[str, Any]) -> dict[str, list[str]]:
    """Discover MCP tool names per server from configuration."""
    details = discover_mcp_tool_details(config)
    return {
        server_name: [tool["name"] for tool in tools if tool.get("name")]
        for server_name, tools in details.items()
    }


def discover_mcp_tool_details(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Discover MCP tool names and schemas per server from configuration."""
    return asyncio.run(_discover_mcp_tool_details_async(_normalize_mcp_config(config)))


def discover_mcp_tool_details_with_failures(
    config: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Exception]]:
    """Discover MCP tool names, schemas, and per-server failures."""
    discovered, failures = asyncio.run(
        _discover_mcp_tool_details_with_failures_async(_normalize_mcp_config(config))
    )
    _record_discovery_failures(failures)
    return discovered, failures


def _record_discovery_failures(failures: dict[str, Exception]) -> None:
    _LAST_DISCOVERY_FAILURES.clear()
    for server_name, exc in failures.items():
        reason = str(exc).strip().splitlines()[0] if exc else "Unknown error"
        _LAST_DISCOVERY_FAILURES[server_name] = reason


def get_last_discovery_failures() -> dict[str, str]:
    """Return last MCP discovery failures per server (if any)."""
    return dict(_LAST_DISCOVERY_FAILURES)


def tool_auto_approved(
    config: dict[str, Any],
    server_name: str,
    tool_name: str,
) -> bool:
    """Return True when a tool is marked as auto-approved."""
    server_config = config.get("servers", {}).get(server_name, {})
    if server_config.get("auto_approve_all"):
        return True
    allowlist = server_config.get("auto_approve_tools", [])
    return tool_name in allowlist


def mark_tool_auto_approved(
    config: dict[str, Any],
    server_name: str,
    tool_name: str,
) -> dict[str, Any]:
    """Record a tool as auto-approved in the MCP config."""
    servers = config.setdefault("servers", {})
    server_config = servers.setdefault(server_name, {})
    allowlist = server_config.setdefault("auto_approve_tools", [])
    if tool_name not in allowlist:
        allowlist.append(tool_name)
        server_config["auto_approve_tools"] = sorted(set(allowlist))
    return config


class MCPToolRunner:
    """Wrapper to invoke MCP tools via langchain-mcp-adapters."""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        *,
        cwd: str | None = None,
        trust_cwd: bool = True,
    ) -> None:
        """Initialize the MCP tool runner for a specific server tool.

        Args:
            server_name: MCP server name from configuration.
            tool_name: Tool name to invoke on the server.
            cwd: Project working directory for merged config loading.
            trust_cwd: Whether *cwd* may contribute its own ``.mcp.json``.
                A runner RE-RESOLVES the merged config at invocation time, long
                after the registry was built — so a directory whose content
                arrived after that build (a clone, say) is first seen here.
                Carrying the trust decision on the runner is what keeps the
                boundary from holding at discovery and leaking at call time.
        """
        self.server_name = server_name
        self.tool_name = tool_name
        self._cwd = cwd
        self._trust_cwd = trust_cwd

    async def _invoke_async(self, input_payload: str | dict[str, Any]) -> str:
        """Invoke an MCP tool asynchronously and return its output.

        Prefers the connection pool for persistent, cached connections.
        Falls back to the one-shot client path when the pool is unavailable.

        Args:
            input_payload: Input payload to send to the MCP tool.

        Returns:
            Stringified tool response.

        Raises:
            RuntimeError: If MCP adapters are not installed.
            ValueError: If the server or tool is not configured.
        """
        try:
            return await self._invoke_via_pool(input_payload)
        except (OSError, json.JSONDecodeError):
            # The MCP config FILE itself is unreadable/malformed -- already
            # diagnosed loudly at the read site (`_read_json_config`). This is
            # a config problem, not a transport hiccup, so falling back to the
            # one-shot client would just re-hit the identical parse failure.
            raise
        except Exception as pool_exc:
            logging.debug(
                "Pool invocation failed for {}.{}, falling back: {}",
                self.server_name,
                self.tool_name,
                pool_exc,
            )
            return await self._invoke_legacy(input_payload)

    async def _invoke_via_pool(self, input_payload: str | dict[str, Any]) -> str:
        """Invoke via the persistent connection pool."""
        from mewbo_tools.integration.mcp_pool import get_mcp_pool

        pool = get_mcp_pool()

        # A server that is already live must never trigger
        # a full-config reload + reconnect mid-query — that re-dialed EVERY
        # server (incl. dead ones) on every tool call, re-incurring the stall.
        # Only when the target isn't connected do we sync config (so a config
        # change / first use is picked up), and even then with connect=False so
        # the eager dial is deferred to get_or_connect for THIS server alone.
        if not pool.is_connected(self.server_name):
            config = _normalize_mcp_config(
                _load_mcp_config(cwd=self._cwd, trust_cwd=self._trust_cwd)
            )
            await pool.refresh_if_config_changed(config, connect=False)

        state = await pool.get_or_connect(self.server_name)

        tool_map = {getattr(t, "name", ""): t for t in state.tools} if state.tools else {}
        tool = tool_map.get(self.tool_name)
        if tool is None:
            raise ValueError(
                f"Tool '{self.tool_name}' not found on MCP server '{self.server_name}'."
            )

        prepared = _prepare_mcp_input(tool, input_payload)
        result = await pool.call_tool(self.server_name, self.tool_name, prepared)
        return str(result)

    async def _invoke_legacy(self, input_payload: str | dict[str, Any]) -> str:
        """One-shot client path: connect, call, disconnect. No pooling."""
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except Exception as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("langchain-mcp-adapters is required for MCP tools.") from exc

        config = _load_mcp_config(cwd=self._cwd, trust_cwd=self._trust_cwd)
        servers = config.get("servers", {})
        if not servers or self.server_name not in servers:
            if self.server_name in disabled_servers():
                raise ValueError(f"MCP server '{self.server_name}' is disabled.")
            raise ValueError(f"MCP server '{self.server_name}' not found in config.")

        client = MultiServerMCPClient({self.server_name: servers[self.server_name]})
        tools = await client.get_tools(server_name=self.server_name)
        tool_map = {tool.name: tool for tool in tools}
        tool = tool_map.get(self.tool_name)
        if tool is None:
            raise ValueError(
                f"Tool '{self.tool_name}' not found on MCP server '{self.server_name}'."
            )
        try:
            result = await tool.ainvoke(_prepare_mcp_input(tool, input_payload))
            return str(result)
        except Exception as exc:
            _log_runtime_failure(self.server_name, self.tool_name, exc)
            raise

    async def arun(self, action_step: ActionStep) -> MockSpeaker:
        """Async execution — preferred when called from an async context.

        Calls ``_invoke_async`` directly, avoiding the ``asyncio.run()``
        wrapper that would fail inside a running event loop.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        MockSpeakerType = get_mock_speaker()
        result = await self._invoke_async(action_step.tool_input)
        return MockSpeakerType(content=result)

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Sync execution — for use from sync-only callers.

        Raises:
            ValueError: If action_step is None.
            RuntimeError: If called from inside a running event loop.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        MockSpeakerType = get_mock_speaker()
        result = asyncio.run(self._invoke_async(action_step.tool_input))
        return MockSpeakerType(content=result)


def _prepare_mcp_input(
    tool: Any,
    input_payload: str | dict[str, Any],
) -> str | dict[str, Any]:
    """Convert action input into the payload expected by MCP tools.

    LangChain MCP tools with args_schema reject raw strings, so we coerce
    string inputs into schema-shaped dictionaries when possible.
    """
    if isinstance(input_payload, dict):
        return input_payload
    if not isinstance(input_payload, str):
        return input_payload

    stripped = input_payload.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    args_schema = getattr(tool, "args_schema", None)
    field_names: list[str] = []
    schema_properties: dict[str, Any] | None = None
    if args_schema is not None:
        if isinstance(args_schema, dict):
            props = args_schema.get("properties")
            if isinstance(props, dict):
                schema_properties = props
                field_names = list(props.keys())
        else:
            fields = getattr(args_schema, "model_fields", None)
            if isinstance(fields, dict):
                field_names = list(fields.keys())
            else:
                fields = getattr(args_schema, "__fields__", None)
                if isinstance(fields, dict):
                    field_names = list(fields.keys())

    if not field_names:
        return input_payload

    def _wrap_value(field_name: str) -> dict[str, Any]:
        if schema_properties and field_name in schema_properties:
            prop = schema_properties[field_name]
            if isinstance(prop, dict) and prop.get("type") == "array":
                items = prop.get("items")
                if isinstance(items, dict) and items.get("type") == "string":
                    return {field_name: [input_payload]}
        return {field_name: input_payload}

    if len(field_names) == 1:
        return _wrap_value(field_names[0])

    for preferred in ("query", "question", "input", "text", "q"):
        if preferred in field_names:
            return _wrap_value(preferred)

    return input_payload
