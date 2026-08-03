#!/usr/bin/env python3
"""What project a session's context DECLARES, as opposed to what it resolves to.

:class:`ProjectCatalog` answers "where on disk is project X". This module answers
the cheaper question that comes first: *given a session's context payload, which
project identity is it claiming?* The two are deliberately separate — resolution
needs the catalog (and therefore config, the project store and the repository
store); identity needs nothing but the payload, which is what lets the session
store call it on the append hot path without dragging that graph behind it.

The rule itself is not new. It was already spelled out inside the MCP session
lister, which matched a row when the requested project equalled ``context.repo``
or ``context.project`` with any ``managed:`` prefix stripped. That copy was the
only definition, so nothing else could filter by project without restating it.
It lives here now, once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Mapping

# The auto-select sentinel. A session carrying this has NOT chosen a project: it
# starts in a temporary directory and the agent picks one (and may pick again
# later, if the task spans several). Declared here rather than in
# ``project_catalog`` so the light modules that only need to RECOGNISE it — the
# session store's append hook among them — need not import the resolver.
AUTO_PROJECT: str = "auto"


class ProjectIdentity:
    """The identity rule for a session context's project binding.

    Pure and I/O-free, like :class:`~mewbo_core.session.session_provenance.SessionOrigin`
    — the payload arrives as an argument, so the same rule serves the append
    path, a filter predicate and a test with no clock, store or catalog in sight.
    """

    #: Prefix marking a project Mewbo created and owns (a managed project or a
    #: worktree). The suffix is a uuid, so the prefixed form is high-cardinality
    #: and useless as a filter value — callers want the bare identity.
    MANAGED_PREFIX: ClassVar[str] = "managed:"

    #: Context keys that can carry a project identity, in precedence order.
    #: ``repo`` wins because it names the underlying repository, which is what a
    #: user means by "this project", whereas ``project`` may be a local alias.
    CONTEXT_KEYS: ClassVar[tuple[str, ...]] = ("repo", "project")

    @classmethod
    def is_auto(cls, value: object) -> bool:
        """Whether *value* is the auto-select sentinel rather than a project."""
        return isinstance(value, str) and value.strip() == AUTO_PROJECT

    @classmethod
    def normalize(cls, value: object) -> str | None:
        """Reduce one raw context value to a filterable identity, or ``None``.

        ``None`` means "declares no project", and the three ways to get there are
        deliberately collapsed: absent, empty, and the auto sentinel. A sentinel
        is not a project — storing it would make ``?project=auto`` look like a
        real query and would file every not-yet-decided session under one name.
        """
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or cls.is_auto(cleaned):
            return None
        if cleaned.startswith(cls.MANAGED_PREFIX):
            cleaned = cleaned[len(cls.MANAGED_PREFIX) :].strip()
        return cleaned or None

    @classmethod
    def from_context(cls, context: Mapping[str, object]) -> str | None:
        """The project identity a context payload declares, if any.

        Takes a ``Mapping`` rather than a ``dict`` so a caller holding a typed
        event payload can pass it without a cast — the rule only ever reads keys.
        """
        for key in cls.CONTEXT_KEYS:
            identity = cls.normalize(context.get(key))
            if identity is not None:
                return identity
        return None

    @classmethod
    def matches(cls, requested: str, identities: list[str]) -> bool:
        """Whether a requested project name selects a session holding *identities*.

        The requested value is normalized the same way the stored ones were, so a
        caller may pass ``managed:<uuid>`` and still match the bare identity that
        was recorded from it.
        """
        wanted = cls.normalize(requested)
        return wanted is not None and wanted in identities
