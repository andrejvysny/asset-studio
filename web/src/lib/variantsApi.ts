// Typed endpoints for asset families and variant planning. All paths are /api/v1/projects/{project}/...
// (variant drafts, plans and families are v1 routes; the Jobs they create are read via jobsApi, which is v2).
// Mutations that carry an `idempotency_key` take it as the LAST optional argument: pass a stable key to make a
// user action safely retryable, or omit it for a fresh key per call.

import {
  P, get, key, send,
  type AnalyzeSourceResult, type AssetList, type Capabilities, type CompareSelectionResult, type CreateJobsResult,
  type DiversityStatusView, type Family, type GroupedAssets, type Kind, type Manifest, type Origin, type ReferenceSet, type RowIn,
  type SourceAnalysis, type VariantDraft, type VariantDraftDetail, type VariantIntent, type VariantMethod, type Constraint,
} from "./api";

const qs = (params: object): string => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params) as [string, unknown][]) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
};

// --- Library: families -----------------------------------------------------------------------------------------
export interface AssetQuery {
  category_id?: string; kind?: Kind; origin?: Origin; q?: string; limit?: number; family_id?: string;
  /** true = archived assets only; default (omitted) = active only. */
  archived?: boolean;
}
/** Flat list (optionally `family_id`-filtered). Rows carry family_id / family_name. */
export const listAssets = (project: string, query: AssetQuery & { offset?: number; planned?: boolean } = {}) =>
  get<AssetList>(`${P(project)}/assets${qs(query)}`);
/** Families and ungrouped assets as units. Page with `cursor` = previous `next_cursor` and the SAME filters
 *  (a changed query or library revision gives 409 `stale_cursor`: restart from the top). */
export const listAssetsGrouped = (project: string, query: AssetQuery & { cursor?: string } = {}) =>
  get<GroupedAssets>(`${P(project)}/assets${qs({ ...query, group_by: "family" })}`);
/** Archive hides an asset from active views (nothing is deleted); restore brings it back with the same id.
 *  `expected_revision` = Manifest.revision; 409 on conflict. */
export const archiveAsset = (project: string, assetId: string, expectedRevision: number) =>
  send<Manifest>("POST", `${P(project)}/assets/${assetId}:archive`, { expected_revision: expectedRevision });
export const restoreAsset = (project: string, assetId: string, expectedRevision: number) =>
  send<Manifest>("POST", `${P(project)}/assets/${assetId}:restore`, { expected_revision: expectedRevision });
/** IRREVERSIBLE. Archived assets only; `confirmName` must equal the asset's name_id. 409 `asset_in_use` carries
 *  `detail: [{type, key, reason}]` (the records that still refer to the asset). */
export const deleteAssetPermanently = (project: string, assetId: string, expectedRevision: number, confirmName: string) =>
  send<{ asset_id: string; versions: number; artifacts_deleted: number; artifacts_kept: number;
    blobs_deleted: number; blobs_kept: number }>("POST", `${P(project)}/assets/${assetId}:delete`,
    { expected_revision: expectedRevision, confirm_name: confirmName });
export const listFamilies = (project: string) => get<{ families: Family[] }>(`${P(project)}/families`);
export const getFamily = (project: string, familyId: string) => get<Family>(`${P(project)}/families/${familyId}`);
/** Rename / describe a family (`expected_revision` = Family.revision; 409 on conflict). */
export const patchFamily = (project: string, familyId: string,
  body: { expected_revision: number; name?: string; description?: string }) =>
  send<Family>("PATCH", `${P(project)}/families/${familyId}`, body);

// --- Capabilities ----------------------------------------------------------------------------------------------
export const getVariantCapabilities = (project: string, assetId: string, versionId: string) =>
  get<Capabilities>(`${P(project)}/assets/${assetId}/versions/${versionId}/variant-capabilities`);

// --- Drafts ----------------------------------------------------------------------------------------------------
export interface CreateDraftBody {
  asset_id: string; version_id: string; method: VariantMethod;
  /** Ignored for direct_transform (draft.intent is null). Default "related". */
  intent?: VariantIntent;
  /** Default 1, max 32. Creates that many placeholder rows when `rows` is empty. */
  requested_variants?: number;
  /** Default 4, max 8. */
  candidates_per_variant?: number;
  change_request?: string; rows?: RowIn[];
  /** New family name; defaults to the source's name. An existing family of the source is reused. */
  family_name?: string;
}
export const createDraft = (project: string, body: CreateDraftBody, idempotencyKey: string = key()) =>
  send<VariantDraft>("POST", `${P(project)}/variant-drafts`, { ...body, idempotency_key: idempotencyKey });
