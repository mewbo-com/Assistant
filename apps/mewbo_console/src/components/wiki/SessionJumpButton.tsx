/**
 * The jump from a wiki surface INTO the Mewbo session doing its work — the
 * inverse of `SessionHeader`'s artifact jump, and the one component both wiki
 * surfaces that have a backing session use (the indexing screen's "Watch the
 * indexing session", the Q&A screen's "Watch the answering session").
 *
 * It exists as a component rather than twice inline because the two surfaces
 * differ only in wording: the vocabulary (ghost `Button` + `ExternalLink`) is
 * fixed, and is the same one `apps/BuildProgress`'s "Watch the builder
 * session" and agentic search's "Open agent session" already spend on this
 * question — a fourth idiom for it would teach a fourth thing.
 *
 * Render it ONLY when a session id is actually in hand, never
 * disabled-with-tooltip: a graph-only index is deliberately sessionless, a
 * just-queued job has no session attached for a beat, and an answer persisted
 * before the binding was surfaced carries none — in every one of those cases
 * absence means "nothing to watch", and a dead control says something else.
 */
import { ExternalLink } from "lucide-react";
import { useLocation } from "wouter";

import { Button } from "@/components/ui/button";

interface SessionJumpButtonProps {
  sessionId: string;
  /** Visible text, also the accessible name. Says what the session IS doing. */
  label: string;
  /** Hover title — the longer form of the same promise. */
  title: string;
}

export function SessionJumpButton({ sessionId, label, title }: SessionJumpButtonProps) {
  const [, navigate] = useLocation();
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      onClick={() => navigate(`/s/${encodeURIComponent(sessionId)}`)}
      leadingIcon={<ExternalLink className="h-3.5 w-3.5" />}
      aria-label={label}
      title={title}
    >
      {label}
    </Button>
  );
}
