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
  /** Library index row extras. `family_*` are null for ungrouped assets. `search` is the server's search text. */
  updated_at: string; search: string; family_id: string | null; family_name: string | null;
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
  schema_version: number; family_id: string | null;
  pointer_log: { from_version: string | null; to_version: string; at: string; op: string; reason: string }[];
}
export interface FileRef { role: string; artifact_id: string; sha256: string; size: number; mime: string }
export interface AssetVersion {
  version_id: string; display_version: number; origin: Origin; sources: Record<string, Json>;
  licence: { status: string; components?: { id: string; name: string; licence: string; status: string }[];
    note?: string };
  validation: Record<string, Json>; qa: Record<string, Json> | null; publication: Record<string, Json>;
  parameters: Record<string, Json>; note: string; config_snapshot_sha: string | null;
  /** Lineage of a variant version; null for every other version. */
  derivation: Derivation | null;
  schema_version: number; asset_id: string; project_id: string; kind: Kind; display_name: string;
  category_id: string | null; tags: string[]; artifacts: Record<string, Omit<FileRef, "role">>;
  models: { key: string; repo: string; revision: string; files: Record<string, { size: number; sha256: string }> }[];
  engine: Record<string, Json>;
}
export interface Fact { key: string; value: Json; mode: string; source: string }
export interface AssetDetail {
  manifest: Manifest; manifest_json: string; kind_label: string; category_label: string | null;
  shown_version: AssetVersion; is_current: boolean; facts: Fact[]; files: FileRef[];
  family_id: string | null; family_name: string | null;
  /** The family record (no member count here; use GET /families/{id} for total_member_count). */
  family: FamilyRecord | null;
  /** Every version of the asset with its derivation (null for non-variant versions). */
  versions: AssetVersionLineage[];
  /** Summary of the exact source recorded at publication of the SHOWN version; null if not a variant. */
  derived_from: DerivedFrom | null;
}
export interface Published { asset_id: string; version_id: string; display_version: number;
  /** The accepted build this publication committed; null/absent on publications recorded before variants. */
  build_run_id?: string | null }
export interface Task { op_id: string; state: string; error: string | null; progress: { done?: number; total?: number } }
export interface CheckResult { rule_id: string; source: string; severity: "major" | "minor";
  result: "pass" | "fail" | "unavailable" | "not_applicable"; reason: string; observed: Json; threshold: Json;
  evaluator?: string }
export interface QaView { id: string; status: QaStatus; coverage: { completed: number; applicable: number };
  results: CheckResult[]; not_evaluated: boolean;
  policy: { failed_major: string[]; failed_minor: string[]; unavailable: string[]; disabled: string[];
    status?: QaStatus; not_evaluated?: boolean; coverage?: { completed: number; applicable: number };
    minor_fail_limit?: number } }
export interface CandidateView { id: string; index: number; artifact_id: string; sha256: string; seed: number;
  width: number; height: number; qa: QaView | null; engine?: Record<string, Json> }
export interface PromptRev { id: string; number: number; origin: string; description: string; template: string;
  positive: string; negative: string; original_brief: string; enhancer: Record<string, Json> | null;
  item_id: string; parent_id: string | null; created_at: string; style_sha: string | null; snapshot_sha: string;
  /** What the revision was written against. All keys optional: revisions before variants carry `{}`. */
  bindings: PromptBindings }
export interface BuildRunView { id: string; status: string; result: "valid" | "invalid" | "validation_unavailable" | null;
  artifacts: Record<string, string>; inputs: Record<string, Json>; kind: "build" | "reexport" | "repair";
  derived_from: string | null; preview: "pending" | "available" | "failed" | "unsupported" | null;
  preview_error: string | null; checkpoints: Record<string, BuildCheckpoint>;
  validation: { ok?: boolean; checks?: { id: string; ok: boolean; detail?: string; advisory?: boolean }[];
    required?: string[]; failure_code?: string; failed_stage?: string };
  error: string | null;
  /** Build kind: "model3d" | "direct_glb" | "direct_raster" | image recipes... */
  build?: string; job_id?: string; item_id?: string; created_at?: string; updated_at?: string;
  op_id?: string | null; executions?: Record<string, string[]> }
