"""Tests for the config API endpoints (GET/PATCH /api/config, GET /api/config/schema)."""

# mypy: ignore-errors
import json
import tempfile
from pathlib import Path

from mewbo_api import backend
from mewbo_core.config import (
    AppConfig,
    ConfigWriteAccess,
    ConfigWriteError,
    reset_config,
    set_app_config_path,
)


def _setup_temp_config(monkeypatch, payload: dict | None = None):
    """Write a temp config file and point the backend at it."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    json.dump(payload or {}, tmp)
    tmp.flush()
    tmp.close()
    set_app_config_path(tmp.name)
    monkeypatch.setattr(backend, "MASTER_API_TOKEN", "test-token")
    return tmp.name


def _teardown(path: str):
    reset_config()
    Path(path).unlink(missing_ok=True)


# ---------- GET /api/config/schema ----------


def test_config_schema_requires_auth():
    """GET /api/config/schema without API key returns 401."""
    client = backend.app.test_client()
    resp = client.get("/api/config/schema")
    assert resp.status_code == 401


def test_config_schema_strips_protected_keeps_secrets(monkeypatch):
    """GET /api/config/schema removes x-protected but keeps x-secret (writeOnly)."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.get(
            "/api/config/schema",
            headers={"X-API-Key": "test-token"},
        )
        assert resp.status_code == 200
        schema = resp.get_json()
        # APIConfig.master_token is x-protected -> removed entirely.
        api_props = schema["$defs"]["APIConfig"]["properties"]
        assert "master_token" not in api_props
        # LLMConfig.api_key is now x-secret -> present but writeOnly so the
        # console can SET it (never read it back).
        llm_props = schema["$defs"]["LLMConfig"]["properties"]
        assert "api_key" in llm_props
        assert llm_props["api_key"].get("writeOnly") is True
        # Langfuse keys are x-secret -> present + writeOnly.
        lf_props = schema["$defs"]["LangfuseConfig"]["properties"]
        assert lf_props["secret_key"].get("writeOnly") is True
        assert lf_props["public_key"].get("writeOnly") is True
    finally:
        _teardown(path)


# ---------- GET /api/config ----------


def test_config_get_omits_protected_and_secrets(monkeypatch):
    """GET /api/config strips protected + secret values and reports secret status."""
    path = _setup_temp_config(
        monkeypatch,
        {"api": {"master_token": "secret"}, "llm": {"api_key": "sk-set"}},
    )
    try:
        client = backend.app.test_client()
        resp = client.get(
            "/api/config",
            headers={"X-API-Key": "test-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        data = body["config"]
        # Protected master_token is stripped from values.
        assert "master_token" not in data.get("api", {})
        # Secret api_key value is stripped (write-only, never read back).
        assert "api_key" not in data.get("llm", {})
        # Non-protected fields remain.
        assert "default_model" in data.get("llm", {})
        # The secrets map reports is-set status; api_key was set above.
        secrets = body["secrets"]
        assert secrets["llm.api_key"] is True
        assert secrets["home_assistant.token"] is False
    finally:
        _teardown(path)


def test_config_get_reports_storage_writable(monkeypatch):
    """GET /api/config reports storage.writable True for an ordinary temp-dir store."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.get("/api/config", headers={"X-API-Key": "test-token"})
        assert resp.status_code == 200
        storage = resp.get_json()["storage"]
        assert storage["writable"] is True
        assert storage["code"] is None
        assert storage["reason"] is None
    finally:
        _teardown(path)


def test_config_get_reports_storage_unwritable(monkeypatch):
    """GET /api/config surfaces a read-only store BEFORE any edit is attempted."""
    path = _setup_temp_config(monkeypatch)
    try:
        monkeypatch.setattr(
            AppConfig,
            "probe_write_access",
            classmethod(
                lambda cls, _path: ConfigWriteAccess(
                    writable=False,
                    code="read_only",
                    reason="The configuration directory is mounted read-only.",
                )
            ),
        )
        client = backend.app.test_client()
        resp = client.get("/api/config", headers={"X-API-Key": "test-token"})
        assert resp.status_code == 200
        storage = resp.get_json()["storage"]
        assert storage["writable"] is False
        assert storage["code"] == "read_only"
        assert storage["reason"]
    finally:
        _teardown(path)


# ---------- PATCH /api/config ----------


def test_config_patch_rejects_protected(monkeypatch):
    """PATCH /api/config with protected field returns 403."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"api": {"master_token": "hacked"}},
        )
        assert resp.status_code == 403
        assert "protected" in resp.get_json()["message"].lower()
    finally:
        _teardown(path)


def test_config_patch_rejects_hooks_from_any_key(monkeypatch):
    """PATCH /api/config touching `hooks` is 403'd even from the master token (P0).

    Command hooks execute unsandboxed shell commands with the API process's
    own privileges, so this section is protected the same as `api.master_token`
    and the `runtime` host paths — settable only by editing the config file
    directly, never over the network regardless of credential.
    """
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},  # the master token itself
            json={"hooks": {"post_tool_use": [{"type": "command", "command": "curl x|sh"}]}},
        )
        assert resp.status_code == 403
        assert "protected" in resp.get_json()["message"].lower()

        # Nothing persisted.
        with open(path) as f:
            on_disk = json.load(f)
        assert "hooks" not in on_disk
    finally:
        _teardown(path)


