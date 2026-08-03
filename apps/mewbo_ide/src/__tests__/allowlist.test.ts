import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { WorkspaceAllowlist } from "../allowlist.js";
import { BrokerError } from "../errors.js";

let tmp: string;
let root: string;
let outside: string;
let allowlist: WorkspaceAllowlist;

beforeAll(() => {
  tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "mewbo-ide-allowlist-")));
  root = path.join(tmp, "workspaces");
  outside = path.join(tmp, "secrets");
  fs.mkdirSync(root);
  fs.mkdirSync(outside);
  fs.mkdirSync(path.join(root, "project-a"));
  fs.writeFileSync(path.join(root, "notes.txt"), "not a directory");
  // A REAL symlink, not a mocked realpath: the escape this guards against is a
  // filesystem fact, and a stubbed resolver would only prove the stub.
  fs.symlinkSync(outside, path.join(root, "escape-hatch"), "dir");
  fs.symlinkSync(path.join(root, "project-a"), path.join(root, "inside-link"), "dir");
  allowlist = new WorkspaceAllowlist([root]);
});

afterAll(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("WorkspaceAllowlist", () => {
  it("accepts a directory inside an allowed root", () => {
    expect(allowlist.resolve(path.join(root, "project-a"))).toBe(path.join(root, "project-a"));
  });

  it("accepts the root itself", () => {
    expect(allowlist.resolve(root)).toBe(root);
  });

  it("accepts a symlink whose target stays inside the root, returning the target", () => {
    expect(allowlist.resolve(path.join(root, "inside-link"))).toBe(path.join(root, "project-a"));
  });

  it("REFUSES a symlink that points outside the root", () => {
    const link = path.join(root, "escape-hatch");
    // The link itself is textually under the root — a prefix test would pass it.
    expect(link.startsWith(root)).toBe(true);
    let thrown: unknown;
    try {
      allowlist.resolve(link);
    } catch (err) {
      thrown = err;
    }
    expect(thrown).toBeInstanceOf(BrokerError);
    const failure = thrown as BrokerError;
    expect(failure.status).toBe(403);
    expect(failure.code).toBe("workspace_denied");
    expect(failure.reason).toContain("outside every allowed root");
    expect(failure.reason).toContain(outside);
  });

  it("refuses a path outside every root", () => {
    expect(() => allowlist.resolve(outside)).toThrowError(/outside every allowed root/);
  });

  it("refuses a path that is not a directory", () => {
    expect(() => allowlist.resolve(path.join(root, "notes.txt"))).toThrowError(
      /is not a directory/,
    );
  });

  it("refuses a relative path", () => {
    expect(() => allowlist.resolve("workspaces/project-a")).toThrowError(/must be absolute/);
  });

  it("refuses a path that does not exist", () => {
    expect(() => allowlist.resolve(path.join(root, "nope"))).toThrowError(/does not exist/);
  });

  it("refuses an empty path", () => {
    expect(() => allowlist.resolve("")).toThrowError(/non-empty/);
  });

  it("refuses to be constructed with no roots", () => {
    expect(() => new WorkspaceAllowlist([])).toThrowError(/at least one root/);
  });

  it("resolves a root that is itself a symlink, so workspaces under it still match", () => {
    const linkedRoot = path.join(tmp, "linked-root");
    fs.symlinkSync(root, linkedRoot, "dir");
    const viaLink = new WorkspaceAllowlist([linkedRoot]);
    expect(viaLink.resolve(path.join(root, "project-a"))).toBe(path.join(root, "project-a"));
  });
});
