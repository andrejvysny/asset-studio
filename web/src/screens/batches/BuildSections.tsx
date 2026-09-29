import { Link } from "react-router-dom";

import { BAD, INFO, OK, W } from "../../components/progress";
import { type JobDetail, type PublishPreviewRow } from "../../lib/api";
import { runTransform } from "../../lib/jobsApi";
import { type BatchCtx } from "./batchModel";
import { Card, GHOST_BTN, MONO_SUB, PRIMARY_BTN, Row } from "./batchUi";
import { post } from "./reviewApi";
import type { RRow } from "./ReviewTab";

type BuildKind = "build" | "retry" | "transform" | "accept" | "none";
export interface BuildRow extends RRow { kind: BuildKind; state: string; color: string }

const ACTIVE = ["queued", "running", "blocked", "reconciling"];

/** One row per item that is between "approved" and "accepted": needs a build, is building, failed, or awaits accept. */
export function buildRowsOf(rows: RRow[]): BuildRow[] {
  const out: BuildRow[] = [];
  for (const r of rows) {
    const { it, job } = r;
    if (it.accepted_build) continue;
    const bt = it.tasks.build;
    const attempt = it.build_history.length;
    if (bt && ACTIVE.includes(bt.state)) {
      const p = bt.progress.total ? ` ${Math.round(((bt.progress.done ?? 0) / bt.progress.total) * 100)}%` : "";
      out.push({ ...r, kind: "none", state: `attempt ${attempt || 1} · ${bt.state}${p}`, color: INFO });
    } else if (job.direct && it.legal.run_transform) {
      out.push({ ...r, kind: "transform", state: it.build?.status === "failed" ? "transform failed" : "transform not run", color: it.build?.status === "failed" ? BAD : "#c9cac6" });
    } else if (it.legal.accept && it.build) {
      out.push({ ...r, kind: "accept", state: `attempt ${attempt} done`, color: "#c9cac6" });
    } else if (!job.direct && it.approval && it.build?.status === "failed") {
      out.push({ ...r, kind: "retry", state: `attempt ${attempt} failed · raw saved`, color: BAD });
    } else if (!job.direct && it.approval && it.legal.build) {
      const invalid = it.build ? ` · attempt ${attempt} ${it.build.result ?? "not valid"}` : "";
      out.push({ ...r, kind: "build", state: `approved${it.approved ? ` R${it.approved.round ?? "?"}` : ""} · not built${invalid}`, color: "#c9cac6" });
    } else if (!job.direct && it.approval) {
      out.push({ ...r, kind: "none", state: "approved · build unavailable", color: "#8b8c87" });
    }
  }
  return out;
}

export function BuildsCard({ ctx, rows, run }: { ctx: BatchCtx; rows: BuildRow[]; run: (fn: () => Promise<void>) => void }) {
  const build = (rs: BuildRow[]) => run(() => post(ctx.project, ctx.active, "build", rs.map((r) => ({
    job_id: r.job.id, item_id: r.it.id, approval_id: r.it.approval, expected_item_revision: r.it.revision,
    mode: r.kind === "retry" ? "retry" : "build" }))));
  const accept = (rs: BuildRow[]) => run(() => post(ctx.project, ctx.active, "accept", rs.map((r) => ({
    job_id: r.job.id, item_id: r.it.id, build_run_id: r.it.build!.id, expected_item_revision: r.it.revision, accept: true }))));
  const toBuild = rows.filter((r) => r.kind === "build");
  const toAccept = rows.filter((r) => r.kind === "accept");
  const act = (r: BuildRow): [string, () => void, string] | null => {
    if (r.kind === "build") return ["Build", () => build([r]), W];
    if (r.kind === "retry") return ["Retry", () => build([r]), BAD];
    if (r.kind === "accept") return ["Accept", () => accept([r]), W];
    if (r.kind === "transform") {
      return ["Run transform", () => run(async () => {
        await runTransform(ctx.project, r.job.id, [{ item_id: r.it.id, expected_item_revision: r.it.revision }]);
      }), W];
    }
    return null;
  };
  return (
    <Card title="Builds" n={rows.length} actions={<>
      <button className="btn" style={GHOST_BTN} disabled={!toAccept.length} onClick={() => accept(toAccept)}>Accept {toAccept.length} built</button>
      <button className="btn btn-primary" style={PRIMARY_BTN} disabled={!toBuild.length} onClick={() => build(toBuild)}>
        {toBuild.length ? `Build ${toBuild.length} approved` : "Nothing to build"}</button></>}>
      {rows.map((r) => {
        const a = act(r);
        return (
          <Row key={r.it.id} cols="190px minmax(0,1fr) auto 70px">
            <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
              <span style={{ fontWeight: 500 }}>{r.it.name}</span>
              <span style={MONO_SUB}>{r.job.alias} · {r.job.kind_label}</span></div>
            <span style={{ font: "500 11.5px 'Geist Mono', monospace", color: r.color }}>{r.state}</span>
            {a ? <button className="btn" style={{ padding: "3px 10px", fontSize: 12, color: a[2], borderColor: a[2] }}
              aria-label={`${a[0]} ${r.it.name}`} onClick={a[1]}>{a[0]}</button> : <span style={{ color: "#8b8c87", fontSize: 12 }}>—</span>}
            <Link to={`/p/${ctx.project}/jobs/${r.job.id}`} className="sub"
              style={{ textAlign: "right", color: "#a3a4a1", textDecoration: "none", fontFamily: "Geist, sans-serif", fontSize: 12 }}>Open →</Link>
          </Row>);
      })}
    </Card>
  );
}

const target = (p: PublishPreviewRow): string => p.new_asset || !p.asset_id
  ? `${p.name_id} · new v1` : `${p.asset_id} · v${p.next_display_version - 1} → v${p.next_display_version}`;

export function PublishCard({ ctx, rows, jobs, run }:
  { ctx: BatchCtx; rows: PublishPreviewRow[]; jobs: JobDetail[]; run: (fn: () => Promise<void>) => void }) {
  const title = (id: string) => jobs.find((j) => j.id === id)?.title ?? id;
  const publish = () => run(() => post(ctx.project, ctx.active, "publish", rows.map((p) => ({
    job_id: p.job_id, item_id: p.item_id, build_run_id: p.build_run_id, expected_item_revision: p.expected_item_revision,
    make_current: true, expected_current_version: p.current_version_id }))));
  return (
    <Card title="Accepted · ready to publish" n={rows.length}
      actions={<button className="btn btn-primary" style={PRIMARY_BTN} onClick={publish}>Publish {rows.length} accepted</button>}>
      {rows.map((p) => (
        <Row key={p.item_id} cols="190px minmax(0,1fr) 70px">
          <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span style={{ fontWeight: 500 }}>{p.name}</span><span style={MONO_SUB}>{title(p.job_id)}</span></div>
          <span style={{ font: "500 11.5px 'Geist Mono', monospace", color: "#c9cac6" }}>{target(p)}
            {p.family ? <span style={{ color: OK }}> · family {p.family.name}</span> : null}
            {p.derived_from ? <span style={{ color: "#8b8c87" }}> · from {p.derived_from.name} v{p.derived_from.display_version}</span> : null}</span>
          <Link to={`/p/${ctx.project}/jobs/${p.job_id}`} className="sub"
            style={{ textAlign: "right", color: "#a3a4a1", textDecoration: "none", fontFamily: "Geist, sans-serif", fontSize: 12 }}>Open →</Link>
        </Row>))}
    </Card>
  );
}
