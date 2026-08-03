import { ProjectSummary } from '../api/contracts';
import { SessionContext } from '../types';

/**
 * The prefix addressing a Mewbo-managed project. Pre-existing wire grammar —
 * it appears in stored session context, in this picker and in the Aura
 * composer — so it is named here rather than respelled at each call site.
 */
export const MANAGED_PREFIX = 'managed:';

/**
 * The reserved project key meaning "no fixed project: the agent picks one".
 *
 * A SIBLING of the `managed:<uuid>` grammar, never a project. The backend's
 * `ProjectCatalog.resolve` refuses it by design (it means "not chosen yet",
 * which must stay distinguishable from "chose the scratch directory"), so
 * every project-SCOPED lookup on this side has to treat it as unscoped rather
 * than pass it through. Mirrors `mewbo_core.project_catalog.AUTO_PROJECT` —
 * one spelling per language, declared where both the composer that sends it
 * and the labels that read it back already import from. A literal spelled in
 * two files is how `app:<app_id>` came to be stamped for a whole sub-product's
 * life with nothing able to read it.
 */
export const AUTO_PROJECT = 'auto';

/** Short name for {@link AUTO_PROJECT} wherever a project is named in one word. */
export const AUTO_PROJECT_LABEL = 'Auto';

/**
 * The picker row's second line. It has to say that the AGENT chooses — a bare
 * "Auto" reads as though Mewbo guesses on the user's behalf, which is the one
 * misreading this mode cannot afford.
 */
export const AUTO_PROJECT_DESCRIPTION =
  'The agent picks the project itself, and can switch to another one as the task needs.';

export type ResolvedProject = {
  /** Human-readable project/repo name, or null when nothing is known. */
  label: string | null;
  /** Associated git branch, when the session runs on a worktree. */
  branch: string | null;
};

/**
 * Resolves a session's stored project identifier to a display label.
 *
 * The console persists ``context.project`` as ``managed:<uuid>`` for managed
 * projects (and worktrees), so a session card would otherwise show a raw UUID.
 * This resolver maps that id back to the project name via the live
 * ``useProjects()`` list — for a worktree it surfaces the parent repo name plus
 * the branch, giving worktree sessions the completeness they lacked. Built once
 * per render from the projects list (DI) and reused by every session row.
 */
export class ProjectLabel {
  private readonly byId: Map<string, ProjectSummary>;
  private readonly byName: Map<string, ProjectSummary>;

  /**
   * Whether a stored/requested project field is the auto-select sentinel.
   *
   * Static because the answer is a property of the KEY, not of any particular
   * projects list — so a caller that has no resolver built (the composer's
   * scoped-lookup guards) can still ask. Trimmed for the same reason the
   * backend's `is_auto_project` trims: the value round-trips through a wire
   * payload before it gets here.
   */
  static isAuto(project?: string | null): boolean {
    return (project ?? '').trim() === AUTO_PROJECT;
  }

  constructor(projects: ProjectSummary[]) {
    this.byId = new Map(
      projects.filter((p) => p.project_id).map((p) => [p.project_id as string, p]),
    );
    this.byName = new Map(projects.map((p) => [p.name, p]));
  }

  /**
   * Canonical ``host/owner/repo`` slug for the session's project, or null when
   * nothing addressable is known.
   *
   * Lives here rather than at a call site because it walks the SAME chain as
   * {@link resolve} — managed id, then worktree-defers-to-parent (a worktree has
   * no remote of its own) — and two copies of that walk would drift the moment
   * one gained a case. A project with no git remote carries neither `repo` nor
   * `aliases` (the backend leaves the keys ABSENT rather than null), so this
   * returns null and the caller renders nothing; we never fabricate a host.
   * `aliases[0]` is already the fully-qualified form WHEN a host is known — it
   * degrades to `owner/repo` for a host-less remote, which `canonicalRepoUrl`
   * correctly refuses to turn into a link.
   */
  repoSlug(context?: SessionContext): string | null {
    const raw = context?.project ?? null;
    if (!raw) return null;
    // The sentinel names no directory, so it can carry no remote either. A
    // configured project could in principle be NAMED "auto"; the sentinel wins,
    // because that is what the backend resolver does with the same string.
    if (ProjectLabel.isAuto(raw)) return null;
    const direct = raw.startsWith(MANAGED_PREFIX)
      ? this.byId.get(raw.slice(MANAGED_PREFIX.length))
      : this.byName.get(raw);
    const parent = direct?.is_worktree && direct.parent_project_id
      ? this.byId.get(direct.parent_project_id)
      : undefined;
    const project = parent ?? direct;
    if (!project) return null;
    if (project.aliases?.length) return project.aliases[0];
    const repo = project.repo;
    return repo ? `${repo.host}/${repo.owner}/${repo.name}` : null;
  }

  resolve(context?: SessionContext): ResolvedProject {
    const raw = context?.project ?? null;
    const branch = context?.branch ?? null;
    if (!raw) return { label: context?.repo ?? null, branch };
    // Auto-select, before the agent has chosen anything. Named rather than
    // passed through, so no surface ever prints the bare wire token "auto" and
    // leaves a user to guess whether that is a project somebody registered.
    if (ProjectLabel.isAuto(raw)) return { label: AUTO_PROJECT_LABEL, branch };
    if (!raw.startsWith(MANAGED_PREFIX)) return { label: raw, branch };

    const project = this.byId.get(raw.slice(MANAGED_PREFIX.length));
    if (!project) return { label: context?.repo ?? 'Managed project', branch };
    if (project.is_worktree) {
      const parent = project.parent_project_id
        ? this.byId.get(project.parent_project_id)
        : undefined;
      return {
        label: parent?.name ?? context?.repo ?? project.name,
        branch: branch ?? project.branch ?? null,
      };
    }
    return { label: project.name, branch };
  }

  /**
   * Resolves ONE entry of `SessionSummary.projects` — the accumulated set a
   * session's context has ever bound to — to a display name.
   *
   * Deliberately a SEPARATE entry point from {@link resolve}: that method takes
   * the raw `context.project` shape (`managed:<uuid>` prefix intact); the
   * backend records `projects` entries with the prefix already STRIPPED (see
   * `mewbo_core.workspaces.project_identity.ProjectIdentity.normalize`), so a
   * managed identity here is a bare uuid, not a `managed:`-prefixed string.
   * Checking `byId` first is what lets that bare uuid resolve to the SAME name
   * `resolve()` would have shown for the prefixed form; falling through to
   * `byName` covers a configured (non-managed) project, whose stored identity
   * is already its bare name. An identity naming neither returns itself — a
   * project this deployment no longer knows about is still a legible filter
   * option, not a blank one.
   */
  resolveIdentity(identity: string): string {
    return this.byId.get(identity)?.name ?? this.byName.get(identity)?.name ?? identity;
  }
}
