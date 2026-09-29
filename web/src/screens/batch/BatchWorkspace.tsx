import type { ReactNode } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { ErrorLine, Loading } from "../../components/ui";
import { type BatchDetail, P } from "../../lib/api";
import { type Loaded, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { TAB_NAMES } from "../Batches";
import { ApproveTab } from "./ApproveTab";
import { BuildTab } from "./BuildTab";
import { CandidatesTab } from "./CandidatesTab";
import { PromptsTab } from "./PromptsTab";
import { PublishTab } from "./PublishTab";

export interface TabProps { batch: BatchDetail; reload: () => void }

export function ActionBar({ note, sub, children }: { note: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="actionbar">
      <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1, minWidth: 280 }}>
        <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>{note}</span>
        {sub && <span className="sub">{sub}</span>}
      </div>
      {children}
    </div>
  );
}

function tabState(b: BatchDetail, t: (typeof TAB_NAMES)[number]): string {
  const c = b.counts;
  switch (t) {
    case "prompts": return `${c.confirmed}/${c.items} confirmed`;
    case "candidates": return `${c.candidates}/${c.items} ready`;
    case "approve": return `${c.approved}/${c.items} approved`;
    case "build": return b.recipe.build_available ? `${c.built}/${c.approved} built` : "unavailable";
    case "publish": return `${c.published}/${c.accepted} published`;
  }
}

export function useBatch(batchId: string): Loaded<BatchDetail> {
  const { id } = useProject();
  return useApi<BatchDetail>(`${P(id)}/batches/${batchId}`, { project: id, batch: batchId, pollMs: 15000 });
}

export function BatchWorkspace() {
  const { id } = useProject();
  const { batchId = "", tab } = useParams();
  const b = useBatch(batchId);
  if (!b.data) return b.error ? <div className="content"><ErrorLine error={b.error} /></div> : <Loading what="batch" />;
  const batch = b.data;
  if (!tab || !(TAB_NAMES as readonly string[]).includes(tab)) {
    const t = batch.current_tab === "done" ? "publish" : batch.current_tab;
    return <Navigate to={`/p/${id}/batches/${batchId}/${t}`} replace />;
  }
  const props: TabProps = { batch, reload: b.reload };
  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100%" }}>
      <div style={{ padding: "16px 24px 0", display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <Link to={`/p/${id}/batches`} className="sub" style={{ textDecoration: "none" }}>
            ← Batches / {batch.alias} · {batch.kind_label} · {batch.category_label ?? "no category"}</Link>
          <h1 className="h1" style={{ margin: "2px 0 0" }}>{batch.title}</h1>
        </div>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <span className="tag">{batch.source}</span>
          <span className="tag" title="seed family (reproducible)">seed {batch.seed_family}</span>
          <span className="tag" title="configuration captured at creation">config r{batch.config_revision}</span>
        </div>
      </div>
      <nav className="tabs" style={{ margin: "14px 24px 0" }} aria-label="batch stages">
        {TAB_NAMES.map((t, i) => (
          <Link key={t} to={`/p/${id}/batches/${batchId}/${t}`} className={`tab${tab === t ? " on" : ""}`}
            aria-current={tab === t ? "page" : undefined}>
            <span className="sub" style={{ fontSize: 10.5 }}>0{i + 1} · {tabState(batch, t)}</span>
            <span>{t === "build" ? batch.recipe.build_label : t[0]!.toUpperCase() + t.slice(1)}</span>
          </Link>
        ))}
      </nav>
      <div style={{ padding: "16px 24px 24px", flex: 1, minWidth: 0 }}>
        {tab === "prompts" && <PromptsTab {...props} />}
        {tab === "candidates" && <CandidatesTab {...props} />}
        {tab === "approve" && <ApproveTab {...props} />}
        {tab === "build" && <BuildTab {...props} />}
        {tab === "publish" && <PublishTab {...props} />}
      </div>
    </div>
  );
}
