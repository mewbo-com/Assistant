import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BrokerConfig } from "../config.js";
import { KIND_LABEL, KIND_VALUE, SESSION_LABEL } from "../containers.js";
import { BrokerServer } from "../server.js";
import { FakeDockerClient } from "./fakeDocker.js";

const TOKEN = "0123456789abcdef0123";
const SID = "a1b2c3d4e5f60718293a4b5c6d7e8f90";
const PASSWORD = "s3cret-token_ABCDEFGH";
const NOW = new Date("2026-01-01T00:00:00.000Z");

let tmp: string;
let workspaceRoot: string;
let stateDir: string;
let docker: FakeDockerClient;
let server: BrokerServer;

const auth = { "x-broker-token": TOKEN };

const validBody = (): Record<string, unknown> => ({
  workspace_path: path.join(workspaceRoot, "project-a"),
  ttl_seconds: 3600,
  password: PASSWORD,
});

beforeEach(() => {
  tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "mewbo-ide-routes-")));
  workspaceRoot = path.join(tmp, "workspaces");
  stateDir = path.join(tmp, "state");
  fs.mkdirSync(path.join(workspaceRoot, "project-a"), { recursive: true });
  fs.mkdirSync(path.join(tmp, "elsewhere"), { recursive: true });

  docker = new FakeDockerClient();
  server = BrokerServer.create({
    config: BrokerConfig.fromEnv({
      MEWBO_IDE_BROKER_TOKEN: TOKEN,
      MEWBO_IDE_ALLOWED_ROOTS: workspaceRoot,
      MEWBO_IDE_STATE_DIR: stateDir,
    }),
    docker,
    now: () => NOW,
    logger: false,
  });
});

afterEach(async () => {
  await server.close();
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("GET /healthz", () => {
  it("answers 200 without a token and without touching the daemon", async () => {
    const response = await server.instance.inject({ method: "GET", url: "/healthz" });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ status: "ok" });
    expect(docker.listCalls).toHaveLength(0);
  });
});

describe("auth", () => {
  it("401s a request with no X-Broker-Token", async () => {
    const response = await server.instance.inject({
      method: "GET",
      url: `/v1/ide/${SID}`,
    });
    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual({
      error: { code: "unauthorized", reason: expect.any(String), retryable: false },
    });
  });

  it("401s a request with the wrong token", async () => {
    const response = await server.instance.inject({
      method: "GET",
      url: `/v1/ide/${SID}`,
      headers: { "x-broker-token": "wrong-token-wrong-token" },
    });
    expect(response.statusCode).toBe(401);
  });

  it("401s a token of the right length but the wrong value", async () => {
    const response = await server.instance.inject({
      method: "GET",
      url: `/v1/ide/${SID}`,
      headers: { "x-broker-token": "0123456789abcdef0124" },
    });
    expect(response.statusCode).toBe(401);
  });

  it("refuses before validating the session id, leaking nothing to an anonymous caller", async () => {
    const response = await server.instance.inject({
      method: "GET",
      url: "/v1/ide/not-a-session",
    });
    expect(response.statusCode).toBe(401);
  });
});

describe("POST /v1/ide/:session_id", () => {
  it("creates the container and answers 201", async () => {
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });

    // The 201 is the whole answer: container name, URL and deadline are all
    // facts the caller derives or owns, so the body carries none of them.
    expect(response.statusCode).toBe(201);
    expect(response.json()).toEqual({});

    const spec = docker.created[0];
    expect(spec?.name).toBe(`mewbo-ide-${SID}`);
    expect(spec?.Image).toBe("codercom/code-server:latest");
    expect(spec?.Env).toEqual([`PASSWORD=${PASSWORD}`]);
    expect(spec?.HostConfig.Binds[0]).toBe(
      `${path.join(workspaceRoot, "project-a")}:/home/coder/project:rw`,
    );
    expect(docker.started).toHaveLength(1);
  });

  it("writes the deadline file BEFORE creating, so the bind source exists", async () => {
    await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });
    const deadlineFile = path.join(stateDir, `${SID}.deadline`);
    expect(fs.readFileSync(deadlineFile, "ascii")).toBe("1767229200");
    expect(docker.created[0]?.HostConfig.Binds[1]).toBe(`${deadlineFile}:/mewbo/deadline:ro`);
  });

  it("replaces a lingering container under the same name", async () => {
    docker.seed({
      Id: "stale",
      Names: [`/mewbo-ide-${SID}`],
      State: "exited",
      Labels: { [KIND_LABEL]: KIND_VALUE, [SESSION_LABEL]: SID },
    });
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });
    expect(response.statusCode).toBe(201);
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
  });

  it("400s a body carrying an unknown extra field", async () => {
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      // `image` is exactly the kind of spec fragment this service must never
      // accept — .strict() turns it into a refusal instead of a silent no-op.
      payload: { ...validBody(), image: "attacker/evil:latest" },
    });
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("bad_request");
    expect(response.json().error.reason).toContain("image");
    expect(docker.created).toHaveLength(0);
  });

  it("400s a malformed session_id", async () => {
    for (const bad of ["not-a-session", "A1B2C3D4E5F60718293A4B5C6D7E8F90", `${SID}0`, "../etc"]) {
      const response = await server.instance.inject({
        method: "POST",
        url: `/v1/ide/${encodeURIComponent(bad)}`,
        headers: auth,
        payload: validBody(),
      });
      expect(response.statusCode, `session_id=${bad}`).toBe(400);
    }
    expect(docker.created).toHaveLength(0);
  });

  it("400s an out-of-range ttl and a malformed password", async () => {
    const cases = [
      { ...validBody(), ttl_seconds: 59 },
      { ...validBody(), ttl_seconds: 604801 },
      { ...validBody(), ttl_seconds: 3600.5 },
      { ...validBody(), password: "short" },
      { ...validBody(), password: "has spaces and $(injection)" },
    ];
    for (const payload of cases) {
      const response = await server.instance.inject({
        method: "POST",
        url: `/v1/ide/${SID}`,
        headers: auth,
        payload,
      });
      expect(response.statusCode, JSON.stringify(payload)).toBe(400);
    }
  });

  it("403s a workspace outside the allowlist", async () => {
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: { ...validBody(), workspace_path: path.join(tmp, "elsewhere") },
    });
    expect(response.statusCode).toBe(403);
    expect(response.json().error.code).toBe("workspace_denied");
    expect(docker.created).toHaveLength(0);
  });

  it("403s a symlink escape", async () => {
    fs.symlinkSync(path.join(tmp, "elsewhere"), path.join(workspaceRoot, "escape"), "dir");
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: { ...validBody(), workspace_path: path.join(workspaceRoot, "escape") },
    });
    expect(response.statusCode).toBe(403);
    expect(docker.created).toHaveLength(0);
  });
});

