/**
 * GraphOutline — the codebase as a navigable document.
 *
 * A WAI-ARIA tree over {@link OutlineModel}: every row is focusable and
 * announceable, and the whole structure is reachable with the keyboard alone.
 * This is the primary navigation model for the graph, not a fallback for the
 * spatial view — the spatial view is one lens over the same shared state.
 *
 * Three decisions worth keeping:
 *
 * - **Flat treeitems with explicit `aria-level` / `aria-posinset` /
 *   `aria-setsize`**, rather than nested `role="group"` wrappers. Both are valid
 *   ARIA; the flat form is what a virtualised list can keep honest later, and it
 *   keeps depth out of the DOM shape where a stray wrapper could desync it.
 * - **Roving tabindex.** Exactly one row is tabbable, so the tree is ONE tab
 *   stop and a reader is never forced to tab through thousands of rows to reach
 *   the next landmark. Focus is moved imperatively when the model's focus moves.
 * - **The live region announces only what assistive tech would otherwise miss.**
 *   Expansion is already carried by `aria-expanded`, so announcing it again is
 *   noise; a go-to jump moves focus somewhere structurally distant with nothing
 *   to signal that, so THAT is what gets announced.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, CornerUpRight, Dot } from "lucide-react";

import { cn } from "@/lib/utils";
import { KIND_DOT } from "./graphTheme";
import {
  OutlineModel,
  type OutlineCommand,
  type OutlineRelation,
  type OutlineRow,
} from "./outlineModel";

/** Key → command. The contract lives here so the model stays keyboard-agnostic. */
const KEY_COMMAND: Record<string, OutlineCommand> = {
  ArrowDown: "next",
  ArrowUp: "prev",
  ArrowRight: "expand",
  ArrowLeft: "collapse",
  Home: "first",
  End: "last",
};

export interface GraphOutlineProps {
  model: OutlineModel;
  /** Shared with the spatial view so both channels stay in lockstep. */
  expanded: ReadonlySet<string>;
  onExpandedChange: (next: Set<string>) => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}

