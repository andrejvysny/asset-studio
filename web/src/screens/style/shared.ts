import type { StudioConfig } from "../../lib/api";

export interface DraftProps { draft: StudioConfig; setDraft: (n: StudioConfig) => void }
export const STYLE_ID_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
