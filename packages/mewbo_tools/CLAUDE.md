> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo Tools — Integrations Guidance

`packages/mewbo_tools/src/mewbo_tools/` — heavyweight tools and external integrations: file
edit, MCP connection pool, LSP, Aider bridge, vendored glue. Deps `mewbo_core` only.

A tool belongs HERE when it brings a large external dependency, manages a long-lived subprocess
or remote connection, or is environment-sensitive. A thin in-process function on Python state
goes in `mewbo_core/builtin_plugins/`. Reusable domain engines (graph/memory/embedding) are not
tools — they belong in `mewbo_graph`.

## MCP connection pool

`integration/mcp_pool.py:MCPConnectionPool` is the only MCP transport layer; never spawn a
client one-off. It holds a persistent connection per configured server, auto-reconnects after 3
consecutive errors, applies a per-request 60s timeout, and detects config changes via a
fingerprint hash. A one-shot client remains as a fallback for environments where the pool cannot
start.

**A slow or unreachable server must never block startup or an unrelated tool call, and lowering
the connect timeout is NOT the fix** — any server can legitimately take arbitrary time. Four
cooperating seams enforce it:

- **Non-blocking startup** (`tool_registry._ensure_auto_manifest`). Discovery is gated on a
  stored `config_hash`: when the cached manifest was built from the *same* MCP config, startup
  reuses it and does **no** live connect, and the pool connects **lazily on first use**. A config
  edit changes the hash and re-triggers discovery; `/mcp` refresh deletes the manifest +
  `reset_mcp_pool()` to force a re-probe, also clearing backoff/quarantine state.
- **Deferred wait-on-first-use.** `refresh_if_config_changed(..., connect=False)` updates config
  and prunes removed/changed servers but **never eagerly dials** — the connect defers to
  `get_or_connect(server)` for the one server actually needed. This is where the on-demand
  `tool_search` round-trip waits for a still-connecting server.
- **Quarantine + backoff** (`_record_failure`). Every failure is unwrapped
  (`unwrap_exception_group` peels the opaque anyio `TaskGroup` wrapper) and classified
  (`classify_connect_failure` → auth/config/dns/refused/timeout/other). **Auth/config never
  auto-retry** — they quarantine until the config hash changes. Transient causes get exponential
  capped backoff; a server inside its window fast-fails in `get_or_connect` instead of being
  re-dialed. `status_snapshot()` surfaces connected/backoff/quarantined/failed/pending in `/mcp`.
- **No per-tool-call churn** (`MCPToolRunner._invoke_via_pool`). A server that
  `pool.is_connected()` skips the config reload + refresh entirely, so a healthy tool call never
  re-dials every other, possibly dead, server mid-query.

## File edit tools

`integration/edit_common.py` is the shared helper for both implementations
(`search_replace_block` and `structured_patch`). Both emit `{"kind": "diff", ...}` so downstream
renderers do not discriminate; route a new variant's diff emission through it.

The active implementation is `AgentConfig.edit_tool`; when empty,
`ToolUseLoop._configured_edit_tool_id()` auto-picks from the ACTIVE model identity via
`llm.model_prefers_structured_patch()`. **The model→variant decision is controllable DATA, not
code:** that function reads `mewbo_core/prompts/model_variants.yaml` via `ModelVariantRegistry`.
Onboard a model or flip its variant by editing that file (longest-prefix match), not Python;
`llm.structured_patch_models` overrides on top. Selection reads the ACTIVE model, so an escalated
model gets ITS variant. Pair a variant with a per-model prompt nudge via a `kind: model` override
on the `file.tools.*` entry under the SAME prefix.

## LSP integration

`integration/lsp/` wraps pygls + lsprotocol and is gracefully absent when the libraries are not
installed — `LspTool` checks at import time and skips registration. Managers are per-session.
Built-in server detection uses `shutil.which`; operators override or add servers via
`agent.lsp.servers` in app.json. Built-ins: pyright, typescript-language-server, gopls,
rust-analyzer.

