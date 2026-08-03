> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo HA Conversation — project guidance

Scope: `apps/mewbo_ha_conversation/` — the Home Assistant conversation agent. It is a
client over the Mewbo REST API and nothing more: no orchestration, no policy.

## Runtime flow

- API entry point: `apps/mewbo_ha_conversation/api.py`.
- An async HTTP client plus a timeout helper calls `POST /api/query`, and returns a parsed
  `MewboQueryResponse` to the Home Assistant host.
- **Continuity rides the `session_id` field of `POST /api/query`.** That endpoint accepts a
  `session_id` to continue an existing server session (`backend.py` `MewboQuery.post` →
  `runtime.resolve_session`, `sync_query_model`). `MewboAgent.query` forwards the id stored
  per conversation. There is no separate sessions-API handshake and no server-side session tag.

## Contracts and assumptions

- `MewboApiClient` requires an `api_key`, sent as `X-API-KEY` and threaded from the config
  entry in `async_setup_entry`, which is the composition root. A config entry with no stored
  key falls back to a default and logs a deprecation warning — never re-add a hardcoded key
  to the client itself.
- `base_url` must include the protocol and be reachable from the Home Assistant host.
- `async_get_models` returns a static stubbed response.
- Everything here must stay fully async — no blocking calls, no synchronous HTTP.

## Pitfalls

- **Multi-turn context breaks silently.** `self.history[conversation_id]` stores the last
  response's `session_id` and `MewboAgent.query` replays it on the next turn. A change that
  stops forwarding that id degrades to single-turn with no error.
- API failures raise `ApiJsonError` or a client exception; every caller must handle both.

## Testing

- Use async client test utilities, or monkeypatch `_session.request` for determinism.
- Cover error mapping (non-2xx → exception) and JSON parsing.
