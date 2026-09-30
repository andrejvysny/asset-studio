// Planned-effects endpoint (v1 project scope).
import { type EffectsView, get, type Kind, P } from "./api";

/** Reflects the SAVED config; `style` previews that (saved) style instead of the resolved one. Empty `categoryId` = project defaults (needs `kind`). 422 `unresolved` if no kind resolves. */
export const getConfigEffects = (project: string, q: { categoryId: string; kind: Kind | ""; style?: string }) => {
  const p = new URLSearchParams();
  if (q.categoryId) p.set("category_id", q.categoryId);
  if (q.kind) p.set("kind", q.kind);
  if (q.style) p.set("style", q.style);
  return get<EffectsView>(`${P(project)}/config:effects?${p}`);
};