describe("GET /v1/ide/:session_id", () => {
  it("reports absent rather than 404 when there is no container", async () => {
    const response = await server.instance.inject({
      method: "GET",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ status: "absent" });
  });

  it("reports running once created", async () => {
    await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });
    const response = await server.instance.inject({
      method: "GET",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(response.json()).toEqual({ status: "running" });
  });

  it("reports exited for a stopped container", async () => {
    docker.seed({
      Id: "x",
      Names: [`/mewbo-ide-${SID}`],
      State: "exited",
      Labels: { [KIND_LABEL]: KIND_VALUE, [SESSION_LABEL]: SID },
    });
    const response = await server.instance.inject({
      method: "GET",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(response.json().status).toBe("exited");
  });
});

describe("POST /v1/ide/:session_id/extend", () => {
  it("rewrites the deadline file without requiring a container", async () => {
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}/extend`,
      headers: auth,
      payload: { ttl_seconds: 7200 },
    });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({});
    // The file IS the extension, so it is the only thing worth asserting on.
    expect(fs.readFileSync(path.join(stateDir, `${SID}.deadline`), "ascii")).toBe("1767232800");
  });

  it("400s an unknown extra field", async () => {
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}/extend`,
      headers: auth,
      payload: { ttl_seconds: 7200, container: "mewbo-ide-proxy" },
    });
    expect(response.statusCode).toBe(400);
    expect(response.json().error.reason).toContain("container");
  });
});

describe("DELETE /v1/ide/:session_id", () => {
  it("removes the container and the deadline file", async () => {
    await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });
    const response = await server.instance.inject({
      method: "DELETE",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ removed: true });
    expect(fs.existsSync(path.join(stateDir, `${SID}.deadline`))).toBe(false);
    expect(docker.removed).toEqual([`mewbo-ide-${SID}`]);
  });

  it("reports removed:false when nothing was there, and is idempotent", async () => {
    const first = await server.instance.inject({
      method: "DELETE",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(first.json()).toEqual({ removed: false });
    const second = await server.instance.inject({
      method: "DELETE",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(second.json()).toEqual({ removed: false });
  });

  it("reports removed:true when only the deadline file existed", async () => {
    await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}/extend`,
      headers: auth,
      payload: { ttl_seconds: 3600 },
    });
    const response = await server.instance.inject({
      method: "DELETE",
      url: `/v1/ide/${SID}`,
      headers: auth,
    });
    expect(response.json()).toEqual({ removed: true });
  });
});

describe("the error envelope", () => {
  it("is used for an unknown route too", async () => {
    const response = await server.instance.inject({ method: "GET", url: "/v1/nope" });
    expect(response.statusCode).toBe(404);
    expect(response.json()).toEqual({
      error: { code: "not_found", reason: expect.any(String), retryable: false },
    });
  });

  it("carries retryable:true only for a reachable-daemon failure", async () => {
    const response = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });
    expect(response.statusCode).toBe(201);

    docker.createFailure = Object.assign(new Error("connect ENOENT /var/run/docker.sock"), {
      code: "ENOENT",
    });
    const failed = await server.instance.inject({
      method: "POST",
      url: `/v1/ide/${SID}`,
      headers: auth,
      payload: validBody(),
    });
    expect(failed.statusCode).toBe(503);
    expect(failed.json()).toEqual({
      error: { code: "docker_unavailable", reason: expect.any(String), retryable: true },
    });
  });
});
