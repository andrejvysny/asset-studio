import { Bar, BAD, bytes, ErrorLine, INFO, Loading, NONE, OK, PageHead, relTime, WARN } from "../components/ui";
import { type Runtime as RT, send } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";

const modelColor = (s: string) => (s === "ok" ? OK : s === "unverified_hash" || s === "pending_access" ? WARN : BAD);
const licColor = (s: string) => (s === "cleared" ? OK : s === "not_cleared" ? BAD : WARN);

export function Runtime() {
  const rt = useApi<RT>("/api/v1/runtime", { pollMs: 5000 });
  const act = useAction();
  if (!rt.data) return rt.error ? <ErrorLine error={rt.error} /> : <Loading what="runtime" />;
  const d = rt.data;
  return (
    <div className="content narrow" style={{ padding: "20px 26px 48px", gap: 20 }}>
      <PageHead sub={`Local backends · no cloud inference · engine: ${d.engine_mode}${d.simulated ? " (SIMULATED)" : ""}`} title="Runtime" />
      {d.simulated && <div className="banner bad">The image engine and aux service are SIMULATED. Outputs are test data, never production evidence.</div>}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(340px,1fr))", gap: 12 }}>
        {d.gpus.length === 0 && <div className="empty">No GPUs visible to the Studio host (nvidia-smi unavailable). Library-only mode still works.</div>}
        {d.gpus.map((g) => {
          const own = g.ownership;
          const running = g.lane ? d.coordinator?.lanes[g.lane]?.running : null;
          const state = own?.state === "unknown" && own.last_error ? "ownership unknown" : running ? "busy" : own?.state === "owned" ? `owned by ${own.owner}` : "idle";
          const color = state === "ownership unknown" ? BAD : running ? WARN : OK;
          return (
            <div key={g.uuid} className="panel" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
              <div className="row" style={{ justifyContent: "space-between" }}><span style={{ fontWeight: 600 }}>GPU{g.index} · {g.name}</span>
                <span className="sub" style={{ color }}>{state}</span></div>
              <Bar pct={(g.vram_used_mb / g.vram_total_mb) * 100} color={color} />
              <span className="sub">{bytes(g.vram_used_mb * 2 ** 20)} / {bytes(g.vram_total_mb * 2 ** 20)} · util {g.util_pct}% · lane {g.lane ?? "—"}
                {g.lane && d.coordinator ? ` · queued ${d.coordinator.lanes[g.lane]?.queued ?? 0}` : ""} · measured {relTime(g.measured_at)}</span>
              {own?.last_error && <><span className="error">{own.last_error}</span>
                <button className="btn" disabled={act.busy} onClick={() => void act.run(async () => { await send("POST", `/api/v1/runtime/lanes/${g.lane}:reset`); rt.reload(); })}>
                  Verify release of all {g.lane} workers</button></>}
            </div>
          );
        })}
      </div>
      <div>
        <div className="label" style={{ marginBottom: 8 }}>Recent model passes (stage work grouped by model residency across Jobs)</div>
        {!d.coordinator?.passes.length ? <div className="empty">No passes yet.</div> : (
          <div className="table">
            {d.coordinator.passes.map((p) => <div key={p.id} className="td" style={{ gridTemplateColumns: "60px minmax(220px,1.6fr) 60px 60px minmax(160px,1fr) 150px 90px", minWidth: 860 }}>
              <span className="mono" style={{ fontSize: 11.5 }}>{p.lane}</span>
              <span className="mono ellipsis" style={{ fontSize: 11 }} title={p.residency}>{p.residency}</span>
              <span className="sub">{p.task_ids.length} tasks</span><span className="sub">{p.jobs.length} Jobs</span>
              <span className="sub" title="measured worker load counters; ComfyUI exposes none (shown as unavailable)">
                loads: {p.measured.model_loads ? Object.entries(p.measured.model_loads).map(([k, v]) => `${k} +${v}`).join(" · ") : "unavailable"}</span>
              <span className="sub" style={{ color: p.close_reason?.startsWith("resource") ? BAD : undefined }}>{p.close_reason ?? "running"}{p.measured.switched ? " · switched" : ""}</span>
              <span className="sub">{relTime(p.started_at)}</span></div>)}
          </div>)}
      </div>
      <div>
        <div className="label" style={{ marginBottom: 8 }}>Services</div>
        <div className="table">
          {d.services.map((s) => <div key={s.name} className="td" style={{ gridTemplateColumns: "140px minmax(200px,1fr) 110px minmax(160px,1fr)", minWidth: 700 }}>
            <span style={{ fontWeight: 500 }}>{s.name}</span><span className="mono dim" style={{ fontSize: 11.5 }}>{s.url}{s.version ? ` · ${s.version}` : ""}</span>
            <span className="sub" style={{ color: d.simulated ? WARN : s.ready ? OK : s.reachable ? WARN : BAD }}>
              {d.simulated ? "simulated" : s.ready ? "ready" : s.reachable ? "reachable, not ready" : "unreachable"}</span>
            <span className="muted" style={{ fontSize: 12 }}>{s.role}{s.problems.length ? ` · ${s.problems.join("; ")}` : ""}</span></div>)}
        </div>
      </div>
      <div>
        <div className="label" style={{ marginBottom: 8 }}>Recipes · readiness</div>
        <div className="table">
          {d.recipes.map((r) => <div key={r.id} className="td" style={{ gridTemplateColumns: "140px repeat(3,minmax(0,1fr))", minWidth: 760 }}>
            <span>{r.label}</span>
            {(["generation", "build", "qa"] as const).map((k) => <span key={k} className="sub" title={r[k].reason}
              style={{ color: r[k].state === "ready" ? OK : r[k].state === "experimental" || r[k].state === "degraded" ? WARN : NONE }}>
              {k}: {r[k].state.replaceAll("_", " ")}</span>)}</div>)}
        </div>
      </div>
      <div>
        <div className="label" style={{ marginBottom: 8 }}>Models · pinned closure (config/models.lock.yaml)</div>
        <div className="table">
          {d.models.map((m) => <div key={m.key} className="td" style={{ gridTemplateColumns: "150px minmax(200px,1fr) 150px 120px minmax(140px,1fr)", minWidth: 800 }}>
            <span className="muted" style={{ fontSize: 12 }}>{m.roles.join(", ")}</span>
            <span className="mono" style={{ fontSize: 12 }}>{m.repo}@{m.revision.slice(0, 8)}</span>
            <span className="sub" style={{ color: licColor(m.licence_status) }}>{m.licence} · {m.licence_status}</span>
            <span className={`pill ${m.status === "ok" ? "ok" : m.status === "pending_access" || m.status === "unverified_hash" ? "warn" : "bad"}`}
              style={{ justifySelf: "start", color: modelColor(m.status) }}>{m.status.replaceAll("_", " ")}</span>
            <span className="dim" style={{ fontSize: 12 }}>{m.detail}{m.full_verified ? "" : " · run `make verify-full` for sha256"}</span></div>)}
        </div>
      </div>
      <div>
        <div className="label" style={{ marginBottom: 8 }}>Licence inventory (captured into every version at creation)</div>
        <div className="table">
          {d.licences.map((l) => <div key={l.id} className="td" style={{ gridTemplateColumns: "minmax(200px,1fr) 200px 110px minmax(160px,1.4fr)", minWidth: 760 }}>
            <span>{l.name}</span><span className="mono dim" style={{ fontSize: 11.5 }}>{l.licence}</span>
            <span className="sub" style={{ color: licColor(l.status) }}>{l.status.replace("_", " ")}</span>
            <span className="muted" style={{ fontSize: 12 }}>{l.note ?? ""}</span></div>)}
        </div>
      </div>
      <span className="sub" style={{ color: INFO }}>Reachable ≠ ready ≠ integrity verified ≠ licence cleared; each is shown separately.</span>
      <ErrorLine error={act.error} />
    </div>
  );
}
