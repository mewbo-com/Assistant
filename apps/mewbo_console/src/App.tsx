import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState } from
'react';
import { toast } from 'sonner';
import { Redirect, Route, Switch, useLocation, useRoute, useSearchParams } from 'wouter';
import type { AppendMessage } from '@assistant-ui/react';
import { AppLayout } from './components/AppLayout';
import type { ActiveProduct } from './components/nav-rail/products';
import { HomeView } from './components/HomeView';
import { SessionDetailView } from './components/SessionDetailView';
const SettingsView = lazy(() => import('./components/SettingsView').then(m => ({ default: m.SettingsView })));
const IdeLoader = lazy(() => import('./components/IdeLoader').then(m => ({ default: m.IdeLoader })));
const AgenticSearchView = lazy(() => import('./components/agentic_search/AgenticSearchView').then(m => ({ default: m.AgenticSearchView })));
const WikiApp = lazy(() => import('./components/wiki/WikiApp').then(m => ({ default: m.WikiApp })));
const AppsView = lazy(() => import('./components/apps/AppsView').then(m => ({ default: m.AppsView })));
import {
  createShare,
  exportSession,
  postQuery,
  sendMessage,
  uploadAttachments
} from './api/client';
import { useConfig } from './hooks/useConfig';
import { useSessions } from './hooks/useSessions';
import {
  AttachmentPayload,
  QueryMode,
  SessionContext,
  SessionSummary,
  SessionTarget,
} from './types';
import { openTargetSession } from './utils/sessionTarget';
import { copyText } from './utils/clipboard';
import { logApiError } from './utils/errors';
import { useNotifications } from './hooks/useNotifications';
import { NotificationBalloon } from './components/NotificationBalloon';

function SuspenseFallback({ fullScreen = false }: { fullScreen?: boolean }) {
  const wrapper = fullScreen
    ? "min-h-screen flex items-center justify-center"
    : "flex-1 flex items-center justify-center";
  return (
    <div className={wrapper}>
      <span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span>
    </div>
  );
}

/**
 * `/triggers` retired into the Settings automation facet. Unlike the other three
 * redirects this one can't be a static `<Redirect to>`: `SessionTriggersSection`
 * deep-links `/triggers?session=<id>` and the automation pane still reads that
 * param, so the query has to survive the hop.
 */
function TriggersRedirect() {
  const [params] = useSearchParams();
  const session = params.get('session');
  const to = session
    ? `/settings?facet=automation&session=${encodeURIComponent(session)}`
    : '/settings?facet=automation';
  return <Redirect to={to} replace />;
}

interface SessionDetailRouteProps {
  id: string;
  sessions: SessionSummary[];
  archivedSessions: SessionSummary[];
  archivedLoading: boolean;
  refreshArchived: () => Promise<void>;
  refresh: () => Promise<void>;
  applyTitle: (sessionId: string, title: string) => void;
  onSelectSession: (sessionId: string) => void;
  onBack: () => void;
  // Session-header obligations.
  onRenameTitle: (sessionId: string, title: string) => Promise<void>;
  onRegenerateTitle: (sessionId: string) => Promise<string>;
  onArchive: (sessionId: string) => void;
  onUnarchive: (sessionId: string) => void;
  onShare: (sessionId: string) => void;
  onExport: (sessionId: string) => void;
  langfuseBaseUrl: string | null;
}

// Resolves a session by id from active + archived lists, lazily fetching the
// archived list if the id isn't found locally. Mirrors the hydration logic the
// old activeSession lookup performed via a side-effect useEffect.
//
// The lists are an ENRICHMENT here, never a gate: `GET /api/sessions` summarises
// every session and is measured in seconds on a large deployment, while this
// page's own per-session fetches answer for one session in a fraction of that.
// So the page mounts from the id alone and `SessionDetailView` fills the
// subject's fields from whichever source arrives first (see its `subject`).
function SessionDetailRoute({
  id,
  sessions,
  archivedSessions,
  archivedLoading,
  refreshArchived,
  refresh,
  applyTitle,
  onSelectSession,
  onBack,
  onRenameTitle,
  onRegenerateTitle,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  langfuseBaseUrl,
}: SessionDetailRouteProps) {
  const listed =
    sessions.find((s) => s.session_id === id) ||
    archivedSessions.find((s) => s.session_id === id);

  useEffect(() => {
    if (!listed && !archivedLoading) {
      void refreshArchived();
    }
  }, [listed, archivedLoading, refreshArchived]);

  // Stands in until a listing row exists — the id is the one fact routing
  // supplies, and every other field the page shows is either in the transcript
  // or in the per-session poll's own response. Memoised so an unrelated list
  // refresh can't hand the view a fresh object identity every render.
  const fallback = useMemo<SessionSummary>(() => ({ session_id: id, title: "" }), [id]);

  return (
    <SessionDetailView
      session={listed ?? fallback}
      onTitleUpdate={applyTitle}
      onSessionChange={refresh}
      onSelectSession={onSelectSession}
      onBack={onBack}
      onRenameTitle={onRenameTitle}
      onRegenerateTitle={onRegenerateTitle}
      onArchive={onArchive}
      onUnarchive={onUnarchive}
      onShare={onShare}
      onExport={onExport}
      langfuseUrl={langfuseBaseUrl ? `${langfuseBaseUrl}/${id}` : null}
    />
  );
}

