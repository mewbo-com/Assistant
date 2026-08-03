/**
 * Persisted Q&A mode selection — fast (direct retrieval) vs deep (hypervisor
 * + probe fan-out). Sticky across navigations and reloads, same as
 * `useStoredModel`, but with no server-seeded default: the product default is
 * a fixed literal (`fast`), not something `/v1/wiki/defaults` resolves.
 */

import { useState } from "react";

import type { QaMode } from "./api/types";

const STORAGE_KEY = "wiki:qa-mode";

export function useStoredQaMode(): [QaMode, (next: QaMode) => void] {
  const [mode, setModeState] = useState<QaMode>(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved === "fast" || saved === "deep") return saved;
    } catch {
      // ignore
    }
    return "fast";
  });

  const setMode = (next: QaMode) => {
    setModeState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // ignore
    }
  };

  return [mode, setMode];
}
