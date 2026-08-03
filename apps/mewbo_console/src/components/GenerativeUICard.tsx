import { GenerativeUIRender } from "@assistant-ui/react";

import { GenerativeUIPayload } from "../types";
import { GenerativeUIFallbackNode } from "./generative-ui/nodes";
import { GENERATIVE_UI_COMPONENTS } from "./generative-ui/registry";

/**
 * Inline container for a `generative_ui` timeline entry — a UI tree the model
 * authored, walked by assistant-ui's `GenerativeUIRender` against our
 * allowlist (`generative-ui/registry.ts`).
 *
 * `GenerativeUIRender` reads ZERO context: it takes the spec, the registry and
 * the fallback as plain props. That is what lets a generated card render
 * inside the console's own bespoke timeline without an assistant-ui Thread or
 * runtime anywhere above it.
 *
 * `Fallback` is mandatory here, not a nicety — the renderer throws
 * `GenerativeUIRenderError` on an unknown component name when none is passed,
 * and a throw at this depth takes the whole transcript down with it.
 *
 * `alt_text` is the server-precomputed plain-text degradation the non-visual
 * surfaces render. The console renders the real tree, so it spends `alt_text`
 * as the group's accessible label — one string that describes the card without
 * a screen reader having to reconstruct it from the nodes.
 */
export function GenerativeUICard({ ui }: { ui: GenerativeUIPayload }) {
  return (
    <div
      role="group"
      aria-label={ui.summary || ui.alt_text || "Generated view"}
      className="flex flex-col gap-2"
    >
      <GenerativeUIRender
        spec={ui.spec}
        components={GENERATIVE_UI_COMPONENTS}
        Fallback={GenerativeUIFallbackNode}
      />
    </div>
  );
}
