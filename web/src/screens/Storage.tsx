import { useEffect, useState } from "react";

import { bytes, ErrorLine, INFO, Loading, OK, PageHead, BAD, Toggle } from "../components/ui";
import { P, send, type StudioConfig } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { clone, useConfig, useProject } from "../lib/project";

interface StorageView { backend: { backend: string; root: string; read_only: boolean }; state: string;
  owner: Record<string, string>; layout: string; retention: Record<string, { keep: boolean; expire_after_days: number | null }>;
  stats: { assets: number; versions: number; blobs: number; blob_bytes: number; logical_bytes: number; staging_bytes?: number };
  s3: { available: boolean; reason: string }; gc: { available: boolean; reason: string } }

const RET: [string, string, string][] = [
  ["rejected_candidates", "Keep rejected candidates", "Needed to change an approval later. Approved inputs are always kept."],
  ["raw_intermediates", "Keep raw intermediates (masks, raw meshes)", "Allows re-export without new inference."],
  ["full_logs", "Keep full job logs", "Compact status/seed/parameter records are always kept."],
];

export function Storage() {
  const { id } = useProject();
  const v = useApi<StorageView>(`${P(id)}/storage`, { project: id });
  const cfg = useConfig();
  const [draft, setDraft] = useState<StudioConfig | null>(null);
  const [test, setTest] = useState<Record<string, unknown> | null>(null);
  const act = useAction();
  useEffect(() => { if (cfg.data && !draft) setDraft(clone(cfg.data.config)); }, [cfg.data, draft]);
  if (!v.data || !draft || !cfg.data) return <Loading what="storage" />;
  const s = v.data;
  const dirty = JSON.stringify(draft.retention) !== JSON.stringify(cfg.data.config.retention);
  const savings = s.stats.logical_bytes - s.stats.blob_bytes;
  return (
    <div className="content narrow" style={{ padding: "20px 26px 48px", gap: 20 }}>
      <PageHead sub="Where the library lives · same logical layout on every backend" title="Storage" />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))", gap: 10 }}>
        <div className="panel" style={{ padding: "12px 14px", borderColor: "var(--dim)", background: "#1c1d20" }}>
          <div className="row" style={{ justifyContent: "space-between" }}><span style={{ fontWeight: 600 }}>Local folder</span>
            <span className="sub" style={{ color: s.state === "read_only" ? BAD : OK }}>{s.state === "read_only" ? "read-only" : "active"}</span></div>
          <span className="muted" style={{ fontSize: 12 }}>Server path <span className="mono">{s.backend.root}</span> (on the Studio host, not your browser machine).</span>
        </div>
        <div className="panel" style={{ padding: "12px 14px", opacity: 0.6 }}>
          <div className="row" style={{ justifyContent: "space-between" }}><span style={{ fontWeight: 600 }}>S3-compatible</span>
            <span className="sub">unavailable</span></div>
          <span className="muted" style={{ fontSize: 12 }}>{s.s3.reason}</span>
        </div>
      </div>
      {s.state === "read_only" && <div className="banner bad">Another Studio instance owns this project ({s.owner.instance_id}). It is open read-only.</div>}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.1fr) minmax(0,1fr)", gap: 20, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="panel" style={{ padding: "10px 14px" }}>
            <div className="row"><button className="btn" disabled={act.busy} onClick={() => void act.run(async () => {
              setTest(await send(`POST`, `${P(id)}/storage:test`)); })}>Test connection</button>
              <span className="sub" style={{ color: test ? (test.ok ? OK : BAD) : undefined }}>
                {test ? (test.ok ? "✓ write · read · list · delete (sentinel only) OK" : String(test.error ?? "failed")) : "not tested"}</span></div>
          </div>
          <div className="panel">
            <div className="label" style={{ padding: "10px 14px", borderBottom: "1px solid #222326" }}>Retention · safe default: keep everything</div>
            {RET.map(([k, label, sub]) => {
              const r = draft.retention[k as keyof StudioConfig["retention"]];
              return (
                <div key={k} className="row tr" style={{ padding: "9px 14px", gap: 12 }}>
                  <Toggle on={r.keep} label={label} onChange={(on) => { const n = clone(draft); n.retention[k as keyof StudioConfig["retention"]].keep = on; setDraft(n); }} />
                  <div className="grow" style={{ display: "flex", flexDirection: "column" }}><span>{label}</span>
                    <span className="dim" style={{ fontSize: 11.5 }}>{sub} Automatic expiry and deletion arrive with GC (Phase 4); nothing is deleted today.</span></div>
                </div>
              );
            })}
            {dirty && <div className="row tr" style={{ padding: "9px 14px" }}><button className="btn btn-primary" onClick={() => void act.run(async () => {
              await cfg.save(draft); setDraft(null); })}>Save retention policy</button></div>}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="panel"><div className="label" style={{ padding: "10px 14px", borderBottom: "1px solid #222326" }}>Layout</div>
            <pre className="pre">{s.layout}</pre></div>
          <div className="kv" style={{ gridTemplateColumns: "1fr 1fr" }}>
            {[["Assets", String(s.stats.assets)], ["Versions", String(s.stats.versions)],
              ["Blobs", `${s.stats.blobs} · ${bytes(s.stats.blob_bytes)}`], ["Saved by dedup (measured)", savings > 0 ? bytes(savings) : "0 B"]].map(([k, val]) => (
              <div key={k}><span className="dim" style={{ fontSize: 11 }}>{k}</span><span className="mono" style={{ fontSize: 13 }}>{val}</span></div>))}
          </div>
          <div className="row">
            <button className="btn" disabled={act.busy} onClick={() => void act.run(async () => {
              const r = await send<{ indexed: number; errors: number }>("POST", `${P(id)}/storage:rebuild-index`);
              setTest({ ok: r.errors === 0, error: `${r.errors} unreadable manifests`, rebuilt: r.indexed }); v.reload(); })}>
              Rebuild index from manifests</button>
            <button className="btn" disabled title={s.gc.reason}>Garbage-collect orphan blobs</button>
          </div>
          {test && "rebuilt" in test && <span className="sub" style={{ color: INFO }}>index rebuilt: {String(test.rebuilt)} assets</span>}
        </div>
      </div>
      <ErrorLine error={act.error ?? v.error} />
    </div>
  );
}
