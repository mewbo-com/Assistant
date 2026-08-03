import Docker from "dockerode";

import { BrokerConfig } from "./config.js";
import type { DockerClientLike } from "./containers.js";
import { BrokerError } from "./errors.js";
import { BrokerServer } from "./server.js";

try {
  // Fail fast and loudly: an unset token or an empty allowlist must stop the
  // process here, not degrade into a broker that accepts every path.
  const config = BrokerConfig.fromEnv();

  // The single untyped boundary in this service. dockerode's surface is far
  // wider than the four calls used, so it is narrowed to `DockerClientLike`
  // immediately and nothing downstream ever sees the raw client.
  const docker = new Docker({ socketPath: config.dockerSocket }) as unknown as DockerClientLike;

  const server = BrokerServer.create({ config, docker });
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => {
      void server.close();
    });
  }

  await server.sweep();
  await server.listen();
} catch (err) {
  console.error(`ide-broker: refusing to start: ${BrokerError.from(err).reason}`);
  process.exit(1);
}
