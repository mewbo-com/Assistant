/**
 * GraphOutlineScreen — the textual channel of the code graph (`/wiki/outline`).
 *
 * A thin adapter, mirroring how {@link KnowledgeGraph3DScreen} adapts the
 * spatial channel: it fetches the same `?hierarchy=1` payload, builds the shared
 * {@link OutlineModel}, and holds the expansion + selection state both channels
 * are meant to share.
 *
 * **Why this is its own route rather than a panel beside the canvas.** The
 * spatial view currently locks the main thread for tens of seconds on a repo of
 * any size, and an outline mounted next to it inherits that freeze — which would
 * make "independently usable" false in exactly the case it matters. A separate
 * route costs one union member and leaves the renderer untouched. Once the
 * payload is bounded server-side the two channels should converge into one
 * layout over this same state; that convergence is deliberately NOT done here.
 */

import { useMemo, useState } from "react";

import { WikiTopBar } from "./WikiTopBar";
import { useKnowledgeGraph } from "./api/hooks";
import { GraphOutline } from "./GraphOutline";
import { GraphIndex } from "./inspector/GraphIndex";
import { OutlineModel } from "./outlineModel";
import { buildHref, type PlatformId } from "./router";

interface GraphOutlineScreenProps {
  slug?: string;
  platform?: PlatformId;
}

export function GraphOutlineScreen({ slug, platform }: GraphOutlineScreenProps) {
  const query = useKnowledgeGraph(slug ?? null, { hierarchy: true });
  const graph = query.data ?? null;

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(
    OutlineModel.initialExpanded,
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // ONE index feeds the model (and, later, the inspector) — a second pass over
  // ~68k nodes and ~112k edges is exactly the cost this screen exists to avoid.
  const model = useMemo(
    () => (graph ? new OutlineModel(graph, new GraphIndex(graph)) : null),
    [graph],
  );

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <WikiTopBar repo={slug} showBackToAll showSettings />
      <div className="px-4 sm:px-6 py-2 border-b border-[hsl(var(--border))] flex items-center gap-3">
        <h1 className="text-sm text-[hsl(var(--foreground))]">Code outline</h1>
        <span className="opacity-30">|</span>
        <a
          href={buildHref({ kind: "graph", slug, platform })}
          className="text-2xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] underline underline-offset-2"
        >
          Open the spatial view
        </a>
      </div>

      {query.isPending && (
        <p className="p-6 text-sm text-[hsl(var(--muted-foreground))]">
          Loading the outline…
        </p>
      )}
      {query.isError && (
        <p className="p-6 text-sm text-[hsl(var(--muted-foreground))]">
          Outline unavailable. Re-index this project and try again.
        </p>
      )}
      {!query.isPending && !query.isError && model && (
        <GraphOutline
          model={model}
          expanded={expanded}
          onExpandedChange={setExpanded}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      )}
    </div>
  );
}
