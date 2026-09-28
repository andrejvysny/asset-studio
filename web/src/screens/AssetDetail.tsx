import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ErrorLine, ModelViewer, relTime } from "../components/ui";
import { api, type AssetItem, attemptFile, fileUrl, type SlotDetail } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";

export function AssetDetail() {
  const { slotId = "" } = useParams();
  const navigate = useNavigate();
  const slot = useApi<SlotDetail>(`/api/slots/${slotId}`);
  const unassigned = useApi<AssetItem[]>("/api/assets?unassigned=1");
  const action = useAction();
  const [copied, setCopied] = useState(false);
  const s = slot.data;
  const a = s?.assignment;

  const assign = (item: AssetItem) => action.run(async () => {
    await api(`/api/slots/${slotId}/assignment`, { method: "PUT",
      body: JSON.stringify({ job_id: item.job_id, attempt_id: item.attempt_id }) });
    slot.reload(); unassigned.reload();
  });
  const unassign = () => action.run(async () => {
    await api(`/api/slots/${slotId}/assignment`, { method: "DELETE" });
    slot.reload(); unassigned.reload();
  });
  const manifestJson = JSON.stringify(s?.manifest ?? { slot: slotId, assignment: null }, null, 2);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 420px", minHeight: "100%" }}>
      <div style={{ padding: "20px 24px 40px", display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <ErrorLine error={slot.error ?? action.error} />
        {s && <>
          <div>
            <Link to={`/library/${s.biome}`} className="sub" style={{ textDecoration: "none" }}>
              ← Library / {s.biome_name} / {s.layer + 1} · {s.layer_name} / {s.family}</Link>
            <div className="h1 mono" style={{ fontSize: 18 }}>{s.id}</div>
            <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
              <span className={`pill ${a ? "ok" : "none"}`}>{a ? "assigned" : "planned"}</span>
              {[s.biome, s.role, "1 m grid", "pivot: base centre"].map((t) => <span key={t} className="pill none">{t}</span>)}
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(180px,1fr))", gap: 10 }}>
            {Object.entries(s.facts).map(([k, v]) => (
              <div key={k} style={{ border: "1px solid var(--line)", borderRadius: 7, padding: "9px 11px" }}>
                <div className="dim" style={{ fontSize: 11 }}>{k.replace(/_/g, " ")}</div>
                <div className="mono" style={{ fontSize: 12 }}>{v}</div>
              </div>
            ))}
          </div>
          <div className="muted" style={{ fontSize: 12.5 }}><span className="dim">Family spec · </span>{s.variants}</div>

          {a ? (
            <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 240px", gap: 16 }}>
              <div>
                <ModelViewer src={fileUrl(a.job_id, a.glb)} height={380} />
                <div className="sub" style={{ marginTop: 6 }}>live view of the delivered GLB</div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <span className="label">Source</span>
                <div className="clickable" onClick={() => navigate(`/attempts/${a.job_id}`)}
                  style={{ border: "1px solid var(--line)", borderRadius: 7, padding: 10 }}>
                  <img className="thumb" alt="" style={{ borderRadius: 5, marginBottom: 8 }}
                    src={attemptFile(a.job_id, a.attempt_id, "selected.png")} />
                  <div style={{ fontWeight: 500 }}>{a.attempt_id} · {a.triangles ?? "?"} tris</div>
                  <div className="sub ellipsis">{a.job_id}</div>
                </div>
                <div className="dim" style={{ fontSize: 12 }}>Assigned {relTime(a.assigned_at)}. Unassigning keeps the job and its attempts.</div>
                <button className="btn" disabled={action.busy} onClick={unassign}>Unassign</button>
              </div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <span className="label">Unassigned approved results</span>
              {(unassigned.data ?? []).length === 0 && <div className="dim">No completed, validated 3D attempts are waiting for a slot.</div>}
              {(unassigned.data ?? []).map((u) => (
                <div key={`${u.job_id}/${u.attempt_id}`} className="row" style={{ border: "1px solid var(--line)",
                  borderRadius: 7, padding: 8 }}>
                  <img alt="" src={attemptFile(u.job_id, u.attempt_id, "selected.png")}
                    style={{ width: 48, height: 48, objectFit: "contain", borderRadius: 4, background: "#1b1c1e" }} />
                  <div className="grow"><div>{u.title} · {u.attempt_id}</div>
                    <div className="sub ellipsis">{u.job_id} · {u.triangles ?? "?"} tris</div></div>
                  <button className="btn" disabled={action.busy} onClick={() => assign(u)}>Assign here</button>
                </div>
              ))}
              <button className="btn-link" style={{ alignSelf: "flex-start" }}
                onClick={() => navigate(`/new?brief=${encodeURIComponent(s.family.toLowerCase())}&slot=${s.id}`)}>
                Or start a new job for this slot →</button>
            </div>
          )}
        </>}
      </div>

      <div style={{ borderLeft: "1px solid var(--line)", background: "var(--panel)", padding: 16, display: "flex",
        flexDirection: "column", gap: 10, minWidth: 0 }}>
        <div className="row">
          <span className="sub grow ellipsis">{a ? `output/${a.job_id}/manifest.json` : "manifest (unassigned)"}</span>
          <button className="btn" style={{ padding: "3px 10px", fontSize: 12 }}
            onClick={() => { void navigator.clipboard?.writeText(manifestJson); setCopied(true); }}>
            {copied ? "Copied" : "Copy"}</button>
        </div>
        <pre className="mono" style={{ margin: 0, fontSize: 11, lineHeight: 1.5, color: "var(--text-2)", overflow: "auto",
          whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{manifestJson}</pre>
      </div>
    </div>
  );
}