def test_config_get_omits_hooks_section(monkeypatch):
    """GET /api/config never surfaces the hooks section's contents."""
    path = _setup_temp_config(
        monkeypatch,
        {"hooks": {"post_tool_use": [{"type": "command", "command": "echo hi"}]}},
    )
    try:
        client = backend.app.test_client()
        resp = client.get("/api/config", headers={"X-API-Key": "test-token"})
        assert resp.status_code == 200
        assert "hooks" not in resp.get_json()["config"]
    finally:
        _teardown(path)


def test_config_patch_updates_the_file_without_rewriting_it(monkeypatch):
    """A save applies the patch to the operator's document and changes nothing else.

    Persisting a re-render of the validated model instead would pin every unset
    field to a default and resolve the ``runtime.*`` directories against the
    saving process's environment — which makes a shared config file usable only
    from whichever process last saved it.
    """
    path = _setup_temp_config(monkeypatch)
    try:
        with open(path, "w") as handle:
            json.dump(
                {
                    "$schema": "./app.schema.json",
                    "runtime": {"cache_dir": ""},
                    "future_feature": {"enabled": True},
                },
                handle,
            )

        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"default_model": "anthropic/claude-sonnet-4-6"}},
        )
        assert resp.status_code == 200

        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["llm"]["default_model"] == "anthropic/claude-sonnet-4-6"
        # A key the model does not declare survives instead of being dropped.
        assert on_disk["$schema"] == "./app.schema.json"
        assert on_disk["future_feature"] == {"enabled": True}
        # "Leave this to the runtime" stays that way rather than being resolved
        # to an absolute path belonging to this process.
        assert on_disk["runtime"]["cache_dir"] == ""
        # Untouched sections are not materialized at their defaults.
        assert set(on_disk) == {"$schema", "runtime", "future_feature", "llm"}
    finally:
        _teardown(path)


