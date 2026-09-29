import { createContext, useCallback, useContext } from "react";

import { type ConfigView, P, send, type StudioConfig, type Summary } from "./api";
import { type Loaded, useApi } from "./hooks";

export interface ProjectCtx { id: string; summary: Loaded<Summary> }
export const ProjectContext = createContext<ProjectCtx | null>(null);

export function useProject(): ProjectCtx {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("no project selected");
  return ctx;
}

/** Project configuration + a save that always names the revision it was based on (409 on conflict). */
export function useConfig(): Loaded<ConfigView> & { save: (next: StudioConfig) => Promise<ConfigView> } {
  const { id } = useProject();
  const loaded = useApi<ConfigView>(`${P(id)}/config`, { project: id });
  const { reload } = loaded;
  const save = useCallback(async (next: StudioConfig) => {
    const out = await send<ConfigView>("PATCH", `${P(id)}/config`, { expected_revision: next.revision, config: next });
    reload();
    return out;
  }, [id, reload]);
  return { ...loaded, save };
}

export const clone = <T,>(v: T): T => structuredClone(v);
