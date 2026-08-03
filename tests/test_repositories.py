"""Repository identity + registry: the shared expectation table and the store.

Drives the real code paths — no store is stubbed beyond the in-memory driver,
which is itself production code. The URL table is read off
``tests/fixtures/repository_identity_cases.json``, the SAME file the console's
slug tests read, so a divergence between the two parsers fails on both sides
instead of being discovered in a browser.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from mewbo_api.repo_identity import RepoIdentity
from mewbo_core.workspaces.repositories import (
    PLATFORM_HOSTS,
    Repository,
    RepositoryCredentialUsage,
    RepositoryPatch,
    RepositoryRef,
    RepositoryUsage,
    RepositoryWikiUsage,
)
from mewbo_core.workspaces.repository_store import InMemoryRepositoryStore, JsonRepositoryStore
from mewbo_graph.wiki.credentials import CredentialScope
from pydantic import ValidationError

FIXTURE = Path(__file__).parent / "fixtures" / "repository_identity_cases.json"
FIXTURE_DOC = json.loads(FIXTURE.read_text())
CASES = FIXTURE_DOC["cases"]
PARSEABLE = [case for case in CASES if case["parsed"] is not None]
UNPARSEABLE = [case for case in CASES if case["parsed"] is None]


def _ids(cases: list[dict]) -> list[str]:
    return [case["id"] for case in cases]


class TestRepositoryRefTable:
    """Every shape in the shared expectation table."""

    @pytest.mark.parametrize("case", PARSEABLE, ids=_ids(PARSEABLE))
    def test_parses_to_the_expected_identity(self, case: dict) -> None:
        ref = RepositoryRef.from_url(case["input"])
        expected = case["parsed"]
        assert ref.slug == expected["slug"]
        assert ref.host == expected["host"]
        assert list(ref.namespace) == expected["namespace"]
        assert ref.owner == expected["owner"]
        assert ref.repo == expected["repo"]

    @pytest.mark.parametrize("case", UNPARSEABLE, ids=_ids(UNPARSEABLE))
    def test_non_identities_raise_and_coerce_to_none(self, case: dict) -> None:
        with pytest.raises(ValueError):
            RepositoryRef.from_url(case["input"])
        assert RepositoryRef.coerce(case["input"]) is None

    @pytest.mark.parametrize("case", PARSEABLE, ids=_ids(PARSEABLE))
    def test_slug_round_trips(self, case: dict) -> None:
        """Re-parsing a composed slug must reproduce it — it is the store key."""
        slug = case["parsed"]["slug"]
        assert RepositoryRef.from_slug(slug).slug == slug

    @pytest.mark.parametrize("case", PARSEABLE, ids=_ids(PARSEABLE))
    def test_platform_matches_the_table(self, case: dict) -> None:
        assert RepositoryRef.from_url(case["input"]).platform == case["parsed"]["platform"]


class TestRepositoryRefRules:
    """The rules that are not a URL shape."""

    def test_subgroup_owner_repo_are_the_last_two_segments(self) -> None:
        ref = RepositoryRef.from_url("https://gitlab.com/acme/platform/beacon.git")
        assert (ref.owner, ref.repo) == ("platform", "beacon")
        # The intermediate group survives in the slug but never widens the host,
        # which a host-scoped credential is keyed by.
        assert ref.host == "gitlab.com"
        assert ref.slug == "gitlab.com/acme/platform/beacon"

    def test_from_slug_rejects_a_url(self) -> None:
        with pytest.raises(ValueError, match="not a URL"):
            RepositoryRef.from_slug("https://github.com/bearlike/Assistant")

    def test_coerce_passes_a_ref_through(self) -> None:
        ref = RepositoryRef.from_url("https://github.com/bearlike/Assistant")
        assert RepositoryRef.coerce(ref) is ref

    def test_empty_part_is_refused_at_definition(self) -> None:
        with pytest.raises(ValidationError):
            RepositoryRef(host="github.com", owner="", repo="Assistant")

    def test_whitespace_part_is_refused_at_definition(self) -> None:
        with pytest.raises(ValidationError):
            RepositoryRef(host="github.com", owner="bear like", repo="Assistant")

    def test_https_url_is_composed_from_the_slug(self) -> None:
        ref = RepositoryRef.from_slug("gitlab.com/acme/platform/beacon")
        assert ref.https_url() == "https://gitlab.com/acme/platform/beacon"

    def test_the_host_mapping_is_the_one_in_the_shared_fixture(self) -> None:
        """Core, the wiki catalogue and the console all read one mapping.

        Asserted against the fixture rather than against a copy in this file,
        because the fixture is what the console's own tests build their
        catalogue from — so a host added here and nowhere else fails on both
        sides of the language boundary instead of drifting silently.
        """
        assert PLATFORM_HOSTS == FIXTURE_DOC["platformHosts"]

    def test_platform_never_guesses_from_a_substring(self) -> None:
        """The rule a heuristic gets wrong, asserted as a property.

        A ``git.`` prefix, a ``gitea`` substring or a ``gitlab`` substring in a
        hostname is not evidence of anything — a persisted platform is rendered
        to users, so ``"git"`` is the honest answer for a host nobody can place.
        """
        for host in ("git.example.com", "gitea.example.com", "gitlab.example.com"):
            assert RepositoryRef.from_slug(f"{host}/acme/beacon").platform == "git"


class TestCredentialScopeCompatibility:
    """``CredentialScope``'s public behaviour after delegating its grammar."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/bearlike/Assistant.git", "github.com/bearlike/Assistant"),
            ("https://Git.Home/o/repo.git/", "git.home/o/repo"),
            ("git@git.example.com:acme/beacon.git", "git.example.com/acme/beacon"),
            ("ssh://git@git.example.com:2222/acme/beacon.git", "git.example.com/acme/beacon"),
            ("git.example.com", "git.example.com"),
            ("gitlab.com/acme/platform/beacon", "gitlab.com/acme/platform/beacon"),
            ("https://github.com/", "github.com"),
        ],
    )
    def test_from_repo_url_values_are_unchanged(self, url: str, expected: str) -> None:
        assert CredentialScope.from_repo_url(url).value == expected

    def test_a_lone_token_is_still_a_host_scope(self) -> None:
        """The projection that must NOT be shared with a repository reference."""
        scope = CredentialScope.from_repo_url("git.example.com")
        assert scope.kind == "host"
        assert scope.host == "git.example.com"
        assert scope.owner is None and scope.repo is None

    def test_subgroup_owner_repo_still_read_the_last_two_segments(self) -> None:
        scope = CredentialScope.from_slug("gitlab.com/acme/platform/beacon")
        assert (scope.host, scope.owner, scope.repo) == ("gitlab.com", "platform", "beacon")
        assert scope.host_scope().value == "gitlab.com"

    def test_host_scope_still_covers_every_repo_on_that_host(self) -> None:
        host = CredentialScope.from_slug("git.example.com")
        repo = CredentialScope.from_repo_url("https://git.example.com/acme/beacon.git")
        assert host.covers(repo)
        assert not repo.covers(host)

    def test_a_blank_remote_still_raises(self) -> None:
        with pytest.raises(ValueError):
            CredentialScope.from_repo_url("")

    def test_coerce_still_degrades_instead_of_raising(self) -> None:
        assert CredentialScope.coerce("git.example.com//repo") is None


