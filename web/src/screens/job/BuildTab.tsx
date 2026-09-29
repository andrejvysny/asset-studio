import { useState } from "react";

import { ErrorLine } from "../../components/ui";
import type { BuildHistoryRow, ItemView, JobDetail } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { BuildPanel } from "./BuildPanel";
import * as act from "./jobActions";
import {
  attemptMarks, attemptPct, attemptState, DIM, FAINT, isRunning, MODE_LABEL, pickLabel, sourceLabel, VERB,
} from "./jobModel";
import type { TabProps } from "./JobWorkspace";

function attemptNote(item: ItemView, row: BuildHistoryRow): { text: string; bad: boolean } {
  if (row.error) return { text: row.error, bad: true };
  const from = item.build_history.findIndex((h) => h.id === row.derived_from);
  return { text: row.mode === "reexport" && from >= 0 ? `Raw from attempt ${from + 1}. No new sampling.` : "", bad: false };
}

function settingsLine(job: JobDetail, item: ItemView, row: BuildHistoryRow): string {
  const bake = row.current ? item.build?.checkpoints.bake?.settings : undefined;
  const tris = row.overrides.triangles ?? (typeof bake?.decimation_target === "number" ? bake.decimation_target : null);
  const tex = row.overrides.texture_size ?? (typeof bake?.texture_size === "number" ? bake.texture_size : null);
  const parts = [row.seed != null ? `seed ${row.seed}` : "", job.kind === "model3d" ? (tris ? `${tris.toLocaleString()} tris` : "default triangles") : "",
    tex ? `${tex}²` : ""];
  return parts.filter(Boolean).join(" · ");
}

function AttemptRow({ job, item, row, n, on, onPick }:
  { job: JobDetail; item: ItemView; row: BuildHistoryRow; n: number; on: boolean; onPick: () => void }) {
  const is3d = job.kind === "model3d" && !job.direct;
  const [state, color] = attemptState(row, is3d);
  const note = attemptNote(item, row);
  const source = sourceLabel(item, row, job.direct, job.kind);
  const line = [source ? `from ${source}` : "", settingsLine(job, item, row)]
    .filter(Boolean).join(" · ");
  return (
    <button className={`jw-attempt${on ? " on" : ""}`} onClick={onPick} aria-pressed={on} aria-label={`attempt ${n}, ${state}`}>
      <div className="n stripes">#{n}</div>
      <div style={{ gridArea: "info", display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
        <span style={{ fontWeight: 500 }}>Attempt {n} · {job.direct ? "transform" : MODE_LABEL[row.mode] ?? row.mode}</span>
        {line && <span className="sub" style={{ overflowWrap: "anywhere" }}>{line}</span>}
        {note.text && <span style={{ fontSize: 11.5, color: note.bad ? "var(--bad)" : DIM }}>{note.text}</span>}
      </div>
      <div style={{ gridArea: "ck", display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
        <div style={{ height: 4, background: "var(--line)", borderRadius: 2 }}>
          <div style={{ height: 4, borderRadius: 2, background: color, width: `${attemptPct(row, is3d)}%` }} /></div>
        <div className="row" style={{ gap: 9, flexWrap: "wrap" }}>
          {attemptMarks(row, is3d).map((m) => <span key={m.k} className="sub" style={{ color: m.color }}>{m.mark} {m.k}</span>)}
        </div>
      </div>
      <div style={{ gridArea: "st", display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 3 }}>
        <span className="sub" style={{ color, fontSize: 11 }}>{state}</span>
        {row.accepted && <span className="sub" style={{ color: "var(--ok)", fontWeight: 600 }}>accepted</span>}
      </div>
    </button>
  );
}

export function BuildTab({ job, item, reload, goTab }: TabProps) {
  const { id } = useProject();
  const a = useAction();
  const [picked, setPicked] = useState<string | null>(null);
  const history = item.build_history;
  const newestFirst = history.map((row, i) => ({ row, n: i + 1 })).reverse();
  const sel = newestFirst.find((x) => x.row.id === picked) ?? newestFirst.find((x) => x.row.current) ?? newestFirst[0];
  const latest = history[history.length - 1];
  const pick = pickLabel(item, job.direct, job.kind);
  const currentRow = history.find((h) => h.current);
  const canStart = job.direct ? item.legal.run_transform && history.length === 0
    : !!item.approval && !isRunning(latest) && !item.accepted_build
      && (history.length === 0 || !item.build || !currentRow?.matches_approval);
  const blocked = !job.direct && !job.recipe.build_available;
  const verb = VERB[job.kind];
  const start = () => void a.run(async () => {
    if (job.direct) await act.startTransform(id, job.id, item.id);
    else await act.startBuild(id, job.id, item.id, "build");
    setPicked(null);
    reload();
  });
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(340px,1fr))", gap: 18, alignItems: "start" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
        {blocked && <div className="banner bad">{job.recipe.build_label} build is not available right now: {job.recipe.build_blocked_reason}.
          Approved candidates and earlier attempts are kept.</div>}
        <div className="row" style={{ alignItems: "baseline", flexWrap: "wrap" }}>
          <span className="label">Attempts · every attempt is kept</span><span className="grow" />
          <span className="sub" style={{ color: "var(--muted)", fontSize: 11 }}>
            {job.direct ? `${pick}${job.variant ? ` · ${job.variant.source_name} v${job.variant.source_display_version}` : ""}`
              : pick ? `approved ${pick}` : "nothing approved"}</span>
        </div>
        {canStart && (
          <div style={{ border: "1px dashed #33353a", borderRadius: 9, padding: "24px 20px", display: "flex", flexDirection: "column",
            alignItems: "center", gap: 12, textAlign: "center" }}>
            <span className="muted" style={{ fontSize: 12.5 }}>
              {job.direct ? "The transform is deterministic and keeps the source untouched. Nothing has been run yet."
                : history.length ? `Approval changed to ${pick}. Build it as a new attempt; earlier attempts stay.` : `Approved ${pick}. Nothing built yet.`}</span>
            <button className="btn btn-primary" disabled={a.busy || blocked} onClick={start}>
              {job.direct ? "Run transform" : `${verb} from ${pick}`}</button>
          </div>)}
        {!canStart && history.length === 0 && (
          <div className="empty" style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
            {job.direct ? "Nothing to run yet." : "Nothing to build yet. Approve a candidate in Prompt & candidates."}
            {!job.direct && <button className="btn" onClick={() => goTab("prompt")}>Go to Prompt & candidates</button>}
          </div>)}
        {history.length > 0 && (
          <div className="jw-attempts">
            {newestFirst.map(({ row, n }) => <AttemptRow key={row.id} job={job} item={item} row={row} n={n} on={row.id === sel?.row.id}
              onPick={() => setPicked(row.id)} />)}
          </div>)}
        <ErrorLine error={a.error} />
        {item.accepted_build && (
          <div className="row" style={{ gap: 10 }}>
            <span className="sub" style={{ color: FAINT }}>An attempt is accepted.</span>
            <button className="btn btn-primary" onClick={() => goTab("publish")}>Go to publish →</button>
          </div>)}
      </div>
      {sel && <BuildPanel job={job} item={item} sel={sel.row} n={sel.n} reload={reload} goPrompt={() => goTab("prompt")} />}
    </div>
  );
}