export interface BuildHistoryRow { id: string; status: string; result: string | null; kind: string; error: string | null;
  derived_from: string | null; created_at: string; preview: string | null; checkpoints: string[]; has_raw: boolean;
  accepted: boolean; current: boolean;
  /** How the run was requested. "reexport" for re-export runs. */
  mode: BuildMode | "reexport"; seed: number | null; overrides: RebuildOverrides }
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
  approval_detail: ApprovalDetail | null;
  build: BuildRunView | null; build_history: BuildHistoryRow[]; job_id: string;
  legal: Record<"edit_prompt" | "enhance" | "confirm" | "regenerate" | "approve" | "mark_regenerate" | "build"
    | "run_transform" | "accept" | "publish" | "retry_preview", boolean>;
  /** Every candidate set (round) in order, oldest first; the last may be an in-flight placeholder (generating). */
  rounds: Round[];
  /** Which candidate is approved and from which round; null when none or the decision is not a candidate one. */
  approved: Approved | null;
  /** True when the references changed after the current prompt was written: re-enhance or edit before confirming. */
  prompt_stale: boolean;
  /** Item references (guidance only, max 4). Change through the jobsApi reference helpers. */
  references: JobReference[]; references_revision: number; enhance_preset: EnhancePreset;
  /** Earlier rounds: candidate set id -> { candidate id -> QA evaluation id }. */
  qa_history: Record<string, Record<string, string>>;
  decisions: string[]; prompt_revisions: string[]; build_runs: string[];
  schema_version: number; snapshot_sha: string; created_at: string; updated_at: string;
  /** Legacy v1 alias of job_id. */
  batch_id: string;
}
export interface Counts { items: number; prompts: number; confirmed: number; candidates: number; approved: number;
  regenerate: number; built: number; accepted: number; published: number; busy: number; failed: number;
  cancelled: number;
  /** Direct-transform items still waiting for the transform to run. */
  to_transform: number }