⚠️ **`shutdown_lsp_managers()` has ZERO production callers.** Nothing in `tool_use_loop.py`,
`orchestrator.py`, `session_runtime.py` or `backend.py` calls it; its only exerciser is its own
test, so LSP managers and their server subprocesses live for the whole process. Treat it as an
available primitive that is not wired, never as a precedent for how teardown works — the live
seam is `HookManager.on_session_end`, which `backend.py` appends to in three places.

Passive diagnostics: after every file edit, `ToolUseLoop` runs an `_append_lsp_feedback` hook
that asks the LSP for diagnostics on the edited file and appends them as a tool result. This is
what catches "you forgot to import this" without explicitly invoking the LSP tool.

## Path guard (`core/__init__.py:resolve_safe_path`)

The allowlist is the TENANT boundary on a shared multi-tenant host — never widen it to `/tmp`
(proven symlink → `/etc/passwd` read via the logical-view branch, plus cross-session reads). It
has TWO shapes, chosen by `_active_scope_roots()`:

- **Scoped** — `agent.path_scope_to_active_project` (on by default) AND the loop published an
  active project root: the session's OWN project, its `allowed_paths`, and the scratch roots.
  Nothing else — not another configured project, and not the process CWD, which IS the harness on
  a container deployment.
- **Historical union** — flag off, or no published root (a direct library caller, a test):
  CWD ∪ every configured `projects[*].path` ∪ the scratch roots.

**Why this axis and not `WorkspaceContainment`:** a root agent is always `full_access`, so
containment never applies to the session an operator actually drives, and the union let
`read_file` return a sibling project's checkout and the harness's own config — routing around the
shell sandbox by not being a shell. Measured live, not inferred.

**Both halves read ONE derivation, and there are TWO of them** — `ShellScope.readmitted_for`
answers "which project is this session's", and `ShellScope.denied_for` answers "what may it not
reach". The second exists because the first was the only one shared: `for_active_root` read
`agent.shell_denied_paths` and the path guard never did, so an operator who denied a directory got
it denied to the shell and ALLOWED to `read_file` — the same escape, reached by not being a shell.
They keep SEPARATE switches on purpose: `shell_sandbox` also governs whether a KERNEL mechanism
applies at all, while a `shell_denied_paths` declaration is POLICY, so the path guard honours it
under its own `path_scope_to_active_project` with no kernel involved.

**The denial is a POST-CONDITION at `resolve_safe_path`'s return (`_refuse_denied`), not a step in
root construction.** The function has TWO mutually exclusive exits — the containment branch and
the tenant union — each building its own root set, so subtracting inside `_get_allowed_roots`
would be invisible to every contained sub-agent. A re-admitted (`allowed`) root wins over a
denial, the same precedence Landlock's compiled grants give it — **which means a
`shell_denied_paths` entry INSIDE the session's own active project is void in BOTH halves**, since
`readmitted_for` puts the active root in `allowed` and `grants()` re-admits it wholesale. An
operator wanting a directory hidden must not bind the project containing it.

**Under scoping an out-of-scope `root` ARGUMENT is ignored, not honoured.** With no containment
the loop passes a model-supplied `root` straight through and the guard inserts it at the front of
the check list, so honouring one outside the session's project would make the scope model-chosen
— precisely what the sandbox refuses to derive from tool arguments. A root that NARROWS (a
worktree inside the project) still resolves. **The gate is the FLAG, never "was a root
published".** Reading an empty `_active_scope_roots()` as "not scoped" inverted the
no-published-root case against Landlock, which falls CLOSED there — and it re-admitted whatever
`root` the model named, so `read_file(path="id_rsa", root="…/.ssh")` resolved on any run that
published none. `SessionRuntime.run_sync`'s `cwd` defaults to `None` and `StructuredResponder`
never passes one, so every `/v1/structured` run is that shape.

