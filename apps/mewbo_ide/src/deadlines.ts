import fs from "node:fs";
import path from "node:path";

/**
 * The broker's only durable state: one file per session holding the epoch
 * second at which that container should self-terminate.
 *
 * The file is bind-mounted read-only into the container at `/mewbo/deadline`
 * and polled by the watchdog in the entrypoint. Extending a session is nothing
 * more than rewriting this file — the watchdog re-reads it on its own 15s tick,
 * so no container needs to be touched.
 *
 * Cost: O(1) per call — one file, never a directory scan.
 */
export class DeadlineFiles {
  readonly stateDir: string;

  constructor(stateDir: string) {
    this.stateDir = stateDir;
  }

  /** The host-side path bound into the container. */
  pathFor(sessionId: string): string {
    return path.join(this.stateDir, `${sessionId}.deadline`);
  }

  /** Create the state directory if it is missing. Called by `write`, which is the only path that needs it. */
  private ensureDir(): void {
    fs.mkdirSync(this.stateDir, { recursive: true });
  }

  /**
   * Write the deadline as bare ASCII epoch seconds, with NO trailing newline.
   *
   * The watchdog compares with `[ $(date +%s) -lt $(cat /mewbo/deadline) ]`;
   * `sh`'s integer comparison refuses a token it cannot read as a number, so a
   * stray newline turns the guard into a permanent error and the loop exits
   * immediately — killing the container the moment it starts.
   */
  write(sessionId: string, expiresAt: Date): string {
    this.ensureDir();
    const target = this.pathFor(sessionId);
    fs.writeFileSync(target, String(Math.floor(expiresAt.getTime() / 1000)), {
      encoding: "ascii",
    });
    return target;
  }

  /** Read the stored deadline, or null when the file is absent or unparseable. */
  read(sessionId: string): Date | null {
    let raw: string;
    try {
      raw = fs.readFileSync(this.pathFor(sessionId), "ascii");
    } catch {
      return null;
    }
    const epoch = Number.parseInt(raw.trim(), 10);
    if (!Number.isFinite(epoch)) {
      return null;
    }
    return new Date(epoch * 1000);
  }

  /** Unlink the file. Returns whether one existed; never throws. */
  clear(sessionId: string): boolean {
    try {
      fs.unlinkSync(this.pathFor(sessionId));
      return true;
    } catch {
      return false;
    }
  }
}
