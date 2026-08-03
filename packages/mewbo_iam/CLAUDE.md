> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo IAM — Identity-Kernel Guidance

`packages/mewbo_iam/src/mewbo_iam/` — principals and authenticators, the closed permission
catalog + roles, teams, resource ownership/grants, group→role/team mappings, durable user
records, and the auth audit trail.

**Base deps are `mewbo-core` + `pydantic` and NOTHING else** — never `mewbo-tools`, never
`mewbo-graph`, never an app. Provider integrations (OIDC discovery, LDAP bind, SAML) sit behind
the `oidc`/`ldap`/`saml` extras plus in-code import-guards, so a bare install carries only the
models and the JSON/Mongo stores. `tests/test_iam_architecture.py` enforces this structurally: it
parses the AST of both trees to prove neither imports the other, reads `pyproject.toml` to prove
the base dependency set, and runs `sys.modules` probes in FRESH SUBPROCESSES to prove importing
the kernel loads no driver dependency and that each extra resolves independently.

## Laws that are easy to get wrong

- **Models never import I/O.** Behavior lives on the model, but the clock, HTTP headers and IdP
  claims arrive as method ARGUMENTS, never fetched inside: `authenticator.resolve(claims)`,
  `decider.decide(principal, …)`, `principal.effective_permissions(role_records)`,
  `UserRecord(created_at=…)`. The network legs live in `drivers/` — same package, opposite side
  of the line. Drivers import the models; never the reverse.
- **`scopes` is three-state, and the states are NOT interchangeable.** `Principal.scopes`:
  `None` = impose no narrowing, `()` = explicitly none (a scopeless service key can do nothing
  scope-gated), a non-empty tuple = exactly those. Collapsing `None`↔`()` is
  the fail-open bug — a scopeless key silently becoming unrestricted.
- **Variant families are discriminated unions — no `if kind ==` dispatch.** `AuthenticatorSpec`
  and `AuthAuditEvent` mirror `mewbo_core.triggers.spec`: each member owns its validators +
  strategy methods, with ONE parse seam per family (`parse_authenticator`, `parse_audit_event`).
  Add a variant by adding a class, never by extending a service switch.
- **A driver inherits its library's INSECURE default unless it overrides it.** `ldap3`'s `Tls`
  validates nothing (`ssl.CERT_NONE`), so `ldaps://` and StartTLS encrypt a bind without ever
  authenticating the peer — the connection looks secure while handing the user's password to
  anyone on the path. Every driver therefore constructs its transport EXPLICITLY, in ONE helper
  all its connection sites share (`drivers/ldap.py`: the search bind and the password-verify
  rebind go through the same builder, so neither can quietly skip the certificate). Never let a
  client library's default stand where a credential crosses it.
- **The permission catalog is CLOSED.** `PermissionCatalog` ids are `Final[Literal[...]]`
  constants, `ALL` is derived from them, and `RoleRecord` validates its permissions ⊆ `ALL` at
  definition. A new verb is a new constant here, never a free string at a call site.

## Where things live

- **Models** (pure): `principal.py`, `authenticators.py`, `permissions.py`, `roles.py`,
  `teams.py`, `access.py`, `mappings.py`, `users.py`, `audit.py`, `settings.py` (the parsed
  `api.auth` block), `scim.py` — the SCIM 2.0 resource subset and the ONE place `extra="ignore"`
  is correct, because the wire format itself defines unknown fields as optional metadata.
- **Stores** (I/O): `stores/` — an ABC + a JSON driver + a `create_*_store` factory per store.
  `pymongo` is imported ONLY in `stores/mongo.py`, lazily when `storage.driver == "mongodb"`, so
  a json install never needs a reachable MongoDB. The json-or-mongo choice is read in ONE place,
  `_JsonCollectionStore.resolve_driver`, which each factory delegates to; a store declares
  `_FILENAME` + `_MONGO_DRIVER` and inherits both its default path and that selection. Built-in
  roles are data constants (`BUILTIN_ROLES`) seeded by the role store, which refuses to update or
  delete them.
- **Drivers** (network + crypto): `drivers/` — the other half of the pure authenticators, behind
  the extras. `drivers/__init__.py` resolves every export lazily through a PEP 562 `__getattr__`
  keyed on one `_EXPORTS` table, so each name probes only ITS OWN extra: an LDAP-only deployment
  never needs the OIDC dependencies.

