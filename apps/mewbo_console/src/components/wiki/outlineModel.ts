/**
 * OutlineModel — the navigable document of a codebase.
 *
 * The spatial view is one lens over a graph; this is the other, and it is the
 * primary one: code is text, symbols have names, and "who calls this" is a list
 * before it is ever a picture. The model is PURE — no React, no fetch, no clock
 * — so the whole traversal contract is unit-testable without a DOM.
 *
 * Two things it deliberately does NOT do:
 *
 * 1. **It does not mirror the canvas's root set.** ~12k payload nodes carry a
 *    null ``parentId`` (synthesized externals, the entity and memory layers, and
 *    symbols the extractor never parented). Rooting the tree on "every parentless
 *    node" would open the document with twelve thousand siblings — the spatial
 *    view's own defect, rewritten as text. The tree roots on real containment
 *    (folders and root files) and every off-tree class is collected into a NAMED
 *    group instead. A group is never silently dropped: ``Unparented symbols`` is
 *    an extraction defect made legible, and it should shrink as the extractor
 *    improves rather than being hidden here.
 * 2. **It does not own focus.** ``apply()`` maps a command plus the current
 *    state onto the next state; which DOM node gets ``.focus()`` is the view's
 *    problem. That split is what lets every key in the traversal contract be
 *    asserted in a plain unit test.
 *
 * The ``expanded`` set is held by the CALLER precisely so the outline and the
 * spatial view can share one — selection and expansion living in one place is
 * what keeps the two channels in lockstep and lets either drive.
 */

import type {
  GraphLayer,
  GraphNodeKind,
  KnowledgeGraph,
  KnowledgeGraphEdge,
} from "./api/types";
import { GraphIndex, type InspectorNode } from "./inspector/GraphIndex";

/** A synthetic root group — an off-tree node class, named rather than dropped. */
export interface OutlineGroup {
  id: string;
  label: string;
  /** Member node ids, in render order. */
  members: string[];
}

/** One rendered line of the outline, carrying everything ARIA needs. */
export interface OutlineRow {
  id: string;
  label: string;
  /**
   * The graph kind, or ABSENT for a synthetic group row. Absence is the
   * discriminator — it keeps the closed ``GraphNodeKind`` union un-polluted by a
   * presentation-only "Group" member that no wire payload can ever carry.
   */
  nodeKind?: GraphNodeKind;
  layer?: GraphLayer;
  /** Source path, for the secondary line on a symbol row. */
  file?: string;
  /** 1-based, feeds ``aria-level``. */
  level: number;
  expandable: boolean;
  expanded: boolean;
  /** 1-based, feeds ``aria-posinset``. */
  posInSet: number;
  /** Feeds ``aria-setsize``. */
  setSize: number;
  parentId: string | null;
}

/** A relationship reachable from the focused node, for the go-to commands. */
export interface OutlineRelation {
  /** The node at the other end. */
  id: string;
  label: string;
  nodeKind?: GraphNodeKind;
  file?: string;
  /** Edge kind, so the row can say HOW the two are related. */
  edgeKind: KnowledgeGraphEdge["data"]["kind"];
  /** False when the edge points at something absent from the payload. */
  resolved: boolean;
}

/** The traversal contract, as commands rather than key names — the view maps
 *  keys onto these so the contract can be asserted without a keyboard. */
export type OutlineCommand =
  | "next"
  | "prev"
  | "expand"
  | "collapse"
  | "descend"
  | "first"
  | "last";

export interface OutlineState {
  expanded: ReadonlySet<string>;
  focusedId: string | null;
}

/** Group ids are prefixed so they can never collide with a real node id. */
const GROUP_PREFIX = "outline:group:";

/**
 * Render order for siblings: containers before contents, then alphabetical.
 * A tree in Map-insertion order is not navigable — a reader scanning for a
 * folder must not have to read every function to find it.
 */
const KIND_RANK: Record<GraphNodeKind, number> = {
  Folder: 0,
  File: 1,
  Module: 2,
  Class: 3,
  Interface: 4,
  Object: 5,
  Method: 6,
  Function: 7,
  Property: 8,
  External: 9,
  Entity: 10,
  Memory: 11,
};

export class OutlineModel {
  // ── State ────────────────────────────────────────────────────────────
  private readonly index: GraphIndex;
  /** Containment roots: real folders and root-level files, in render order. */
  private readonly treeRoots: string[];
  /** Off-tree node classes, each named. Empty groups are never rendered. */
  private readonly groups: OutlineGroup[];
  private readonly groupById = new Map<string, OutlineGroup>();
  /** Group id per member, so ``revealPath`` can route an off-tree node home. */
  private readonly groupOfMember = new Map<string, string>();

  // ── Construction ─────────────────────────────────────────────────────

