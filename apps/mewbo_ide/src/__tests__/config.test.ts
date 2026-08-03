import { describe, expect, it } from "vitest";

import { BrokerConfig } from "../config.js";

const VALID = {
  MEWBO_IDE_BROKER_TOKEN: "0123456789abcdef0123",
  MEWBO_IDE_ALLOWED_ROOTS: "/srv/workspaces",
} as const;

describe("BrokerConfig boot validation", () => {
  it("REFUSES TO START without MEWBO_IDE_BROKER_TOKEN", () => {
    expect(() =>
      BrokerConfig.fromEnv({ MEWBO_IDE_ALLOWED_ROOTS: "/srv/workspaces" }),
    ).toThrowError(/MEWBO_IDE_BROKER_TOKEN/);
  });

  it("REFUSES TO START on a token shorter than 16 characters", () => {
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_BROKER_TOKEN: "short" })).toThrowError(
      /at least 16 characters/,
    );
  });

  it("REFUSES TO START without MEWBO_IDE_ALLOWED_ROOTS", () => {
    expect(() =>
      BrokerConfig.fromEnv({ MEWBO_IDE_BROKER_TOKEN: VALID.MEWBO_IDE_BROKER_TOKEN }),
    ).toThrowError(/MEWBO_IDE_ALLOWED_ROOTS/);
  });

  it("REFUSES TO START on an empty MEWBO_IDE_ALLOWED_ROOTS", () => {
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_ALLOWED_ROOTS: "" })).toThrowError(
      /at least one absolute path/,
    );
  });

  it("REFUSES TO START when the roots list is only separators", () => {
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_ALLOWED_ROOTS: ": : " })).toThrowError(
      /at least one absolute path/,
    );
  });

  it("REFUSES TO START on a relative root rather than resolving it against the cwd", () => {
    expect(() =>
      BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_ALLOWED_ROOTS: "workspaces" }),
    ).toThrowError(/must be absolute paths/);
  });

  it("REFUSES TO START on a bad memory string", () => {
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_MEMORY: "1gb" })).toThrowError(
      /memory must match/,
    );
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_MEMORY: "lots" })).toThrowError(
      /memory must match/,
    );
  });

  it("REFUSES TO START on a zero memory limit, which docker reads as unlimited", () => {
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_MEMORY: "0g" })).toThrowError(
      /greater than zero/,
    );
  });

  it("REFUSES TO START on a non-positive cpu or pids limit", () => {
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_CPUS: "0" })).toThrowError(
      /greater than zero/,
    );
    expect(() => BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_PIDS_LIMIT: "-1" })).toThrowError(
      /positive integer/,
    );
  });

  it("applies the documented defaults", () => {
    const config = BrokerConfig.fromEnv({ ...VALID });
    // 0.0.0.0, not loopback: inside a container, a loopback listener is
    // unreachable both through a published port and from a sibling container,
    // and the healthcheck runs inside the container so it would still pass.
    // The off-host restriction lives in the compose publish and the shared
    // secret. Do not "harden" this back to 127.0.0.1 — that is an outage.
    expect(config.host).toBe("0.0.0.0");
    expect(config.port).toBe(5128);
    expect(config.stateDir).toBe("/tmp/mewbo-ide");
    expect(config.image).toBe("codercom/code-server:latest");
    expect(config.network).toBe("mewbo-ide");
    expect(config.memory).toBe("1g");
    expect(config.cpus).toBe(1.0);
    expect(config.pidsLimit).toBe(512);
    expect(config.dockerSocket).toBe("/var/run/docker.sock");
  });

  it("derives the units the docker API actually wants", () => {
    const config = BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_MEMORY: "512m", MEWBO_IDE_CPUS: "1.5" });
    expect(config.memoryBytes()).toBe(512 * 1024 * 1024);
    expect(config.nanoCpus()).toBe(1_500_000_000);
    expect(BrokerConfig.fromEnv({ ...VALID }).memoryBytes()).toBe(1024 * 1024 * 1024);
  });

  it("splits and normalizes multiple roots", () => {
    const config = BrokerConfig.fromEnv({
      ...VALID,
      MEWBO_IDE_ALLOWED_ROOTS: "/srv/workspaces/:/home/coder//projects",
    });
    expect(config.allowedRoots).toEqual(["/srv/workspaces", "/home/coder/projects"]);
  });

  it("defaults MEWBO_IDE_VOLUME_ROOTS to empty — the feature is entirely optional", () => {
    const config = BrokerConfig.fromEnv({ ...VALID });
    expect(config.volumeRoots).toEqual([]);
  });

  it("parses one or more path=volume pairs", () => {
    const config = BrokerConfig.fromEnv({
      ...VALID,
      MEWBO_IDE_VOLUME_ROOTS:
        "/tmp/mewbo/wiki/clones=assistant_wiki-clones,/tmp/mewbo/apps=assistant_apps-staging",
    });
    expect(config.volumeRoots).toEqual([
      { root: "/tmp/mewbo/wiki/clones", volume: "assistant_wiki-clones" },
      { root: "/tmp/mewbo/apps", volume: "assistant_apps-staging" },
    ]);
  });

  it("REFUSES TO START on a volume-root entry missing '='", () => {
    expect(() =>
      BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_VOLUME_ROOTS: "/tmp/mewbo/wiki/clones" }),
    ).toThrowError(/path=volume/);
  });

  it("REFUSES TO START on a relative volume root", () => {
    expect(() =>
      BrokerConfig.fromEnv({ ...VALID, MEWBO_IDE_VOLUME_ROOTS: "relative/path=some-volume" }),
    ).toThrowError(/must be an absolute path/);
  });

  it("ignores an unrecognised MEWBO_IDE_* variable rather than refusing to boot", () => {
    // .env is shared with the API, which legitimately owns
    // MEWBO_IDE_BROKER_URL there. Refusing on every unknown prefixed key would
    // make the broker fail to start on the API's own setting.
    const config = BrokerConfig.fromEnv({
      ...VALID,
      MEWBO_IDE_BROKER_URL: "http://127.0.0.1:5128",
    });
    expect(config.port).toBe(5128);
  });
});
