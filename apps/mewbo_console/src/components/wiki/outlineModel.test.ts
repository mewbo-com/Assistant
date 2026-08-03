/**
 * OutlineModel — the traversal contract, asserted without a DOM.
 *
 * The model is pure by design so every key in the contract is a plain function
 * call here. A test that drove real keystrokes would be asserting jsdom's event
 * plumbing as much as the contract; the component test above it covers the
 * key→command mapping, and this file covers what the commands MEAN.
 */

import { describe, expect, it } from "vitest";

import type {
  GraphNodeKind,
  KnowledgeGraph,
  KnowledgeGraphEdge,
  KnowledgeGraphNode,
} from "./api/types";
import { OutlineModel, type OutlineRow } from "./outlineModel";

/** `Array.find` narrows to `T | undefined`; a `!` fights the lint rule that
 *  bans non-null assertions, so a throwing lookup is the sanctioned escape —
 *  it fails LOUDLY (with the id) rather than silently coercing `undefined`. */
function requireRow(rows: OutlineRow[], id: string): OutlineRow {
  const row = rows.find((r) => r.id === id);
  if (!row) throw new Error(`expected a row for ${id}`);
  return row;
}

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

/**
 * A miniature of the live payload's actual shape: two folder roots, a nested
 * folder, files with symbols, plus one of every off-tree class the real graph
 * carries (external / entity / memory / a parentless symbol).
 */
function makeGraph(): KnowledgeGraph {
  const nodes: KnowledgeGraphNode[] = [
    node("f:pkg", "Folder", "packages"),
    node("f:apps", "Folder", "apps"),
    node("f:src", "Folder", "src", "f:pkg"),
    node("file:a", "File", "a.py", "f:src", "packages/src/a.py"),
    node("file:b", "File", "b.py", "f:src", "packages/src/b.py"),
    node("sym:zeta", "Function", "zeta", "file:a", "packages/src/a.py"),
    node("sym:alpha", "Function", "alpha", "file:a", "packages/src/a.py"),
    node("sym:Klass", "Class", "Klass", "file:a", "packages/src/a.py"),
    node("root.py", "File", "root.py", null, "root.py"),
    // Off-tree classes — each must land in a named group, never be dropped.
    node("ext:requests", "External", "requests"),
    node("ent:auth", "Entity", "Authentication"),
    node("mem:note", "Memory", "A note"),
    node("orphan:1", "Method", "strayMethod"),
  ];
  const edges: KnowledgeGraphEdge[] = [
    edge("e1", "f:pkg", "f:src", "CONTAINS"),
    edge("e2", "file:a", "sym:alpha", "CONTAINS"),
    edge("e3", "sym:alpha", "sym:zeta", "CALLS"),
    edge("e4", "sym:Klass", "sym:alpha", "CALLS"),
    edge("e5", "sym:alpha", "ext:missing", "IMPORTS"),
  ];
  return { slug: "demo", nodes, edges, stats: { nodeCount: nodes.length, edgeCount: edges.length, kinds: {} } } as KnowledgeGraph;
}

const model = () => new OutlineModel(makeGraph());
const ids = (rows: { id: string }[]) => rows.map((r) => r.id);

