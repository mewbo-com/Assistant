import { openAppSession } from '../api/apps';
import { openProjectSession } from '../components/wiki/api/client';
import type { SessionTarget } from '../types';

/**
 * Mint (or reuse) the session a composer TARGET names, returning its id.
 *
 * The one place the target union is switched on, and the reason a target is not
 * simply another `SessionContext` key: each product owns a get-or-create route
 * keyed by its own identifier, and the session it returns is already bound to
 * that product server-side. A plain session carrying an extra field would reach
 * the product's tools with nothing for them to resolve.
 *
 * Both routes are idempotent BY DEFAULT, so the gallery card / app-header
 * "open" buttons (which call `openProjectSession`/`openAppSession` directly,
 * never through here) continue the existing conversation on a second click —
 * that is why THEY are labelled "open", not "new".
 *
 * The composer is a different surface with a different job: it is a CREATE
 * affordance, and a second submit against the same target must not silently
 * append to whatever conversation the target last produced.
 * `App.handleCreateAndRun` is the one caller that passes `{requestNew: true}`,
 * which both routes forward as a request for a fresh session instead of the
 * default reuse. `requestNew` is this function's OWN internal parameter name —
 * it does not need to match the wire, and it doesn't: the wiki route's field is
 * `newSession` (camelCase, matching `{sessionId, created}`) and the apps
 * route's is `new_session` (snake_case, matching `{session_id, created}`).
 * `openProjectSession`/`openAppSession` each absorb their own casing, which is
 * exactly what having a per-product client buys — this function stays casing
 * agnostic.
 *
 * The two clients spell the id differently (the wiki's is camelCase, the apps
 * client returns the raw wire shape); normalising that here keeps the split out
 * of the caller. It lives in its own module rather than beside
 * `App.handleCreateAndRun` because `App.tsx` may export only components —
 * `react-refresh/only-export-components` runs at `--max-warnings=0`.
 */
export async function openTargetSession(
  target: SessionTarget,
  opts: { requestNew?: boolean } = {},
): Promise<string> {
  if (target.kind === 'wiki') {
    return (await openProjectSession(target.slug, opts)).sessionId;
  }
  return (await openAppSession(target.appId, opts)).session_id;
}
