// Pure helpers for the single-asset Job workspace: labels, colours, word diff, attempt state.
import { BAD, INFO, NONE, OK, WARN } from "../../components/ui";
import {
  ApiError, type BuildHistoryRow, type CandidateView, type CheckResult, type GlbTransformReport, type ItemOutcome,
  type ItemView, type JobDetail, type Kind, type ProgressState, type RasterTransformReport, type Round,
} from "../../lib/api";

export const TABS = ["prompt", "build", "publish"] as const;
export type Tab = (typeof TABS)[number];
/** Slugs of the old five-tab layout keep working. */
const LEGACY_TAB: Record<string, Tab> = {
  prompts: "prompt", candidates: "prompt", approve: "prompt", build: "build", publish: "publish", prompt: "prompt",
};
export const tabFromSlug = (slug: string | undefined): Tab | null => (slug ? LEGACY_TAB[slug] ?? null : null);

export const VERB: Record<Kind, string> = {
  model3d: "Generate 3D", sprite: "Cut out", icon: "Compose into frame", vfx_flipbook: "Generate in-betweens",
  material: "Generate maps", sprite_sheet: "Generate frames", concept_art: "Upscale",
};

export const MODE_LABEL: Record<string, string> = {
  build: "first build", retry: "retry · same settings", resample: "resample · new seed",
  reexport: "re-export from raw", rebuild: "rebuild · new settings",
};

export const DIM = "var(--dim)";
export const FAINT = "var(--faint)";

export const progressColor = (s: ProgressState): string =>
  s === "bad" ? BAD : s === "wait" ? WARN : s === "run" ? INFO : DIM;

/** Items that count; a fully cancelled Job still shows its first item. */
export function liveItems(job: JobDetail): ItemView[] {
  const live = job.items.filter((i) => !i.cancelled);
  return live.length ? live : job.items;
}

export const isBusy = (item: ItemView, stage: string): boolean =>
  ["queued", "running", "cancel_requested", "reconciling"].includes(item.tasks[stage]?.state ?? "");

// --- Rounds / candidates ---------------------------------------------------------------------------------------
export function candidateKey(c: CandidateView): number { return c.index + 1; }

export function pickLabel(item: ItemView, direct: boolean, kind: Kind): string {
  if (direct) return kind === "model3d" ? "source GLB" : "source image";
  const a = item.approved;
  if (!a) return "";
  const cand = item.rounds.find((r) => r.number === a.round)?.candidates.find((c) => c.id === a.candidate_id);
  if (a.round == null || !cand) return a.round != null ? `R${a.round}` : "approved candidate";
  return `R${a.round} #${candidateKey(cand)}`;
}

/** Where an attempt came from: its own recorded source, never the item's current approval. */
export function sourceLabel(item: ItemView, row: BuildHistoryRow, direct: boolean, kind: Kind): string {
  if (direct) return pickLabel(item, direct, kind);
  const src = row.source;
  if (!src) return "";
  const cand = item.rounds.find((r) => r.candidate_set_id === src.candidate_set_id)?.candidates.find((c) => c.id === src.candidate_id);
  if (src.round == null) return "approved candidate";
  return cand ? `R${src.round} #${candidateKey(cand)}` : `R${src.round}`;
}

export function isApprovedHere(item: ItemView, round: Round | undefined, cand: CandidateView | undefined): boolean {
  const a = item.approved;
  return !!a && !!round && !!cand && a.candidate_set_id === round.candidate_set_id && a.candidate_id === cand.id;
}

/** [text, colour] of the QA verdict of one candidate. */
export function qaText(c: CandidateView): [string, string] {
  if (!c.qa) return ["QA pending", NONE];
  return c.qa.status === "recommended" ? ["recommended", OK]
    : c.qa.status === "not_recommended" ? ["not recommended", BAD] : ["unverified", NONE];
}

export function roundWhy(rounds: Round[], i: number): string {
  if (i === 0) return "first round";
  const cur = rounds[i]?.prompt, prev = rounds[i - 1]?.prompt;
  if (cur && prev && cur.positive === prev.positive) return "same prompt, new seeds";
  return cur?.origin === "edited" ? "edited prompt" : "re-enhanced prompt";
}

export interface Token { w: string; added: boolean }
/** Words of `cur`, marking those missing from `prev` (case-insensitive) as added. */
export function diffWords(prev: string | null | undefined, cur: string): Token[] {
  const seen = prev == null ? null : new Set(prev.toLowerCase().split(/\s+/));
  return cur.split(/\s+/).filter(Boolean).map((w) => ({ w, added: !!seen && !seen.has(w.toLowerCase()) }));
}

export const capitalize = (s: string): string => (s ? s[0]!.toUpperCase() + s.slice(1) : s);

// --- QA checks ---------------------------------------------------------------------------------------------------
export function checkColor(result: CheckResult["result"], soft: boolean): string {
  return result === "pass" ? OK : result === "fail" ? (soft ? WARN : BAD) : NONE;
}
export function checkText(result: CheckResult["result"]): string {
  return result === "pass" ? "pass" : result === "fail" ? "fail" : result === "unavailable" ? "unavail." : "n/a";
}
export const isRefCheck = (id: string): boolean => /^ref_\d+$/.test(id);
export const isVariantCheck = (id: string): boolean => id.startsWith("variant_");

// --- Builds --------------------------------------------------------------------------------------------------------
export interface Mark { k: string; mark: "✓" | "✗" | "·"; color: string }
const mark = (k: string, v: 1 | 0 | -1): Mark => ({ k, mark: v === -1 ? "✗" : v ? "✓" : "·", color: v === -1 ? BAD : v ? OK : FAINT });

