/**
 * Render coverage for the generative-UI allowlist (`components/generative-ui/`)
 * and the card that mounts it (`components/GenerativeUICard.tsx`).
 *
 * Two properties are worth locking down here, and they are both about what
 * happens when the model gets it WRONG — the happy path is the easy half:
 *
 *  1. **Nothing a model writes can take down the transcript.** An unknown
 *     component name, a malformed node, a prop of the wrong type: each must
 *     degrade to a placeholder or to nothing at all. The vendored renderer
 *     throws `GenerativeUIRenderError` on an unknown name unless a `Fallback`
 *     is supplied, so the fallback wiring is a real regression risk, not a
 *     hypothetical one.
 *  2. **A hostile `href` never reaches the DOM.** Props spread directly onto
 *     the resolved component, so `Link` re-checks the scheme at the render
 *     boundary rather than trusting the emit-side validator.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GenerativeUICard } from "../components/GenerativeUICard";
import { GENERATIVE_UI_COMPONENTS } from "../components/generative-ui/registry";
import { GenerativeUINodeSpec, GenerativeUIPayload } from "../types";

afterEach(cleanup);

function payload(root: GenerativeUINodeSpec[]): GenerativeUIPayload {
  return {
    ui_id: "gui-abc12345",
    session_id: "s1",
    spec: { root },
    alt_text: "plain text degradation",
    summary: "a generated view",
  };
}

/** One minimal, VALID node per allowlisted name, plus the text it must show. */
const VOCABULARY: { node: GenerativeUINodeSpec; expect: string }[] = [
  { node: { component: "Text", props: { value: "hello text" } }, expect: "hello text" },
  { node: { component: "Heading", props: { value: "hello heading", level: 2 } }, expect: "hello heading" },
  {
    node: {
      component: "Card",
      props: { title: "hello card" },
      children: [{ component: "Text", props: { value: "card body" } }],
    },
    expect: "card body",
  },
  {
    node: {
      component: "Stack",
      props: { direction: "horizontal", gap: "sm" },
      children: [{ component: "Text", props: { value: "stacked" } }],
    },
    expect: "stacked",
  },
  { node: { component: "Badge", props: { label: "hello badge", status: "success" } }, expect: "hello badge" },
  {
    node: { component: "KeyValue", props: { items: [{ label: "Region", value: "eu-west" }] } },
    expect: "eu-west",
  },
  {
    node: { component: "Table", props: { columns: ["Name"], rows: [["beacon"]] } },
    expect: "beacon",
  },
  { node: { component: "CodeBlock", props: { code: "print(1)", language: "python" } }, expect: "print(1)" },
  {
    node: { component: "Alert", props: { title: "Heads up", body: "hello alert", variant: "warning" } },
    expect: "hello alert",
  },
  { node: { component: "Link", props: { href: "https://example.com/x", label: "hello link" } }, expect: "hello link" },
];