describe("OutlineModel — the root document", () => {
  it("roots on real containment and does NOT open with every parentless node", () => {
    const rows = model().rows(OutlineModel.initialExpanded());
    // 3 containment roots (apps, packages, root.py) + 4 named groups.
    expect(rows).toHaveLength(7);
    expect(ids(rows).filter((id) => id.startsWith("outline:group:"))).toHaveLength(4);
  });

  it("names every off-tree class instead of dropping it", () => {
    const labels = model()
      .rows(OutlineModel.initialExpanded())
      .filter((r) => r.nodeKind === undefined)
      .map((r) => r.label);
    expect(labels).toEqual([
      "Entities (1)",
      "Memory notes (1)",
      "Unparented symbols (1)",
      "External references (1)",
    ]);
  });

  it("omits a group with no members rather than rendering an empty one", () => {
    const g: KnowledgeGraph = {
      slug: "d",
      nodes: [node("f:only", "Folder", "only")],
      edges: [],
      stats: { nodeCount: 1, edgeCount: 0, kinds: {} },
    } as KnowledgeGraph;
    expect(new OutlineModel(g).rows(new Set())).toHaveLength(1);
  });

  it("orders siblings containers-first then alphabetically", () => {
    const m = model();
    const expanded = new Set(["f:pkg", "f:src", "file:a"]);
    const under = m.rows(expanded).filter((r) => r.parentId === "file:a");
    // Class outranks Function; the two Functions sort alpha, so zeta is last
    // despite being declared first in the payload.
    expect(under.map((r) => r.label)).toEqual(["Klass", "alpha", "zeta"]);
  });
});

describe("OutlineModel — ARIA metadata", () => {
  it("carries level, posInSet and setSize for every row", () => {
    const m = model();
    const rows = m.rows(new Set(["f:pkg"]));
    const pkg = requireRow(rows, "f:pkg");
    const src = requireRow(rows, "f:src");
    expect(pkg.level).toBe(1);
    expect(pkg.setSize).toBe(7);
    expect(src.level).toBe(2);
    expect(src.posInSet).toBe(1);
    expect(src.setSize).toBe(1);
  });

  it("marks a childless node unexpandable so the view renders no twisty", () => {
    const rows = model().rows(new Set(["f:pkg", "f:src", "file:a"]));
    expect(requireRow(rows, "sym:zeta").expandable).toBe(false);
    expect(requireRow(rows, "file:a").expandable).toBe(true);
  });
});

describe("OutlineModel — traversal contract", () => {
  const start = { expanded: new Set<string>(), focusedId: null };

  it("lands on the first row when focus is absent or unknown", () => {
    expect(model().apply("next", start).focusedId).toBe("f:apps");
    expect(
      model().apply("next", { expanded: new Set(), focusedId: "gone" }).focusedId,
    ).toBe("f:apps");
  });

  it("next/prev walk the flattened visible rows and clamp at the ends", () => {
    const m = model();
    const s1 = m.apply("next", start);
    const s2 = m.apply("next", s1);
    expect(s2.focusedId).toBe("f:pkg");
    expect(m.apply("prev", m.apply("prev", s2)).focusedId).toBe("f:apps");
    // Clamped, not wrapped — wrapping loses a reader's place.
    expect(m.apply("prev", { expanded: new Set(), focusedId: "f:apps" }).focusedId).toBe(
      "f:apps",
    );
  });

  it("first/last jump to the ends", () => {
    const m = model();
    expect(
      m.apply("last", { expanded: new Set(), focusedId: "f:apps" }).focusedId,
    ).toBe("outline:group:external");
    expect(
      m.apply("first", { expanded: new Set(), focusedId: "root.py" }).focusedId,
    ).toBe("f:apps");
  });

  it("first/last name an absolute position, so they work as the FIRST keystroke", () => {
    // Regression: these once resolved after the unknown-focus fallback, so End
    // on a freshly-focused tree landed on the first row.
    const m = model();
    expect(m.apply("last", start).focusedId).toBe("outline:group:external");
    expect(m.apply("first", start).focusedId).toBe("f:apps");
  });

  it("expand opens a closed parent, then steps into an open one", () => {
    const m = model();
    const opened = m.apply("expand", { expanded: new Set(), focusedId: "f:pkg" });
    expect(opened.expanded.has("f:pkg")).toBe(true);
    expect(opened.focusedId).toBe("f:pkg");

    const stepped = m.apply("expand", opened);
    expect(stepped.focusedId).toBe("f:src");
    expect(stepped.expanded.has("f:src")).toBe(false);
  });

  it("expand is inert on a leaf", () => {
    const m = model();
    const s = { expanded: new Set(["f:pkg", "f:src", "file:a"]), focusedId: "sym:zeta" };
    expect(m.apply("expand", s)).toBe(s);
  });

  it("collapse closes an open parent, then ascends from a closed one", () => {
    const m = model();
    const closed = m.apply("collapse", {
      expanded: new Set(["f:pkg"]),
      focusedId: "f:pkg",
    });
    expect(closed.expanded.has("f:pkg")).toBe(false);
    expect(closed.focusedId).toBe("f:pkg");

    const ascended = m.apply("collapse", {
      expanded: new Set(["f:pkg"]),
      focusedId: "f:src",
    });
    expect(ascended.focusedId).toBe("f:pkg");
  });

  it("collapse from a root stays put rather than stranding focus", () => {
    const m = model();
    expect(
      m.apply("collapse", { expanded: new Set(), focusedId: "f:apps" }).focusedId,
    ).toBe("f:apps");
  });

  it("descend opens and steps in with one stroke", () => {
    const m = model();
    const s = m.apply("descend", { expanded: new Set(), focusedId: "f:pkg" });
    expect(s.expanded.has("f:pkg")).toBe(true);
    expect(s.focusedId).toBe("f:src");
  });

  it("descend into a group walks its members", () => {
    const m = model();
    const s = m.apply("descend", {
      expanded: new Set(),
      focusedId: "outline:group:entities",
    });
    expect(s.focusedId).toBe("ent:auth");
  });
});

