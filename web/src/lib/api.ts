// Typed client for the Studio API. The browser never talks to ComfyUI or the filesystem.

export type Kind = "model3d" | "sprite" | "icon" | "vfx_flipbook" | "material" | "sprite_sheet" | "concept_art";
export type Origin = "generated" | "imported" | "derived" | "mixed";
export type QaStatus = "recommended" | "not_recommended" | "unverified";
export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };

export const KIND_LABEL: Record<Kind, string> = {
  model3d: "3D model", sprite: "Sprite", icon: "Icon", vfx_flipbook: "VFX flipbook", material: "Material",
  sprite_sheet: "Sprite sheet", concept_art: "Concept art",
};
export const KINDS = Object.keys(KIND_LABEL) as Kind[];

export interface ProjectRow { id: string; name: string; root: string; open: boolean; read_only: boolean | null }
export interface Summary {
  id: string; name: string; read_only: boolean; simulated: boolean; owner: Record<string, string>;
  storage: { backend: string; state: string; root: string };
  counts: { assets: number; planned: number; shots: number; jobs: number; batches: number; active_batches: number;
    categories: number; recipes: number };
  waiting: { jobs: number; items: number; by_gate: Record<string, number>;
    detail: { job_id: string; alias: string; next_action: string }[] };
}
export interface CategoryNode {
  id: string; label: string; slug: string; path: string; parent_id: string | null; depth: number;
  kind: Kind | null; kind_source: string; count: number;
}
export interface AssetRow {
  asset_id: string; name_id: string; display_name: string; kind: Kind; origin: Origin; category_id: string | null;
  tags: string[]; current_version_id: string | null; display_version: number | null; version_count: number;
  preview_artifact_id: string | null; kind_label: string;
}
export interface ShotRow {
  id: string; name: string; category_id: string | null; kind: Kind | null; brief: string;
  priority: "low" | "med" | "high"; notes: string; target_asset_id: string | null; external_id: string | null;
  archived: boolean; status: "planned" | "in_batch" | "published" | "archived"; effective_kind: Kind | null;
  membership: { batch_id: string; batch_alias: string; item_id: string; published: Published | null } | null;
}
export interface AssetList { items: AssetRow[]; total: number; planned: ShotRow[]; planned_total: number;
  all_assets_total: number }
export interface VersionRef { version_id: string; display_version: number; published_at: string;
  publication_op: string; preview_artifact_id: string | null; note: string }
export interface Manifest {
  asset_id: string; name_id: string; display_name: string; kind: Kind; origin: Origin; category_id: string | null;
  tags: string[]; created_at: string; current_version_id: string | null; versions: VersionRef[]; revision: number;
}
export interface FileRef { role: string; artifact_id: string; sha256: string; size: number; mime: string }
export interface AssetVersion {
  version_id: string; display_version: number; origin: Origin; sources: Record<string, Json>;
  licence: { status: string; components?: { id: string; name: string; licence: string; status: string }[];
    note?: string };
  validation: Record<string, Json>; qa: Record<string, Json> | null; publication: Record<string, Json>;
  parameters: Record<string, Json>; note: string; config_snapshot_sha: string | null;
}
export interface Fact { key: string; value: Json; mode: string; source: string }
export interface AssetDetail {
  manifest: Manifest; manifest_json: string; kind_label: string; category_label: string | null;
  shown_version: AssetVersion; is_current: boolean; facts: Fact[]; files: FileRef[];
}
export interface Published { asset_id: string; version_id: string; display_version: number }
export interface Task { op_id: string; state: string; error: string | null; progress: { done?: number; total?: number } }
export interface CheckResult { rule_id: string; source: string; severity: "major" | "minor";
  result: "pass" | "fail" | "unavailable" | "not_applicable"; reason: string; observed: Json; threshold: Json }
export interface QaView { id: string; status: QaStatus; coverage: { completed: number; applicable: number };
  results: CheckResult[]; not_evaluated: boolean;
  policy: { failed_major: string[]; failed_minor: string[]; unavailable: string[]; disabled: string[] } }
export interface CandidateView { id: string; index: number; artifact_id: string; sha256: string; seed: number;
  width: number; height: number; qa: QaView | null }
export interface PromptRev { id: string; number: number; origin: string; description: string; template: string;
  positive: string; negative: string; original_brief: string; enhancer: Record<string, Json> | null }
export interface BuildRunView { id: string; status: string; result: "valid" | "invalid" | "validation_unavailable" | null;
  artifacts: Record<string, string>; inputs: Record<string, Json>; kind: "build" | "reexport" | "repair";
  derived_from: string | null; preview: "pending" | "available" | "failed" | "unsupported" | null;
  preview_error: string | null; checkpoints: Record<string, { stage: string; committed_at: string }>;
  validation: { ok?: boolean; checks?: { id: string; ok: boolean; detail?: string; advisory?: boolean }[];
    failure_code?: string; failed_stage?: string };
  error: string | null }
