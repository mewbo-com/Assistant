> ↑ [root /CLAUDE.md](../CLAUDE.md)

# Documentation site — authoring guide

Scope: `docs/` — the MkDocs site (`mkdocs.yml`, theme `shadcn` / mkdocs-shadcn-mewbo). The REST
reference is Scalar over `docs/openapi.json`; most pages are hand-authored markdown.

## Code-reference badges

**Any reference to an artifact we own renders as a badge, never a bare hyperlink or inline
code.** Two kinds, and only these two:

1. **A file in this repo** — `` [`backend.py`](repo:apps/mewbo_api/src/mewbo_api/backend.py) ``,
   with an optional line range `…#L12-L20`.
2. **One of our REST endpoints** — `[POST /api/sessions](endpoint:POST /api/sessions)`, method
   optional.

Write a normal markdown link with a `repo:` or `endpoint:` URI. The theme
(mkdocs-shadcn-mewbo >= 1.4.0, configured via `code_refs:` in `mkdocs.yml`) rewrites it to a
badge at build time — file badges are SHA-pinned to the build commit, endpoint badges are
method-tinted and deep-link into the Scalar reference. Badge styling and rewrite logic both ship
with the theme. **Never hand-write badge HTML.**

### Do not badge — leave as plain inline code

- **Anything we do not own**: third-party endpoints, external URLs, package names.
- Config keys, env vars, CLI commands, tool/event/field names, JSON values, MIME types.
- `configs/app.json` — user-created and gitignored, so it is not a committed artifact.
  `configs/app.schema.json` and `configs/app.example.json` are, so badge those.
- **Headings and fenced code blocks** — a badge breaks the TOC anchor or the code sample.

### Guardrails

- A `repo:` path must be repo-root-relative and must exist. Verify before adding, and omit the
  line range unless you have confirmed the lines.
- **An `endpoint:` badge only resolves for a surface that reaches the spec.** The Scalar
  reference is generated from the Flask-RESTX schema alone
  (`scripts/ci/generate_openapi_spec.py` captures `api.__schema__`), so plain Flask **Blueprint**
  surfaces — `/v1/wiki/*`, `/v1/git/credentials*`, `/v1/git/repositories*` — contribute zero
  paths to `docs/openapi.json`. A badge for one of those points at an operation the reference
  does not contain. Write them as plain inline code.
- **A namespace `backend.py` mounts behind a config flag DOES reach the spec, and only because
  the generator mounts it itself.** Capturing the live schema alone made the reference a
  function of the generating machine's config — the Web IDE namespace, which needs both
  `agent.web_ide.enabled` and a Mongo-backed session store, was absent from every published
  build. `GATED_NAMESPACES` in the generator is the list; a new config-gated namespace must be
  added there or it is invisible to readers, with an empty diff to show for it.
- Minimal edits: swap the reference token to a badge link, do not rewrite prose.

## Build

- `make docs-build` = `mkdocs build`. Strictness comes from `strict: true` in `mkdocs.yml`, not
  from a CLI flag, so every caller gets it — including the docs-host leg, which accepts no
  strict flag of its own.
- The badge hook runs `on_page_content` against rendered HTML, so refs inside code fences
  survive untouched.
- A backtick link label renders to `<code>` inside the anchor; the hook strips inline tags to
  plain text, so do not rely on the wrapper.
- `docs/hooks/` is in `exclude_docs`, so build scripts are not published.
- **`configuration.md` and `openapi.json` are generated** — edit the generators
  (`docs/hooks/schema_to_md.py`, `scripts/ci/generate_openapi_spec.py`), never the output.
  `configuration.md` emits the same `repo:` scheme; keep new generators consistent.
