import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Box, ErrorLine, relTime } from "../../components/ui";
import { NextPill, ProgressPills, INFO, OK, WARN, W } from "../../components/progress";
import { type BatchGroup, get, send, V2 } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { type BatchCtx, plural } from "./batchModel";
import { runPill } from "./batchUi";

const COLS = "52px minmax(200px,1.4fr) 84px 40px minmax(240px,1.6fr) 170px 64px";
const MONO = { font: "500 11px 'Geist Mono', monospace", color: "#a3a4a1" } as const;

export function preflightText(n: number): string {
  return `Start enhances ${plural(n, "prompt")} in one pass on GPU1 with Qwen3-VL, then stops for prompt review. `
    + "No images are generated until you confirm. Jobs that already have rounds are not re-enhanced.";
}

function Picker({ ctx, onDone }: { ctx: BatchCtx; onDone: () => void }) {
  const { batch, all, project } = ctx;
  const [sel, setSel] = useState<Set<string>>(new Set());
  const act = useAction();
  const candidates = all.filter((j) => !batch.job_ids.includes(j.id) && !j.archived_at);
  const add = () => void act.run(async () => {
    const ids = [...sel];
    if (!ids.length) return;
    await send("PATCH", `${V2(project)}/batches/${batch.id}`, {
      expected_revision: batch.revision, job_ids: [...batch.job_ids, ...ids] });
    // Move semantics: drop the Jobs from every other Batch (each with its own fresh revision).
    const others = await get<{ batches: BatchGroup[] }>(`${V2(project)}/batches`);
    for (const o of others.batches) {
      if (o.id === batch.id || !o.job_ids.some((j) => sel.has(j))) continue;
      await send("PATCH", `${V2(project)}/batches/${o.id}`, {
        expected_revision: o.revision, job_ids: o.job_ids.filter((j) => !sel.has(j)) });
    }
    ctx.reload();
    onDone();
  });
  return (
    <div style={{ border: "1px solid #3a3c40", borderRadius: 8, overflow: "hidden", background: "#15161a" }}>
      {candidates.length === 0 && <div style={{ padding: "12px 14px", color: "#8b8c87", fontSize: 12.5 }}>
        Every saved Job of this project is already in this Batch.</div>}
      {candidates.map((j) => {
        const on = sel.has(j.id);
        const toggle = (v: boolean) => { const n = new Set(sel); if (v) n.add(j.id); else n.delete(j.id); setSel(n); };
        return (
          <div key={j.id} onClick={() => toggle(!on)} style={{ cursor: "pointer", display: "grid",
            gridTemplateColumns: "30px minmax(180px,1fr) 90px minmax(140px,.8fr) 150px", gap: 12, padding: "8px 14px",
            borderTop: "1px solid #222326", alignItems: "center" }}>
            <Box on={on} label={`add ${j.title}`} onChange={toggle} />
            <span style={{ fontWeight: 500 }}>{j.title}</span>
            <span style={MONO}>{j.kind_label}</span>
            <span style={{ ...MONO, color: "#8b8c87" }}>{j.batch ? `in ${j.batch.title}` : "no Batch"}</span>
            <span style={{ fontSize: 12, color: "#a3a4a1", textAlign: "right" }}>{j.progress.label}</span>
          </div>);
      })}
      <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "10px 14px", borderTop: "1px solid #26282b" }}>
        <button className="btn btn-primary" style={{ padding: "5px 12px", fontSize: 12.5 }} disabled={act.busy || !sel.size} onClick={add}>
          Add {sel.size} Jobs</button>
        <span style={{ fontSize: 12, color: "#8b8c87" }}>Jobs already in another Batch move here.</span>
      </div>
      <ErrorLine error={act.error} />
    </div>
  );
}

