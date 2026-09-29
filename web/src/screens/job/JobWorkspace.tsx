import { useEffect } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ErrorLine, Loading, WARN } from "../../components/ui";
import { J, type ItemView, type JobDetail, type JobSummary } from "../../lib/api";
import { type Loaded, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { BuildTab } from "./BuildTab";
import { DIM, liveItems, pickLabel, progressColor, tabFromSlug, TABS, type Tab, typing } from "./jobModel";
import { PromptTab } from "./PromptTab";
import { PublishTab } from "./PublishTab";
import "./job.css";

export interface TabProps { job: JobDetail; item: ItemView; reload: () => void; goTab: (t: Tab) => void }

export function useJob(jobId: string): Loaded<JobDetail> {
  const { id } = useProject();
  return useApi<JobDetail>(`${J(id)}/${jobId}`, { project: id, job: jobId, pollMs: 15000 });
}

/** The state line under each tab title. */
function tabStates(job: JobDetail, item: ItemView): [string, string, string] {
  const rounds = item.rounds.filter((r) => r.candidate_set_id).length;
  const pick = pickLabel(item, job.direct, job.kind);
  const attempts = item.build_history.length;
  const acceptedAt = item.build_history.findIndex((h) => h.accepted);
  const prompt = job.direct ? "direct transform · no prompt"
    : rounds ? `${rounds} round${rounds > 1 ? "s" : ""} · ${item.approved ? `approved ${pick}` : "none approved"}`
      : "not run yet";
  const build = attempts ? `${attempts} attempt${attempts > 1 ? "s" : ""}${acceptedAt >= 0 ? ` · accepted #${acceptedAt + 1}` : ""}`
    : job.direct ? (item.legal.run_transform ? "ready to transform" : "—") : item.approval ? "ready to build" : "—";
  const publish = item.published ? "published" : item.accepted_build ? "ready" : "—";
  return [prompt, build, publish];
}

function JobStepper({ pos, total, step }: { pos: number; total: number; step: (d: number) => void }) {
  return (
    <div className="jw-stepper" role="group" aria-label="Job stepper">
      <button title="Previous Job ( [ )" aria-label="Previous Job" onClick={() => step(-1)}>‹</button>
      <span className="sub" aria-live="polite">{pos + 1} / {total}</span>
      <button title="Next Job ( ] )" aria-label="Next Job" onClick={() => step(1)}>›</button>
    </div>
  );
}

/** [ and ] step through the Jobs list, except while typing or with a dialog open. */
function useStepKeys(step: (d: number) => void): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e) || e.metaKey || e.ctrlKey || e.altKey || document.querySelector("[role=dialog]")) return;
      if (e.key === "[") step(-1);
      else if (e.key === "]") step(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
}