const TERMINAL = ["succeeded", "failed", "blocked", "cancelled"];
export const isRunning = (row: BuildHistoryRow | undefined): boolean => !!row && !TERMINAL.includes(row.status);

/** [label, colour] of one attempt. */
export function attemptState(row: BuildHistoryRow, is3d: boolean): [string, string] {
  if (row.status === "succeeded") {
    return row.result === "invalid" ? ["invalid", BAD] : row.result === "validation_unavailable" ? ["unverified", NONE]
      : ["done", OK];
  }
  if (row.status === "failed" || row.status === "blocked") return [row.status, BAD];
  if (row.status === "cancelled") return ["cancelled", NONE];
  if (row.status === "queued") return ["queued", NONE];
  if (!is3d) return ["building", INFO];
  const has = (s: string) => row.checkpoints.includes(s);
  return [!has("segment") ? "cut-out" : !has("sample") ? "meshing" : "exporting", INFO];
}

/** Checkpoint marks: segment/sample/bake/finalize show as cut-out · raw · GLB · preview. */
export function attemptMarks(row: BuildHistoryRow, is3d: boolean): Mark[] {
  const ok = row.status === "succeeded";
  const dead = row.status === "failed" || row.status === "blocked";
  const has = (s: string) => row.checkpoints.includes(s);
  const preview = row.preview === "failed" ? -1 : row.preview === "available" ? 1 : 0;
  if (is3d) {
    const steps: [string, boolean][] = [["cut-out", has("segment")], ["raw", has("sample")],
      ["GLB", has("bake") && row.result !== "invalid"]];
    const failedAt = dead ? steps.findIndex(([, d]) => !d) : -1;
    return [...steps.map(([k, d], i): Mark => mark(k, d ? 1 : i === failedAt || (k === "GLB" && row.result === "invalid") ? -1 : 0)),
      mark("preview", preview)];
  }
  return [mark("output", ok ? 1 : dead ? -1 : 0), mark("validate", row.result === "valid" ? 1 : row.result === "invalid" ? -1 : 0),
    mark("preview", preview)];
}

export function attemptPct(row: BuildHistoryRow, is3d: boolean): number {
  if (row.status === "succeeded") return 100;
  if (row.status === "queued") return 5;
  if (!is3d) return 50;
  return (["segment", "sample", "bake"].filter((s) => row.checkpoints.includes(s)).length / 4) * 100 || 10;
}

// --- Transforms ----------------------------------------------------------------------------------------------------
export function isGlbReport(t: unknown): t is GlbTransformReport {
  return !!t && typeof t === "object" && "effective_scale" in t;
}
export function isRasterReport(t: unknown): t is RasterTransformReport {
  return !!t && typeof t === "object" && "input_size" in t && "output_size" in t;
}
const fmt = (n: number): string => Number(n.toPrecision(6)).toString();
export function describeTransform(t: { op?: string } & Record<string, unknown>): string {
  const anchor = t.anchor ? ` · anchor ${String(t.anchor).replace("_", " ")}` : "";
  switch (t.op) {
    case "uniform_scale": return `uniform scale ×${fmt(Number(t.factor))}${anchor}`;
    case "axis_scale": return `axis scale ×${fmt(Number(t.x))} ×${fmt(Number(t.y))} ×${fmt(Number(t.z))}${anchor}`;
    case "target_height": return `target height ${fmt(Number(t.height_m))} m${anchor}`;
    case "resize_keep_aspect": return `fit within ${String(t.max_width)}×${String(t.max_height)} px`;
    case "pad_canvas": return `pad canvas to ${String(t.width)}×${String(t.height)} px`;
    default: return JSON.stringify(t);
  }
}

const anchorWords = (a: unknown): string => (a === "source_origin" ? "the scene origin" : String(a ?? "bottom_center").replace("_", " ").replace("bottom center", "bottom centre").replace("bounds center", "bounds centre"));
/** One sentence for the direct-transform card. */
export function transformSentence(t: { op?: string } & Record<string, unknown>): string {
  const at = `, anchored at ${anchorWords(t.anchor)}`;
  switch (t.op) {
    case "target_height": return `Scales the source GLB to ${fmt(Number(t.height_m))} m height${at}.`;
    case "uniform_scale": return `Scales the source GLB by ×${fmt(Number(t.factor))}${at}.`;
    case "axis_scale": return `Scales the source GLB by ×${fmt(Number(t.x))} ×${fmt(Number(t.y))} ×${fmt(Number(t.z))}${at}.`;
    case "resize_keep_aspect": return `Fits the source image within ${String(t.max_width)}×${String(t.max_height)} px, keeping its aspect.`;
    case "pad_canvas": return `Pads the source image canvas to ${String(t.width)}×${String(t.height)} px.`;
    default: return "Transforms the source deterministically.";
  }
}

// --- Errors --------------------------------------------------------------------------------------------------------
export class ActionError extends Error {
  constructor(public code: string, message: string) { super(message); }
}
export function explain(code: string | undefined, message: string | undefined): ActionError {
  if (code === "stale_instruction_confirmation") {
    return new ActionError(code, "References changed — re-enhance or edit the prompt");
  }
  return new ActionError(code ?? "error", message ?? code ?? "failed");
}
export function firstFailure(res: { results?: ItemOutcome[] } | undefined): ActionError | null {
  const bad = res?.results?.find((r) => !r.ok);
  return bad ? explain(bad.code, bad.message) : null;
}
/** Runs a request and rewrites known API error codes into the copy of the design. */
export async function guard<T>(fn: () => Promise<T>): Promise<T> {
  try { return await fn(); } catch (e) {
    if (e instanceof ApiError) throw explain(e.code, e.message);
    throw e;
  }
}

/** True while the key event comes from a text field. */
export function typing(e: KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null;
  return !!el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
}
