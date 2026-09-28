import { useNavigate } from "react-router-dom";

import { ErrorLine, PageHead, relTime, StageStrip, stateColor } from "../components/ui";
import type { JobSummary } from "../lib/api";
import { useApi } from "../lib/hooks";

/** Where a job's primary action leads. */
export function jobTarget(j: JobSummary): string {
  if (j.state === "prompt_enhanced" || j.state === "created" || j.state === "failed_prompt") return `/new/${j.job_id}`;
  if (["prompt_confirmed", "candidates_generated", "qa_completed", "waiting_for_selection", "failed_generation"].includes(j.state))
    return `/review/${j.job_id}`;
  return j.current_attempt ? `/attempts/${j.job_id}` : `/review/${j.job_id}`;
}

export function Jobs() {
  const navigate = useNavigate();
  const jobs = useApi<JobSummary[]>("/api/jobs", 4000);
  const cols = "minmax(220px,1.4fr) 64px minmax(260px,1.6fr) minmax(150px,1fr)";

  return (
    <div className="page">
      <PageHead sub="GET /api/jobs · output/*/job_state.json" title="Jobs">
        <button className="btn btn-primary" onClick={() => navigate("/new")}>New job</button>
      </PageHead>
      <ErrorLine error={jobs.error} />
      <div className="panel">
        {(jobs.data ?? []).length === 0 && <div className="dim" style={{ padding: 16 }}>No jobs yet.</div>}
        {(jobs.data ?? []).map((j, i) => (
          <div key={j.job_id} className={`clickable ${i ? "tr" : ""}`} onClick={() => navigate(jobTarget(j))}
            style={{ display: "grid", gridTemplateColumns: cols, gap: 18, padding: "12px 16px", alignItems: "center" }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 500 }} className="ellipsis">{j.title}</div>
              <div className="sub ellipsis">{j.job_id} · {relTime(j.created_at)}</div>
            </div>
            <span className="mono" style={{ fontSize: 10.5, color: "var(--muted)", border: "1px solid var(--line-2)",
              borderRadius: 4, padding: "2px 6px", textAlign: "center" }}>3D</span>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StageStrip stage={j.stage} failed={j.failed} waiting={j.waiting} />
              <span className="mono" style={{ fontSize: 11, color: stateColor(j.state) }}>
                {j.state}{j.current_attempt ? ` · ${j.current_attempt}` : ""}{j.active_operation ? ` · ${j.active_operation.name}…` : ""}
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", minWidth: 0 }}>
              {j.waiting
                ? <span style={{ fontSize: 12, background: "var(--text)", color: "var(--bg)", borderRadius: 5,
                    padding: "3px 9px", fontWeight: 500 }}>{j.action}</span>
                : <span className="dim ellipsis" style={{ fontSize: 12, color: j.failed ? "var(--bad)" : undefined }}
                    title={j.action}>{j.action}</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
