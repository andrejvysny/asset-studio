import { useEffect, useState } from "react";

import { ErrorLine, Loading, NONE, OK, PageHead, BAD, Toggle } from "../components/ui";
import { KIND_LABEL, type QaRuleCfg, type StudioConfig } from "../lib/api";
import { useAction } from "../lib/hooks";
import { clone, useConfig } from "../lib/project";

const METRICS = ["mask_margin", "mask_fill", "mask_single_blob", "min_resolution", "palette_reserved"];
const METRIC_SOURCE: Record<string, string> = { mask_margin: "mask_metric", mask_fill: "mask_metric",
  mask_single_blob: "mask_metric", min_resolution: "image_metric", palette_reserved: "image_metric" };
const INTEGRITY = ["decode", "hash_matches_approval", "glb reload · geometry · indices", "frame metadata (Phase 5)"];

export function QaRules() {
  const cfg = useConfig();
  const [draft, setDraft] = useState<StudioConfig | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const act = useAction();
  useEffect(() => { if (cfg.data && !draft) setDraft(clone(cfg.data.config)); }, [cfg.data, draft]);
  if (!cfg.data || !draft) return <Loading what="QA rules" />;
  const ids = Object.keys(draft.qa_rulesets);
  const setId = sel && ids.includes(sel) ? sel : ids[0];
  const rs = setId ? draft.qa_rulesets[setId] : undefined;
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfg.data.config);
  const edit = (fn: (rules: QaRuleCfg[]) => void) => { const n = clone(draft); fn(n.qa_rulesets[setId!]!.rules); setDraft(n); };
  const usedBy = cfg.data.config.categories.filter((c) => cfg.data?.effective[c.id]?.qa_ruleset?.value === setId).map((c) => c.id);
  return (
    <div className="split">
      <aside className="side-list">
        <div className="row" style={{ justifyContent: "space-between", padding: "0 8px 8px" }}><span className="label">Rule sets</span>
          <button className="dim" style={{ fontSize: 12 }} onClick={() => { const name = window.prompt("Rule set id (e.g. sprite or props_strict)");
            if (!name) return; const n = clone(draft); n.qa_rulesets[name] = { label: name, kind: null, rules: [], policy: { minor_fail_limit: 2 } };
            setDraft(n); setSel(name); }}>+ Add</button></div>
        {ids.map((i) => <button key={i} className={`side-row${i === setId ? " on" : ""}`} onClick={() => setSel(i)}>
          <span>{draft.qa_rulesets[i]!.label || i}</span><span className="n">{draft.qa_rulesets[i]!.rules.length}</span></button>)}
      </aside>
      <section className="content" style={{ maxWidth: 1100 }}>
        {!rs ? <div className="empty">No rule sets. Without rules every candidate is “unverified” (never green by default).</div> : <>
          <PageHead sub={`qa_rulesets.${setId} · used by ${usedBy.length ? usedBy.join(", ") : setId && setId in KIND_LABEL ? `all ${setId} items by default` : "nothing yet"}`}
            title={`${rs.label || setId} checks`}>
            <button className="btn" onClick={() => edit((r) => r.push({ id: `check_${r.length + 1}`, source: "vlm", stage: "candidate",
              enabled: true, severity: "minor", question: "Describe the requirement as a yes/no statement.", metric: null, params: {} }))}>+ VLM question</button>
            <select className="input" aria-label="add metric" value="" onChange={(e) => { const m = e.target.value; if (m) edit((r) => r.push({
              id: m, source: METRIC_SOURCE[m]!, stage: "candidate", enabled: true, severity: "minor", question: null, metric: m, params: {} })); }}>
              <option value="">+ Metric…</option>{METRICS.map((m) => <option key={m}>{m}</option>)}</select>
          </PageHead>
          <div className="row panel" style={{ padding: "10px 14px", gap: 18, flexWrap: "wrap", fontSize: 12 }}>
            <span><span style={{ color: OK }}>recommended</span> = every applicable check ran, no major fail, fewer than {rs.policy.minor_fail_limit} minor fails</span>
            <span><span style={{ color: BAD }}>not recommended</span> = any major fail, or {rs.policy.minor_fail_limit}+ minor fails</span>
            <span><span style={{ color: NONE }}>unverified</span> = a check could not run, or no checks apply</span>
          </div>
          <div className="table">
            {rs.rules.map((r, i) => (
              <div key={i} className="td" style={{ gridTemplateColumns: "40px 160px 110px minmax(220px,1fr) 90px 90px 30px", minWidth: 800, opacity: r.enabled ? 1 : 0.55 }}>
                <Toggle on={r.enabled} label={`enable ${r.id}`} onChange={(v) => edit((x) => { x[i]!.enabled = v; })} />
                <input className="input" value={r.id} aria-label="rule id" onChange={(e) => edit((x) => { x[i]!.id = e.target.value; })} />
                <span className="tag" style={{ textAlign: "center" }}>{r.source === "vlm" ? "VLM" : r.metric}</span>
                {r.source === "vlm" ? <input className="input" value={r.question ?? ""} aria-label="question" style={{ fontFamily: "var(--sans)" }}
                  onChange={(e) => edit((x) => { x[i]!.question = e.target.value; })} />
                  : <span className="mono muted" style={{ fontSize: 11.5 }}>{JSON.stringify(r.params)} (defaults from metric registry)</span>}
                <select className="input" value={r.severity} aria-label="severity" onChange={(e) => edit((x) => { x[i]!.severity = e.target.value as "major" | "minor"; })}>
                  <option>major</option><option>minor</option></select>
                <span className="sub">{r.stage}</span>
                <button className="btn-link" aria-label="remove" onClick={() => edit((x) => { x.splice(i, 1); })}>✕</button>
              </div>
            ))}
          </div>
          <div className="panel" style={{ padding: "10px 14px", display: "flex", flexDirection: "column", gap: 4 }}>
            <span className="label">Mandatory structural validation · locked, never overridable</span>
            {INTEGRITY.map((x) => <span key={x} className="mono dim" style={{ fontSize: 11.5 }}>🔒 {x}</span>)}
          </div>
          <div className="row">
            <button className="btn btn-primary" disabled={!dirty || act.busy} onClick={() => void act.run(async () => { await cfg.save(draft); setDraft(null); })}>
              Save rules</button>
            <button className="btn" disabled={!dirty} onClick={() => setDraft(clone(cfg.data!.config))}>Discard</button>
            <span className="sub" style={{ fontFamily: "var(--sans)", fontSize: 12 }}>Edits apply to new batches; existing evaluations keep the rules they ran with.</span>
          </div>
          <ErrorLine error={act.error} />
        </>}
      </section>
    </div>
  );
}
