import { type ReactNode, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { BAD, Box, ErrorLine, INFO, Loading, OK, PageHead, QaPill, relTime, taskColor, WARN } from "../../components/ui";
import { artifactUrl, type ItemView, key, type ModelPass, type RunDetail, send, V2 } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { runPill } from "./batchUi";

type Row = ItemView & { job_title: string; job_id: string };


function Section({ title, sub, children }: { title: string; sub?: string; children: ReactNode }) {
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="label">{title}</span>{sub && <span className="sub">{sub}</span>}</div>
      {children}
    </section>
  );
}

function useSelection(): [Set<string>, (id: string, on: boolean) => void, (ids: string[]) => void] {
  const [sel, setSel] = useState<Set<string>>(new Set());
  const toggle = (id: string, on: boolean) => { const n = new Set(sel); if (on) n.add(id); else n.delete(id); setSel(n); };
  return [sel, toggle, (ids) => setSel(new Set(ids))];
}

function jobsOf(rows: Row[]): number {
  return new Set(rows.map((r) => r.job_id)).size;
}

function PromptWave({ rows, base, reload, project }: { rows: Row[]; base: string; reload: () => void; project: string }) {
  const ready = rows.filter((r) => r.legal.confirm && r.prompt);
  const [sel, toggle, setAll] = useSelection();
  const act = useAction();
  const chosen = ready.filter((r) => sel.has(r.id));
  if (!ready.length) return null;
  return (
    <Section title="Prompts awaiting confirmation" sub="Confirming binds the exact prompt revision shown; edit prompts in their Job">
      <div className="table">
        {ready.map((r) => (
          <div key={r.id} className="td" style={{ gridTemplateColumns: "30px 170px minmax(0,1fr)", alignItems: "start" }}>
            <Box on={sel.has(r.id)} label={`confirm ${r.name}`} onChange={(v) => toggle(r.id, v)} />
            <span><span style={{ fontWeight: 500 }}>{r.name}</span><br /><Link className="sub" to={`/p/${project}/jobs/${r.job_id}/prompts`}>{r.job_title}</Link></span>
            <span style={{ fontSize: 12.5 }}>{r.prompt?.description} <span className="sub">rev {r.prompt?.number}</span></span>
          </div>))}
      </div>
      <div className="row">
        <button className="btn-link" onClick={() => setAll(sel.size === ready.length ? [] : ready.map((r) => r.id))}>
          {sel.size === ready.length ? "Select none" : "Select all"}</button>
        <button className="btn btn-primary" disabled={act.busy || !chosen.length} onClick={() => void act.run(async () => {
          const res = await send<{ results: { ok: boolean; message?: string }[] }>("POST", `${base}:confirm-prompts`, {
            idempotency_key: key(), items: chosen.map((r) => ({ job_id: r.job_id, item_id: r.id,
              prompt_revision_id: r.current_prompt, expected_item_revision: r.revision })) });
          setAll([]); reload();
          const bad = res.results.filter((x) => !x.ok);
          if (bad.length) throw new Error(bad.map((x) => x.message).join("; "));
        })}>Confirm {chosen.length} prompts + generate ({jobsOf(chosen)} Jobs)</button>
        <span className="sub">Unselected rows stay unconfirmed and can join a later wave.</span>
      </div>
      <ErrorLine error={act.error} />
    </Section>
  );
}

