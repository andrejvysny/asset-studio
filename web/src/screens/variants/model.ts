// Form model of the Create-variants wizard: local rows, conversion to/from the API shapes, client-side checks
// that mirror the server's `validate_rows`, and readable text for API error codes.

import {
  ApiError,
  type Constraint, type DraftWork, type GlbAnchor, type GlbTransform, type GlbTransformIn, type Json, type Kind,
  type RasterTransform, type RasterTransformIn, type RowIn, type VariantIntent, type VariantMethod, type VariantRow,
} from "../../lib/api";

export const GENERATIVE: VariantMethod[] = ["image_edit_reconstruct", "image_edit"];
export const isDirect = (m: VariantMethod): boolean => m === "direct_transform";

export const METHOD_LABEL: Record<VariantMethod, string> = {
  image_edit_reconstruct: "Structural reconstruction", image_edit: "Design variant", direct_transform: "Direct size transform",
};
export const methodSub = (m: VariantMethod, kind: Kind): string => m === "image_edit_reconstruct"
  ? "Edits a render of the source with your change request. You approve a candidate, then a new 3D build runs. "
    + "New mesh: topology, UVs and hidden detail are not preserved."
  : m === "image_edit"
    ? "Edits the source image with your change request. You approve a candidate, then the result is built as a new asset."
    : kind === "model3d" ? "Scales the original GLB. Topology and materials are kept. No AI, one deterministic result per row."
      : "Resizes or pads the original image. No AI, one deterministic result per row.";

export const INTENTS: { id: VariantIntent; label: string; note: string }[] = [
  { id: "subtle", label: "Subtle", note: "Small changes. The family resemblance stays strong." },
  { id: "related", label: "Related", note: "Clear differences with the same identity. Default." },
  { id: "exploratory", label: "Exploratory", note: "Larger departures. Identity may drift; QA flags low resemblance." },
];
export const DEFAULT_PRESERVE = "Keep the same asset identity, material treatment and overall style.";
export const PRESERVE_ID = "preserve_identity";
export const STYLE_CONFLICT_TEXT = "The source was created under an earlier project style. New generated variants use the "
  + "current style; exact appearance matching may conflict.";
export const CANDIDATE_OPTIONS = [2, 4, 6, 8];
export const MAX_ROWS = 32;

export const WARNING_TEXT: Record<string, string> = {
  blend_approximated: "transparent (BLEND) materials are shown as cut-outs at 50% alpha",
  texture_unrendered: "a base colour texture could not be sampled (missing UVs); those parts show material colour only",
  sampler_wrap_ignored: "texture clamp/mirror wrap modes are rendered as repeat",
  texture_missing: "no base colour texture or material colour; the reference shows flat grey shading only",
};
/** Readable text for a reference warning; `unsupported_extension:<name>` carries the glTF extension name. */
export const warningText = (w: string): string => WARNING_TEXT[w]
  ?? (w.startsWith("unsupported_extension:") ? `glTF extension ${w.slice(22)} is not rendered` : w);
export const VIEW_LABEL: Record<string, string> = {
  three_quarter: "Three-quarter", rear: "Rear", side: "Side", image: "Image",
};

export type TransformOp = "target_height" | "uniform_scale" | "axis_scale" | "resize_keep_aspect" | "pad_canvas";
export const GLB_OPS: { id: TransformOp; label: string }[] = [
  { id: "target_height", label: "Target height (m)" }, { id: "uniform_scale", label: "Uniform scale factor" },
  { id: "axis_scale", label: "Axis scale x, y, z" },
];
export const RASTER_OPS: { id: TransformOp; label: string }[] = [
  { id: "resize_keep_aspect", label: "Resize (keep aspect)" }, { id: "pad_canvas", label: "Pad canvas" },
];
export const ANCHORS: { id: GlbAnchor; label: string }[] = [
  { id: "source_origin", label: "Source origin" }, { id: "bounds_center", label: "Bounds centre" },
  { id: "bottom_center", label: "Bottom centre" },
];