export function App() {
  const [location, setLocation] = useLocation();
  const [isSettings] = useRoute('/settings');
  const [isSearch] = useRoute('/search');
  const [isWikiRoot] = useRoute('/wiki');
  const [isWikiSub] = useRoute('/wiki/*');
  const isWiki = isWikiRoot || isWikiSub;
  const [isAppsRoot] = useRoute('/apps');
  const [isAppsSub] = useRoute('/apps/*');
  const isApps = isAppsRoot || isAppsSub;
  const [isSessionRoute, sessionParams] = useRoute<{ id: string }>('/s/:id');
  const [isIdeLoader, ideLoaderParams] = useRoute<{ sessionId: string }>('/ide-loader/:sessionId');

  const activeSessionId = isSessionRoute && sessionParams
    ? decodeURIComponent(sessionParams.id)
    : null;

  const [actionError, setActionError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try {
      return window.localStorage.getItem('mewbo:theme') === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  });
  const {
    notifications,
    dismiss: dismissNotification,
    clearAll: clearNotifications
  } = useNotifications();
  // Keep the <html> class in sync with theme (initial value may be persisted).
  // toggleTheme also mutates the class synchronously before dispatching
  // wiki:theme-change; this effect is an idempotent re-assert, not the primary sync.
  useEffect(() => {
    if (theme === 'light') {
      document.documentElement.classList.add('light');
    } else {
      document.documentElement.classList.remove('light');
    }
  }, [theme]);
  // Langfuse config, to construct session dashboard URLs — reads the SAME
  // ['config'] TanStack query every other config consumer shares, instead of
  // a one-off fetch-in-useEffect that bypassed the cache.
  const { config } = useConfig();
  const langfuseBaseUrl = useMemo(() => {
    const lf = config?.langfuse as Record<string, unknown> | undefined;
    if (!lf?.enabled || !lf?.host || !lf?.project_id) return null;
    const host = String(lf.host).replace(/\/+$/, '');
    return `${host}/project/${lf.project_id}/sessions`;
  }, [config]);
  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      if (next === 'light') {
        document.documentElement.classList.add('light');
      } else {
        document.documentElement.classList.remove('light');
      }
      // Notify wiki Mermaid blocks (and any other theme-aware paint) to re-render.
      window.dispatchEvent(new CustomEvent('wiki:theme-change', { detail: next }));
      try {
        window.localStorage.setItem('mewbo:theme', next);
      } catch {
        // No-op: persistence is best-effort (private browsing, quota, etc.)
      }
      return next;
    });
  }, []);
  const {
    sessions,
    archivedSessions,
    loading,
    archivedLoading,
    error,
    archivedError,
    create,
    refresh,
    refreshArchived,
    archive,
    unarchive,
    pin,
    unpin,
    updateTitle,
    regenerateTitle,
    applyTitle
  } = useSessions();
  const activeSession =
    sessions.find((session) => session.session_id === activeSessionId) ||
    archivedSessions.find((session) => session.session_id === activeSessionId);

  const goToSession = useCallback((sessionId: string) => {
    setLocation(`/s/${encodeURIComponent(sessionId)}`);
  }, [setLocation]);
  const goHome = useCallback(() => {
    setLocation('/');
  }, [setLocation]);
  const handleSessionSelect = useCallback((sessionId: string) => {
    setActionError(null);
    goToSession(sessionId);
  }, [goToSession]);
  const handleCreateAndRun = async (
  query: string,
  context?: SessionContext,
  mode?: QueryMode,
  attachments?: File[],
  target?: SessionTarget | null) =>
  {
    setActionError(null);
    setCreating(true);
    let sessionId: string;
    try {
      // A target REPLACES `create`; it does not decorate it. The product's route
      // mints a session already bound to that wiki project or app, which is the
      // whole reason a target cannot be expressed as a context key — a plain
      // session with an extra field on it would reach the product's tools with
      // nothing for them to resolve.
      //
      // The composer is a CREATE surface, so a targeted submit always asks for a
      // genuinely NEW session (`{requestNew: true}`) rather than the endpoint's
      // default get-or-create reuse. Reuse is still correct for the gallery card /
      // app header "open" buttons — those call `openProjectSession`/`openAppSession`
      // directly, never through this function, so they are untouched by this flag.
      sessionId = target ? await openTargetSession(target, { requestNew: true }) : await create(context);
    } catch (err) {
      // The operator is still on the landing page, so its inline Alert is the
      // right surface — and there is no session to show them instead. This is
      // the whole reason the target call sits BEFORE the hop: it is the one
      // failure that leaves nothing to navigate to, so it must fail here, where
      // an inline Alert is still mounted, rather than after.
      setActionError(logApiError('createAndRun', err));
      setCreating(false);
      return;
    }
    // Routing needs nothing but the id, and the session page can already paint
    // its shell (header, composer, and the run's starting beat) from the
    // transcript. Waiting for /query to be accepted first held the operator on
    // the landing page for a round trip that told them nothing.
    goToSession(sessionId);
    // The landing page's job is done the moment we leave it: leaving `creating`
    // set would strand its composer as busy if the operator navigated back
    // while the query below is still in flight.
    setCreating(false);
    try {
      const attachmentRecords: AttachmentPayload[] | undefined =
      attachments && attachments.length > 0 ?
      await uploadAttachments(sessionId, attachments) :
      undefined;
      await postQuery(sessionId, query, context, mode, attachmentRecords);
      await refresh();
      window.setTimeout(() => {
        void refresh();
      }, 800);
    } catch (err) {
      // `actionError` renders on the landing page, which is no longer mounted —
      // setting it here would swallow the failure. A toast is the surface that
      // reaches the operator on the session page they are now looking at, and
      // the same one every other post-navigation action failure uses.
      toast.error(logApiError('createAndRun', err));
    }
  };
  // assistant-ui composer submit seam (`onNew` on the external-store runtime).
  // The bespoke InputBar still owns the live composer, so this only fires once
  // the assistant-ui composer WP is wired in. It routes create-and-run (landing)
  // vs steer (open session); that WP refines context/mode/attachment handling.
  const handleComposerNew = async (message: AppendMessage) => {
    const text = message.content
      .map((part) => (part.type === 'text' ? part.text : ''))
      .join('')
      .trim();
    if (!text) return;
    if (activeSessionId) {
      // Mirror useSessionQuery.send: a mid-run turn must STEER via /message,
      // not start a second run via /query (double-fire otherwise).
      if (activeSession?.running) {
        await sendMessage(activeSessionId, text);
      } else {
        await postQuery(activeSessionId, text);
      }
      await refresh();
    } else {
      await handleCreateAndRun(text);
    }
  };
  const handleBack = useCallback(() => {
    setActionError(null);
    goHome();
    void refresh();
  }, [goHome, refresh]);
  const handleShareSession = useCallback(async (sessionId: string) => {
    try {
      const record = await createShare(sessionId);
      const shareUrl = `${window.location.origin}/api/share/${record.token}`;
      // `copyText` already owns the Clipboard-API-vs-execCommand fallback —
      // no need to hand-roll that split here too.
      await copyText(shareUrl);
      toast.success('Share link copied to clipboard.', { description: shareUrl });
    } catch (err) {
      const message = logApiError('shareSession', err);
      toast.error(message);
    }
  }, []);
  const handleExportSession = useCallback(async (sessionId: string) => {
    try {
      const payload = await exportSession(sessionId);
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json'
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `session-${sessionId}.json`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      const message = logApiError('exportSession', err);
      toast.error(message);
    }
  }, []);
  useEffect(() => {
    if (isSettings) {
      document.title = 'Settings | Mewbo';
    } else if (isSearch) {
      document.title = 'Agentic Search | Mewbo';
    } else if (isWiki) {
      // WikiApp manages its own title; leave the default here.
    } else if (isApps) {
      // AppsView manages its own title; leave the default here.
    } else if (isIdeLoader) {
      document.title = 'Opening Web IDE | Mewbo';
    } else if (isSessionRoute) {
      const title = activeSession?.title?.trim();
      document.title = title ? `${title} | Mewbo` : 'Session | Mewbo';
    } else {
      document.title = 'Agentic Tasks | Mewbo';
    }
  }, [isSettings, isSearch, isWiki, isApps, isIdeLoader, isSessionRoute, activeSession]);

  // The IDE loader is a standalone full-screen page with no chrome — render
  // it outside the AppLayout so it can't be mistaken for a session view.
  if (isIdeLoader && ideLoaderParams) {
    const ideSessionId = decodeURIComponent(ideLoaderParams.sessionId);
    return (
      <Suspense fallback={<SuspenseFallback fullScreen />}>
        <IdeLoader sessionId={ideSessionId} />
      </Suspense>
    );
  }

  // What the rail is scoped to: `wiki > search > apps > tasks`; a session page and the
  // task landing are both Tasks. `/settings` scopes the rail's section to the
  // settings facets while marking NO product row current (it has no switcher
  // row), which is how that route gets primary navigation without a fifth
  // product. Any other route is `null`: no row current, section falls back to
  // Tasks, and the rail still renders.
  const activeProduct: ActiveProduct = isWiki
    ? 'wiki'
    : isSearch
      ? 'search'
      : isApps
        ? 'apps'
        : isSettings
          ? 'settings'
          : (isSessionRoute || location === '/')
            ? 'tasks'
            : null;

  return (
    <>
    <NotificationBalloon notifications={notifications} />
    <AppLayout
      activeProduct={activeProduct}
      theme={theme}
      onToggleTheme={toggleTheme}
      notifications={notifications}
      onDismissNotification={dismissNotification}
      onClearNotifications={clearNotifications}
      sessions={sessions}
      archivedSessions={archivedSessions}
      activeSessionId={activeSessionId}
      isRunning={activeSession?.running ?? false}
      onComposerNew={handleComposerNew}
      onSwitchToThread={handleSessionSelect}
      onSwitchToNewThread={goHome}
      onRenameThread={updateTitle}
      onArchiveThread={archive}>
      <Switch>
        <Route path="/settings">
          <Suspense fallback={<SuspenseFallback />}>
            <SettingsView />
          </Suspense>
        </Route>
        {/* Retired pages — Projects / Plugins / API Keys / Triggers are now
            Settings facets. Redirects (not deletions) so bookmarks and existing
            in-app deep links keep resolving. */}
        <Route path="/projects">
          <Redirect to="/settings?facet=workspace" replace />
        </Route>
        <Route path="/plugins">
          <Redirect to="/settings?facet=plugins" replace />
        </Route>
        <Route path="/keys">
          <Redirect to="/settings?facet=security" replace />
        </Route>
        <Route path="/triggers">
          <TriggersRedirect />
        </Route>
        <Route path="/search">
          <Suspense fallback={<SuspenseFallback />}>
            <AgenticSearchView />
          </Suspense>
        </Route>
        <Route path="/wiki/*?">
          <Suspense fallback={<SuspenseFallback />}>
            <WikiApp />
          </Suspense>
        </Route>
        <Route path="/apps/*?">
          <Suspense fallback={<SuspenseFallback />}>
            <AppsView />
          </Suspense>
        </Route>
        <Route path="/s/:id">
          {(params) => (
            <SessionDetailRoute
              id={decodeURIComponent(params.id)}
              sessions={sessions}
              archivedSessions={archivedSessions}
              archivedLoading={archivedLoading}
              refreshArchived={refreshArchived}
              refresh={refresh}
              applyTitle={applyTitle}
              onSelectSession={handleSessionSelect}
              onBack={handleBack}
              onRenameTitle={updateTitle}
              onRegenerateTitle={regenerateTitle}
              onArchive={archive}
              onUnarchive={unarchive}
              onShare={handleShareSession}
              onExport={handleExportSession}
              langfuseBaseUrl={langfuseBaseUrl}
            />
          )}
        </Route>
        <Route>
          <div className="flex-1 overflow-hidden">
            <HomeView
              sessions={sessions}
              archivedSessions={archivedSessions}
              loading={loading}
              archivedLoading={archivedLoading}
              error={error}
              archivedError={archivedError}
              actionError={actionError}
              onSelectSession={handleSessionSelect}
              onCreateAndRun={handleCreateAndRun}
              onLoadArchived={refreshArchived}
              onArchive={archive}
              onUnarchive={unarchive}
              onPin={pin}
              onUnpin={unpin}
              isCreating={creating}
              onRetry={refresh} />
          </div>
        </Route>
      </Switch>
    </AppLayout>
    </>);

}
