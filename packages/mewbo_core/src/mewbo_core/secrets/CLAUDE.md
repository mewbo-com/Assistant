> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `secrets/` — API key storage and verification

Scope: `key_store.py` (the `KeyStoreBase` ABC, the filesystem driver, the
`create_key_store()` factory) and `key_store_mongo.py`.

## The store-composition pattern, of which this is the reference

**The whole public surface is composed ON the base out of a handful of abstract
persistence primitives that each driver implements.** `rotate_key`,
`list_keys_for_owner`, `revoke_keys_for_owner` and `purge_expired` are written
once on `KeyStoreBase`; no driver reimplements them, so no driver can drift from
another on the merge, the normalization or the round-trip. `triggers/store.py`,
`workspaces/repository_store.py` and `system_instructions/store.py` follow it.
**A store that overrides a COMPOSED method instead of a primitive is the shape to
reject in review.**

The Mongo driver is imported lazily inside the factory: a `storage.driver=json`
deployment must never import `pymongo`.

## Key handling

- **A key is high-entropy, so it gets ONE unsalted SHA-256 pass** — deliberately
  not bcrypt/argon2, which defend against low-entropy passwords. That is not the
  threat model for a `secrets.token_urlsafe` value, and the cost would be paid on
  every authenticated request.
- **Only the hash is persisted; the plaintext is returned exactly once, at
  creation.** There is no read-back path. A caller that "just needs to show it
  again" is asking for a rotation.
- **`scopes` is THREE-STATE on read: absent/`None` means unrestricted, `[]` means
  explicitly-none, and the two are NOT interchangeable.** `KeyScopes` wraps that
  law as behaviour on one class (`.matches(required)`) so no caller re-derives it
  with a scattered `if` — same failure mode and same cure as `allowed_tools`
  (`tooling/CLAUDE.md`).
- **This package stays a plain store and never imports the identity kernel.** A
  record MAY carry `owner_subject`/`roles`/`scopes`/`team_id`/`expires_at`, but
  mapping a record to a principal belongs to the app-side auth kit. `KeyScopes` is
  the pure matcher `mewbo_iam` imports DOWN — the arrow points that way and only
  that way.
