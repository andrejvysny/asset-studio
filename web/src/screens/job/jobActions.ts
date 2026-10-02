// Commands of the Job workspace. Each one re-reads the item first, so a quick second click never sends a stale
// revision, and folds per-item failures into thrown ActionErrors.
import {
  J, send, type BuildMode, type CandidateView, type ItemOutcome, type ItemView, type RebuildOverrides, type Round,
} from "../../lib/api";
import * as api from "../../lib/jobsApi";
import { ActionError, explain, firstFailure, guard } from "./jobModel";

export async function freshItem(pid: string, jobId: string, itemId: string): Promise<ItemView> {
  const item = (await api.getJob(pid, jobId)).items.find((i) => i.id === itemId);
  if (!item) throw new ActionError("not_found", "this item no longer exists");
  return item;
}

export const runEnhance = (pid: string, jobId: string) => guard(async () => { await api.runJob(pid, jobId); });
export const reEnhance = (pid: string, jobId: string, itemId: string) =>
  guard(async () => { await api.enhance(pid, jobId, [itemId]); });

export const changePreset = (pid: string, jobId: string, itemId: string, preset: "conservative" | "creative") =>
  guard(async () => {
    const it = await freshItem(pid, jobId, itemId);
    await api.setPreset(pid, jobId, itemId, { preset, expected_item_revision: it.revision });
  });

async function saveDescription(pid: string, jobId: string, it: ItemView, description: string): Promise<void> {
  const res = await send<{ results: ItemOutcome[] }>("POST", `${J(pid)}/${jobId}:edit-prompts`, {
    items: [{ item_id: it.id, description, expected_item_revision: it.revision }] });
  const bad = firstFailure(res);
  if (bad) throw bad;
}

/** Rewrites one preview slot's prompt (variant 0 is the main prompt; the others only change that slot). */
export const editVariant = (pid: string, jobId: string, itemId: string, index: number, description: string) =>
  guard(async () => {
    const it = await freshItem(pid, jobId, itemId);
    const res = await send<{ results: ItemOutcome[] }>("POST", `${J(pid)}/${jobId}:edit-prompts`, {
      items: [{ item_id: it.id, description, variant_index: index, expected_item_revision: it.revision }] });
    const bad = firstFailure(res);
    if (bad) throw bad;
  });

const changed = (it: ItemView, draft: string | null): draft is string =>
  draft !== null && draft.trim() !== "" && draft.trim() !== (it.prompt?.description ?? "").trim();

/** Confirms the current prompt (saving an edited draft first) and starts a generation round. */
export const confirmAndGenerate = (pid: string, jobId: string, itemId: string, draft: string | null) => guard(async () => {
  let it = await freshItem(pid, jobId, itemId);
  if (changed(it, draft)) {
    if (it.current_set) { await regenerate(pid, jobId, it, draft); return; }
    await saveDescription(pid, jobId, it, draft);
    it = await freshItem(pid, jobId, itemId);
  }
  if (!it.current_prompt) throw new ActionError("no_prompt", "there is no prompt to confirm yet");
  const bad = firstFailure(await api.confirmAndGenerate(pid, jobId, [{
    item_id: it.id, prompt_revision_id: it.current_prompt, expected_item_revision: it.revision }]));
  if (bad) throw bad;
});

async function regenerate(pid: string, jobId: string, it: ItemView, description?: string): Promise<void> {
  const bad = firstFailure(await api.regenerate(pid, jobId, [{
    item_id: it.id, expected_item_revision: it.revision, ...(description ? { description } : {}) }]));
  if (bad) throw bad;
}

/** Next round with the edited prompt and/or changed references (an explicit description refreshes the bindings). */
export const generateNextRound = (pid: string, jobId: string, itemId: string, draft: string | null) => guard(async () => {
  const it = await freshItem(pid, jobId, itemId);
  const pending = !!it.current_prompt && it.prompt_confirmed !== it.current_prompt;
  if (!changed(it, draft) && pending && !it.prompt_stale) {
    const bad = firstFailure(await api.confirmAndGenerate(pid, jobId, [{
      item_id: it.id, prompt_revision_id: it.current_prompt!, expected_item_revision: it.revision }]));
    if (bad) throw bad;
    return;
  }
  await regenerate(pid, jobId, it, changed(it, draft) ? draft : it.prompt?.description);
});

/** Same prompt, fresh seeds. */
export const regenerateSameSeeds = (pid: string, jobId: string, itemId: string) => guard(async () => {
  await regenerate(pid, jobId, await freshItem(pid, jobId, itemId));
});

export async function approve(pid: string, jobId: string, itemId: string, round: Round, cand: CandidateView,
  override: { reason: string } | null): Promise<ItemOutcome> {
  return guard(async () => {
    const it = await freshItem(pid, jobId, itemId);
    if (!it.legal.approve) throw new ActionError("not_allowed", "this item cannot be approved now");
    if (!round.candidate_set_id || !round.prompt_revision_id) throw new ActionError("not_ready", "round not generated yet");
    const res = await api.approveCandidates(pid, jobId, [{
      item_id: it.id, expected_item_revision: it.revision, candidate_set_id: round.candidate_set_id,
      candidate_id: cand.id, image_sha256: cand.sha256, prompt_revision_id: round.prompt_revision_id,
      qa_evaluation_id: cand.qa?.id ?? null, override_qa: !!override, override_reason: override?.reason || null }]);
    const r = res.results[0];
    if (!r) throw new ActionError("empty", "no result");
    if (!r.ok) throw explain(r.code, r.message);
    return r;
  });
}

export const clearApproval = (pid: string, jobId: string, itemId: string) => guard(async () => {
  const it = await freshItem(pid, jobId, itemId);
  const res = await send<{ results: ItemOutcome[] }>("POST", `${J(pid)}/${jobId}:clear-approval`, {
    items: [{ item_id: it.id, expected_item_revision: it.revision }] });
  const bad = firstFailure(res);
  if (bad) throw bad;
});

/** Starts a build attempt from the current approval; `approval` skips the re-read after a fresh approve. */
export const startBuild = (pid: string, jobId: string, itemId: string, mode: BuildMode,
  overrides?: RebuildOverrides, approval?: { id: string; revision: number }) => guard(async () => {
  const it = approval ? null : await freshItem(pid, jobId, itemId);
  const approvalId = approval?.id ?? it?.approval;
  if (!approvalId) throw new ActionError("no_approval", "approve a candidate first");
  const bad = firstFailure(await api.buildApproved(pid, jobId, [{
    item_id: itemId, approval_id: approvalId, expected_item_revision: approval?.revision ?? it!.revision, mode,
    ...(overrides ? { overrides } : {}) }]));
  if (bad) throw bad;
});

export const startTransform = (pid: string, jobId: string, itemId: string) => guard(async () => {
  const it = await freshItem(pid, jobId, itemId);
  const bad = firstFailure(await api.runTransform(pid, jobId, [{ item_id: it.id, expected_item_revision: it.revision }]));
  if (bad) throw bad;
});

export const setAccepted = (pid: string, jobId: string, itemId: string, runId: string, accept: boolean) => guard(async () => {
  const it = await freshItem(pid, jobId, itemId);
  const bad = firstFailure(await api.acceptBuilds(pid, jobId, [{
    item_id: it.id, build_run_id: runId, expected_item_revision: it.revision, accept }]));
  if (bad) throw bad;
});
