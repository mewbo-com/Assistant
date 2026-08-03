package com.mewbo.aura.ui.sessions

import com.mewbo.aura.data.model.SessionSummary
import java.time.Instant
import java.time.ZoneOffset
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [SessionGrouping.group] contract: bucket by `updatedAt` (falling back to `createdAt`) via the
 * shared [com.mewbo.aura.data.model.Timestamps] parser, calendar-day comparison in the caller's
 * zone, unparseable/blank timestamps sink to OLDER rather than crashing, empty buckets are
 * omitted, and input order is preserved within a bucket (the API already returns created_at
 * desc — see `ui/sessions/RelativeTime` for the same parsing house-idiom this reuses).
 */
class SessionGroupingTest {

    private fun session(
        id: String,
        updatedAt: String = "",
        createdAt: String = "2026-01-01T00:00:00+00:00",
        pinned: Boolean = false,
        pinnedAt: String? = null,
    ) = SessionSummary(
        sessionId = id,
        title = "Session $id",
        status = "idle",
        running = false,
        doneReason = null,
        origin = null,
        recoverable = true,
        createdAt = createdAt,
        updatedAt = updatedAt,
        pinned = pinned,
        pinnedAt = pinnedAt,
    )

    // Fixed "now": 2026-07-15T12:00:00Z. In UTC, today = 2026-07-15, so the previous-7-days
    // window (>= today-7, < today) is [2026-07-08, 2026-07-15).
    private val now = Instant.parse("2026-07-15T12:00:00Z")
    private val utc = ZoneOffset.UTC

