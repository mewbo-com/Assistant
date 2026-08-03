import path from "node:path";

import { z } from "zod";

/** `1g` / `512m` — the same spelling `agent.web_ide.memory` already uses. */
const MEMORY_RE = /^(\d+)([mgMG])$/;

const MIB = 1024 * 1024;
const GIB = 1024 * MIB;

/**
 * A configured path prefix that lives in a docker NAMED VOLUME rather than on
 * the host filesystem — `MEWBO_IDE_VOLUME_ROOTS` maps one to the other so
 * `IdeContainers.buildSpec` can mount a volume-backed workspace by volume
 * (with a subpath) instead of by bind. `root` is kept as the raw configured
 * string here, uncanonicalized — exactly how `allowedRoots` is kept — because
 * canonicalization needs a live filesystem read and belongs where it's used
 * (`WorkspaceAllowlist`, `IdeContainers`), not at config-parse time.
 */
export interface VolumeRoot {
  readonly root: string;
  readonly volume: string;
}

/**
 * The broker's whole configuration surface, validated at boot.
 *
 * Deliberately NOT `.strict()`, unlike the two request schemas: an
 * unrecognised key is stripped, not refused. `.env` is `env_file` for
 * both this service and the API, and the API legitimately owns
 * `MEWBO_IDE_BROKER_URL` there — refusing every unknown `MEWBO_IDE_*` var
 * would make the broker fail to boot on the API's own setting.
 */
const brokerEnvSchema = z
  .object({
    MEWBO_IDE_BROKER_TOKEN: z
      .string()
      .min(16, "MEWBO_IDE_BROKER_TOKEN must be at least 16 characters"),
    MEWBO_IDE_ALLOWED_ROOTS: z
      .string()
      .min(1, "MEWBO_IDE_ALLOWED_ROOTS must not be empty")
      .superRefine((raw, ctx) => {
        try {
          BrokerConfig.parseRoots(raw);
        } catch (err) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: err instanceof Error ? err.message : String(err),
          });
        }
      }),
    // Empty by default: most deployments have no volume-backed workspace root
    // at all, and an empty allowlist-of-one-more-thing is a no-op, not a gap.
    MEWBO_IDE_VOLUME_ROOTS: z
      .string()
      .default("")
      .superRefine((raw, ctx) => {
        try {
          BrokerConfig.parseVolumeRoots(raw);
        } catch (err) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: err instanceof Error ? err.message : String(err),
          });
        }
      }),
    // 0.0.0.0 INSIDE the container, deliberately. Container-loopback is a
    // private namespace: docker forwards a published port to the container's
    // bridge IP and never to its loopback, and a sibling container resolves
    // that same bridge IP. Binding 127.0.0.1 here would leave the broker
    // listening where nothing can reach it — while the healthcheck, which runs
    // inside the container, still passed. The "never off-host" property comes
    // from the compose publish (`127.0.0.1:5128:5128`) and the shared secret on
    // every /v1 route, not from this address.
    MEWBO_IDE_BROKER_HOST: z.string().min(1).default("0.0.0.0"),
    MEWBO_IDE_BROKER_PORT: z.coerce.number().int().min(1).max(65535).default(5128),
    MEWBO_IDE_STATE_DIR: z.string().min(1).default("/tmp/mewbo-ide"),
    MEWBO_IDE_IMAGE: z.string().min(1).default("codercom/code-server:latest"),
    MEWBO_IDE_NETWORK: z.string().min(1).default("mewbo-ide"),
    MEWBO_IDE_MEMORY: z
      .string()
      .superRefine((raw, ctx) => {
        try {
          BrokerConfig.parseMemory(raw);
        } catch (err) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: err instanceof Error ? err.message : String(err),
          });
        }
      })
      .default("1g"),
    MEWBO_IDE_CPUS: z.coerce
      .number()
      .positive("MEWBO_IDE_CPUS must be greater than zero")
      .default(1.0),
    MEWBO_IDE_PIDS_LIMIT: z.coerce
      .number()
      .int()
      .positive("MEWBO_IDE_PIDS_LIMIT must be a positive integer")
      .default(512),
    DOCKER_SOCKET: z.string().min(1).default("/var/run/docker.sock"),
  });

type BrokerEnv = z.infer<typeof brokerEnvSchema>;

/**
 * Boot-validated broker settings, plus the two derived quantities the docker
 * API wants in units nobody configures in.
 */
