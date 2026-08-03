import fs from "node:fs";
import path from "node:path";

/**
 * Default `workbench.colorTheme`, verified against a real
 * `codercom/code-server:latest` container rather than assumed: its bundled
 * `theme-monokai` extension (`vscode.theme-monokai`) contributes exactly one
 * theme, id `"Monokai"` (`extensions/theme-monokai/package.json`).
 */
export const DEFAULT_COLOR_THEME = "Monokai";

/**
 * Where code-server reads user settings from, inside the container.
 *
 * Verified by running the image and reading its own log line
 * (`Using user-data-dir /home/coder/.local/share/code-server`), then the
 * VS Code convention of `<user-data-dir>/User/settings.json` underneath it —
 * confirmed by starting the container and observing the `User/` directory
 * created on first HTTP request (the file itself is created lazily, on the
 * first setting a user or this bind ever writes).
 */
export const CODE_SERVER_SETTINGS_PATH = "/home/coder/.local/share/code-server/User/settings.json";

/**
 * The one seeded settings file bound read-only into every container.
 *
 * Shared across sessions, unlike `DeadlineFiles` — the theme is broker
 * configuration, not a per-session value, so one file serves every container
 * `buildSpec` creates. Written once at construction (broker startup) into the
 * same state directory `DeadlineFiles` already relies on being identical
 * inside this process and inside the docker daemon's view of the host: a
 * bind whose SOURCE is missing at create time materializes as a directory,
 * not a file, so it must exist before the first `create` runs.
 *
 * Read-only, and there is nowhere for a user's in-session theme change to
 * land: code-server's own settings live in the container's writable layer,
 * which `create` discards on every force-remove-and-respawn. A user picking
 * a different theme in the IDE works for that container's lifetime and is
 * lost on the next reopen — a known limitation, not a bug, until a
 * per-session writable mount exists for it.
 */
export class DefaultSettingsFile {
  private readonly path: string;

  constructor(stateDir: string, contents: Record<string, unknown> = { "workbench.colorTheme": DEFAULT_COLOR_THEME }) {
    fs.mkdirSync(stateDir, { recursive: true });
    this.path = path.join(stateDir, "default-settings.json");
    fs.writeFileSync(this.path, JSON.stringify(contents), "utf8");
  }

  /** The host-side path bound into every container. */
  hostPath(): string {
    return this.path;
  }
}