/** A Job: a configured production workflow of one or more items (formerly called a batch). */
export interface JobSummary {
  id: string; alias: string; title: string; kind: Kind; kind_label: string; recipe_id: string;
  category_id: string | null; category_label: string | null; created_at: string; source: string; counts: Counts;
  by_stage: Record<string, number>; current_tab: string; waiting_on_user: boolean; next_action: string;
  active_run: string | null; legacy: boolean; legacy_recipe: string | null;
  archived_at: string | null;
  /** Set on Jobs created from a variant plan: the exact source, row and method. */
  variant: VariantContext | null;
  /** Direct transform Job: no prompt, candidates or approval; `run_transform` is its only build path. */
  direct: boolean;
  /** Family the variant Job publishes into (variant Jobs only). */
  family: FamilyRef | null;
  /** First Batch that lists this Job. */
  batch: BatchRef | null;
  /** Number of candidate rounds; only computed for single-item Jobs (0 otherwise, including variant Jobs before
   *  their first generation). */
  rounds: number;
}
export interface Stage { tag: string; name: string; backend: string }
export interface JobDetail extends JobSummary {
  seed_family: number; config_revision: number; locked_template: string; items: ItemView[];
  recipe: { id: string; label: string; build_label: string; build_available: boolean; build_blocked_reason: string;
    build_state: string; generation_available: boolean; generation_blocked_reason: string; stages: Stage[] };
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

// ---------------------------------------------------------------------------------------------------------------
// Families, variants, rounds, references, build modes. Shapes verified against real Studio responses.
// ---------------------------------------------------------------------------------------------------------------

/** A project-local same-kind group of assets. Membership is on the asset manifest (`family_id`). */
export interface FamilyRecord {
  schema_version: number; id: string; name: string; description: string; kind: Kind; anchor_asset_id: string;
  anchor_version_id: string; created_at: string; updated_at: string; revision: number; created_by_op: string;
}
/** GET /families, GET|PATCH /families/{id}: the record plus the count of members with a published version. */
export interface Family extends FamilyRecord { total_member_count: number }
/** Compact family reference carried by Jobs and publish-preview rows. */
export interface FamilyRef { id: string; name: string }
export interface BatchRef { id: string; title: string }

/** GET /assets?group_by=family: an ungrouped asset. */
export interface AssetGroup { type: "asset"; asset: AssetRow }
/** GET /assets?group_by=family: a family, filtered to the members matching the query. */
export interface FamilyGroup {
  type: "family"; family_id: string; name: string;
  /** Members matching the current filters / all members with a published version. */
  matching_count: number; total_member_count: number;
  /** The best matching member (ordering head). Look it up in `member_preview` for its preview artifact. */
  representative_asset_id: string;
  /** At most a handful of matching members; use `assets?family_id=` for the full list. */
  member_preview: AssetRow[];
  /** Reserved; always null today. */
  members_cursor: string | null;
}
export type AssetGroupItem = FamilyGroup | AssetGroup;
export interface GroupedAssets {
  group_by: "family"; matching_asset_count: number; matching_group_count: number; query_revision: number;
  groups: AssetGroupItem[]; next_cursor: string | null; all_assets_total: number;
}
/** `AssetDetail.derived_from`: snapshot of the exact source at publication time. */
export interface DerivedFrom {
  asset_id: string; version_id: string; display_version: number; display_name: string; method: VariantMethod;
}
/** `AssetDetail.versions[]`. */
export interface AssetVersionLineage { version_id: string; display_version: number; derivation: Derivation | null }

// --- Variant vocabulary -----------------------------------------------------------------------------------------
export type VariantMethod = "image_edit_reconstruct" | "image_edit" | "direct_transform";
export type VariantIntent = "subtle" | "related" | "exploratory";
export type Enforcement = "machine_enforced" | "advisory_visual" | "unsupported";
export type GlbAnchor = "source_origin" | "bounds_center" | "bottom_center";
export type EnhancePreset = "conservative" | "creative";

export interface UniformScale { op: "uniform_scale"; factor: number; anchor: GlbAnchor }
export interface AxisScale { op: "axis_scale"; x: number; y: number; z: number; anchor: GlbAnchor }
/** Needs `units_confirmed: true` (1 scene unit = 1 m) or the draft is refused. */
export interface TargetHeight { op: "target_height"; height_m: number; anchor: GlbAnchor; units_confirmed: boolean }
/** Direct 3D transform as stored/returned (defaults filled in). */
export type GlbTransform = UniformScale | AxisScale | TargetHeight;
export interface ResizeKeepAspect { op: "resize_keep_aspect"; max_width: number; max_height: number;
  resample: "lanczos" | "nearest" }
export interface PadCanvas {
  op: "pad_canvas"; width: number; height: number; placement: "center" | "bottom_center" | "top_left";
  /** "transparent", "source_edge" or "#rrggbb". */
  background: string;
}
/** Direct 2D transform as stored/returned (defaults filled in). */
export type RasterTransform = ResizeKeepAspect | PadCanvas;
/** Request forms: defaulted fields may be omitted. */
export type GlbTransformIn = (Omit<UniformScale, "anchor"> | Omit<AxisScale, "anchor"> | Omit<TargetHeight, "anchor">
  ) & { anchor?: GlbAnchor };
export type RasterTransformIn = (Omit<ResizeKeepAspect, "resample"> & { resample?: ResizeKeepAspect["resample"] })
  | (Omit<PadCanvas, "placement" | "background"> & { placement?: PadCanvas["placement"]; background?: string });

export interface Constraint { id: string; text: string; enforcement: Enforcement }
/** One intended new asset. `id` is stable across edits. Server-side limits: 1-32 rows, 1-8 candidates. */
export interface VariantRow {
  id: string; label: string; change_request: string;
  /** Generative 3D only: exact height applied after reconstruction. */
  final_height_m: number | null;
  glb_transform: GlbTransform | null; raster_transform: RasterTransform | null;
  candidate_count: number; confirm_duplicate: boolean;
}
/** Row as sent in create/patch draft bodies (omit `id` for new rows). */
export interface RowIn {
  id?: string; label: string; change_request?: string; final_height_m?: number | null;
  glb_transform?: GlbTransformIn | null; raster_transform?: RasterTransformIn | null;
  candidate_count?: number | null; confirm_duplicate?: boolean;
}
export interface SourceArtifactRef { role: string; artifact_id: string; sha256: string; size: number; mime: string }
/** The exact source version a plan is bound to. */
export interface SourceBinding {
  project_id: string; asset_id: string; version_id: string; display_version: number; version_sha256: string;
  kind: Kind; origin: string; display_name: string; primary_role: string; artifacts: SourceArtifactRef[];
  style_sha: string | null; licence: AssetVersion["licence"];
}
/** One single-object image derived from the source version. */
export interface ReferenceImage {
  artifact_id: string; sha256: string; role: "primary" | "auxiliary";
  /** e.g. "three_quarter", "rear", "side", "image". */
  view: string; params: Record<string, Json>;
}
/** POST :prepare-references response. */
export interface ReferenceSet {
  id: string; source: SourceBinding; primary_view: string; images: ReferenceImage[]; warnings: string[];
}
/** Either an existing family (`family_id`) or a new one (`new_name`); exactly one is set. */
export interface FamilyChoice { family_id: string | null; new_name: string | null }

export type TaskState = "queued" | "running" | "succeeded" | "failed" | "cancel_requested" | "cancelled" | "blocked"
  | "reconciling";
export interface DraftTask {
  state: "idle" | TaskState; error: string | null; code: string | null; task_id: string | null;
}
export interface DraftSuggestionRow { label: string; change_request: string }
/** The last row suggestion. Never applied without `applyDraftSuggestion`. */
export interface Suggestion {
  task_id: string; rows: DraftSuggestionRow[];
  /** How many fewer rows than requested the model produced. */
  short_by: number; notes: string[]; model: string; created_at: string; count: number; intent: VariantIntent;
  request: string;
}
export interface DraftWork { rows: number; image_edits: number; builds: number; transforms: number }
export interface VariantDraftMaterialized { plan_id: string; job_ids: string[]; batch_id: string | null;
  family_id: string }
/** Mutable wizard state (optimistic `revision`). POST/PATCH return this; GET returns `VariantDraftDetail`. */
export interface VariantDraft {
  schema_version: number; id: string; project_id: string; revision: number; source: SourceBinding;
  method: VariantMethod;
  /** null for direct_transform drafts. */
  intent: VariantIntent | null;
  preserve: Constraint[]; rows: VariantRow[]; family: FamilyChoice; candidates_per_row: number;
  request: string; reference_set_id: string | null; primary_view: string | null; analysis_id: string | null;
  suggestion: Suggestion | null; style_ack: boolean; created_at: string; updated_at: string;
  /** Set once saved as Jobs; the draft is then frozen. */
  materialized: VariantDraftMaterialized | null;
}
export interface VariantDraftDetail extends VariantDraft {
  tasks: { analyze: DraftTask; suggest: DraftTask };
  work: DraftWork;
  capabilities: Capabilities;
}

export type CapabilityReason = "missing_source" | "corrupt_source" | "unsupported_source_features"
  | "missing_models" | "blocked_licence" | "unsupported_configuration" | "engine_unavailable" | "experimental"
  | "source_not_published";
export interface MethodCapability {
  method: VariantMethod; label: string; available: boolean;
  /** Known reasons are in CapabilityReason; direct-transform inspection can add more codes. */
  reason: CapabilityReason | (string & Record<never, never>) | null;
  message: string; warnings: string[];
}
/** GET .../variant-capabilities. */
export interface Capabilities {
  source: SourceBinding; family: FamilyRef | null;
  /** conflict: the project style differs from the one the source was made with. */
  style: { current_sha: string | null; source_sha: string | null; conflict: boolean };
  methods: MethodCapability[];
}
export interface CreateJobsResult {
  plan_id: string; family_id: string; job_ids: string[];
  /** null when the plan has a single row. */
  batch_id: string | null; summary: DraftWork;
}
/** GET .../analysis. */
export interface SourceAnalysis {
  id: string; draft_id: string; reference_set_id: string; input_hashes: { view: string; sha256: string }[];
  model: string; observations: { text: string; images: number[] }[]; uncertainties: string[];
  proposed_preserve: { id: string; text: string }[]; proposed_changeable: string[];
  raw_ref: Record<string, Json>; created_at: string;
}
/** POST :analyze-source. 200 = cached (analysis inline, task_id null); 202 = queued or joined. */
export interface AnalyzeSourceResult {
  task_id: string | null; analysis_id?: string; analysis?: SourceAnalysis; joined?: boolean;
}
export interface VariantContext {
  plan_id: string; plan_sha256: string; row_id: string; family_id: string; method: VariantMethod;
  intent: VariantIntent | null; source_asset_id: string; source_version_id: string; source_display_version: number;
  source_name: string; change_request: string; final_height_m: number | null;
}
/** Report saved with a direct 3D version (`Derivation.transform`). */
export interface GlbTransformReport {
  transform: GlbTransform; effective_scale: number[]; anchor: GlbAnchor; anchor_point: number[];
  input_bounds: { min: number[]; max: number[] }; output_bounds: { min: number[]; max: number[] };
  input_height: number; output_height: number; tolerance: number;
  /** Only for target_height transforms. */
  height_ok?: boolean;
}
export interface RasterTransformReport {
  op: RasterTransform["op"]; input_size: [number, number]; output_size: [number, number];
  resample: string | null; background: string | null; content_bounds?: number[];
}
/** `AssetVersion.derivation`: immutable lineage of a variant version. */
export interface Derivation {
  method: VariantMethod; intent: VariantIntent | null; source: SourceBinding; family_id_at_publication: string;
  family_anchor: { anchor_asset_id?: string; anchor_version_id?: string };
  plan_id: string; plan_sha256: string; row_id: string;
  style: { current_sha: string | null; source_sha: string | null; conflict: boolean; acknowledged: boolean;
    application: string };
  /** Direct transforms only. */
  transform: GlbTransformReport | RasterTransformReport | null;
  references: ReferenceImage[];
}

// --- Job references, rounds, approval, build modes --------------------------------------------------------------
/** Normalized rectangle (0..1, origin top-left). */
export interface Crop { x: number; y: number; w: number; h: number }
export interface JobReference {
  /** jrf_... */
  id: string; artifact_id: string; sha256: string; origin: "upload" | "library"; note: string; crop: Crop | null;
  label: string | null;
  library: { asset_id: string; version_id: string; role: "image" | "preview" | null } | null;
}
export interface PromptBindings {
  preset?: EnhancePreset; mode?: "t2i" | "edit"; references_revision?: number; reference_ids?: string[];
  facts?: string[]; additions?: string[]; assumptions?: string[]; reference_cues?: { index: number; cue: string }[];
  /** Variant (edit mode) prompts only. */
  plan_id?: string; plan_sha256?: string; reference_set_id?: string; primary_reference_sha256?: string;
}
export type RoundCandidate = CandidateView;
export interface Round {
  number: number;
  /** null while the round is still being generated (no record yet). */
  candidate_set_id: string | null; prompt_revision_id: string | null;
  prompt: { positive: string; origin: string; preset: EnhancePreset | null; references_revision: number | null;
    additions: string[] } | null;
  created_at: string | null; requested: number | null; generating: boolean;
  /** In-flight placeholder only. */
  progress?: Task["progress"] | null;
  candidates: RoundCandidate[];
}
export interface Approved { candidate_set_id: string; candidate_id: string | null;
  /** null if the approved set is not among `rounds`. */
  round: number | null }
export type Gate = "prompt_confirmation" | "candidate_approval" | "build" | "final_acceptance" | "publication"
  | "transform_confirmation";
export interface ApprovalDetail {
  schema_version: number; id: string; gate: Gate; job_id: string; item_id: string; run_id: string | null;
  wave_id: string | null;
  /** Candidate approvals bind {candidate_set_id, candidate_id, image_sha256, artifact_id, prompt_revision_id,
   *  qa_evaluation_id, seed}; direct transforms bind {direct: true, artifact_id, image_sha256, plan_id, plan_sha256,
   *  row_id, transform, confirm_duplicate, source: {asset_id, version_id}}. */
  bound: Record<string, Json>;
  qa_status: string | null; override_qa: boolean; override_reason: string | null; failed_checks: string[];
  missing_checks: string[]; decided_at: string; actor: string; idempotency_key: string;
}
export interface BuildCheckpoint {
  stage: string; committed_at: string; stage_version?: number; inputs?: Record<string, Json>;
  settings?: Record<string, Json>; identities?: Record<string, Json>; outputs?: Record<string, string>;
  receipt?: Record<string, Json>;
}
/** build = first build; retry = resume the failed stage; resample = new seed (3D); rebuild = changed settings (3D). */
export type BuildMode = "build" | "retry" | "resample" | "rebuild";
/** The settings a rebuild may change (at least one is required for mode "rebuild"). */
export interface RebuildOverrides { triangles?: number; texture_size?: number; remesh?: boolean;
  pipeline_type?: string }

// --- Publish / diversity ----------------------------------------------------------------------------------------
/** GET .../publish-preview items (Job, Batch-run and v1 forms). `family`/`derived_from` only on variant Jobs. */
export interface PublishPreviewRow {
  job_id: string; item_id: string; name: string; build_run_id: string; expected_item_revision: number;
  published: boolean; asset_id: string | null; name_id: string; new_asset: boolean;
  current_version_id: string | null; next_display_version: number;
  family?: FamilyRef; derived_from?: { asset_id: string; version_id: string; display_version: number; name: string };
}
export type DiversityStatus = "missing" | "current" | "stale";
export type DiversityVerdict = "pass" | "fail" | "unavailable";
export interface DiversityPair {
  /** Job ids. */
  a: string; b: string; result: DiversityVerdict; reason: string; evaluator?: string;
  evaluated?: "deterministic";
}
export interface DiversityReport {
  id: string; plan_id: string; digest: string; ruleset: string; evaluator: string; simulated: boolean;
  created_at: string; task_id: string; coverage: "full" | "partial"; total_pairs: number;
  not_evaluated_pairs: number;
  selection: { job_id: string; item_id: string; row_id: string; candidate_id: string; artifact_id: string;
    sha256: string; basis: "candidate" | string }[];
  deterministic: { exact_duplicates: string[][]; dhash: Record<string, string>;
    dhash_pairs: { a: string; b: string; distance: number }[] };
  pairs: DiversityPair[];
}
/** GET /variant-plans/{plan}/diversity. `report` is the latest one, also when stale; null when missing. */
export interface DiversityStatusView {
  plan_id: string; status: DiversityStatus; ruleset: string;
  /** null when fewer than one Job has an approved candidate. */
  selection_digest: string | null; selected: number; report: DiversityReport | null;
  /** Per Job id. */
  jobs: Record<string, DiversityVerdict>;
}
/** POST :compare-selection (202; `current`/`busy` are also possible). */
export interface CompareSelectionResult {
  plan_id: string; digest: string; report_id: string; coverage: "full" | "partial"; selected: number;
  pairs: number; total_pairs: number; status: "queued" | "current"; task_id: string | null;
}

// --- Command responses ------------------------------------------------------------------------------------------
export interface ItemOutcome {
  job_id: string | null; item_id: string; ok: boolean; code?: string; message?: string;
  /** New item revision after a successful item mutation. */
  revision?: number; build_run_id?: string; approval_id?: string;
}
/** Async commands (enhance, confirm, regenerate, build, run-transform, reexport, publish...). HTTP 202. */
export interface CommandAccepted {
  command_id: string; tasks: string[]; results?: ItemOutcome[];
  operation?: { id: string; kind: string; tasks: string[] };
  operations?: { id: string; kind: string }[];
  skipped?: { job_id: string; item_id: string; reason: string }[];
}
/** POST /jobs/{id}:run (standalone). */
export interface JobRunResult { run_id: string; tasks: string[]; skipped: Json[]; plan: RunPlan }
/** Responses of :add-reference, :update-reference, :remove-reference, :set-preset. */
export interface ReferencesState {
  item_id: string; revision: number; references_revision: number; enhance_preset: EnhancePreset;
  references: JobReference[];
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
