import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { INFO, WARN } from "../../components/progress";
import { ErrorLine, Loading } from "../../components/ui";
import { type BatchGroupDetail, J, type JobSummary, key, type RunPlan, send, V2 } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { activeRun, type BatchCtx, batchStats, plural } from "./batchModel";
import { ExecutionTab } from "./ExecutionTab";
import { Overview } from "./Overview";
import { ReviewTab } from "./ReviewTab";

const TABS = ["overview", "review", "execution"] as const;
type Tab = (typeof TABS)[number];
const LABEL: Record<Tab, string> = { overview: "Overview", review: "Review", execution: "Execution" };

export function BatchDetail() {
  const { id } = useProject();
  const { batchId = "", tab: tabParam } = useParams();
  const nav = useNavigate();
  const b = useApi<BatchGroupDetail>(`${V2(id)}/batches/${batchId}`, { project: id, pollMs: 5000 });
  const all = useApi<{ jobs: JobSummary[] }>(J(id), { project: id, pollMs: 10000 });
  const act = useAction();
  const [notice, setNotice] = useState<string | null>(null);
  if (!b.data) return b.error ? <div className="content"><ErrorLine error={b.error} /></div> : <Loading what="batch" />;
  const batch = b.data;
  const tab: Tab = TABS.find((t) => t === tabParam) ?? "overview";
  const go = (t: Tab) => nav(`/p/${id}/batches/${batchId}/${t}`);
  const jobs = batch.jobs_detail;
  const stats = batchStats(jobs);
  const active = activeRun(batch.run_history);
  const ctx: BatchCtx = { project: id, batch, jobs, all: all.data?.jobs ?? [], active, stats,
    reload: () => { b.reload(); all.reload(); } };

  // Start = plan + start in one click; the plan is frozen and hashed server-side, so we bind its id and hash.
  const start = () => void act.run(async () => {
    setNotice(null);
    const plan = await send<RunPlan>("POST", `${V2(id)}/batches/${batchId}:plan`, {});
    const bad = Object.entries(plan.preflight).filter(([, v]) => v !== "ready");
    if (bad.length) { setNotice(`Not started. Preflight: ${bad.map(([k, v]) => `${k} ${v}`).join("; ")}`); return; }
    if (!plan.counts.enhance) { setNotice("Nothing to enhance: every Job already has prompts or is managed by an open run."); return; }
    await send("POST", `${V2(id)}/batches/${batchId}:start`, { plan_id: plan.plan_id, plan_sha256: plan.plan_sha256,
      batch_revision: plan.batch_revision, idempotency_key: key() });
    ctx.reload();
    go("execution");
  });

  const primary = stats.drafts
    ? { label: `Start Batch · enhance ${plural(stats.enhanceItems, "prompt")}`, on: start, enabled: !act.busy && stats.enhanceItems > 0 }
    : stats.wait ? { label: `Review ${stats.wait} waiting`, on: () => go("review"), enabled: true }
      : { label: stats.run ? "Running…" : "Nothing to do", on: () => undefined, enabled: false };
  const tabs: [Tab, string, string][] = [
    ["overview", `${plural(jobs.length, "Job")} · ${plural(stats.drafts, "draft")}`, "#8b8c87"],
    ["review", stats.wait ? `${stats.wait} waiting on you` : "nothing waiting", stats.wait ? WARN : "#8b8c87"],
    ["execution", stats.run ? `${stats.run} running` : "idle", stats.run ? INFO : "#8b8c87"]];

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100%" }}>
      <div style={{ padding: "16px 24px 0", display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <Link to={`/p/${id}/batches`} className="sub" style={{ textDecoration: "none" }}>← Batches / {batch.alias}</Link>
          <h1 className="h1" style={{ margin: "2px 0 0" }}>{batch.title}</h1>
          <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            {[plural(jobs.length, "Job"), stats.kinds, stats.status].map((t) => <span key={t} className="tag">{t}</span>)}
          </div>
        </div>
        <button className={`btn${primary.enabled ? " btn-primary" : ""}`} style={{ padding: "8px 16px" }} disabled={!primary.enabled}
          onClick={primary.on}>{primary.label}</button>
      </div>
      <div role="tablist" style={{ margin: "14px 24px 0", display: "grid", gridTemplateColumns: "repeat(3,minmax(0,1fr))",
        border: "1px solid #26282b", borderRadius: 8, overflow: "hidden" }}>
        {tabs.map(([t, sub, color]) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => go(t)} style={{
            cursor: "pointer", padding: "9px 12px", border: 0, borderRight: "1px solid #26282b", textAlign: "left",
            background: tab === t ? "#1c1d20" : "transparent", display: "flex", flexDirection: "column", gap: 3,
            borderBottom: `2px solid ${tab === t ? "#e8e8e6" : "transparent"}` }}>
            <span style={{ font: "500 10.5px 'Geist Mono', monospace", color }}>{sub}</span>
            <span style={{ color: tab === t ? "#fff" : "#c9cac6" }}>{LABEL[t]}</span>
          </button>))}
      </div>
      <div role="tabpanel" style={{ padding: "16px 24px 32px", flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
        {notice && <div role="status" style={{ fontSize: 12.5, color: WARN }}>{notice}</div>}
        <ErrorLine error={act.error ? (/managed by run/.test(act.error)
          ? `${act.error}. Close that run on the Execution tab, then start again.` : act.error) : all.error} />
        {tab === "overview" && <Overview ctx={ctx} preflight={stats.drafts > 0} />}
        {tab === "review" && <ReviewTab ctx={ctx} />}
        {tab === "execution" && <ExecutionTab ctx={ctx} />}
      </div>
    </div>
  );
}
