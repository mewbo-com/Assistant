/**
 * `reasonFrom` — extracting the human-readable `reason` out of the API's
 * structured refusal envelope (`{"error": {code, reason, retryable}}`).
 *
 * `readJson`/`readError` fold a body with no top-level `message`/`detail` down
 * to its raw JSON text (see `messageFrom`) — deliberately, since that raw text
 * is what lets a guard like `isSessionTerminatedError` re-parse the original
 * shape. `reasonFrom` is the display-side counterpart: it re-parses that same
 * text back into the plain string a UI actually wants to show.
 */
import { describe, expect, it } from "vitest";

import { reasonFrom } from "@/api/httpBase";

describe("reasonFrom", () => {
  it("extracts the reason out of a structured envelope error", () => {
    // Exact string verified against a live route response.
    const err = new Error(
      JSON.stringify({ error: { code: 400, reason: "Project 'Ghost' not configured.", retryable: false } }),
    );
    expect(reasonFrom(err)).toBe("Project 'Ghost' not configured.");
  });

  it("falls back to the raw message when it is not the envelope shape", () => {
    const err = new Error("plain failure text");
    expect(reasonFrom(err)).toBe("plain failure text");
  });

  it("falls back to the raw message when the envelope has no reason", () => {
    const err = new Error(JSON.stringify({ message: "legacy shape" }));
    expect(reasonFrom(err)).toBe(JSON.stringify({ message: "legacy shape" }));
  });

  it("handles a non-Error rejection", () => {
    expect(reasonFrom("just a string")).toBe("just a string");
  });
});
