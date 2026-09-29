import { type ReactNode } from "react";

import { INFO, WARN } from "../../components/progress";
import {
  artifactUrl, type Capabilities, type FamilyRef, type Kind, KIND_LABEL, type MethodCapability, type ReferenceSet,
  type Suggestion, type VariantMethod,
} from "../../lib/api";
import { relevantMethods } from "./useVariantDraft";
import {
  ANCHORS, GLB_OPS, isDirect, type LocalRow, methodSub, METHOD_LABEL, RASTER_OPS, type TransformForm, type TransformOp,
  VIEW_LABEL, WARNING_TEXT,
} from "./model";

export interface VersionChip { version_id: string; display_version: number }

export function SourceCard(p: {
  project: string; caps: Capabilities; versions: VersionChip[]; onVersion: (id: string) => void;
  thumbId: string | null; family: FamilyRef | null; familyName: string; onFamilyName: (v: string) => void;
}) {
  const s = p.caps.source;
  return (
    <aside className="vz-src" aria-label="source">
      <div className="thumbbox stripes">
        {p.thumbId ? <img src={artifactUrl(p.project, p.thumbId)} alt={`${s.display_name} v${s.display_version}`} />
          : <span>source render · v{s.display_version}</span>}
      </div>
      <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
        <span className="label">Source · exact version</span>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          <span style={{ fontWeight: 500 }}>{s.display_name}</span>
          <span className="sub">{s.asset_id} · {KIND_LABEL[s.kind]}</span>
        </div>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }} role="group" aria-label="source version">
          {p.versions.map((v) => (
            <button key={v.version_id} className="vz-vchip" aria-pressed={v.version_id === s.version_id}
              onClick={() => v.version_id !== s.version_id && p.onVersion(v.version_id)}>v{v.display_version}</button>
          ))}
        </div>
        <span className="label" style={{ marginTop: 6 }}>Family</span>
        {p.family ? <span style={{ fontSize: 12.5 }}>{p.family.name} · new variants join it</span> : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <input className="input" aria-label="family name" value={p.familyName} onChange={(e) => p.onFamilyName(e.target.value)}
              maxLength={120} style={{ fontFamily: "var(--sans)", fontSize: 12.5 }} />
            <span className="vz-note">New family. This asset becomes its anchor.</span>
          </div>
        )}
        <span className="vz-note" style={{ marginTop: 4 }}>
          Each variant is published as a new asset. The source and its versions are not changed.</span>
      </div>
    </aside>
  );
}

