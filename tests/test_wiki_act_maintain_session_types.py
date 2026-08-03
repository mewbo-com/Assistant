"""A scoped-refresh act session and an Apps maintainer session must not be
misclassified as wiki Q&A — each is a distinct workload with its own retry
policy (see ``GoalRetryGate.EXCLUDED_SESSION_TYPES``)."""

from mewbo_core.session.session_provenance import SessionOrigin, SessionTag


def test_wiki_sub_kinds_classify_distinctly():
    cases = [
        ("wiki:job:job-1", "wiki_index", {"wiki_id": "job-1"}),
        ("wiki:act:job-2", "wiki_act", {"wiki_id": "job-2"}),
        ("wiki:maintain:app-3", "wiki_maintain", {"wiki_id": "app-3"}),
        ("wiki:qa:ans-4", "wiki_qa", {"wiki_id": "ans-4"}),
    ]
    session_types = set()
    for tag, expected_type, expected_ids in cases:
        parsed = SessionTag.parse(tag)
        assert parsed is not None, f"{tag} failed to parse"
        assert parsed.session_type == expected_type
        assert parsed.ids == expected_ids
        assert parsed.origin is SessionOrigin.WIKI
        session_types.add(parsed.session_type)

    # Four tags, four distinct session types — none fell through to a shared default.
    assert len(session_types) == 4
