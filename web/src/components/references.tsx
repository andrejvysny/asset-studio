import { type PointerEvent, useRef, useState } from "react";

import { artifactUrl, type AssetRow, type Crop, KIND_LABEL, P } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { uploadReference } from "../lib/jobsApi";
import { Dialog, Empty, ErrorLine, Loading } from "./ui";

export const MAX_REFS = 4;

/** A reference chosen in the form, before the Job exists. Uploads are already registered artifacts. */
export interface RefDraft {
  key: string; origin: "upload" | "library"; label: string; artifact_id: string; note: string; crop: Crop | null;
  library?: { asset_id: string; version_id: string };
}

const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));
const pct = (v: number): string => `${v * 100}%`;

/** Drag-a-rectangle overlay producing a normalised crop (origin top-left). */
function CropOverlay({ onDone }: { onDone: (c: Crop) => void }) {
  const [start, setStart] = useState<{ x: number; y: number } | null>(null);
  const [now, setNow] = useState<Crop | null>(null);
  const at = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) };
  };
  const rect = (a: { x: number; y: number }, b: { x: number; y: number }): Crop =>
    ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });
  return (
    <div aria-label="drag to mark region" style={{ position: "absolute", inset: 0, cursor: "crosshair", touchAction: "none",
      background: "#0006" }}
      onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); const p = at(e); setStart(p); setNow(rect(p, p)); }}
      onPointerMove={(e) => { if (start) setNow(rect(start, at(e))); }}
      onPointerUp={(e) => {
        if (!start) return;
        const c = rect(start, at(e));
        setStart(null);
        setNow(null);
        if (c.w >= 0.03 && c.h >= 0.03) onDone(c);
      }}>
      {now && <CropBox crop={now} />}
    </div>
  );
}

function CropBox({ crop }: { crop: Crop }) {
  return <div style={{ position: "absolute", left: pct(crop.x), top: pct(crop.y), width: pct(crop.w), height: pct(crop.h),
    border: "1.5px dashed var(--text)", borderRadius: 3, pointerEvents: "none" }} />;
}

function RefCard({ project, r, onChange, onRemove }:
  { project: string; r: RefDraft; onChange: (patch: Partial<RefDraft>) => void; onRemove: () => void }) {
  const [marking, setMarking] = useState(false);
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 7, overflow: "hidden", background: "var(--card)",
      display: "flex", flexDirection: "column" }}>
      <div className="checker" style={{ position: "relative", minHeight: 60 }}>
        <img src={artifactUrl(project, r.artifact_id)} alt={r.label} style={{ display: "block", width: "100%", height: "auto" }} />
        {r.crop && !marking && <CropBox crop={r.crop} />}
        {marking && <CropOverlay onDone={(c) => { onChange({ crop: c }); setMarking(false); }} />}
        <span className="corner" style={{ left: 6 }}>{r.origin}</span>
        <button aria-label={`remove ${r.label}`} onClick={onRemove}
          style={{ position: "absolute", top: 5, right: 6, fontSize: 12, color: "var(--text-2)", background: "#111213cc",
            borderRadius: 3, padding: "0 6px" }}>×</button>
      </div>
      <div style={{ padding: "7px 8px", display: "flex", flexDirection: "column", gap: 5 }}>
        <span className="mono ellipsis" style={{ fontSize: 11, fontWeight: 500 }}>{r.label}</span>
        <input className="input" aria-label={`note for ${r.label}`} placeholder="What matters in this image?" value={r.note}
          maxLength={500} onChange={(e) => onChange({ note: e.target.value })} style={{ padding: "4px 7px", fontSize: 11.5 }} />
        {marking ? (
          <span className="row" style={{ gap: 8, fontSize: 11 }}>
            <span className="dim">Drag on the image</span>
            <button style={{ color: "var(--muted)", textDecoration: "underline" }}
              onClick={() => { onChange({ crop: { x: 0.25, y: 0.25, w: 0.5, h: 0.5 } }); setMarking(false); }}>use centre</button>
            <button style={{ color: "var(--muted)" }} onClick={() => setMarking(false)}>cancel</button>
          </span>
        ) : (
          <button style={{ fontSize: 11, color: r.crop ? "var(--text)" : "var(--dim)", textAlign: "left" }}
            onClick={() => (r.crop ? onChange({ crop: null }) : setMarking(true))}>
            {r.crop ? "✓ Region marked · clear" : "Mark region that matters"}</button>
        )}
      </div>
    </div>
  );
}