export interface BuildHistoryRow { id: string; status: string; result: string | null; kind: string; error: string | null;
  derived_from: string | null; created_at: string; preview: string | null; checkpoints: string[]; has_raw: boolean;
  accepted: boolean; current: boolean }
export interface ItemView {
  id: string; name: string; brief: string; revision: number; category_id: string | null; shot_id: string | null;
  target_asset_id: string | null; current_prompt: string | null; prompt_confirmed: string | null;
  current_set: string | null; candidate_sets: string[]; approval: string | null; regen_requested: boolean;
  current_build: string | null; accepted_build: string | null; published: Published | null; cancelled: boolean;
  tasks: Record<string, Task>; qa: Record<string, string>;
  stage: { stage: string; state: string; waiting_on_user: boolean; busy: boolean; failed: boolean };
  prompt: PromptRev | null; prompt_locked: boolean;
  candidate_set: { id: string; number: number; prompt_revision_id: string; requested: number;
    generation: Record<string, Json>; candidates: CandidateView[] } | null;
  approval_detail: { id: string; bound: Record<string, Json>; qa_status: string | null; override_qa: boolean;
    failed_checks: string[]; missing_checks: string[] } | null;
  build: BuildRunView | null; build_history: BuildHistoryRow[]; job_id: string;
  legal: Record<"edit_prompt" | "enhance" | "confirm" | "approve" | "mark_regenerate" | "build" | "accept" | "publish"
    | "retry_preview", boolean>;
}
export interface Counts { items: number; prompts: number; confirmed: number; candidates: number; approved: number;
  regenerate: number; built: number; accepted: number; published: number; busy: number; failed: number;
  cancelled: number }
/** A Job: a configured production workflow of one or more items (formerly called a batch). */
export interface JobSummary {
  id: string; alias: string; title: string; kind: Kind; kind_label: string; recipe_id: string;
  category_id: string | null; category_label: string | null; created_at: string; source: string; counts: Counts;
  by_stage: Record<string, number>; current_tab: string; waiting_on_user: boolean; next_action: string;
  active_run: string | null; legacy: boolean; legacy_recipe: string | null;
}
export interface Stage { tag: string; name: string; backend: string }
export interface JobDetail extends JobSummary {
  seed_family: number; config_revision: number; locked_template: string; items: ItemView[];
  recipe: { id: string; label: string; build_label: string; build_available: boolean; build_blocked_reason: string;
    generation_available: boolean; generation_blocked_reason: string; stages: Stage[] };
}
export interface RunCounts { jobs: number; items: number; enhanced: number; enhance_failed: number;
  prompts_confirmed: number; prompts_waiting: number; candidates_ready: number; approved: number; undecided: number;
  builds_valid: number; builds_invalid: number; builds_failed: number; accepted: number; published: number;
  active_tasks: number; failed_tasks: number; paused_tasks: number }
export interface RunSummary { id: string; batch_id: string | null; plan_id: string; created_at: string;
  closed_at: string | null; stop_at: string; status: string; counts: RunCounts; waves: string[]; job_ids: string[] }
/** A Batch: a named group of Jobs scheduled together. It owns no item content. */
export interface BatchGroup { id: string; alias: string; title: string; job_ids: string[]; kinds: Kind[]; jobs: number;
  items: number; revision: number; created_at: string; updated_at: string; runs: string[];
  latest_run: RunSummary | null }
export interface BatchGroupDetail extends BatchGroup { jobs_detail: JobSummary[]; run_history: RunSummary[] }
export interface PlanItem { item_id: string; name: string; revision: number; action: "enhance" | "at_gate" | "excluded" | "done";
  reason: string }
export interface RunPlan { plan_id: string; plan_sha256: string; batch_id: string | null; batch_revision: number | null;
  stop_at: string; jobs: { job_id: string; title: string; kind: Kind; recipe_id: string; items: PlanItem[] }[];
  residency_groups: Record<string, number>; preflight: Record<string, string>;
  counts: { jobs: number; items: number; enhance: number; at_gate: number; excluded: number; done: number } }
export interface StageTask { id: string; job_id: string; item_id: string; run_id: string | null; stage: string;
  family: string; lane: string; residency: string; state: string; control: string; attempts: number;
  error: { code: string; message: string; retryable?: boolean } | null; created_at: string; updated_at: string }
export interface ModelPass { id: string; lane: string; residency: string; worker: string | null; task_ids: string[];
  jobs: string[]; started_at: string; ended_at: string | null; close_reason: string | null;
  measured: { model_loads: Record<string, number> | null; switched?: boolean; previous_residency?: string | null } }
