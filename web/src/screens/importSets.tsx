import type { Kind } from "../lib/api";

export interface FramesInfo { count: number; size: [number, number]; order: string[] }
export interface MapInfo { filename: string; suggested_role: string | null; image: { width: number; height: number } }
export interface AtlasParams { fps: number; columns: number; padding: number; pow2: boolean; pivot: string; blend: string }

export const DEFAULT_ATLAS: AtlasParams = { fps: 12, columns: 0, padding: 2, pow2: false, pivot: "bottom_center", blend: "alpha" };

/** Only parameters the target recipe declares are sent: the server rejects unknown keys. */
export function atlasPayload(p: AtlasParams, kind: Kind): Record<string, number | boolean | string> {
  const base = { fps: p.fps, columns: p.columns, padding: p.padding, pow2: p.pow2 };
  return kind === "vfx_flipbook" ? { ...base, blend: p.blend } : { ...base, pivot: p.pivot };
}

function Num({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number;
  onChange: (v: number) => void }) {
  return <label className="field grow"><span>{label}</span>
    <input className="input" type="number" min={min} max={max} value={value}
      onChange={(e) => onChange(Number(e.target.value))} /></label>;
}

export function FramesOptions({ info, kind, value, onChange }: { info: FramesInfo; kind: Kind; value: AtlasParams;
  onChange: (v: AtlasParams) => void }) {
  const set = (patch: Partial<AtlasParams>) => onChange({ ...value, ...patch });
  const shown = info.order.slice(0, 6);
  return (
    <div className="panel" style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="sub">{info.count} frames · {info.size[0]}×{info.size[1]} px · natural filename order:{" "}
        <span className="mono">{shown.join(", ")}{info.order.length > shown.length ? ", …" : ""}</span></span>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <Num label="FPS" value={value.fps} min={1} max={120} onChange={(fps) => set({ fps })} />
        <Num label="Columns (0 = auto)" value={value.columns} min={0} max={256} onChange={(columns) => set({ columns })} />
        <Num label="Padding px" value={value.padding} min={0} max={64} onChange={(padding) => set({ padding })} />
      </div>
      <div className="row" style={{ alignItems: "flex-start" }}>
        {kind === "vfx_flipbook" ? (
          <label className="field grow"><span>Blend</span>
            <select className="input" value={value.blend} onChange={(e) => set({ blend: e.target.value })}>
              <option value="alpha">alpha</option><option value="additive">additive</option></select></label>
        ) : (
          <label className="field grow"><span>Pivot</span>
            <select className="input" value={value.pivot} onChange={(e) => set({ pivot: e.target.value })}>
              <option value="bottom_center">bottom centre</option><option value="center">centre</option></select></label>
        )}
        <label className="row grow" style={{ gap: 6, marginTop: 20 }}>
          <input type="checkbox" checked={value.pow2} onChange={(e) => set({ pow2: e.target.checked })} />
          <span className="sub">power-of-two atlas</span></label>
      </div>
    </div>
  );
}

export function MaterialMapping({ maps, roles, value, onChange }: { maps: MapInfo[]; roles: string[];
  value: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const used = Object.values(value).filter(Boolean);
  const dup = used.length !== new Set(used).size;
  return (
    <div className="panel">
      <div className="sub" style={{ padding: "8px 12px" }}>Map every file to one role. Suggestions come from filename
        suffixes and are never applied silently — check each row.</div>
      {maps.map((m) => (
        <div key={m.filename} className="row tr" style={{ padding: "6px 12px" }}>
          <span className="mono grow ellipsis" style={{ fontSize: 12 }}>{m.filename}</span>
          <span className="sub mono">{m.image.width}×{m.image.height}</span>
          <select className="input" aria-label={`role for ${m.filename}`} value={value[m.filename] ?? ""}
            onChange={(e) => onChange({ ...value, [m.filename]: e.target.value })} style={{ width: 200 }}>
            <option value="">— choose —</option>
            {roles.map((r) => <option key={r} value={r}>{r}{m.suggested_role === r ? " · suggested" : ""}</option>)}
          </select>
        </div>
      ))}
      {dup && <div className="error" style={{ padding: "6px 12px" }}>each role can be used once</div>}
    </div>
  );
}

export function mappingReady(maps: MapInfo[], value: Record<string, string>): boolean {
  const used = maps.map((m) => value[m.filename]).filter(Boolean);
  return used.length === maps.length && new Set(used).size === used.length && used.includes("base_color");
}