function LibraryPicker({ project, onPick, onClose }:
  { project: string; onPick: (a: AssetRow) => void; onClose: () => void }) {
  const [q, setQ] = useState("");
  const search = useApi<{ items: AssetRow[] }>(
    `${P(project)}/assets?limit=120&planned=0${q ? `&q=${encodeURIComponent(q)}` : ""}`, { project });
  const usable = (search.data?.items ?? []).filter((a) => a.preview_artifact_id && a.current_version_id);
  return (
    <Dialog title="Reference from library" onClose={onClose}>
      <input className="input" aria-label="search library" placeholder="Search assets" value={q}
        onChange={(e) => setQ(e.target.value)} />
      <ErrorLine error={search.error} />
      {!search.data ? <Loading what="assets" /> : usable.length === 0 ? <Empty>No assets with an image or preview.</Empty> : (
        <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))" }}>
          {usable.map((a) => (
            <button key={a.asset_id} className="card" aria-label={`use ${a.display_name}`} onClick={() => onPick(a)}>
              <div className="media checker">
                <img src={artifactUrl(project, a.preview_artifact_id ?? "")} alt="" /></div>
              <div className="meta"><span className="ellipsis" style={{ fontWeight: 500 }}>{a.display_name}</span>
                <span className="sub">{KIND_LABEL[a.kind]}</span></div>
            </button>))}
        </div>)}
    </Dialog>
  );
}

/** Reference-image cards + "Upload image" / "From library" (max 4). Guidance only. */
export function ReferenceEditor({ project, refs, onChange }:
  { project: string; refs: RefDraft[]; onChange: (next: RefDraft[]) => void }) {
  const act = useAction();
  const file = useRef<HTMLInputElement>(null);
  const [picking, setPicking] = useState(false);
  const full = refs.length >= MAX_REFS;
  const patch = (k: string, p: Partial<RefDraft>) => onChange(refs.map((r) => (r.key === k ? { ...r, ...p } : r)));
  const add = (r: Omit<RefDraft, "key" | "note" | "crop">) =>
    onChange([...refs, { ...r, key: crypto.randomUUID(), note: "", crop: null }]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="muted" style={{ fontSize: 12 }}>Reference images · optional</span>
        <span className="sub">guidance only · {refs.length}/{MAX_REFS}</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))", gap: 8 }}>
        {refs.map((r) => (
          <RefCard key={r.key} project={project} r={r} onChange={(p) => patch(r.key, p)}
            onRemove={() => onChange(refs.filter((x) => x.key !== r.key))} />))}
        {!full && (
          <div style={{ border: "1px dashed #33353a", borderRadius: 7, display: "flex", flexDirection: "column",
            justifyContent: "center", gap: 6, padding: 12, minHeight: 120 }}>
            <button className="btn" style={{ padding: "5px 9px", fontSize: 12, textAlign: "center" }} disabled={act.busy}
              onClick={() => file.current?.click()}>{act.busy ? "Uploading…" : "+ Upload image"}</button>
            <button className="btn" style={{ padding: "5px 9px", fontSize: 12, textAlign: "center" }}
              onClick={() => setPicking(true)}>+ From library</button>
            <input ref={file} type="file" accept="image/*" hidden aria-label="upload reference image" onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) void act.run(async () => {
                const out = await uploadReference(project, f);
                add({ origin: "upload", label: f.name, artifact_id: out.artifact_id });
              });
            }} />
          </div>)}
      </div>
      <ErrorLine error={act.error} />
      <span className="dim" style={{ fontSize: 11.5 }}>The text model describes each reference into the prompt, using your note to
        decide what matters. QA then compares every candidate against them. References are not passed to the image model.</span>
      {picking && <LibraryPicker project={project} onClose={() => setPicking(false)} onPick={(a) => {
        setPicking(false);
        add({ origin: "library", label: a.display_name, artifact_id: a.preview_artifact_id ?? "",
          library: { asset_id: a.asset_id, version_id: a.current_version_id ?? "" } });
      }} />}
    </div>
  );
}
