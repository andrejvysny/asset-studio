import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Box, ErrorLine, Loading, PageHead, relTime, WARN } from "../../components/ui";
import { type BatchGroupDetail, type JobSummary, J, key, KIND_LABEL, type RunPlan, send, V2 } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { runPill } from "./RunView";

const ACTION_LABEL: Record<string, string> = { enhance: "will be enhanced", at_gate: "waits at a review gate",
  excluded: "excluded", done: "already published" };

function PlanPreview({ plan, onStart, busy }: { plan: RunPlan; onStart: () => void; busy: boolean }) {
  return (
    <div className="panel" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="label">Run plan · frozen · stops at prompt review</span>
        <span className="mono dim" style={{ fontSize: 11 }}>{plan.plan_sha256.slice(0, 12)}</span></div>
      <span className="muted" style={{ fontSize: 12.5 }}>{plan.counts.jobs} Jobs · {plan.counts.items} items ·
        {" "}{plan.counts.enhance} to enhance · {plan.counts.at_gate} at a gate · {plan.counts.excluded} excluded ·
        {" "}{plan.counts.done} done</span>
      {Object.entries(plan.residency_groups).map(([r, n]) => <span key={r} className="sub">model group {r.split(":")[0]}: {n} tasks in one residency</span>)}
      {Object.entries(plan.preflight).filter(([, v]) => v !== "ready").map(([k, v]) =>
        <span key={k} className="sub" style={{ color: WARN }}>{k}: {v}</span>)}
      <div className="table">
        {plan.jobs.flatMap((j) => j.items.map((it) => (
          <div key={it.item_id} className="td" style={{ gridTemplateColumns: "minmax(140px,1fr) minmax(140px,1fr) minmax(200px,1.4fr)" }}>
            <span className="sub">{j.title} · {KIND_LABEL[j.kind]}</span><span>{it.name}</span>
            <span className="sub" style={{ color: it.action === "excluded" ? WARN : undefined }}>{ACTION_LABEL[it.action]} · {it.reason}</span>
          </div>)))}
      </div>
      <div className="row"><button className="btn btn-primary" disabled={busy || plan.counts.enhance + plan.counts.at_gate === 0}
        onClick={onStart}>Start Batch</button>
        <span className="sub" style={{ fontFamily: "var(--sans)" }}>Starting authorizes enhancement only; every later gate is your explicit action.</span></div>
    </div>
  );
}

export function BatchDetail() {
  const { id } = useProject();
  const { batchId = "" } = useParams();
  const nav = useNavigate();
  const b = useApi<BatchGroupDetail>(`${V2(id)}/batches/${batchId}`, { project: id, pollMs: 10000 });
  const jobs = useApi<{ jobs: JobSummary[] }>(J(id), { project: id });
  const [plan, setPlan] = useState<RunPlan | null>(null);
  const [editing, setEditing] = useState<Set<string> | null>(null);
  const act = useAction();
  if (!b.data) return b.error ? <div className="content"><ErrorLine error={b.error} /></div> : <Loading what="batch" />;
  const batch = b.data;
  const active = batch.run_history.find((r) => !r.closed_at && r.status !== "completed");
  return (
    <div className="content narrow" style={{ maxWidth: 1180 }}>
      <Link to={`/p/${id}/batches`} className="sub" style={{ textDecoration: "none" }}>← Batches / {batch.alias}</Link>
      <PageHead sub={`${batch.jobs} Jobs · ${batch.items} items · ${batch.kinds.map((k) => KIND_LABEL[k]).join(", ") || "no Jobs"} · revision ${batch.revision}`}
        title={batch.title}>
        {active ? <Link className="btn btn-primary" to={`/p/${id}/runs/${active.id}`} style={{ textDecoration: "none" }}>Open active run →</Link>
          : <button className="btn btn-primary" disabled={act.busy || batch.jobs === 0}
            onClick={() => void act.run(async () => setPlan(await send<RunPlan>("POST", `${V2(id)}/batches/${batchId}:plan`, {})))}>
            Plan run…</button>}
      </PageHead>
      {plan && !active && <PlanPreview plan={plan} busy={act.busy} onStart={() => void act.run(async () => {
        const out = await send<{ run_id: string }>("POST", `${V2(id)}/batches/${batchId}:start`, {
          plan_id: plan.plan_id, plan_sha256: plan.plan_sha256, batch_revision: plan.batch_revision, idempotency_key: key() });
        nav(`/p/${id}/runs/${out.run_id}`);
      })} />}
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="label">Jobs in this Batch</span>
        {editing ? <span className="row">
          <button className="btn btn-primary" disabled={act.busy} onClick={() => void act.run(async () => {
            await send("PATCH", `${V2(id)}/batches/${batchId}`, { expected_revision: batch.revision, job_ids: [...editing] });
            setEditing(null); setPlan(null); b.reload(); })}>Save membership</button>
          <button className="btn" onClick={() => setEditing(null)}>Cancel</button></span>
          : <button className="btn" onClick={() => setEditing(new Set(batch.job_ids))}>Edit membership</button>}
      </div>
      {editing && <span className="sub" style={{ fontFamily: "var(--sans)" }}>Membership changes apply to the next run; a started run keeps its frozen selection. Removing a Job never deletes it.</span>}
      <div className="table">
        {(editing ? jobs.data?.jobs ?? [] : batch.jobs_detail).map((j) => (
          <div key={j.id} className="td" style={{ gridTemplateColumns: "30px minmax(160px,1fr) 120px minmax(160px,1fr) 150px" }}>
            {editing ? <Box on={editing.has(j.id)} label={`include ${j.title}`} onChange={(v) => {
              const n = new Set(editing); if (v) n.add(j.id); else n.delete(j.id); setEditing(n); }} /> : <span />}
            <Link to={`/p/${id}/jobs/${j.id}`}>{j.title} <span className="sub">{j.alias}</span></Link>
            <span className="tag">{j.kind_label}</span>
            <span className="sub">{j.counts.items} items · {j.next_action}</span>
            <span className="sub">{j.active_run ? `in run ${j.active_run.slice(-6)}` : "idle"}</span>
          </div>))}
      </div>
      <span className="label">Run history</span>
      {batch.run_history.length === 0 ? <span className="sub">Never run.</span> : (
        <div className="table">
          {batch.run_history.slice().reverse().map((r) => (
            <Link key={r.id} to={`/p/${id}/runs/${r.id}`} className="td clickable" style={{ textDecoration: "none", color: "inherit",
              gridTemplateColumns: "140px minmax(200px,1fr) 150px" }}>
              <span className="mono" style={{ fontSize: 11.5 }}>{r.id.slice(-10)} · {relTime(r.created_at)}</span>
              <span className="sub">{r.counts.items} items · {r.counts.prompts_confirmed} confirmed · {r.counts.approved} approved ·
                {" "}{r.counts.builds_valid} valid builds · {r.counts.published} published</span>
              {runPill(r.status)}
            </Link>))}
        </div>)}
      <ErrorLine error={act.error ?? jobs.error} />
    </div>
  );
}
