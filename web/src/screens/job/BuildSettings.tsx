import { useState } from "react";

import type { RebuildOverrides } from "../../lib/api";

export interface SettingsBase { triangles: number | null; texture_size: number | null; remesh: boolean | null }

const PIPELINES = ["512", "1024", "1024_cascade", "1536_cascade"];
const TEXTURES = [1024, 2048, 4096];

function Opts<T extends string | number | boolean>({ label, opts, value, show, onPick }:
  { label: string; opts: T[]; value: T | null; show?: (o: T) => string; onPick: (o: T) => void }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "118px minmax(0,1fr)", gap: 8, alignItems: "center" }}>
      <span style={{ fontSize: 12, color: "var(--dim)" }}>{label}</span>
      <div className="row" style={{ gap: 4, flexWrap: "wrap" }} role="group" aria-label={label}>
        {opts.map((o) => <button key={String(o)} className={`jw-opt${value === o ? " on" : ""}`} aria-pressed={value === o}
          onClick={() => onPick(o)}>{show ? show(o) : String(o)}</button>)}
      </div>
    </div>
  );
}

/** "Change settings, rebuild": only settings that differ from the last attempt are sent (rebuild needs at least one). */
export function BuildSettings({ base, busy, onRun, onCancel }:
  { base: SettingsBase; busy: boolean; onRun: (o: RebuildOverrides) => void; onCancel: () => void }) {
  const [tris, setTris] = useState<string>(base.triangles ? String(base.triangles) : "");
  const [tex, setTex] = useState<number | null>(base.texture_size);
  const [remesh, setRemesh] = useState<boolean | null>(base.remesh);
  const [pipe, setPipe] = useState<string | null>(null);
  const overrides: RebuildOverrides = {};
  const n = Number(tris);
  if (tris && Number.isFinite(n) && n >= 1000 && n <= 2_000_000 && n !== base.triangles) overrides.triangles = Math.round(n);
  if (tex !== null && tex !== base.texture_size) overrides.texture_size = tex;
  if (remesh !== null && remesh !== base.remesh) overrides.remesh = remesh;
  if (pipe) overrides.pipeline_type = pipe;
  const invalidTris = tris !== "" && !(n >= 1000 && n <= 2_000_000);
  const changed = Object.keys(overrides).length;
  return (
    <div className="jw-box" style={{ display: "flex", flexDirection: "column", gap: 8, background: "var(--card)", borderColor: "var(--line-3)" }}>
      <span style={{ fontWeight: 500 }}>Change settings, then rebuild</span>
      <div style={{ display: "grid", gridTemplateColumns: "118px minmax(0,1fr)", gap: 8, alignItems: "center" }}>
        <label htmlFor="jw-tris" style={{ fontSize: 12, color: "var(--dim)" }}>Target triangles</label>
        <input id="jw-tris" className="input" type="number" min={1000} max={2000000} value={tris} placeholder="from category"
          style={{ padding: "3px 7px", maxWidth: 160 }} onChange={(e) => setTris(e.target.value)} aria-invalid={invalidTris} />
      </div>
      <Opts label="Texture size" opts={TEXTURES} value={tex} show={(o) => `${o}²`} onPick={setTex} />
      <Opts label="Remesh (cleanup)" opts={[false, true]} value={remesh} show={(o) => (o ? "on" : "off")} onPick={setRemesh} />
      <Opts label="Mesh resolution" opts={PIPELINES} value={pipe} onPick={(o) => setPipe(pipe === o ? null : o)} />
      {invalidTris && <span className="error">Triangles must be between 1000 and 2000000.</span>}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn btn-primary" style={{ padding: "5px 12px" }} disabled={busy || !changed || invalidTris}
          title={changed ? "" : "Change at least one setting"} onClick={() => onRun(overrides)}>Rebuild</button>
        <button className="btn-link" onClick={onCancel}>Cancel</button>
        {!changed && <span className="sub">change at least one setting</span>}
      </div>
    </div>
  );
}
