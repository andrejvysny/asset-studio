import { useEffect, useState } from "react";

import { MediaPicker } from "../components/MediaPicker";
import { ErrorLine, Loading, PageHead, Toggle } from "../components/ui";
import { artifactUrl, KIND_LABEL, KINDS, type Kind, P, type RecipeInfo, type StudioConfig, upload } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { clone, useConfig, useProject } from "../lib/project";

interface Loras { speed: { id: string; file: string; ready: boolean; steps: number }[]; style: { id: string; file: string }[]; note: string }

export function Style() {
  const { id } = useProject();
  const cfg = useConfig();
  const caps = useApi<{ recipes: RecipeInfo[] }>("/api/v1/capabilities");
  const loras = useApi<Loras>("/api/v1/loras");
  const [draft, setDraft] = useState<StudioConfig | null>(null);
  const [tmpl, setTmpl] = useState("model3d.default");
  const act = useAction();
  const [pickFor, setPickFor] = useState<string | null>(null);
  useEffect(() => { if (cfg.data && !draft) setDraft(clone(cfg.data.config)); }, [cfg.data, draft]);
  if (!cfg.data || !draft) return <Loading what="style" />;
  const styleId = Object.keys(draft.styles)[0] ?? "default";
  const style = draft.styles[styleId] ?? { label: "Default", guide: "", negative: "", palette: [] };
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfg.data.config);
  const edit = (fn: (s: typeof style, n: StudioConfig) => void) => { const n = clone(draft);
    n.styles[styleId] ??= clone(style); fn(n.styles[styleId]!, n); setDraft(n); };
  const recipe = caps.data?.recipes.find((r) => r.id === tmpl);
  return (
    <div className="content narrow" style={{ padding: "20px 26px 48px", gap: 24 }}>
      <PageHead sub={`studio.yaml → styles.${styleId} · read by the prompt enhancer and QA; not pasted into image prompts`} title="Style">
        {!(styleId in draft.styles) && <span className="sub">no style profile yet — editing creates “default”</span>}
      </PageHead>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.2fr) minmax(0,1fr)", gap: 20 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="label">Style guide</span>
          <textarea className="input" rows={8} aria-label="style guide" value={style.guide} placeholder="Optional. Empty is fine."
            onChange={(e) => edit((s) => { s.guide = e.target.value; })} />
          <span className="sub" style={{ fontFamily: "var(--sans)" }}>Assign the profile to categories in Schema (field “Style”).</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="label">Palette · reserved colours fail the palette check outside their allowed kinds</span>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8 }}>
            {style.palette.map((p, i) => (
              <div key={i} className="panel" style={{ borderColor: p.reserved ? "var(--warn)" : undefined }}>
                <input type="color" aria-label="colour" value={p.hex} style={{ width: "100%", height: 38, border: 0, padding: 0, background: "none" }}
                  onChange={(e) => edit((s) => { s.palette[i]!.hex = e.target.value; })} />
                <div style={{ padding: "5px 7px", display: "flex", flexDirection: "column", gap: 3 }}>
                  <span className="mono" style={{ fontSize: 10.5 }}>{p.hex}</span>
                  <label className="row" style={{ gap: 5, fontSize: 10.5 }}><Toggle on={p.reserved} label="reserved"
                    onChange={(v) => edit((s) => { s.palette[i]!.reserved = v; })} />reserved</label>
                  {p.reserved && <select className="input" style={{ fontSize: 10, padding: 2 }} aria-label="allowed kind"
                    value={p.allowed_kinds[0] ?? ""} onChange={(e) => edit((s) => { s.palette[i]!.allowed_kinds = e.target.value ? [e.target.value as Kind] : []; })}>
                    <option value="">allowed nowhere</option>{KINDS.map((k) => <option key={k} value={k}>only {KIND_LABEL[k]}</option>)}</select>}
                  <button className="btn-link" style={{ padding: 0, fontSize: 10.5 }} onClick={() => edit((s) => { s.palette.splice(i, 1); })}>remove</button>
                </div>
              </div>
            ))}
            <button className="panel dim" style={{ minHeight: 80 }} onClick={() => edit((s) => { s.palette.push({ hex: "#888888", label: "",
              reserved: false, allowed_kinds: [], allowed_categories: [], tolerance_delta_e: 8 }); })}>+ colour</button>
          </div>
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div className="row"><span className="label grow">Reference sets · up to 4 images · mode says exactly how they are used</span>
          <button className="btn" onClick={() => { const name = window.prompt("Reference set id"); if (!name) return;
            const n = clone(draft); n.reference_sets[name] = { label: name, mode: "prompt_guidance", images: [] }; setDraft(n); }}>+ Set</button></div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))", gap: 12 }}>
          {Object.entries(draft.reference_sets).map(([rid, rs]) => (
            <div key={rid} className="panel" style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
              <div className="row"><span style={{ fontWeight: 500 }} className="grow">{rs.label || rid}</span><span className="sub">{rs.images.length}/4</span></div>
              <select className="input" value={rs.mode} aria-label="reference mode" onChange={(e) => { const n = clone(draft); n.reference_sets[rid]!.mode = e.target.value; setDraft(n); }}>
                <option value="prompt_guidance">prompt guidance (VLM reads refs; image model does not)</option>
                <option value="image_conditioning">image conditioning (blocked: no reference-capable adapter)</option>
                <option value="qa_reference">QA reference</option></select>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 6 }}>
                {rs.images.map((im, j) => <button key={im.artifact_id} title="remove" onClick={() => { const n = clone(draft); n.reference_sets[rid]!.images.splice(j, 1); setDraft(n); }}>
                  <img src={artifactUrl(id, im.artifact_id)} alt={im.label} style={{ width: "100%", aspectRatio: "1", objectFit: "cover", borderRadius: 5 }} /></button>)}
                {rs.images.length < 4 && <label className="panel dim" style={{ aspectRatio: "1", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", borderStyle: "dashed" }}>+
                  <input type="file" accept=".png,.jpg,.jpeg,.webp" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void act.run(async () => {
                    const r = await upload<{ artifact_id: string }>(`${P(id)}/references:upload`, f);
                    const n = clone(draft); n.reference_sets[rid]!.images.push({ artifact_id: r.artifact_id, label: f.name, role: "", source_rights: "unknown" }); setDraft(n); }); }} /></label>}
                {rs.images.length < 4 && <button className="panel dim" aria-label={`add media to ${rid}`} style={{ aspectRatio: "1", fontSize: 11, cursor: "pointer" }}
                  onClick={() => setPickFor(rid)}>media</button>}
              </div>
            </div>
          ))}
          {Object.keys(draft.reference_sets).length === 0 && <div className="empty">No reference sets.</div>}
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 20 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="label">LoRA library</span>
          <div className="table">
            {(loras.data?.speed ?? []).map((l) => <div key={l.id} className="td" style={{ gridTemplateColumns: "minmax(0,1fr) 90px" }}>
              <div style={{ display: "flex", flexDirection: "column" }}><span className="mono" style={{ fontSize: 12 }}>{l.id}</span>
                <span className="dim" style={{ fontSize: 11 }}>speed · {l.steps} steps · {l.file}</span></div>
              <span className="sub" style={{ color: l.ready ? "var(--ok)" : "var(--bad)" }}>{l.ready ? "installed" : "missing"}</span></div>)}
            {(loras.data?.style ?? []).length === 0 && <div className="td sub">No style LoRAs registered on this host (none are bundled).</div>}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}><span className="label" style={{ marginRight: 6 }}>Prompt templates</span>
            {(caps.data?.recipes ?? []).map((r) => <button key={r.id} className={`chip${tmpl === r.id ? " on" : ""}`} style={{ fontSize: 11.5, padding: "2px 8px" }}
              onClick={() => setTmpl(r.id)}>{r.label}</button>)}</div>
          <textarea className="input" readOnly rows={5} value={draft.pipelines[tmpl]?.template ?? recipe?.template ?? ""}
            style={{ font: "400 12px/1.6 var(--mono)", borderStyle: "dashed" }} aria-label="locked template" />
          <span className="sub" style={{ fontFamily: "var(--sans)" }}>Locked technical suffix appended after the enhanced description.
            Negative: <span className="mono">{recipe?.negative || "—"}</span></span>
        </div>
      </div>
      <div className="row">
        <button className="btn btn-primary" disabled={!dirty || act.busy} onClick={() => void act.run(async () => { await cfg.save(draft); setDraft(null); })}>Save style</button>
        <button className="btn" disabled={!dirty} onClick={() => setDraft(clone(cfg.data!.config))}>Discard</button>
      </div>
      <ErrorLine error={act.error} />
      {pickFor && <MediaPicker project={id} onClose={() => setPickFor(null)} onPick={(m) => {
        const n = clone(draft); n.reference_sets[pickFor]?.images.push({ artifact_id: m.artifact_id, label: m.name, role: "",
          source_rights: m.source_rights }); setDraft(n); setPickFor(null); }} />}
    </div>
  );
}