export interface RunDetail extends RunSummary { jobs: JobDetail[]; passes: ModelPass[]; tasks: StageTask[] }
export interface Operation { id: string; kind: string; state: string; lane: string; batch_id: string | null;
  progress: Record<string, Json>; error: { code: string; message: string; retryable?: boolean } | null;
  created_at: string; updated_at: string }
export interface Readiness { state: string; reason: string; missing?: string[] }
export interface Param { key: string; type: string; default: Json; note: string; min: number | null;
  max: number | null; choices: string[] }
export interface RecipeInfo { id: string; kind: Kind; label: string; version: number; generation: Readiness;
  build: Readiness; qa: Readiness; stages: Stage[]; params: Param[]; template: string; negative: string }
export interface ModelRow { key: string; repo: string; revision: string; status: string; ready: boolean;
  licence: string; licence_status: string; roles: string[]; optional: boolean; gated: boolean; detail: string;
  bytes_expected: number; full_verified: boolean }
export interface Runtime {
  simulated: boolean; engine_mode: string;
  gpus: { index: string; uuid: string; name: string; vram_used_mb: number; vram_total_mb: number; util_pct: number;
    measured_at: string; lane: string | null;
    ownership: { owner: string | null; state: string; last_error: string | null; workers: string[] } | null }[];
  services: { name: string; role: string; url: string; reachable: boolean; ready: boolean; problems: string[];
    version?: string | null; loaded?: Record<string, boolean> }[];
  lanes: Record<string, { lane: string; owner: string | null; state: string; last_error: string | null }>;
  coordinator: { lanes: Record<string, { running: string | null; queued: number; blocked: number }>;
    passes: ModelPass[] } | null;
  models: ModelRow[]; licences: { id: string; name: string; licence: string; status: string; note?: string }[];
  recipes: RecipeInfo[];
}
export interface ConfigView {
  config: StudioConfig; yaml: string; revision: number; categories: CategoryNode[];
  effective: Record<string, Record<string, { value: Json; mode: string; source: string }> & {
    _naming_examples?: string[] }>;
}
export interface Override { mode: "inherit" | "value" | "disabled"; value: Json }
export interface CategoryCfg { id: string; parent_id: string | null; slug: string; label: string; archived: boolean;
  defaults: Record<string, Override>; metadata: Record<string, string> }
export interface QaRuleCfg { id: string; source: string; stage: string; enabled: boolean; severity: "major" | "minor";
  question: string | null; metric: string | null; params: Record<string, Json> }
export interface PaletteColor { hex: string; label: string; reserved: boolean; allowed_kinds: Kind[];
  allowed_categories: string[]; tolerance_delta_e: number }
export interface StudioConfig {
  schema_version: number; project: { id: string; name: string }; revision: number;
  defaults: Record<string, Override>; categories: CategoryCfg[];
  pipelines: Record<string, { recipe_version: number; parameters: Record<string, Json>; template: string | null }>;
  qa_rulesets: Record<string, { label: string; kind: Kind | null; rules: QaRuleCfg[]; policy: { minor_fail_limit: number } }>;
  styles: Record<string, { label: string; guide: string; negative: string; palette: PaletteColor[] }>;
  reference_sets: Record<string, { label: string; mode: string; images: { artifact_id: string; label: string;
    role: string; source_rights: string }[] }>;
  export_presets: Record<string, Json>;
  retention: Record<"rejected_candidates" | "raw_intermediates" | "full_logs",
    { keep: boolean; expire_after_days: number | null }>;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail: Json | undefined) {
    super(message);
  }
}

const MUTATION_HEADERS = { "Content-Type": "application/json", "X-AssetStudio": "1" };

async function parse<T>(r: Response): Promise<T> {
  const text = await r.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* non-JSON error body */ }
  if (!r.ok) {
    const err = (body as { error?: { code: string; message: string; detail?: Json } } | null)?.error;
    throw new ApiError(r.status, err?.code ?? "http_error", err?.message ?? `HTTP ${r.status}`, err?.detail);
  }
  return body as T;
}

export async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(path, { headers: { Accept: "application/json" } }));
}

export async function send<T>(method: "POST" | "PUT" | "PATCH", path: string, body?: unknown): Promise<T> {
  return parse<T>(await fetch(path, { method, headers: MUTATION_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body) }));
}

export async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return parse<T>(await fetch(path, { method: "POST", headers: { "X-AssetStudio": "1" }, body: form }));
}

export async function uploadMany<T>(path: string, files: File[]): Promise<T> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  return parse<T>(await fetch(path, { method: "POST", headers: { "X-AssetStudio": "1" }, body: form }));
}

export const key = (): string => crypto.randomUUID();
export const P = (project: string) => `/api/v1/projects/${project}`;
export const V2 = (project: string) => `/api/v2/projects/${project}`;
export const J = (project: string) => `${V2(project)}/jobs`;
export const artifactUrl = (project: string, artifactId: string) => `${P(project)}/artifacts/${artifactId}/content`;