    @Test
    fun `session updated today buckets as TODAY`() {
        val s = session("a", updatedAt = "2026-07-15T00:00:01+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.TODAY), result.map { it.header })
        assertEquals(listOf(s), result.single().sessions)
    }

    @Test
    fun `session updated yesterday is NOT today, falls into previous 7 days`() {
        val s = session("a", updatedAt = "2026-07-14T23:59:59+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.PREVIOUS_7_DAYS), result.map { it.header })
    }

    @Test
    fun `day 6 back is within the previous-7-days window`() {
        // today.minusDays(6) = 2026-07-09
        val s = session("a", updatedAt = "2026-07-09T12:00:00+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.PREVIOUS_7_DAYS), result.map { it.header })
    }

    @Test
    fun `day 7 back is the inclusive lower edge of the previous-7-days window`() {
        // today.minusDays(7) = 2026-07-08 — spec says ">= today-7", so this is still in-window.
        val s = session("a", updatedAt = "2026-07-08T00:00:00+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.PREVIOUS_7_DAYS), result.map { it.header })
    }

    @Test
    fun `day 8 back falls outside the window into OLDER`() {
        // today.minusDays(8) = 2026-07-07
        val s = session("a", updatedAt = "2026-07-07T23:59:59+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.OLDER), result.map { it.header })
    }

    @Test
    fun `far past timestamp buckets as OLDER`() {
        val s = session("a", updatedAt = "2020-01-01T00:00:00+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.OLDER), result.map { it.header })
    }

    @Test
    fun `unparseable updatedAt does not crash and buckets as OLDER`() {
        val s = session("a", updatedAt = "not-a-timestamp", createdAt = "also-not-a-timestamp")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.OLDER), result.map { it.header })
        assertEquals(listOf(s), result.single().sessions)
    }

    @Test
    fun `blank updatedAt falls back to createdAt`() {
        val s = session("a", updatedAt = "", createdAt = "2026-07-15T01:00:00+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertEquals(listOf(SessionSectionHeader.TODAY), result.map { it.header })
    }

    @Test
    fun `empty input yields an empty list`() {
        val result = SessionGrouping.group(emptyList(), now, utc)

        assertEquals(emptyList<SessionSection>(), result)
    }

    @Test
    fun `empty buckets are omitted and section order is fixed`() {
        val today = session("today", updatedAt = "2026-07-15T00:00:00+00:00")
        val older = session("older", updatedAt = "2020-01-01T00:00:00+00:00")
        // Deliberately no PREVIOUS_7_DAYS session — that bucket must be absent, not empty.

        val result = SessionGrouping.group(listOf(older, today), now, utc)

        assertEquals(listOf(SessionSectionHeader.TODAY, SessionSectionHeader.OLDER), result.map { it.header })
    }

    @Test
    fun `input order is preserved within a bucket`() {
        val first = session("first", updatedAt = "2026-07-15T01:00:00+00:00")
        val second = session("second", updatedAt = "2026-07-15T02:00:00+00:00")
        val third = session("third", updatedAt = "2026-07-15T03:00:00+00:00")

        // Input deliberately NOT in recency order — grouping must not re-sort.
        val result = SessionGrouping.group(listOf(third, first, second), now, utc)

        assertEquals(listOf(third, first, second), result.single().sessions)
    }

    @Test
    fun `zone conversion changes which bucket a session lands in`() {
        // now = 2026-07-02T00:30:00Z: just after UTC midnight.
        val zonedNow = Instant.parse("2026-07-02T00:30:00Z")
        // One hour before "now", still the previous UTC calendar day.
        val s = session("a", updatedAt = "2026-07-01T23:30:00+00:00")

        val inUtc = SessionGrouping.group(listOf(s), zonedNow, ZoneOffset.UTC)
        // UTC: today = 2026-07-02, session date = 2026-07-01 -> yesterday, in the 7-day window.
        assertEquals(listOf(SessionSectionHeader.PREVIOUS_7_DAYS), inUtc.map { it.header })

        // A +2:00 zone shifts BOTH "now" and the session's instant forward two hours: now lands at
        // 2026-07-02T02:30+02:00 (still date 2026-07-02) and the session lands at
        // 2026-07-02T01:30+02:00 (ALSO date 2026-07-02) - the same session is TODAY here, proving
        // bucketing is computed against the caller's zone, not a UTC-fixed calendar day.
        val plusTwo = ZoneOffset.ofHours(2)
        val inPlusTwo = SessionGrouping.group(listOf(s), zonedNow, plusTwo)
        assertEquals(listOf(SessionSectionHeader.TODAY), inPlusTwo.map { it.header })

        assertTrue(inUtc.single().header != inPlusTwo.single().header)
    }

    @Test
    fun `a pinned session gets its own PINNED bucket ahead of every date bucket, regardless of age`() {
        // Old enough to sort OLDER on recency alone — pinning must override that entirely.
        val ancientPinned = session("a", updatedAt = "2020-01-01T00:00:00+00:00", pinned = true, pinnedAt = "2026-07-01T00:00:00+00:00")
        val today = session("b", updatedAt = "2026-07-15T00:00:00+00:00")

        val result = SessionGrouping.group(listOf(today, ancientPinned), now, utc)

        assertEquals(listOf(SessionSectionHeader.PINNED, SessionSectionHeader.TODAY), result.map { it.header })
        assertEquals(listOf(ancientPinned), result.first().sessions)
    }

    @Test
    fun `a pinned session is excluded from date bucketing, never appearing twice`() {
        val pinned = session("a", updatedAt = "2026-07-15T00:00:00+00:00", pinned = true, pinnedAt = "2026-07-10T00:00:00+00:00")

        val result = SessionGrouping.group(listOf(pinned), now, utc)

        assertEquals(listOf(SessionSectionHeader.PINNED), result.map { it.header })
        assertEquals(1, result.flatMap { it.sessions }.size)
    }

    @Test
    fun `pinned sessions order most-recently-pinned first`() {
        val olderPin = session("a", pinned = true, pinnedAt = "2026-07-01T00:00:00+00:00")
        val newerPin = session("b", pinned = true, pinnedAt = "2026-07-10T00:00:00+00:00")

        val result = SessionGrouping.group(listOf(olderPin, newerPin), now, utc)

        assertEquals(listOf(newerPin, olderPin), result.single().sessions)
    }

    @Test
    fun `an unparseable pinnedAt sinks to the end of the pinned bucket rather than crashing`() {
        val goodPin = session("a", pinned = true, pinnedAt = "2026-07-10T00:00:00+00:00")
        val badPin = session("b", pinned = true, pinnedAt = "not-a-timestamp")

        val result = SessionGrouping.group(listOf(badPin, goodPin), now, utc)

        assertEquals(listOf(goodPin, badPin), result.single().sessions)
    }

    @Test
    fun `no pinned sessions omits the PINNED bucket entirely`() {
        val s = session("a", updatedAt = "2026-07-15T00:00:00+00:00")

        val result = SessionGrouping.group(listOf(s), now, utc)

        assertTrue(result.none { it.header == SessionSectionHeader.PINNED })
    }
}
