#!/usr/bin/env python3
"""The ONE home of an app's on-disk staging area.

An app's source lives in its manifest, not on disk: ``submit_app`` reads a bundle
off disk at submit time and the store keeps it from then on. A staging directory
(``${MEWBO_APPS_ROOT:-/tmp/mewbo/apps}/${SESSION_ID}/<app_id>/``) is therefore
EPHEMERAL — it exists only while some caller has re-materialized it, and it does
not survive a restart.

Two callers need exactly that materialization and they must not each own a copy
of it: the ``get_app`` tool's ``stage`` operation (the agent-facing recovery path)
and the Web IDE mount resolver, which has to put a maintainer session's app on
disk before a container can bind it. The file-writing rule — where the directory
is, what is confined to what, which files are written — lives here once.

Cost: ``O(one app)`` — the bundle is capped at ``submit_app``'s ``_MAX_FILES`` /
``_MAX_TOTAL_BYTES``, so a materialization is bounded by one manifest and never
by how many apps or sessions are stored.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from mewbo_core.session.session_provenance import SessionTag
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_api.apps.models import AppSpec
    from mewbo_api.apps.store import AppStoreBase

_DEFAULT_APPS_ROOT = "/tmp/mewbo/apps"

# The ``product`` facet ``SessionTag`` gives every Mewbo Apps tag. Read through
# ``parse`` rather than prefix-matched on ``app:`` so this package never
# re-spells a grammar core owns — and so the two-segment canonical tag and the
# per-session-unique variant are recognised by ONE rule.
_APPS_PRODUCT = "apps"


class AppStagingError(Exception):
    """A staging attempt that is refused or fails, carrying an operator sentence.

    One exception rather than a per-caller sentinel: the ``get_app`` tool renders
    it into its structured-error envelope and the IDE resolver renders it into a
    409 body, and both need the same sentence.
    """


class StagedFile(BaseModel):
    """One file written by a materialization — path relative to the app dir."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    bytes: int = Field(ge=0)