class TestRepoIdentityCompatibility:
    """``RepoIdentity``'s public behaviour after delegating its grammar."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/bearlike/Assistant.git", ("github.com", "bearlike", "Assistant")),
            ("https://git.example.com/acme/Assistant", ("git.example.com", "acme", "Assistant")),
            ("git@github.com:bearlike/Assistant.git", ("github.com", "bearlike", "Assistant")),
            (
                "ssh://git@git.example.com:2222/acme/Assistant.git",
                ("git.example.com", "acme", "Assistant"),
            ),
            ("https://GitHub.COM/Bearlike/Assistant.git", ("github.com", "Bearlike", "Assistant")),
            ("bearlike/Assistant", ("", "bearlike", "Assistant")),
            ("Assistant", ("", "", "Assistant")),
            ("Assistant.git", ("", "", "Assistant")),
            ("https://github.com/", ("github.com", "", "")),
        ],
    )
    def test_from_remote_url_triples_are_unchanged(
        self, url: str, expected: tuple[str, str, str]
    ) -> None:
        identity = RepoIdentity.from_remote_url(url)
        assert identity is not None
        assert (identity.host, identity.owner, identity.repo) == expected

    def test_blank_input_still_returns_none(self) -> None:
        assert RepoIdentity.from_remote_url("") is None
        assert RepoIdentity.from_remote_url("   ") is None

    def test_a_lone_token_is_still_a_repo_name(self) -> None:
        """The mirror image of the credential-scope projection above."""
        identity = RepoIdentity.from_remote_url("Assistant")
        assert identity is not None
        assert identity.repo == "Assistant"
        assert identity.host == ""

    def test_aliases_are_unchanged(self) -> None:
        identity = RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")
        assert identity.aliases() == [
            "github.com/bearlike/Assistant",
            "bearlike/Assistant",
            "Assistant",
        ]
        assert identity.canonical() == "github.com/bearlike/Assistant"


class TestRepositoryModel:
    """The registry record."""

    def test_identity_is_derived_from_the_slug(self) -> None:
        repository = Repository(slug="GitHub.com/Bearlike/Assistant.git")
        assert repository.slug == "github.com/Bearlike/Assistant"
        assert (repository.host, repository.owner, repository.repo) == (
            "github.com",
            "Bearlike",
            "Assistant",
        )

    def test_a_supplied_triple_cannot_drift_from_the_slug(self) -> None:
        repository = Repository(slug="github.com/bearlike/Assistant", owner="someone-else")
        assert repository.owner == "bearlike"

    def test_platform_is_derived_from_the_host(self) -> None:
        assert Repository(slug="github.com/bearlike/Assistant").platform == "github"
        assert Repository(slug="codeberg.org/acme/beacon").platform == "gitea"
        assert Repository(slug="acme.visualstudio.com/acme/beacon").platform == "azure"

    def test_an_unknown_host_leaves_platform_neutral_AND_unset(self) -> None:
        """Both halves matter — see the merge test below for why ``unset`` does."""
        repository = Repository(slug="git.example.com/acme/beacon")
        assert repository.platform == "git"
        assert "platform" not in repository.model_fields_set

    def test_an_explicit_platform_survives_re_validation(self) -> None:
        """An operator labelling a self-hosted forge must not be re-derived away."""
        labelled = Repository(slug="git.example.com/acme/beacon", platform="gitlab")
        assert labelled.platform == "gitlab"
        assert Repository.model_validate(labelled.model_dump()).platform == "gitlab"

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Repository(slug="github.com/bearlike/Assistant", token="mk_secret")

    def test_a_slug_that_is_not_an_identity_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Repository(slug="bearlike/Assistant")

    def test_clone_url_prefers_the_persisted_remote(self) -> None:
        composed = Repository(slug="github.com/bearlike/Assistant")
        assert composed.clone_url() == "https://github.com/bearlike/Assistant"
        stored = Repository(
            slug="git.example.com/acme/beacon",
            repoUrl="ssh://git@git.example.com:2222/acme/beacon.git",
        )
        assert stored.clone_url() == "ssh://git@git.example.com:2222/acme/beacon.git"

    def test_display_name_falls_back_to_owner_repo(self) -> None:
        assert Repository(slug="github.com/bearlike/Assistant").display_name == "bearlike/Assistant"
        named = Repository(slug="github.com/bearlike/Assistant", name="Mewbo")
        assert named.display_name == "Mewbo"

    def test_to_wire_is_camel_case_with_usage_folded_in(self) -> None:
        repository = Repository(
            slug="github.com/bearlike/Assistant",
            repoUrl="https://github.com/bearlike/Assistant.git",
            platform="github",
            defaultBranch="main",
            createdAt="2020-01-01T00:00:00+00:00",
            updatedAt="2020-01-02T00:00:00+00:00",
        )
        usage = RepositoryUsage(
            wiki=RepositoryWikiUsage(indexed=True, indexedAt="2020-01-03T00:00:00Z", pages=12),
            credential=RepositoryCredentialUsage(scope="github.com", scopeType="host"),
        )
        wire = repository.to_wire(usage)
        assert set(wire) == {
            "slug",
            "host",
            "owner",
            "repo",
            "repoUrl",
            "platform",
            "defaultBranch",
            "name",
            "description",
            "origin",
            "createdAt",
            "updatedAt",
            "usage",
        }
        assert wire["repoUrl"] == "https://github.com/bearlike/Assistant.git"
        assert wire["defaultBranch"] == "main"
        assert wire["usage"]["wiki"] == {
            "indexed": True,
            "indexedAt": "2020-01-03T00:00:00Z",
            "pages": 12,
        }
        assert wire["usage"]["credential"] == {"scope": "github.com", "scopeType": "host"}
        assert wire["usage"]["tasks"] is None

    def test_to_wire_without_usage_still_carries_the_key(self) -> None:
        wire = Repository(slug="github.com/bearlike/Assistant").to_wire()
        assert wire["usage"] is None

    def test_usage_is_pure_and_defaults_to_absent(self) -> None:
        usage = RepositoryUsage()
        assert (usage.wiki, usage.tasks, usage.credential) == (None, None, None)

    def test_usage_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            RepositoryUsage(search={"indexed": True})


class TestRepositoryPatch:
    """Partial updates."""

    def test_only_set_fields_move(self) -> None:
        repository = Repository(
            slug="github.com/bearlike/Assistant", name="Mewbo", platform="github"
        )
        patched = RepositoryPatch(description="An assistant").apply(repository)
        assert patched.description == "An assistant"
        assert patched.name == "Mewbo"
        assert patched.platform == "github"

    def test_an_explicit_null_clears(self) -> None:
        repository = Repository(slug="github.com/bearlike/Assistant", name="Mewbo")
        assert RepositoryPatch(name=None).apply(repository).name is None

    def test_identity_fields_are_not_patchable(self) -> None:
        with pytest.raises(ValidationError):
            RepositoryPatch(slug="github.com/someone/else")


class TestRepositoryStore:
    """Registry semantics, exercised on the in-memory driver."""

    def _store(self) -> InMemoryRepositoryStore:
        return InMemoryRepositoryStore()

    def test_register_persists_and_stamps_timestamps(self) -> None:
        store = self._store()
        stored = store.register(Repository(slug="github.com/bearlike/Assistant"))
        assert stored.created_at and stored.updated_at
        assert store.get("github.com/bearlike/Assistant") == stored

    def test_register_is_idempotent_on_slug(self) -> None:
        store = self._store()
        first = store.register(Repository(slug="github.com/bearlike/Assistant", origin="manual"))
        second = store.register(
            Repository(slug="github.com/bearlike/Assistant", origin="wiki", platform="github")
        )
        assert len(store.list()) == 1
        # created_at and origin record WHEN and by WHOM the repo entered.
        assert second.created_at == first.created_at
        assert second.origin == "manual"
        # A field the re-registration fills still wins.
        assert second.platform == "github"

    def test_re_registration_does_not_clobber_with_defaults(self) -> None:
        store = self._store()
        store.register(
            Repository(slug="github.com/bearlike/Assistant", platform="github", name="Mewbo")
        )
        again = store.register(Repository(slug="github.com/bearlike/Assistant"))
        assert again.platform == "github"
        assert again.name == "Mewbo"

    def test_re_registration_preserves_an_operator_platform_label(self) -> None:
        """The trap the neutral-default carve-out exists for.

        A self-hosted forge an operator labelled `gitlab` must survive a wiki or
        task re-registering the same slug — those callers name no platform, and
        the host implies none, so nothing may be written over the label.
        """
        store = self._store()
        store.register(Repository(slug="git.example.com/acme/beacon", platform="gitlab"))
        again = store.register(Repository(slug="git.example.com/acme/beacon", origin="wiki"))
        assert again.platform == "gitlab"

    def test_lookup_normalizes_the_slug(self) -> None:
        store = self._store()
        store.register(Repository(slug="github.com/bearlike/Assistant"))
        assert store.get("GitHub.com/bearlike/Assistant.git/") is not None
        assert store.get("https://github.com/bearlike/Assistant") is not None

    def test_lookup_of_a_non_identity_is_a_miss_not_a_raise(self) -> None:
        store = self._store()
        assert store.get("bearlike/Assistant") is None
        assert store.delete("") is False

    def test_list_is_ordered_by_slug(self) -> None:
        store = self._store()
        for slug in ("git.example.com/acme/beacon", "github.com/bearlike/Assistant"):
            store.register(Repository(slug=slug))
        store.register(Repository(slug="gitlab.com/acme/platform/beacon"))
        assert [r.slug for r in store.list()] == [
            "git.example.com/acme/beacon",
            "github.com/bearlike/Assistant",
            "gitlab.com/acme/platform/beacon",
        ]

    def test_patch_updates_and_returns_none_for_an_unknown_slug(self) -> None:
        store = self._store()
        store.register(Repository(slug="github.com/bearlike/Assistant"))
        patched = store.patch("github.com/bearlike/Assistant", RepositoryPatch(name="Mewbo"))
        assert patched is not None and patched.name == "Mewbo"
        assert store.get("github.com/bearlike/Assistant").name == "Mewbo"
        assert store.patch("github.com/nobody/nothing", RepositoryPatch(name="x")) is None

    def test_delete_reports_whether_a_row_was_removed(self) -> None:
        store = self._store()
        store.register(Repository(slug="github.com/bearlike/Assistant"))
        assert store.delete("github.com/bearlike/Assistant") is True
        assert store.delete("github.com/bearlike/Assistant") is False
        assert store.list() == []

    def test_registration_touches_nothing_outside_the_store(self, monkeypatch) -> None:
        """Registration is INERT — no subprocess, no network, no clone."""

        def _forbidden(*args, **kwargs):  # pragma: no cover - the assertion is that it never runs
            raise AssertionError("registration must not shell out")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        store = self._store()
        store.register(
            Repository(
                slug="git.example.com/acme/beacon",
                repoUrl="https://git.example.com/acme/beacon.git",
            )
        )
        assert store.get("git.example.com/acme/beacon") is not None


def test_wiki_platform_cards_carry_the_core_hosts() -> None:
    """The wizard's cards and the registry must claim the same hosts.

    Asserts the DERIVATION, not the current values: every host on a card comes
    from ``PLATFORM_HOSTS`` and every host in ``PLATFORM_HOSTS`` reaches its
    card. A second hand-kept list in the catalogue would drift invisibly — the
    wizard would pre-select one platform while the registry stored another.
    """
    from mewbo_api.wiki.catalogues import PLATFORMS

    on_cards = {host: card.id for card in PLATFORMS for host in card.hosts}
    assert on_cards == PLATFORM_HOSTS


_BASE_INSTALL_PROBE = """
import json, sys
import mewbo_core.workspaces.repositories as repositories
import mewbo_core.workspaces.repository_store as repository_store