def test_config_patch_allows_secret(monkeypatch):
    """PATCH /api/config may SET an x-secret field; it persists but is not read back."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"api_key": "sk-new-secret"}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # Secret value is not echoed back in the config dump...
        assert "api_key" not in body["config"].get("llm", {})
        # ...but the secrets map reports it as now-set.
        assert body["secrets"]["llm.api_key"] is True
        # And it actually persisted to disk.
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["llm"]["api_key"] == "sk-new-secret"
    finally:
        _teardown(path)


def test_config_patch_empty_secret_leaves_the_stored_value_intact(monkeypatch):
    """The outage: saving the model section blanked `llm.api_key` and returned 200.

    The console's form hydrates without the secret (it is stripped from every
    read), materializes an empty string, and PATCHes the section back whole. The
    seam both surfaces pass their own tests against is this one — the empty
    value must not reach disk while the non-secret edit beside it does.
    """
    path = _setup_temp_config(
        monkeypatch,
        {"llm": {"api_key": "sk-live", "default_model": "old-model"}},
    )
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"api_key": "", "default_model": "anthropic/claude-sonnet-4-6"}},
        )
        assert resp.status_code == 200

        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["llm"]["api_key"] == "sk-live"
        assert on_disk["llm"]["default_model"] == "anthropic/claude-sonnet-4-6"
        assert resp.get_json()["secrets"]["llm.api_key"] is True
    finally:
        _teardown(path)


def test_config_patch_preserves_an_env_reference(monkeypatch):
    """`${VAR}` is the form the deployment relies on; a save must not rewrite it."""
    monkeypatch.setenv("MEWBO_TEST_LLM_KEY", "sk-from-env")
    path = _setup_temp_config(
        monkeypatch,
        {"llm": {"api_key": "${MEWBO_TEST_LLM_KEY}"}},
    )
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"api_key": "", "default_model": "anthropic/claude-sonnet-4-6"}},
        )
        assert resp.status_code == 200

        with open(path) as f:
            on_disk = json.load(f)
        # The reference itself, not the value it resolved to.
        assert on_disk["llm"]["api_key"] == "${MEWBO_TEST_LLM_KEY}"
    finally:
        _teardown(path)


def test_config_patch_null_secret_clears_it(monkeypatch):
    """Clearing stays possible, and is said with an explicit null."""
    path = _setup_temp_config(monkeypatch, {"llm": {"api_key": "sk-live"}})
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"api_key": None}},
        )
        assert resp.status_code == 200
        assert resp.get_json()["secrets"]["llm.api_key"] is False

        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["llm"]["api_key"] == ""
    finally:
        _teardown(path)


def test_config_patch_keeps_a_list_element_secret(monkeypatch):
    """A list is merged by REPLACEMENT, so an entry's credential must ride along.

    Editing any field of an authenticator re-sends the whole list with its
    credentials empty; without the guard the entry lands on disk without one.
    """
    stored = {
        "api": {
            "auth": {
                "enabled": True,
                "session": {"secret": "cookie-key"},
                "authenticators": [
                    {
                        "name": "corp-oidc",
                        "kind": "oidc",
                        "issuer": "https://idp.example.com",
                        "client_id": "mewbo-console",
                        "client_secret": "OIDC-SECRET",
                    }
                ],
            }
        }
    }
    path = _setup_temp_config(monkeypatch, stored)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={
                "api": {
                    "auth": {
                        "session": {"secret": ""},
                        "authenticators": [
                            {
                                "name": "corp-oidc",
                                "kind": "oidc",
                                "issuer": "https://idp.example.com",
                                "client_id": "console",
                                "client_secret": "",
                            }
                        ],
                    }
                }
            },
        )
        assert resp.status_code == 200

        with open(path) as f:
            on_disk = json.load(f)
        entry = on_disk["api"]["auth"]["authenticators"][0]
        assert entry["client_secret"] == "OIDC-SECRET"
        assert entry["client_id"] == "console"
        assert on_disk["api"]["auth"]["session"]["secret"] == "cookie-key"
    finally:
        _teardown(path)


def test_config_patch_still_403s_a_protected_path_beside_a_secret(monkeypatch):
    """The secret rule runs AFTER the protected refusal and does not soften it."""
    path = _setup_temp_config(monkeypatch, {"llm": {"api_key": "sk-live"}})
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"api_key": ""}, "api": {"master_token": "hacked"}},
        )
        assert resp.status_code == 403

        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk == {"llm": {"api_key": "sk-live"}}
    finally:
        _teardown(path)


def test_config_patch_validates_input(monkeypatch):
    """PATCH /api/config with invalid type returns 422."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        # llm expects an object, sending a string should fail validation
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": "not-an-object"},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert "errors" in body
    finally:
        _teardown(path)


