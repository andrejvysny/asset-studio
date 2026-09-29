// Review-tab commands. Jobs owned by the Batch's open run go through the run's wave endpoints (one call across
// Jobs); every other Job uses its own per-Job endpoint. Both accept `job_id` per unit.
import { type ItemOutcome, J, key, send, V2 } from "../../lib/api";
import type { RunSummary } from "../../lib/api";

export type Unit = { job_id: string } & Record<string, unknown>;
export type Action = "confirm" | "approve" | "build" | "accept" | "publish";

const NAME: Record<Action, [wave: string, job: string]> = {
  confirm: ["confirm-prompts", "confirm-and-generate"], approve: ["approve-candidates", "approve-candidates"],
  build: ["build-approved", "build-approved"], accept: ["accept-builds", "accept-builds"], publish: ["publish", "publish"],
};

export const scopeBase = (project: string, active: RunSummary | null, jobId: string): { base: string; wave: boolean } =>
  active && active.job_ids.includes(jobId) ? { base: `${V2(project)}/runs/${active.id}`, wave: true }
    : { base: `${J(project)}/${jobId}`, wave: false };

function groups(project: string, active: RunSummary | null, units: Unit[]): Map<string, { wave: boolean; units: Unit[] }> {
  const out = new Map<string, { wave: boolean; units: Unit[] }>();
  for (const u of units) {
    const { base, wave } = scopeBase(project, active, u.job_id);
    // Wave calls are cross-Job (one group per run); Job calls are per Job.
    const g = out.get(base) ?? { wave, units: [] };
    g.units.push(u);
    out.set(base, g);
  }
  return out;
}

/** Sends the units grouped by scope. Per-unit failures come back in `results` and are joined into one error. */
export async function post(project: string, active: RunSummary | null, action: Action, units: Unit[]): Promise<void> {
  const failed: string[] = [];
  for (const [base, g] of groups(project, active, units)) {
    const res = await send<{ results?: ItemOutcome[] }>("POST", `${base}:${NAME[action][g.wave ? 0 : 1]}`,
      { items: g.units, idempotency_key: key() });
    for (const r of res.results ?? []) if (!r.ok) failed.push(r.message ?? r.code ?? "failed");
  }
  if (failed.length) throw new Error(failed.join("; "));
}

export interface Proposal { item_id: string; job_id: string; candidate_id: string }

export async function previewBest(project: string, active: RunSummary | null, units: { job_id: string; item_id: string }[]):
  Promise<Proposal[]> {
  const out: Proposal[] = [];
  for (const [base, g] of groups(project, active, units.map((u) => ({ ...u })))) {
    const res = await send<{ proposals: Proposal[] }>("POST", `${base}:preview-best`,
      { item_ids: g.units.map((u) => u.item_id as string) });
    out.push(...res.proposals);
  }
  return out;
}

export async function editPrompts(project: string, edits: { job_id: string; item_id: string; expected_item_revision: number;
  description: string }[]): Promise<void> {
  const byJob = new Map<string, typeof edits>();
  for (const e of edits) byJob.set(e.job_id, [...(byJob.get(e.job_id) ?? []), e]);
  const failed: string[] = [];
  for (const [jid, items] of byJob) {
    const res = await send<{ results: ItemOutcome[] }>("POST", `${J(project)}/${jid}:edit-prompts`, { items });
    for (const r of res.results) if (!r.ok) failed.push(r.message ?? r.code ?? "edit failed");
  }
  if (failed.length) throw new Error(failed.join("; "));
}