export function MethodPicker({ caps, value, onChange }: { caps: Capabilities; value: VariantMethod; onChange: (m: VariantMethod) => void }) {
  const kind = caps.source.kind;
  const list = relevantMethods(kind).map((m) => caps.methods.find((x) => x.method === m)).filter((x): x is MethodCapability => !!x);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span className="vz-lab" id="vz-method-label">Method</span>
      <div className="vz-methods" role="radiogroup" aria-labelledby="vz-method-label">
        {list.map((m) => {
          const experimental = m.warnings.find((w) => w.toLowerCase().startsWith("experimental"));
          return (
            <button key={m.method} role="radio" aria-checked={value === m.method} aria-disabled={!m.available}
              className="vz-method" onClick={() => m.available && onChange(m.method)}>
              <span style={{ fontWeight: 500, display: "flex", gap: 8, alignItems: "center" }}>
                {METHOD_LABEL[m.method]}
                {experimental && <span className="pill warn" title={experimental}>experimental</span>}
              </span>
              <span className="vz-note">{methodSub(m.method, kind)}</span>
              {experimental && <span className="vz-note" style={{ color: WARN }}>{experimental}</span>}
              {!m.available && <span className="vz-note" style={{ color: "var(--bad)" }}>
                Unavailable: {m.message || m.reason}{m.reason ? ` (${m.reason})` : ""}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Num({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return <input className="input mono-in" aria-label={label} value={value} placeholder={placeholder} inputMode="decimal"
    onChange={(e) => onChange(e.target.value)} />;
}

function TransformEditor({ n, kind, t, onChange }: { n: number; kind: Kind; t: TransformForm; onChange: (t: TransformForm) => void }) {
  const set = (patch: Partial<TransformForm>) => onChange({ ...t, ...patch });
  const ops = kind === "model3d" ? GLB_OPS : RASTER_OPS;
  return (
    <div className="vz-tf">
      <select className="input" aria-label={`row ${n} transform`} value={t.op} onChange={(e) => set({ op: e.target.value as TransformOp })}>
        {ops.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
      </select>
      {t.op === "target_height" && <Num label={`row ${n} target height (m)`} value={t.v1} onChange={(v) => set({ v1: v })} placeholder="m" />}
      {t.op === "uniform_scale" && <Num label={`row ${n} scale factor`} value={t.v1} onChange={(v) => set({ v1: v })} placeholder="×" />}
      {t.op === "axis_scale" && (["v1", "v2", "v3"] as const).map((k, i) => (
        <Num key={k} label={`row ${n} scale ${"xyz"[i]}`} value={t[k]} onChange={(v) => set({ [k]: v })} placeholder={"xyz"[i]} />))}
      {kind === "model3d" && (
        <select className="input" aria-label={`row ${n} anchor`} value={t.anchor} onChange={(e) => set({ anchor: e.target.value as TransformForm["anchor"] })}>
          {ANCHORS.map((a) => <option key={a.id} value={a.id}>anchor: {a.label}</option>)}
        </select>
      )}
      {t.op === "target_height" && (
        <label className="row" style={{ gap: 6, fontSize: 11.5, color: "var(--muted)" }}>
          <input type="checkbox" checked={t.units} onChange={(e) => set({ units: e.target.checked })} aria-label={`row ${n} units are meters`} />
          units are meters
        </label>
      )}
      {(t.op === "resize_keep_aspect" || t.op === "pad_canvas") && (<>
        <Num label={`row ${n} ${t.op === "pad_canvas" ? "canvas width" : "max width"}`} value={t.v1} onChange={(v) => set({ v1: v })} placeholder="width px" />
        <Num label={`row ${n} ${t.op === "pad_canvas" ? "canvas height" : "max height"}`} value={t.v2} onChange={(v) => set({ v2: v })} placeholder="height px" />
      </>)}
      {t.op === "resize_keep_aspect" && (
        <select className="input" aria-label={`row ${n} resample`} value={t.resample} onChange={(e) => set({ resample: e.target.value as TransformForm["resample"] })}>
          <option value="lanczos">Lanczos</option><option value="nearest">Nearest (pixel art)</option>
        </select>
      )}
      {t.op === "pad_canvas" && (<>
        <select className="input" aria-label={`row ${n} placement`} value={t.placement} onChange={(e) => set({ placement: e.target.value as TransformForm["placement"] })}>
          <option value="center">place: centre</option><option value="bottom_center">place: bottom centre</option><option value="top_left">place: top left</option>
        </select>
        <select className="input" aria-label={`row ${n} background`} value={t.background} onChange={(e) => set({ background: e.target.value as TransformForm["background"] })}>
          <option value="transparent">background: transparent</option><option value="source_edge">background: source edge</option>
        </select>
      </>)}
    </div>
  );
}

export function RowsTable(p: {
  method: VariantMethod; kind: Kind; rows: LocalRow[]; problems: (string | null)[];
  onEdit: (key: string, patch: Partial<LocalRow>) => void; onRemove: (key: string) => void;
}) {
  const direct = isDirect(p.method);
  const showHeight = !direct && p.kind === "model3d";
  const cols = direct ? "80px minmax(120px,.8fr) minmax(320px,2.4fr) 30px"
    : showHeight ? "80px minmax(120px,.8fr) minmax(220px,2fr) 110px 30px" : "80px minmax(120px,.8fr) minmax(220px,2fr) 30px";
  return (
    <div className="vz-table" role="table" aria-label="plan rows">
      <div className="vz-tr head" role="row" style={{ gridTemplateColumns: cols }}>
        <span>Row id</span><span>Name</span><span>{direct ? "Transform" : "Change request"}</span>
        {showHeight && <span>Final height (m)</span>}<span />
      </div>
      {p.rows.length === 0 && <div className="vz-tr sub" style={{ gridTemplateColumns: "1fr" }}>No rows yet. Add a row for each new asset.</div>}
      {p.rows.map((r, i) => (
        <div key={r.key} className="vz-tr" role="row" style={{ gridTemplateColumns: cols }}>
          <span className="sub ellipsis" style={{ color: "var(--faint)", fontSize: 10.5, paddingTop: 7 }} title={r.id ?? "not saved yet"}>{r.id ?? "new"}</span>
          <input className="input" aria-label={`row ${i + 1} name`} value={r.label} onChange={(e) => p.onEdit(r.key, { label: e.target.value })} maxLength={120} />
          {direct ? <TransformEditor n={i + 1} kind={p.kind} t={r.t} onChange={(t) => p.onEdit(r.key, { t })} /> : (
            <input className="input" aria-label={`row ${i + 1} change request`} value={r.change} placeholder="What should differ?"
              onChange={(e) => p.onEdit(r.key, { change: e.target.value })} maxLength={2000} />
          )}
          {showHeight && <input className="input mono-in" aria-label={`row ${i + 1} final height`} value={r.height} placeholder="unchanged"
            inputMode="decimal" onChange={(e) => p.onEdit(r.key, { height: e.target.value })} />}
          <button className="vz-x" aria-label={`remove row ${i + 1}`} onClick={() => p.onRemove(r.key)}>×</button>
          {p.problems[i] && <span className="vz-note" style={{ gridColumn: "2 / -1", color: WARN }}>Row {i + 1}: {p.problems[i]}</span>}
        </div>
      ))}
    </div>
  );
}

export function ReferencesPanel(p: { project: string; state: string; refs: ReferenceSet | null; error: string | null;
  onSelect: (view: string) => void; onRetry: () => void }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span className="vz-lab">Source references · pick the primary view</span>
      {p.state === "preparing" && <span className="sub" style={{ color: INFO }}>Preparing reference images…</span>}
      {p.state === "error" && <div className="banner bad" role="alert"><span>{p.error}</span>
        <button className="vz-small" onClick={p.onRetry}>Retry</button></div>}
      {p.refs && (<>
        <div className="vz-refs" role="group" aria-label="reference views">
          {p.refs.images.map((im) => (
            <button key={im.artifact_id} className="vz-ref" aria-pressed={im.role === "primary"} onClick={() => p.onSelect(im.view)}>
              <img src={artifactUrl(p.project, im.artifact_id)} alt={`${VIEW_LABEL[im.view] ?? im.view} view`} loading="lazy" />
              <span>{VIEW_LABEL[im.view] ?? im.view}{im.role === "primary" ? " · primary" : ""}</span>
            </button>
          ))}
        </div>
        {p.refs.warnings.map((w) => <span key={w} className="vz-note" style={{ color: WARN }}>{WARNING_TEXT[w] ?? w}</span>)}
      </>)}
    </div>
  );
}

export function SuggestionBox({ s, onUse, busy }: { s: Suggestion; onUse: () => void; busy: boolean }) {
  return (
    <div className="panel" style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6 }} aria-label="suggested rows">
      <div className="row"><span className="label grow">Suggested rows · not applied yet</span>
        <button className="btn btn-primary" style={{ padding: "4px 12px", fontSize: 12 }} disabled={busy} onClick={onUse}>Use these rows</button></div>
      {s.rows.map((r, i) => (
        <div key={i} style={{ fontSize: 12.5 }}><b style={{ fontWeight: 500 }}>{r.label}</b><span className="muted"> · {r.change_request}</span></div>
      ))}
      {s.short_by > 0 && <span className="vz-note" style={{ color: WARN }}>{s.short_by} fewer rows than requested.</span>}
      {s.notes.map((n, i) => <span key={i} className="vz-note">{n}</span>)}
    </div>
  );
}

export function Field({ label, children }: { label: ReactNode; children: ReactNode }) {
  return <label style={{ display: "flex", flexDirection: "column", gap: 6 }}><span className="vz-lab">{label}</span>{children}</label>;
}