/** Editable transform fields as strings so half-typed numbers survive re-renders. */
export interface TransformForm {
  op: TransformOp; v1: string; v2: string; v3: string; anchor: GlbAnchor; units: boolean;
  placement: "center" | "bottom_center" | "top_left"; background: "transparent" | "source_edge";
  resample: "lanczos" | "nearest";
}
export interface LocalRow {
  key: string; id?: string; label: string; change: string; height: string; t: TransformForm;
  /** Server value kept so unchanged fields (duplicate ack) are echoed back. */
  confirmDuplicate: boolean;
}

export const defaultTransform = (kind: Kind): TransformForm => ({
  op: kind === "model3d" ? "target_height" : "resize_keep_aspect", v1: "", v2: "", v3: "", anchor: "bottom_center",
  units: false, placement: "center", background: "transparent", resample: "lanczos",
});
export const newKey = (): string => `k${Math.random().toString(36).slice(2, 10)}`;
export const blankRow = (kind: Kind, n: number): LocalRow => ({
  key: newKey(), label: `Variant ${n}`, change: "", height: "", t: defaultTransform(kind), confirmDuplicate: false,
});

const str = (n: number): string => String(n);
function transformFromServer(kind: Kind, g: GlbTransform | null, r: RasterTransform | null): TransformForm {
  const t = defaultTransform(kind);
  if (g) {
    t.op = g.op; t.anchor = g.anchor;
    if (g.op === "target_height") { t.v1 = str(g.height_m); t.units = g.units_confirmed; }
    else if (g.op === "uniform_scale") t.v1 = str(g.factor);
    else { t.v1 = str(g.x); t.v2 = str(g.y); t.v3 = str(g.z); }
  } else if (r) {
    t.op = r.op;
    if (r.op === "resize_keep_aspect") { t.v1 = str(r.max_width); t.v2 = str(r.max_height); t.resample = r.resample; }
    else {
      t.v1 = str(r.width); t.v2 = str(r.height); t.placement = r.placement;
      t.background = r.background === "source_edge" ? "source_edge" : "transparent";
    }
  }
  return t;
}
export function rowFromServer(r: VariantRow, kind: Kind, prev?: LocalRow): LocalRow {
  return { key: prev?.key ?? newKey(), id: r.id, label: r.label, change: r.change_request,
    height: r.final_height_m === null ? "" : str(r.final_height_m),
    t: transformFromServer(kind, r.glb_transform, r.raster_transform), confirmDuplicate: r.confirm_duplicate };
}

const num = (s: string): number | null => {
  const v = s.trim() === "" ? NaN : Number(s.trim().replace(",", "."));
  return Number.isFinite(v) ? v : null;
};
const within = (v: number | null, lo: number, hi: number): v is number => v !== null && v >= lo && v <= hi;
const wholePx = (v: number | null): v is number => v !== null && Number.isInteger(v) && v >= 1 && v <= 8192;

/** null when the fields do not (yet) describe a valid transform. */
export function glbTransform(t: TransformForm): GlbTransformIn | null {
  const a = num(t.v1);
  if (t.op === "target_height") return within(a, 1e-4, 10000) ? { op: "target_height", height_m: a, anchor: t.anchor, units_confirmed: t.units } : null;
  if (t.op === "uniform_scale") return within(a, 1e-3, 1000) ? { op: "uniform_scale", factor: a, anchor: t.anchor } : null;
  if (t.op === "axis_scale") {
    const b = num(t.v2), c = num(t.v3);
    return within(a, 1e-3, 1000) && within(b, 1e-3, 1000) && within(c, 1e-3, 1000)
      ? { op: "axis_scale", x: a, y: b, z: c, anchor: t.anchor } : null;
  }
  return null;
}
export function rasterTransform(t: TransformForm): RasterTransformIn | null {
  const a = num(t.v1), b = num(t.v2);
  if (!wholePx(a) || !wholePx(b)) return null;
  if (t.op === "resize_keep_aspect") return { op: "resize_keep_aspect", max_width: a, max_height: b, resample: t.resample };
  if (t.op === "pad_canvas") return { op: "pad_canvas", width: a, height: b, placement: t.placement, background: t.background };
  return null;
}

