/**
 * LogsView — a RUNNING tool call must render its custom card, not raw JSON.
 *
 * `renderShell` dispatches to `<TerminalCard>` on `shellCommand` alone. A row
 * built from a `tool_call` (emitted before dispatch) has no result to parse a
 * command out of, so when it carried only `shellInput` it fell through to the
 * generic `<LogEventCard>` and printed the tool arguments as pretty JSON — the
 * custom component appeared only once the command had already finished.
 *
 * These assert the RENDER, not the parse: `logs.ts` unit tests can confirm the
 * fields are set while the dispatch still picks the wrong component, and a
 * bundle grep proves only that the code compiled.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LogsView } from "../LogsView";
import type { EventRecord } from "../../types";

const COMMAND = "sleep 45 && echo done-sleeping";

function toolCall(): EventRecord {
  return {
    ts: "2026-06-08T00:00:00Z",
    type: "tool_call",
    payload: {
      tool_call_id: "toolu_01",
      tool_id: "aider_shell_tool",
      operation: "set",
      tool_input: { command: COMMAND, root: "/tmp/mewbo/session" },
      agent_id: "root0000",
      depth: 0,
    },
  };
}

function toolResult(): EventRecord {
  return {
    ts: "2026-06-08T00:00:46Z",
    type: "tool_result",
    payload: {
      tool_call_id: "toolu_01",
      tool_id: "aider_shell_tool",
      operation: "set",
      success: true,
      tool_input: { command: COMMAND },
      result: JSON.stringify({
        kind: "shell",
        command: COMMAND,
        exit_code: 0,
        stdout: "done-sleeping",
        duration_ms: 45012,
      }),
    },
  };
}

/** The terminal-window signature: three chrome dots inside a font-mono card. */
function terminalCards(container: HTMLElement): Element[] {
  return Array.from(container.querySelectorAll("div.font-mono")).filter(
    (card) => card.querySelectorAll("span.rounded-full").length >= 3,
  );
}

afterEach(cleanup);

describe("LogsView — a running tool renders its custom component", () => {
  it("renders a TerminalCard for a shell call that has not returned", () => {
    const { container } = render(<LogsView events={[toolCall()]} />);

    expect(terminalCards(container)).toHaveLength(1);
    // The command itself is shown, which is the whole point of the card. Matched
    // on the card's text content rather than a single node: the prompt glyph and
    // the command are separate elements, so a node-level matcher would fail for
    // a reason that has nothing to do with the behaviour under test.
    expect(terminalCards(container)[0].textContent).toContain(COMMAND);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("does NOT print the tool arguments as JSON while the command runs", () => {
    // The exact regression: the generic fallback rendered
    // `prettyJsonIfValid(shellInput)`, so the quoted argument keys appeared.
    const { container } = render(<LogsView events={[toolCall()]} />);

    expect(container.textContent).not.toMatch(/"command"\s*:/);
    expect(container.textContent).not.toMatch(/"root"\s*:/);
  });

  it("claims no outcome it cannot know — no exit code and no duration yet", () => {
    render(<LogsView events={[toolCall()]} />);

    // A settled card shows a duration; a running one must not invent one.
    expect(screen.queryByText(/45\.0s|45012/)).toBeNull();
    expect(screen.queryByText("0")).toBeNull();
  });

  it("settles into ONE finished TerminalCard when the result arrives", () => {
    const { container } = render(
      <LogsView events={[toolCall(), toolResult()]} />,
    );

    // Replaced in place, never a second card for the same call.
    expect(terminalCards(container)).toHaveLength(1);
    // The card is settled: the running claim is gone and the finished readouts
    // (duration, output line count) are present. `stdout` itself lives behind
    // the collapsed body, so it is deliberately NOT asserted here — that would
    // be testing the expand affordance, not the settle.
    expect(screen.queryByText("Running")).toBeNull();
    expect(terminalCards(container)[0].textContent).toContain(COMMAND);
  });

  it("still renders a TerminalCard for a legacy result with no tool_call", () => {
    // The session page replays FULL history, so transcripts written before the
    // initiation event existed must be unaffected.
    const { container } = render(<LogsView events={[toolResult()]} />);

    expect(terminalCards(container)).toHaveLength(1);
    expect(screen.queryByText("Running")).toBeNull();
  });
});
