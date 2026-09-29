import { Link, useNavigate } from "react-router-dom";

import { Empty, ErrorLine, Loading, PageHead } from "../../components/ui";
import { type BatchGroup, J, type JobSummary, key, send, V2 } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { batchStats, plural } from "./batchModel";

const COLS = "minmax(0,1.2fr) minmax(0,.8fr) minmax(0,.9fr) minmax(0,1.3fr) auto";

function actionOf(s: ReturnType<typeof batchStats>): { label: string; primary: boolean } {
  if (!s.total) return { label: "Add Jobs", primary: false };
  if (s.drafts) return { label: `Start · ${plural(s.drafts, "draft")}`, primary: true };
  if (s.wait) return { label: "Review", primary: true };
  return { label: "Open", primary: false };
}

export function Batches() {
  const { id } = useProject();
  const nav = useNavigate();
  const list = useApi<{ batches: BatchGroup[] }>(`${V2(id)}/batches`, { project: id, pollMs: 10000 });
  const jobs = useApi<{ jobs: JobSummary[] }>(J(id), { project: id, pollMs: 10000 });
  const act = useAction();
  const create = () => void act.run(async () => {
    const n = (list.data?.batches.length ?? 0) + 1;
    const out = await send<{ batch: BatchGroup }>("POST", `${V2(id)}/batches`, {
      title: `Batch ${n}`, job_ids: [], idempotency_key: key() });
    nav(`/p/${id}/batches/${out.batch.id}?add=1`);
  });
  const byId = new Map((jobs.data?.jobs ?? []).map((j) => [j.id, j]));
  return (
    <div className="content narrow" style={{ maxWidth: 1320 }}>
      <PageHead sub="Groups of Jobs · each stage runs across all of them" title="Batches">
        <button className="btn btn-primary" disabled={act.busy || !list.data} onClick={create}>New Batch</button>
      </PageHead>
      <ErrorLine error={act.error ?? list.error ?? jobs.error} />
      {!list.data || !jobs.data ? <Loading what="batches" /> : list.data.batches.length === 0 ? (
        <Empty>No Batches yet. New Batch creates an empty group; then add saved Jobs from this project.</Empty>
      ) : (
        <div className="table" role="list" aria-label="Batches">
          {list.data.batches.map((b) => {
            const s = batchStats(b.job_ids.flatMap((jid) => byId.get(jid) ?? []));
            const a = actionOf(s);
            return (
              <Link key={b.id} role="listitem" to={`/p/${id}/batches/${b.id}${s.total ? "" : "?add=1"}`} className="td clickable"
                style={{ textDecoration: "none", color: "inherit", gridTemplateColumns: COLS, padding: "12px 16px" }}>
                <span style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 500 }}>{b.title}</div>
                  <div className="sub">{b.alias} · {s.total} Jobs</div>
                </span>
                <span className="sub" style={{ color: "#a3a4a1" }}>{s.kinds}</span>
                <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, color: s.statusColor }}>
                  <span aria-hidden style={{ width: 6, height: 6, borderRadius: "50%", background: s.statusColor }} />{s.status}</span>
                <span style={{ fontSize: 12, color: "#a3a4a1" }}>{s.gates}</span>
                <span style={{ display: "flex", justifyContent: "flex-end" }}>
                  <span style={{ fontSize: 12, borderRadius: 5, padding: "3px 9px", fontWeight: 500,
                    background: a.primary ? "#e8e8e6" : "transparent", color: a.primary ? "#111213" : "#a3a4a1",
                    border: `1px solid ${a.primary ? "#e8e8e6" : "#2e3033"}` }}>{a.label}</span></span>
              </Link>);
          })}
        </div>)}
      <div style={{ fontSize: 12, color: "#8b8c87", maxWidth: 820 }}>
        A Batch groups Jobs but does not own their content. Start runs the first stage for every Job, then stops at each review gate.
        Jobs you leave undecided stay in the Batch for a later wave.</div>
    </div>
  );
}
