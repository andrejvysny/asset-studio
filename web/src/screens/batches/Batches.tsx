import { Link } from "react-router-dom";

import { Empty, ErrorLine, Loading, PageHead } from "../../components/ui";
import { type BatchGroup, KIND_LABEL, V2 } from "../../lib/api";
import { useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { runPill } from "./RunView";

export function Batches() {
  const { id } = useProject();
  const list = useApi<{ batches: BatchGroup[] }>(`${V2(id)}/batches`, { project: id, pollMs: 10000 });
  return (
    <div className="content narrow" style={{ maxWidth: 1320 }}>
      <PageHead sub="A Batch groups saved Jobs; one start runs their compatible stages together, model by model" title="Batches">
        <Link className="btn" to={`/p/${id}/jobs`} style={{ textDecoration: "none" }}>Select Jobs to group →</Link>
      </PageHead>
      <ErrorLine error={list.error} />
      {!list.data ? <Loading what="batches" /> : list.data.batches.length === 0 ? (
        <Empty>No Batches yet. Select saved Jobs on the <Link to={`/p/${id}/jobs`}>Jobs</Link> screen and choose “Create Batch”.</Empty>
      ) : (
        <div className="table">
          <div className="th" style={{ gridTemplateColumns: "minmax(180px,1.4fr) 110px minmax(160px,1fr) minmax(220px,1.4fr) 150px" }}>
            <span>Batch</span><span>Jobs · items</span><span>Kinds</span><span>Latest run</span><span>Status</span></div>
          {list.data.batches.map((b) => {
            const r = b.latest_run;
            return (
              <Link key={b.id} to={`/p/${id}/batches/${b.id}`} className="td clickable" style={{ textDecoration: "none", color: "inherit",
                gridTemplateColumns: "minmax(180px,1.4fr) 110px minmax(160px,1fr) minmax(220px,1.4fr) 150px" }}>
                <span><span style={{ fontWeight: 500 }}>{b.title}</span> <span className="sub">{b.alias}</span></span>
                <span className="sub">{b.jobs} · {b.items}</span>
                <span className="sub">{b.kinds.map((k) => KIND_LABEL[k]).join(", ") || "—"}</span>
                <span className="sub">{r ? `${r.counts.prompts_waiting} prompts waiting · ${r.counts.undecided} undecided · ${r.counts.builds_valid} valid builds` : "never run"}</span>
                <span>{r ? runPill(r.status) : <span className="pill none">not started</span>}</span>
              </Link>);
          })}
        </div>)}
    </div>
  );
}