**The residual, deliberate:** with no published root the "every OTHER configured project"
component of the denial set is dropped, so a sibling configured project stays reachable to the path
tools while Landlock denies it. Falling fully closed there breaks every direct library caller, test
and CLI invocation. The operator-declared and `harness_roots` components still apply, which is what
closes the harness source and the directory holding `app.json` in that case — and they apply
whatever the active root is, since `harness_roots` answers to `agent.harness_self_deny` alone and
no longer to whether the active root happens to contain the harness. **That means BOTH halves of
the boundary move together on one knob**, which is the point: an operator developing Mewbo who
turns it off gets `read_file` and the shell agreeing, rather than one of the two.

- **`resolve_safe_path` is NOT a boundary for the shell, and never was.** It validates a path
  ARGUMENT; a shell command is an opaque string, so the one call the shell tool makes selects the
  subprocess's starting `cwd` and nothing more — `args.command` is never parsed. Demonstrated
  live from a `cwd` confined to one project: `grep -r`, `cat` and `python3 open()` each returned
  a sibling project's secret verbatim, exit code 0. Say this wherever the guard is described; the
  failure mode is someone planning against a boundary that does not exist. The kernel-level half
  that DOES hold for the shell is `integration/landlock.py`.
- **A rejection must name what IS allowed.** The error lists the allowed roots so the model
  restages in one turn; since the shell only guards `cwd`, a mute denial trains the model to
  bypass the guard via `cat`.
- **Prompt–guard agreement.** No skill/agent/doc may teach a path the guard rejects — a SKILL.md
  teaching bare-`/tmp` staging, or a `/tmp/mewbo_gh_results.json` *sibling* of `/tmp/mewbo`, is
  the real root cause behind "/tmp is broken" reports. When touching the allowlist or any staging
  instruction, grep prompts and the roots together.

## Inline `@<ref>` expansion + file catalog

`integration/reference_expansion.py:ReferenceExpander` is the submit-time preprocessor that
expands `@file`/`@dir/`/`@diff`/`@https://…` tokens into bounded inline context blocks
(truncate-not-reject caps, dedupe, no recursion, unresolved → literal). It lives HERE, not in an
app, because BOTH the api and the in-process CLI invoke it at their own submit seams and an app
cannot import another app. Each app calls `expand_references(text, cwd, attachments=)`; the api
builds the session's attachment map first. It composes existing renderers
(`mewbo_core.session.attachments.parse_to_markdown`, `git diff HEAD`, `FileCatalog`) — never a
parser per type.

`integration/file_catalog.py:FileCatalog` is the single "what files belong to this project?"
authority: git-index first (`git ls-files --cached --others --exclude-standard`, worktree-safe
via `-C`), bounded-walk fallback for non-git dirs. It backs THREE agreeing callers — the
expander's scoping gate (`contains`), the api's `/api/files` autocomplete, and the CLI completer
(`list_files`) — and `.gitignore`d secrets are out of scope by construction. Neither class ever
raises: a missing repo, absent `git` or unreadable tree degrades to empty.

## Aider bridge

`aider_bridge/` is a thin shim exposing the Aider library as a Mewbo tool; Aider has its own
opinions about prompt engineering, edit format and file context and we do not fight them.

`integration/aider_shell_tool.py` deliberately does NOT use the vendored `run_cmd`: it picks a
pexpect PTY whenever the parent has a tty, which lets a paginated command like `git log` block
forever on `less`, leaks the interactive rc-file banner into tool output, and orphans children
after core's tool-call timeout gives up on the hung await. A pager-safe env
(`GIT_PAGER`/`PAGER=cat`, `GIT_TERMINAL_PROMPT=0`) and `start_new_session=True` apply to every
command, and `DEFAULT_TIMEOUT_S` sits just under `ToolSpec`'s 120s default so the tool reaps
itself — cancelling the outer `asyncio.wait_for` cannot kill a blocking subprocess.

## Shell sessions (`integration/shell_session.py`)

The ONE process primitive behind both shell tools. A foreground call is the same `ShellSession`
waited on until it exits, so there is no parallel one-shot path. `ShellSessionStore` is the
bounded registry; `SHELL_SESSIONS` its single process-wide instance.

