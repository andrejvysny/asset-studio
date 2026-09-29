import { useEffect, useState } from "react";

import { OutputView } from "../../components/outputs";
import { BAD, ErrorLine, INFO, NONE, OK, WARN } from "../../components/ui";
import {
  J, key, send, type BuildHistoryRow, type BuildRunDetail, type BuildRunView, type GlbTransformReport, type ItemView,
  type JobDetail, type RasterTransformReport, type RebuildOverrides,
} from "../../lib/api";
import { getBuildRun } from "../../lib/jobsApi";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { BuildSettings, type SettingsBase } from "./BuildSettings";
import * as act from "./jobActions";
import { attemptState, DIM, describeTransform, isGlbReport, isRasterReport, isRunning, pickLabel } from "./jobModel";
import { ReexportDialog } from "./ReexportDialog";

/** Full record + meta report of the selected attempt (any attempt of the item). */
function useRun(jobId: string, sel: BuildHistoryRow, fallback: BuildRunView | null): BuildRunDetail | null {
  const { id } = useProject();
  const [run, setRun] = useState<BuildRunDetail | null>(null);
  useEffect(() => {
    let alive = true;
    void getBuildRun(id, jobId, sel.id).then((r) => { if (alive) setRun(r); }).catch(() => undefined);
    return () => { alive = false; };
  }, [id, jobId, sel.id, sel.status, sel.result, sel.preview]);
  if (run && run.id === sel.id) return run;
  return fallback && fallback.id === sel.id ? { ...fallback, meta: null } : null;
}

const fmt = (n: number): string => Number(n.toPrecision(6)).toString();

function Stats({ cells }: { cells: [string, string, string?][] }) {
  return (
    <div className="jw-stats">
      {cells.map(([k, v, c]) => (
        <div key={k}><span style={{ fontSize: 10.5, color: DIM }}>{k}</span>
          <span className="mono" style={{ fontSize: 11.5, fontWeight: 500, color: c ?? "var(--text)", overflowWrap: "anywhere" }}>{v}</span></div>))}
    </div>
  );
}

function transformCells(t: GlbTransformReport | RasterTransformReport | undefined): [string, string, string?][] {
  if (isGlbReport(t)) {
    return [["requested", describeTransform(t.transform as unknown as Record<string, never>)],
      ["effective scale", t.effective_scale.map(fmt).join(" × ")], ["anchor", t.anchor.replace("_", " ")],
      ["input height", `${fmt(t.input_height)} m`], ["output height", `${fmt(t.output_height)} m`],
      ["height check", t.height_ok === undefined ? "n/a" : t.height_ok ? "ok" : "off target", t.height_ok === false ? BAD : t.height_ok ? OK : undefined]];
  }
  if (isRasterReport(t)) {
    return [["operation", t.op.replace(/_/g, " ")], ["input", `${t.input_size.join("×")} px`], ["output", `${t.output_size.join("×")} px`],
      ["resample", t.resample ?? "—"], ["background", t.background ?? "—"], ["content", t.content_bounds ? "measured" : "—"]];
  }
  return [["transform report", "loading…"]];
}

interface Props { job: JobDetail; item: ItemView; sel: BuildHistoryRow; n: number; reload: () => void; goPrompt: () => void }

