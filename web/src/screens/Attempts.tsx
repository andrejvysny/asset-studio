import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorLine, ModelViewer, PageHead, relTime, stateColor } from "../components/ui";
import { api, type Attempt, attemptFile, type JobDetail } from "../lib/api";
import { queueWorkflow } from "../lib/comfy";
import { useAction, useApi } from "../lib/hooks";
import type { RuntimeInfo } from "./Runtime";

function note(a: Attempt): string {
  if (a.error) return `${a.failed_stage ? `[${a.failed_stage}] ` : ""}${a.error}`;
  const c = a.params?.cleanup ?? a.cleanup;
  const clean = c ? `cleanup: ${[c.remesh && "remesh", c.drop_floaters && "floaters"].filter(Boolean).join(" + ") || "conservative"}` : "";
  const qa = `#${String(a.index ?? "").padStart(2, "0")} · ${a.qa_status ?? "no QA"}${a.qa_override ? " · QA override" : ""}`;
  return [a.raw_from ? `re-export of ${a.raw_from} raw, no resample` : qa, clean]
    .filter(Boolean).join(" · ");
}

function formatBytes(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;
}

function Stats({ a }: { a: Attempt }) {
  const t = a.triangles;
  const rows: [string, string, string?][] = [
    ["triangles", t ? `${t.actual.toLocaleString()} / req ${t.requested ?? "—"}` : "—", t && t.reason === "triangle_floor" ? "var(--warn)" : undefined],
    ["decimation", t ? `${t.decimation_target.toLocaleString()} (${t.reason})` : "—"],
    ["components", a.mesh ? `${a.mesh.components}${a.mesh.removed_floater_components ? ` (−${a.mesh.removed_floater_components})` : ""}` : "—"],
    ["UVs", a.mesh ? (a.mesh.has_uv ? "yes" : "missing") : "—"],
    ["texture", a.mesh ? (a.mesh.has_base_color_texture
      ? (a.params?.texture_size ? `${a.params.texture_size}² · embedded` : "embedded") : "missing") : "—"],
    ["file", a.mesh ? formatBytes(a.mesh.file_size_bytes) : "—"],
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8 }}>
      {rows.map(([k, v, c]) => (
        <div key={k} style={{ border: "1px solid var(--line)", borderRadius: 6, padding: "6px 9px" }}>
          <div className="dim" style={{ fontSize: 11 }}>{k}</div><div className="mono" style={{ fontSize: 12, color: c }}>{v}</div>
        </div>
      ))}
    </div>
  );
}