describe("generative-UI vocabulary", () => {
  it("carries exactly the eleven v1 component names", () => {
    expect(Object.keys(GENERATIVE_UI_COMPONENTS).sort()).toEqual(
      [
        "Alert",
        "Badge",
        "Card",
        "CodeBlock",
        "Divider",
        "Heading",
        "KeyValue",
        "Link",
        "Stack",
        "Table",
        "Text",
      ].sort(),
    );
  });

  it.each(VOCABULARY)("renders $node.component", ({ node, expect: text }) => {
    render(<GenerativeUICard ui={payload([node])} />);
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  // Divider has no text of its own, so it gets its own structural assertion
  // rather than being bent into the text-based table above.
  it("renders Divider as a rule", () => {
    const { container } = render(<GenerativeUICard ui={payload([{ component: "Divider" }])} />);
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("renders every allowlisted name without throwing", () => {
    // The whole vocabulary in ONE tree — catches a name that renders alone but
    // breaks when nested under a container.
    const all: GenerativeUINodeSpec[] = [
      ...VOCABULARY.map((c) => c.node),
      { component: "Divider" },
    ];
    expect(() => render(<GenerativeUICard ui={payload(all)} />)).not.toThrow();
  });

  it("renders the card's accessible label from the summary", () => {
    render(<GenerativeUICard ui={payload([{ component: "Text", props: { value: "x" } }])} />);
    expect(screen.getByRole("group", { name: "a generated view" })).toBeInTheDocument();
  });
});

describe("generative-UI degradation", () => {
  it("routes an unknown component to the fallback instead of throwing", () => {
    const ui = payload([
      { component: "Text", props: { value: "before" } },
      { component: "Carousel", props: { value: "nope" } },
      { component: "Text", props: { value: "after" } },
    ]);
    expect(() => render(<GenerativeUICard ui={ui} />)).not.toThrow();
    expect(screen.getByText("Carousel")).toBeInTheDocument();
    // The siblings still render — one bad name costs one row, not the tree.
    expect(screen.getByText("before")).toBeInTheDocument();
    expect(screen.getByText("after")).toBeInTheDocument();
  });

  it("skips a malformed node and keeps its siblings", () => {
    // The renderer dev-warns on a malformed node; silence it so the suite's
    // output stays readable while still asserting the skip happened.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const ui = payload([
      { component: "Text", props: { value: "kept" } },
      // No `component` key at all — not a node, not a string.
      { props: { value: "dropped" } } as unknown as GenerativeUINodeSpec,
    ]);
    expect(() => render(<GenerativeUICard ui={ui} />)).not.toThrow();
    expect(screen.getByText("kept")).toBeInTheDocument();
    expect(screen.queryByText("dropped")).toBeNull();
    warn.mockRestore();
  });

  it("drops a leaf's props of the wrong type back to defaults", () => {
    const ui = payload([
      { component: "Text", props: { value: 42, tone: "chartreuse" } },
      { component: "Table", props: { columns: "not-an-array", rows: null } },
      { component: "KeyValue", props: { items: [{ label: "", value: "orphan" }] } },
    ]);
    const { container } = render(<GenerativeUICard ui={ui} />);
    // A non-string value is not coerced into a rendered "42".
    expect(screen.queryByText("42")).toBeNull();
    expect(container.querySelector("table")).toBeNull();
    expect(screen.queryByText("orphan")).toBeNull();
  });

  it("pads a ragged table row rather than throwing", () => {
    const ui = payload([
      { component: "Table", props: { columns: ["A", "B"], rows: [["only-one"]] } },
    ]);
    const { container } = render(<GenerativeUICard ui={ui} />);
    expect(screen.getByText("only-one")).toBeInTheDocument();
    expect(container.querySelectorAll("tbody td")).toHaveLength(2);
  });
});

describe("generative-UI link safety", () => {
  it.each([
    ["javascript:alert(1)"],
    ["JaVaScRiPt:alert(1)"],
    ["  javascript:alert(1)  "],
    ["java\nscript:alert(1)"],
    ["data:text/html,<script>alert(1)</script>"],
    ["file:///etc/passwd"],
    ["//evil.example.com/phish"],
    ["/settings?facet=security"],
  ])("neutralizes a hostile href: %s", (href) => {
    const { container } = render(
      <GenerativeUICard ui={payload([{ component: "Link", props: { href, label: "click me" } }])} />,
    );
    // The label survives as inert text; no anchor is produced at all, so
    // there is no href attribute left to sanitize.
    expect(screen.getByText("click me")).toBeInTheDocument();
    expect(container.querySelector("a")).toBeNull();
  });

  it.each([
    ["https://example.com/docs", "https://example.com/docs"],
    ["http://example.com/", "http://example.com/"],
    ["mailto:ops@example.com", "mailto:ops@example.com"],
  ])("keeps a safe href: %s", (href, expected) => {
    const { container } = render(
      <GenerativeUICard ui={payload([{ component: "Link", props: { href, label: "docs" } }])} />,
    );
    const anchor = container.querySelector("a");
    expect(anchor?.getAttribute("href")).toBe(expected);
    expect(anchor?.getAttribute("rel")).toContain("noopener");
  });

  it("never lets a smuggled event handler or raw HTML reach the DOM", () => {
    const ui = payload([
      {
        component: "Text",
        props: {
          value: "<img src=x onerror=alert(1)>",
          onClick: "alert(1)",
          dangerouslySetInnerHTML: { __html: "<b>pwn</b>" },
        },
      },
    ]);
    const { container } = render(<GenerativeUICard ui={ui} />);
    // The angle brackets render as TEXT — an adapter never spreads its props
    // onto a DOM element, so neither the handler nor the HTML survives.
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });
});
