import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { NextPill, ProgressPills } from "../components/progress";
import { Box, Empty, ErrorLine, Loading, PageHead } from "../components/ui";
import { type BatchGroup, J, type JobSummary, key, send, V2 } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { runJob } from "../lib/jobsApi";
import { useProject } from "../lib/project";

export const TAB_NAMES = ["prompts", "candidates", "approve", "build", "publish"] as const;

const MODES = [["stage", "Stage"], ["batch", "Batch"], ["family", "Family"], ["kind", "Kind"], ["none", "None"]] as const;
type Mode = (typeof MODES)[number][0];
const COLS = "34px minmax(200px,1.4fr) 84px 70px 40px minmax(250px,1.6fr) 170px";

const tabOf = (b: JobSummary): string => TAB_NAMES[Math.max(0, Math.min(TAB_NAMES.length - 1, b.progress.tab))] ?? "prompts";

function source(b: JobSummary): string {
  if (b.variant) return `variant of ${b.variant.source_name}`;
  return b.source.startsWith("shot list") ? "shot list" : "manual";
}

/** "order|label" keys: the order digit sorts groups, the label is shown. */
function groupOf(mode: Mode, b: JobSummary, batchLabel: (j: JobSummary) => string): string {
  const s = b.progress.state;
  if (mode === "stage") {
    return s === "wait" || s === "bad" ? "1|Waiting on you" : s === "run" ? "2|Running" : s === "draft" ? "3|Drafts · not run"
      : "4|Published";
  }
  if (mode === "batch") return b.batch ? `1|${batchLabel(b)}` : "2|Not in a Batch";
  if (mode === "family") return b.family ? `1|${b.family.name || b.family.id}` : "2|No family";
  if (mode === "kind") return `1|${b.kind_label}`;
  return "1|All";
}

/** Moves Jobs into `target`: removes them from every other Batch first (a Job then has exactly one Batch). */
async function moveIntoBatch(project: string, target: BatchGroup, all: BatchGroup[], jobIds: string[]): Promise<void> {
  for (const b of all) {
    if (b.id === target.id || !b.job_ids.some((j) => jobIds.includes(j))) continue;
    await send("PATCH", `${V2(project)}/batches/${b.id}`, { expected_revision: b.revision,
      job_ids: b.job_ids.filter((j) => !jobIds.includes(j)) });
  }
  await send("PATCH", `${V2(project)}/batches/${target.id}`, { expected_revision: target.revision,
    job_ids: [...target.job_ids, ...jobIds.filter((j) => !target.job_ids.includes(j))] });
}

interface RunResult { alias: string; ok: boolean; text: string }

