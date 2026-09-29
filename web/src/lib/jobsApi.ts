// Typed endpoints for Jobs (v2): /api/v2/projects/{project}/jobs/... Asset/Family/draft routes are v1: see variantsApi.
// Item-level commands need `expected_item_revision` (ItemView.revision); a stale value gives 409 `stale_item`.
// Commands that take `idempotency_key` default to a fresh key; pass a stable one to make a retry safe.

import {
  J, P, get, key, send, upload,
  type BuildMode, type CommandAccepted, type Crop, type EnhancePreset, type JobDetail, type JobRunResult,
  type JobSummary, type PublishPreviewRow, type RebuildOverrides, type ReferencesState, type ItemOutcome,
} from "./api";

const item = (project: string, jobId: string, itemId: string) => `${J(project)}/${jobId}/items/${itemId}`;

export const listJobs = (project: string) => get<{ jobs: JobSummary[] }>(J(project));
export const getJob = (project: string, jobId: string) => get<JobDetail>(`${J(project)}/${jobId}`);

/** Runs a Job standalone (409 `job_in_active_run` if a Batch run owns it). 202. Enhances prompts, then stops at
 *  prompt review. */
export const runJob = (project: string, jobId: string, idempotencyKey: string = key()) =>
  send<JobRunResult>("POST", `${J(project)}/${jobId}:run`, { idempotency_key: idempotencyKey });

// --- Item references (guidance images, max 4) and enhancement preset --------------------------------------------
/** Registers an image and returns its artifact id for addReference. */
export const uploadReference = (project: string, file: File) =>
  upload<{ artifact_id: string; sha256: string }>(`${P(project)}/references:upload`, file);
export type AddReferenceBody = { note?: string; crop?: Crop; label?: string; expected_item_revision: number } & (
  { artifact_id: string; library?: never } | { library: { asset_id: string; version_id: string; role?: "image" | "preview" };
    artifact_id?: never });
/** 422 `too_many_references` / `invalid_crop`; 409 `busy` while generating; 409 `stale_item`. */
export const addReference = (project: string, jobId: string, itemId: string, body: AddReferenceBody) =>
  send<ReferencesState>("POST", `${item(project, jobId, itemId)}:add-reference`, body);
/** Omit `crop` to keep it; `crop: null` clears it. */
export const updateReference = (project: string, jobId: string, itemId: string,
  body: { reference_id: string; note?: string; crop?: Crop | null; expected_item_revision: number }) =>
  send<ReferencesState>("PATCH", `${item(project, jobId, itemId)}:update-reference`, body);
export const removeReference = (project: string, jobId: string, itemId: string,
  body: { reference_id: string; expected_item_revision: number }) =>
  send<ReferencesState>("POST", `${item(project, jobId, itemId)}:remove-reference`, body);
/** Any reference or preset change makes the current prompt stale (ItemView.prompt_stale): re-enhance or edit. */
export const setPreset = (project: string, jobId: string, itemId: string,
  body: { preset: EnhancePreset; expected_item_revision: number }) =>
  send<ReferencesState>("PATCH", `${item(project, jobId, itemId)}:set-preset`, body);

// --- Prompt / candidate commands --------------------------------------------------------------------------------
export const enhance = (project: string, jobId: string, itemIds: string[], idempotencyKey: string = key()) =>
  send<CommandAccepted>("POST", `${J(project)}/${jobId}:enhance`, { item_ids: itemIds, idempotency_key: idempotencyKey });
/** Confirms the CURRENT prompt and starts generation (a new round). Result code `stale_instruction_confirmation`
 *  when references changed after the prompt was written. */
export const confirmAndGenerate = (project: string, jobId: string,
  items: { item_id: string; prompt_revision_id: string; expected_item_revision: number }[],
  idempotencyKey: string = key()) =>
  send<CommandAccepted>("POST", `${J(project)}/${jobId}:confirm-and-generate`,
    { items, idempotency_key: idempotencyKey });
/** New candidate set (round) with the same prompt and fresh seeds; an optional `description` edits the prompt first. */
export const regenerate = (project: string, jobId: string,
  items: { item_id: string; expected_item_revision: number; description?: string }[],
  idempotencyKey: string = key()) =>
  send<CommandAccepted>("POST", `${J(project)}/${jobId}:regenerate`, { items, idempotency_key: idempotencyKey });
export interface ApproveItemBody {
  item_id: string; expected_item_revision: number; candidate_set_id: string; candidate_id: string;
  image_sha256: string; prompt_revision_id: string; qa_evaluation_id: string | null; override_qa?: boolean;
  override_reason?: string | null;
}
/** Approve from ANY round: send that round's candidate_set_id / prompt_revision_id (Round fields) and the candidate's
 *  sha256 and QA id. Per-item failures are in `results` (codes sha_mismatch, stale_prompt, stale_qa, stale_set). */
export const approveCandidates = (project: string, jobId: string, items: ApproveItemBody[],
  idempotencyKey: string = key()) =>
  send<{ results: ItemOutcome[] }>("POST", `${J(project)}/${jobId}:approve-candidates`,
    { items, idempotency_key: idempotencyKey });

// --- Build, transform, accept, publish --------------------------------------------------------------------------
export interface BuildItemBody {
  item_id: string; approval_id: string; expected_item_revision: number;
  /** Default "build". resample/rebuild are 3D only; overrides belong to "rebuild" only (and need >= 1 key). */
  mode?: BuildMode; overrides?: RebuildOverrides;
}
/** Not for direct-transform Jobs (422 `not_applicable`): use runTransform. */
export const buildApproved = (project: string, jobId: string, items: BuildItemBody[], idempotencyKey: string = key()) =>
  send<CommandAccepted>("POST", `${J(project)}/${jobId}:build-approved`, { items, idempotency_key: idempotencyKey });
/** Confirms and runs the deterministic transform of a direct Job (ItemView.legal.run_transform). */
export const runTransform = (project: string, jobId: string,
  items: { item_id: string; expected_item_revision: number }[], idempotencyKey: string = key()) =>
  send<CommandAccepted>("POST", `${J(project)}/${jobId}:run-transform`, { items, idempotency_key: idempotencyKey });
export const acceptBuilds = (project: string, jobId: string,
  items: { item_id: string; build_run_id: string; expected_item_revision: number; accept?: boolean }[],
  idempotencyKey: string = key()) =>
  send<{ results: ItemOutcome[] }>("POST", `${J(project)}/${jobId}:accept-builds`,
    { items, idempotency_key: idempotencyKey });
/** Rows have family / derived_from for variant Jobs. */
export const getPublishPreview = (project: string, jobId: string) =>
  get<{ items: PublishPreviewRow[] }>(`${J(project)}/${jobId}/publish-preview`);
export const publish = (project: string, jobId: string,
  items: { item_id: string; build_run_id: string; expected_item_revision: number; make_current?: boolean;
    expected_current_version?: string | null }[], idempotencyKey: string = key()) =>
  send<CommandAccepted>("POST", `${J(project)}/${jobId}:publish`, { items, idempotency_key: idempotencyKey });