export function GraphOutline({
  model,
  expanded,
  onExpandedChange,
  selectedId,
  onSelect,
}: GraphOutlineProps) {
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const rowRefs = useRef(new Map<string, HTMLDivElement>());

  const rows = useMemo(() => model.rows(expanded), [model, expanded]);

  // The tabbable row: the model's focus when it is still visible, else the
  // first row. A focused row can vanish when an ancestor collapses, and a tree
  // with no tabbable row silently drops out of the tab order.
  const tabbableId = useMemo(() => {
    if (focusedId && rows.some((r) => r.id === focusedId)) return focusedId;
    return rows[0]?.id ?? null;
  }, [focusedId, rows]);

  useEffect(() => {
    if (!focusedId) return;
    rowRefs.current.get(focusedId)?.focus();
  }, [focusedId, rows]);

  const run = useCallback(
    (command: OutlineCommand) => {
      const next = model.apply(command, { expanded, focusedId: tabbableId });
      if (next.expanded !== expanded) onExpandedChange(new Set(next.expanded));
      setFocusedId(next.focusedId);
    },
    [model, expanded, tabbableId, onExpandedChange],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const command = KEY_COMMAND[e.key];
      if (command) {
        e.preventDefault();
        run(command);
        return;
      }
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (tabbableId) {
          // A synthetic group has no wire node behind it, so nothing in the
          // inspector could ever describe it. Selecting one anyway would
          // highlight a row (`aria-selected`) while the panel says "select a
          // row" — a contradiction caught driving a real browser. `descend`
          // still runs: Enter on a group is "open and step in", not a no-op.
          const row = rows.find((r) => r.id === tabbableId);
          if (row?.nodeKind !== undefined) onSelect(tabbableId);
          run("descend");
        }
      }
    },
    [run, onSelect, tabbableId, rows],
  );

  /** Jump to a node anywhere in the tree: expand its ancestors, focus, select. */
  const revealNode = useCallback(
    (id: string, why: string) => {
      onExpandedChange(model.revealPath(id, expanded));
      setFocusedId(id);
      onSelect(id);
      setAnnouncement(why);
    },
    [model, expanded, onExpandedChange, onSelect],
  );

  // Paired rather than two separate variables so TS narrows both together
  // below — `selected.node` truthy is exactly `selected.id` non-null.
  const selectedNode = selectedId ? model.node(selectedId) : undefined;
  const selected = selectedId && selectedNode ? { id: selectedId, node: selectedNode } : null;
  const breadcrumb = useMemo(
    () =>
      selectedId
        ? // `labelFor` covers both real nodes AND the synthetic group an
          // off-tree selection's ancestor chain can include (see its doc).
          model.ancestors(selectedId).map((id) => model.labelFor(id) ?? "…").filter(Boolean)
        : [],
    [model, selectedId],
  );

  return (
    <div className="flex flex-1 min-h-0">
      <div className="flex flex-col flex-1 min-h-0 border-r border-[hsl(var(--border))]">
        <div className="px-4 py-2 border-b border-[hsl(var(--border))] text-2xs text-[hsl(var(--muted-foreground))]">
          {rows.length} rows shown. Arrow keys to move, right to expand, left to
          collapse, Enter to descend.
        </div>
        <div
          role="tree"
          aria-label="Code outline"
          tabIndex={-1}
          onKeyDown={handleKeyDown}
          className="flex-1 min-h-0 overflow-y-scroll py-1"
        >
          {rows.map((row) => (
            <OutlineRowView
              key={row.id}
              row={row}
              selected={row.id === selectedId}
              tabbable={row.id === tabbableId}
              registerRef={(el) => {
                if (el) rowRefs.current.set(row.id, el);
                else rowRefs.current.delete(row.id);
              }}
              onActivate={() => {
                setFocusedId(row.id);
                // A group organizes; it is never itself inspectable — see
                // the matching guard in handleKeyDown's Enter/Space branch.
                if (row.nodeKind !== undefined) onSelect(row.id);
              }}
              onToggle={() => {
                setFocusedId(row.id);
                run(row.expanded ? "collapse" : "expand");
              }}
            />
          ))}
          {rows.length === 0 && (
            <p className="px-4 py-6 text-sm text-[hsl(var(--muted-foreground))]">
              This project has no indexed graph yet.
            </p>
          )}
        </div>
      </div>

      <aside
        aria-label="Selection details"
        className="w-80 shrink-0 flex flex-col min-h-0 overflow-y-scroll"
      >
        {selected ? (
          <div className="p-4 flex flex-col gap-4">
            <div>
              {breadcrumb.length > 0 && (
                <p className="text-2xs text-[hsl(var(--muted-foreground))] mb-1">
                  {breadcrumb.join(" › ")}
                </p>
              )}
              <h2 className="text-lg text-[hsl(var(--foreground))]">
                {selected.node.data.label}
              </h2>
              <p className="text-xs text-[hsl(var(--muted-foreground))]">
                {selected.node.data.kind}
                {/* Same redundancy as the row's file span: a Folder/root-File
                    node's `file` equals its own label — verified against the
                    live payload. Only worth a line when it says something the
                    heading doesn't. */}
                {selected.node.data.file && selected.node.data.file !== selected.node.data.label
                  ? ` · ${selected.node.data.file}`
                  : ""}
              </p>
            </div>
            <RelationList
              title="Callers"
              emptyLabel="No callers."
              relations={model.callers(selected.id)}
              onGo={(r) =>
                revealNode(r.id, `Moved to ${r.label}, a caller of ${selected.node.data.label}.`)
              }
            />
            <RelationList
              title="Callees"
              emptyLabel="No callees."
              relations={model.callees(selected.id)}
              onGo={(r) =>
                revealNode(r.id, `Moved to ${r.label}, called by ${selected.node.data.label}.`)
              }
            />
          </div>
        ) : (
          <p className="p-4 text-sm text-[hsl(var(--muted-foreground))]">
            Select a row to see what reaches it and what it reaches.
          </p>
        )}
      </aside>

      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
    </div>
  );
}

interface OutlineRowViewProps {
  row: OutlineRow;
  selected: boolean;
  tabbable: boolean;
  registerRef: (el: HTMLDivElement | null) => void;
  onActivate: () => void;
  onToggle: () => void;
}

