import { ZodError } from "zod";

/**
 * Wire codes carried in the error envelope.
 *
 * Exactly ONE code changes what the caller does: `workspace_denied` becomes a
 * `ValueError` carrying the reason, which the API's extend route renders as a
 * 400. Every other code — `docker_error` and `docker_unavailable` included —
 * maps onto the same `DockerUnavailable`, so the difference between them is
 * DIAGNOSTIC: it survives into the exception message and therefore into a log,
 * and no consumer branches on it.
 */
export type BrokerErrorCode =
  | "bad_request"
  | "unauthorized"
  | "not_found"
  | "workspace_denied"
  | "docker_error"
  | "docker_unavailable"
  | "internal_error";

export interface BrokerErrorBody {
  error: {
    code: BrokerErrorCode;
    reason: string;
    retryable: boolean;
  };
}

/**
 * A refusal that knows its own status, wire code and retryability.
 *
 * Every non-2xx the broker emits is one of these. Handlers throw; the single
 * error hook registered by `BrokerServer` renders. That is what lets a refusal
 * travel up out of `WorkspaceAllowlist.resolve` or `IdeContainers.create`
 * without each caller threading it back as a sentinel value.
 */
export class BrokerError extends Error {
  readonly status: number;
  readonly code: BrokerErrorCode;
  readonly reason: string;
  readonly retryable: boolean;

  constructor(status: number, code: BrokerErrorCode, reason: string, retryable = false) {
    super(`${code}: ${reason}`);
    this.name = "BrokerError";
    this.status = status;
    this.code = code;
    this.reason = reason;
    this.retryable = retryable;
  }

  body(): BrokerErrorBody {
    return { error: { code: this.code, reason: this.reason, retryable: this.retryable } };
  }

  static badRequest(reason: string): BrokerError {
    return new BrokerError(400, "bad_request", reason, false);
  }

  static unauthorized(): BrokerError {
    // Deliberately says nothing about which half was wrong — a missing header
    // and a wrong secret are indistinguishable to the caller.
    return new BrokerError(401, "unauthorized", "missing or invalid X-Broker-Token", false);
  }

  static notFound(reason: string): BrokerError {
    return new BrokerError(404, "not_found", reason, false);
  }

  static workspaceDenied(reason: string): BrokerError {
    return new BrokerError(403, "workspace_denied", reason, false);
  }

  /** The daemon answered and refused. Reads as 502 in a log; the caller treats it as any other docker failure. */
  static dockerError(reason: string): BrokerError {
    return new BrokerError(502, "docker_error", reason, false);
  }

  /** The daemon could not be reached at all. Same caller handling as `dockerError`; the code is what tells them apart in a log. */
  static dockerUnavailable(reason: string): BrokerError {
    return new BrokerError(503, "docker_unavailable", reason, true);
  }

  /**
   * Normalize any thrown value into a renderable refusal.
   *
   * A Zod failure is a 400 rather than a 500 because every schema in this
   * service guards a request body — `.strict()` rejecting a smuggled field is
   * the caller's bug, not the broker's.
   */
  static from(err: unknown): BrokerError {
    if (err instanceof BrokerError) {
      return err;
    }
    if (err instanceof ZodError) {
      return BrokerError.badRequest(BrokerError.describeZod(err));
    }
    const reason = err instanceof Error ? err.message : String(err);
    // A framework-level 4xx (an unparseable JSON body, an unsupported content
    // type) is still the caller's bug — it must not read as a broker fault.
    const status = (err as { statusCode?: number }).statusCode;
    if (typeof status === "number" && status >= 400 && status < 500) {
      return BrokerError.badRequest(reason);
    }
    return new BrokerError(500, "internal_error", reason, false);
  }

  private static describeZod(err: ZodError): string {
    return err.issues
      .map((issue) => {
        const at = issue.path.join(".");
        return at.length > 0 ? `${at}: ${issue.message}` : issue.message;
      })
      .join("; ");
  }
}
