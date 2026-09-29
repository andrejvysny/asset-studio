import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Bar, Box, Dialog, Empty, ErrorLine, INFO, Loading, OK, PageHead, WARN } from "../components/ui";
import { type BatchGroup, J, type JobSummary, key, send, V2 } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { useProject } from "../lib/project";

export const TAB_NAMES = ["prompts", "candidates", "approve", "build", "publish"] as const;

/** Five aggregate strips with explicit denominators (never one misleading percentage). */
export function stageStrips(b: JobSummary): { label: string; pct: number; color: string }[] {
  const c = b.counts;
  const n = Math.max(c.items, 1);
  const s = (label: string, done: number, of: number, active: boolean) => ({
    label: `${label} ${done}/${of}`, pct: of ? (done / of) * 100 : 0,
    color: of && done >= of ? OK : active ? WARN : INFO });
  return [
    s("prompts", c.confirmed, c.items, b.current_tab === "prompts"),
    s("cands", c.candidates, c.items, b.current_tab === "candidates"),
    s("approved", c.approved, n, b.current_tab === "approve"),
    s("built", c.built, c.approved || 0, b.current_tab === "build"),
    s("published", c.published, c.accepted || c.built || 0, b.current_tab === "publish"),
  ];
}

function AddToBatch({ jobIds, onClose }: { jobIds: string[]; onClose: () => void }) {
  const { id } = useProject();
  const nav = useNavigate();
  const batches = useApi<{ batches: BatchGroup[] }>(`${V2(id)}/batches`, { project: id });
  const act = useAction();
  return (
    <Dialog title={`Add ${jobIds.length} Jobs to a Batch`} onClose={onClose}>
      <span className="sub" style={{ fontFamily: "var(--sans)" }}>A Batch groups Jobs for one coordinated run. Adding
        Jobs never starts inference; it changes the NEXT run of that Batch.</span>
      {!batches.data ? <Loading what="batches" /> : batches.data.batches.length === 0 ? <Empty>No Batches yet.</Empty> : (
        <div className="table">
          {batches.data.batches.map((b) => (
            <button key={b.id} className="td clickable" style={{ gridTemplateColumns: "minmax(0,1fr) auto", textAlign: "left" }}
              disabled={act.busy} onClick={() => void act.run(async () => {
                await send("PATCH", `${V2(id)}/batches/${b.id}`, { expected_revision: b.revision,
                  job_ids: [...b.job_ids, ...jobIds.filter((j) => !b.job_ids.includes(j))] });
                nav(`/p/${id}/batches/${b.id}`);
              })}>
              <span>{b.title} <span className="sub">{b.alias} · {b.jobs} Jobs</span></span>
              <span className="sub">add →</span>
            </button>))}
        </div>)}
      <ErrorLine error={act.error ?? batches.error} />
    </Dialog>
  );
}

export function Jobs() {
  const { id } = useProject();
  const nav = useNavigate();
  const list = useApi<{ jobs: JobSummary[] }>(J(id), { project: id, pollMs: 10000 });
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const act = useAction();
  const jobs = list.data?.jobs ?? [];
  const toggle = (jid: string, on: boolean) => { const n = new Set(sel); if (on) n.add(jid); else n.delete(jid); setSel(n); };
  const createBatch = () => void act.run(async () => {
    const out = await send<{ batch: { id: string } }>("POST", `${V2(id)}/batches`, {
      title: `Batch of ${sel.size} Jobs`, job_ids: [...sel], idempotency_key: key() });
    nav(`/p/${id}/batches/${out.batch.id}`);
  });
  return (
    <div className="content narrow" style={{ maxWidth: 1320 }}>
      <PageHead sub="A Job is one production workflow of one or more items · saving a Job never starts inference" title="Jobs">
        <Link className="btn btn-primary" to={`/p/${id}/jobs/new`} style={{ textDecoration: "none" }}>New Job</Link>
      </PageHead>
      {sel.size > 0 && (
        <div className="row panel" style={{ padding: "8px 12px", gap: 10 }}>
          <span className="muted" style={{ fontSize: 12 }}>{sel.size} Jobs selected</span>
          <button className="btn btn-primary" disabled={act.busy} onClick={createBatch}>Create Batch</button>
          <button className="btn" onClick={() => setAdding(true)}>Add to Batch…</button>
          <button className="btn-link" onClick={() => setSel(new Set())}>Clear</button>
        </div>)}
      <ErrorLine error={list.error ?? act.error} />
      {!list.data ? <Loading what="jobs" /> : jobs.length === 0 ? (
        <Empty>No Jobs yet. A single asset is a Job of one — <Link to={`/p/${id}/jobs/new`}>create one</Link>.</Empty>
      ) : (
        <div className="table">
          {jobs.map((b) => (
            <div key={b.id} className="td" style={{ gridTemplateColumns: "30px minmax(150px,1fr) auto minmax(260px,2fr) auto", padding: "12px 16px" }}>
              <Box on={sel.has(b.id)} label={`select ${b.title}`} onChange={(v) => toggle(b.id, v)} />
              <Link to={`/p/${id}/jobs/${b.id}`} style={{ minWidth: 0, textDecoration: "none", color: "inherit" }}>
                <div style={{ fontWeight: 500 }}>{b.title}</div>
                <div className="sub">{b.alias} · {b.counts.items} items · {b.category_label ?? "no category"}
                  {b.legacy ? " · created before Jobs/Batches" : ""}</div>
              </Link>
              <span className="tag">{b.kind_label}</span>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 4 }}>
                {stageStrips(b).map((st) => (
                  <div key={st.label} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    <Bar pct={st.pct} color={st.color} />
                    <span className="sub" style={{ fontSize: 10 }}>{st.label}</span>
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, alignItems: "center" }}>
                {b.legacy_recipe && <span className="pill warn" title={b.legacy_recipe}>legacy recipe</span>}
                {b.active_run && <span className="pill none" title="continue it from its run">in run {b.active_run.slice(-6)}</span>}
                {b.counts.failed > 0 && <span className="pill bad">{b.counts.failed} failed</span>}
                <span className={`pill ${b.waiting_on_user ? "warn" : b.next_action === "done" ? "ok" : "none"}`}>{b.next_action}</span>
              </div>
            </div>
          ))}
        </div>
      )}
      {adding && <AddToBatch jobIds={[...sel]} onClose={() => setAdding(false)} />}
      <div className="sub" style={{ fontFamily: "var(--sans)", fontSize: 12 }}>
        Stages run as model passes across every Job with compatible ready work; the Runtime screen shows measured passes and loads.</div>
    </div>
  );
}
