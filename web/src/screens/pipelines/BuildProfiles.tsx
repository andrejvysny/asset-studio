import { useEffect, useState } from "react";

import { ErrorLine } from "../../components/ui";
import { ApiError, type BuildProfile, type Override, type StudioConfig } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { clone, useConfig } from "../../lib/project";
import { EMPTY_GEOMETRY, EMPTY_MATERIAL, ProfileFields } from "../profile/ProfileFields";

export const PROFILE_ID = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

const refs = (o: Override | undefined, id: string): boolean => o?.mode === "value" && o.value === id;
const users = (cfg: StudioConfig, id: string): string[] => [
  ...(refs(cfg.defaults.build_profile, id) ? ["project defaults"] : []),
  ...cfg.categories.filter((c) => refs(c.defaults.build_profile, id)).map((c) => c.label || c.id)];

/** Typed build profiles: "default" (null) keeps today's behaviour for that key. */
export function BuildProfiles() {
  const cfg = useConfig();
  const [draft, setDraft] = useState<StudioConfig | null>(null);
  const [sel, setSel] = useState("");
  const [errors, setErrors] = useState<{ path: string; message: string }[]>([]);
  const act = useAction();
  useEffect(() => { if (cfg.data && !draft) setDraft(clone(cfg.data.config)); }, [cfg.data, draft]);
  if (!cfg.data || !draft) return null;
  const ids = Object.keys(draft.build_profiles);
  const id = sel in draft.build_profiles ? sel : ids[0] ?? "";
  const prof: BuildProfile | undefined = draft.build_profiles[id];
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfg.data.config);
  const edit = (fn: (p: BuildProfile) => void) => { const n = clone(draft); fn(n.build_profiles[id]!); setDraft(n); };
  const used = users(cfg.data.config, id);
  const add = () => {
    const raw = window.prompt("Profile id (a-z, 0-9, _ . -)")?.trim();
    if (!raw) return;
    if (!PROFILE_ID.test(raw)) { window.alert("Invalid id: use a-z, 0-9, _ . - (max 64, start with a letter or digit)"); return; }
    if (raw in draft.build_profiles) { window.alert(`Profile ${raw} already exists`); return; }
    const n = clone(draft);
    n.build_profiles[raw] = { label: raw, geometry: { ...EMPTY_GEOMETRY }, material: { ...EMPTY_MATERIAL } };
    setDraft(n);
    setSel(raw);
  };
  const remove = () => { const n = clone(draft); delete n.build_profiles[id]; setDraft(n); setSel(""); };
  const save = () => void act.run(async () => {
    setErrors([]);
    try { const out = await cfg.save(draft); setDraft(clone(out.config)); } catch (e) {
      if (e instanceof ApiError && Array.isArray(e.detail)) setErrors(e.detail as { path: string; message: string }[]);
      throw e;
    }
  });
  return (
    <section className="panel" aria-label="build profiles" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <span className="label">Build profiles</span>
        {ids.map((p) => <button key={p} className={`chip${p === id ? " on" : ""}`} style={{ fontSize: 11.5, padding: "2px 8px" }}
          onClick={() => setSel(p)}>{p}</button>)}
        <button className="dim" style={{ fontSize: 12 }} onClick={add}>+ Profile</button>
      </div>
      {!prof && <span className="sub">No build profiles. Assign one per category in Schema → Build profile.</span>}
      {prof && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "150px minmax(0,1fr)", gap: 8, alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 12 }}>Label</span>
            <input className="input" aria-label="profile label" value={prof.label} onChange={(e) => edit((p) => { p.label = e.target.value; })} />
          </div>
          <ProfileFields geometry={prof.geometry} material={prof.material}
            onGeometry={(g) => edit((p) => { p.geometry = g; })} onMaterial={(m) => edit((p) => { p.material = m; })} />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn" disabled={used.length > 0} onClick={remove}
              title={used.length ? `Used by ${used.join(", ")}` : ""}>Delete profile</button>
            {used.length > 0 && <span className="sub">used by {used.join(", ")}</span>}
          </div>
        </>
      )}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn btn-primary" disabled={!dirty || act.busy} onClick={save}>Save profiles</button>
        <button className="btn" disabled={!dirty} onClick={() => setDraft(clone(cfg.data!.config))}>Discard</button>
        {dirty && <span className="sub" style={{ color: "var(--warn)" }}>unsaved changes</span>}
      </div>
      {errors.map((e, i) => <div key={i} className="error">{e.path ? `${e.path}: ` : ""}{e.message}</div>)}
      <ErrorLine error={act.error} />
    </section>
  );
}
