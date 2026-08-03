"""Two writers racing ``update_job`` must not revert one another's fields.

A single-threaded test cannot fail on the defect these cover. The lost update
lives entirely in the window between one writer's read and its write, so the
second writer has to be INSIDE that window — and the window is opened
DETERMINISTICALLY here (one writer parks on an event after its read while the
other runs to completion) rather than by starting real threads and hoping the
interleaving turns up. That is the difference between a test that fails on
broken code every time and one that fails sometimes.

The writers are the two the pipeline actually runs concurrently: a phase tool
writing progress on a 50ms-to-5s cadence, and an HTTP request thread writing
``status``. Whichever backend is underneath, the rule is the same one — a write
names its fields, and leaves every field it did not name alone — so both
drivers run the same bodies.
"""
from __future__ import annotations

import threading
from typing import Any

import mongomock
import pytest
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore
from mewbo_graph.wiki.types import IndexingJob

_JOB_ID = "job-race"
_SLUG = "org/repo"
# Every wait is a handshake between two threads that are already running, so a
# generous ceiling costs nothing and only ever fires as a "this deadlocked"
# signal — never as the normal path.
_HANDOFF_TIMEOUT_S = 5.0


def _job() -> IndexingJob:
    """A job mid-scan: the state both racing writers start from."""
    return IndexingJob(
        job_id=_JOB_ID,
        slug=_SLUG,
        status="scanning",
        scanned_count=0,
        total_count=10,
        current_file=None,
    )


class _PausingRead:
    """A store whose FIRST job read parks until it is released.

    ``get_job`` is the read every ``update_job`` begins with, so holding one
    caller there is what puts a second writer inside the first's read→write
    window. Only the FIRST read parks: the writer we paused has to be able to
    finish, and a driver that re-reads under its own lock before writing must
    get a fresh answer rather than the same trap a second time.

    Overriding a store method the drivers legitimately call — not the method
    under test — is what keeps this a test of ``update_job``'s persisted
    outcome rather than a test of its internals.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the store and leave the trap disarmed until :meth:`arm`."""
        super().__init__(*args, **kwargs)
        self.parked = threading.Event()
        self.resume = threading.Event()
        self._armed = False

    def arm(self) -> None:
        """Make the next job read park, so a rival writer can land behind it."""
        self._armed = True

    def get_job(self, job_id: str) -> IndexingJob | None:
        """Read the job, parking the first caller that arrives while armed."""
        job = super().get_job(job_id)  # type: ignore[misc]
        if self._armed:
            self._armed = False
            self.parked.set()
            self.resume.wait(timeout=_HANDOFF_TIMEOUT_S)
        return job


class _PausingJsonStore(_PausingRead, JsonWikiStore):
    """The file-backed driver, with the read trap mixed in."""


class _PausingMongoStore(_PausingRead, MongoWikiStore):
    """The Mongo driver, with the read trap mixed in."""


@pytest.fixture(params=["json", "mongo"])
def store(request: pytest.FixtureRequest, tmp_path: Any) -> Any:
    """One racing-writer store per backend, so neither can drift from the rule."""
    if request.param == "json":
        return _PausingJsonStore(tmp_path / "wiki")
    return _PausingMongoStore(client=mongomock.MongoClient(), database="test_wiki")


def test_a_cancel_inside_a_progress_writes_window_survives(store: Any) -> None:
    """The status write must not be reverted by a writer that never named it.

    The ordering that loses it: the progress writer reads the job, a cancel
    lands and commits, and the progress writer then puts back the whole document
    it merged in memory — including the ``status`` it read before the cancel
    existed. The job goes back to running with no record that a cancel was ever
    requested.
    """
    store.create_job(_job())
    store.arm()

    def write_progress() -> None:
        store.update_job(_JOB_ID, scanned_count=7, current_file="a.py")

    writer = threading.Thread(target=write_progress, daemon=True)
    writer.start()
    assert store.parked.wait(timeout=_HANDOFF_TIMEOUT_S), "writer never read the job"

    # The whole cancel — read, status write, event append — lands in the window.
    assert store.cancel_job(_JOB_ID) is True

    store.resume.set()
    writer.join(timeout=_HANDOFF_TIMEOUT_S)
    assert not writer.is_alive(), "the progress writer never completed"

    final = store.get_job(_JOB_ID)
    assert final is not None
    assert final.status == "cancelled", "the cancel was reverted by the progress write"
    # ...and the progress writer's own fields still landed: the fix narrows the
    # write, it does not drop it.
    assert final.scanned_count == 7
    assert final.current_file == "a.py"


