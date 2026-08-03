"""Tests for DiffStat and the per-session diff aggregation it feeds."""

import json

import pytest
from mewbo_core.contracts.diff_stat import DiffStat
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_store import SessionStore

_ONE_FILE_DIFF = """--- a.py
+++ a.py
@@ -1,3 +1,4 @@
 keep me
-old line
+new line
+extra line
"""

_TWO_FILE_DIFF = _ONE_FILE_DIFF + """--- b.py
+++ b.py
@@ -1,2 +1,1 @@
 header
-dropped
"""


# -- from_unified_diff ------------------------------------------------------


def test_from_unified_diff_ignores_file_headers():
    """`+++`/`---` are file headers, not changed lines."""
    stat = DiffStat.from_unified_diff(_ONE_FILE_DIFF)
    assert (stat.additions, stat.deletions) == (2, 1)


def test_from_unified_diff_empty_text():
    """No diff at all is zero, not a crash."""
    assert DiffStat.from_unified_diff("") == DiffStat()


def test_from_unified_diff_multi_file():
    """A multi-file diff sums every hunk, skipping each file's own headers."""
    stat = DiffStat.from_unified_diff(_TWO_FILE_DIFF)
    assert (stat.additions, stat.deletions) == (2, 2)


def test_from_unified_diff_ignores_hunk_and_context_lines():
    """Only `+`/`-` bodies count; `@@` headers and context lines do not."""
    stat = DiffStat.from_unified_diff("@@ -1,1 +1,1 @@\n context\n+a\n")
    assert (stat.additions, stat.deletions) == (1, 0)


# -- from_texts -------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("a\nb\n", "a\nc\n", (1, 1)),  # replace counts on both sides
        ("", "x\ny\n", (2, 0)),  # a brand-new file is all additions
        ("x\ny\n", "", (0, 2)),
        ("same\n", "same\n", (0, 0)),
    ],
)
def test_from_texts(old, new, expected):
    """Opcode counting over the two sides."""
    stat = DiffStat.from_texts(old, new)
    assert (stat.additions, stat.deletions) == expected


# -- arithmetic + projections ----------------------------------------------


def test_add_aggregates():
    """`+` folds two stats, which is how a transcript rolls up."""
    total = DiffStat(additions=2, deletions=1) + DiffStat(additions=5, deletions=0)
    assert total == DiffStat(additions=7, deletions=1)


def test_add_rejects_a_foreign_operand():
    """Only a DiffStat can be summed into one."""
    with pytest.raises(TypeError):
        _ = DiffStat() + 3


def test_is_empty_and_render():
    """`is_empty` is the omit-the-field cue; `render` is the shared label."""
    assert DiffStat().is_empty
    assert not DiffStat(additions=1).is_empty
    assert DiffStat(additions=7, deletions=1).render() == "+7 -1"


def test_is_frozen_and_forbids_extras():
    """The trust-boundary contract: immutable and closed."""
    stat = DiffStat(additions=1, deletions=1)
    with pytest.raises(Exception):
        stat.additions = 4
    with pytest.raises(Exception):
        DiffStat(additions=1, deletions=1, changed=2)


def test_negative_counts_are_refused():
    """`ge=0` is validated at definition."""
    with pytest.raises(Exception):
        DiffStat(additions=-1)


# -- from_tool_result -------------------------------------------------------


def _envelope(**extra):
    doc = {"kind": "diff", "title": "edit", "text": _ONE_FILE_DIFF, "files": ["a.py"]}
    doc.update(extra)
    return doc


def _result_event(result, *, tool_id="file_edit_tool", success=True, tool_input=None):
    payload = {"tool_id": tool_id, "result": result, "success": success}
    if tool_input is not None:
        payload["tool_input"] = tool_input
    return {"type": "tool_result", "payload": payload}


def test_from_tool_result_prefers_the_recorded_counts():
    """The producer's own numbers win over re-tallying the text."""
    payload = _result_event(
        json.dumps(_envelope(additions=9, deletions=4))
    )["payload"]
    stat = DiffStat.from_tool_result(payload)
    assert (stat.additions, stat.deletions) == (9, 4)


def test_from_tool_result_falls_back_for_historical_events():
    """A document written before the counts existed is re-tallied from `text`."""
    payload = _result_event(json.dumps(_envelope()))["payload"]
    stat = DiffStat.from_tool_result(payload)
    assert (stat.additions, stat.deletions) == (2, 1)


def test_from_tool_result_accepts_a_dict_result():
    """Both wire shapes (JSON string and dict) resolve identically."""
    payload = _result_event(_envelope())["payload"]
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=2, deletions=1)


def test_from_tool_result_ignores_a_quoted_diff_envelope():
    """A tool that merely READ a diff document wrote no lines.

    The substring only gates the parse — the parsed `kind` is the authority.
    Without that, a page fetch or a code search over this repository would
    inflate a session's counts with lines it never produced.
    """
    quoted = json.dumps(
        {"kind": "web", "content": 'the page says {"kind": "diff", "text": "+a"}'}
    )
    payload = _result_event(quoted, tool_id="web_url_read")["payload"]
    assert DiffStat.from_tool_result(payload).is_empty


