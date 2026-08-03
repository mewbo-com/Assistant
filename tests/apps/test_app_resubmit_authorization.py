"""Who may submit WHICH app — ``AppLifecycle.submit``'s two authorization edges.

The defect these pin: a console composer session opened against an existing app
is minted as a deliberately non-maintainer session, so its resubmit was refused
with a message naming "submit a new app_id" as the remedy — and a second app
appeared. Two rules close it, and they are opposite halves of the same fact
about a session's binding:

* **UPDATE** — a session the SERVER bound to this app (its id fields OR its
  stamped ``app:<id>[:<session>]`` tag) takes the version-bump branch.
* **INSERT** — a session bound to app A may not mint app B.

The trap that rides with the first rule has its own test below: the branch must
keep arming/cancelling on the app's OWN maintainer session, never on whoever
submitted, or a composer resubmit leaks the maintainer's armed triggers.

Real JSON app + trigger stores and a real ``TriggerPolicy``; the session backend
is the one faked I/O boundary, with a tag map keyed exactly as the real one is
(one tag → ONE session). NOW is injected.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import (
    AppFrontend,
    AppSpec,
    CronSchedule,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.store import JsonAppStore
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


class FakeSessions:
    """Deterministic session ids + the real tag keying (one tag → one session)."""

    def __init__(self) -> None:
        self._n = 0
        self.tags: dict[str, str] = {}
        self.terminated: set[str] = set()

    def create_session(self) -> str:
        self._n += 1
        return f"session-{self._n}"

    def tag_session(self, session_id: str, tag: str) -> None:
        self.tags[tag] = session_id

    def tags_for_session(self, session_id: str) -> list[str]:
        return [t for t, sid in self.tags.items() if sid == session_id]

    def append_context_event(self, session_id: str, context: dict) -> None:
        pass

    def append_event(self, session_id: str, event: dict) -> None:
        pass

    def is_terminated(self, session_id: str) -> bool:
        return session_id in self.terminated


def _make(tmp_path):
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    trigger_store = JsonTriggerStore(data_file=tmp_path / "triggers.json")
    sessions = FakeSessions()
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=trigger_store,
        trigger_policy=TriggerPolicy(),
        sessions=sessions,
        now_fn=lambda: NOW,
    )
    return lifecycle, app_store, trigger_store, sessions


def _draft(
    app_id: str, *, builder_sid: str, title: str = "Inbox digest", pipelines=None
) -> AppSpec:
    return AppSpec(
        app_id=app_id,
        title=title,
        summary="Groups email into tasks.",
        owner_session_id=builder_sid,
        workspace_ref=WorkspaceRef(kind="own", key="default"),
        frontend=AppFrontend(entrypoint="app.py", files={"app.py": "import streamlit as st\n"}),
        pipelines=pipelines or [],
        status="building",
        created_at=NOW,
        updated_at=NOW,
    )


def _live(lifecycle, app_id: str = "app-x", *, pipelines=None) -> AppSpec:
    """Submit a v1 live app from a plain builder session (no tag of its own).

    The builder id is derived from *app_id* so a test seeding TWO apps does not
    reuse one builder session for both — which would itself trip the new
    bound-session insert guard and mask what the test is actually asserting.
    """
    builder = f"builder-{app_id}"
    return lifecycle.submit(
        _draft(app_id, builder_sid=builder, pipelines=pipelines),
        builder_session_id=builder,
    )


class TestComposerSessionCanUpdateTheAppItWasOpenedAgainst:
    """The product promise: open a session against an app and change THAT app."""

    def test_a_tag_bound_session_bumps_the_version_instead_of_being_refused(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = _live(lifecycle)
        composer, created = lifecycle.get_or_create_maintainer_session("app-x", fresh=True)
        assert created is True and composer != live.maintainer_session_id

        updated = lifecycle.submit(
            _draft("app-x", builder_sid=composer, title="Inbox digest (edited)"),
            builder_session_id=composer,
        )

        assert updated.version == 2  # a version bump, NOT a second app
        assert updated.title == "Inbox digest (edited)"
        assert updated.status == "live"
        # Identity is still the original row's: one app, not two.
        assert updated.app_id == "app-x"
        assert updated.owner_session_id == live.owner_session_id
        assert updated.created_at == live.created_at
        assert [a.app_id for a in app_store.list_apps(include_archived=True)] == ["app-x"]
        assert [v.version for v in app_store.list_versions("app-x")] == [1, 2]

    def test_the_maintainer_field_is_NOT_repointed_at_the_composer_session(self, tmp_path):
        """Two claimants would make the resolvers' scan order load-bearing.

        ``maintainer_session_id`` is what the repair wake dereferences; widening
        who may SUBMIT must not change who the app points at.
        """
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = _live(lifecycle)
        composer, _ = lifecycle.get_or_create_maintainer_session("app-x", fresh=True)

        lifecycle.submit(
            _draft("app-x", builder_sid=composer), builder_session_id=composer
        )

        assert app_store.get("app-x").maintainer_session_id == live.maintainer_session_id

    def test_the_apps_own_maintainer_still_takes_the_same_branch(self, tmp_path):
        """The app-repair path is unchanged by the widening."""
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = _live(lifecycle)
        maintainer = live.maintainer_session_id

        repaired = lifecycle.submit(
            _draft("app-x", builder_sid=maintainer), builder_session_id=maintainer
        )

        assert repaired.version == 2
        assert repaired.maintainer_session_id == maintainer
        assert app_store.list_versions("app-x")[-1].author == "repair"


class TestTriggersStayOnTheMaintainer:
    """THE TRAP: the branch arms and cancels on the APP's session, not the caller.

    A tag-bound composer session owns no triggers. Cancelling "the submitter's"
    triggers would cancel an empty set while the real maintainer's armed trigger
    survives, keeps firing its stale ``wake_prompt``, and — its ``trigger_id`` no
    longer matching any pipeline — runs with broad session grants. Re-arming on
    the submitter would be the mirror defect: the repair wake dereferences
    ``maintainer_session_id`` and would find nothing armed there.
    """

    def _cron(self, wake: str) -> PipelineSpec:
        return PipelineSpec(
            name="ingest", wake_prompt=wake, schedule=CronSchedule(cron="0 9 * * *")
        )

    def test_a_composer_resubmit_cancels_the_MAINTAINERS_trigger_and_rearms_there(
        self, tmp_path
    ):
        lifecycle, app_store, trigger_store, _ = _make(tmp_path)
        live = _live(lifecycle, pipelines=[self._cron("v1 wake")])
        maintainer = live.maintainer_session_id
        v1_trigger_id = live.pipelines[0].trigger_ref
        assert len(trigger_store.list(session_id=maintainer, status="armed")) == 1
        composer, _ = lifecycle.get_or_create_maintainer_session("app-x", fresh=True)

        updated = lifecycle.submit(
            _draft("app-x", builder_sid=composer, pipelines=[self._cron("v2 wake")]),
            builder_session_id=composer,
        )

        # The prior trigger is terminal — no orphan firing a stale wake_prompt.
        assert trigger_store.get(v1_trigger_id).status == "cancelled"
        # Exactly one armed trigger, and it lives on the MAINTAINER session, which
        # is the session `maintainer_session_id` still points at.
        armed = trigger_store.list(session_id=maintainer, status="armed")
        assert len(armed) == 1
        assert armed[0].id != v1_trigger_id
        assert armed[0].wake_prompt == "v2 wake"
        assert updated.pipelines[0].trigger_ref == armed[0].id
        # ...and NOTHING was armed on the resubmitting session.
        assert trigger_store.list(session_id=composer, status="armed") == []
        assert app_store.get("app-x").maintainer_session_id == maintainer

    def test_repeated_composer_resubmits_do_not_accrete_triggers(self, tmp_path):
        """The leak is cumulative, so one resubmit is not enough to pin it."""
        lifecycle, _, trigger_store, _ = _make(tmp_path)
        live = _live(lifecycle, pipelines=[self._cron("v1 wake")])
        maintainer = live.maintainer_session_id

        for n in range(2, 5):
            composer, _ = lifecycle.get_or_create_maintainer_session("app-x", fresh=True)
            spec = lifecycle.submit(
                _draft("app-x", builder_sid=composer, pipelines=[self._cron(f"v{n} wake")]),
                builder_session_id=composer,
            )
            assert spec.version == n

        armed = trigger_store.list(session_id=maintainer, status="armed")
        assert len(armed) == 1
        assert armed[0].wake_prompt == "v4 wake"


class TestABoundSessionCannotMintADifferentApp:
    """The INSERT half — the path the operator's duplicate app actually took."""

    def test_a_session_bound_to_app_a_cannot_insert_app_b(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        _live(lifecycle, "app-a")
        composer, _ = lifecycle.get_or_create_maintainer_session("app-a", fresh=True)

        with pytest.raises(ValueError, match="bound to app 'app-a'"):
            lifecycle.submit(
                _draft("app-b", builder_sid=composer), builder_session_id=composer
            )

        # No second app was created — the whole point of the guard.
        assert app_store.get("app-b") is None
        assert [a.app_id for a in app_store.list_apps(include_archived=True)] == ["app-a"]

    def test_the_maintainer_of_app_a_cannot_insert_app_b_either(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = _live(lifecycle, "app-a")

        with pytest.raises(ValueError, match="bound to app 'app-a'"):
            lifecycle.submit(
                _draft("app-b", builder_sid=live.maintainer_session_id),
                builder_session_id=live.maintainer_session_id,
            )
        assert app_store.get("app-b") is None

    def test_the_refusal_does_not_advise_creating_another_app(self, tmp_path):
        """The refusal STRING is the defect's proximate cause — pin it.

        The old message ended "(submit a new app_id instead)"; the model complied
        and a second app appeared. A refusal must name the correct next move, and
        forking is not it.
        """
        lifecycle, _, _, _ = _make(tmp_path)
        _live(lifecycle, "app-a")
        composer, _ = lifecycle.get_or_create_maintainer_session("app-a", fresh=True)

        with pytest.raises(ValueError) as excinfo:
            lifecycle.submit(
                _draft("app-b", builder_sid=composer), builder_session_id=composer
            )
        message = str(excinfo.value)

        assert "new app_id instead" not in message
        # It names the id the caller should have used.
        assert "app-a" in message

    def test_an_unbound_session_can_still_create_a_genuinely_new_app(self, tmp_path):
        """The chat builder's first app must keep working — no tag, no binding."""
        lifecycle, app_store, _, _ = _make(tmp_path)

        created = lifecycle.submit(
            _draft("app-new", builder_sid="chat-builder"), builder_session_id="chat-builder"
        )

        assert created.status == "live"
        assert created.version == 1
        assert app_store.get("app-new") is not None


class TestTheProductionAdapterReadsTags:
    """The wiring half — a fake backend proves nothing about the deployed one.

    Every other test here injects a fake session backend, so the widened
    membership test would pass identically if the REAL adapter resolved no tags
    at all — and the operator's composer session would still be refused. This
    drives ``RuntimeSessionBackend`` over a real ``SessionStore``.
    """

    def test_the_runtime_backend_returns_the_tags_it_stamped(self, tmp_path):
        from mewbo_api.apps.lifecycle import RuntimeSessionBackend
        from mewbo_core.session.session_store import SessionStore

        store = SessionStore(root_dir=str(tmp_path / "sessions"))

        class _Runtime:
            """The two members the adapter reaches for on a real ``SessionRuntime``."""

            session_store = store

            @staticmethod
            def tag_session(session_id: str, tag: str) -> None:
                store.tag_session(session_id, tag)

        backend = RuntimeSessionBackend(_Runtime())
        backend.tag_session("session-abc", "app:app-x:session-abc")

        assert backend.tags_for_session("session-abc") == ["app:app-x:session-abc"]
        assert backend.tags_for_session("session-untagged") == []


class TestTheLiveOverwriteGuardStillHolds:
    """Only the membership test widened; the guard itself is intact."""

    def test_a_session_with_no_binding_to_this_app_cannot_overwrite_it(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        _live(lifecycle)

        with pytest.raises(ValueError, match="refusing to overwrite a live app"):
            lifecycle.submit(
                _draft("app-x", builder_sid="unrelated-session"),
                builder_session_id="unrelated-session",
            )

        assert app_store.get("app-x").version == 1  # untouched
        assert app_store.get("app-x").status == "live"

    def test_the_live_overwrite_refusal_does_not_advise_forking(self, tmp_path):
        lifecycle, _, _, _ = _make(tmp_path)
        _live(lifecycle)

        with pytest.raises(ValueError) as excinfo:
            lifecycle.submit(
                _draft("app-x", builder_sid="unrelated-session"),
                builder_session_id="unrelated-session",
            )

        assert "new app_id instead" not in str(excinfo.value)

    def test_a_tag_for_a_DIFFERENT_app_does_not_authorize_this_one(self, tmp_path):
        """Binding is per-app: holding app-a's tag buys nothing against app-b.

        The refusal here is the INSERT guard rather than the live-overwrite one
        (app-b never existed), which is the point — either way no write lands.
        """
        lifecycle, app_store, _, _ = _make(tmp_path)
        _live(lifecycle, "app-a")
        _live(lifecycle, "app-b")
        composer_a, _ = lifecycle.get_or_create_maintainer_session("app-a", fresh=True)

        with pytest.raises(ValueError):
            lifecycle.submit(
                _draft("app-b", builder_sid=composer_a), builder_session_id=composer_a
            )

        assert app_store.get("app-b").version == 1

    def test_an_app_id_CONTEXT_key_authorizes_no_submit(self, tmp_path):
        """Only the server-stamped TAG binds — never the re-writable context key.

        ``backend.py``'s ``_build_context_payload`` merges a request's ``context``
        verbatim, so any caller can put ``app_id`` on a session it owns. This
        asserts the property directly: a session carrying the key and no tag is
        refused, while the same session holding the SERVER-stamped tag is
        accepted — so the test would still fail if the tag check were swapped for
        a context read.
        """
        lifecycle, app_store, _, sessions = _make(tmp_path)
        _live(lifecycle)
        # A session the caller controls: it has an app_id in its context (which
        # this lifecycle never reads) and no tag.
        forged = sessions.create_session()

        with pytest.raises(ValueError, match="refusing to overwrite a live app"):
            lifecycle.submit(
                _draft("app-x", builder_sid=forged), builder_session_id=forged
            )

        # The positive control: the SAME session, once the server stamps the tag,
        # is authorized — proving the refusal above is about authorization.
        sessions.tag_session(forged, f"app:app-x:{forged}")
        assert lifecycle.submit(
            _draft("app-x", builder_sid=forged), builder_session_id=forged
        ).version == 2
        assert app_store.get("app-x").version == 2

    def test_a_backend_without_a_tag_reader_degrades_to_the_id_fields(self, tmp_path):
        """Fail-CLOSED: no tag tier ⇒ exactly the pre-tag membership test.

        Several session backends in this suite predate ``tags_for_session``; the
        tier only ever WIDENS, so its absence must refuse, never crash.
        """

        class _NoTags(FakeSessions):
            tags_for_session = None  # type: ignore[assignment]

        app_store = JsonAppStore(root_dir=tmp_path / "apps")
        lifecycle = AppLifecycle(
            app_store=app_store,
            trigger_store=JsonTriggerStore(data_file=tmp_path / "triggers.json"),
            trigger_policy=TriggerPolicy(),
            sessions=_NoTags(),
            now_fn=lambda: NOW,
        )
        live = _live(lifecycle)

        with pytest.raises(ValueError, match="refusing to overwrite a live app"):
            lifecycle.submit(
                _draft("app-x", builder_sid="stranger"), builder_session_id="stranger"
            )
        # ...while the maintainer, bound by an ID FIELD, still updates.
        assert lifecycle.submit(
            _draft("app-x", builder_sid=live.maintainer_session_id),
            builder_session_id=live.maintainer_session_id,
        ).version == 2