- **All four verbs ship together — start, read, write, kill.** A background start nothing can
  read or stop is a leak with a feature's name.
- **A PTY is opt-in per call (`tty`), never inherited.** That is what reconciles "drive an
  interactive program" with the pexpect burn above: the model asks for a terminal when it intends
  to write to one.
- **Foreground gets `stdin=DEVNULL`; only a backgrounded session gets a pipe.** A foreground call
  returns no `shell_id`, so nothing can ever write to it, while an open pipe leaves `cat`/a
  pager/a credential prompt blocked until the timeout. Proven live: opening the pipe
  unconditionally took `cat` from instant to 115s.
- **The drain thread is load-bearing.** A pipe holds ~64KB and an undrained child BLOCKS on write
  once it fills, so "background" becomes nominal. It must read the raw DESCRIPTOR —
  `BufferedReader.read(n)` blocks until n bytes or EOF, silently reducing every incremental read
  to "wait for the end".
- **Exit and the final append are NOT ordered.** A read that trusts `status` alone drops the tail
  — the end carrying the traceback. `read()` settles the reader before its last buffer read, so
  "exited" means "everything is here".
- **Liveness is read from the process handle, NEVER from output.** A block-buffered child prints
  nothing for its whole run; an empty read is not a finished command.
- **The cursor is ABSOLUTE over the whole stream**, so a reader that fell behind learns it MISSED
  output (`missed_characters`) instead of receiving a later window as though it were contiguous.
- **Bounded with a named eviction rule** (`MAX_SESSIONS`): terminal sessions go LRU to make room,
  and a store full of RUNNING ones REFUSES a new start naming the cap and how to free a slot. It
  never kills work nobody asked to stop.
- **Reaping is LAZY, on the access path, plus `atexit`** — deliberately not a reaper thread,
  which is one more thing that can stop running unnoticed. A sweep on create/get/list cannot rot.
- **KNOWN GAP:** `start_new_session=True` detaches these from the parent's signal group. A clean
  shutdown reaps via `atexit`, but a `SIGKILL` of the api leaves orphans with no registry, and the
  idle TTL cannot help because the store enforcing it is gone. Surviving that needs an on-disk
  status sidecar.

## Shell sandbox (`integration/landlock.py`)

The kernel half of workspace containment, and the ONLY thing that actually confines the shell.
Gated on `agent.shell_sandbox`, **on by default** — an unbound session reaches no configured
project at all, and opening one reveals exactly that project plus its own `allowed_paths`.

- **It is a DENY-list, and that direction IS the design.** The allowlist version broke the harness
  twice, both silently: a denied virtualenv `site-packages` surfaces as `ModuleNotFoundError`, not
  a denial, because an unreadable directory just makes Python's path finder come up empty, and a
  denied `/dev/null` failed the same quiet way. Naming what must be DENIED is a short, knowable
  list; naming what must be ALLOWED is every dependency of every command an agent might run.
- **Landlock has no deny rule, so a denial is COMPILED into grants.** `ShellScope.grants()` walks
  the ancestry of each denied path and, at every level, grants the siblings that do not lead to a
  denial — mechanical (`os.listdir` at build time), not a list anyone maintains. Measured: ~95
  rules on a real deployment shape, about a millisecond to compile and apply; 28 rules in-container.
- **A true deny primitive is not available here.** A mount namespace with an empty overmount would
  give one, but unprivileged user namespaces are refused on BOTH the host
  (`apparmor_restrict_unprivileged_userns=1`) and inside the container (seccomp), and `bwrap` is
  absent. Buying one means granting `CAP_SYS_ADMIN`, a larger hole than this closes.
