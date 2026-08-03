"""Wiki backend — opt-in via ``mewbo-api[wiki]`` extras.

``init_wiki(app, runtime)`` mounts the /v1/wiki/* routes when the extras
are installed and reachable. It returns False silently otherwise so the
API server starts cleanly without the wiki feature.
"""
from __future__ import annotations

from mewbo_core.common import get_logger

logging = get_logger(name="api.wiki")

# Restart-recovery façade. Imported behind a guard so a graph-less
# ``mewbo-api`` install (no ``wiki`` extra) still imports this package cleanly —
# ``recovery`` pulls ``mewbo_graph`` transitively. ``None`` means the feature is
# absent; ``_run_recovery`` then no-ops. Bound at module level so it's a
# patchable attribute (tests monkeypatch ``recover_interrupted``).
try:
    from .recovery import JobRecovery
except ImportError:  # pragma: no cover — graph-less install
    JobRecovery = None  # type: ignore[assignment,misc]


def init_wiki(app, runtime, hook_manager=None) -> bool:
    """Mount /v1/wiki/* on the Flask app. Returns False if wiki extras are absent.

    ``hook_manager`` (the API's shared :class:`HookManager`) is threaded through
    so the QA session-end finalizer can register on ``on_session_end`` AND the
    same instance is handed to ``WikiQaSession.start`` — the wiki-qa hypervisor
    has no terminal tool of its own, so its ``complete`` event + snapshot
    reconciliation ride this hook. It reaches startup recovery for the same
    reason it reaches the interactive start paths: an ``Orchestrator`` given no
    manager builds a fresh empty one, so a boot-time re-drive would otherwise
    run without the session-end reconciler that hands a re-stranded job back.
    ``None`` runs the re-drive with no hooks at all.
    """
    try:
        from mewbo_graph.wiki.store import create_wiki_store, set_wiki_store
    except ImportError as exc:
        logging.info("wiki extras not installed ({}); skipping /v1/wiki/* routes", exc)
        return False
    try:
        store = create_wiki_store()
    except Exception as exc:
        logging.warning("wiki store init failed: {}; skipping routes", exc)
        return False
    # Pin the process-wide singleton so the relocated wiki SessionTools resolve
    # the SAME store instance (down-only) instead of reaching up into the API
    # runtime; keep ``runtime.wiki_store`` for the API routes that read it.
    set_wiki_store(store)
    runtime.wiki_store = store
    from .routes import register

    register(app, runtime, hook_manager=hook_manager)
    # Product-wide git credential registry — lives alongside the wiki routes
    # (same store), but mounted at /v1/git/* since wiki is only its first
    # consumer (task/vcs-pickup flows are expected next).
    from .git_credentials_routes import register as register_git_credentials

    register_git_credentials(app, runtime)
    # Restart durability: re-drive jobs that were running when the previous
    # process died. Their sessions are gone, but credentials are persisted
    # per-slug, so the existing refresh path rebuilds them from clone.
    _run_recovery(runtime.wiki_store, runtime, hook_manager=hook_manager)
    # Restart durability, QA side: settle answers a process death stranded at
    # status="running" before QaSessionEndHook ever got to run for them (a
    # crash never reaches a session-end hook at all).
    _sweep_qa_answers(runtime)
    # Stage 2 of a scoped refresh: the down-only launcher the sessionless
    # ``ScopedRefreshRunner`` drives to rewrite the pages its delta pass flagged.
    _register_act_launcher(runtime, hook_manager=hook_manager)
    logging.info("wiki routes mounted at /v1/wiki/* and /v1/git/credentials*")
    return True


def _register_act_launcher(runtime, hook_manager=None) -> None:
    """Wire the scoped refresh's act-phase launcher for the ``wiki`` plugin.

    ``ScopedRefreshRunner`` lives in ``mewbo_graph`` and cannot start a Mewbo
    session without importing up, so — exactly like
    ``_register_search_launcher`` — the api injects a concrete launcher bound to
    this process's session runtime. Two independent guards, as that function
    has: reaching here at all means the ``wiki`` extra resolved (``init_wiki``
    returns early otherwise), and the import is lazy + ``ImportError``-swallowing
    so a partial install never registers. With nothing registered the act phase
    is simply absent and a scoped refresh finalizes as it does today.
    """
    try:
        from mewbo_graph.plugins.wiki.act_launcher import ActLauncher

        from .act_launcher_impl import SessionActLauncher
    except ImportError:  # pragma: no cover — graph-less / partial install
        return

    ActLauncher.register(SessionActLauncher(runtime=runtime, hook_manager=hook_manager))


def _run_recovery(store, runtime, hook_manager=None) -> None:
    """Re-drive interrupted indexing jobs via the refresh path on startup."""
    if JobRecovery is None:  # pragma: no cover — graph-less install
        return
    try:
        JobRecovery.recover_interrupted(store, runtime, hook_manager=hook_manager)
    except Exception as exc:  # pragma: no cover — recovery is best-effort
        logging.warning("wiki: startup recovery failed ({}); skipping", exc)


def _sweep_qa_answers(runtime) -> None:
    """Settle QA answers a process death stranded at ``status: "running"``."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from .qa_sweep import QaAnswerSweeper  # noqa: PLC0415

    try:
        settled = QaAnswerSweeper(runtime, now=lambda: datetime.now(timezone.utc)).sweep()
        if settled:
            logging.warning("wiki: settled {} orphaned QA answer(s) at startup", settled)
    except Exception as exc:  # pragma: no cover — the sweep is best-effort
        logging.warning("wiki: startup QA sweep failed ({}); skipping", exc)
