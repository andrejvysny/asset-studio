import type { BuildProfile } from "../../lib/api";

export type Geometry = BuildProfile["geometry"];
export type Material = BuildProfile["material"];
export const EMPTY_GEOMETRY: Geometry = { small_components: null, fill_holes: null, expect_single_component: null };
export const EMPTY_MATERIAL: Material = { alpha_mode: null, alpha_cutoff: null, double_sided: null, metallic: null,
  roughness_min: null, roughness_max: null };

export const GEOMETRY_HELP = "applies on the 3D worker export (needs a re-export)";
export const MATERIAL_HELP = "applied to the GLB on the CPU; a material-only change never re-runs the 3D worker";

const ROW = { display: "grid", gridTemplateColumns: "150px minmax(0,1fr)", gap: 8, alignItems: "center" } as const;
const yn = (v: boolean | null): string => (v === null ? "" : v ? "yes" : "no");
const fromYn = (s: string): boolean | null => (s === "" ? null : s === "yes");

function Sel({ label, value, opts, onPick }: { label: string; value: string; opts: string[]; onPick: (v: string) => void }) {
  return (
    <div style={ROW}>
      <span className="muted" style={{ fontSize: 12 }}>{label}</span>
      <select className="input" style={{ width: "auto", maxWidth: 200 }} aria-label={label} value={value} onChange={(e) => onPick(e.target.value)}>
        <option value="">default</option>{opts.map((o) => <option key={o} value={o}>{o}</option>)}</select>
    </div>
  );
}

function Num({ label, value, onPick }: { label: string; value: number | null; onPick: (v: number | null) => void }) {
  return (
    <div style={ROW}>
      <span className="muted" style={{ fontSize: 12 }}>{label}</span>
      <input className="input" type="number" min={0} max={1} step={0.05} aria-label={label} placeholder="default" value={value ?? ""}
        style={{ width: 110 }} onChange={(e) => onPick(e.target.value === "" ? null : Number(e.target.value))} />
    </div>
  );
}

interface Props { geometry: Geometry; material: Material; onGeometry: (g: Geometry) => void; onMaterial: (m: Material) => void }

/** Shared by the profile editor and the rebuild dialog; null = "default" (today's behaviour). */
export function ProfileFields({ geometry: g, material: m, onGeometry, onMaterial }: Props) {
  const setG = (patch: Partial<Geometry>) => onGeometry({ ...g, ...patch });
  const setM = (patch: Partial<Material>) => onMaterial({ ...m, ...patch });
  const cutoff = m.alpha_mode === "mask" || m.alpha_mode === "auto";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span className="label">Geometry</span><span className="sub">{GEOMETRY_HELP}</span>
        <Sel label="small_components" value={g.small_components ?? ""} opts={["remove", "preserve"]}
          onPick={(v) => setG({ small_components: (v || null) as Geometry["small_components"] })} />
        <Sel label="fill_holes" value={g.fill_holes ?? ""} opts={["upstream", "disabled"]}
          onPick={(v) => setG({ fill_holes: (v || null) as Geometry["fill_holes"] })} />
        <Sel label="expect_single_component" value={yn(g.expect_single_component)} opts={["yes", "no"]}
          onPick={(v) => setG({ expect_single_component: fromYn(v) })} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span className="label">Material</span><span className="sub">{MATERIAL_HELP}</span>
        <Sel label="alpha_mode" value={m.alpha_mode ?? ""} opts={["opaque", "mask", "blend", "auto"]}
          onPick={(v) => { const mode = (v || null) as Material["alpha_mode"];
            setM({ alpha_mode: mode, ...(mode === "mask" || mode === "auto" ? {} : { alpha_cutoff: null }) }); }} />
        {cutoff && <Num label="alpha_cutoff" value={m.alpha_cutoff} onPick={(v) => setM({ alpha_cutoff: v })} />}
        <Sel label="double_sided" value={yn(m.double_sided)} opts={["yes", "no"]} onPick={(v) => setM({ double_sided: fromYn(v) })} />
        <Num label="metallic" value={m.metallic} onPick={(v) => setM({ metallic: v })} />
        <Num label="roughness_min" value={m.roughness_min} onPick={(v) => setM({ roughness_min: v })} />
        <Num label="roughness_max" value={m.roughness_max} onPick={(v) => setM({ roughness_max: v })} />
      </div>
    </div>
  );
}

/** Only the set keys, flat, as the rebuild API expects. */
export function flatOverrides(g: Geometry, m: Material): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  for (const [k, v] of [...Object.entries(g), ...Object.entries(m)]) if (v !== null) out[k] = v;
  if (m.alpha_mode !== "mask" && m.alpha_mode !== "auto") delete out.alpha_cutoff;
  return out;
}
