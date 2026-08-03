> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `workspaces/` — name → directory, and the git-remote registry

Scope: `project_catalog.py` · `project_identity.py` · `project_store.py` ·
`project_switch.py` · `workspace.py` · `worktree.py` · `repositories.py` ·
`repository_store.py` · `repository_store_mongo.py`.

Two questions live here and they are not the same one: **"what directory does this
name resolve to"** (the catalog) and **"what git remotes does Mewbo know about"**
(the registry). A repository has no path until somebody checks it out, which is why
registering one is inert and resolving one is not.

## Workspace containment

**Containment crosses the package DAG via a contextvar.** `WorkspaceContainment`
lives in core (`workspace.py`); tools enforces it in `resolve_safe_path`. The live
object reaches tools through `active_containment()`, set around registry-tool
execution — core cannot import tools, JSON args cannot carry objects, and it
propagates into `asyncio.to_thread`. Both narrowable axes (`capability_mode`,
`workspace_mode`) share ONE `_narrow(parent, requested, rank)` on `AgentContext`.

`agent.workspace_enforcement` defaults **True**: a sub-agent's `workspace_mode`
(default `workspace_write` at the `spawn_agent` seam) confines its
file-edit/file-read/LSP tools to its own workspace root + Mewbo scratch, closing
the escape where a child names a path outside the workspace it was handed. **The
ROOT agent stays `full_access`** — it acts directly for the operator; containment
attenuates privilege across a spawn, not at the session's own root.

**Scope, stated so "enforced" isn't over-read:** this mechanism confines the PATH
ARGUMENTS flowing through `resolve_safe_path` — file-edit, file-read and LSP. Two
surfaces sit outside it by design, governed by whether the agent holds the tool at
all (`capability_mode` / `allowed_tools`): an interactive shell's command text (the
shell only ever confines `cwd` — `mewbo_tools/CLAUDE.md` → "Path guard") and any
MCP-backed tool's arguments (MCP specs are excluded from the loop's root
injection). Granting either tool is a decision independent of `workspace_mode`.

