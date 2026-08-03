import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DeadlineFiles } from "../deadlines.js";

const SID = "a1b2c3d4e5f60718293a4b5c6d7e8f90";

let tmp: string;
let deadlines: DeadlineFiles;

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mewbo-ide-deadlines-"));
  deadlines = new DeadlineFiles(path.join(tmp, "state"));
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("DeadlineFiles", () => {
  it("writes epoch seconds as ASCII digits with NO trailing newline", () => {
    const expiresAt = new Date("2026-01-01T01:00:00.000Z");
    const written = deadlines.write(SID, expiresAt);

    const raw = fs.readFileSync(written, "ascii");
    expect(raw).toBe(String(Math.floor(expiresAt.getTime() / 1000)));
    expect(raw).toBe("1767229200");
    expect(raw.endsWith("\n")).toBe(false);
    expect(/^\d+$/.test(raw)).toBe(true);
  });

  it("creates the state directory on first write", () => {
    expect(fs.existsSync(path.join(tmp, "state"))).toBe(false);
    deadlines.write(SID, new Date("2026-01-01T01:00:00.000Z"));
    expect(fs.existsSync(path.join(tmp, "state"))).toBe(true);
  });

  it("names the file after the session id", () => {
    expect(deadlines.pathFor(SID)).toBe(path.join(tmp, "state", `${SID}.deadline`));
  });

  it("round-trips through read, truncating sub-second precision", () => {
    deadlines.write(SID, new Date("2026-01-01T01:00:00.750Z"));
    expect(deadlines.read(SID)?.toISOString()).toBe("2026-01-01T01:00:00.000Z");
  });

  it("reads null for an absent or unparseable file", () => {
    expect(deadlines.read(SID)).toBeNull();
    deadlines.write(SID, new Date());
    fs.writeFileSync(deadlines.pathFor(SID), "not-a-number", "ascii");
    expect(deadlines.read(SID)).toBeNull();
  });

  it("overwrites rather than appending, so an extension replaces the deadline", () => {
    deadlines.write(SID, new Date("2026-01-01T01:00:00.000Z"));
    deadlines.write(SID, new Date("2026-01-01T02:00:00.000Z"));
    expect(fs.readFileSync(deadlines.pathFor(SID), "ascii")).toBe("1767232800");
  });

  it("reports whether a file existed on clear, and never throws", () => {
    expect(deadlines.clear(SID)).toBe(false);
    deadlines.write(SID, new Date());
    expect(deadlines.clear(SID)).toBe(true);
    expect(deadlines.clear(SID)).toBe(false);
  });
});