  /** ``index`` is injected so the screen can share ONE index with the
   *  inspector rather than paying for a second O(N+E) pass. */
  constructor(graph: KnowledgeGraph, index?: GraphIndex) {
    this.index = index ?? new GraphIndex(graph);

    const roots: InspectorNode[] = [];
    const externals: InspectorNode[] = [];
    const entities: InspectorNode[] = [];
    const memory: InspectorNode[] = [];
    const orphans: InspectorNode[] = [];

    for (const n of graph.nodes) {
      // A truthy check excludes both null and undefined, matching how the
      // wire encodes "this node has no container".
      if (n.data.parentId) continue;
      switch (n.data.kind) {
        case "Folder":
        case "File":
          roots.push(n);
          break;
        case "External":
          externals.push(n);
          break;
        case "Entity":
          entities.push(n);
          break;
        case "Memory":
          memory.push(n);
          break;
        default:
          // A Class/Function/Method with no container is an extraction gap,
          // not a root. Named, not hidden — see the module docstring.
          orphans.push(n);
      }
    }

    this.treeRoots = OutlineModel.sortNodes(roots).map((n) => n.data.id);
    this.groups = [
      OutlineModel.makeGroup("entities", "Entities", entities),
      OutlineModel.makeGroup("memory", "Memory notes", memory),
      OutlineModel.makeGroup("unparented", "Unparented symbols", orphans),
      OutlineModel.makeGroup("external", "External references", externals),
    ].filter((g): g is OutlineGroup => g !== null);

    for (const g of this.groups) {
      this.groupById.set(g.id, g);
      for (const m of g.members) this.groupOfMember.set(m, g.id);
    }
  }

  // ── Queries ──────────────────────────────────────────────────────────

  /** Every row currently visible, depth-first, in render order. */
  rows(expanded: ReadonlySet<string>): OutlineRow[] {
    const out: OutlineRow[] = [];
    const setSize = this.treeRoots.length + this.groups.length;
    let pos = 0;

    for (const id of this.treeRoots) {
      pos += 1;
      this.pushNode(out, id, 1, pos, setSize, null, expanded);
    }
    for (const group of this.groups) {
      pos += 1;
      const isOpen = expanded.has(group.id);
      out.push({
        id: group.id,
        label: `${group.label} (${group.members.length})`,
        level: 1,
        expandable: group.members.length > 0,
        expanded: isOpen,
        posInSet: pos,
        setSize,
        parentId: null,
      });
      if (!isOpen) continue;
      group.members.forEach((memberId, i) => {
        this.pushNode(out, memberId, 2, i + 1, group.members.length, group.id, expanded);
      });
    }
    return out;
  }

  /** Children of a node in render order. A group's members are its children. */
  childIds(id: string): string[] {
    const group = this.groupById.get(id);
    if (group) return group.members;
    return OutlineModel.sortNodes(this.index.children(id)).map((n) => n.data.id);
  }

  isExpandable(id: string): boolean {
    return this.childIds(id).length > 0;
  }

  node(id: string): InspectorNode | undefined {
    return this.index.node(id);
  }

  /** The row label for any id `rows()` can produce — a real node's label, or
   *  a synthetic group's name (bare, without its member count). `ancestors()`
   *  can return a group id when the id is an off-tree member, and a caller
   *  building a breadcrumb needs a name for EVERY entry in that chain, not
   *  just the ones backed by a wire node. */
  labelFor(id: string): string | undefined {
    return this.index.node(id)?.data.label ?? this.groupById.get(id)?.label;
  }

  /** Ancestor ids from the root down to (but excluding) ``id``. */
  ancestors(id: string): string[] {
    const group = this.groupOfMember.get(id);
    if (group) return [group];
    const chain: string[] = [];
    let parent = this.index.node(id)?.data.parentId ?? null;
    // A malformed payload could in principle cycle; the visited set bounds it.
    const seen = new Set<string>([id]);
    while (parent && !seen.has(parent)) {
      chain.unshift(parent);
      seen.add(parent);
      parent = this.index.node(parent)?.data.parentId ?? null;
    }
    return chain;
  }

  /** The expanded set needed to make ``id`` visible, merged onto ``expanded``. */
  revealPath(id: string, expanded: ReadonlySet<string>): Set<string> {
    const next = new Set(expanded);
    for (const a of this.ancestors(id)) next.add(a);
    return next;
  }

  /** Incoming non-containment relationships — "who reaches this". */
  callers(id: string): OutlineRelation[] {
    return this.index
      .edgesOf(id)
      .incoming.filter((e) => e.edge.data.kind !== "CONTAINS")
      .map((e) => OutlineModel.toRelation(e.otherId, e.other, e.edge));
  }

  /** Outgoing non-containment relationships — "what this reaches". */
  callees(id: string): OutlineRelation[] {
    return this.index
      .edgesOf(id)
      .outgoing.filter((e) => e.edge.data.kind !== "CONTAINS")
      .map((e) => OutlineModel.toRelation(e.otherId, e.other, e.edge));
  }

  // ── The traversal contract ───────────────────────────────────────────

