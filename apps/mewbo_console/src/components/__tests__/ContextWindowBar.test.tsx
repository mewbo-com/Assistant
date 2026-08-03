/**
 * ContextWindowBar — an overflowing session must read as overflowing, not
 * as exactly full. `used` used to clamp to `window_`, which pinned the bar
 * (and the percent label) at 100% whether the session was AT the window or
 * well past it — indistinguishable states. See ContextWindowBar.tsx for the
 * full rationale.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ContextWindowBar } from "../ContextWindowBar";
import type { SessionUsage } from "../../types";

afterEach(cleanup);

function makeUsage(overrides: Partial<SessionUsage>): SessionUsage {
  return {
    root_model: "test-model",
    models_used: ["test-model"],
    root_max_input_tokens: 100_000,
    root_last_input_tokens: 50_000,
    root_utilization: 0.5,
    tokens_until_compact: 30_000,
    compact_threshold: 0.8,
    root_peak_input_tokens: 50_000,
    sub_peak_input_tokens: 0,
    root_input_tokens_billed: 50_000,
    sub_input_tokens_billed: 0,
    total_input_tokens_billed: 50_000,
    root_output_tokens: 0,
    sub_output_tokens: 0,
    total_output_tokens: 0,
    root_cache_creation_tokens: 0,
    root_cache_read_tokens: 0,
    root_reasoning_tokens: 0,
    sub_cache_creation_tokens: 0,
    sub_cache_read_tokens: 0,
    sub_reasoning_tokens: 0,
    total_cache_creation_tokens: 0,
    total_cache_read_tokens: 0,
    total_reasoning_tokens: 0,
    root_llm_calls: 1,
    sub_llm_calls: 0,
    sub_agent_count: 0,
    compaction_count: 0,
    compaction_tokens_saved: 0,
    ...overrides,
  };
}

describe("ContextWindowBar overflow", () => {
  it("reads a used token count exceeding the window as overflowing, not exactly full", () => {
    const usage = makeUsage({
      root_max_input_tokens: 100_000,
      root_last_input_tokens: 142_000,
      tokens_until_compact: 0,
    });
    render(<ContextWindowBar usage={usage} />);

    // The clamped-then-broken behavior would have shown "100k/100k" and
    // frozen the percent at 100 — the real figures must survive instead.
    const bar = screen.getByTitle(/142k of 100k — 142% over window/);
    expect(bar).toBeTruthy();
    expect(screen.getByRole("button")).toHaveTextContent("142k/100k (142%)");

    // The fill segment's rendered width still clamps to the box — a bar
    // cannot literally paint past its own edge — but the underlying % used
    // for that clamp is the unclamped figure, not a pre-clamped `used`.
    const fill = bar.querySelector("div");
    expect(fill?.getAttribute("style")).toContain("width: 100%");
  });

  it("an exactly-full session (used == window) still renders as 100%, not overflowing", () => {
    const usage = makeUsage({
      root_max_input_tokens: 100_000,
      root_last_input_tokens: 100_000,
      tokens_until_compact: 0,
    });
    render(<ContextWindowBar usage={usage} />);

    expect(screen.getByTitle(/100k of 100k \(100%\)/)).toBeTruthy();
    expect(screen.getByRole("button")).toHaveTextContent("100k/100k");
    expect(screen.queryByText(/over window/)).toBeNull();
  });

  it("renders nothing when there is no usage or no resolved window", () => {
    const { container: withoutUsage } = render(<ContextWindowBar usage={null} />);
    expect(withoutUsage.firstChild).toBeNull();

    const { container: zeroWindow } = render(
      <ContextWindowBar usage={makeUsage({ root_max_input_tokens: 0 })} />,
    );
    expect(zeroWindow.firstChild).toBeNull();
  });
});