## The team store owns both sides of membership

`TeamStoreBase` persists `TeamRecord`s AND the `TeamMembershipRecord` edges pointing at them — a
second file (`iam_team_members.json`) or a second collection (`iam_team_members`).

- **Edges are NOT fields on `TeamRecord`.** SCIM does a full-document `update` on every group
  rename, and a members field would let a rename silently clear the roster.
- **`delete` cascades, and it is concrete on the base** (over `_remove_team`) precisely so no
  driver can forget it. Same reason `create`/`update` are templates over
  `_insert_team`/`_replace_team`: uniqueness on id/slug/`external_id` is enforced identically for
  both drivers instead of only by mongodb's unique indexes, which stay as the backstop for the
  read-then-write race the check cannot close.
- **`_ordered` is the only place edges are sorted** — drivers return matches in backend order and
  the base imposes `(team_id, user_id)`. Never push that sort down; that is how two drivers drift.

**Deleting a user does not cascade here.** The user store must not import the team store, so an
edge can outlive its user. Deliberate and inert: `list_members` returns edges and every caller
resolves `user_id` against the user store, skipping what no longer resolves.

**Two membership shapes, one projection seam.** `TeamMembershipRecord` is the durable edge (it
names the user); `TeamMembership` is what a `Principal` carries (it does not — the principal
already IS that user). `list_for_user` is the ONE place one becomes the other, and
`Principal.with_team_memberships` the ONE way they get attached. Never assemble that tuple inline.

## `TeamMembership.team_id` is an ID, never a slug

Memberships reach a principal from two sources with different keys, and conflating them is
silent, not loud: store-backed memberships are durable, `team:<uuid>`-keyed, written by SCIM and
the admin surface, and are the only source that can express `team_admin`; config-mapped ones are
ephemeral, recomputed from IdP groups on every login, and `GroupTeamMapping.resolve` returns
**slugs** and cannot express a role.

`AccessDecider` matches `team_id` against `OwnershipStamp.team_id` and team grantee ids, both
durable ids. A slug written into `team_id` type-checks, stores, and then matches nothing — every
resource a team owns goes invisible to its own members. So the mapping resolves through
`TeamStoreBase.memberships_for_slugs`, which returns memberships carrying real ids.

**An unmatched slug is skipped, never auto-created.** Auto-creation would make login a write
path: a config typo would mint a durable resource-owning team, and two concurrent first logins
would race into the uniqueness check and fail the login itself. A mapping miss degrades the
principal and logs, exactly as an unknown role name is inert in `effective_permissions`.

**Store-backed wins a conflict**, and the reason is privilege loss rather than taste: mapping
only ever yields `team_member`, so letting it win would demote a stored `team_admin` on every
login, while the reverse gains nothing because mapping can never confer a role storage lacks.
`Principal.with_team_memberships(durable, mapped=…)` applies that precedence in one place.

**SCIM's `replace` goes through `replace_members`,** not a hand-rolled diff: "drop all, re-add"
demotes every `team_admin` in the team, because a SCIM member list carries no roles.
`replace_members` preserves a retained member's existing role and adds only new members.

**`external_id` is last-write-wins when present, and is never cleared.** A push that omits
`externalId` keeps the stored link, or one client omitting it strands the team behind the slug
fallback; a push sending a different one re-points it. First-write-wins was rejected because
`external_id` is uniquely indexed, so a stale value would permanently block the real group from
claiming it short of DB surgery.

**Loguru, not `%s`.** `get_logger` returns a bound loguru logger, which formats with `{}`. A
`logging.warning("... %s", x)` silently drops `x` and prints the literal `%s` — the diagnostic
loses the one value it existed to show.

## Ordering the audit trail

`AuthAuditStoreBase.list` is a concrete template and the ONLY place audit events are ordered.
`ts` persists as an ISO-8601 STRING, so a backend-side sort on it is lexicographic — `...Z` and
`+00:00` spellings of one instant order by their text. Drivers implement `_stored_records`
(exact-value filters only); the base parses, sorts by instant, then applies `since` and `limit`.
Never push the sort down.
