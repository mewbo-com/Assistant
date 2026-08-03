/**
 * `QaModeControl` — the fast/deep toggle for the Q&A composer. Unit-tests the
 * standalone component (default label, menu contents, selection callback)
 * independent of `QADock`/`useQaConversation`, which have their own coverage
 * for the request-body/persistence wiring (`useQaConversation.test.tsx`).
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QaModeControl } from "@/components/wiki/QaModeControl";

afterEach(cleanup);

describe("QaModeControl", () => {
  it("renders the current mode as the trigger label", () => {
    render(<QaModeControl value="fast" onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Q&A mode" })).toHaveTextContent("Fast");
  });

  it("renders 'Deep' when value is deep", () => {
    render(<QaModeControl value="deep" onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Q&A mode" })).toHaveTextContent("Deep");
  });

  it("opens to reveal both modes and fires onChange on selection", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<QaModeControl value="fast" onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Q&A mode" }));
    expect(screen.getByRole("menuitemradio", { name: /Fast/ })).toBeInTheDocument();
    const deepItem = screen.getByRole("menuitemradio", { name: /Deep/ });
    expect(deepItem).toBeInTheDocument();

    await user.click(deepItem);
    expect(onChange).toHaveBeenCalledWith("deep");
  });
});
