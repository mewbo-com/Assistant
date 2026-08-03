import { useMutation, useQueryClient } from "@tanstack/react-query";
import { rebindSessionProject } from "../api/client";
import { SessionSpecResponse } from "../types";

/**
 * Durable project rebind — `PUT /api/sessions/<id>/project`. The one
 * sanctioned way to change a purpose-bound session's project: the server
 * refuses the per-turn override (`SessionSpec.field_editable` — see
 * `useSessionSpec`'s `editable.project`), so a picker that only wrote local
 * composer state would silently discard the pick. This mutation persists the
 * change instead and writes the response straight onto the `session-spec`
 * cache entry, so `useSessionSpec(sessionId)` reflects the new binding without
 * an extra round trip.
 *
 * `project` is a required, non-empty string — the route only ever BINDS.
 * There is no unbind verb (an empty name would null the session's `cwd`,
 * sending the next turn into an empty temp dir), so callers pick from the
 * project list, never "Temporary directory", while a session is locked.
 *
 * `AUTO_PROJECT` ("auto") is a legitimate value here and NOT the missing unbind:
 * it is a non-empty key naming a mode, so it binds like any other. Nothing is
 * validated client-side — the server decides which keys a given session may
 * rebind to, and its refusal surfaces through `ConfigMenu`'s inline error.
 */
export function useRebindProject(sessionId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (project: string) => {
      if (!sessionId) {
        // Unreachable from the UI — the rebind affordance only renders for a
        // session already open in detail mode — but keeps the mutation typed
        // without a non-null assertion at the call site.
        return Promise.reject(new Error("no session id"));
      }
      return rebindSessionProject(sessionId, project);
    },
    onSuccess: (response: SessionSpecResponse) => {
      qc.setQueryData(["session-spec", response.session_id], response);
    },
  });
}
