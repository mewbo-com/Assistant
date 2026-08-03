import fs from "node:fs";
import path from "node:path";

import { BrokerError } from "./errors.js";

/**
 * Resolve a root once, at construction.
 *
 * A root that is itself a symlink has to be compared post-resolution or every
 * legitimate workspace under it would fail containment. A root that does not
 * exist is kept normalized rather than fatal: refusing to boot over a mount
 * that has not appeared yet would trade a diagnosable 403 for an outage, and
 * a path no `realpath` can produce simply never matches.
 *
 * Exported so `IdeContainers` can canonicalize a configured volume root the
 * identical way — a volume root is unioned into this allowlist (see
 * `server.ts`), so the two must agree on what "the same path" means or a
 * workspace could pass containment under one spelling and fail the
 * volume-vs-bind lookup under another.
 */
export function canonicalRoot(root: string): string {
  try {
    return fs.realpathSync(root);
  } catch {
    return path.normalize(root);
  }
}

/**
 * The containment boundary for `workspace_path`.
 *
 * The check is a REALPATH comparison, never a string prefix test: a caller who
 * can create a symlink inside an allowed root would otherwise be able to point
 * a bind mount at anything on the host — `/`, the docker socket's directory,
 * another tenant's checkout. Resolving first and comparing after is what makes
 * the escape fail.
 *
 * This is also why the broker must MOUNT the allowed roots. It resolves the
 * path in its OWN mount namespace, so a root it cannot see resolves to nothing
 * and every workspace under it is refused — the failure is a clean 403, but the
 * cause is deployment, not the caller.
 *
 * Cost: O(1) — one `realpath` and one `stat` per call, independent of how much
 * is stored under a root.
 */
export class WorkspaceAllowlist {
  readonly roots: readonly string[];

  constructor(roots: readonly string[]) {
    if (roots.length === 0) {
      throw new Error("WorkspaceAllowlist requires at least one root");
    }
    this.roots = Object.freeze(roots.map((root) => canonicalRoot(root)));
  }

  /**
   * Return the realpath of `candidate`, or throw a `workspace_denied` refusal.
   *
   * The refusal names what failed rather than a generic "denied" so an operator
   * can tell a missing bind mount from a genuine escape attempt.
   */
  resolve(candidate: string): string {
    if (candidate.length === 0) {
      throw BrokerError.workspaceDenied("workspace_path must be a non-empty string");
    }
    if (!path.isAbsolute(candidate)) {
      throw BrokerError.workspaceDenied(`workspace_path must be absolute: ${candidate}`);
    }

    let resolved: string;
    try {
      resolved = fs.realpathSync(candidate);
    } catch {
      throw BrokerError.workspaceDenied(
        `workspace_path does not exist in the broker's mount namespace: ${candidate}`,
      );
    }

    let isDirectory: boolean;
    try {
      // `realpathSync` already followed every link, so this stats the target.
      isDirectory = fs.statSync(resolved).isDirectory();
    } catch {
      throw BrokerError.workspaceDenied(`workspace_path is not readable: ${candidate}`);
    }
    if (!isDirectory) {
      throw BrokerError.workspaceDenied(`workspace_path is not a directory: ${candidate}`);
    }

    if (!this.contains(resolved)) {
      throw BrokerError.workspaceDenied(
        `workspace_path resolves outside every allowed root: ${candidate} -> ${resolved}`,
      );
    }
    return resolved;
  }

  private contains(resolved: string): boolean {
    return this.roots.some((root) => {
      if (resolved === root) {
        return true;
      }
      const prefix = root.endsWith(path.sep) ? root : `${root}${path.sep}`;
      return resolved.startsWith(prefix);
    });
  }
}
