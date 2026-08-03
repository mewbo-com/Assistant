> ↑ [root /CLAUDE.md](../CLAUDE.md)

# Tests — Project Guidance

Scope: the root `tests/` suite and shared test patterns.

## Where the suites live

Each file's own docstrings list what it covers; only the non-obvious seams are
recorded here.

| Suite | The seam it pins |
|---|---|
| `test_tool_use_loop.py` | The loop runs to natural completion — there is no `max_steps`. Drives `asyncio.run()` + `AgentContext.root()` + `AsyncMock` on `model.ainvoke`. |
| `test_agent_context.py` | Hypervisor lifecycle, admission, budgets, stall detection. `send_message`/`cancel_agent`/`send_to_parent` return `str \| None` — None means SUCCESS, a str is the diagnostic failure reason. |
| `test_spawn_agent.py` | Tool filtering goes through `filter_specs()` in `tool_registry`, so mock `mewbo_core.tooling.tool_registry.get_config_value` — NOT `spawn_agent.get_config_value` — when testing config-denied tools. |
| `test_user_turn_persistence.py` | The accepted turn is recorded exactly once. `start_async` writes the `user` event before the executor's cold start, asserted through explicit gates and never sleeps (a timing-based version passes regardless of which seam wrote it); a refused start writes neither `run_accepted` nor `user`; the default `user_turn_persisted=False` keeps a direct `Orchestrator.run` writing its own. The cardinality test drives the REAL `start_async → run_sync → orchestrate_session → Orchestrator.arun` chain, stubbing only `ToolUseLoop.run`, so a flag dropped at any hand-off surfaces as a duplicate turn. |
| `test_package_imports.py` | Every module of all seven shipped packages, one fresh interpreter per package, probed concurrently (~20s). Discovery excludes only what has no dotted name — a `.py` file whose directory has no `__init__.py` — and that excluded set is pinned EXACTLY, so an `__init__.py` vanishing from a real package directory fails instead of silently dropping every module beneath it out of the sweep. |
| `test_packaged_resource_anchors.py` | The sibling defect an import gate cannot see: a packaged asset resolved from a module's own `__file__` instead of from the package that owns it. |
| `test_hooks_http.py` | Mocks `_post_json` to avoid real HTTP; covers session env enrichment (`MEWBO_SESSION_ID`, `MEWBO_ERROR`). |
| `test_channels.py`, `test_email_channel.py` | Adapter protocol compliance, HMAC verification, MIME/threading parsing, mention gating — all offline. |
| `apps/mewbo_api/tests/test_vcs_pickup.py` | Stub by patching `vcs_pickup._service` attributes; one real-git test (bare origin + clone) exercises fetch→worktree→ff. |
| `test_github_workflows.py` | Workflow contract for `.github/workflows/agent-pickup.yml`, incl. injection safety: event data reaches `run:` only via `env:`, never inline `${{ github.event.* }}`. |

Also covered: tool registry disabling on init failure, MCP discovery (schema
normalization, per-server failures, CLI visibility when tools are missing),
plugin discovery/install/hook translation/session-init integration, the agent
definition registry, session runtime (fork-from-tag, `fork_at_ts`, archiving),
instruction discovery, orchestration flow, and CLI display + integration.

## Assumptions

- Many tests rely on `monkeypatch` for env vars (LLM config, MCP config, log levels).
- LLM calls are mocked at the orchestration boundary (`orchestrate_session`) or via `AsyncMock` on `model.ainvoke`.
- `ToolUseLoop` tests require `AgentContext.root()` with an `AgentHypervisor`.
- `ToolUseLoop.run()` is async — wrap calls with `asyncio.run()` in sync test methods.
- Agent lifecycle hooks (`on_agent_start`/`on_agent_stop`) fire from async context — use thread-safe mocks.
- Never pull in real MCP servers or external HTTP.

## Pitfalls

