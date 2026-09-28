// Typed client for the library service (/api) and the proxied ComfyUI line_a routes (/comfy/line_a).

export type Status = "recommended" | "not_recommended" | "unverified";

export interface Coverage { ran: number; total: number; missing: string[] }
export interface QaResult {
  candidate: string;
  status: Status;
  recommended: boolean;
  checks: Record<string, boolean>;
  coverage?: Coverage;  // absent in legacy QA files
  failed_major: string[];
  failed_minor: string[];
  reasons: string[];
  warnings?: string[];
  summary: string;
  services: { vlm: string; mask: string };
}

export interface ValidationCheck { id: string; ok: boolean; detail: string }
export interface Attempt {
  id: string;
  state: string;
  created_at: string;
  updated_at?: string;
  index?: number;
  candidate?: string;
  candidate_set?: string;
  image_sha256?: string;
  qa_status?: Status | null;
  qa_override?: boolean;
  raw_from?: string;
  cleanup?: { remesh: boolean; drop_floaters: boolean };
  validated?: boolean;
  validation?: { ok: boolean; checks: ValidationCheck[] };
  mesh?: { triangles: number; vertices: number; components: number; has_uv: boolean;
    has_base_color_texture: boolean; file_size_bytes: number; removed_floater_components: number };
  triangles?: { requested: number | null; decimation_target: number; reason: string; actual: number };
  outputs?: Record<string, string | null>;
  params?: { cleanup?: { remesh: boolean; drop_floaters: boolean }; texture_size?: number };
  known_limitations?: string[];
  error?: string | null;
  failed_stage?: string | null;
}

export interface JobSummary {
  job_id: string;
  title: string;
  prompt: string;
  asset_type: string | null;
  state: string;
  stage: number;
  waiting: boolean;
  failed: boolean;
  action: string;
  active_operation: { name: string; started_at: string } | null;
  current_attempt: string | null;
  candidates: number;
  created_at: string | null;
}

export interface JobDetail {
  job_id: string;
  state: string;
  active_operation: { name: string; started_at: string } | null;
  current_attempt: string | null;
  history: { state: string; at: string; error?: string }[];
  request: { prompt: string; asset_type: string | null; target_triangles: number | null; candidate_count: number;
    seed_family: number; lora_name: string | null };
  enhancement: { enhanced_prompt: string; description: string; short_title: string; asset_tags: string[];
    camera_hint: string; meta: { model: { repo: string; revision: string }; seconds: number } } | null;
  candidate_set: { set_id: string; images: Record<string, string> } | null;
  candidates: string[];
  qa: Record<string, QaResult>;
  attempts: Attempt[];
  manifest: Record<string, unknown>;
  summary: JobSummary;
  slots: Record<string, string>;
}

export interface SlotRef { id: string; assigned: boolean; job_id?: string; attempt_id?: string }
export interface Family { id: string; name: string; variants: string; count: number; assigned: number; slots: SlotRef[] }
export interface BiomeTree {
  id: string; name: string; prefix: string;
  layers: { index: number; title: string; name: string; role: string; families: Family[] }[];
}
export interface BiomeSummary {
  id: string; name: string; prefix: string; order: number; catalog_estimate: number; base: number; assigned: number;
  families: number; layers: { index: number; name: string; families: number }[];
}

export interface SlotDetail {
  id: string; biome: string; biome_name: string; layer: number; layer_name: string; role: string; family: string;
  variants: string; facts: Record<string, string>;
  assignment: { job_id: string; attempt_id: string; assigned_at: string; glb: string; triangles: number | null } | null;
  manifest: Record<string, unknown> | null;
}

export interface AssetItem { job_id: string; attempt_id: string; title: string; triangles: number | null; glb: string;
  created_at: string; slot: string | null }

export interface QaCheckSpec { id: string; severity: "major" | "minor"; question: string; source: "vlm" | "mask" }
export interface StudioConfig {
  template: string;
  image: { speed_presets: Record<string, { steps: number }>; steps: number };
  mesh: { triangle_floor: number; texture_size: number };
  qa_checks: QaCheckSpec[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  const text = await r.text();
  if (!r.ok) {
    let msg = text;
    try { msg = (JSON.parse(text) as { detail?: string }).detail ?? text; } catch { /* plain text body */ }
    throw new ApiError(r.status, msg);
  }
  return (text ? JSON.parse(text) : null) as T;
}

export const fileUrl = (job: string, rel: string): string => `/api/files/${job}/${rel}`;
export const attemptFile = (job: string, att: string, rel: string): string =>
  fileUrl(job, `model/attempts/${att}/${rel}`);

export function approve(job: string, setId: string, index: number, sha: string, override: boolean) {
  return api<{ attempt: Attempt; created: boolean }>(`/comfy/line_a/jobs/${job}/approve`, {
    method: "POST", body: JSON.stringify({ set_id: setId, index, image_sha256: sha, override }),
  });
}
