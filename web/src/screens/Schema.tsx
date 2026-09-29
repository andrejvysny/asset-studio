import { useEffect, useState } from "react";

import { ErrorLine, Loading, PageHead } from "../components/ui";
import { ApiError, type CategoryCfg, type Json, KIND_LABEL, KINDS, type Override, P, send, type StudioConfig } from "../lib/api";
import { useAction } from "../lib/hooks";
import { clone, useConfig, useProject } from "../lib/project";

type FieldType = "kind" | "text" | "int" | "budget" | "ref" | "lora";
const FIELDS: [string, string, FieldType, string?][] = [
  ["kind", "Asset type", "kind"], ["recipe_id", "Pipeline", "ref", "recipes"], ["naming", "Naming rule", "text"],
  ["budget", "Budget (triangles)", "budget"], ["build_profile", "Texturing / build", "text"],
  ["qa_ruleset", "QA rule set", "ref", "qa"], ["reference_set", "Reference set", "ref", "refs"],
  ["style", "Style", "ref", "styles"], ["style_lora", "Style LoRA", "lora"], ["candidate_count", "Candidates", "int"],
];
const RECIPES = ["model3d.default", "icon.default", "sprite.default", "concept.default", "material.default",
  "sheet.default", "vfx.default"];

function display(v: Json): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object" && !Array.isArray(v)) {
    const o = v as Record<string, Json>;
    if ("triangles" in o) { const t = o.triangles as { min: number | null; max: number | null } | null;
      return t ? `${t.min ?? ""}–${t.max ?? ""}` : ""; }
    if ("model_id" in o) return `${String(o.model_id)} @ ${String(o.strength)}`;
  }
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

function parseValue(t: FieldType, raw: string): Json | undefined {
  const s = raw.trim();
  if (!s) return undefined;
  if (t === "int") return Number.isInteger(Number(s)) ? Number(s) : undefined;
  if (t === "budget") { const m = s.match(/^(\d*)\s*[-–]\s*(\d*)$/);
    return m ? { triangles: { min: m[1] ? Number(m[1]) : null, max: m[2] ? Number(m[2]) : null, advisory: true } } : undefined; }
  if (t === "lora") { const m = s.match(/^(\S+)\s*@\s*(-?[\d.]+)$/);
    return m ? { model_id: m[1]!, strength: Number(m[2]) } : undefined; }
  return s;
}

