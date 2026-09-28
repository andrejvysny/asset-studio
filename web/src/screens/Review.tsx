import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ErrorLine, Segmented, StatusPill, statusColor } from "../components/ui";
import { approve, fileUrl, type JobDetail, type JobSummary, type QaResult, type StudioConfig } from "../lib/api";
import { queueWorkflow } from "../lib/comfy";
import { useAction, useApi } from "../lib/hooks";

const REVIEW_STATES = ["prompt_confirmed", "candidates_generated", "qa_completed", "waiting_for_selection"];
const APPROVABLE = ["waiting_for_selection", "candidate_selected", "completed", "failed_cutout", "failed_trellis", "failed_postprocess"];
type View = "grid" | "focus" | "matrix";

/** Legacy QA files lack `coverage`; derive it from the checks actually recorded. */
function cov(qa: QaResult | undefined, total: number): string {
  if (!qa) return `0/${total}`;
  return qa.coverage ? `${qa.coverage.ran}/${qa.coverage.total}` : `${Object.keys(qa.checks ?? {}).length}/${total}`;
}

function checkCell(qa: QaResult | undefined, id: string): { res: string; color: string } {
  if (!qa || !(id in qa.checks)) return { res: "n/a", color: "var(--dim)" };
  return qa.checks[id] ? { res: "pass", color: "var(--ok)" } : { res: "fail", color: "var(--bad)" };
}

