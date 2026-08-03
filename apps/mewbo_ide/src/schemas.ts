import { z } from "zod";

/** The same 32-hex session id the Python side has always validated. */
export const SESSION_ID_RE = /^[a-f0-9]{32}$/;

/** 60s floor, 7d ceiling. The API's own TTL policy sits above this. */
const ttlSeconds = z
  .number()
  .int()
  .min(60, "ttl_seconds must be at least 60")
  .max(604800, "ttl_seconds must be at most 604800");

/**
 * `.strict()` is the whole point of these schemas, not hygiene.
 *
 * It is what turns a caller smuggling an `image`, a `binds` list, an `env` map
 * or a `network` into a clean 400 instead of a field the broker silently
 * ignores — and "silently ignores" is indistinguishable from "quietly honours"
 * to whoever reads the code next. The broker accepts coordinates; a spec
 * fragment is a protocol error, and it says so.
 */
export const createIdeBody = z
  .object({
    workspace_path: z.string().min(1, "workspace_path is required"),
    ttl_seconds: ttlSeconds,
    password: z
      .string()
      .regex(/^[A-Za-z0-9_-]{16,128}$/, "password must be 16-128 chars of [A-Za-z0-9_-]"),
  })
  .strict();

export const extendIdeBody = z
  .object({
    ttl_seconds: ttlSeconds,
  })
  .strict();