export function BuildPanel({ job, item, sel, n, reload, goPrompt }: Props) {
  const { id } = useProject();
  const a = useAction();
  const [rx, setRx] = useState<"reexport" | "rebuild" | null>(null);
  const view = useRun(job.id, sel, item.build);
  const meta = view?.meta ?? null;
  const is3d = job.kind === "model3d";
  const [state, color] = attemptState(sel, is3d);
  const bake = view?.checkpoints.bake?.settings;
  const latest = item.build_history[item.build_history.length - 1];
  const idle = !isRunning(latest);
  const base = `${J(id)}/${job.id}`;
  const run = (fn: () => Promise<unknown>) => void a.run(async () => { await fn(); reload(); });
  const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
  const settings: SettingsBase = { triangles: num(bake?.decimation_target), texture_size: num(bake?.texture_size),
    remesh: typeof bake?.remesh === "boolean" ? bake.remesh : null };
  const valid = sel.result === "valid";
  const from = sel.current && item.approval ? ` · from ${pickLabel(item, job.direct, job.kind)}` : "";

  const cells: [string, string, string?][] = job.direct ? transformCells(meta?.transform) : is3d ? [
    ["triangles", meta?.mesh?.triangles?.toLocaleString() ?? "—"],
    ["budget", meta?.budget && (meta.budget.min != null || meta.budget.max != null) ? `${meta.budget.min ?? "—"}–${meta.budget.max ?? "—"}` : "none"],
    ["target", meta?.budget?.effective?.toLocaleString() ?? "—"],
    ["texture", settings.texture_size ? `${settings.texture_size}²` : "—"],
    ["remesh (cleanup)", settings.remesh === null ? "—" : settings.remesh ? "on" : "off"],
    ["GLB check", view?.validation.ok === undefined ? "—" : view.validation.ok ? "pass" : "fail", view?.validation.ok ? OK : view?.validation.ok === false ? BAD : undefined],
  ] : [
    ["check", view?.validation.ok === undefined ? "—" : view.validation.ok ? "pass" : "fail", view?.validation.ok ? OK : view?.validation.ok === false ? BAD : undefined],
    ["checks ok", view ? `${(view.validation.checks ?? []).filter((c) => c.ok).length}/${(view.validation.checks ?? []).length}` : "—"],
    ["preview", sel.preview ?? "—"], ["seed", sel.seed == null ? "—" : String(sel.seed)],
    ["from", from ? pickLabel(item, false, job.kind) : "—"], ["files", view ? String(Object.keys(view.artifacts).length) : "—"],
  ];

  const tryAgain: { label: string; sub: string; off?: boolean; go: () => void }[] = job.direct
    ? [{ label: "Retry same settings", sub: "Same source and transform", off: !item.legal.run_transform,
      go: () => run(() => act.startTransform(id, job.id, item.id)) }]
    : [{ label: "Retry same settings", sub: latest?.status === "failed" && latest.has_raw ? "Resumes from the saved raw mesh" : "Same seed and settings",
      go: () => run(() => act.startBuild(id, job.id, item.id, "retry")) },
    ...(is3d ? [
      { label: "Resample · new seed", sub: "Full 3D rebuild from the approved image", go: () => run(() => act.startBuild(id, job.id, item.id, "resample")) },
      { label: "Re-export from raw", sub: "New triangle or texture settings, no resampling", off: !sel.has_raw, go: () => setRx("reexport") },
      { label: "Change settings, rebuild", sub: "Edit mesh settings, then full rebuild", go: () => setRx("rebuild") }] : []),
    { label: "Pick a different candidate", sub: "Back to rounds. Attempts are kept.", go: goPrompt }];

  return (
    <div className="jw-sticky">
      <div className="row" style={{ justifyContent: "space-between", padding: "8px 12px", borderBottom: "1px solid #222326", font: "500 11px var(--mono)" }}>
        <span>Attempt {n}{from}</span><span style={{ color }}>{state}</span>
      </div>
      {view ? <OutputView key={view.id} project={id} roles={view.artifacts} alt={`attempt ${n} result`} />
        : <div className="stripes sub" style={{ height: 160, display: "flex", alignItems: "center", justifyContent: "center", padding: 12, textAlign: "center" }}>
          No output yet.</div>}
      {sel.error && <div className="banner bad" style={{ margin: 12 }}>{sel.error}
        {sel.has_raw ? " · the raw output is kept: building again or re-exporting reuses it." : ""}</div>}
      {view?.status === "failed" && !sel.error && <div className="banner bad" style={{ margin: 12 }}>
        {view.validation.failed_stage ?? "build"} failed ({view.validation.failure_code}): {view.error}</div>}
      {view?.result === "valid" && view.preview === "failed" && (
        <div className="banner note" style={{ margin: 12 }}>Preview failed ({view.preview_error}). The result itself is valid; retrying renders the preview again.
          <button className="btn" style={{ marginLeft: 8 }} disabled={a.busy || !item.legal.retry_preview}
            onClick={() => run(() => send("POST", `${base}:retry-preview`, { idempotency_key: key(),
              items: [{ item_id: item.id, expected_item_revision: item.revision }] }))}>Retry preview</button></div>)}
      <Stats cells={cells} />
      {(view?.validation.checks ?? []).length > 0 && (
        <div style={{ padding: "8px 12px", display: "flex", flexDirection: "column", gap: 3, borderTop: "1px solid #222326" }}>
          <span className="label">{job.direct ? "Preservation checks" : "Structural validation"}</span>
          {view!.validation.checks!.map((c) => (
            <div key={c.id} className="row" style={{ gap: 8, alignItems: "baseline" }}>
              <span className="dot" style={{ background: c.ok ? OK : c.advisory ? WARN : BAD }} />
              <span className="mono" style={{ fontSize: 11 }}>{c.id}</span>
              <span className="sub grow" style={{ textAlign: "right", overflowWrap: "anywhere" }}>{c.ok ? "ok" : c.advisory ? "advisory" : "FAIL"} {c.detail ?? ""}</span>
            </div>))}
        </div>)}
      <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 9 }}>
        {valid && (
          <button className={`btn jw-cta${sel.accepted ? "" : " btn-primary"}`} style={sel.accepted ? { color: OK, borderColor: OK } : undefined}
            disabled={a.busy || !!item.published} onClick={() => run(() => act.setAccepted(id, job.id, item.id, sel.id, !sel.accepted))}>
            {item.published ? "Published ✓" : sel.accepted ? `Accepted attempt ${n} ✓ · click to undo` : `Accept attempt ${n}`}</button>)}
        {idle && !item.accepted_build && !item.published && !rx && (item.build_history.length > 0) && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="label">Not right? Try again</span>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              {tryAgain.map((t) => <button key={t.label} className="jw-retry" disabled={a.busy || t.off} onClick={t.go}>
                <span style={{ fontSize: 12, fontWeight: 500 }}>{t.label}</span><small>{t.sub}</small></button>)}
            </div>
          </div>)}
        {rx === "rebuild" && <BuildSettings base={settings} busy={a.busy} onCancel={() => setRx(null)}
          onRun={(o: RebuildOverrides) => void a.run(async () => { await act.startBuild(id, job.id, item.id, "rebuild", o); setRx(null); reload(); })} />}
        {item.accepted_build && !item.published && <span className="sub" style={{ color: INFO }}>Undo the acceptance to try again.</span>}
        {!job.recipe.build_available && !job.direct && <span className="sub" style={{ color: NONE }}>{job.recipe.build_label} build unavailable: {job.recipe.build_blocked_reason}</span>}
        <ErrorLine error={a.error} />
      </div>
      {rx === "reexport" && <ReexportDialog base={base} itemId={item.id} runId={sel.id} revision={item.revision}
        onClose={() => setRx(null)} onDone={() => { setRx(null); reload(); }} />}
    </div>
  );
}