function CandidateWave({ rows, base, reload, project }: { rows: Row[]; base: string; reload: () => void; project: string }) {
  const open = rows.filter((r) => r.legal.approve && r.candidate_set && !r.approval);
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [override, setOverride] = useState(false);
  const act = useAction();
  if (!open.length) return null;
  const picked = open.filter((r) => choice[r.id]);
  const needsOverride = picked.some((r) => r.candidate_set!.candidates.find((c) => c.id === choice[r.id])?.qa?.status !== "recommended");
  return (
    <Section title="Candidates awaiting approval" sub="Click the candidate to approve for each item; nothing is approved until you confirm">
      {open.map((r) => (
        <div key={r.id} className="panel" style={{ padding: "8px 10px", display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ width: 170 }}><span style={{ fontWeight: 500 }}>{r.name}</span><br /><span className="sub">{r.job_title}</span></span>
          {r.candidate_set!.candidates.map((c) => (
            <button key={c.id} aria-pressed={choice[r.id] === c.id} onClick={() => setChoice({ ...choice, [r.id]: c.id })}
              style={{ border: `2px solid ${choice[r.id] === c.id ? "var(--text)" : "transparent"}`, borderRadius: 6, padding: 0, background: "none" }}>
              <img src={artifactUrl(project, c.artifact_id)} alt={`${r.name} candidate ${c.index + 1}`}
                style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 4, display: "block" }} />
              <QaPill status={c.qa?.status} />
            </button>))}
        </div>))}
      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className="btn" disabled={act.busy} onClick={() => void act.run(async () => {
          const res = await send<{ proposals: { item_id: string; candidate_id: string }[] }>("POST", `${base}:preview-best`, {});
          const next = { ...choice };
          res.proposals.forEach((p) => { if (open.some((r) => r.id === p.item_id)) next[p.item_id] = p.candidate_id; });
          setChoice(next);
        })}>Propose best recommended (preview)</button>
        <label className="row" style={{ gap: 6, fontSize: 12 }}><Box on={override} label="approve non-recommended" onChange={setOverride} />
          approve non-recommended picks as an explicit QA override</label>
        <button className="btn btn-primary" disabled={act.busy || !picked.length || (needsOverride && !override)}
          onClick={() => void act.run(async () => {
            const res = await send<{ results: { ok: boolean; message?: string }[] }>("POST", `${base}:approve-candidates`, {
              idempotency_key: key(), items: picked.map((r) => {
                const c = r.candidate_set!.candidates.find((x) => x.id === choice[r.id])!;
                return { job_id: r.job_id, item_id: r.id, expected_item_revision: r.revision, candidate_set_id: r.candidate_set!.id,
                  candidate_id: c.id, image_sha256: c.sha256, prompt_revision_id: r.candidate_set!.prompt_revision_id,
                  qa_evaluation_id: c.qa?.id ?? null, override_qa: c.qa?.status !== "recommended" };
              }) });
            setChoice({}); reload();
            const bad = res.results.filter((x) => !x.ok);
            if (bad.length) throw new Error(bad.map((x) => x.message).join("; "));
          })}>Approve {picked.length} chosen ({jobsOf(picked)} Jobs)</button>
      </div>
      <ErrorLine error={act.error} />
    </Section>
  );
}

function BuildWave({ rows, base, reload }: { rows: Row[]; base: string; reload: () => void }) {
  const ready = rows.filter((r) => r.legal.build);
  const valid = rows.filter((r) => r.legal.accept && r.build);
  const [sel, toggle, setAll] = useSelection();
  const [acc, toggleAcc, setAcc] = useSelection();
  const act = useAction();
  if (!ready.length && !valid.length) return null;
  const chosen = ready.filter((r) => sel.has(r.id));
  const accepting = valid.filter((r) => acc.has(r.id));
  return (
    <Section title="Builds" sub="Build only exact approved candidates; accept only structurally valid results">
      {ready.length > 0 && <div className="table">
        {ready.map((r) => <div key={r.id} className="td" style={{ gridTemplateColumns: "30px minmax(0,1fr) 160px" }}>
          <Box on={sel.has(r.id)} label={`build ${r.name}`} onChange={(v) => toggle(r.id, v)} />
          <span>{r.name} <span className="sub">{r.job_title}</span></span><span className="sub">approved</span></div>)}
      </div>}
      {ready.length > 0 && <div className="row">
        <button className="btn-link" onClick={() => setAll(sel.size === ready.length ? [] : ready.map((r) => r.id))}>
          {sel.size === ready.length ? "Select none" : "Select all"}</button>
        <button className="btn btn-primary" disabled={act.busy || !chosen.length} onClick={() => void act.run(async () => {
          const res = await send<{ results: { ok: boolean; message?: string }[] }>("POST", `${base}:build-approved`, {
            idempotency_key: key(), items: chosen.map((r) => ({ job_id: r.job_id, item_id: r.id, approval_id: r.approval,
              expected_item_revision: r.revision })) });
          setAll([]); reload();
          const bad = res.results.filter((x) => !x.ok);
          if (bad.length) throw new Error(bad.map((x) => x.message).join("; "));
        })}>Build {chosen.length} approved ({jobsOf(chosen)} Jobs)</button></div>}
      {valid.length > 0 && <div className="table">
        {valid.map((r) => <div key={r.id} className="td" style={{ gridTemplateColumns: "30px minmax(0,1fr) 160px" }}>
          <Box on={acc.has(r.id)} label={`accept ${r.name}`} onChange={(v) => toggleAcc(r.id, v)} />
          <span>{r.name} <span className="sub">{r.job_title}</span></span>
          <span className="sub" style={{ color: OK }}>valid{r.build?.preview === "failed" ? " · preview failed" : ""}</span></div>)}
      </div>}
      {valid.length > 0 && <button className="btn btn-primary" disabled={act.busy || !accepting.length} style={{ alignSelf: "flex-start" }}
        onClick={() => void act.run(async () => {
          await send("POST", `${base}:accept-builds`, { idempotency_key: key(), items: accepting.map((r) => ({
            job_id: r.job_id, item_id: r.id, build_run_id: r.build!.id, expected_item_revision: r.revision, accept: true })) });
          setAcc([]); reload();
        })}>Accept {accepting.length} valid results</button>}
      <ErrorLine error={act.error} />
    </Section>
  );
}

