import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getConfig, getConfigSchema, patchConfig } from "../api/client";
import type { ConfigState, ConfigStorageStatus } from "../api/client";
import { logApiError } from "../utils/errors";

export type UseConfig = {
  schema: Record<string, unknown> | null;
  config: Record<string, unknown> | null;
  secrets: Record<string, boolean>;
  /** `null` once no GET has returned one yet (e.g. still loading) — treat as writable. */
  storage: ConfigStorageStatus | null;
  loading: boolean;
  saving: boolean;
  /** A failed schema/config LOAD — kept separate from `saveError` so the two never share one banner. */
  error: string | null;
  /** The most recent failed `savePatch` call's server-provided reason, cleared on the next attempt. */
  saveError: string | null;
  /**
   * Persist an ALREADY-COMPUTED section patch (e.g. from
   * `SettingsModel.patchFor`). Per-section diffing now lives in the model, so
   * this is a thin wrapper over the `patchConfig` mutation. Resolves the
   * updated `{config, secrets}` on success (the caller re-seeds only the saved
   * section so it goes non-dirty), or `null` on failure.
   */
  savePatch: (patch: Record<string, unknown>) => Promise<ConfigState | null>;
  refresh: () => Promise<void>;
};

const EMPTY_SECRETS: Record<string, boolean> = {};

export function useConfig(): UseConfig {
  const qc = useQueryClient();

  const schemaQ = useQuery({
    queryKey: ["config-schema"],
    queryFn: getConfigSchema,
    staleTime: Infinity,
  });
  const configQ = useQuery({ queryKey: ["config"], queryFn: getConfig });

  const patchM = useMutation({
    mutationFn: (patch: Record<string, unknown>) => patchConfig(patch),
    onSuccess: (updated: ConfigState) => {
      qc.setQueryData<ConfigState>(["config"], updated);
    },
  });

  const savePatch = async (
    patch: Record<string, unknown>
  ): Promise<ConfigState | null> => {
    try {
      return await patchM.mutateAsync(patch);
    } catch {
      return null;
    }
  };

  const refresh = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["config"] }),
      qc.invalidateQueries({ queryKey: ["config-schema"] }),
    ]);
  };

  const loadError = schemaQ.error || configQ.error;
  return {
    schema: schemaQ.data ?? null,
    config: configQ.data?.config ?? null,
    secrets: configQ.data?.secrets ?? EMPTY_SECRETS,
    storage: configQ.data?.storage ?? null,
    loading: schemaQ.isPending || configQ.isPending,
    saving: patchM.isPending,
    error: loadError ? logApiError("config", loadError) : null,
    saveError: patchM.error ? logApiError("config-save", patchM.error) : null,
    savePatch,
    refresh,
  };
}