export function Overview({ ctx, preflight }: { ctx: BatchCtx; preflight: boolean }) {
  const { batch, jobs, stats, project, active } = ctx;
  const [params] = useSearchParams();
  const [picker, setPicker] = useState(params.get("add") === "1");
  const act = useAction();
  const patch = (ids: string[]) => void act.run(async () => {
    await send("PATCH", `${V2(project)}/batches/${batch.id}`, { expected_revision: batch.revision, job_ids: ids });
    ctx.reload();
  });
  const move = (i: number, d: number) => {
    const ids = jobs.map((j) => j.id);
    ids.splice(i + d, 0, ...ids.splice(i, 1));
    patch(ids);
  };
  const counts: [string, number, string][] = [["Jobs", jobs.length, W], ["Drafts", stats.drafts, "#c9cac6"],
    ["Running", stats.run, INFO], ["Waiting on you", stats.wait, WARN], ["Published", stats.done, OK]];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))", gap: 1, background: "#26282b",
        border: "1px solid #26282b", borderRadius: 8, overflow: "hidden" }}>
        {counts.map(([k, v, color]) => (
          <div key={k} style={{ background: "#111213", padding: "9px 12px", display: "flex", flexDirection: "column", gap: 1 }}>
            <span style={{ fontSize: 11, color: "#8b8c87" }}>{k}</span>
            <span style={{ font: "600 15px 'Geist Mono', monospace", color }}>{v}</span></div>))}
      </div>
      {preflight && <div role="note" style={{ border: "1px solid #3a3c40", borderRadius: 8, padding: "10px 14px", background: "#15161a",
        fontSize: 12.5, color: "#c9cac6" }}>{preflightText(stats.enhanceItems)}</div>}
      {jobs.length > 0 && (
        <div className="table" role="table" aria-label="Jobs in this Batch">
          <div className="th" role="row" style={{ gridTemplateColumns: COLS, minWidth: 980, padding: "9px 14px" }}>
            <span>Order</span><span>Job</span><span>Kind</span><span>Rnd</span><span>Progress</span>
            <span style={{ textAlign: "right" }}>Next</span><span /></div>
          {jobs.map((j, i) => {
            const to = `/p/${project}/jobs/${j.id}`;
            return (
              <div key={j.id} className="td" role="row" style={{ gridTemplateColumns: COLS, minWidth: 980, padding: "9px 14px" }}>
                <span style={{ display: "flex", gap: 2 }}>
                  <button className="btn-link" style={{ padding: "0 5px" }} aria-label={`Move ${j.title} up`} disabled={act.busy || i === 0}
                    onClick={() => move(i, -1)}>↑</button>
                  <button className="btn-link" style={{ padding: "0 5px" }} aria-label={`Move ${j.title} down`}
                    disabled={act.busy || i === jobs.length - 1} onClick={() => move(i, 1)}>↓</button></span>
                <Link to={to} style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0, textDecoration: "none", color: "inherit" }}>
                  <span className="ellipsis" style={{ fontWeight: 500 }}>{j.title}</span>
                  <span className="sub">{j.alias} · {j.variant ? "variant" : j.source}</span></Link>
                <span style={MONO}>{j.kind_label}</span>
                <span style={MONO}>{j.rounds ? `R${j.rounds}` : "—"}</span>
                <ProgressPills job={j} />
                <span style={{ display: "flex", justifyContent: "flex-end" }}>
                  <Link to={to} style={{ textDecoration: "none" }}><NextPill progress={j.progress} /></Link></span>
                <button className="btn-link" style={{ justifySelf: "end", fontSize: 12, padding: 0 }}
                  title="Remove from Batch (the Job is kept)" aria-label={`Remove ${j.title} from Batch`} disabled={act.busy}
                  onClick={() => patch(jobs.filter((x) => x.id !== j.id).map((x) => x.id))}>Remove</button>
              </div>);
          })}
        </div>)}
      {jobs.length === 0 && <div className="empty">No Jobs yet. Add saved Jobs from this project.</div>}
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <button className="btn" style={{ padding: "6px 12px" }} aria-expanded={picker} onClick={() => setPicker(!picker)}>
          {picker ? "Close" : "+ Add Jobs"}</button>
        <span style={{ fontSize: 12, color: "#8b8c87" }}>Removing a Job from the Batch never deletes it or its history.</span>
      </div>
      {picker && <Picker ctx={ctx} onDone={() => setPicker(false)} />}
      <ErrorLine error={act.error} />
      <RunHistory ctx={ctx} activeId={active?.id ?? null} />
    </div>
  );
}

function RunHistory({ ctx, activeId }: { ctx: BatchCtx; activeId: string | null }) {
  const runs = ctx.batch.run_history.slice().reverse();
  if (!runs.length) return null;
  return (
    <section aria-label="Runs" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span className="label">Runs</span>
      <div className="table">
        {runs.map((r) => (
          <div key={r.id} className="td" style={{ gridTemplateColumns: "150px minmax(200px,1fr) 150px", padding: "6px 14px" }}>
            <span className="mono" style={{ fontSize: 11.5 }}>{r.id.slice(-10)} · {relTime(r.created_at)}</span>
            <span className="sub">{r.counts.items} items · {r.counts.prompts_confirmed} confirmed · {r.counts.approved} approved ·
              {" "}{r.counts.builds_valid} valid builds · {r.counts.published} published{r.id === activeId ? " · active" : ""}</span>
            {runPill(r.status)}
          </div>))}
      </div>
    </section>
  );
}

