import { type PointerEvent, useEffect, useRef, useState } from "react";

import { MediaPicker } from "../../components/MediaPicker";
import { Dialog, ErrorLine, Loading, WARN } from "../../components/ui";
import { artifactUrl, type AssetList, type AssetRow, type Crop, type ItemEffects, type ItemView, type JobReference, P, type RefBinding } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import * as api from "../../lib/jobsApi";
import { useProject } from "../../lib/project";
import { DIM } from "./jobModel";
import { freshItem } from "./jobActions";

const MAX_REFS = 4;
const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));
const pct = (v: number): string => `${v * 100}%`;

function CropBox({ crop, solid }: { crop: Crop; solid?: boolean }) {
  return <div style={{ position: "absolute", left: pct(crop.x), top: pct(crop.y), width: pct(crop.w), height: pct(crop.h),
    border: `1.5px ${solid ? "solid" : "dashed"} var(--text)`, borderRadius: 3, pointerEvents: "none" }} />;
}

/** Drag a rectangle over the image; produces a normalised crop (origin top-left). */
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
    <div aria-label="drag to mark region" style={{ position: "absolute", inset: 0, cursor: "crosshair", touchAction: "none", background: "#0006" }}
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

interface CardProps { r: JobReference; disabled: boolean; onNote: (note: string) => void; onCrop: (c: Crop | null) => void;
  onRemove: () => void }