- **Scope is keyed on the BOUND PROJECT, never on the containment tier — this is the trap.**
  `workspace_mode` scopes PRIVILEGE; the sandbox scopes DATA. A root agent is always
  `full_access`, so a scope hung off `WorkspaceContainment` would never apply to the session an
  operator actually drives. The carrier is the `active_project_root` contextvar, published
  UNCONDITIONALLY by `ToolUseLoop` around EVERY tool run — unlike containment, which is a no-op
  for `full_access`. **"Every" means the WHOLE dispatch chain, and for a while it did not:** the
  `with` sat inside the final registry `else:`, so `spawn_agent`/`spawn_agents`/`check_agents`/
  `steer_agent`, every SESSION TOOL, `tool_search` and `activate_skill` each ran with
  `get_active_project_root() is None` and fell back to the unscoped tenant union. A branch added
  to that chain inherits the publication only because the `with` wraps the chain, not a branch.
- **The scope must never derive from tool ARGUMENTS.** With no containment the loop honours a
  model-supplied `root`, so deriving the sandbox from one makes a model-chosen root a
  model-chosen sandbox.
- **The harness denies ITSELF unconditionally (`ShellScope.harness_roots`), and the one exception
  is a knob, not a derivation.** `agent.harness_self_deny` is on by default; off is the one-line
  opt-in for a workstation where Mewbo's packages ARE the work. There used to be a carve-out —
  drop any harness root lying inside the active root — and measured on the deployed container it
  did not narrow the self-deny, it ERASED it: `harness_roots(None)` returned
  `['/app/apps', '/app/configs', '/app/packages']` and `harness_roots('/app')` returned `[]`,
  putting the file holding the API keys back in reach. **A wrong carve-out is invisible on the box
  that matters, while a wrong denial is obvious on the box that does not.** Three derivations have
  now failed — "is the harness inside the active project" is the one that just did, "am I in a
  container" is inverted for the dev container, "CLI vs API" is invisible to a scope object — so
  do not add a fourth guess.
- **Path-prefix denial cannot cover an ALIASED mount of the same inodes, and that is a topology
  problem.** Measured: a local compose override bind-mounts the host checkout over the baked
  `/app`, so `/app/packages/…/config.py` and the host project path are the same `st_dev`+`st_ino`
  — and `os.path.realpath` does NOT collapse a bind mount, so denying the `/app` prefix leaves
  every one of those files readable by its project path. The cure is not to bind-mount the source
  over `/app`. **Do not "fix" it with inode-identity denial**: that would deny a legitimate project
  mount, and a project mount must behave like any other project.
- **Never deny a path that CONTAINS the runtime.** Denying `/app` (which holds `/app/.venv` on the
  deployed shape) leaves Python unable to start — measured, not assumed. `ShellScope._survivable`
  refuses any denial covering `sys.prefix`/`sys.base_prefix` and refuses the filesystem root
  outright, logging a warning rather than silently honouring it. Operators name subdirectories
  (`/app/packages`, `/app/configs`), never the runtime's own root.
- **The compile's one real limitation:** a NEW entry created directly inside an EXPANDED ancestor
  is invisible without a rebuild, since the ruleset is a `listdir` snapshot at spawn time. Rebuilt
  per spawn, so the window is one command wide. The default deny set never hits this; it would
  bite a deployment that denies something directly under `/tmp`.
- **The ruleset is built in the PARENT; only `restrict_self` runs in the child**, through a
  `PyDLL` handle rather than the ordinary `CDLL` one — `CDLL` releases the GIL around every call,
  and a drop/take cycle on a GIL mutex copied from a multithreaded parent at fork time can block
  forever on a lock with no owner. These syscalls do not block, so holding the GIL costs nothing.
- **A denied child says so on fd 2 before it dies.** `os._exit` raises nothing, so the parent's
  `Popen` succeeds regardless; without the notice a refused spawn is exit 127 and an EMPTY buffer,
  indistinguishable from "command not found" with nothing logged.
- **The spawn happens OUTSIDE `ShellSessionStore._lock`.** `Popen` blocks until the child execs or
  dies, and with a `preexec_fn` that is no longer a bounded wait; holding the store lock across it
  would let one wedged child freeze every other verb in a single-worker deployment.
