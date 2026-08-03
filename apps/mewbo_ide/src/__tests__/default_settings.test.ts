import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DEFAULT_COLOR_THEME, DefaultSettingsFile } from "../default_settings.js";

let tmp: string;

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mewbo-ide-default-settings-"));
});

describe("DefaultSettingsFile", () => {
  it("seeds the Monokai theme by default", () => {
    const settings = new DefaultSettingsFile(path.join(tmp, "state"));
    const written = JSON.parse(fs.readFileSync(settings.hostPath(), "utf8")) as Record<
      string,
      unknown
    >;
    expect(written).toEqual({ "workbench.colorTheme": DEFAULT_COLOR_THEME });
    expect(DEFAULT_COLOR_THEME).toBe("Monokai");
  });

  it("creates the state directory on construction, unlike DeadlineFiles' lazy write", () => {
    const stateDir = path.join(tmp, "state");
    expect(fs.existsSync(stateDir)).toBe(false);
    new DefaultSettingsFile(stateDir);
    expect(fs.existsSync(stateDir)).toBe(true);
  });

  it("is one file shared across every session, not keyed by session id", () => {
    const settings = new DefaultSettingsFile(path.join(tmp, "state"));
    expect(path.basename(settings.hostPath())).toBe("default-settings.json");
  });
});
