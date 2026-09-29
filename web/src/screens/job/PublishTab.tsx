import { useState } from "react";
import { Link } from "react-router-dom";

import { ErrorLine, OK, Toggle } from "../../components/ui";
import { ApiError, type PublishPreviewRow } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import * as api from "../../lib/jobsApi";
import { useProject } from "../../lib/project";
import { DIM, firstFailure, isBusy, pickLabel } from "./jobModel";
import type { TabProps } from "./JobWorkspace";

function Kv({ k, v, color, children }: { k: string; v?: string; color?: string; children?: React.ReactNode }) {
  return (
    <div className="jw-kv">
      <span style={{ fontSize: 12, color: DIM }}>{k}</span>
      <span className="mono" style={{ fontSize: 12, fontWeight: 500, color: color ?? "var(--text)", overflowWrap: "anywhere" }}>{v}{children}</span>
    </div>
  );
}

export function PublishTab({ job, item, reload }: TabProps) {
  const { id } = useProject();
  const prev = useApi<{ items: PublishPreviewRow[] }>(`/api/v2/projects/${id}/jobs/${job.id}/publish-preview`, { project: id, job: job.id });
  const a = useAction();
  const [keepCurrent, setKeepCurrent] = useState(false);
  const row = prev.data?.items.find((r) => r.item_id === item.id);
  const accepted = item.build_history.findIndex((h) => h.id === item.accepted_build);
  const publishing = isBusy(item, "publish");
  const failed = item.tasks.publish?.state === "failed";
  const published = !!item.published || !!row?.published;
  const newAsset = row?.new_asset ?? true;
  const makeCurrent = newAsset || !keepCurrent;
  const from = accepted >= 0 ? `attempt ${accepted + 1}${item.build?.id === item.accepted_build ? ` · ${pickLabel(item, job.direct, job.kind)}` : ""}` : "no accepted attempt";
  const derived = row?.derived_from;

  const publish = () => void a.run(async () => {
    if (!row) return;
    try {
      const res = await api.publish(id, job.id, [{ item_id: row.item_id, build_run_id: row.build_run_id,
        expected_item_revision: row.expected_item_revision, make_current: makeCurrent,
        expected_current_version: row.current_version_id }]);
      const bad = firstFailure(res);
      if (bad && bad.code !== "already_published") throw bad;
    } catch (e) {
      if (!(e instanceof ApiError && e.code === "already_published")) throw e;
    }
    reload();
    prev.reload();
  });

  const label = published ? "Published ✓" : publishing ? "Publishing…" : row ? "Publish" : "Accept an attempt first";
  const ready = !!row && !published && !publishing;
  return (
    <div style={{ maxWidth: 760, display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="jw-attempts" role="table" aria-label="publication">
        <Kv k="Publishes as" v={row?.name_id ?? "—"} />
        <Kv k="Version" v={published && item.published ? `v${item.published.display_version} · published`
          : !row ? "—" : newAsset ? "new asset · v1" : `v${row.next_display_version - 1} → v${row.next_display_version}`}
          color={newAsset && row && !published ? OK : undefined} />
        <Kv k="From" v={from} />
        <Kv k="Family" v={row?.family?.name ?? job.family?.name ?? "—"} />
        <Kv k="Derived from">
          {derived ? <Link to={`/p/${id}/assets/${derived.asset_id}?version=${derived.version_id}`}>{derived.name} · v{derived.display_version}</Link> : "—"}
        </Kv>
        <Kv k="Current pointer" v={!row || published ? "—" : newAsset ? "new asset" : makeCurrent ? "set to new version" : "stays on the current version"}
          color={newAsset && row && !published ? OK : undefined}>
          {row && !newAsset && !published && <span style={{ marginLeft: 10, verticalAlign: "middle" }}>
            <Toggle on={makeCurrent} label="make the new version current" onChange={(v) => setKeepCurrent(!v)} /></span>}
        </Kv>
        {published && item.published && (
          <Kv k="Published as"><Link to={`/p/${id}/assets/${item.published.asset_id}`}>v{item.published.display_version} published</Link></Kv>)}
      </div>
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        <button className={`btn${ready ? " btn-primary" : ""}`} style={{ padding: "8px 16px" }} disabled={!ready || a.busy} onClick={publish}>{label}</button>
        <span className="muted" style={{ fontSize: 12 }}>{published ? "Written as an immutable version."
          : "Publishing writes an immutable version. It does not export anything."}</span>
      </div>
      {failed && <div className="banner bad">Publishing failed: {item.tasks.publish?.error}</div>}
      <ErrorLine error={a.error ?? prev.error} />
    </div>
  );
}