- **`caplog` captures NOTHING from these modules — they log through loguru.** `caplog` hooks stdlib `logging`; loguru is a separate sink chain, so a test asserting on a warning via `caplog` passes vacuously (empty records — and an `assert "x" not in caplog.text` check is worse, it can never fail). Add a temporary loguru sink appending to a list for the duration of the assertion, which is loguru's own documented test pattern. Applies to every module using `common.get_logger`.
- **Never run two pytest invocations concurrently** — suites share file-backed stores (session/project store under the same data root), so parallel runs cross-contaminate and produce one-off phantom failures that don't reproduce.
- **Before diagnosing an ordering failure, confirm the file that RAN is the file at HEAD.** This checkout hosts sibling git worktrees under `.worktrees/` and `.claude/worktrees/`, both gitignored, and `testpaths` excludes them — so a worktree is invisible to `git status` AND to collection, and an agent working in one that writes into the MAIN checkout's `tests/` leaves no trace either place. **Get the failing test NAMES and grep for them at HEAD first**: a name that exists nowhere at HEAD means the run collected another session's in-flight file, not that you have a regression. The failure COUNT misleads here — such failures partition along plausible-looking seams unrelated to the cause.
- **Full-suite ordering failures = global-state leak** (once the above is ruled out). A test that passes in isolation but fails under full-suite ordering has a leaked singleton (namespace, event loop, config). Route tests must NOT re-register a Flask-RESTX namespace on the shared backend app — `add_url_rule` after the app handled its first request raises (Flask setup frozen); rely on `backend.py`'s import-time wiring. Async unit tests use `asyncio.run` (fresh loop per call), never a shared `get_event_loop()` a prior test may have closed.
- **Assert loudly on the match, not just on the derived result.** A parser or classifier built on a regex degrades silently the moment the upstream string format changes: `if matches:` is a guard, not an assertion, so a test written the same way falls through the `else` branch and still passes against a now-unmatched input. Where a fixture is known to match, assert `matches` itself.
- **Anchor a fixture by a stable identifier, never by position.** A helper that reaches for "the last event of this type" or "the first matching row" is one new record away from reaching for the wrong one. Filter on a stable id/name carried IN the record; position is an accident of write order, not a contract.
- **A model that DERIVES its own id discards the one your fixture wrote — and a test bound to that literal passes vacuously.** `Entity.id` (`sha1(normalized_name|type)`) and `MemoryNode.node_id` (`compute_node_id`) are both recomputed at validation, so `Entity(id="e1", …)` stores a hash and an edge written against `"e1"` matches NOTHING. The dangerous half is the direction that still passes: an assertion of the form "this anchor is DROPPED" (`== ()`, `is None`, `not in`) is satisfied just as well by the edge never being collected at all, so it goes green while testing nothing. **Bind fixtures to the id the model actually minted (`ent.id`, `note.node_id`), and always pair an it-is-dropped assertion with an it-resolves case over the same seam** — the positive is what proves the negative is not vacuous.
- **An expectation must be spelled the way the STORE spells it, and the way to guarantee that is to call the store's own normaliser.** Timestamps are the recurring case: `EventCursor.canonical` deliberately always writes microseconds (a bare `isoformat()` drops a zero-microsecond field, and every `ts` comparison in the store is TEXTUAL, so the two spellings sort differently). Re-spelling the format in the test just moves the drift one file over — call the normaliser.
- **Reproduce, don't describe.** A claim built from reading a prompt, a truncated grep result, or an editor's rendered overlay is a hypothesis, not a finding. A reproduction case must construct the shape under test BY HAND where necessary: a fixture built by current code can never reach a path that only a legacy record takes, since current code no longer writes that shape.
- **Assert against the runtime the ROUTE holds, not `backend.runtime`.** `test_backend._reset_backend` rebinds `backend.runtime`/`backend.session_store` by **plain assignment** (no monkeypatch restore) → it leaks for the rest of the suite. A namespace captured its runtime at `init_*` time (e.g. `realtime.routes._runtime`), so the session a route persisted lives THERE, not in whatever `backend.runtime` now points at. Read the route module's own `_runtime` to verify a persisted side-effect. Corollary for write-behind routes: force the persist synchronous (patch `persist_async`→`persist`) so the daemon-thread write has landed before the assert.
- Over-mocking hides real behavior. Mock only the LLM call boundary and tool execution boundary.
- Exercise schema mismatches (string tool_input, dict tool_input, invalid schema, required fields). Missing-tool tests assert `last_error` and follow the production path.
- Keep tests deterministic: fixed timestamps, fixed session IDs, explicit env.