def test_config_patch_success(monkeypatch):
    """PATCH /api/config with valid data persists and returns updated config."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"default_model": "anthropic/claude-sonnet-4-6"}},
        )
        assert resp.status_code == 200
        data = resp.get_json()["config"]
        assert data["llm"]["default_model"] == "anthropic/claude-sonnet-4-6"

        # Verify it persisted to disk
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["llm"]["default_model"] == "anthropic/claude-sonnet-4-6"
    finally:
        _teardown(path)


def test_config_patch_write_failure_returns_structured_500(monkeypatch):
    """PATCH /api/config surfaces a write failure as a structured 500, not a traceback.

    Stubs only the I/O boundary (``AppConfig.write_document``) with the typed
    failure the persistence layer raises on a real read-only mount / permission
    / disk-full error, per the repo's testing doctrine (stub I/O, not the thing
    under test).
    """
    path = _setup_temp_config(monkeypatch)
    try:

        def _raise_write_error(cls, config_path, document, *, indent=2):
            raise ConfigWriteError(
                Path(config_path),
                "read_only",
                "The configuration directory is mounted read-only; settings cannot be saved "
                "until the deployment grants write access.",
            )

        monkeypatch.setattr(AppConfig, "write_document", classmethod(_raise_write_error))

        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"default_model": "anthropic/claude-sonnet-4-6"}},
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["code"] == "read_only"
        assert body["message"]
        # The server-side path must never ride in the response body.
        assert path not in json.dumps(body)

        # Nothing persisted — the file still holds the original empty payload.
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk == {}
    finally:
        _teardown(path)


def test_config_patch_empty_payload(monkeypatch):
    """PATCH /api/config with empty body returns 400."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={},
        )
        assert resp.status_code == 400
    finally:
        _teardown(path)


# ---------- Project CWD validation ----------


def test_projects_endpoint_includes_available(monkeypatch, tmp_path):
    """GET /api/projects includes available flag per project."""
    real_dir = str(tmp_path / "real")
    Path(real_dir).mkdir()
    fake_dir = str(tmp_path / "nonexistent")
    config = {
        "projects": {
            "real": {"path": real_dir, "description": "exists"},
            "fake": {"path": fake_dir, "description": "missing"},
        }
    }
    path = _setup_temp_config(monkeypatch, config)
    try:
        client = backend.app.test_client()
        resp = client.get(
            "/api/projects",
            headers={"X-API-Key": "test-token"},
        )
        assert resp.status_code == 200
        projects = {p["name"]: p for p in resp.get_json()["projects"]}
        assert projects["real"]["available"] is True
        assert projects["fake"]["available"] is False
    finally:
        _teardown(path)


def test_resolve_project_cwd_rejects_missing_dir(monkeypatch, tmp_path):
    """_resolve_project_cwd raises ValueError for nonexistent project path.

    Asserts the two properties a caller can rely on — it RAISES rather than
    returning a path nothing can use, and the refusal NAMES the directory it
    looked for — rather than a fragment of the sentence. The wording moved when
    resolution moved onto ``ProjectCatalog``, and matching prose made a message
    improvement read as a regression in a test whose actual subject is the
    refusal.
    """
    missing = tmp_path / "nonexistent"
    config = {
        "projects": {
            "phantom": {"path": str(missing), "description": "gone"},
        }
    }
    path = _setup_temp_config(monkeypatch, config)
    try:
        import pytest

        with pytest.raises(ValueError) as excinfo:
            backend._resolve_project_cwd({"project": "phantom"})
        assert str(missing) in str(excinfo.value)
    finally:
        _teardown(path)
