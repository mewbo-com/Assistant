/**
 * Runtime config injected by nginx at container start
 * (see ``docker/console-entrypoint.sh``).
 *
 * Accepts either ``__MEESEEKS_CONFIG__`` or ``__MEWBO_CONFIG__`` —
 * whichever the running container's entrypoint wrote wins, since not
 * every deployed image runs the same entrypoint version.
 *
 * Single source of truth so every consumer reads the same thing.
 */

interface RuntimeConfig {
  VITE_API_BASE_URL?: string;
  VITE_API_KEY?: string;
  VITE_API_USE_PROXY?: string;
}

export function readRuntimeConfig(): RuntimeConfig | undefined {
  const win = window as unknown as Record<string, unknown>;
  return (
    (win.__MEWBO_CONFIG__ as RuntimeConfig | undefined) ??
    (win.__MEESEEKS_CONFIG__ as RuntimeConfig | undefined)
  );
}

export type { RuntimeConfig };