## Preferred patterns

- Fake tools implementing the same `ToolSpec` interface.
- `_make_agent_context()` (in `test_tool_use_loop.py`) for root contexts.
- `AsyncMock` for `model.ainvoke` — `return_value` or `side_effect` for multi-step conversations; `_text_response()` / `_tool_call_response()` to build mock `AIMessage` objects.
- Integration-style tests covering a full turn: user → tool calls → final text response.
- Parameterize micro-variants instead of duplicating tests (schema coercion, tool discovery).
- Lightweight stubs for CLI I/O (dummy input/output) to avoid terminal dependencies.

## Coverage scope & conventions

- CI's coverage gate (`.github/workflows/coverage.yml`) measures **only** `--source=mewbo_core,mewbo_tools,mewbo_ha_conversation`. The apps (`mewbo_api`/`mewbo_cli`/`mewbo_mcp`) and `mewbo_graph` are executed but **not** in the gated number — add them to `--source` to see the whole picture locally. The committed root `coverage.json` is a stale point-in-time snapshot, not live output; regenerate, don't trust it.
- Full local run: `.venv/bin/coverage run --source=mewbo_core,mewbo_tools,mewbo_graph,mewbo_api,mewbo_cli,mewbo_mcp,mewbo_ha_conversation -m pytest` then `coverage json -o coverage.json` and slice per-package from `files[].summary.missing_lines`. `testpaths` = `tests/` + `apps/{mewbo_api,mewbo_mcp,mewbo_cli}/tests` + `demo/seeder/tests`.
- Under pytest, `tests/` is on `sys.path`, so coverage tests reuse sibling helpers directly (e.g. `from test_tool_use_loop import _make_agent_context, _text_response, _tool_call_response`). Naming: broad coverage/integration suites use `*_integration.py` / `*_flow.py` / `*_extra.py`; graph wiki-plugin tool tests live in `tests/wiki/test_tool_*.py`, SCG engine tests in `tests/agentic_search/scg/`.
- New tests are integration/contract-first: drive the public caller site, assert real outputs/side-effects, stub only I/O boundaries. Reject namesake tests (a test that still passes if the production body were deleted is worthless).

## What a green suite cannot prove

