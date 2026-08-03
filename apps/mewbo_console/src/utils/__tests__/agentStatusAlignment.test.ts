/**
 * Pins `agentStatus.ts`'s vocabulary against `hypervisor.py`'s `AgentStatus`
 * Literal (packages/mewbo_core/src/mewbo_core/hypervisor.py `AgentStatus`) — a
 * hardcoded mirror, not a codegen pipeline. The FE has no way to import a
 * Python Literal, so this is a deliberate, proportionate stand-in: it turns
 * "the comment says six keys" into a test that fails the moment the two
 * files disagree, rather than a comment that goes stale silently (which is
 * exactly how `rejected` went missing from `StatusKey` for a while — the
 * comment claimed alignment and nobody had a red test to say otherwise).
 *
 * If `hypervisor.py`'s `AgentStatus` ever changes, update `HYPERVISOR_AGENT_STATUS`
 * here in the SAME change.
 */
import { describe, expect, it } from "vitest";
import {
  AGENT_STATUS_KEYS,
  STATUS_ORDER,
  STATUS_STYLES,
  isTerminal,
  statusKey,
  type StatusKey,
} from "../agentStatus";

// Verbatim copy of hypervisor.py's `AgentStatus` Literal members. No `queued`
// — A2A v1.0 (which that Literal cites) has no queued/pending state; a
// capacity-deferred unit is `submitted` with a real `agent_id`.
const HYPERVISOR_AGENT_STATUS: readonly StatusKey[] = [
  "submitted",
  "running",
  "completed",
  "failed",
  "cancelled",
  "rejected",
];

describe("agentStatus.ts vocabulary matches hypervisor.py's AgentStatus", () => {
  it("AGENT_STATUS_KEYS is exactly the six hypervisor states, no more, no fewer", () => {
    expect(new Set(AGENT_STATUS_KEYS)).toEqual(new Set(HYPERVISOR_AGENT_STATUS));
    expect(AGENT_STATUS_KEYS).toHaveLength(6);
  });

  it("never gains a queued/pending member — A2A v1.0 has no such state", () => {
    expect(AGENT_STATUS_KEYS).not.toContain("queued");
    expect(AGENT_STATUS_KEYS).not.toContain("pending");
  });

  it("STATUS_STYLES and STATUS_ORDER cover exactly the same set as AGENT_STATUS_KEYS", () => {
    expect(new Set(Object.keys(STATUS_STYLES))).toEqual(new Set(AGENT_STATUS_KEYS));
    expect(new Set(STATUS_ORDER)).toEqual(new Set(AGENT_STATUS_KEYS));
  });

  it("every hypervisor status resolves to itself, not folded to submitted", () => {
    for (const s of HYPERVISOR_AGENT_STATUS) {
      expect(statusKey(s)).toBe(s);
    }
  });

  it("an unrecognised status folds to submitted (the documented default)", () => {
    expect(statusKey("queued")).toBe("submitted");
    expect(statusKey("bogus_future_status")).toBe("submitted");
  });

  it("rejected is terminal — a permanently refused agent must never read as still working", () => {
    expect(isTerminal("rejected")).toBe(true);
  });

  it("completed/failed/cancelled/rejected are terminal; submitted/running are not", () => {
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("submitted")).toBe(false);
    expect(isTerminal("running")).toBe(false);
  });
});