- **Monotonic.** Rulesets intersect, never replace: a child calling `restrict_self` again with a
  wider set still cannot reach the addition — the same attenuation law as `AgentContext._narrow`.
- **It degrades, never fails a run — including at IMPORT.** `prctl` is Linux-only, so resolving it
  with a bare attribute access raises at import on macOS/BSD, and this module is imported by
  `shell_session`, which the tool registry imports — the whole shell toolchain becomes
  *unimportable* rather than gracefully absent, and CI stays green because it only runs Linux.
  Symbols resolve through `_resolve`, and a missing one degrades through the ordinary probe path.
- **Known limit, stated so nobody over-reads "enforced":** Landlock does not gate unix sockets, so
  it never closed the Docker-socket escape — that is a topology fix.

## Launcher shim (`integration/sandbox_launcher.py`)

The same `ShellScope`, applied at the two spawn seams inside somebody else's library: an MCP
**stdio** server (spawned by the adapter) and a language server (spawned by `pygls`). Neither
call takes a `preexec_fn`, but the command string is ours, so the shim is prefixed onto it,
applies the ruleset to ITSELF (`ShellScope.apply_to_self`) and `exec`s the real server, which
inherits it because a ruleset survives `execve`. One policy object, one more application point;
there is no second deny list.

- **Gated on `agent.server_sandbox` AND `agent.shell_sandbox`, off by default.** The conjunction
  is the point: same kernel mechanism, so a deployment that turned the shell sandbox off must not
  find it reapplied under its language servers. Off by default because a denied path inside a
  server surfaces as a *missing file*, not a refusal — the same silent shape that broke the
  original allowlist — and which servers a deployment runs is not knowable in advance.
- **The scope travels in the ENVIRONMENT (`MEWBO_SANDBOX_SCOPE`)** as
  `{"denied": [...], "allowed": [...]}`, not argv, where a path list has to survive two quoting
  layers. It must be set in the MCP server's **config `env`**, never `os.environ` — the MCP stdio
  transport hands the child a fixed inherited subset merged with the config's `env`, so a variable
  set in the parent simply does not arrive. `pygls` is the opposite: `create_subprocess_exec`
  REPLACES the environment, so the sandboxed arm carries `os.environ` over explicitly and the
  unsandboxed arm passes no `env` kwarg.
- **It degrades at every step and never fails a launch** — no Landlock, no variable, bad JSON, a
  refused ruleset: one line on stderr and it `exec`s anyway. That is the OPPOSITE of the shell's
  child hook, which kills a shell it could not confine, and it is deliberate: a model-authored
  shell command has no claim to run, whereas a server that fails to start takes tool discovery or
  editor diagnostics down wholesale. Never write to stdout from it — for both server kinds stdout
  IS the protocol.
- **MCP is scoped per SERVER, not per session, and that is a real limitation.**
  `MCPConnectionPool` is process-wide and a connection outlives the tool call that opened it, so a
  session-keyed scope would let the first session to dial a server pin what every later session
  inherits — a boundary that reports healthy and enforces the wrong thing. The scope is the
  server's own configured `cwd`; a server naming none is denied **every** configured project. LSP
  has no such problem: a manager is built per session around a fixed `cwd`.
- **The pool stores the ORIGINAL config, never the launch form.** `refresh_if_config_changed`
  compares `ServerState.config` against the config it is handed, so storing the wrapped one makes
  every refresh see a difference and reconnect the whole fleet.
- **Cost: one interpreter start (~0.2s) per server START**, almost all of it imports. Acceptable
  only because it is never on a tool-call path.

## Vendored code

`vendor/` holds license-compatible third-party code we cannot depend on as a package. Do not
modify it directly — patch via a wrapper in the same package. Each vendored module carries a
`VENDOR.md` naming upstream URL, pinned version, license and patches applied.

## Testing

Integration tests live in the root `tests/` with mock I/O. MCP tests mock at the
`langchain_mcp_adapters` boundary, LSP tests at the pygls boundary — never spawn real language
servers in CI. File edit tests use real string transforms against in-memory file contents.
