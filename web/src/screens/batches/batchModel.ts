import { INFO, OK, WARN } from "../../components/progress";
import { KIND_LABEL, type JobSummary, type RunSummary } from "../../lib/api";

export interface BatchStats {
  run: number; wait: number; drafts: number; done: number; total: number;
  /** Prompts the first Start would enhance (draft, non-direct Jobs). */
  enhanceItems: number;
  kinds: string; gates: string; status: string; statusColor: string;
}

/** Derived only from the backend's per-Job progress (design pills); nothing is guessed client-side. */
export function batchStats(jobs: JobSummary[]): BatchStats {
  const st = (j: JobSummary) => j.progress.state;
  const run = jobs.filter((j) => st(j) === "run").length;
  const waiting = jobs.filter((j) => st(j) === "wait" || st(j) === "bad");
  const draftJobs = jobs.filter((j) => st(j) === "draft" && !j.direct);
  const drafts = jobs.filter((j) => st(j) === "draft").length;
  const done = jobs.filter((j) => st(j) === "done").length;
  const g = { p: 0, c: 0, b: 0, u: 0 };
  for (const j of waiting) {
    const s = j.progress.stage;
    if (s === 0) g.p++; else if (s <= 2) g.c++; else if (s === 4) g.u++; else g.b++;
  }
  const gates = [g.p && `${g.p} prompts`, g.c && `${g.c} candidate sets`, g.b && `${g.b} builds`, g.u && `${g.u} to publish`]
    .filter(Boolean).join(" · ") || "nothing waiting";
  const n = jobs.length;
  const status = !n ? "empty" : run ? `running · ${run} Job${run > 1 ? "s" : ""}`
    : drafts === n ? "draft · not started" : waiting.length ? "waiting on you" : done === n ? "done" : "idle";
  const statusColor = !n || drafts === n ? "#8b8c87" : run ? INFO : waiting.length ? WARN : OK;
  const kinds = [...new Set(jobs.map((j) => KIND_LABEL[j.kind] ?? j.kind_label))].join(" · ") || "—";
  return { run, wait: waiting.length, drafts, done, total: n, kinds, gates, status, statusColor,
    enhanceItems: draftJobs.reduce((a, j) => a + j.counts.items, 0) };
}

/** The open run that owns the Batch's Jobs; Review actions go through its wave endpoints. */
export function activeRun(history: RunSummary[]): RunSummary | null {
  return history.filter((r) => !r.closed_at).at(-1) ?? null;
}

export const plural = (n: number, w: string): string => `${n} ${w}${n === 1 ? "" : "s"}`;

import type { BatchGroupDetail } from "../../lib/api";

/** What every Batch tab receives. `jobs` are the members in Batch order; `all` every Job of the project. */
export interface BatchCtx {
  project: string; batch: BatchGroupDetail; jobs: JobSummary[]; all: JobSummary[]; active: RunSummary | null;
  stats: BatchStats; reload: () => void;
}