describe("OutlineModel — relationships", () => {
  it("splits callers from callees and excludes the containment tree", () => {
    const m = model();
    expect(m.callers("sym:alpha").map((r) => r.id)).toEqual(["sym:Klass"]);
    expect(m.callees("sym:alpha").map((r) => r.id).sort()).toEqual([
      "ext:missing",
      "sym:zeta",
    ]);
    // CONTAINS is the tree itself; repeating it as a relationship would be noise.
    expect(m.callers("sym:alpha").some((r) => r.edgeKind === "CONTAINS")).toBe(false);
  });

  it("keeps an unresolved edge visible and flags it rather than hiding it", () => {
    const missing = model()
      .callees("sym:alpha")
      .find((r) => r.id === "ext:missing");
    expect(missing).toBeDefined();
    expect(missing?.resolved).toBe(false);
    expect(missing?.edgeKind).toBe("IMPORTS");
  });

  it("reports no relationships as an empty list, not an error", () => {
    expect(model().callers("root.py")).toEqual([]);
  });
});

describe("OutlineModel — reveal", () => {
  it("expands every ancestor so a deep node becomes visible", () => {
    const m = model();
    const expanded = m.revealPath("sym:alpha", new Set());
    expect(expanded).toEqual(new Set(["f:pkg", "f:src", "file:a"]));
    expect(ids(m.rows(expanded))).toContain("sym:alpha");
  });

  it("routes an off-tree node to its group instead of returning nothing", () => {
    const m = model();
    const expanded = m.revealPath("orphan:1", new Set());
    expect(expanded).toEqual(new Set(["outline:group:unparented"]));
    expect(ids(m.rows(expanded))).toContain("orphan:1");
  });
});

describe("OutlineModel — labelFor (breadcrumb support)", () => {
  it("resolves a real node's label", () => {
    expect(model().labelFor("sym:alpha")).toBe("alpha");
  });

  it("resolves a synthetic group's bare name, without the member count", () => {
    // A group's ROW label carries "(N)" (see the root-document tests above);
    // labelFor is for a breadcrumb segment, which must not repeat that count.
    expect(model().labelFor("outline:group:unparented")).toBe("Unparented symbols");
  });

  it("returns undefined for an id that is neither a node nor a group", () => {
    expect(model().labelFor("nothing:here")).toBeUndefined();
  });
});
