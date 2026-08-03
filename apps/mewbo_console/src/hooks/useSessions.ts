import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  archiveSession,
  createSession,
  listSessions,
  pinSession,
  regenerateTitle as apiRegenerateTitle,
  unarchiveSession,
  unpinSession,
  updateSessionTitle,
} from "../api/client";
import { SessionContext, SessionSummary } from "../types";
import { logApiError } from "../utils/errors";

// Stable empty fallbacks. A fresh `[]` literal here is NOT cosmetic: these two
// arrays flow into `MewboRuntimeProvider`'s `useMemo`s and from there into
// assistant-ui's `useExternalStoreRuntime`, whose thread-list core compares
// `threads`/`archivedThreads` by REFERENCE and notifies its subscribers on any
// inequality. Those subscribers read through `useSyncExternalStore`, which
// re-checks its snapshot after every commit — so a new array each render means
// a new snapshot each render, and React tears the tree down with "Maximum
// update depth exceeded". The `archived` query below is `enabled: false`, so
// its `data` is permanently undefined and the fallback is the value that is
// ALWAYS read; that is what made a live session page crash.
const EMPTY_SESSIONS: SessionSummary[] = [];

export function useSessions() {
  const qc = useQueryClient();
  const active = useQuery({
    queryKey: ["sessions", "active"],
    queryFn: () => listSessions(false),
  });
  const archived = useQuery({
    queryKey: ["sessions", "archived"],
    queryFn: () => listSessions(true).then((d) => d.filter((s) => s.archived)),
    enabled: false,
  });

  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ["sessions", "active"] });
  };
  const refreshArchived = async () => {
    await archived.refetch();
  };

  const createM = useMutation({
    mutationFn: (ctx?: SessionContext) =>
      createSession(ctx ?? { mcp_tools: [] }),
    // Seed the row for the session that was just created, rather than
    // refetching. The console routes to `/s/<id>` the moment this resolves and
    // that route resolves its session out of THIS list, so the row has to exist
    // locally — and a refetch cannot supply it: the runtime deliberately hides a
    // session with no visible event yet and no run in flight from
    // `list_sessions`, which is exactly what a session between create and its
    // first query is. The row is superseded by server truth on the next refresh
    // (the one the caller runs once its query is accepted, by which point the
    // run makes the session listable).
    //
    // Only what the create call itself establishes is written: the id it
    // returned and the context that was sent. The title the runtime derives
    // later stays empty rather than invented, and an absent `created_at` already
    // renders as "Just now" (`formatSessionTime`) — a plausible timestamp is
    // never minted here.
    onSuccess: (sessionId, ctx) => {
      qc.setQueryData<SessionSummary[]>(["sessions", "active"], (prev) => [
        { session_id: sessionId, title: "", running: false, context: ctx },
        ...(prev ?? []),
      ]);
    },
  });
  const archiveM = useMutation({
    mutationFn: (id: string) => archiveSession(id),
    onSuccess: () => {
      void refresh();
      void refreshArchived();
    },
  });
  const unarchiveM = useMutation({
    mutationFn: (id: string) => unarchiveSession(id),
    onSuccess: () => {
      void refresh();
      void refreshArchived();
    },
  });

  // Pin/unpin patch the cached row in place (instant re-sort to the top),
  // same shape as `applyTitle` below — the trailing `refresh()` is
  // server-truth reconciliation, not the source of the immediate update.
// ⚠️ No `?? []` on these updaters. TanStack treats an `undefined` return as
// "do not write", which is exactly right when the listing has not loaded:
// fabricating `[]` publishes "there are zero sessions" into the cache, so a
// directly-visited session page can never find its own row — and a guard that
// compares against that missing row can then never settle. That is what let a
// title sync re-fire on every commit until React gave up with "Maximum update
// depth exceeded".
  const applyPinned = (id: string, pinned: boolean, pinnedAt: string | null) => {
    const patch = (prev?: SessionSummary[]) =>
      prev?.map((s) =>
        s.session_id === id ? { ...s, pinned, pinned_at: pinnedAt } : s
      );
    qc.setQueryData<SessionSummary[]>(["sessions", "active"], patch);
    qc.setQueryData<SessionSummary[]>(["sessions", "archived"], patch);
  };
  const pinM = useMutation({
    mutationFn: (id: string) => pinSession(id),
    onSuccess: (res) => {
      applyPinned(res.session_id, res.pinned, res.pinned_at);
      void refresh();
    },
  });
  const unpinM = useMutation({
    mutationFn: (id: string) => unpinSession(id),
    onSuccess: (res) => {
      applyPinned(res.session_id, res.pinned, res.pinned_at);
      void refresh();
    },
  });

  const applyTitle = (id: string, title: string) => {
    const patch = (prev?: SessionSummary[]) =>
      prev?.map((s) => (s.session_id === id ? { ...s, title } : s));
    qc.setQueryData<SessionSummary[]>(["sessions", "active"], patch);
    qc.setQueryData<SessionSummary[]>(["sessions", "archived"], patch);
  };

  const updateTitleM = useMutation({
    mutationFn: (vars: { id: string; title: string }) =>
      updateSessionTitle(vars.id, vars.title),
    onSuccess: (res) => applyTitle(res.session_id, res.title),
  });
  const regenerateTitleM = useMutation({
    mutationFn: (id: string) => apiRegenerateTitle(id),
    onSuccess: (res) => applyTitle(res.session_id, res.title),
  });

  return {
    sessions: active.data ?? EMPTY_SESSIONS,
    archivedSessions: archived.data ?? EMPTY_SESSIONS,
    loading: active.isPending,
    archivedLoading: archived.isFetching,
    error: active.error ? logApiError("listSessions", active.error) : null,
    archivedError: archived.error
      ? logApiError("listArchivedSessions", archived.error)
      : null,
    refresh,
    refreshArchived,
    create: async (ctx?: SessionContext) => createM.mutateAsync(ctx),
    archive: async (id: string) => {
      await archiveM.mutateAsync(id);
    },
    unarchive: async (id: string) => {
      await unarchiveM.mutateAsync(id);
    },
    pin: async (id: string) => {
      await pinM.mutateAsync(id);
    },
    unpin: async (id: string) => {
      await unpinM.mutateAsync(id);
    },
    updateTitle: async (id: string, title: string) => {
      await updateTitleM.mutateAsync({ id, title });
    },
    regenerateTitle: async (id: string) =>
      (await regenerateTitleM.mutateAsync(id)).title,
    applyTitle,
  };
}