function AttemptView({ job, a }: { job: string; a: Attempt }) {
  const glb = a.outputs?.glb;
  return (
    <div className="panel" style={{ padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row sub"><span className="grow">{a.id} · model/attempts/{a.id}/{glb ?? "processed/model.glb"}</span><span>orbit · auto-rotate</span></div>
      {glb && a.state === "completed"
        ? <ModelViewer src={attemptFile(job, a.id, glb)} height={360} />
        : <div className="stripes" style={{ height: 360, borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <span className="sub">{a.state === "completed" ? "no GLB" : `no model: ${a.state}`}</span></div>}
      <Stats a={a} />
      <div className="panel">
        <div className="label panel-head">Export validation</div>
        {(a.validation?.checks ?? []).length === 0 && <div className="dim tr" style={{ padding: "7px 10px", fontSize: 12 }}>not validated</div>}
        {(a.validation?.checks ?? []).map((c) => (
          <div key={c.id} className="row tr" style={{ padding: "6px 10px", fontSize: 12, gap: 8 }}>
            <span className="dot" style={{ background: c.ok ? "var(--ok)" : "var(--bad)" }} />
            <span className="grow">{c.id.replace(/_/g, " ")}</span><span className="dim">{c.detail}</span>
            <span className="mono" style={{ color: c.ok ? "var(--ok)" : "var(--bad)" }}>{c.ok ? "pass" : "fail"}</span>
          </div>
        ))}
      </div>
      {(a.known_limitations ?? []).map((l) => <div key={l} className="dim" style={{ fontSize: 11.5 }}>limitation: {l}</div>)}
    </div>
  );
}

export function Attempts() {
  const { jobId = "" } = useParams();
  const job = useApi<JobDetail>(`/api/jobs/${jobId}`, 4000);
  const runtime = useApi<RuntimeInfo>("/api/runtime");
  const [sel, setSel] = useState<string | null>(null);
  const [compare, setCompare] = useState(false);
  const [cleanup, setCleanup] = useState({ remesh: false, drop_floaters: false });
  const [slot, setSlot] = useState("");
  const action = useAction();
  const j = job.data;
  const attempts = j?.attempts ?? [];
  useEffect(() => { if (!sel && j?.current_attempt) setSel(j.current_attempt); }, [j, sel]);
  const a = attempts.find((x) => x.id === sel) ?? attempts.at(-1);
  const prev = [...attempts].reverse().find((x) => x.id !== a?.id && x.state === "completed");
  const rawSource = a && (a.outputs?.raw ? a : attempts.find((x) => x.id === a.raw_from));
  const blocked = (runtime.data?.licences ?? []).filter((l) => l.status === "not_cleared");

  const reexport = () => action.run(async () => {
    if (!rawSource) return;
    await queueWorkflow("line_a_reexport", { reexport: { job_id: jobId, from_attempt: rawSource.id, ...cleanup } });
    setSel(null);
  });
  const assign = () => action.run(async () => {
    if (!a) return;
    await api(`/api/slots/${slot.trim()}/assignment`, { method: "PUT", body: JSON.stringify({ job_id: jobId, attempt_id: a.id }) });
    job.reload();
  });

  return (
    <div style={{ padding: "18px 24px 40px", display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHead sub={`${jobId} · ${j?.current_attempt ? `current ${j.current_attempt}` : "no attempt"} · set ${j?.candidate_set?.set_id ?? "—"}`}
        title={`${j?.enhancement?.short_title || j?.request.prompt || "…"} — 3D attempts`}>
        <button className="btn" onClick={() => setCompare(!compare)} disabled={!prev}
          style={{ background: compare ? "#2a2b2e" : undefined, fontSize: 12 }}>{compare ? "Single view" : "Compare with previous"}</button>
      </PageHead>
      {blocked.length > 0 && (
        <div className="banner bad"><span className="dot" style={{ background: "var(--bad)", marginTop: 6 }} />
          <span>Exported with the current trellis-worker runtime, which uses {blocked.map((b) => b.name).join(", ")} ({blocked[0]?.licence}) for
            texture baking. Fine for evaluation. Don't ship it until a cleared exporter is in place.</span></div>
      )}
      <ErrorLine error={job.error ?? action.error} />
      {j && attempts.length === 0 && <div className="dim">No 3D attempts yet. <Link to={`/review/${jobId}`}>Review candidates</Link> and approve one.</div>}
      {j && a && (
        <div style={{ display: "grid", gridTemplateColumns: "220px minmax(0,1fr)", gap: 16 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="label" style={{ padding: "0 2px 4px" }}>Attempts</span>
            {[...attempts].reverse().map((x) => (
              <div key={x.id} className="clickable" onClick={() => setSel(x.id)} style={{ border: `1px solid ${x.id === a.id ? "#8b8c87" : "var(--line)"}`,
                background: x.id === a.id ? "#1c1d20" : "transparent", borderRadius: 7, padding: "9px 10px", display: "flex", flexDirection: "column", gap: 3 }}>
                <div className="row" style={{ justifyContent: "space-between" }}><span className="mono" style={{ fontWeight: 600, fontSize: 12 }}>{x.id}</span>
                  <span className="mono" style={{ fontSize: 10.5, color: stateColor(x.state) }}>{x.state}</span></div>
                <span className="muted" style={{ fontSize: 11.5, wordBreak: "break-word" }}>{note(x).slice(0, 180)}</span>
                <span className="sub">{relTime(x.updated_at ?? x.created_at)}{j.slots[x.id] ? ` · ${j.slots[x.id]}` : ""}</span>
              </div>
            ))}
            <img className="thumb" alt="approved candidate" style={{ borderRadius: 6, marginTop: 8 }} src={attemptFile(jobId, a.id, "selected.png")} />
            <span className="sub">approved image · #{String(a.index ?? "").padStart(2, "0")}
              {a.image_sha256 ? ` · sha ${a.image_sha256.slice(0, 8)}` : ""}{a.qa_override ? " · QA override" : ""}</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: compare && prev ? "1fr 1fr" : "1fr", gap: 12 }}>
            {compare && prev && <AttemptView job={jobId} a={prev} />}
            <AttemptView job={jobId} a={a} />
          </div>
        </div>
      )}
      {j && a && (
        <div className="panel row" style={{ padding: "12px 14px", gap: 16, flexWrap: "wrap" }}>
          <div className="grow" style={{ minWidth: 260 }}>
            <div style={{ fontWeight: 500 }}>Re-export from raw intermediate</div>
            <div className="dim" style={{ fontSize: 12 }}>{rawSource ? `Uses the raw TRELLIS output of ${rawSource.id}. Creates a new attempt without resampling.`
              : "This attempt has no raw intermediate (TRELLIS did not finish)."}</div>
          </div>
          {([["remesh", "Remesh"], ["drop_floaters", "Floater removal"]] as const).map(([k, label]) => (
            <label key={k} className="row" style={{ gap: 6, fontSize: 12 }}>
              <input type="checkbox" checked={cleanup[k]} onChange={(e) => setCleanup({ ...cleanup, [k]: e.target.checked })} />{label}</label>
          ))}
          <span className="dim" style={{ fontSize: 12 }} title="upstream o_voxel.to_glb, not switchable">Hole fill: always on (upstream)</span>
          <button className="btn btn-primary" disabled={!rawSource || action.busy || !!j.active_operation} onClick={reexport}>Re-export → new attempt</button>
        </div>
      )}
      {j && a?.state === "completed" && a.validated && (
        <div className="panel row" style={{ padding: "12px 14px", gap: 12, flexWrap: "wrap" }}>
          <div className="grow"><div style={{ fontWeight: 500 }}>Assign to slot</div>
            <div className="dim" style={{ fontSize: 12 }}>{j.slots[a.id] ? `Currently in ${j.slots[a.id]}; assigning elsewhere moves it.` : "Enter a library slot id, e.g. forest_prop_containers_storage_a"}</div></div>
          <input className="input" style={{ minWidth: 320 }} placeholder="slot id" value={slot} onChange={(e) => setSlot(e.target.value)} />
          <button className="btn" disabled={!slot.trim() || action.busy} onClick={assign}>Assign</button>
        </div>
      )}
    </div>
  );
}