**The shell half has a SECOND enforcement point with a DIFFERENT scope model, not
this one applied twice.** `agent.shell_sandbox` (on by default) derives a
**Landlock** deny-list, applied at the `ShellSession` spawn, from the
`active_project_root` contextvar — the directory the current tool call is working
in — never from this object's `allowed_roots()`. The two axes answer different
questions: `workspace_mode` scopes PRIVILEGE (what a sub-agent may attenuate to),
the shell sandbox scopes DATA (which configured project's files a shell may reach).
**Keying the shell off containment does not work**: a root agent is always
`full_access`, so a Landlock scope hung off `WorkspaceContainment` would never
apply to the session an operator actually drives. `active_project_root` is
published UNCONDITIONALLY by `ToolUseLoop` around EVERY tool run, unlike
containment, which is a no-op for `full_access`. Mechanism and measured limits:
`mewbo_tools/CLAUDE.md` → "Shell sandbox". **MCP subprocesses remain outside
both.**

**The same contextvar scopes the PATH-TAKING tools, through the SAME resolution.**
Sandboxing only the shell leaves `read_file`/edit/LSP resolving against the
historical tenant union, so `read_file` reaches what `cat` cannot — the sandbox
routed around by not being a shell. `resolve_safe_path` reads
`ShellScope.readmitted_for(get_active_project_root())`, gated on its own
`agent.path_scope_to_active_project`. One resolution, two enforcement points, two
switches — `mewbo_tools/CLAUDE.md` → "Path guard" for why the switches stay
separate.

## Repository registry (`repositories.py` + `repository_store.py`)

A **repository** is a git remote Mewbo knows about, and a product-level object in
its own right — distinct from a wiki `Project`, which is an INDEX of one. They are
separate because they cost different things: registering is inert, indexing is a
paid agent run.

- **Why core, not `mewbo_graph`.** The graph library is an optional extra; agentic
  tasks run on a base install without it. A registry hosted there would be missing
  from precisely the deployments that need it, and neither the wiki nor a
  base-install app could import the other's copy. Core is the only layer both
  import DOWN into. `PlatformId` lives here for the same reason and is re-exported
  from `mewbo_graph.wiki.types`, so every existing importer keeps resolving.
- **Registration is INERT, and that is the feature.** `register` normalizes,
  validates, dedupes and persists. It does not clone, `ls-remote`, resolve a
  credential, probe reachability or start an index — the happy path touches no
  network at all, which makes naming a repository cheap enough to be the first
  thing a user does. Consumers opt in later and deliberately: the wiki through its
  configure wizard, an agentic task through a checkout. Deregistration is inert too.
- **ONE grammar, per-consumer PROJECTION — the distinction is the whole design.**
  `RepositoryRef.split_remote` is the single grammar (scheme URL, scp-style
  `git@host:owner/repo`, bare slug; host lowercased, trailing `.git` stripped,
  leading/trailing slashes dropped), and `CredentialScope` + the api's
  `RepoIdentity` both delegate to it. An INTERIOR empty segment (`host/o//repo`) is
  deliberately preserved rather than collapsed, because the two consumers disagree
  about it — a credential scope refuses such a value outright, a repo reference
  filters it out — and healing it in the grammar would silently change both. Each
  keeps its own reading of the resulting parts, and merging THAT breaks one of them:
  a reference with no structural host reads its lone token as a bare HOST to a
  credential scope and as a repo NAME to a repository reference. **Share the
  grammar; never the projection.**
- **`namespace` is what keeps `slug` lossless.** A GitLab subgroup
  (`gitlab.com/group/sub/proj`) has more segments than `host/owner/repo` can hold.
  `host` stays the first segment (what a host-scoped credential is shared by),
  `owner`/`repo` stay the LAST TWO, and the middle is preserved — dropping it
  silently re-keys that project's pages, jobs and credentials onto a different slug.
- **Strict at the WRITE boundary, tolerant on READ paths** — `from_url`/`from_slug`
  raise, `coerce` returns `None`, mirroring `CredentialScope`. A ref with an empty
  part stringifies to a slug that misses every store lookup, which is how a
  malformed credential scope degrades a private clone to anonymous with no visible
  cause. `from_slug` additionally refuses a URL, so the same repository cannot be
  registered under two spellings.
- **`Repository` derives `host`/`owner`/`repo` FROM the slug in a `mode="before"`
  validator**, rather than accepting them alongside it. It runs on every
  construction and every re-validation of a stored document, so the triple and the
  key cannot drift apart even if a caller supplies one out of step.
- **Re-registration MERGES, and reads "unset" off `exclude_unset`, not off the
  value.** `platform` has a meaningful non-empty default, so a re-registration
  silent about it would clobber a stored `github` back to the neutral `git`.
  `created_at`/`origin` are immutable after the first write. Clearing a field is
  `RepositoryPatch`'s job, never registration's.
- **The store follows the composition pattern in `secrets/CLAUDE.md`.** The Mongo
  driver is imported lazily by the factory, and **the non-Mongo default is the
  FILE-backed driver, not the in-memory one:** a registry that forgets every
  repository on restart is not a registry.
- **`RepositoryUsage` is a pure projection with no I/O.** What the wiki, a task or
  a credential knows about a repository is a join across three stores, none of which
  the model may reach for — the REST layer resolves each leg and hands it in as an
  argument, the same rule that keeps `TriggerSpec` taking the clock as a method arg.
  Every leg is nullable and defaults to absent, because "never indexed" is the
  ORDINARY case for a registry whose registration is a no-op: **an absent leg is a
  fact, not a failure to look.**

## Project catalog (`project_catalog.py`) — the one name→directory resolver

Four unrelated things here are called a "project" and only three are workspaces an
agent can run in: a **configured** project (`ProjectConfig`, a directory an operator
registered in `app.json`), a **managed** project (`VirtualProject`, one Mewbo
created and owns, addressed `managed:<id>`), a **worktree** (a managed project with
`is_worktree`), and a **repository** (a git remote identity with NO path until
somebody checks it out). A wiki `Project` is an INDEX of a repository, not a
workspace, and is absent here.

- **Why core.** The CLI drives `ToolUseLoop` in-process with no `mewbo_api`, and the
  tools that consume this are core. An app-side home would sit above two of its own
  callers in the DAG.
- **One resolver, because a duplicated one diverges at whichever copy nobody
  re-reads.** App-side copies resolved a configured project but returned nothing for
  `managed:<id>`, so scoping `/api/tools?project=` or `/api/skills?project=` to a
  worktree-backed session silently fell back to the unscoped list.
- **The repository→checkout join is an INJECTED locator, not catalog logic.**
  Matching a remote identity against a managed project's git remotes needs the app's
  canonical matcher, which is above core. The app injects `checkout_locator`, and a
  core-only caller lists repositories with no path — the honest answer rather than a
  wrong one. Same rule as `RepositoryUsage`: share the grammar, hand in the legs.
- **`resolve()` REFUSES the `auto` sentinel rather than returning a temp dir.** If
  it resolved, "no project chosen yet" and "chose the scratch directory" would be
  indistinguishable at every downstream call site. Callers that legitimately fall
  back to a temp directory test for the sentinel themselves, before asking.
  `AUTO_PROJECT`/`is_auto_project` are declared once and imported — a duplicated
  sentinel literal is how a tag prefix came to be stamped with no classifier arm
  able to read it (`session/CLAUDE.md` → "Trace provenance"). **`AUTO_PROJECT`
  itself lives in `project_identity.py` and is re-exported here**, because this
  module cannot be imported from the session store's append hot path — it pulls
  config, the project store and the repository store behind it, and the hook only
  needs to RECOGNISE the sentinel, not resolve it.

## Project identity (`project_identity.py`) — declaring vs resolving

Two questions, deliberately split across two modules. The catalog answers **"where
on disk is project X"** and needs the whole dependency graph above. `ProjectIdentity`
answers the cheaper one that comes first — **"given a session's context payload,
which project is it CLAIMING?"** — and needs nothing but the payload. That is what
lets the session store call it on every context append without dragging the
resolver behind it. One definition serves the store's append hook, `SessionQuery`'s
validator and the MCP surface.

- **Pure and I/O-free, like `SessionOrigin`** — the payload arrives as an argument,
  so the same rule serves the append path, a filter predicate and a test with no
  clock, store or catalog in sight.
- **`normalize` collapses THREE ways of declaring nothing into `None`**: absent,
  empty, and the `auto` sentinel. Storing the sentinel would make `?project=auto`
  look like a real query and file every not-yet-decided session under one name. The
  same normalization runs on the REQUESTED side, which lets a caller pass
  `managed:<uuid>` and still match the bare identity recorded from it.
- **`repo` wins over `project`** — it names the underlying repository, which is what
  a user means by "this project", whereas `project` may be a local alias.
- **A refusal NAMES what is available.** Same contract as the path guard
  (`mewbo_tools/CLAUDE.md`) and for the same measured reason: a mute denial trains a
  model to hunt for workarounds, while one that lists the real options is corrected
  in one turn.
- **⚠️ A dedup key no producer populates is DEAD CODE, and it reads as working.**
  Keying the repository/checkout dedup on `ProjectEntry.repo` consults a field only
  the repository leg sets, and only on rows it has not appended yet — the set is
  unconditionally empty, the skip unreachable, and one directory is listed twice
  under two names on every call, with nothing raised. Compare the thing that can
  actually BE the same twice: the resolved path, via `realpath` so a symlinked
  projects root still matches, stamping the identity onto the surviving entry rather
  than merely suppressing the duplicate. **When you write a dedup, name the producer
  of its key before trusting it.**
- **Switching INTO a managed project works because the loop injects `root`, not
  because the path is allowlisted.** `_get_allowed_roots` (`mewbo_tools/core`) is
  either the session's own active project or `os.getcwd()` +
  `config.projects[*].path`, plus the Mewbo scratch roots — a managed project's
  directory is in NONE of them. `resolve_safe_path` checks an explicit `root` first,
  and `ToolUseLoop._tool_call_to_action_step` supplies it, injecting the session cwd
  (or, under active containment, the authoritative workspace root) into every
  registered non-MCP tool's arguments. **That injection is what makes managed
  workspaces reachable at all; do not "simplify" it.** It survives active-project
  scoping because the injected cwd IS the published active root, so it never widens
  — a `root` the MODEL supplies from outside that project is the one case scoping
  drops.