- **In-process tests structurally cannot prove OPTIONALITY.** A test shares `sys.modules` with everything the suite already imported, so a module importable only because an earlier test enabled a subsystem still looks fine, and an "uninstalled" extra is still resident from someone else's import. Any claim about the IMPORT GRAPH or about what exists ON DISK — this package pulls in no driver, importing the stores does not import `pymongo`, auth-disabled mounts no routes — must run in a fresh interpreter. `test_iam_architecture.py:_run_probe` is the pattern: a subprocess with its own cwd and env reporting JSON, never a mock of `sys.modules`.
- **A green run is evidence about the TESTS, not the PACKAGES.** The suite covers what the suite imports, unevenly, so a package can be entirely unimportable while every test passes — a module naming a moved import raises `ModuleNotFoundError` and reaches the main branch green if no test imports it. The structural import-graph test does not close this: an edge to a nonexistent module is dropped before the cycle search, and a missing edge can only make a graph MORE acyclic. **A reference to nothing and a reference in a circle are different failures; only one shows up in the shape of the graph.**
- **A test must never touch the real `~/.mewbo`.** A bare `import mewbo_api` in a fresh interpreter lays down `cache/` and `sessions/` under whatever home resolves, so a subprocess probe pins `MEWBO_HOME` for the same reason an in-process test does. Stores derive their default path from the config chain, so any store built without an explicit `tmp_path` writes into the live config directory and persists forever. The redirect belongs in an autouse fixture in `conftest.py` beside `app_config_file` — a per-test convention leaks the first time an author forgets, and it leaks silently.
- **An injected-factory suite proves nothing about the DEFAULT factory.** Injecting a fake at every call site means the default never executes: LDAP's `_default_connection_factory` passed a float timeout to `ldap3`, which sets `SO_RCVTIMEO` via `struct.pack("LL", …)` and rejects a float, killing every real connection with a `struct.error` that is not an `LDAPException` and so escapes the driver's error normalization too. **"Stub only I/O boundaries" degrades into "stub the thing under test" the moment the injected collaborator IS the boundary.** Wherever a default factory or client exists, one test must EXECUTE the real one against something controlled — a local listener, a temp file, a captured request.
- **A green suite is not evidence for a SECURITY property.** Tests written against the implementation assert what it DID, so a secret returned to any `config.read` holder, a guard defaulting fail-open, auth re-implemented against a hardcoded token, a shim accepting expired keys and lexicographically ordered ISO timestamps all passed. Assert the PROPERTY: the value is absent from the response, an unknown permission denies, the expired key is refused. **A test that would still pass with the check deleted proves nothing about the check.**
- **High statement coverage on a resource pool proves nothing about its failure-and-release path.** The happy path (accept, dispatch, settle) is easy to cover; the arm where a launcher RAISES and the scheduler must hand the slot it already holds to the next waiter (`hypervisor.py:AgentQueue._dispatch`/`_pump`/`release`) is not. Miss it and a failed dispatch permanently shrinks the pool by one for the life of the process, suite green. Cover the exception arm and assert the pool's own counters (free slots, pending count) afterward — not merely that the call returned.
- **If a test needs to patch the thing whose behavior it is asserting, it is testing the patch.** Reaching a state by hand-draining the underlying primitive, then asserting a disjunction any error string satisfies, exercises no production code and cannot fail. Drive the real call into the real state: an over-cap spawn returns `submitted` with a live `agent_id`, appears in the liveness index while waiting, and starts when a sibling's release hands it the slot.
- **Two green suites can both be wrong when they mock each other.** Where a producer and consumer share a wire contract and each suite mocks the OTHER side, neither can detect a field-name mismatch — the feature goes inert while both stay green. Assert the actually-serialized payload round-trips through the aliases the consumer really reads, never a hand-typed fixture.
- **A green suite says nothing about COST.** Every fixture here is small, so a function scaling with stored data is indistinguishable from one that does not; the answer is never wrong, only slower forever. Pin the cost characteristic separately.

## Testing cost characteristics

Root `CLAUDE.md` → "Performance is a contract at every boundary" defines the cost
classes. A boundary that declares one needs a test that pins it, or the declaration
decays into a comment.

- **Assert READ COUNTS, never wall-clock.** A timing assertion is flaky on a loaded
  machine and, worse, passes for the wrong reason on a fast one. Seed N records with M
  children each, call the thing, and assert the number of store reads (or documents
  examined) does not grow with M.
- **Scale the dimension you claim is bounded, not the one that is obviously bounded.**
  A listing test with ten sessions of three events each proves nothing about either
  axis. Vary M with N fixed to pin `O(collection)`, and vary N to pin the bound on rows.
- **A cursor test must assert the WORK, not the payload.** An incremental fetch that
  returns nothing can still read an entire record. Asserting the response is small is
  exactly the assertion that lets that ship.
- **Fail-closed filters need a test for the unparseable case**, asserting a refusal. The
  tempting assertion — "a bad filter returns no rows" — is satisfied by returning ALL
  rows in most fixtures, because a small fixture's full set looks like a plausible
  result.

## Reading a failure honestly

This repo has known order-dependent failures under full-suite load, and this machine
often runs several suites at once. A red result is a hypothesis, not a verdict.

**Three data points settle it, and fewer do not:** the same test (1) in isolation on
your branch, (2) in isolation on the base branch, (3) under the full suite.
Isolated-green plus full-suite-red is order dependence, not your regression. Red on the
base branch too is pre-existing. Only isolated-red on your branch with isolated-green on
base is a regression you introduced.

A failure count that CHANGES between two runs of the same tree is itself the signal — the
suite is load-dependent right now and no single run of it is evidence either way. Re-run
in isolation before concluding anything, and never report a full-suite green from a
loaded box as verification.