interface PubRow { job_id: string; item_id: string; name: string; build_run_id: string; expected_item_revision: number;
  published: boolean; name_id: string; new_asset: boolean; current_version_id: string | null }

function PublishWave({ base, project, reload }: { base: string; project: string; reload: () => void }) {
  const prev = useApi<{ items: PubRow[] }>(`${base}/publish-preview`, { project });
  const act = useAction();
  const todo = (prev.data?.items ?? []).filter((r) => !r.published);
  if (!todo.length) return null;
  return (
    <Section title="Accepted results ready to publish" sub="Publishing commits immutable versions to this project's library">
      <div className="table">{todo.map((r) => <div key={r.item_id} className="td" style={{ gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)" }}>
        <span>{r.name}</span><span className="mono" style={{ fontSize: 11.5 }}>{r.new_asset ? "new asset " : ""}{r.name_id}</span></div>)}</div>
      <button className="btn btn-primary" disabled={act.busy} style={{ alignSelf: "flex-start" }} onClick={() => void act.run(async () => {
        await send("POST", `${base}:publish`, { idempotency_key: key(), items: todo.map((r) => ({ job_id: r.job_id,
          item_id: r.item_id, build_run_id: r.build_run_id, expected_item_revision: r.expected_item_revision, make_current: true,
          expected_current_version: r.current_version_id })) });
        prev.reload(); reload();
      })}>Publish {todo.length} accepted</button>
      <ErrorLine error={act.error ?? prev.error} />
    </Section>
  );
}

function Passes({ passes }: { passes: ModelPass[] }) {
  if (!passes.length) return <span className="sub">No model passes yet.</span>;
  return (
    <div className="table">
      <div className="th" style={{ gridTemplateColumns: "60px minmax(220px,1.6fr) 70px 80px minmax(140px,1fr) 120px" }}>
        <span>Lane</span><span>Model group (residency)</span><span>Tasks</span><span>Jobs</span><span>Measured loads</span><span>Closed</span></div>
      {passes.map((p) => (
        <div key={p.id} className="td" style={{ gridTemplateColumns: "60px minmax(220px,1.6fr) 70px 80px minmax(140px,1fr) 120px" }}>
          <span className="mono" style={{ fontSize: 11.5 }}>{p.lane}</span>
          <span className="mono ellipsis" style={{ fontSize: 11 }} title={p.residency}>{p.residency}</span>
          <span>{p.task_ids.length}</span><span>{p.jobs.length}</span>
          <span className="sub" title="Measured worker model-load counters during the pass; not the number of resource grants">
            {p.measured.model_loads ? Object.entries(p.measured.model_loads).map(([k, v]) => `${k} +${v}`).join(" · ") : "unavailable"}</span>
          <span className="sub" style={{ color: p.close_reason?.startsWith("resource") ? BAD : INFO }}>{p.close_reason ?? "running"}</span>
        </div>))}
    </div>
  );
}

export function RunView() {
  const { id } = useProject();
  const { runId = "" } = useParams();
  const base = `${V2(id)}/runs/${runId}`;
  const r = useApi<RunDetail>(base, { project: id, pollMs: 5000 });
  const act = useAction();
  if (!r.data) return r.error ? <div className="content"><ErrorLine error={r.error} /></div> : <Loading what="run" />;
  const run = r.data;
  // Batch runs live in the Batch's Review tab; only standalone (Job) runs keep this minimal view.
  if (run.batch_id) return <Navigate to={`/p/${id}/batches/${run.batch_id}/review`} replace />;
  const rows: Row[] = run.jobs.flatMap((j) => j.items.map((i) => ({ ...i, job_title: j.title, job_id: j.id })));
  const c = run.counts;
  const paused = run.control === "paused" || c.paused_tasks > 0;
  const ended = run.control === "cancelled" || run.status === "closed";
  const control = (action: string) => void act.run(async () => { await send("POST", `${base}:${action}`); r.reload(); });
  return (
    <div className="content narrow" style={{ maxWidth: 1240, gap: 18 }}>
      {run.batch_id ? <Link to={`/p/${id}/batches/${run.batch_id}`} className="sub" style={{ textDecoration: "none" }}>← Batch</Link>
        : <Link to={`/p/${id}/jobs/${run.job_ids[0]}`} className="sub" style={{ textDecoration: "none" }}>← Job (standalone run)</Link>}
      <PageHead sub={`started ${relTime(run.created_at)} · frozen selection: ${c.jobs} Jobs, ${c.items} items`} title={<>Run {runId.slice(-8)} {runPill(run.status)}</>}>
        <button className="btn" disabled={act.busy || ended} onClick={() => control(paused ? "resume" : "pause")}>
          {paused ? "Resume" : "Pause"}</button>
        <button className="btn" disabled={act.busy || !c.active_tasks} onClick={() => control("cancel")}>Cancel run's work</button>
        <button className="btn" disabled={act.busy || c.active_tasks > 0 || run.status === "closed"} onClick={() => control("close")}
          title="Keeps undecided items in their Jobs for a later run">Close run</button>
      </PageHead>
      <div className="panel" style={{ padding: "10px 14px", display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(170px,1fr))", gap: 6, fontSize: 12.5 }}>
        <span>Enhancement: {c.enhanced} complete{c.enhance_failed ? `, ${c.enhance_failed} failed` : ""} / {c.items}</span>
        <span>Prompts: {c.prompts_confirmed} confirmed, {c.prompts_waiting} waiting</span>
        <span>Review: {c.approved} approved, {c.undecided} undecided</span>
        <span>Builds: {c.builds_valid} valid, {c.builds_invalid} invalid, {c.builds_failed} failed</span>
        <span>Accepted {c.accepted} · published {c.published}</span>
        <span style={{ color: c.failed_tasks ? WARN : undefined }}>Tasks: {c.active_tasks} active · {c.failed_tasks} failed</span>
      </div>
      <PromptWave rows={rows} base={base} reload={r.reload} project={id} />
      <CandidateWave rows={rows} base={base} reload={r.reload} project={id} />
      <BuildWave rows={rows} base={base} reload={r.reload} />
      <PublishWave base={base} project={id} reload={r.reload} />
      <Section title="Items" sub="Every selected item, its Job and current gate">
        <div className="table">{rows.map((it) => (
          <div key={it.id} className="td" style={{ gridTemplateColumns: "minmax(140px,1fr) minmax(140px,1fr) minmax(160px,1fr) minmax(200px,1.4fr)" }}>
            <Link to={`/p/${id}/jobs/${it.job_id}`}>{it.job_title}</Link><span>{it.name}</span>
            <span className="sub">{it.stage.stage} · {it.stage.state}</span>
            <span className="sub">{Object.entries(it.tasks).map(([f, t]) => <span key={f} style={{ color: taskColor(t.state), marginRight: 8 }}>
              {f} {t.state}{t.error ? ` (${t.error})` : ""}</span>)}</span>
          </div>))}</div>
      </Section>
      <Section title="Execution · model passes" sub="Compatible stage work of every Job shares one model residency per pass">
        <Passes passes={run.passes} />
      </Section>
      <ErrorLine error={act.error} />
    </div>
  );
}
