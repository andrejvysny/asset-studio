import { useState } from "react";
import { Link } from "react-router-dom";

import { Box, ErrorLine, OK, Toggle } from "../../components/ui";
import { key, P, send } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { ActionBar, type TabProps } from "./BatchWorkspace";

interface Row { item_id: string; name: string; build_run_id: string; expected_item_revision: number; published: boolean;
  asset_id: string | null; name_id: string; new_asset: boolean; current_version_id: string | null;
  next_display_version: number }

export function PublishTab({ batch, reload }: TabProps) {
  const { id } = useProject();
  const prev = useApi<{ items: Row[] }>(`${P(id)}/batches/${batch.id}/publish-preview`, { project: id, batch: batch.id });
  const [skip, setSkip] = useState<Set<string>>(new Set());
  const [keepCurrent, setKeepCurrent] = useState<Set<string>>(new Set());
  const act = useAction();
  const rows = prev.data?.items ?? [];
  const todo = rows.filter((r) => !r.published && !skip.has(r.item_id));
  const publishing = batch.items.filter((i) => i.tasks.publish && ["queued", "running"].includes(i.tasks.publish.state));
  const failed = batch.items.filter((i) => i.tasks.publish?.state === "failed");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {rows.length === 0 ? <div className="empty">Accept built results in the build tab first.</div> : (
        <div className="table">
          <div className="th" style={{ gridTemplateColumns: "30px minmax(150px,1fr) minmax(220px,1.4fr) 120px 150px", minWidth: 760 }}>
            <span /><span>Item</span><span>Publishes as</span><span>Version</span><span>Current</span></div>
          {rows.map((r) => {
            const item = batch.items.find((i) => i.id === r.item_id);
            const on = !skip.has(r.item_id) && !r.published;
            const cur = r.new_asset || !keepCurrent.has(r.item_id);
            return (
              <div key={r.item_id} className="td" style={{ gridTemplateColumns: "30px minmax(150px,1fr) minmax(220px,1.4fr) 120px 150px",
                minWidth: 760, opacity: r.published ? 0.6 : 1 }}>
                {r.published ? <span className="sub" style={{ color: OK }}>✓</span> : <Box on={on} label={`publish ${r.name}`}
                  onChange={(v) => { const n = new Set(skip); if (v) n.delete(r.item_id); else n.add(r.item_id); setSkip(n); }} />}
                <span style={{ fontWeight: 500 }}>{r.name}</span>
                <span className="mono" style={{ fontSize: 11.5 }}>{r.new_asset ? "new asset " : ""}{r.name_id}</span>
                <span className="sub" style={{ color: r.published ? OK : undefined }}>
                  {r.published && item?.published ? <Link to={`/p/${id}/assets/${item.published.asset_id}`}>v{item.published.display_version} published</Link>
                    : `v${r.next_display_version}${r.new_asset ? " (first)" : ""}`}</span>
                <label className="row" style={{ gap: 7, fontSize: 12 }}>
                  <Toggle on={cur} label="make current" disabled={r.new_asset || r.published} onChange={(v) => {
                    const n = new Set(keepCurrent); if (v) n.delete(r.item_id); else n.add(r.item_id); setKeepCurrent(n); }} />
                  {r.new_asset ? "current (first)" : cur ? "make current" : "keep existing"}</label>
              </div>
            );
          })}
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))", gap: 12 }}>
        <div className="panel" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="label">Written to storage</span>
          <div className="row" style={{ justifyContent: "space-between", fontSize: 12 }}><span className="muted">Versions</span>
            <span className="mono">{todo.length} immutable records</span></div>
          <div className="row" style={{ justifyContent: "space-between", fontSize: 12 }}><span className="muted">Manifests</span>
            <span className="mono">{todo.length} conditional updates</span></div>
          <div className="row" style={{ justifyContent: "space-between", fontSize: 12 }}><span className="muted">Blobs</span>
            <span className="mono">already stored at build (content-addressed)</span></div>
        </div>
        <div className="panel" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="label">Export on publish</span>
          <span className="muted" style={{ fontSize: 12 }}>Export targets (files, Godot, Git) arrive in Phase 4. Publication
            commits to the library only; exports will run as separate tracked operations.</span>
        </div>
      </div>
      {failed.map((i) => <div key={i.id} className="banner bad">{i.name}: {i.tasks.publish?.error}</div>)}
      <ErrorLine error={act.error ?? prev.error} />
      <ActionBar note={publishing.length ? `Publishing ${publishing.length}…` : `${todo.length} assets will be written as immutable versions.`}
        sub="Publish means commit to this project's library, not public release. Retrying never duplicates a version.">
        <button className="btn btn-primary" disabled={act.busy || !todo.length || publishing.length > 0}
          onClick={() => void act.run(async () => {
            await send("POST", `${P(id)}/batches/${batch.id}:publish`, { idempotency_key: key(), items: todo.map((r) => ({
              item_id: r.item_id, build_run_id: r.build_run_id, expected_item_revision: r.expected_item_revision,
              make_current: r.new_asset || !keepCurrent.has(r.item_id), expected_current_version: r.current_version_id })) });
            reload();
            prev.reload();
          })}>Publish {todo.length} versions</button>
      </ActionBar>
    </div>
  );
}