function OutlineRowView({
  row,
  selected,
  tabbable,
  registerRef,
  onActivate,
  onToggle,
}: OutlineRowViewProps) {
  return (
    <div
      ref={registerRef}
      role="treeitem"
      aria-level={row.level}
      aria-posinset={row.posInSet}
      aria-setsize={row.setSize}
      aria-expanded={row.expandable ? row.expanded : undefined}
      aria-selected={selected}
      tabIndex={tabbable ? 0 : -1}
      onClick={onActivate}
      // Indentation is padding, not nesting — see the flat-treeitem note above.
      style={{ paddingLeft: `${8 + (row.level - 1) * 14}px` }}
      className={cn(
        "flex items-center gap-1.5 pr-3 py-1 text-sm cursor-default select-none",
        "focus:outline-none focus-visible:ring-1 focus-visible:ring-[hsl(var(--primary))]",
        "hover:bg-[hsl(var(--muted))]/40",
        selected && "bg-[hsl(var(--muted))]/60",
      )}
    >
      {row.expandable ? (
        <button
          type="button"
          tabIndex={-1}
          aria-hidden="true"
          onClick={(e) => {
            e.stopPropagation();
            onToggle();
          }}
          className="shrink-0 p-0.5 -m-0.5 text-[hsl(var(--muted-foreground))]"
        >
          <ChevronRight
            className={cn("size-3 transition-transform", row.expanded && "rotate-90")}
          />
        </button>
      ) : (
        <span className="shrink-0 size-3" />
      )}

      {row.nodeKind ? (
        <span
          aria-hidden="true"
          className={cn("shrink-0 size-2 rounded-full", KIND_DOT[row.nodeKind])}
        />
      ) : (
        <Dot aria-hidden="true" className="shrink-0 size-3 text-[hsl(var(--muted-foreground))]" />
      )}

      <span className="truncate text-[hsl(var(--foreground))]">{row.label}</span>
      {/* Only when `file` says something the label doesn't — a symbol under a
          file benefits from seeing which one, but the wire sets `file` equal
          to `label` on Folder and root-level File nodes themselves, and
          rendering both there would double the accessible name (verified
          against the live payload: `{kind:"Folder", label:"apps",
          file:"apps"}`). */}
      {row.file && row.file !== row.label && (
        <span className="truncate text-2xs text-[hsl(var(--muted-foreground))]">
          {row.file}
        </span>
      )}
    </div>
  );
}

interface RelationListProps {
  title: string;
  emptyLabel: string;
  relations: OutlineRelation[];
  onGo: (relation: OutlineRelation) => void;
}

/** A relationship group. Every group declares its own empty state — "no
 *  callers" is information, whereas a missing section is a bug. */
function RelationList({ title, emptyLabel, relations, onGo }: RelationListProps) {
  return (
    <section>
      <h3 className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
        {title} ({relations.length})
      </h3>
      {relations.length === 0 ? (
        <p className="text-xs text-[hsl(var(--muted-foreground))]">{emptyLabel}</p>
      ) : (
        <ul className="flex flex-col">
          {relations.map((r, i) => (
            <li key={`${r.edgeKind}:${r.id}:${i}`}>
              <button
                type="button"
                disabled={!r.resolved}
                onClick={() => onGo(r)}
                className={cn(
                  "w-full flex items-center gap-1.5 px-1 py-1 rounded text-left text-xs",
                  r.resolved
                    ? "hover:bg-[hsl(var(--muted))]/40 text-[hsl(var(--foreground))]"
                    : "text-[hsl(var(--muted-foreground))] cursor-default",
                )}
              >
                {r.resolved && (
                  <CornerUpRight aria-hidden="true" className="shrink-0 size-3" />
                )}
                <span className="truncate flex-1">{r.label}</span>
                <span className="shrink-0 text-2xs text-[hsl(var(--muted-foreground))]">
                  {/* An edge the resolver never grounded is named, not hidden —
                      it is the honest reading of a graph missing cross-file
                      resolution, and silently dropping it would overstate what
                      the index knows. */}
                  {r.resolved ? r.edgeKind : `${r.edgeKind} · unresolved`}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
