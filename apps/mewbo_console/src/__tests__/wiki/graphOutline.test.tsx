/**
 * Component-level coverage for {@link GraphOutline}: the WAI-ARIA tree
 * metadata a screen reader depends on, and the keyboard traversal contract
 * driven through real DOM events (jsdom + user-event) rather than calling
 * ``OutlineModel.apply`` directly — ``outlineModel.test.ts`` covers the
 * command semantics in isolation; this file covers the key→command wiring
 * and the focus side effect the pure model deliberately does not own.
 *
 * Every scenario establishes its starting focus via a real ``user.click`` on
 * the named row rather than a raw ``element.focus()``. The component runs a
 * roving tabindex: only the row named by internal ``focusedId`` state carries
 * ``tabIndex=0``, and commands are computed from that state, not from
 * whatever the DOM happens to report as ``document.activeElement``. A bare
 * ``.focus()`` can move native DOM focus onto a ``tabIndex=-1`` row without
 * moving the component's own notion of "current row" — clicking exercises the
 * same `onActivate` → `setFocusedId` → focus-effect path a real pointer user
 * takes, which is what keeps the two in sync.
 */
import { useState } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { GraphOutline } from "@/components/wiki/GraphOutline";
import type {
  GraphNodeKind,
  KnowledgeGraph,
  KnowledgeGraphEdge,
  KnowledgeGraphNode,
} from "@/components/wiki/api/types";
import { OutlineModel } from "@/components/wiki/outlineModel";

afterEach(cleanup);

function node(
  id: string,
  kind: GraphNodeKind,
  label: string,
  parentId: string | null = null,
  file?: string,
): KnowledgeGraphNode {
  return { data: { id, label, kind, parentId, ...(file ? { file } : {}) } } as KnowledgeGraphNode;
}

function edge(
  id: string,
  source: string,
  target: string,
  kind: KnowledgeGraphEdge["data"]["kind"],
): KnowledgeGraphEdge {
  return { data: { id, source, target, kind } } as KnowledgeGraphEdge;
}

/** Two folders, one file, two symbols with a CALLS edge, and one PARENTLESS
 *  symbol — enough to exercise expand/descend/collapse, the caller/callee
 *  panel, and the off-tree group's breadcrumb, without payload noise. Labels
 *  are picked so alphabetical sibling order ("apps" < "packages") matches
 *  declaration order, so the fixture reads the same as it sorts. */
function makeGraph(): KnowledgeGraph {
  const nodes: KnowledgeGraphNode[] = [
    node("f:apps", "Folder", "apps"),
    node("f:pkg", "Folder", "packages"),
    node("file:a", "File", "a.py", "f:pkg", "packages/a.py"),
    node("sym:alpha", "Function", "alpha", "file:a", "packages/a.py"),
    node("sym:beta", "Function", "beta", "file:a", "packages/a.py"),
    // A real-world instance of a stale-generation duplicate: two live
    // node ids sharing label/kind/file/parentId. The outline must render
    // BOTH rather than silently dedupe — hiding it would mask the backend
    // defect this fixture guards against. Verified against the live payload.
    node("orphan:1", "Class", "_ResponderRuntime", null, "structured_response.py"),
  ];
  const edges: KnowledgeGraphEdge[] = [
    edge("e1", "f:pkg", "file:a", "CONTAINS"),
    edge("e2", "file:a", "sym:alpha", "CONTAINS"),
    edge("e3", "file:a", "sym:beta", "CONTAINS"),
    edge("e4", "sym:alpha", "sym:beta", "CALLS"),
  ];
  return {
    slug: "demo",
    nodes,
    edges,
    stats: { nodeCount: nodes.length, edgeCount: edges.length, kinds: {} },
  } as KnowledgeGraph;
}

/** Wires GraphOutline's controlled props to local state, the way a real
 *  screen would, so a keystroke's effect on `expanded`/`selectedId` is
 *  observable through the SAME re-render the component itself goes through. */
function Harness() {
  const model = new OutlineModel(makeGraph());
  return <ControlledOutline model={model} />;
}

function ControlledOutline({ model }: { model: OutlineModel }) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  return (
    <GraphOutline
      model={model}
      expanded={expanded}
      onExpandedChange={setExpanded}
      selectedId={selectedId}
      onSelect={setSelectedId}
    />
  );
}

describe("GraphOutline — ARIA tree shape", () => {
  it("exposes a role=tree with flat treeitem rows carrying level/posinset/setsize", () => {
    render(<Harness />);
    expect(screen.getByRole("tree", { name: /code outline/i })).toBeInTheDocument();

    const items = screen.getAllByRole("treeitem");
    // Two folders as roots, plus the ONE non-empty group ("Unparented
    // symbols" — the fixture's single orphan). Entities/Memory/External stay
    // absent — confirms an empty group never renders.
    expect(items).toHaveLength(3);
    expect(screen.queryByRole("treeitem", { name: /entities/i })).toBeNull();
    expect(screen.queryByRole("treeitem", { name: /memory notes/i })).toBeNull();
    expect(screen.queryByRole("treeitem", { name: /external references/i })).toBeNull();
    expect(items[0]).toHaveAttribute("aria-level", "1");
    expect(items[0]).toHaveAttribute("aria-posinset", "1");
    expect(items[0]).toHaveAttribute("aria-setsize", "3");
  });

  it("marks exactly one row tabbable (roving tabindex)", () => {
    render(<Harness />);
    const items = screen.getAllByRole("treeitem");
    const tabbable = items.filter((el) => el.getAttribute("tabindex") === "0");
    expect(tabbable).toHaveLength(1);
    expect(items.filter((el) => el.getAttribute("tabindex") === "-1")).toHaveLength(
      items.length - 1,
    );
  });

  it("omits aria-expanded entirely on a leaf, rather than setting it false", () => {
    render(<Harness />);
    // "apps" carries no children in this fixture — a real leaf.
    const leaf = screen.getByRole("treeitem", { name: /apps/i });
    expect(leaf).not.toHaveAttribute("aria-expanded");
  });

  it("sets aria-expanded=false on a collapsed expandable row", () => {
    render(<Harness />);
    const folder = screen.getByRole("treeitem", { name: /^packages$/i });
    expect(folder).toHaveAttribute("aria-expanded", "false");
  });
});