def test_from_tool_result_survives_malformed_json():
    """A clipped payload must degrade to zero, never raise."""
    broken = '{"kind": "diff", "title": "edit", "text": "+a\n[trunc'
    payload = _result_event(broken)["payload"]
    assert DiffStat.from_tool_result(payload).is_empty


def test_from_tool_result_survives_a_truncated_text():
    """A truncation marker is neither an addition nor a deletion."""
    payload = _result_event(
        json.dumps(_envelope(text="--- a.py\n+++ a.py\n+one\n[truncated]"))
    )["payload"]
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=1, deletions=0)


def test_from_tool_result_ignores_a_bogus_recorded_count():
    """A non-integer or negative count falls back to the text tally."""
    payload = _result_event(
        json.dumps(_envelope(additions="lots", deletions=-3))
    )["payload"]
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=2, deletions=1)


def test_from_tool_result_skips_a_failed_call():
    """A failed edit produced no lines."""
    payload = _result_event(json.dumps(_envelope()), success=False)["payload"]
    assert DiffStat.from_tool_result(payload).is_empty


def test_from_tool_result_synthesizes_from_edit_arguments():
    """An external edit tool leaves no envelope, so the arguments are the record."""
    payload = _result_event(
        "ok",
        tool_id="Edit",
        tool_input={"file_path": "x.py", "old_string": "a\nb\n", "new_string": "a\nc\n"},
    )["payload"]
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=1, deletions=1)


def test_from_tool_result_counts_a_write_as_all_additions():
    """A `Write` names no `old_string`, and `content` stands in for `new_string`."""
    payload = _result_event(
        "ok", tool_id="Write", tool_input={"file_path": "x.py", "content": "a\nb\nc\n"}
    )["payload"]
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=3, deletions=0)


def test_from_tool_result_never_double_counts():
    """An envelope wins; the same result must not also be synthesized."""
    payload = _result_event(
        json.dumps(_envelope(additions=2, deletions=1)),
        tool_id="file_edit_tool",
        tool_input={"file_path": "a.py", "old_string": "x\n" * 40, "new_string": ""},
    )["payload"]
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=2, deletions=1)


def test_from_tool_result_ignores_a_non_edit_tool():
    """A shell result is uncapturable and deliberately contributes nothing."""
    payload = _result_event(
        "applied",
        tool_id="aider_shell_tool",
        tool_input={"command": "git apply p.patch", "old_string": "a\n"},
    )["payload"]
    assert DiffStat.from_tool_result(payload).is_empty


def test_from_tool_result_needs_a_file_path():
    """No `file_path` means the arguments do not describe a file edit."""
    payload = _result_event(
        "ok", tool_id="Edit", tool_input={"old_string": "a\n", "new_string": "b\n"}
    )["payload"]
    assert DiffStat.from_tool_result(payload).is_empty


# -- the producer -----------------------------------------------------------


def test_format_diff_result_emits_the_counts():
    """The counts are DURABLE: recorded at the producer, not re-derived later.

    A consumer reading them back may only ever see a truncated `text`, so the
    numbers have to be written when the full diff is still in hand.
    """
    from mewbo_tools.integration.edit_common import format_diff_result

    result = format_diff_result(_ONE_FILE_DIFF, "File Edit", ["a.py"])
    assert result["kind"] == "diff"
    assert result["text"] == _ONE_FILE_DIFF
    assert result["files"] == ["a.py"]
    assert (result["additions"], result["deletions"]) == (2, 1)
    # And the round trip a session summary actually performs.
    payload = {"tool_id": "file_edit_tool", "success": True, "result": result}
    assert DiffStat.from_tool_result(payload) == DiffStat(additions=2, deletions=1)


# -- summarize_session aggregation -----------------------------------------


def test_summarize_session_aggregates_diff_stats(tmp_path):
    """Every edit-shaped result in the transcript folds into one rollup.

    Covers all three shapes together: the current envelope with recorded counts,
    an envelope carrying none (fallback parse), and an external edit
    tool with no envelope at all.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "edit it"}})
    store.append_event(
        session_id, _result_event(json.dumps(_envelope(additions=9, deletions=4)))
    )
    store.append_event(session_id, _result_event(json.dumps(_envelope())))
    store.append_event(
        session_id,
        _result_event(
            "ok",
            tool_id="Write",
            tool_input={"file_path": "n.py", "content": "a\nb\n"},
        ),
    )

    summary = runtime.summarize_session(session_id)
    assert summary["diff_stat"] == {"additions": 9 + 2 + 2, "deletions": 4 + 1 + 0}


def test_summarize_session_omits_diff_stat_when_nothing_changed(tmp_path):
    """Append-when-present: a session that edited nothing carries no key."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        session_id, _result_event("some output", tool_id="read_file_tool")
    )
    assert "diff_stat" not in runtime.summarize_session(session_id)


def test_summarize_session_tolerates_malformed_diff_payloads(tmp_path):
    """A broken or truncated result must never take a session listing down."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "edit"}})
    store.append_event(session_id, _result_event('{"kind": "diff", "text": "+a'))
    store.append_event(session_id, _result_event(None))
    store.append_event(session_id, {"type": "tool_result", "payload": "not-a-dict"})
    store.append_event(
        session_id, _result_event(json.dumps(_envelope(additions=1, deletions=0)))
    )

    summary = runtime.summarize_session(session_id)
    assert summary["diff_stat"] == {"additions": 1, "deletions": 0}
