import { Link } from "react-router-dom";

import { Bar, Empty, ErrorLine, INFO, Loading, OK, PageHead, WARN } from "../components/ui";
import { type BatchSummary, P } from "../lib/api";
import { useApi } from "../lib/hooks";
import { useProject } from "../lib/project";

export const TAB_NAMES = ["prompts", "candidates", "approve", "build", "publish"] as const;

/** Five aggregate strips with explicit denominators (never one misleading percentage). */
export function stageStrips(b: BatchSummary): { label: string; pct: number; color: string }[] {
  const c = b.counts;
  const n = Math.max(c.items, 1);
  const s = (label: string, done: number, of: number, active: boolean) => ({
    label: `${label} ${done}/${of}`, pct: of ? (done / of) * 100 : 0,
    color: of && done >= of ? OK : active ? WARN : INFO });
  return [
    s("prompts", c.confirmed, c.items, b.current_tab === "prompts"),
    s("cands", c.candidates, c.items, b.current_tab === "candidates"),
    s("approved", c.approved, n, b.current_tab === "approve"),
    s("built", c.built, c.approved || 0, b.current_tab === "build"),
    s("published", c.published, c.accepted || c.built || 0, b.current_tab === "publish"),
  ];
}

export function Batches() {
  const { id } = useProject();
  const list = useApi<{ batches: BatchSummary[] }>(`${P(id)}/batches`, { project: id, pollMs: 10000 });
  return (
    <div className="content narrow" style={{ maxWidth: 1320 }}>
      <PageHead sub="Prompts → candidates → approve → build → publish" title="Batches">
        <Link className="btn btn-primary" to={`/p/${id}/batches/new`} style={{ textDecoration: "none" }}>New batch</Link>
      </PageHead>
      <ErrorLine error={list.error} />
      {!list.data ? <Loading what="batches" /> : list.data.batches.length === 0 ? (
        <Empty>No batches yet. A single asset is a batch of one — <Link to={`/p/${id}/batches/new`}>start one</Link>.</Empty>
      ) : (
        <div className="table">
          {list.data.batches.map((b) => (
            <Link key={b.id} to={`/p/${id}/batches/${b.id}`} className="td clickable"
              style={{ gridTemplateColumns: "minmax(150px,1fr) auto minmax(260px,2fr) auto", textDecoration: "none", color: "inherit", padding: "12px 16px" }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 500 }}>{b.title}</div>
                <div className="sub">{b.alias} · {b.counts.items} items · {b.category_label ?? "no category"}</div>
              </div>
              <span className="tag">{b.kind_label}</span>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 4 }}>
                {stageStrips(b).map((st) => (
                  <div key={st.label} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    <Bar pct={st.pct} color={st.color} />
                    <span className="sub" style={{ fontSize: 10 }}>{st.label}</span>
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, alignItems: "center" }}>
                {b.counts.failed > 0 && <span className="pill bad">{b.counts.failed} failed</span>}
                <span className={`pill ${b.waiting_on_user ? "warn" : b.next_action === "done" ? "ok" : "none"}`}>{b.next_action}</span>
              </div>
            </Link>
          ))}
        </div>
      )}
      <div className="sub" style={{ fontFamily: "var(--sans)", fontSize: 12 }}>
        Each stage runs as a pass over the selected items so a model stays loaded for the pass; the scheduler records passes, not guaranteed load counts.</div>
    </div>
  );
}