export function JobWorkspace() {
  const { id } = useProject();
  const { jobId = "", tab } = useParams();
  const loc = useLocation();
  const [sp] = useSearchParams();
  const b = useJob(jobId);
  const nav = useNavigate();
  const jobs = useApi<{ jobs: JobSummary[] }>(J(id), { project: id, pollMs: 20000 });
  const list = jobs.data?.jobs ?? [];
  const pos = list.findIndex((j) => j.id === jobId);
  const step = (d: number) => {
    if (pos >= 0 && list.length > 1) nav(`/p/${id}/jobs/${list[(pos + d + list.length) % list.length]!.id}`);
  };
  useStepKeys(step);
  if (!b.data) return b.error ? <div className="content"><ErrorLine error={b.error} /></div> : <Loading what="job" />;
  const job = b.data;
  const active = tabFromSlug(tab);
  if (!active || tab !== active) {
    const slug = active ?? TABS[job.progress.tab] ?? "prompt";
    return <Navigate to={`/p/${id}/jobs/${jobId}/${slug}${loc.search}`} replace />;
  }
  const items = liveItems(job);
  const item = items.find((i) => i.id === sp.get("item")) ?? items[0];
  if (!item) return <div className="content"><div className="empty">This Job has no items.</div></div>;
  const multi = items.length > 1;
  const qs = multi ? `?item=${item.id}` : "";
  const hrefFor = (t: Tab) => `/p/${id}/jobs/${jobId}/${t}${qs}`;
  const states = tabStates(job, item);
  const labels = ["Prompt & candidates", job.build_label, "Publish"];
  const p = job.progress;
  const chips: { t: string; to?: string }[] = [
    job.batch ? { t: `Batch ${job.batch.id} · ${job.batch.title}`, to: `/p/${id}/batches/${job.batch.id}` } : { t: "Not in a Batch" },
    ...(job.family ? [{ t: `family · ${job.family.name}` }] : []),
    ...(job.style ? [{ t: `project style ${job.style.name}`, to: `/p/${id}/style` }] : []),
    { t: `seed family ${job.seed_family.toString(16).slice(0, 6)}` },
  ];
  const props: TabProps = { job, item, reload: b.reload,
    goTab: (t) => nav(hrefFor(t)) };
  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100%" }}>
      <div style={{ padding: "16px 24px 0", display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <Link to={`/p/${id}/jobs`} className="sub" style={{ textDecoration: "none" }}>
            ← Jobs / {job.alias} · {job.kind_label} · {job.category_label ?? "no category"}</Link>
          <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 2, flexWrap: "wrap" }}>
            <h1 className="h1" style={{ margin: 0 }}>{job.title}</h1>
            <span className="sub" style={{ color: progressColor(p.state), fontSize: 11.5 }}>● {p.label}</span>
          </div>
          <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            {chips.map((c) => c.to ? <Link key={c.t} className="tag" to={c.to} style={{ color: "var(--text-2)" }}>{c.t}</Link>
              : <span key={c.t} className="tag">{c.t}</span>)}
          </div>
        </div>
        {pos >= 0 && list.length > 1 && <JobStepper pos={pos} total={list.length} step={step} />}
      </div>
      {job.legacy_recipe && <div className="banner note" style={{ margin: "10px 24px 0" }}>Recorded with {job.legacy_recipe}.
        Its history stays readable; building it needs a fork to the current recipe (the old snapshot is never rewritten).</div>}
      {multi && (
        <div className="row" style={{ margin: "12px 24px 0", gap: 6, flexWrap: "wrap" }} role="group" aria-label="items of this Job">
          <span className="label">Items · {items.length}</span>
          {items.map((i) => (
            <Link key={i.id} to={`/p/${id}/jobs/${jobId}/${active}?item=${i.id}`} className={`chip${i.id === item.id ? " on" : ""}`}
              aria-current={i.id === item.id ? "true" : undefined}>{i.name} <span className="dim">· {i.stage.state}</span></Link>
          ))}
        </div>
      )}
      <nav className="jw-tabs" style={{ margin: "14px 24px 0" }} aria-label="job stages">
        {TABS.map((t, i) => {
          const on = t === active;
          const waiting = p.tab === i && p.state === "wait";
          return (
            <Link key={t} id={`jw-tab-${t}`} to={hrefFor(t)} className={`jw-tab${on ? " on" : ""}`} aria-current={on ? "page" : undefined}>
              <span className="sub" style={{ fontSize: 10.5, color: waiting ? WARN : DIM }}>
                {i + 1} · {states[i]}{waiting ? " · waiting on you" : ""}</span>
              <span>{labels[i]}</span>
            </Link>
          );
        })}
      </nav>
      <div style={{ padding: "16px 24px 32px", flex: 1, minWidth: 0 }}>
        {active === "prompt" && <PromptTab key={item.id} {...props} />}
        {active === "build" && <BuildTab {...props} />}
        {active === "publish" && <PublishTab {...props} />}
      </div>
    </div>
  );
}