function Img({ job, idx, border, label }: { job: string; idx: string; border: string; label?: boolean }) {
  return (
    <div style={{ position: "relative", border: `1.5px solid ${border}`, borderRadius: 8, overflow: "hidden" }} className="stripes">
      <img className="thumb" alt={`candidate ${idx}`} src={fileUrl(job, `candidates/${idx}.png`)} />
      {label !== false && <span className="mono" style={{ position: "absolute", top: 8, left: 8, fontWeight: 600, fontSize: 12,
        background: "#111213cc", padding: "2px 7px", borderRadius: 4 }}>#{idx}</span>}
    </div>
  );
}

function Reasons({ qa }: { qa?: QaResult }) {
  if (!qa) return <div className="dim" style={{ fontSize: 12 }}>QA not run yet.</div>;
  const lines = [...qa.reasons, ...(qa.warnings ?? [])];
  if (!lines.length) return <div style={{ fontSize: 12, color: "var(--text-2)" }}>{qa.coverage ? `All ${qa.coverage.total} configured checks ran and passed.` : "No issues reported (legacy QA, coverage unknown)."}</div>;
  return <>{lines.map((r) => <div key={r} style={{ fontSize: 12, color: "var(--text-2)" }}>{r}</div>)}</>;
}

export function Review() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const jobs = useApi<JobSummary[]>("/api/jobs", 4000);
  const tabs = (jobs.data ?? []).filter((j) => REVIEW_STATES.includes(j.state));
  const current = jobId ?? tabs.find((j) => j.state === "waiting_for_selection")?.job_id ?? tabs[0]?.job_id;
  const job = useApi<JobDetail>(current ? `/api/jobs/${current}` : null, 3000);
  const cfg = useApi<StudioConfig>("/api/config");
  const [view, setView] = useState<View>("focus");
  const [focus, setFocus] = useState(0);
  const [override, setOverride] = useState(false);
  const action = useAction();
  useEffect(() => setFocus(0), [current]);
  useEffect(() => setOverride(false), [current, focus]);  // override is per candidate, never sticky

  const j = job.data;
  const set = j?.candidate_set;
  // Legacy jobs (before frozen candidate sets) are shown read-only: approval must bind to a frozen set.
  const idxs = set ? Object.keys(set.images).sort() : (j?.qa && Object.keys(j.qa).length ? j.candidates : []);
  const legacy = !!j && !set && idxs.length > 0;
  const fc = idxs[focus];
  const fqa = fc ? j?.qa[fc] : undefined;
  const checks = cfg.data?.qa_checks ?? [];
  // Until QA has finished (or while any operation runs) nothing is approvable and no override is offered.
  const pending = !!j && (!APPROVABLE.includes(j.state) || !!j.active_operation);
  const needsOverride = !pending && fqa?.status !== "recommended";
  const allFailed = idxs.length > 0 && idxs.every((i) => j?.qa[i]?.status && j.qa[i]?.status !== "recommended");
  const canApprove = !!j && !!set && !!fc && !pending && (!needsOverride || override);

  const doApprove = () => action.run(async () => {
    if (!j || !set || !fc) return;
    const { attempt } = await approve(j.job_id, set.set_id, Number(fc), set.images[fc]!, needsOverride && override);
    if (attempt.state === "approved") {
      await queueWorkflow("line_a_3d", { cutout: { job_id: j.job_id, attempt_id: attempt.id } });
    }
    navigate(`/attempts/${j.job_id}`);
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100%" }}>
      <div style={{ padding: "16px 24px 0", display: "flex", gap: 6, flexWrap: "wrap", borderBottom: "1px solid var(--line)" }}>
        {tabs.length === 0 && !jobId && <div className="dim" style={{ padding: "8px 0 12px" }}>Nothing to review.</div>}
        {tabs.map((t) => (
          <div key={t.job_id} className="clickable" onClick={() => navigate(`/review/${t.job_id}`)} style={{ padding: "8px 12px",
            borderBottom: `2px solid ${t.job_id === current ? "var(--text)" : "transparent"}`,
            color: t.job_id === current ? "#fff" : "var(--muted)", display: "flex", gap: 8, alignItems: "baseline" }}>
            <span>{t.title}</span><span className="sub">{t.state === "waiting_for_selection" ? "3D" : t.state}</span>
          </div>
        ))}
      </div>
      <ErrorLine error={job.error} />
      {j && <>
        <div style={{ padding: "16px 24px 0", display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap" }}>
          <div className="grow" style={{ minWidth: 280 }}>
            <div className="sub">{j.job_id}</div>
            <div className="h1">{j.enhancement?.short_title || j.request.prompt}</div>
            <div className="sub" style={{ color: "var(--muted)", marginTop: 4 }}>
              candidate set {set?.set_id ?? "—"} · {idxs.length} candidates · seed family {j.request.seed_family} ·
              Qwen-Image-2512 · {String((j.manifest.generation as { speed_preset?: string } | undefined)?.speed_preset ?? "")}</div>
          </div>
          <Segmented value={view} onChange={setView} options={[{ id: "2a", key: "grid", label: "Grid" },
            { id: "2b", key: "focus", label: "Focus" }, { id: "2c", key: "matrix", label: "Check matrix" }]} />
        </div>

        <div style={{ padding: "16px 24px 24px", flex: 1 }}>
          {allFailed && set && <div className="banner bad" style={{ marginBottom: 14 }}>
            <span className="dot" style={{ background: "var(--bad)", marginTop: 6 }} /><span>No candidate passed QA. QA is advisory:
            pick the best one and approve it with <b>Override QA</b>, or start a new job with a revised brief.</span></div>}
          {legacy && <div className="banner note" style={{ marginBottom: 14 }}>Legacy job: generated before candidate sets were
            frozen with sha256, so it can't be approved here. Start a new job to produce an approvable set.</div>}
          {!set && !legacy && <div className="dim">{j.active_operation ? `${j.active_operation.name} running…` : j.state}
            {" "}— {j.summary.candidates}/{j.request.candidate_count} images so far</div>}
          {idxs.length > 0 && view === "grid" && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(250px,1fr))", gap: 14 }}>
              {idxs.map((idx, i) => (
                <div key={idx} className="clickable" onClick={() => setFocus(i)} style={{ border: `1.5px solid ${i === focus ? "var(--text)" : "var(--line)"}`,
                  borderRadius: 9, overflow: "hidden", background: "var(--card)" }}>
                  <Img job={j.job_id} idx={idx} border="transparent" />
                  <div style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6 }}>
                    <div className="row sub" style={{ justifyContent: "space-between" }}>
                      <StatusPill status={j.qa[idx]?.status} />
                      <span>seed {j.request.seed_family + i} · coverage {cov(j.qa[idx], checks.length)}</span>
                    </div>
                    <Reasons qa={j.qa[idx]} />
                  </div>
                </div>
              ))}
            </div>
          )}
          {idxs.length > 0 && view === "focus" && fc && (
            <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 320px", gap: 18 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ width: "min(100%, 62vh)", alignSelf: "center" }}><Img job={j.job_id} idx={fc} border={statusColor(fqa?.status)} /></div>
                <div style={{ display: "grid", gridTemplateColumns: `repeat(${idxs.length},minmax(0,120px))`, gap: 8, justifyContent: "center" }}>
                  {idxs.map((idx, i) => (
                    <div key={idx} className="clickable" onClick={() => setFocus(i)} style={{ position: "relative" }}>
                      <Img job={j.job_id} idx={idx} border={i === focus ? "var(--text)" : "transparent"} />
                      <span className="dot" style={{ position: "absolute", left: 8, bottom: 8, width: 8, height: 8, background: statusColor(j.qa[idx]?.status) }} />
                    </div>
                  ))}
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div className="row"><StatusPill status={fqa?.status} />
                  <span className="sub">coverage {cov(fqa, checks.length)}</span></div>
                <Reasons qa={fqa} />
                <div className="panel" style={{ marginTop: 4 }}>
                  {checks.map((c) => {
                    const cell = checkCell(fqa, c.id);
                    return (
                      <div key={c.id} className="row tr" style={{ padding: "6px 10px", gap: 8, fontSize: 12 }} title={c.question}>
                        <span className="dot" style={{ background: cell.color }} />
                        <span className="mono grow" style={{ fontSize: 11.5 }}>{c.id}</span>
                        <span className="sub" style={{ fontSize: 10 }}>{c.severity}</span>
                        <span className="mono" style={{ fontSize: 11, color: cell.color, width: 30, textAlign: "right" }}>{cell.res}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
          {idxs.length > 0 && view === "matrix" && (
            <div className="panel" style={{ overflow: "auto" }}>
              <div style={{ display: "grid", gridTemplateColumns: `minmax(260px,1.6fr) 60px repeat(${idxs.length},minmax(120px,1fr))`, minWidth: 820 }}>
                <div className="panel-head" /><div className="panel-head" />
                {idxs.map((idx, i) => (
                  <div key={idx} className="clickable panel-head" onClick={() => { setFocus(i); setView("focus"); }}
                    style={{ borderLeft: "1px solid #222326", display: "flex", flexDirection: "column", gap: 8 }}>
                    <Img job={j.job_id} idx={idx} border={i === focus ? "var(--text)" : "transparent"} />
                    <StatusPill status={j.qa[idx]?.status} />
                  </div>
                ))}
                {checks.map((c) => [
                  <div key={`${c.id}-q`} className="tr" style={{ padding: "7px 12px", display: "flex", flexDirection: "column" }}>
                    <span className="mono" style={{ fontSize: 11.5 }}>{c.id}</span><span className="dim" style={{ fontSize: 11.5 }}>{c.question}</span></div>,
                  <div key={`${c.id}-s`} className="tr sub" style={{ padding: "7px 4px" }}>{c.severity}</div>,
                  ...idxs.map((idx) => {
                    const cell = checkCell(j.qa[idx], c.id);
                    return <div key={`${c.id}-${idx}`} className="tr mono" style={{ borderLeft: "1px solid #222326", padding: "7px 10px",
                      fontSize: 11, color: cell.color, background: cell.res === "fail" ? "var(--bad-bg)" : "transparent" }}>{cell.res}</div>;
                  }),
                ])}
              </div>
            </div>
          )}
        </div>

        {set && fc && (
          <div style={{ position: "sticky", bottom: 0, borderTop: "1px solid var(--line)", background: "var(--panel)",
            padding: "12px 24px", display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
            <div className="grow" style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 300 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                {fqa?.status === "recommended" ? `#${fc} passes all configured checks.` : `#${fc} is ${fqa?.status?.replace("_", " ") ?? "unchecked"}. Tick Override QA to approve it anyway (recorded in the manifest).`}
                {" "}Approving runs cut-out → TRELLIS.2 → GLB.{j.active_operation ? ` (${j.active_operation.name} running)` : ""}</span>
              <span className="sub">binds job {j.job_id.slice(-4)} · set {set.set_id} · sha256 {set.images[fc]?.slice(0, 10)}…</span>
              <ErrorLine error={action.error} />
            </div>
            {needsOverride && (
              <label className="row" style={{ gap: 6, fontSize: 12, color: "var(--warn)" }}>
                <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />Override QA</label>
            )}
            <button className="btn-link" onClick={() => navigate("/jobs")}>Keep reviewing later</button>
            <button className="btn btn-primary" disabled={!canApprove || action.busy} onClick={doApprove}>
              {action.busy ? "Approving…" : pending ? `${j.active_operation?.name ?? j.state.replace(/_/g, " ")} running…`
                : `Approve #${fc}${needsOverride ? " anyway" : ""}`}</button>
          </div>
        )}
      </>}
    </div>
  );
}
