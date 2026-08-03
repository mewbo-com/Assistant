import { useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { toast } from "sonner";

import { getErrorMessage } from "../utils/errors";

/**
 * Shared "open a session about this thing" mutation. Both the wiki project
 * landing screen and the app detail header need the same shape: mint-or-reuse
 * a session for the target on screen, then jump into it — the wiki and apps
 * endpoints behind ``open`` are both get-or-create, so this is always "open",
 * never "new" (calling it twice returns to the SAME conversation).
 *
 * ``open`` resolves straight to the session id rather than either backend's
 * own wire shape (``{sessionId}`` for wiki, ``{session_id}`` for apps) so the
 * hook stays agnostic to that difference — each call site does the one-line
 * `.then(r => r.sessionId)` normalisation itself.
 *
 * A caller mounts one instance per target (one per gallery card, one per
 * detail header) for independent pending state, the same convention
 * ``PipelineRow``'s per-row mutations use.
 *
 * **``onSuccess``/``onError`` are bound at the ``mutate``/``mutateAsync`` CALL
 * site, not in the ``useMutation`` options passed here — that placement is
 * load-bearing, not a style choice.** ``query-core``'s ``Mutation.execute()``
 * invokes hook-level callbacks unconditionally from the mutation's own promise
 * chain; call-level callbacks instead run through ``MutationObserver#notify``,
 * which gates them on ``hasListeners()`` — false once the owning component has
 * unmounted (`useMutation`'s `useSyncExternalStore` subscription is what
 * keeps that count). Hook-level callbacks would mean a slow mint that
 * resolves AFTER the user has navigated away by some other means (a different
 * card's own open action, the nav rail, anything) still fires ``navigate()``
 * against wherever the user is now — yanking them to a session they've
 * already left the context of. Binding at the call site makes an unmounted
 * caller's success/failure a no-op instead. See
 * ``useOpenTargetSession.test.tsx``'s unmount case for the regression this
 * closes.
 *
 * **A rapid double-click is guarded by a synchronous ref, not by
 * ``isPending``.** Both call sites disable their button on
 * ``openSession.isPending`` for the visible affordance, but that value only
 * updates once React re-renders off the mutation observer's notification —
 * and ``query-core`` schedules that notification via ``setTimeout(0)``
 * (`notifyManager`'s default scheduler), a MACROTASK. Two ``mutate()`` calls
 * with no yield between them (a fast double-click, or two ``fireEvent.click``s
 * in one test body) both run before that timeout fires, so both would see
 * ``isPending`` still ``false`` and both would mint. ``inFlight`` is set
 * synchronously on the FIRST call and cleared in ``onSettled``, so the second
 * call is a no-op regardless of when React gets around to re-rendering.
 */
export function useOpenTargetSession(open: () => Promise<string>, errorLabel: string) {
  const [, navigate] = useLocation();
  const mutation = useMutation({ mutationFn: open });
  const inFlight = useRef(false);

  const callOptions = {
    onSuccess: (sessionId: string) => {
      navigate(`/s/${encodeURIComponent(sessionId)}`);
    },
    // The mint can 404 (slug/app id names nothing) or fail outright. By the
    // time this settles the caller may already be on a different page than
    // when the click happened (wiki: same landing page; apps: same detail
    // page) — either way nothing has navigated yet, so a toast is still the
    // right surface: there's no page transition whose inline Alert would be
    // swallowed, but there's also no dedicated banner slot for a hover-cluster
    // icon button or a header icon button to write into.
    onError: (err: unknown) => {
      toast.error(`${errorLabel} — ${getErrorMessage(err)}`);
    },
    onSettled: () => {
      inFlight.current = false;
    },
  };

  return {
    ...mutation,
    mutate: () => {
      if (inFlight.current) return;
      inFlight.current = true;
      mutation.mutate(undefined, callOptions);
    },
    mutateAsync: () => mutation.mutateAsync(undefined, callOptions),
  };
}
