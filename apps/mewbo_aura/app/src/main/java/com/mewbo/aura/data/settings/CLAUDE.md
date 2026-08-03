> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Settings Store — data/settings/

Scope: `data/settings/` — `SettingsStore` (DataStore-preferences, file-scoped `aura_settings`) and
`KeystoreCipher`. Exposes only `Flow`s; the blocking `first()` reads happen at the SEAMS that cannot
suspend (`AuthInterceptor`/`BaseUrlInterceptor` on OkHttp's chain, `SessionStreamClient`,
`DeviceToolGate`), never here.

## Keys whose DEFAULT is load-bearing

- `base_url` → **default `""`** — forces onboarding through Settings; never guess a host.
- `api_key_ciphertext` + `api_key_iv` → the two halves of the encrypted key (below). Missing EITHER
  ⇒ `null`.
- `selected_model` → the full-app default (bare/unprefixed; also written by the chat top-bar picker).
  `overlay_default_model` → the independent assist-overlay default, read only at the overlay's
  session-creation seam. Both blank ⇒ the API's configured default, so a voice trigger and the app can
  run different models.
- `disabled_device_tool_ids` (a `stringSet`) → **empty = every `device_*` tool ENABLED.** Read through
  `DeviceToolGate` ([`data/device/CLAUDE.md`](../device/CLAUDE.md)).
- `streamlit_widgets_enabled` → **default ON.** Gates BOTH the `stlite` capability header AND
  `widget_ready` rendering at the same seam; OFF is an escape hatch to a plain chat client.
- `selected_project` → default `""` = the Temporary temp-dir cwd; the value is a
  `ProjectSummary.contextKey`.
- `speak_responses` (`true`), `reduced_motion` (`false`), `voice_use_fakes`, `display_name`.

## API key at rest — Keystore AES/GCM, decrypt off-main

`KeystoreCipher` wraps the key with a hardware-backed AndroidKeystore AES/GCM/NoPadding 256-bit key
(alias `mewbo_aura_api_key`), storing Base64 ciphertext + a per-encrypt random IV in DataStore.

**The `apiKey: Flow<String?>` decrypt is pinned to `Dispatchers.IO` via `.flowOn`** —
`KeystoreCipher.decrypt` is a synchronous Keystore/TEE IPC call, and without it the `.map{}` runs on
the collector's dispatcher, usually Main. A decrypt failure degrades to `null`, never a throw.

Deliberately NOT `EncryptedSharedPreferences`: settings already live in DataStore, so one AES/GCM
helper beats a second storage mechanism for a single field.

**A 401 on the dev device almost always means this stored key is GONE** (container recreated, `data/`
wiped, fresh install), not an auth bug — re-seed via the app-root CLAUDE.md's Recurring-401 recipe.