store = repository_store.InMemoryRepositoryStore()
row = store.register(repositories.Repository(slug="github.com/bearlike/Assistant"))
print(json.dumps({
    "pymongo": "pymongo" in sys.modules,
    "graph": any(name.startswith("mewbo_graph") for name in sys.modules),
    "slug": row.slug,
}))
"""


def test_registry_is_usable_on_a_base_install() -> None:
    """The registry must not drag in pymongo or the optional graph library.

    The whole reason this lives in core is that agentic tasks run on a base
    install. That is a claim about the IMPORT GRAPH, which an in-process test
    structurally cannot make — this module's own suite already imported
    ``mewbo_graph`` for the credential-scope checks above, so ``sys.modules``
    would report it resident no matter what. Hence a fresh interpreter, the
    ``test_iam_architecture.py`` probe pattern.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _BASE_INSTALL_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["pymongo"] is False
    assert result["graph"] is False
    assert result["slug"] == "github.com/bearlike/Assistant"


class TestJsonRepositoryStore:
    """The durable driver behaves identically and survives a reopen."""

    def test_crud_round_trips_through_the_file(self, tmp_path: Path) -> None:
        data_file = tmp_path / "repositories.json"
        store = JsonRepositoryStore(data_file=data_file)
        store.register(Repository(slug="github.com/bearlike/Assistant", platform="github"))
        store.patch("github.com/bearlike/Assistant", RepositoryPatch(name="Mewbo"))

        reopened = JsonRepositoryStore(data_file=data_file)
        row = reopened.get("github.com/bearlike/Assistant")
        assert row is not None
        assert (row.platform, row.name) == ("github", "Mewbo")
        assert reopened.delete("github.com/bearlike/Assistant") is True
        assert JsonRepositoryStore(data_file=data_file).list() == []

    def test_a_malformed_record_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        data_file = tmp_path / "repositories.json"
        data_file.write_text(
            json.dumps([{"slug": "bearlike/Assistant"}, {"slug": "github.com/bearlike/Assistant"}])
        )
        assert [r.slug for r in JsonRepositoryStore(data_file=data_file).list()] == [
            "github.com/bearlike/Assistant"
        ]
