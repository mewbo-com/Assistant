import { timingSafeEqual } from "node:crypto";

import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";

import type { WorkspaceAllowlist } from "./allowlist.js";
import type { BrokerConfig } from "./config.js";
import type { IdeContainers, IdeStatus } from "./containers.js";
import type { DeadlineFiles } from "./deadlines.js";
import { BrokerError } from "./errors.js";
import { createIdeBody, extendIdeBody, SESSION_ID_RE } from "./schemas.js";

interface SessionParams {
  session_id: string;
}

/**
 * The body of a route whose whole answer is its status code.
 *
 * Deliberately empty. Everything this service could echo back about a container
 * it just created or extended is a fact the API already holds: the container
 * name and the browser URL are derived from the session id on both sides, and
 * the API owns `expires_at` authoritatively in its own store. Sending them back
 * would be a second channel for a fact that already has one — and the caller's
 * `extra="forbid"` parse means an unread field is not free, it is a field that
 * has to stay in lockstep across two languages for no reader's benefit.
 */
export type CommandResponse = Record<string, never>;

export interface IdeStatusResponse {
  status: IdeStatus;
}

export interface DeleteIdeResponse {
  removed: boolean;
}

export interface HealthResponse {
  status: "ok";
}

/**
 * The HTTP adapter. Collaborators arrive as constructor fields; every handler
 * validates, delegates and serializes — no decision of its own.
 */
export class IdeRoutes {
  private readonly config: BrokerConfig;
  private readonly allowlist: WorkspaceAllowlist;
  private readonly containers: IdeContainers;
  private readonly deadlines: DeadlineFiles;
  private readonly now: () => Date;

  constructor(
    config: BrokerConfig,
    allowlist: WorkspaceAllowlist,
    containers: IdeContainers,
    deadlines: DeadlineFiles,
    now: () => Date,
  ) {
    this.config = config;
    this.allowlist = allowlist;
    this.containers = containers;
    this.deadlines = deadlines;
    this.now = now;
  }

  register(app: FastifyInstance): void {
    app.get("/healthz", async (): Promise<HealthResponse> => this.health());

    app.post<{ Params: SessionParams }>(
      "/v1/ide/:session_id",
      async (request, reply): Promise<CommandResponse> => this.create(request, reply),
    );

    app.get<{ Params: SessionParams }>(
      "/v1/ide/:session_id",
      async (request): Promise<IdeStatusResponse> => this.status(request),
    );

    app.post<{ Params: SessionParams }>(
      "/v1/ide/:session_id/extend",
      async (request): Promise<CommandResponse> => this.extend(request),
    );

    app.delete<{ Params: SessionParams }>(
      "/v1/ide/:session_id",
      async (request): Promise<DeleteIdeResponse> => this.remove(request),
    );
  }

  /**
   * Liveness only: touches neither the daemon nor the filesystem, so it stays
   * answerable while docker is down. Unauthenticated by design — a health probe
   * that needs the shared secret cannot be wired into compose.
   *
   * Cost: O(1).
   */
  private health(): HealthResponse {
    return { status: "ok" };
  }

  /**
   * Create or replace this session's container.
   *
   * Order is load-bearing: the deadline file is written BEFORE the container is
   * created, because docker materializes a missing bind source as a DIRECTORY —
   * `cat /mewbo/deadline` would then fail forever and the watchdog would kill
   * the container on its first tick.
   *
   * Cost: O(1).
   */
  private async create(
    request: FastifyRequest<{ Params: SessionParams }>,
    reply: FastifyReply,
  ): Promise<CommandResponse> {
    this.authorize(request);
    const sessionId = IdeRoutes.sessionId(request);
    const body = createIdeBody.parse(request.body);

    const workspacePath = this.allowlist.resolve(body.workspace_path);
    const createdAt = this.now();
    const expiresAt = new Date(createdAt.getTime() + body.ttl_seconds * 1000);

    this.deadlines.write(sessionId, expiresAt);
    await this.containers.create({
      sessionId,
      workspacePath,
      password: body.password,
      createdAt,
      expiresAt,
    });

    reply.code(201);
    return {};
  }

  /**
   * Status probe. Never 404s — "absent" is a status.
   *
   * A 404 would force the caller to distinguish "no container" from "the broker
   * is broken", and the cheap reading of that ambiguity is to treat both as
   * "not running" — a fail-open that hides an outage behind a normal-looking
   * empty state.
   *
   * Cost: O(1).
   */
  private async status(
    request: FastifyRequest<{ Params: SessionParams }>,
  ): Promise<IdeStatusResponse> {
    this.authorize(request);
    const sessionId = IdeRoutes.sessionId(request);

    return { status: await this.containers.inspect(sessionId) };
  }

  /**
   * Push the deadline forward.
   *
   * Deliberately does NOT require the container to exist: rewriting the file IS
   * the extension, since the watchdog re-reads it on its own 15s poll. Mirrors
   * the Python `extend()`, which writes the deadline before and independently
   * of any container check.
   *
   * Cost: O(1).
   */
  private async extend(
    request: FastifyRequest<{ Params: SessionParams }>,
  ): Promise<CommandResponse> {
    this.authorize(request);
    const sessionId = IdeRoutes.sessionId(request);
    const body = extendIdeBody.parse(request.body);

    this.deadlines.write(sessionId, new Date(this.now().getTime() + body.ttl_seconds * 1000));
    return {};
  }

  /**
   * Tear down. `removed` is true if EITHER the container was removed or the
   * deadline file existed, so a partially-created session still reports that
   * something was cleaned up.
   *
   * Cost: O(1).
   */
  private async remove(
    request: FastifyRequest<{ Params: SessionParams }>,
  ): Promise<DeleteIdeResponse> {
    this.authorize(request);
    const sessionId = IdeRoutes.sessionId(request);

    const removedContainer = await this.containers.removeBestEffort(sessionId);
    const removedFile = this.deadlines.clear(sessionId);
    return { removed: removedContainer || removedFile };
  }

  /**
   * Checked before the session id, so an unauthenticated caller learns nothing
   * about what the broker considers a well-formed request.
   */
  private authorize(request: FastifyRequest): void {
    const header = request.headers["x-broker-token"];
    const provided = Array.isArray(header) ? header[0] : header;
    if (typeof provided !== "string" || !IdeRoutes.tokensMatch(provided, this.config.token)) {
      throw BrokerError.unauthorized();
    }
  }

  private static tokensMatch(provided: string, expected: string): boolean {
    const left = Buffer.from(provided, "utf8");
    const right = Buffer.from(expected, "utf8");
    // `timingSafeEqual` throws on a length mismatch, so the length is compared
    // first. That leaks the secret's length and nothing else.
    if (left.length !== right.length) {
      return false;
    }
    return timingSafeEqual(left, right);
  }

  private static sessionId(request: FastifyRequest<{ Params: SessionParams }>): string {
    const raw = request.params.session_id;
    if (typeof raw !== "string" || !SESSION_ID_RE.test(raw)) {
      throw BrokerError.badRequest(`session_id must match ${SESSION_ID_RE.source}`);
    }
    return raw;
  }
}