function RefCard({ r, disabled, onNote, onCrop, onRemove }: CardProps) {
  const { id } = useProject();
  const [note, setNote] = useState(r.note);
  const [marking, setMarking] = useState(false);
  const label = r.label ?? (r.library ? "library asset" : r.origin === "media" ? "media" : "upload");
  return (
    <div className="jw-ref">
      <div className="media checker">
        <img src={artifactUrl(id, r.artifact_id)} alt={label} />
        {r.crop && !marking && <CropBox crop={r.crop} />}
        {marking && <CropOverlay onDone={(c) => { setMarking(false); onCrop(c); }} />}
        <span className="badge">{r.origin === "library" || r.origin === "media" ? r.origin : "upload"}</span>
        <button className="x" aria-label={`remove reference ${label}`} disabled={disabled} onClick={onRemove}>×</button>
      </div>
      <div className="body">
        <span className="mono ellipsis" style={{ fontSize: 11 }} title={label}>{label}</span>
        <input className="input" style={{ padding: "4px 7px", fontSize: 11.5 }} value={note} disabled={disabled}
          aria-label={`note for ${label}`} placeholder="What matters in this image?"
          onChange={(e) => setNote(e.target.value)}
          onBlur={() => { if (note !== r.note) onNote(note); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
        {r.crop && !marking ? (
          <button className="jw-link" style={{ textAlign: "left", color: "var(--text)" }} disabled={disabled}
            onClick={() => onCrop(null)}>✓ Region marked · clear</button>
        ) : marking ? (
          <span className="row" style={{ gap: 8, fontSize: 11 }}>
            <button className="jw-link" onClick={() => { setMarking(false); onCrop({ x: 0.2, y: 0.2, w: 0.6, h: 0.6 }); }}>Use centre region</button>
            <button className="jw-link" onClick={() => setMarking(false)}>Cancel</button>
          </span>
        ) : (
          <button className="jw-link" style={{ textAlign: "left", fontSize: 11, color: DIM }} disabled={disabled}
            onClick={() => setMarking(true)}>Mark region that matters</button>
        )}
      </div>
    </div>
  );
}

function LibraryPicker({ onPick, onClose }: { onPick: (row: AssetRow) => void; onClose: () => void }) {
  const { id } = useProject();
  const [q, setQ] = useState("");
  const list = useApi<AssetList>(`${P(id)}/assets?limit=60${q ? `&q=${encodeURIComponent(q)}` : ""}`, { project: id });
  const rows = (list.data?.items ?? []).filter((a) => a.current_version_id && a.preview_artifact_id);
  return (
    <Dialog title="Add reference from library" onClose={onClose}>
      <input className="input" aria-label="search library" placeholder="Search assets" value={q} onChange={(e) => setQ(e.target.value)} />
      {!list.data ? <Loading what="assets" /> : rows.length === 0 ? <div className="empty">No assets with a preview.</div> : (
        <div className="jw-refs" style={{ maxHeight: 360, overflow: "auto" }}>
          {rows.map((a) => (
            <button key={a.asset_id} className="jw-ref" style={{ cursor: "pointer", textAlign: "left", color: "inherit", padding: 0 }}
              onClick={() => onPick(a)} aria-label={`use ${a.display_name}`}>
              <div className="media checker"><img src={artifactUrl(id, a.preview_artifact_id!)} alt="" loading="lazy" /></div>
              <div className="body"><span className="mono ellipsis" style={{ fontSize: 11 }}>{a.display_name}</span>
                <span className="sub">v{a.display_version}</span></div>
            </button>
          ))}
        </div>
      )}
      <ErrorLine error={list.error} />
    </Dialog>
  );
}

/** Read-only: project reference-set images that reach the enhancer / compare QA, and references left out (with why). */
function SetReferences({ fx }: { fx: ItemEffects }) {
  const { id } = useProject();
  const g = fx.references.prompt_guidance;
  const q = fx.references.qa_reference;
  const fromSet: [RefBinding, string][] = [...g.selected.map((b): [RefBinding, string] => [b, "set · guidance"]),
    ...q.selected.map((b): [RefBinding, string] => [b, "set · QA"])].filter(([b]) => b.origin === "project_set");
  const excluded = [...g.excluded, ...q.excluded];
  if (fromSet.length === 0 && excluded.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {fromSet.length > 0 && <>
        <span className="label">From project reference set</span>
        <div className="jw-refs" aria-label="from project reference set">
          {fromSet.map(([b, badge], i) => (
            <div key={`${b.id}-${i}`} className="jw-ref">
              <div className="media checker"><img src={artifactUrl(id, b.artifact_id)} alt={`set reference ${b.note || b.id}`} />
                <span className="badge">{badge}</span></div>
              {b.note && <div className="body"><span className="mono ellipsis" style={{ fontSize: 11 }} title={b.note}>{b.note}</span></div>}
            </div>
          ))}
        </div>
      </>}
      {excluded.length > 0 && <>
        <span className="label">Not used</span>
        <ul aria-label="references not used" style={{ margin: 0, paddingLeft: 18, fontSize: 11.5, color: DIM }}>
          {excluded.map((x, i) => <li key={`${x.id}-${i}`}><span className="mono">{x.id}</span> — {x.reason}</li>)}
        </ul>
      </>}
    </div>
  );
}

interface Props { item: ItemView; jobId: string; locked: boolean; changed: boolean; reload: () => void }

export function ReferencesPanel({ item, jobId, locked, changed, reload }: Props) {
  const { id } = useProject();
  const act = useAction();
  const file = useRef<HTMLInputElement>(null);
  const [picking, setPicking] = useState(false);
  const [pickingMedia, setPickingMedia] = useState(false);
  const refs = item.references;
  const [fx, setFx] = useState<ItemEffects | null>(null);
  useEffect(() => {
    let live = true;
    api.getItemEffects(id, jobId, item.id).then((v) => { if (live) setFx(v); }).catch(() => { if (live) setFx(null); });
    return () => { live = false; };
  }, [id, jobId, item.id, item.revision]);
  const disabled = locked || act.busy;
  const rev = async () => (await freshItem(id, jobId, item.id)).revision;
  const run = (fn: () => Promise<unknown>) => void act.run(async () => { await fn(); reload(); });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="label">References · {refs.length}</span>
        <span className="sub" style={{ color: changed ? WARN : DIM }}>{changed ? "changed · applies to next round" : "guidance only"}</span>
      </div>
      <div className="jw-refs">
        {refs.map((r) => (
          <RefCard key={r.id} r={r} disabled={disabled}
            onNote={(note) => run(async () => api.updateReference(id, jobId, item.id, { reference_id: r.id, note, expected_item_revision: await rev() }))}
            onCrop={(crop) => run(async () => api.updateReference(id, jobId, item.id, { reference_id: r.id, crop, expected_item_revision: await rev() }))}
            onRemove={() => run(async () => api.removeReference(id, jobId, item.id, { reference_id: r.id, expected_item_revision: await rev() }))} />
        ))}
        {refs.length < MAX_REFS && (
          <div className="jw-add">
            <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" hidden aria-label="upload reference image" onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) run(async () => {
                const up = await api.uploadReference(id, f);
                await api.addReference(id, jobId, item.id, { artifact_id: up.artifact_id, expected_item_revision: await rev() });
              });
            }} />
            <button className="btn" disabled={disabled} onClick={() => file.current?.click()}>+ Upload</button>
            <button className="btn" disabled={disabled} onClick={() => setPicking(true)}>+ From library</button>
            <button className="btn" disabled={disabled} onClick={() => setPickingMedia(true)}>+ From media</button>
          </div>
        )}
      </div>
      {fx && <SetReferences fx={fx} />}
      <span className="muted" style={{ fontSize: 11.5 }}>Guidance only. Notes become reference cues in the prompt, and QA checks
        candidates against these images.</span>
      <ErrorLine error={act.error} />
      {pickingMedia && <MediaPicker project={id} onClose={() => setPickingMedia(false)} onPick={(m) => {
        setPickingMedia(false);
        run(async () => api.addReference(id, jobId, item.id, { media_id: m.id, note: m.note, label: m.name,
          expected_item_revision: await rev() }));
      }} />}
      {picking && <LibraryPicker onClose={() => setPicking(false)} onPick={(a) => {
        setPicking(false);
        run(async () => api.addReference(id, jobId, item.id, {
          library: { asset_id: a.asset_id, version_id: a.current_version_id!, role: a.kind === "model3d" ? "preview" : "image" },
          expected_item_revision: await rev() }));
      }} />}
    </div>
  );
}