def test_a_progress_write_inside_a_cancels_window_survives(store: Any) -> None:
    """The mirrored ordering: the LATE writer must not revert the early one.

    Reversing which writer parks is what proves the property is "only the named
    fields are written" rather than "the status field is special". Here the
    cancel is the one holding a stale snapshot, and the progress fields written
    behind it are the ones that must not be rolled back.
    """
    store.create_job(_job())
    store.arm()

    def cancel() -> None:
        store.update_job(_JOB_ID, status="cancelled")

    writer = threading.Thread(target=cancel, daemon=True)
    writer.start()
    assert store.parked.wait(timeout=_HANDOFF_TIMEOUT_S), "writer never read the job"

    store.update_job(_JOB_ID, scanned_count=7, current_file="a.py")

    store.resume.set()
    writer.join(timeout=_HANDOFF_TIMEOUT_S)
    assert not writer.is_alive(), "the cancelling writer never completed"

    final = store.get_job(_JOB_ID)
    assert final is not None
    assert final.status == "cancelled"
    assert final.scanned_count == 7, "the progress write was reverted by the cancel"
    assert final.current_file == "a.py"


def test_the_returned_record_carries_the_concurrent_writers_field(store: Any) -> None:
    """``update_job`` returns what is STORED, not the caller's merged guess.

    A locally merged return value reports the loser's own snapshot back to it,
    so a caller that reads ``status`` off the result sees a cancel that has
    already landed as though it had not.
    """
    store.create_job(_job())
    store.arm()

    returned: list[IndexingJob] = []

    def write_progress() -> None:
        returned.append(store.update_job(_JOB_ID, scanned_count=7))

    writer = threading.Thread(target=write_progress, daemon=True)
    writer.start()
    assert store.parked.wait(timeout=_HANDOFF_TIMEOUT_S), "writer never read the job"

    store.update_job(_JOB_ID, status="cancelled")

    store.resume.set()
    writer.join(timeout=_HANDOFF_TIMEOUT_S)
    assert returned and returned[0].status == "cancelled"
    assert returned[0].scanned_count == 7


def test_an_explicitly_named_none_is_written_not_skipped(store: Any) -> None:
    """A named ``None`` is a VALUE — it is how ``emit_phase`` clears progress.

    Narrowing the write to the caller's fields must key off which fields were
    NAMED, never off which values are truthy: the phase transition clears the
    ``phase_progress_*`` triple by naming it, and that clear is the invariant
    that keeps a non-null progress reading tied to the phase it belongs to.
    """
    store.create_job(_job())
    store.update_job(
        _JOB_ID, phase="graph", phase_progress_current=12, phase_progress_unit="nodes"
    )

    store.update_job(
        _JOB_ID,
        phase="enrich",
        phase_progress_current=None,
        phase_progress_total=None,
        phase_progress_unit=None,
    )

    final = store.get_job(_JOB_ID)
    assert final is not None
    assert final.phase == "enrich"
    assert final.phase_progress_current is None
    assert final.phase_progress_unit is None


def test_naming_no_field_is_a_read(store: Any) -> None:
    """Nothing to narrow means nothing to write — and no empty backend update.

    A field-scoped write built from an empty patch is not merely pointless: a
    ``$set`` with no fields is rejected outright by the Mongo driver, so the
    no-field call has to settle before it reaches the backend.
    """
    store.create_job(_job())

    unchanged = store.update_job(_JOB_ID)

    assert unchanged.status == "scanning"
    assert unchanged.scanned_count == 0


def test_updating_an_absent_job_raises(store: Any) -> None:
    """A write to a job that does not exist stays a ``KeyError`` in both drivers."""
    with pytest.raises(KeyError):
        store.update_job("no-such-job", status="cancelled")
