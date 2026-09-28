import { NavLink, Outlet, useNavigate } from "react-router-dom";

import type { JobSummary } from "../lib/api";
import { useApi } from "../lib/hooks";
import type { RuntimeInfo } from "./Runtime";

function Nav({ to, label, count }: { to: string; label: string; count?: string | number }) {
  return (
    <NavLink to={to} className="clickable" style={({ isActive }) => ({
      display: "flex", alignItems: "center", gap: 8, padding: "7px 10px", borderRadius: 5, textDecoration: "none",
      background: isActive ? "var(--active-2)" : "transparent", color: isActive ? "#fff" : "var(--text-2)",
    })}>
      <span style={{ flex: 1 }}>{label}</span>
      <span className="sub">{count ?? ""}</span>
    </NavLink>
  );
}

function GpuDot({ label, busy, up }: { label: string; busy: boolean; up: boolean }) {
  const color = !up ? "var(--bad)" : busy ? "var(--info)" : "var(--ok)";
  return <span className="row" style={{ gap: 5 }}><span className="dot" style={{ background: color }} />
    {label} {!up ? "down" : busy ? "busy" : "idle"}</span>;
}

export function Shell() {
  const navigate = useNavigate();
  const jobs = useApi<JobSummary[]>("/api/jobs", 5000);
  const runtime = useApi<RuntimeInfo>("/api/runtime", 10000);
  const waiting = (jobs.data ?? []).filter((j) => j.waiting);
  const reviewable = waiting.filter((j) => j.state === "waiting_for_selection").length;
  const notCleared = (runtime.data?.licences ?? []).filter((l) => l.status === "not_cleared");
  const rt = runtime.data;
  const gpu1Loaded = rt ? Object.values(rt.workers).some((w) => w.loaded || w.trellis_loaded || w.birefnet_loaded) : false;

  return (
    <div style={{ height: "100vh", display: "grid", gridTemplateRows: "44px minmax(0,1fr)",
      gridTemplateColumns: "196px minmax(0,1fr)" }}>
      <div style={{ gridColumn: "1 / 3", display: "flex", alignItems: "center", gap: 16, padding: "0 14px",
        borderBottom: "1px solid var(--line)", background: "var(--panel)" }}>
        <div className="row" style={{ gap: 9 }}>
          <div style={{ width: 14, height: 14, border: "1.5px solid var(--text)", transform: "rotate(45deg)" }} />
          <span style={{ fontWeight: 600, letterSpacing: "-.01em" }}>Asset Studio</span>
          <span className="sub">line_a · ComfyUI v1 API</span>
        </div>
        <div style={{ flex: 1 }} />
        {notCleared.length > 0 && (
          <button className="row btn-link" onClick={() => navigate("/runtime")} style={{ padding: "4px 10px",
            borderRadius: 5, background: "var(--bad-bg)", color: "oklch(0.8 0.12 25)", fontSize: 12 }}>
            <span className="dot" style={{ background: "var(--bad)" }} />Export runtime not cleared for commercial use
          </button>
        )}
        <button className="row btn-link" onClick={() => navigate("/jobs")} style={{ padding: "4px 10px",
          borderRadius: 5, background: "var(--active)", fontSize: 12, color: "var(--text)" }}>
          <span className="mono" style={{ fontWeight: 600, fontSize: 11, background: "var(--text)", color: "var(--bg)",
            borderRadius: 3, padding: "0 5px" }}>{waiting.length}</span>waiting on you
        </button>
        <div className="row sub" style={{ gap: 10, color: "var(--muted)" }}>
          <GpuDot label="GPU0" up={!!rt?.comfyui.reachable} busy={(rt?.queue.running ?? 0) > 0} />
          <GpuDot label="GPU1" up={!!rt && Object.values(rt.workers).some((w) => w.reachable)} busy={gpu1Loaded} />
        </div>
      </div>

      <nav style={{ borderRight: "1px solid var(--line)", background: "var(--panel)", padding: "12px 8px",
        display: "flex", flexDirection: "column", gap: 2, overflow: "auto" }}>
        <div className="label" style={{ padding: "4px 10px 6px" }}>Library</div>
        <Nav to="/library" label="Browse" />
        <Nav to="/coverage" label="Coverage" />
        <div className="label" style={{ padding: "14px 10px 6px" }}>Production</div>
        <Nav to="/jobs" label="Jobs" count={jobs.data?.length} />
        <Nav to="/new" label="New job" />
        <Nav to="/review" label="Review" count={reviewable || ""} />
        <div className="label" style={{ padding: "14px 10px 6px" }}>System</div>
        <Nav to="/runtime" label="Runtime" count={notCleared.length ? "!" : ""} />
        <div style={{ flex: 1 }} />
        <div className="sub" style={{ padding: 10, lineHeight: 1.5 }}>3D line · catalog from Asset Library Kit</div>
      </nav>

      <main style={{ overflow: "auto", minWidth: 0 }}>
        <Outlet />
      </main>
    </div>
  );
}