describe("GraphOutline — keyboard traversal", () => {
  it("ArrowDown moves focus to the next row", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /apps/i }));

    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toHaveAttribute("aria-posinset", "2");
    expect(document.activeElement).toHaveAccessibleName(/^packages$/i);
  });

  it("ArrowRight expands a collapsed folder, revealing its children", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const pkg = screen.getByRole("treeitem", { name: /^packages$/i });
    await user.click(pkg);

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("treeitem", { name: /^packages$/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("treeitem", { name: /a\.py/i })).toBeInTheDocument();
  });

  it("ArrowRight on an already-open folder steps into its first child", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /^packages$/i }));
    await user.keyboard("{ArrowRight}"); // open
    await user.keyboard("{ArrowRight}"); // step in

    expect(document.activeElement).toHaveAttribute("aria-level", "2");
    expect(document.activeElement).toHaveAccessibleName(/a\.py/i);
  });

  it("ArrowLeft collapses an open folder without losing focus", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /^packages$/i }));
    await user.keyboard("{ArrowRight}");
    await user.keyboard("{ArrowLeft}");

    const pkg = screen.getByRole("treeitem", { name: /^packages$/i });
    expect(pkg).toHaveAttribute("aria-expanded", "false");
    expect(document.activeElement).toBe(pkg);
    expect(screen.queryByRole("treeitem", { name: /a\.py/i })).toBeNull();
  });

  it("ArrowLeft on a leaf ascends to its parent", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /^packages$/i }));
    await user.keyboard("{ArrowRight}"); // open packages
    await user.keyboard("{ArrowRight}"); // into a.py
    await user.keyboard("{ArrowRight}"); // open a.py
    await user.keyboard("{ArrowRight}"); // into alpha (a leaf)
    expect(document.activeElement).toHaveAttribute("aria-level", "3");

    await user.keyboard("{ArrowLeft}");
    expect(document.activeElement).toHaveAttribute("aria-level", "2"); // back at a.py
    expect(document.activeElement).toHaveAccessibleName(/a\.py/i);
  });

  it("End then Home jump to the last and first row", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /apps/i }));

    await user.keyboard("{End}");
    expect(document.activeElement).toHaveAttribute("aria-posinset", "3");
    expect(document.activeElement).toHaveAccessibleName(/unparented symbols/i);

    await user.keyboard("{Home}");
    expect(document.activeElement).toHaveAttribute("aria-posinset", "1");
    expect(document.activeElement).toHaveAccessibleName(/apps/i);
  });

  it("Enter selects a row and shows its callers/callees in the inspector", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /^packages$/i }));
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}{ArrowRight}");
    expect(document.activeElement).toHaveAccessibleName(/alpha/i);

    await user.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "alpha" })).toBeInTheDocument();
    expect(screen.getByText(/callees \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText(/callers \(0\)/i)).toBeInTheDocument();
    expect(screen.getByText(/no callers/i)).toBeInTheDocument();
  });

  it("selecting an off-tree node breadcrumbs to its group's real name, not an ellipsis", async () => {
    // Regression: `ancestors()` can return a synthetic group id, and looking
    // that up via `model.node()` (real nodes only) resolved to nothing —
    // caught driving a real browser against the live payload, where an
    // "Unparented symbols" member's breadcrumb read as a bare "…".
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /unparented symbols/i }));
    await user.keyboard("{ArrowRight}{ArrowRight}{Enter}");

    expect(screen.getByRole("heading", { name: "_ResponderRuntime" })).toBeInTheDocument();
    expect(screen.getByText("Unparented symbols")).toBeInTheDocument();
    expect(screen.queryByText("…")).toBeNull();
  });

  it("never marks a group row selected — it has nothing inspectable behind it", async () => {
    // Regression: Enter used to select whatever row was focused unconditionally,
    // so a group could carry aria-selected=true while the panel simultaneously
    // showed "Select a row" — a selected-but-empty contradiction, caught the
    // same way as the breadcrumb bug above.
    const user = userEvent.setup();
    render(<Harness />);
    const group = screen.getByRole("treeitem", { name: /unparented symbols/i });
    await user.click(group);
    expect(group).toHaveAttribute("aria-selected", "false");

    await user.keyboard("{Enter}"); // opens the group; nothing to select yet
    expect(group).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText(/select a row/i)).toBeInTheDocument();
  });
});

describe("GraphOutline — go-to-callers / go-to-callees", () => {
  it("jumping to a callee reveals it, expanding ancestors, and selects it", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("treeitem", { name: /^packages$/i }));
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}{ArrowRight}{Enter}");

    await user.click(screen.getByRole("button", { name: /beta/i }));

    // beta becomes the inspected node; its ancestors are now expanded.
    expect(screen.getByRole("heading", { name: "beta" })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /beta/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    // The jump is announced for anyone not watching the panel.
    expect(screen.getByRole("status")).toHaveTextContent(/moved to beta/i);
  });
});
