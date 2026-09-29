import type { ReactNode } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { ErrorLine, Loading } from "../../components/ui";
import { J, type JobDetail, key, send } from "../../lib/api";
import { type Loaded, useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { TAB_NAMES } from "../Jobs";
import { ApproveTab } from "./ApproveTab";
import { BuildTab } from "./BuildTab";
import { CandidatesTab } from "./CandidatesTab";
import { PromptsTab } from "./PromptsTab";
import { PublishTab } from "./PublishTab";

export interface TabProps { job: JobDetail; reload: () => void }

export function ActionBar({ note, sub, children }: { note: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="actionbar">
      <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1, minWidth: 280 }}>
        <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>{note}</span>
        {sub && <span className="sub">{sub}</span>}
      </div>
      {children}
    </div>
  );
}

function tabState(b: JobDetail, t: (typeof TAB_NAMES)[number]): string {
  const c = b.counts;
  switch (t) {
    case "prompts": return `${c.confirmed}/${c.items} confirmed`;
    case "candidates": return `${c.candidates}/${c.items} ready`;
    case "approve": return `${c.approved}/${c.items} approved`;
    case "build": return b.recipe.build_available ? `${c.built}/${c.approved} built` : "unavailable";
    case "publish": return `${c.published}/${c.accepted} published`;
  }
}

export function useJob(jobId: string): Loaded<JobDetail> {
  const { id } = useProject();
  return useApi<JobDetail>(`${J(id)}/${jobId}`, { project: id, job: jobId, pollMs: 15000 });
}

function RunJob({ jobId, n, onDone }: { jobId: string; n: number; onDone: () => void }) {
  const { id } = useProject();
  const act = useAction();
  return <>
    <button className="btn btn-primary" disabled={act.busy} title="Standalone run: same planner and scheduler as a Batch"
      onClick={() => void act.run(async () => { await send("POST", `${J(id)}/${jobId}:run`, { idempotency_key: key() }); onDone(); })}>
      Run Job (enhance {n})</button>
    <ErrorLine error={act.error} /></>;
}

export function JobWorkspace() {
  const { id } = useProject();
  const { jobId = "", tab } = useParams();
  const b = useJob(jobId);
  if (!b.data) return b.error ? <div className="content"><ErrorLine error={b.error} /></div> : <Loading what="job" />;
  const job = b.data;
  if (!tab || !(TAB_NAMES as readonly string[]).includes(tab)) {
    const t = job.current_tab === "done" ? "publish" : job.current_tab;
    return <Navigate to={`/p/${id}/jobs/${jobId}/${t}`} replace />;
  }
  const props: TabProps = { job, reload: b.reload };
  const briefOnly = job.items.filter((i) => i.current_prompt === null && i.legal.enhance).length;
  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100%" }}>
      <div style={{ padding: "16px 24px 0", display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <Link to={`/p/${id}/jobs`} className="sub" style={{ textDecoration: "none" }}>
            ← Jobs / {job.alias} · {job.kind_label} · {job.category_label ?? "no category"}</Link>
          <h1 className="h1" style={{ margin: "2px 0 0" }}>{job.title}</h1>
        </div>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <span className="tag">{job.source}</span>
          <span className="tag" title="seed family (reproducible)">seed {job.seed_family}</span>
          <span className="tag" title="configuration captured at creation">config r{job.config_revision}</span>
          {job.active_run ? <Link className="tag" to={`/p/${id}/runs/${job.active_run}`}>in run {job.active_run.slice(-6)} →</Link>
            : briefOnly > 0 && <RunJob jobId={job.id} n={briefOnly} onDone={b.reload} />}
        </div>
      </div>
      {job.legacy_recipe && <div className="banner note" style={{ margin: "10px 24px 0" }}>Recorded with {job.legacy_recipe}.
        Its history stays readable; building it needs a fork to the current recipe (the old snapshot is never rewritten).</div>}
      <nav className="tabs" style={{ margin: "14px 24px 0" }} aria-label="job stages">
        {TAB_NAMES.map((t, i) => (
          <Link key={t} to={`/p/${id}/jobs/${jobId}/${t}`} className={`tab${tab === t ? " on" : ""}`}
            aria-current={tab === t ? "page" : undefined}>
            <span className="sub" style={{ fontSize: 10.5 }}>0{i + 1} · {tabState(job, t)}</span>
            <span>{t === "build" ? job.recipe.build_label : t[0]!.toUpperCase() + t.slice(1)}</span>
          </Link>
        ))}
      </nav>
      <div style={{ padding: "16px 24px 24px", flex: 1, minWidth: 0 }}>
        {tab === "prompts" && <PromptsTab {...props} />}
        {tab === "candidates" && <CandidatesTab {...props} />}
        {tab === "approve" && <ApproveTab {...props} />}
        {tab === "build" && <BuildTab {...props} />}
        {tab === "publish" && <PublishTab {...props} />}
      </div>
    </div>
  );
}
