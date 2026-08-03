"""Canonical git repository identity for a project.

One atomic class, ``RepoIdentity`` — a frozen ``(host, owner, repo)`` triple
parsed from a git remote URL or a free-form reference. It lets the API match
an incoming project key (a name, a ``owner/repo``, a full ``host/owner/repo``,
or a bare repo name) against the projects it actually manages, and it enriches
``GET /api/projects`` with each project's git identity + every alias form it is
addressable by.

Parsing handles the four ref shapes Mewbo sees in the wild:

- ``https://host/owner/repo(.git)``        (HTTP/S clone URL)
- ``ssh://git@host:port/owner/repo.git``   (SSH-scheme clone URL)
- ``git@host:owner/repo.git``              (scp-like SSH shorthand)
- ``owner/repo`` / ``repo``                (host-less / bare reference)

Recognising those shapes is NOT re-implemented here — it delegates to the one
shared grammar, ``mewbo_core.workspaces.repositories.RepositoryRef.split_remote``, which
the wiki's ``CredentialScope`` also parses through. This module keeps only its
own PROJECTION of the resulting parts, which genuinely differs from a
credential scope's: a reference with no structural host reads its LAST token as
a repo name (``Assistant`` is a repo), where a credential scope reads that same
token as a bare host. Both are right for their domain.

The host is lowercased and a trailing ``.git`` is stripped; owner/repo case is
preserved (forge hosts are case-insensitive on host but path case can matter).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from mewbo_core.workspaces.repositories import RepositoryRef


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """A normalized ``(host, owner, repo)`` view of one git remote.

    ``host`` and ``owner`` may be empty for host-less (``owner/repo``) and bare
    (``repo``) references respectively; ``repo`` is always present.
    """

    host: str
    owner: str
    repo: str

    # -- parsing -----------------------------------------------------------

    @classmethod
    def from_remote_url(cls, url: str) -> RepoIdentity | None:
        """Parse a remote URL or free-form ref into a ``RepoIdentity``.

        Returns ``None`` for a blank/whitespace-only input. The shape
        recognition (scheme URL, scp shorthand, host-less ref) and the
        normalization (host lowercased, trailing ``.git`` dropped, empty
        segments removed) come from the shared grammar; the projection below —
        owner/repo are the LAST TWO segments, everything before them collapses
        into the host or is dropped — is this class's own and is unchanged.
        """
        raw = (url or "").strip()
        if not raw:
            return None
        host, raw_segments = RepositoryRef.split_remote(raw)
        # An interior empty segment (``host/o//repo``) is preserved by the
        # shared grammar because a credential scope refuses it; a project
        # reference has always healed it instead, so drop them here.
        segments = [segment for segment in raw_segments if segment]
        if not segments:
            return cls(host=host, owner="", repo="")
        owner = segments[-2] if len(segments) >= 2 else ""
        return cls(host=host, owner=owner, repo=segments[-1])

    # -- addressing --------------------------------------------------------

    def canonical(self) -> str:
        """Return the most-specific addressable form for this identity."""
        return "/".join(p for p in (self.host, self.owner, self.repo) if p)

    def aliases(self) -> list[str]:
        """Return every form this repo is addressable by, most→least specific.

        ``host/owner/repo`` (when a host is known), ``owner/repo`` (when an
        owner is known), and the bare ``repo``. Order-preserving, de-duped.
        """
        forms: list[str] = []
        if self.host and self.owner and self.repo:
            forms.append(f"{self.host}/{self.owner}/{self.repo}")
        if self.owner and self.repo:
            forms.append(f"{self.owner}/{self.repo}")
        if self.repo:
            forms.append(self.repo)
        # Order-preserving dedupe (dict keys preserve insertion order).
        return list(dict.fromkeys(forms))

    # -- project-path reads ------------------------------------------------

    @staticmethod
    def _read_remote_urls(path: str) -> list[str]:
        """Return the unique remote fetch/push URLs configured at *path*.

        Best-effort: a non-repo path or a missing ``git`` binary yields an
        empty list (never raises).
        """
        try:
            proc = subprocess.run(
                ["git", "-C", path, "remote", "-v"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        urls: list[str] = []
        for line in proc.stdout.splitlines():
            # Format: ``<name>\t<url> (fetch|push)``
            parts = line.split()
            if len(parts) >= 2 and parts[1] not in urls:
                urls.append(parts[1])
        return urls

    @classmethod
    def for_path(cls, path: str) -> list[RepoIdentity]:
        """Return the distinct repo identities for the git repo at *path*."""
        identities: list[RepoIdentity] = []
        for url in cls._read_remote_urls(path):
            identity = cls.from_remote_url(url)
            if identity is not None and identity.repo and identity not in identities:
                identities.append(identity)
        return identities

    @classmethod
    def aliases_for_path(cls, path: str) -> list[str]:
        """Union the alias forms of every remote at *path* (order-preserving)."""
        forms: list[str] = []
        for identity in cls.for_path(path):
            forms.extend(identity.aliases())
        return list(dict.fromkeys(forms))