export class BrokerConfig {
  readonly token: string;
  readonly allowedRoots: readonly string[];
  readonly volumeRoots: readonly VolumeRoot[];
  readonly host: string;
  readonly port: number;
  readonly stateDir: string;
  readonly image: string;
  readonly network: string;
  readonly memory: string;
  readonly cpus: number;
  readonly pidsLimit: number;
  readonly dockerSocket: string;

  private constructor(env: BrokerEnv) {
    this.token = env.MEWBO_IDE_BROKER_TOKEN;
    this.allowedRoots = Object.freeze(BrokerConfig.parseRoots(env.MEWBO_IDE_ALLOWED_ROOTS));
    this.volumeRoots = Object.freeze(BrokerConfig.parseVolumeRoots(env.MEWBO_IDE_VOLUME_ROOTS));
    this.host = env.MEWBO_IDE_BROKER_HOST;
    this.port = env.MEWBO_IDE_BROKER_PORT;
    this.stateDir = path.normalize(env.MEWBO_IDE_STATE_DIR);
    this.image = env.MEWBO_IDE_IMAGE;
    this.network = env.MEWBO_IDE_NETWORK;
    this.memory = env.MEWBO_IDE_MEMORY;
    this.cpus = env.MEWBO_IDE_CPUS;
    this.pidsLimit = env.MEWBO_IDE_PIDS_LIMIT;
    this.dockerSocket = env.DOCKER_SOCKET;
  }

  /**
   * Parse and validate. Throws a `ZodError` naming every offending variable,
   * which the entrypoint prints before exiting non-zero.
   */
  static fromEnv(env: Record<string, string | undefined> = process.env): BrokerConfig {
    return new BrokerConfig(brokerEnvSchema.parse(env));
  }

  /** Memory limit in bytes, the unit `HostConfig.Memory` is expressed in. */
  memoryBytes(): number {
    return BrokerConfig.parseMemory(this.memory);
  }

  /** CPU limit in nano-CPUs, the unit `HostConfig.NanoCpus` is expressed in. */
  nanoCpus(): number {
    return Math.round(this.cpus * 1e9);
  }

  /**
   * Split the colon-separated roots list.
   *
   * A relative entry is refused rather than resolved against the process cwd:
   * the allowlist is a containment boundary, and silently anchoring it to
   * whatever directory the container happened to start in is the fail-open
   * shape this whole service exists to close.
   */
  static parseRoots(raw: string): string[] {
    const entries = raw
      .split(":")
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
    if (entries.length === 0) {
      throw new Error("MEWBO_IDE_ALLOWED_ROOTS must name at least one absolute path");
    }
    const relative = entries.filter((entry) => !path.isAbsolute(entry));
    if (relative.length > 0) {
      throw new Error(
        `MEWBO_IDE_ALLOWED_ROOTS entries must be absolute paths: ${relative.join(", ")}`,
      );
    }
    return entries.map((entry) => {
      const normalized = path.normalize(entry).replace(/\/+$/, "");
      return normalized.length > 0 ? normalized : path.sep;
    });
  }

  /**
   * Parse `path=volume[,path=volume...]`.
   *
   * Empty input is valid and means "no volume roots configured" — the feature
   * is entirely optional. Each root must be absolute for the same reason
   * `parseRoots` refuses a relative one: a containment/lookup boundary
   * anchored to the process's own cwd is a fail-open in a different costume.
   */
  static parseVolumeRoots(raw: string): VolumeRoot[] {
    const entries = raw
      .split(",")
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
    return entries.map((entry) => {
      const eq = entry.indexOf("=");
      if (eq <= 0 || eq === entry.length - 1) {
        throw new Error(
          `MEWBO_IDE_VOLUME_ROOTS entries must be path=volume, got: ${entry}`,
        );
      }
      const root = entry.slice(0, eq).trim();
      const volume = entry.slice(eq + 1).trim();
      if (!path.isAbsolute(root)) {
        throw new Error(`MEWBO_IDE_VOLUME_ROOTS root must be an absolute path: ${root}`);
      }
      return { root: path.normalize(root).replace(/\/+$/, "") || path.sep, volume };
    });
  }

  /** `512m` / `2g` to bytes. Zero is refused — docker reads it as "unlimited". */
  static parseMemory(raw: string): number {
    const match = MEMORY_RE.exec(raw);
    if (match === null) {
      throw new Error(`memory must match ${MEMORY_RE.source} (for example 512m or 1g): ${raw}`);
    }
    const amount = Number(match[1]);
    const unit = (match[2] ?? "").toLowerCase();
    const bytes = unit === "g" ? amount * GIB : amount * MIB;
    if (bytes <= 0) {
      throw new Error(`memory must be greater than zero: ${raw}`);
    }
    return bytes;
  }
}