export function rowToApi(r: LocalRow, i: number, method: VariantMethod, kind: Kind): RowIn {
  const out: RowIn = { label: r.label.trim() || `Variant ${i + 1}`, change_request: r.change, confirm_duplicate: r.confirmDuplicate };
  if (r.id) out.id = r.id;
  if (isDirect(method)) {
    if (kind === "model3d") out.glb_transform = glbTransform(r.t); else out.raster_transform = rasterTransform(r.t);
  } else if (kind === "model3d") {
    const h = num(r.height);
    out.final_height_m = h !== null && h > 0 ? h : null;
  }
  return out;
}

export function rowProblem(r: LocalRow, method: VariantMethod, kind: Kind): string | null {
  if (!isDirect(method)) {
    if (!r.change.trim()) return "describe what should differ";
    if (kind === "model3d" && r.height.trim() && !within(num(r.height), 1e-4, 10000)) return "final height must be a number of metres";
    return null;
  }
  if (kind !== "model3d") return rasterTransform(r.t) ? null : "enter whole pixel sizes (1–8192)";
  if (!glbTransform(r.t)) return r.t.op === "target_height" ? "enter a target height in metres" : "enter valid scale values";
  if (r.t.op === "target_height" && !r.t.units) return "confirm that the source uses metres before an exact height";
  return null;
}

export function workOf(method: VariantMethod, rows: number, candidates: number): DraftWork {
  return isDirect(method) ? { rows, image_edits: 0, builds: rows, transforms: rows }
    : { rows, image_edits: rows * candidates, builds: rows, transforms: 0 };
}

export const preserveToApi = (text: string): Constraint[] => text.trim()
  ? [{ id: PRESERVE_ID, text: text.trim(), enforcement: "advisory_visual" }] : [];
export const preserveFromApi = (c: Constraint[]): string => c.find((x) => x.id === PRESERVE_ID)?.text ?? c[0]?.text ?? "";

// --- errors --------------------------------------------------------------------------------------------------------
export interface Problem { text: string; details: string[]; code: string; status: number }
const ERROR_TEXT: Record<string, string> = {
  style_source_conflict: STYLE_CONFLICT_TEXT + " Acknowledge the style change below to continue.",
  references_missing: "The source reference images are not prepared yet. They are being prepared; try saving again.",
  source_family_changed: "The source's family changed since this draft was made. Start a new draft to pick up the change.",
  source_version_mismatch: "The source version changed since this draft was made. Start a new draft.",
  draft_materialized: "This draft was already saved as Jobs. Start a new draft to make more variants.",
  stale_variant_plan: "The draft was changed elsewhere (another tab or a planning task). The latest version was reloaded; check your last edit.",
  empty_plan: "Add at least one row first.",
  conflicting_variant_requirements: "Some rows have conflicting requirements.",
  plan_task_active: "A planning task is still running for this draft. Wait for it to finish.",
  rows_not_empty: "The plan already has rows; the suggestion was appended instead.",
  no_suggestion: "There is no suggestion to apply.",
  too_many_rows: `A plan holds at most ${MAX_ROWS} rows.`,
  reference_conditioning_unavailable: "The source reference images cannot be prepared.",
  unsupported_source_features: "The source uses glTF features the reference renderer cannot show faithfully, so it cannot condition image edits.",
  enforcement_unsupported: "Only advisory constraints are supported; no machine check exists for this one.",
  engine_unavailable: "The image-edit workflow is not runnable in ComfyUI right now.",
  family_fixed: "The source already belongs to a family, so the family name cannot be changed here.",
  source_integrity_failed: "The source files failed verification, so no variants can be made from this version.",
  source_not_published: "That version is not a published version of this asset.",
  unknown_asset: "This asset does not exist.",
};
export function describeError(e: unknown): Problem {
  if (!(e instanceof ApiError)) return { text: (e as Error).message, details: [], code: "error", status: 0 };
  const details: string[] = [];
  if (Array.isArray(e.detail)) {
    for (const d of e.detail as Json[]) {
      const o = d as { label?: string; message?: string } | null;
      if (o && typeof o === "object" && !Array.isArray(o) && o.message) details.push(`${o.label ? `${o.label}: ` : ""}${o.message}`);
    }
  }
  const known = ERROR_TEXT[e.code];
  const text = known ?? `${e.message} (${e.code})`;
  return { text, details, code: e.code, status: e.status };
}
