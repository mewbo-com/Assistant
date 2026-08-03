/**
 * QuestionCard — the inline ask-user-question form.
 *
 * Three invariants earn tests here. The card must stay ANSWERABLE on every
 * outcome except `answered` (a user who comes back an hour after the run gave
 * up is the whole reason this card exists, and greying it out hides the
 * affordance at exactly that moment). Blank notes must be omitted from the
 * request rather than sent as an empty string, which the server contract
 * forbids. And the confirmation must say where the answer actually landed —
 * "delivered to the waiting run" and "sent as a new message" are different
 * facts and the card is not allowed to blur them.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionCard } from "../QuestionCard";
import type { QuestionMeta, QuestionStatus } from "../../types";

afterEach(cleanup);

const RUN_STOPPED: QuestionStatus[] = [
  "timed_out",
  "declined",
  "interrupted",
  "cancelled",
];

function meta(overrides: Partial<QuestionMeta> = {}): QuestionMeta {
  return {
    callId: "c1",
    callToken: "tok",
    status: "pending",
    questions: [
      {
        header: "Scope",
        question: "How far should I go?",
        options: [
          { label: "This file", description: null },
          { label: "The module", description: "wider" },
        ],
        multi_select: false,
      },
    ],
    ...overrides,
  };
}

describe("QuestionCard — the optional notes box", () => {
  it("renders no notes box when the model asked for none", () => {
    render(<QuestionCard question={meta()} onAnswer={vi.fn()} />);
    expect(screen.queryByLabelText(/Additional notes/i)).toBeNull();
  });

  it("uses the model's string as the placeholder, with a real label", () => {
    render(
      <QuestionCard
        question={meta({ notesPlaceholder: "Anything else I should know?" })}
        onAnswer={vi.fn()}
      />,
    );
    const box = screen.getByLabelText(/Additional notes/i);
    expect(box).toHaveAttribute("placeholder", "Anything else I should know?");
  });

  it("submits with the notes key omitted when the box is left blank", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ ok: true, delivery: "run" });
    const user = userEvent.setup();
    render(
      <QuestionCard question={meta({ notesPlaceholder: "notes?" })} onAnswer={onAnswer} />,
    );
    await user.click(screen.getByText("This file"));
    await user.click(screen.getByText("Submit"));
    await waitFor(() => expect(onAnswer).toHaveBeenCalled());
    // Fourth argument is `notes` — undefined, so the caller omits the key.
    expect(onAnswer).toHaveBeenCalledWith(
      "c1",
      "tok",
      [{ selected_indexes: [0] }],
      undefined,
    );
  });

  it("submits trimmed notes alongside the answers", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ ok: true, delivery: "run" });
    const user = userEvent.setup();
    render(
      <QuestionCard question={meta({ notesPlaceholder: "notes?" })} onAnswer={onAnswer} />,
    );
    await user.click(screen.getByText("The module"));
    await user.type(screen.getByLabelText(/Additional notes/i), "  keep the diff small  ");
    await user.click(screen.getByText("Submit"));
    await waitFor(() => expect(onAnswer).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith(
      "c1",
      "tok",
      [{ selected_indexes: [1] }],
      "keep the diff small",
    );
  });

  it("keeps Submit disabled on notes alone — the questions still need answers", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    render(
      <QuestionCard question={meta({ notesPlaceholder: "notes?" })} onAnswer={onAnswer} />,
    );
    await user.type(screen.getByLabelText(/Additional notes/i), "just a thought");
    expect(screen.getByText("Submit")).toBeDisabled();
  });
});

describe("QuestionCard — the wait hint", () => {
  it("shows a coarse duration when the model bounded the wait", () => {
    render(<QuestionCard question={meta({ timeoutSeconds: 120 })} onAnswer={vi.fn()} />);
    expect(screen.getByText("waits up to 2m")).toBeInTheDocument();
  });

  it("says nothing when the run waits indefinitely", () => {
    render(<QuestionCard question={meta()} onAnswer={vi.fn()} />);
    expect(screen.queryByText(/waits up to/)).toBeNull();
  });

  it("drops the hint once the run stopped waiting — the wait is over", () => {
    render(
      <QuestionCard
        question={meta({ status: "timed_out", timeoutSeconds: 120 })}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.queryByText(/waits up to/)).toBeNull();
  });
});

describe("QuestionCard — stays answerable until actually answered", () => {
  it.each(RUN_STOPPED)("%s keeps the form live with honest copy", async (status) => {
    const onAnswer = vi.fn().mockResolvedValue({ ok: true, delivery: "message" });
    const user = userEvent.setup();
    render(<QuestionCard question={meta({ status })} onAnswer={onAnswer} />);
    // Not a closed record: the badge says the question is still open, the
    // options are still there, and Submit still works.
    expect(screen.getByText("Still open")).toBeInTheDocument();
    await user.click(screen.getByText("This file"));
    await user.click(screen.getByText("Submit"));
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(1));
  });

  it.each(RUN_STOPPED)("%s explains why the run stopped", (status) => {
    render(<QuestionCard question={meta({ status })} onAnswer={vi.fn()} />);
    const expected =
      status === "cancelled"
        ? /session was cancelled .* may no longer accept it/is
        : /You can still answer — it arrives as a new message/;
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("answered is the one outcome that closes the form", () => {
    render(
      <QuestionCard
        question={meta({
          status: "answered",
          answers: [{ selected_indexes: [1] }],
          answeredVia: "console",
          notes: "keep it small",
        })}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByText("Answered")).toBeInTheDocument();
    expect(screen.queryByText("Submit")).toBeNull();
    expect(screen.getByText(/The module/)).toBeInTheDocument();
    expect(screen.getByText(/keep it small/)).toBeInTheDocument();
    expect(screen.getByText(/answered via console/)).toBeInTheDocument();
  });

  it("notes the late arrival on a card answered after the run moved on", () => {
    render(
      <QuestionCard
        question={meta({
          status: "answered",
          answers: [{ selected_indexes: [0] }],
          answeredVia: "console",
          delivery: "message",
        })}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByText(/arrived as a new message/)).toBeInTheDocument();
  });
});

describe("QuestionCard — confirms honestly what happened to the answer", () => {
  async function submitWith(result: unknown, question: QuestionMeta = meta()) {
    const onAnswer = vi.fn().mockResolvedValue(result);
    const user = userEvent.setup();
    render(<QuestionCard question={question} onAnswer={onAnswer} />);
    await user.click(screen.getByText("This file"));
    await user.click(screen.getByText("Submit"));
    await waitFor(() => expect(onAnswer).toHaveBeenCalled());
  }

  it("delivery run — the waiting agent received it", async () => {
    await submitWith({ ok: true, delivery: "run" });
    expect(await screen.findByText(/Delivered to the run that was waiting/)).toBeInTheDocument();
    expect(screen.getByText("Sent")).toBeInTheDocument();
  });

  it("delivery message — the run had moved on", async () => {
    await submitWith({ ok: true, delivery: "message" }, meta({ status: "timed_out" }));
    expect(
      await screen.findByText(/already moved on — sent as a new message/),
    ).toBeInTheDocument();
  });

  it("a 404/409 settles silently as answered elsewhere, never as an error", async () => {
    await submitWith({ ok: false, kind: "superseded", message: "not pending" });
    expect(await screen.findByText(/Already answered elsewhere/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("not pending")).toBeNull();
  });

  it("a 410 says the answer was not delivered — no event is coming", async () => {
    await submitWith({ ok: false, kind: "terminated", message: "session terminated" });
    expect(await screen.findByText(/was not delivered/)).toBeInTheDocument();
    // The badge must not read like an answer still in flight.
    expect(screen.getByText("Not delivered")).toBeInTheDocument();
  });

  it("a 422 is user-actionable: show the message and keep the form open", async () => {
    await submitWith({ ok: false, kind: "invalid", message: "takes exactly one selection" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("takes exactly one selection");
    // Still answerable so the user can fix it and resubmit.
    expect(screen.getByText("Submit")).toBeInTheDocument();
  });
});

describe("QuestionCard — read-only views", () => {
  it("disables Submit and says why when no answer handler is wired", () => {
    render(<QuestionCard question={meta()} />);
    expect(screen.getByText("Submit")).toBeDisabled();
    expect(screen.getByText("Read-only view.")).toBeInTheDocument();
  });
});
