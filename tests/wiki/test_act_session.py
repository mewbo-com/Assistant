"""``_start_refresh_act_session`` — stage 2 of a scoped refresh, api side."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from mewbo_api.wiki import jobs
from mewbo_core.config import is_untrusted_cwd, unregister_untrusted_cwd
from mewbo_graph import plugins_root
from mewbo_graph.wiki.memory_types import DocPageNote
from mewbo_graph.wiki.types import IndexingJob, RefreshDecision, WizardSubmission


class _FakeStore:
    """The three store surfaces the act start path touches."""

    def __init__(self, notes: dict[str, DocPageNote] | None = None):
        self.notes = notes or {}
        self.attached: list[tuple[str, str]] = []

    def attach_job_session(self, job_id: str, session_id: str) -> None:
        self.attached.append((job_id, session_id))

    def get_doc_note(self, slug: str, page_id: str) -> DocPageNote | None:
        return self.notes.get(page_id)

    def get_job_submission(self, job_id: str):
        return None


class _FakeRuntime:
    def __init__(self):
        self.tag: str | None = None
        self.context: dict = {}
        self.started: dict = {}

    def resolve_session(self, *, session_tag: str) -> str:
        self.tag = session_tag
        return "sess-act"

    def append_context_event(self, session_id: str, payload: dict) -> None:
        self.context = payload

    def start_async(self, **kwargs) -> str:
        self.started = kwargs
        return "sess-act:r1"


def _note(page_id: str) -> DocPageNote:
    return DocPageNote(
        slug="h/o/r",
        page_id=page_id,
        title=f"Title {page_id}",
        content_hash="abc",
        page_type="module",
        stale_anchor_keys=["src/a.py", "src/b.py#Thing"],
        deleted_anchor_keys=["src/gone.py"],
    )


def _start(store, runtime, **kw):
    return jobs._start_refresh_act_session(
        store=store,
        runtime=runtime,
        job_id="job-1",
        slug="h/o/r",
        page_ids=kw.pop("page_ids", ["p1"]),
        **kw,
    )


# ── the tool ceiling ────────────────────────────────────────────────────────


def test_act_tools_match_the_agent_def_frontmatter():
    """The allowlist IS the ceiling for a session root, so it must not drift."""
    text = Path(plugins_root() / "wiki" / "agents" / "wiki-refresh-act.md").read_text()
    match = re.search(r"^tools:\s*\[(.+)\]\s*$", text, re.MULTILINE)
    assert match, "wiki-refresh-act.md must declare a frontmatter tools: list"
    declared = [t.strip() for t in match.group(1).split(",")]
    assert jobs.ACT_TOOLS == declared


def test_act_ceiling_excludes_finalize_and_core_read_file():
    # wiki_finalize prunes to plan membership — on a narrowed plan it would
    # delete every untouched page. read_file would need a repository cwd.
    assert "wiki_finalize" not in jobs.ACT_TOOLS
    assert "read_file" not in jobs.ACT_TOOLS
    assert "wiki_read_file" in jobs.ACT_TOOLS


# ── the session start ───────────────────────────────────────────────────────


def test_start_binds_the_session_to_the_job_under_its_own_tag():
    store, runtime = _FakeStore(), _FakeRuntime()
    assert _start(store, runtime) == "sess-act"
    assert runtime.tag == "wiki:act:job-1"
    # The binding find_job_by_session resolves — every tool on the ceiling
    # reaches its project through it.
    assert store.attached == [("job-1", "sess-act")]


def test_start_scopes_the_run_and_gives_it_no_cwd():
    store, runtime = _FakeStore(), _FakeRuntime()
    _start(store, runtime)

    assert runtime.started["allowed_tools"] == jobs.ACT_TOOLS
    assert runtime.started["strict_tool_scope"] is True
    assert runtime.started["enable_skills"] is False
    # No repository cwd: the wiki source tools resolve the checkout server-side,
    # so the untrusted <cwd>/.mcp.json tier is never consulted here.
    assert "cwd" not in runtime.started
    assert "cwd" not in runtime.context

    # The durable half — a re-engage can only re-apply what the start wrote.
    assert runtime.context["client_capabilities"] == ["wiki"]
    assert runtime.context["mcp_tools"] == jobs.ACT_TOOLS
    assert runtime.context["strict_tool_scope"] is True


def test_start_carries_the_act_playbook_and_operator_guidance():
    store, runtime = _FakeStore(), _FakeRuntime()
    submission = WizardSubmission(
        repoUrl="https://git.example.com/o/r",
        slug="h/o/r",
        platform="gitea",
        token=None,
        depth="comprehensive",
        language="en",
        model="m1",
        filterMode="exclude",
        dirs=[],
        files=[],
        customInstructions="Write for platform engineers.",
    )
    _start(store, runtime, submission=submission)

    instructions = runtime.started["skill_instructions"]
    assert "stage 2 of a scoped refresh" in instructions  # the act playbook body
    assert "Write for platform engineers." in instructions
    assert runtime.started["model_name"] == "m1"


def test_start_degrades_to_the_playbook_alone_without_a_submission():
    store, runtime = _FakeStore(), _FakeRuntime()
    _start(store, runtime)
    assert runtime.started["skill_instructions"] == jobs._load_act_playbook()
    assert runtime.started["model_name"] is None


def test_start_reports_empty_when_the_run_registry_refuses():
    """A refused ``start_async`` (a run already live on this tag) must not
    read as success — the caller would otherwise wait() on whatever OTHER
    run holds the session and read its outcome as stage 2's."""

    class _RefusingRuntime(_FakeRuntime):
        def start_async(self, **kwargs) -> str:
            self.started = kwargs
            return ""

    store, runtime = _FakeStore(), _RefusingRuntime()
    assert _start(store, runtime) == ""
    # The session was still created and attached — only the START is refused.
    assert store.attached == [("job-1", "sess-act")]