  /**
   * Map a command onto the next state. Pure: same inputs, same output, no
   * focus side effect. Semantics follow the WAI-ARIA tree pattern, which is
   * what a screen reader user already has in their fingers:
   *
   * - ``expand`` on a collapsed parent opens it; on an OPEN parent it steps to
   *   the first child. That second arm is what makes a right-arrow feel like
   *   "go deeper" rather than dead-ending once a node is already open.
   * - ``collapse`` on an open parent closes it; otherwise it ascends. So the
   *   same key both closes and walks out, which is how the pattern keeps a
   *   reader from getting stranded at depth.
   * - ``descend`` opens a parent AND steps into it in one stroke; on a leaf it
   *   is a no-op for state, leaving activation to the caller.
   */
  apply(command: OutlineCommand, state: OutlineState): OutlineState {
    const rows = this.rows(state.expanded);
    if (rows.length === 0) return state;

    // first/last name an absolute position, so they resolve BEFORE the
    // unknown-focus fallback — otherwise End as the very first keystroke would
    // land on the first row, which is the opposite of what it says.
    if (command === "first") return { ...state, focusedId: rows[0].id };
    if (command === "last") return { ...state, focusedId: rows[rows.length - 1].id };

    const at = state.focusedId
      ? rows.findIndex((r) => r.id === state.focusedId)
      : -1;
    // An unknown or absent focus lands on the first row, so every relative
    // command is usable as the first keystroke after the tree gains focus.
    if (at < 0) return { ...state, focusedId: rows[0].id };
    const row = rows[at];

    switch (command) {
      case "next":
        return { ...state, focusedId: rows[Math.min(at + 1, rows.length - 1)].id };
      case "prev":
        return { ...state, focusedId: rows[Math.max(at - 1, 0)].id };
      case "expand": {
        if (!row.expandable) return state;
        if (!row.expanded) {
          const expanded = new Set(state.expanded);
          expanded.add(row.id);
          return { expanded, focusedId: row.id };
        }
        return { ...state, focusedId: this.childIds(row.id)[0] ?? row.id };
      }
      case "collapse": {
        if (row.expandable && row.expanded) {
          const expanded = new Set(state.expanded);
          expanded.delete(row.id);
          return { expanded, focusedId: row.id };
        }
        return { ...state, focusedId: row.parentId ?? row.id };
      }
      case "descend": {
        if (!row.expandable) return state;
        const expanded = new Set(state.expanded);
        expanded.add(row.id);
        return { expanded, focusedId: this.childIds(row.id)[0] ?? row.id };
      }
    }
  }

  // ── Static helpers ───────────────────────────────────────────────────

  /** The expansion a reader should land on: the containment roots, closed.
   *  Nothing is pre-expanded — the root IS the summary, and 16 lines that name
   *  what a repository is made of beat a thousand that bury it. */
  static initialExpanded(): Set<string> {
    return new Set<string>();
  }

  private static makeGroup(
    key: string,
    label: string,
    nodes: InspectorNode[],
  ): OutlineGroup | null {
    if (nodes.length === 0) return null;
    return {
      id: `${GROUP_PREFIX}${key}`,
      label,
      members: OutlineModel.sortNodes(nodes).map((n) => n.data.id),
    };
  }

  private static sortNodes(nodes: InspectorNode[]): InspectorNode[] {
    return [...nodes].sort((a, b) => {
      const rank = KIND_RANK[a.data.kind] - KIND_RANK[b.data.kind];
      if (rank !== 0) return rank;
      return a.data.label.localeCompare(b.data.label);
    });
  }

  private static toRelation(
    otherId: string,
    other: InspectorNode | undefined,
    edge: KnowledgeGraphEdge,
  ): OutlineRelation {
    return {
      id: otherId,
      // A cross-file edge the resolver never grounded carries a name but no
      // node. Showing the name beats showing an opaque id.
      label: other?.data.label ?? edge.data.label ?? otherId,
      nodeKind: other?.data.kind,
      file: other?.data.file,
      edgeKind: edge.data.kind,
      resolved: other !== undefined,
    };
  }

  // ── Private ──────────────────────────────────────────────────────────

  private pushNode(
    out: OutlineRow[],
    id: string,
    level: number,
    posInSet: number,
    setSize: number,
    parentId: string | null,
    expanded: ReadonlySet<string>,
  ): void {
    const node = this.index.node(id);
    if (!node) return;
    const children = this.childIds(id);
    const isOpen = expanded.has(id);
    out.push({
      id,
      label: node.data.label,
      nodeKind: node.data.kind,
      layer: node.data.layer,
      file: node.data.file,
      level,
      expandable: children.length > 0,
      expanded: isOpen,
      posInSet,
      setSize,
      parentId,
    });
    if (!isOpen) return;
    children.forEach((childId, i) => {
      this.pushNode(out, childId, level + 1, i + 1, children.length, id, expanded);
    });
  }
}