export function Jobs() {
  const { id } = useProject();
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const list = useApi<{ jobs: JobSummary[] }>(J(id), { project: id, pollMs: 10000 });
  const batchList = useApi<{ batches: BatchGroup[] }>(`${V2(id)}/batches`, { project: id });
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [addTo, setAddTo] = useState(false);
  const [results, setResults] = useState<RunResult[]>([]);
  const act = useAction();
  const mode = (MODES.find(([k]) => k === sp.get("group"))?.[0] ?? "stage") as Mode;
  const jobs = useMemo(() => list.data?.jobs ?? [], [list.data]);
  const batches = batchList.data?.batches ?? [];
  const batchAlias = (bid: string) => batches.find((x) => x.id === bid)?.alias ?? bid;
  const selected = jobs.filter((j) => sel.has(j.id));
  const n = selected.length;

  const groups = useMemo(() => {
    const m = new Map<string, JobSummary[]>();
    const label = (j: JobSummary) => `${j.batch?.title ?? ""} · ${batchAlias(j.batch?.id ?? "")}`;
    for (const j of jobs) {
      const k = groupOf(mode, j, label);
      m.set(k, [...(m.get(k) ?? []), j]);
    }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, mode, batches]);

  const setMode = (m: Mode) => {
    const next = new URLSearchParams(sp);
    if (m === "stage") next.delete("group"); else next.set("group", m);
    setSp(next, { replace: true });
  };
  const toggle = (jid: string, on: boolean) => { const s = new Set(sel); if (on) s.add(jid); else s.delete(jid); setSel(s); };
  const clear = () => { setSel(new Set()); setAddTo(false); };
  const createBatch = () => void act.run(async () => {
    const out = await send<{ batch: { id: string } }>("POST", `${V2(id)}/batches`, {
      title: `Batch of ${n} Jobs`, job_ids: selected.map((j) => j.id), idempotency_key: key() });
    nav(`/p/${id}/batches/${out.batch.id}`);
  });
  const moveTo = (target: BatchGroup) => void act.run(async () => {
    await moveIntoBatch(id, target, batches, selected.map((j) => j.id));
    clear();
    list.reload();
    batchList.reload();
  });
  const runSelected = () => void act.run(async () => {
    const out: RunResult[] = [];
    for (const j of selected) {
      try {
        const r = await runJob(id, j.id);
        out.push({ alias: j.alias, ok: true, text: `started ${r.run_id}` });
      } catch (e) {
        out.push({ alias: j.alias, ok: false, text: (e as Error).message });
      }
    }
    setResults(out);
    clear();
    list.reload();
  });

  return (
    <div className="content" style={{ maxWidth: 1360 }}>
      <PageHead sub="One Job = one asset · prompt → candidates → approve → build → publish" title="Jobs">
        <Link className="btn" to={`/p/${id}/shots`} style={{ textDecoration: "none" }}>From shot list</Link>
        <Link className="btn btn-primary" to={`/p/${id}/jobs/new`} style={{ textDecoration: "none" }}>New Job</Link>
      </PageHead>
      <div className="row" style={{ flexWrap: "wrap", minHeight: 34 }}>
        <span className="dim" style={{ fontSize: 12 }}>Group by</span>
        <div className="seg" role="group" aria-label="group by">
          {MODES.map(([k, label]) => (
            <button key={k} className={mode === k ? "on" : ""} aria-pressed={mode === k} onClick={() => setMode(k)}>{label}</button>))}
        </div>
        <span className="grow" />
        {n > 0 && (
          <div className="row" role="toolbar" aria-label="selection"
            style={{ gap: 8, flexWrap: "wrap", padding: "4px 6px 4px 12px", border: "1px solid var(--line-3)",
              borderRadius: 7, background: "var(--card)" }}>
            <span style={{ fontSize: 12 }}>{n} selected</span>
            <button className="btn btn-primary" style={{ padding: "4px 11px", fontSize: 12 }} disabled={act.busy}
              onClick={createBatch}>Create Batch</button>
            <button className="btn" style={{ padding: "3px 10px", fontSize: 12 }} aria-expanded={addTo}
              onClick={() => setAddTo(!addTo)}>Add to Batch ▾</button>
            <button className="btn" style={{ padding: "3px 10px", fontSize: 12 }} disabled={act.busy}
              onClick={runSelected}>Run standalone</button>
            <button className="btn-link" style={{ fontSize: 12, padding: "0 6px" }} onClick={clear}>Clear</button>
          </div>)}
      </div>
      {addTo && n > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <span className="dim" style={{ fontSize: 12 }}>Move {n} into</span>
          {batches.length === 0 && <span className="sub">no Batches yet</span>}
          {batches.map((b) => (
            <button key={b.id} className="btn" style={{ padding: "3px 10px", fontSize: 12 }} disabled={act.busy}
              onClick={() => moveTo(b)}>{b.title} · {b.alias}</button>))}
        </div>)}
      {results.length > 0 && (
        <div className="panel" role="status" style={{ padding: "8px 12px" }}>
          <div className="row"><span className="label grow">Run standalone</span>
            <button className="btn-link" style={{ padding: 0 }} onClick={() => setResults([])}>dismiss</button></div>
          {results.map((r) => (
            <div key={r.alias} className="sub" style={{ color: r.ok ? "var(--ok)" : "var(--bad)" }}>
              {r.alias}: {r.ok ? r.text : `not started · ${r.text}`}</div>))}
        </div>)}
      <ErrorLine error={list.error ?? batchList.error ?? act.error} />
      {!list.data ? <Loading what="jobs" /> : jobs.length === 0 ? (
        <Empty>No Jobs yet. A Job is one asset — <Link to={`/p/${id}/jobs/new`}>create one</Link>.</Empty>
      ) : (
        <div className="table">
          <div className="th" style={{ gridTemplateColumns: COLS, minWidth: 1000 }}>
            <Box on={n > 0 && n === jobs.length} label="select all jobs"
              onChange={(v) => setSel(new Set(v ? jobs.map((j) => j.id) : []))} />
            <span>Job</span><span>Kind</span><span>Batch</span><span>Rnd</span><span>Progress</span>
            <span style={{ textAlign: "right" }}>Next</span>
          </div>
          {groups.map(([k, rows]) => (
            <div key={k} style={{ minWidth: 1000 }}>
              {mode !== "none" && (
                <div className="row" role="rowheader" style={{ gap: 8, alignItems: "baseline", padding: "8px 14px 6px",
                  borderTop: "1px solid var(--line)", background: "var(--panel)" }}>
                  <span style={{ fontSize: 12, fontWeight: 500 }}>{k.split("|")[1]}</span>
                  <span className="sub">{rows.length}</span>
                </div>)}
              {rows.map((b) => (
                <div key={b.id} className={`td clickable${sel.has(b.id) ? " sel" : ""}`} style={{ gridTemplateColumns: COLS }}
                  onClick={() => nav(`/p/${id}/jobs/${b.id}/${tabOf(b)}`)}>
                  <Box on={sel.has(b.id)} label={`select ${b.title}`} onChange={(v) => toggle(b.id, v)} />
                  <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
                    <Link to={`/p/${id}/jobs/${b.id}`} className="ellipsis" onClick={(e) => e.stopPropagation()}
                      style={{ fontWeight: 500, textDecoration: "none", color: "inherit" }}>{b.title}</Link>
                    <span className="sub ellipsis" style={{ fontSize: 10.5 }}>
                      {b.alias} · {source(b)}{b.counts.items > 1 ? ` · ${b.counts.items} items` : ""}
                      {b.legacy ? " · legacy" : ""}</span>
                  </div>
                  <span className="mono muted" style={{ fontSize: 11 }}>{b.kind_label}</span>
                  {b.batch ? (
                    <Link to={`/p/${id}/batches/${b.batch.id}`} className="mono ellipsis" title={b.batch.title}
                      onClick={(e) => e.stopPropagation()} style={{ fontSize: 11 }}>{batchAlias(b.batch.id)}</Link>
                  ) : <span className="mono" style={{ fontSize: 11, color: "var(--faint)" }}>—</span>}
                  <span className="mono muted" style={{ fontSize: 11 }}>{b.rounds ? `R${b.rounds}` : "—"}</span>
                  <ProgressPills job={b} />
                  <div style={{ display: "flex", justifyContent: "flex-end" }} onClick={(e) => e.stopPropagation()}>
                    <NextPill progress={b.progress} onClick={() => nav(`/p/${id}/jobs/${b.id}/${tabOf(b)}`)} />
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      <div className="dim" style={{ fontSize: 12, maxWidth: 820 }}>
        Saving a Job never starts it. Tick several Jobs and create a Batch to run each stage across all of them while its model
        stays loaded. You can also run a single Job on its own.</div>
    </div>
  );
}