# ── the work-list ───────────────────────────────────────────────────────────


def test_work_list_carries_each_page_and_its_flagged_anchors():
    store = _FakeStore({"p1": _note("p1")})
    runtime = _FakeRuntime()
    _start(store, runtime, page_ids=["p1", "p2"])

    query = runtime.started["user_query"]
    assert "pages: 2" in query
    assert "- pageId: p1" in query
    assert "- pageId: p2" in query
    assert "staleAnchors: src/a.py, src/b.py#Thing" in query
    assert "deletedAnchors: src/gone.py" in query


def test_work_list_degrades_to_bare_ids_when_a_note_read_fails():
    class _Broken(_FakeStore):
        def get_doc_note(self, slug, page_id):
            raise RuntimeError("mongo hiccup")

    store, runtime = _Broken(), _FakeRuntime()
    _start(store, runtime, page_ids=["p1"])
    assert "- pageId: p1" in runtime.started["user_query"]


# ── the indexer's clone root is an untrusted directory ──────────────────────


def test_the_indexer_registers_its_clone_root_as_untrusted(tmp_path, monkeypatch):
    """An indexed repository's own .mcp.json must never contribute MCP servers.

    The full indexer DOES take the clone as its cwd (unlike the act session,
    which takes none), so the registration is what keeps a repository nobody
    audited from naming a process for config resolution to spawn.
    """
    clone_root = tmp_path / "clones"
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(clone_root))
    runtime = _FakeRuntime()
    try:
        jobs._start_indexer_session(
            store=_FakeStore(),
            runtime=runtime,
            job_id="job-1",
            model="m1",
            user_query="index it",
        )
        # The ROOT is registered, so every job under it is covered — including
        # the one this call just made and any future one.
        assert is_untrusted_cwd(clone_root) is True
        assert is_untrusted_cwd(clone_root / "job-1") is True
        assert is_untrusted_cwd(clone_root / "job-2" / "deep" / "dir") is True
        assert is_untrusted_cwd(tmp_path / "elsewhere") is False
        # The indexer still runs IN the clone — the registration is what makes
        # that safe, not a change of directory.
        assert runtime.started["cwd"] == str(clone_root / "job-1")
    finally:
        # The registry is process-wide; leaving a tmp_path root in it would
        # leak into every later test in this interpreter.
        unregister_untrusted_cwd(clone_root)


# ── the session-end hook must not settle a scoped job ───────────────────────


def _job(status: str, *, path: str | None) -> IndexingJob:
    decision = (
        None
        if path is None
        else RefreshDecision(
            path=path, reason=None if path == "scoped" else "requested"
        )
    )
    return IndexingJob(
        job_id="job-1",
        slug="h/o/r",
        status=status,
        scanned_count=0,
        total_count=0,
        current_file=None,
        refresh_decision=decision,
    )


class _HookStore:
    def __init__(self, job: IndexingJob):
        self.job = job
        self.updates: list[dict] = []
        self.events: list[dict] = []

    def find_job_by_session(self, session_id):
        return self.job.job_id

    def get_job(self, job_id):
        return self.job

    def update_job(self, job_id, **fields):
        self.updates.append(fields)

    def append_job_event(self, job_id, event):
        self.events.append(event)


class _HookRuntime:
    def __init__(self, store):
        self.wiki_store = store


@pytest.mark.parametrize("status", ["scanning", "finalizing"])
def test_session_end_hook_leaves_a_scoped_refresh_job_to_its_runner(status):
    # The act session ends BEFORE the runner finalizes. Marking the job
    # interrupted here would race that finalize, and the outcome assertion
    # would flip the act session to unmet_goal — which the runner reads back
    # as "stage 2 failed" on a run that succeeded.
    store = _HookStore(_job(status, path="scoped"))
    assertion = jobs.WikiIndexingSessionEndHook(_HookRuntime(store))("sess-act", None)
    assert assertion is None
    assert store.updates == []
    assert store.events == []


def test_session_end_hook_still_settles_a_full_index_job():
    store = _HookStore(_job("scanning", path="full"))
    assertion = jobs.WikiIndexingSessionEndHook(_HookRuntime(store))("sess-1", None)
    assert assertion is not None
    assert store.updates == [{"status": "interrupted"}]
