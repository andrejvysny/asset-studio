import { useEffect, useState } from "react";

import { ErrorLine, Loading, PageHead, WARN } from "../components/ui";
import { type RecipeInfo, type StudioConfig, type StyleProfile } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { clone, useConfig } from "../lib/project";
import { EffectsPanel } from "./style/EffectsPanel";
import { ReferenceSets } from "./style/ReferenceSets";
import { StyleFields } from "./style/StyleFields";
import { StyleHistory } from "./style/StyleHistory";
import { StylePicker } from "./style/StylePicker";

interface Loras { speed: { id: string; file: string; ready: boolean; steps: number }[]; style: { id: string; file: string }[]; note: string }

export function Style() {
  const cfg = useConfig();
  const caps = useApi<{ recipes: RecipeInfo[] }>("/api/v1/capabilities");
  const loras = useApi<Loras>("/api/v1/loras");
  const [draft, setDraft] = useState<StudioConfig | null>(null);
  const [sel, setSel] = useState("");
  const [tmpl, setTmpl] = useState("model3d.default");
  const act = useAction();
  useEffect(() => { if (cfg.data && !draft) setDraft(clone(cfg.data.config)); }, [cfg.data, draft]);
  if (!cfg.data || !draft) return <Loading what="style" />;
  const styleId = sel in draft.styles ? sel : Object.keys(draft.styles)[0] ?? "default";
  const style: StyleProfile = draft.styles[styleId] ?? { label: "Default", guide: "", negative: "", palette: [] };
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfg.data.config);
  const edit = (fn: (s: StyleProfile) => void) => { const n = clone(draft);
    n.styles[styleId] ??= clone(style); fn(n.styles[styleId]!); setDraft(n); };
  const restore = (content: StyleProfile) => { const n = clone(draft); n.styles[styleId] = clone(content); setDraft(n); };
  const recipe = caps.data?.recipes.find((r) => r.id === tmpl);
  const warnings = cfg.data.warnings ?? [];
  return (
    <div className="content narrow" style={{ padding: "20px 26px 48px", gap: 24 }}>
      <PageHead sub={`studio.yaml → styles.${styleId} · guide goes to the prompt enhancer, negative to the image model, reserved palette to QA`} title="Style">
        {!(styleId in draft.styles) && <span className="sub">no style profile yet — editing creates “default”</span>}
      </PageHead>
      <StylePicker draft={draft} setDraft={setDraft} styleId={styleId} onSelect={setSel} />
      <StyleFields style={style} edit={edit} />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 20, alignItems: "start" }}>
        <StyleHistory styleId={styleId} saved={styleId in cfg.data.config.styles} configRevision={cfg.data.revision} onRestore={restore} />
        <EffectsPanel categories={cfg.data.config.categories} configRevision={cfg.data.revision} dirty={dirty}
          styleId={styleId} savedStyleIds={Object.keys(cfg.data.config.styles)} />
      </div>
      <ReferenceSets draft={draft} setDraft={setDraft} />
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
        <button className="btn btn-primary" disabled={!dirty || act.busy} onClick={() => void act.run(async () => { const out = await cfg.save(draft); setDraft(clone(out.config)); })}>Save style</button>
        <button className="btn" disabled={!dirty} onClick={() => setDraft(clone(cfg.data!.config))}>Discard</button>
      </div>
      {warnings.length > 0 && <ul aria-label="config warnings" style={{ margin: 0, paddingLeft: 18, color: WARN, fontSize: 12 }}>
        {warnings.map((w) => <li key={w.path}><span className="mono">{w.path}</span> {w.message}</li>)}</ul>}
      <ErrorLine error={act.error} />
    </div>
  );
}