/** Includes planning task states, work summary and capabilities. */
export const getDraft = (project: string, draftId: string) =>
  get<VariantDraftDetail>(`${P(project)}/variant-drafts/${draftId}`);
export interface PatchDraftBody {
  expected_revision: number; method?: VariantMethod; intent?: VariantIntent; preserve?: Constraint[];
  rows?: RowIn[]; candidates_per_row?: number; request?: string; family_name?: string; primary_view?: string;
  style_ack?: boolean;
}
/** 409 `stale_variant_plan` if the draft moved on; 409 `draft_materialized` after create-jobs. */
export const patchDraft = (project: string, draftId: string, body: PatchDraftBody) =>
  send<VariantDraft>("PATCH", `${P(project)}/variant-drafts/${draftId}`, body);
/** Renders the source reference images (3D) or profiles the image. Required before create-jobs. */
export const prepareReferences = (project: string, draftId: string) =>
  send<ReferenceSet>("POST", `${P(project)}/variant-drafts/${draftId}:prepare-references`);
/** Saves the draft as one-item Jobs (plus a Batch if several rows). 409 `references_missing` if not prepared.
 *  Nothing runs until the Jobs are started. */
export const createVariantJobs = (project: string, draftId: string, expectedRevision: number,
  idempotencyKey: string = key()) =>
  send<CreateJobsResult>("POST", `${P(project)}/variant-drafts/${draftId}:create-jobs`,
    { expected_revision: expectedRevision, idempotency_key: idempotencyKey });

// --- Planning (asynchronous; poll getDraft().tasks or listen to change events) ---------------------------------
export const analyzeSource = (project: string, draftId: string, idempotencyKey: string = key()) =>
  send<AnalyzeSourceResult>("POST", `${P(project)}/variant-drafts/${draftId}:analyze-source`,
    { idempotency_key: idempotencyKey });
export const suggestPlan = (project: string, draftId: string, body: { count: number; request?: string },
  idempotencyKey: string = key()) =>
  send<{ task_id: string }>("POST", `${P(project)}/variant-drafts/${draftId}:suggest-plan`,
    { ...body, idempotency_key: idempotencyKey });
/** Copies suggested rows into the draft. "replace_empty" needs an empty draft; `indices` picks suggestion rows. */
export const applySuggestion = (project: string, draftId: string,
  body: { expected_revision: number; mode: "replace_empty" | "append"; indices?: number[] }) =>
  send<VariantDraft>("POST", `${P(project)}/variant-drafts/${draftId}:apply-suggestion`, body);
/** 404 `no_analysis` if the draft has none. */
export const getAnalysis = (project: string, draftId: string) =>
  get<SourceAnalysis>(`${P(project)}/variant-drafts/${draftId}/analysis`);

// --- Diversity of a plan's approved selection (advisory) ---------------------------------------------------------
/** 409 `selection_too_small` unless at least two rows have an approved candidate. */
export const compareSelection = (project: string, planId: string, idempotencyKey: string = key()) =>
  send<CompareSelectionResult>("POST", `${P(project)}/variant-plans/${planId}:compare-selection`,
    { idempotency_key: idempotencyKey });
export const getDiversity = (project: string, planId: string) =>
  get<DiversityStatusView>(`${P(project)}/variant-plans/${planId}/diversity`);

// --- Library: categories -----------------------------------------------------------------------------------------
export interface SetCategoryResult { category_id: string | null; changed: number;
  results: { asset_id: string; ok: boolean; changed?: boolean; revision?: number; code?: string; message?: string }[] }
/** Move assets to one category in one call; `categoryId` null = Uncategorized. Metadata only; per-asset results. */
export const setAssetsCategory = (project: string, assetIds: string[], categoryId: string | null) =>
  send<SetCategoryResult>("POST", `${P(project)}/assets:set-category`, { asset_ids: assetIds, category_id: categoryId });