export function Schema() {
  const { id } = useProject();
  const cfg = useConfig();
  const [draft, setDraft] = useState<StudioConfig | null>(null);
  const [selId, setSelId] = useState<string | null>(null);
  const [yaml, setYaml] = useState<string | null>(null);
  const [errors, setErrors] = useState<{ path: string; message: string }[]>([]);
  const act = useAction();
  useEffect(() => { if (cfg.data && !draft) setDraft(clone(cfg.data.config)); }, [cfg.data, draft]);
  if (!cfg.data || !draft) return <Loading what="schema" />;
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfg.data.config);
  const cat = draft.categories.find((c) => c.id === selId) ?? draft.categories[0];
  const eff = cat ? cfg.data.effective[cat.id] : undefined;
  const opts = { recipes: RECIPES, qa: Object.keys(draft.qa_rulesets), refs: Object.keys(draft.reference_sets),
    styles: Object.keys(draft.styles) } as Record<string, string[]>;
  const setCat = (patch: (c: CategoryCfg) => void) => {
    const next = clone(draft);
    const c = next.categories.find((x) => x.id === cat!.id);
    if (c) patch(c);
    setDraft(next);
  };
  const setField = (name: string, ov: Override) => setCat((c) => { c.defaults[name] = ov; });
  const save = (body: object) => act.run(async () => {
    setErrors([]);
    try {
      await send("PATCH", `${P(id)}/config`, body);
      setDraft(null);
      setYaml(null);
      cfg.reload();
    } catch (e) {
      if (e instanceof ApiError && Array.isArray(e.detail)) setErrors(e.detail as { path: string; message: string }[]);
      throw e;
    }
  });
  const tree = cfg.data.categories;
  const unsaved = draft.categories.filter((c) => !tree.some((t) => t.id === c.id));
  return (
    <div className="split wide">
      <aside className="side-list" aria-label="categories">
        <div className="row" style={{ justifyContent: "space-between", padding: "0 8px 8px" }}>
          <span className="label">Categories</span>
          <button className="dim" style={{ fontSize: 12 }} onClick={() => {
            const label = window.prompt("Category name");
            if (!label) return;
            const slug = label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || "category";
            const idBase = cat && window.confirm(`Nest under “${cat.label}”?`) ? cat : null;
            const newId = idBase ? `${idBase.id}.${slug}` : slug;
            const next = clone(draft);
            next.categories.push({ id: newId, parent_id: idBase?.id ?? null, slug, label, archived: false, defaults: {}, metadata: {} });
            setDraft(next);
            setSelId(newId);
          }}>+ Add</button>
        </div>
        {[...tree.map((t) => ({ id: t.id, label: t.label, depth: t.depth, kind: t.kind })),
          ...unsaved.map((u) => ({ id: u.id, label: `${u.label} (unsaved)`, depth: u.parent_id ? 1 : 0, kind: null }))].map((t) => (
          <button key={t.id} className={`side-row${cat?.id === t.id ? " on" : ""}`} style={{ paddingLeft: 8 + t.depth * 16 }}
            onClick={() => setSelId(t.id)}><span>{t.label}</span><span className="n">{t.kind ?? ""}</span></button>
        ))}
        {tree.length === 0 && unsaved.length === 0 && <div className="sub" style={{ padding: 8 }}>No categories. Add one to set inherited defaults.</div>}
      </aside>
      <section className="content" style={{ maxWidth: 980 }}>
        <PageHead sub={cat ? `studio.yaml → categories.${cat.id} · revision ${cfg.data.revision}` : `studio.yaml · revision ${cfg.data.revision}`}
          title={cat?.label ?? "Project schema"}>
          <div className="seg">
            <button className={yaml === null ? "on" : ""} onClick={() => setYaml(null)}>Visual</button>
            <button className={yaml !== null ? "on" : ""} onClick={() => setYaml(cfg.data?.yaml ?? "")}>YAML</button>
          </div>
        </PageHead>
        {yaml !== null ? (
          <>
            <textarea className="input" aria-label="studio.yaml" value={yaml} onChange={(e) => setYaml(e.target.value)} rows={28}
              style={{ font: "400 12px/1.65 var(--mono)" }} spellCheck={false} />
            <div className="row">
              <button className="btn" onClick={() => void act.run(async () => {
                const r = await send<{ ok: boolean; errors: { path: string; message: string }[] }>("POST", `${P(id)}/config:validate`, { yaml });
                setErrors(r.errors);
                if (r.ok) setErrors([{ path: "", message: "valid" }]);
              })}>Validate</button>
              <button className="btn btn-primary" disabled={act.busy || yaml === cfg.data.yaml}
                onClick={() => void save({ expected_revision: cfg.data?.revision, yaml })}>Save YAML</button>
            </div>
          </>
        ) : cat ? (
          <>
            <div className="table">
              <div className="td" style={{ gridTemplateColumns: "150px minmax(120px,1fr) auto" }}>
                <span className="muted" style={{ fontSize: 12 }}>Label</span>
                <input className="input" value={cat.label} onChange={(e) => setCat((c) => { c.label = e.target.value; })} />
                <span className="sub">id {cat.id} · slug {cat.slug}</span>
              </div>
              {FIELDS.map(([name, label, type, ref]) => {
                const ov = cat.defaults[name] ?? { mode: "inherit", value: null };
                const e = eff?.[name];
                const here = ov.mode !== "inherit";
                const src = ov.mode === "disabled" ? "disabled here" : here ? "set here"
                  : e?.source?.startsWith("category:") ? `inherited from ${e.source.slice(9)}` : e?.source ?? "unset";
                const placeholder = ov.mode === "inherit" && e?.value != null ? display(e.value) : "";
                const onChange = (raw: string) => {
                  const v = parseValue(type, raw);
                  setField(name, v === undefined ? { mode: "inherit", value: null } : { mode: "value", value: v });
                };
                return (
                  <div key={name} className="td" style={{ gridTemplateColumns: "150px minmax(120px,1fr) 170px auto" }}>
                    <span className="muted" style={{ fontSize: 12 }}>{label}</span>
                    {type === "kind" || type === "ref" ? (
                      <select className="input" value={ov.mode === "value" ? String(ov.value) : ""} disabled={ov.mode === "disabled"}
                        onChange={(ev) => onChange(ev.target.value)} aria-label={label}>
                        <option value="">{placeholder ? `inherit (${type === "kind" ? KIND_LABEL[placeholder as keyof typeof KIND_LABEL] ?? placeholder : placeholder})` : "inherit (unset)"}</option>
                        {(type === "kind" ? KINDS : opts[ref!] ?? []).map((o) => <option key={o} value={o}>{type === "kind" ? KIND_LABEL[o as keyof typeof KIND_LABEL] : o}</option>)}
                      </select>
                    ) : (
                      <input className="input" aria-label={label} disabled={ov.mode === "disabled"}
                        defaultValue={ov.mode === "value" ? display(ov.value) : ""} key={`${cat.id}-${name}-${ov.mode}`}
                        placeholder={ov.mode === "disabled" ? "disabled" : placeholder || (type === "budget" ? "min–max" : type === "lora" ? "lora_id @ 0.6" : "")}
                        onBlur={(ev) => onChange(ev.target.value)} />
                    )}
                    <span style={{ fontSize: 11.5, color: here ? "var(--text-2)" : "var(--dim)" }}>{src}</span>
                    <div className="row" style={{ gap: 8 }}>
                      {here && <button className="btn-link" style={{ padding: 0, fontSize: 11.5 }} onClick={() => setField(name, { mode: "inherit", value: null })}>reset</button>}
                      {ov.mode !== "disabled" && ["style_lora", "qa_ruleset", "reference_set", "style"].includes(name) &&
                        <button className="btn-link" style={{ padding: 0, fontSize: 11.5 }} onClick={() => setField(name, { mode: "disabled", value: null })}>disable</button>}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="panel" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="label">Naming preview (saved config)</span>
              {(eff?._naming_examples ?? []).map((n, i) => <div key={n} className="row" style={{ fontSize: 12.5 }}>
                <span className="muted" style={{ width: 160 }}>{i === 0 ? "Example item" : "Second example"}</span><span className="mono">{n}</span></div>)}
            </div>
            <div className="row">
              <button className="btn" onClick={() => setCat((c) => { c.archived = !c.archived; })}>{cat.archived ? "Unarchive" : "Archive"}</button>
              <button className="btn" onClick={() => { if (window.confirm(`Delete category ${cat.label}? Refused while assets or shot rows use it.`)) {
                const next = clone(draft); next.categories = next.categories.filter((c) => c.id !== cat.id && c.parent_id !== cat.id); setDraft(next); setSelId(null); } }}>Delete</button>
            </div>
          </>
        ) : <div className="empty">Add a category to define inherited defaults.</div>}
        {yaml === null && (
          <div className="row">
            <button className="btn btn-primary" disabled={!dirty || act.busy} onClick={() => void save({ expected_revision: draft.revision, config: draft })}>
              Save schema</button>
            <button className="btn" disabled={!dirty} onClick={() => setDraft(clone(cfg.data!.config))}>Discard</button>
            {dirty && <span className="sub" style={{ color: "var(--warn)" }}>unsaved changes</span>}
          </div>
        )}
        {errors.map((e, i) => <div key={i} className={e.message === "valid" ? "sub" : "error"}>{e.path ? `${e.path}: ` : ""}{e.message}</div>)}
        <ErrorLine error={act.error} />
        <div className="sub" style={{ fontFamily: "var(--sans)", fontSize: 12 }}>Empty fields inherit from the parent category.
          Changing a spec affects new Jobs only; existing Jobs and published versions keep the snapshot they were made with.</div>
      </section>
    </div>
  );
}