class StagedBundle(BaseModel):
    """The result of materializing an app's stored bundle onto disk.

    A validated model rather than a loose dict because it crosses two trust
    boundaries in the same shape: it becomes the ``get_app``/``stage`` tool result
    an LLM reads, and its ``directory`` becomes the bind SOURCE of an IDE
    container.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_id: str = Field(min_length=1)
    directory: str = Field(min_length=1)
    files: tuple[StagedFile, ...] = ()


class AppStagingArea:
    """The staging directory belonging to ONE session, and how to fill it.

    Atomic: the session id is the state, the store arrives as a method ARG (the
    ``get_app`` tool resolves its store lazily and the IDE resolver holds its own,
    so capturing one here would pin the wrong instance for one of them), and the
    apps root is injectable so a test never writes under the real ``/tmp`` root.
    """

    def __init__(self, *, session_id: str, apps_root: str | None = None) -> None:
        """Bind the area to *session_id*, optionally overriding the apps root."""
        self._session_id = session_id
        self._apps_root = apps_root or self.root()

    @staticmethod
    def root() -> str:
        """Base directory for app staging (env-driven, no config coupling).

        ``MEWBO_APPS_ROOT`` with a ``/tmp/mewbo/apps`` fallback — the AgentDef
        prompt uses the same ``${MEWBO_APPS_ROOT:-/tmp/mewbo/apps}`` pattern so
        writer and reader agree without config plumbing. An empty string is
        treated as unset, so ``Path("")`` can never resolve to the process CWD.
        """
        return os.environ.get("MEWBO_APPS_ROOT") or _DEFAULT_APPS_ROOT

    def app_for_session(
        self, app_store: AppStoreBase, *, session_tags: Sequence[str] = ()
    ) -> AppSpec | None:
        """The app this session owns, maintains, or was opened against; else ``None``.

        Two tiers, in order.

        **The two id FIELDS**, first: the builder session (``owner_session_id``,
        pre-submit) or the maintainer session (``maintainer_session_id``,
        post-submit) binds a session to exactly one app, and every app tool
        resolves by them — so a session matching here is one whose own tools all
        work. ``include_archived=True`` so a maintainer can still reach an
        archived app it owns.

        **The server-stamped ``app:<id>[:…]`` TAG**, second, which is how a
        session opened against an app WITHOUT becoming its maintainer resolves at
        all. Re-pointing ``maintainer_session_id`` at such a session was the
        alternative and is worse: the repair wake dereferences that field, and
        two claimants would make the first-match scan above load-bearing for
        which one keeps working.

        **The tag, and never the ``app_id`` CONTEXT key** — the same ruling the
        wiki tier already carries. A request's ``context`` is merged VERBATIM
        into the session (``backend.py:_build_context_payload`` refuses no
        unknown key) and is re-writable on any later turn, so the key ADDRESSES
        an app but authorizes nothing: reading it would let any caller name any
        app and be handed its manifest and its source. The tag is stamped only by
        ``AppLifecycle``, only for an app it just read, and cannot be re-pointed.
        It is parsed through the core grammar rather than prefix-matched, so this
        package never re-spells a tag core owns.

        *session_tags* defaults to empty, i.e. exactly the pre-tag behaviour, so
        a caller that holds no tag reader is unchanged.

        Cost: ``O(collection)`` in the number of stored apps for the field scan,
        plus one ``O(1)`` get per apps tag — the same scan ``get_app`` already
        performs per call.
        """
        for app in app_store.list_apps(include_archived=True):
            if self._session_id in (app.maintainer_session_id, app.owner_session_id):
                return app
        for tag in session_tags:
            parsed = SessionTag.parse(tag)
            if parsed is None or parsed.product != _APPS_PRODUCT:
                continue
            app_id = parsed.ids.get("app_id")
            app = app_store.get(app_id) if app_id else None
            if app is not None:
                return app
        return None

    def directory_for(self, app_id: str) -> Path:
        """The absolute staging directory for *app_id* under this session.

        Confined: the app dir must resolve under THIS session's dir, because the
        model's ``app_id`` validator bans only ``:`` — not ``/`` or ``..`` — so a
        hostile stored id could otherwise relocate anywhere within the apps root.
        """
        base = Path(self._apps_root).resolve()
        session_dir = (base / self._session_id).resolve()
        app_dir = (session_dir / app_id).resolve()
        try:
            app_dir.relative_to(session_dir)
        except ValueError:
            raise AppStagingError(
                f"app_id {app_id!r} escapes the session directory"
            ) from None
        return app_dir

    def materialize(self, app: AppSpec) -> StagedBundle:
        """Write the FULL stored bundle for *app* into its staging directory.

        The raw ``spec.frontend.files`` — every frontend file AND every
        ``mode="code"`` pipeline source, never the browser-served
        ``served_frontend_files()`` projection, since a maintainer editing a
        pipeline needs its source. Confined a second time: every target file must
        resolve under the app dir, even though each stored path already passed
        ``AppFrontend``'s traversal validator.

        Files are written in sorted order and the first escaping path aborts, so
        a refused bundle leaves nothing outside the app dir.
        """
        app_dir = self.directory_for(app.app_id)
        written: list[StagedFile] = []
        try:
            app_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in sorted(app.frontend.files.items()):
                target = (app_dir / rel).resolve()
                try:
                    target.relative_to(app_dir)
                except ValueError:
                    raise AppStagingError(
                        f"file path {rel!r} escapes the app directory"
                    ) from None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append(
                    StagedFile(path=rel, bytes=len(content.encode("utf-8")))
                )
        except OSError as exc:
            raise AppStagingError(f"could not stage the app bundle: {exc}") from exc
        return StagedBundle(
            app_id=app.app_id, directory=str(app_dir), files=tuple(written)
        )


__all__ = [
    "AppStagingArea",
    "AppStagingError",
    "StagedBundle",
    "StagedFile",
]
