"""Cohesive, pure view over the ``AppConfig`` JSON schema.

``ConfigSchemaView`` is the single source of truth for how the ``/config``
endpoints treat sensitive fields. It classifies every field as either

* **protected** (``x-protected``) — never read, never written: stripped from
  the public schema and from value dumps, and rejected in PATCH payloads. Used
  for host paths and the API master token.
* **secret** (``x-secret``) — write-only: settable via PATCH but never read
  back. Kept in the public schema marked ``writeOnly: true``; its value is
  stripped from dumps; is-set status is reported separately. Because the value
  is never read back, an EMPTY one arriving in a PATCH means "unchanged" —
  ``resolve_secret_writes`` is the seam that says so.

A single traversal of the generated schema (walking ``$defs`` + ``$ref`` and
inline object properties) collects both sets of dot-paths up front; every
public method then operates on those precomputed sets. The class is pure (no
Flask/HTTP imports) and fully unit-testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from mewbo_core.config import AppConfig
from pydantic import BaseModel

_PROTECTED_KEY = "x-protected"
_SECRET_KEY = "x-secret"


class ConfigSchemaView:
    """Cohesive view over the ``AppConfig`` JSON schema.

    Classifies fields as protected (never exposed) or secret (write-only), and
    serves the schema/value transforms the ``/config`` endpoints need. One
    traversal, dependency-injected with the generated schema.
    """

    def __init__(self, schema: dict) -> None:
        """Build the view from a generated JSON *schema* (one traversal)."""
        self._schema = schema
        self._defs: dict = schema.get("$defs", {})
        protected: set[str] = set()
        secret: set[str] = set()
        self._classify(schema, protected=protected, secret=secret, prefix="")
        self._protected = protected
        self._secret = secret

    @classmethod
    def from_model(cls, model: type[BaseModel] = AppConfig) -> ConfigSchemaView:
        """Build a view from a Pydantic model's generated JSON schema."""
        return cls(model.model_json_schema())

    # -- one traversal -----------------------------------------------------

    def _classify(
        self,
        schema: dict,
        *,
        protected: set[str],
        secret: set[str],
        prefix: str,
    ) -> None:
        """Walk *schema*'s properties, recording protected/secret dot-paths.

        Recurses through ``$ref`` (resolved against ``$defs``) and inline
        ``object`` definitions so nested sections are fully classified.

        A ``$ref``'d submodel field drops any sibling ``json_schema_extra`` on
        the FIELD itself (Pydantic quirk, see ``packages/mewbo_core/CLAUDE.md``
        "Config curation annotations"), so a whole-section flag has to live on
        the submodel's own ``model_config`` instead, landing on the ``$defs``
        entry's top level (e.g. ``HooksConfig``). Check the ref TARGET for
        that before descending: a class-level flag protects/secrets the whole
        subtree as one opaque node — there is no need (and no way, since
        ``HookEntry``'s own fields would never be reached) to enumerate every
        leaf underneath it.
        """
        props = schema.get("properties", {})
        for name, prop in props.items():
            path = name if not prefix else f"{prefix}.{name}"
            if prop.get(_PROTECTED_KEY):
                protected.add(path)
            if prop.get(_SECRET_KEY):
                secret.add(path)
            self._classify_node(prop, protected=protected, secret=secret, path=path)

    def _classify_node(
        self,
        node: dict,
        *,
        protected: set[str],
        secret: set[str],
        path: str,
    ) -> None:
        """Descend into one property's TYPE: ref target, inline object, or array.

        A LIST ELEMENT CONTRIBUTES NO PATH SEGMENT. Items are classified under
        the array's OWN path, so a marked leaf reads as "this field, in every
        element" (``api.auth.authenticators.client_secret``) rather than naming
        one index. That is the only representation the three consumers below can
        act on: they walk a decoded config, where an index-bearing path would
        have to be re-parsed and matched positionally, and a marking they cannot
        apply is the same leak in a new place. Fanning over every element is
        also the safer reading for a redactor — it cannot miss an element.
        """
        ref = node.get("$ref")
        if ref:
            target = self._defs.get(ref.rsplit("/", 1)[-1])
            if target is not None:
                if target.get(_PROTECTED_KEY):
                    protected.add(path)
                elif target.get(_SECRET_KEY):
                    secret.add(path)
                else:
                    self._classify(target, protected=protected, secret=secret, prefix=path)
        if node.get("type") == "object" and "properties" in node:
            self._classify(node, protected=protected, secret=secret, prefix=path)
        if node.get("type") == "array":
            items = node.get("items")
            if isinstance(items, dict):
                self._classify_node(items, protected=protected, secret=secret, path=path)

    # -- classification accessors -----------------------------------------

    def protected_paths(self) -> set[str]:
        """Dot-paths of all ``x-protected`` fields."""
        return set(self._protected)

    def secret_paths(self) -> set[str]:
        """Dot-paths of all ``x-secret`` fields."""
        return set(self._secret)

    # -- schema transform --------------------------------------------------

    def public_schema(self) -> dict:
        """Return the schema with protected removed and secrets marked writeOnly.

        ``x-protected`` properties are REMOVED (and dropped from each def's
        ``required``); ``x-secret`` properties are KEPT but marked
        ``writeOnly: true``. Includes the ROOT schema's own top-level
        properties alongside every ``$defs`` entry — the root model's fields
        never live in ``$defs`` themselves (only nested submodels do), so a
        top-level protected/secret field (e.g. ``hooks``, flagged on
        ``HooksConfig``'s own class rather than the field) would otherwise be
        silently skipped.
        """
        schema = deepcopy(self._schema)
        defs = schema.get("$defs", {})
        for def_schema in (schema, *defs.values()):
            props = def_schema.get("properties")
            if not props:
                continue
            to_remove = [
                k
                for k, v in props.items()
                if v.get(_PROTECTED_KEY) or self._ref_flag(v, defs, _PROTECTED_KEY)
            ]
            for key in to_remove:
                del props[key]
            for value in props.values():
                if value.get(_SECRET_KEY) or self._ref_flag(value, defs, _SECRET_KEY):
                    value["writeOnly"] = True
            req = def_schema.get("required")
            if req:
                def_schema["required"] = [r for r in req if r not in to_remove]
        return schema

    @staticmethod
    def _ref_flag(prop: dict, defs: dict, key: str) -> bool:
        """True if *prop* is a ``$ref`` whose target carries *key* (see ``_classify``)."""
        ref = prop.get("$ref")
        if not ref:
            return False
        target = defs.get(ref.rsplit("/", 1)[-1])
        return bool(target and target.get(key))

    # -- value transforms --------------------------------------------------

    def strip_values(self, data: Mapping[str, object]) -> dict[str, object]:
        """Return a deep copy with BOTH protected and secret VALUES removed.

        Secrets are write-only — their values are never read back.
        """
        result = deepcopy(dict(data))
        self._strip(result, self._protected | self._secret, prefix="")
        return result

    def _strip(self, data: dict[str, object], paths: set[str], *, prefix: str) -> None:
        """Remove *paths* from *data* in place."""
        for key in list(data.keys()):
            path = key if not prefix else f"{prefix}.{key}"
            if path in paths:
                del data[key]
            else:
                self._strip_value(data[key], paths, prefix=path)

    def _strip_value(self, value: object, paths: set[str], *, prefix: str) -> None:
        """Descend one value, stripping through lists as well as dicts.

        A list is fanned over at the SAME prefix — see ``_classify_node``: an
        element adds no path segment, so every element is stripped by the one
        marking.
        """
        if isinstance(value, dict):
            self._strip(value, paths, prefix=prefix)
        elif isinstance(value, list):
            for item in value:
                self._strip_value(item, paths, prefix=prefix)

    def secret_status(self, cfg: Mapping[str, object]) -> dict[str, bool]:
        """Map each secret dot-path to whether *cfg* holds a non-empty value."""
        return {path: self._any_set(cfg, path.split(".")) for path in self._secret}

    @classmethod
    def _any_set(cls, node: object, parts: list[str]) -> bool:
        """Whether a non-empty value sits at *parts* under *node*.

        Since the VALUE is stripped from every read, this is the only channel
        telling an operator a secret is configured — so for a list-element path
        it reports set when ANY element carries one, matching the every-element
        meaning ``_classify_node`` gives such a path. Reporting "not set" for a
        configured credential would invite an operator to re-enter it.
        """
        if isinstance(node, list):
            return any(cls._any_set(item, parts) for item in node)
        if not parts:
            return bool(node)
        if not isinstance(node, Mapping) or parts[0] not in node:
            return False
        return cls._any_set(node[parts[0]], parts[1:])

    #: The value an ``x-secret`` field holds when it is not set. Every one of
    #: them is a string (two are optional), so empty clears them all, and it is
    #: exactly what ``secret_status`` already reports as not-set.
    _CLEARED_SECRET = ""

    def resolve_secret_writes(
        self, patch: Mapping[str, object], stored: Mapping[str, object]
    ) -> dict[str, object]:
        """Return *patch* with write-only secrets resolved against *stored*.

        A secret's value is stripped from every read, so a client editing a
        section it cannot see round-trips the field back EMPTY. Taken literally
        that erases the credential while the save still reports 200 — the whole
        deployment loses its model gateway over an unrelated edit. So an empty
        secret means UNCHANGED here and carries the stored value forward.
        Clearing one is said with an explicit ``null``: absence and empty are
        both "the client had nothing to send", and only a value the client must
        deliberately choose can mean "erase it".

        Carrying the value forward rather than dropping the key is what makes
        the rule hold inside a LIST: ``_deep_merge`` replaces a list wholesale,
        so an authenticator entry patched without its ``client_secret`` would
        lose it exactly as the top-level clobber did. Elements pair by INDEX —
        the only correspondence the wire carries, since a list-element path
        names no index (see ``_classify_node``).

        *stored* must be the operator's own document, never a dumped model: an
        ``${ENV_VAR}`` reference survives a round-trip only if what is carried
        forward is the reference written on disk rather than its resolved value.

        Cost: ``O(one record)`` — one walk of the patch.
        """
        result = deepcopy(dict(patch))
        self._resolve_secrets(result, stored, prefix="")
        return result

    def _resolve_secrets(self, patch: dict[str, object], stored: object, *, prefix: str) -> None:
        """Resolve secret paths in *patch* in place, against the same place in *stored*."""
        for key in list(patch.keys()):
            path = key if not prefix else f"{prefix}.{key}"
            held = stored.get(key) if isinstance(stored, Mapping) else None
            if path in self._secret:
                self._resolve_secret(patch, key, held)
            else:
                self._resolve_secret_value(patch[key], held, prefix=path)

    def _resolve_secret(self, patch: dict[str, object], key: str, held: object) -> None:
        """Apply the unchanged / clear / set rule to ONE secret leaf."""
        value = patch[key]
        if value is None:
            patch[key] = self._CLEARED_SECRET
        elif value == "":
            if held is None:
                del patch[key]
            else:
                patch[key] = held

    def _resolve_secret_value(self, value: object, held: object, *, prefix: str) -> None:
        """Descend one patch value, pairing list elements with *held* by index.

        Kept in step with ``_strip_value`` / ``_find_value``: an element adds no
        path segment, so one marking covers every element.
        """
        if isinstance(value, dict):
            self._resolve_secrets(value, held, prefix=prefix)
        elif isinstance(value, list):
            items = held if isinstance(held, list) else []
            for index, item in enumerate(value):
                self._resolve_secret_value(
                    item, items[index] if index < len(items) else None, prefix=prefix
                )

    def reject_protected(self, patch: Mapping[str, object]) -> list[str]:
        """Return protected dot-paths present in a PATCH payload (caller 403s).

        ``x-secret`` paths are ALLOWED in patches; what happens to their VALUES
        is ``resolve_secret_writes``' job, and it runs after this refusal.
        """
        violations: list[str] = []
        self._find(patch, self._protected, prefix="", out=violations)
        return violations

    def _find(
        self, patch: Mapping[str, object], paths: set[str], *, prefix: str, out: list[str]
    ) -> None:
        """Collect *paths* present in *patch* into *out*."""
        for key, value in patch.items():
            path = key if not prefix else f"{prefix}.{key}"
            if path in paths:
                out.append(path)
            else:
                self._find_value(value, paths, prefix=path, out=out)

    def _find_value(self, value: object, paths: set[str], *, prefix: str, out: list[str]) -> None:
        """Descend one patch value, searching through lists as well as dicts.

        Kept in step with ``_strip_value`` deliberately: the classifier can mark
        a protected leaf inside a list, and a path the stripper hides on read
        but the PATCH gate cannot see on write would be a write-side hole of the
        same shape. No protected leaf sits in a list today — this is what keeps
        that true if one ever does.
        """
        if isinstance(value, Mapping):
            self._find(value, paths, prefix=prefix, out=out)
        elif isinstance(value, list):
            for item in value:
                self._find_value(item, paths, prefix=prefix, out=out)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        """Return a compact summary of the classification counts."""
        return f"ConfigSchemaView(protected={len(self._protected)}, secret={len(self._secret)})"


__all__ = ["ConfigSchemaView"]
