import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";

import { OK, WARN, BAD, NONE } from "../components/ui";
import { P, type ProjectRow, type Runtime, type Summary } from "../lib/api";
import { useApi } from "../lib/hooks";
import { ProjectContext } from "../lib/project";

const NAV: ([string, string] | [string, string, (s: Summary) => string | number])[] = [
  ["h", "Library"], ["assets", "Assets", (s) => s.counts.assets], ["shots", "Shot list", (s) => s.counts.shots],
  ["h", "Production"],
  ["batches", "Batches", (s) => (s.waiting.batches ? `${s.waiting.batches} waiting` : s.counts.batches)],
  ["h", "Project"], ["schema", "Schema", (s) => s.counts.categories], ["pipelines", "Pipelines", (s) => s.counts.recipes],
  ["qa", "QA rules"], ["style", "Style"], ["storage", "Storage", (s) => s.storage.state], ["export", "Export"],
  ["runtime", "Runtime"],
];

function gpuChip(rt: Runtime | null, lane: string): { text: string; color: string } {
  const g = rt?.gpus.find((x) => x.lane === lane);
  if (!rt) return { text: "…", color: NONE };
  if (!g) return { text: "not found", color: BAD };
  const own = g.ownership;
  const running = rt.coordinator?.lanes[lane]?.running;
  if (own && own.state === "unknown" && own.last_error) return { text: "ownership unknown", color: BAD };
  if (running) return { text: `busy · ${g.util_pct}%`, color: WARN };
  return { text: `idle · ${Math.round(g.vram_used_mb / 1024)}/${Math.round(g.vram_total_mb / 1024)} GB`, color: OK };
}

export function Shell() {
  const { project = "" } = useParams();
  const nav = useNavigate();
  const summary = useApi<Summary>(`${P(project)}/summary`, { project, pollMs: 15000 });
  const projects = useApi<{ projects: ProjectRow[] }>("/api/v1/projects");
  const rt = useApi<Runtime>("/api/v1/runtime", { pollMs: 5000 });
  const s = summary.data;
  const g0 = gpuChip(rt.data, "gpu0");
  const g1 = gpuChip(rt.data, "gpu1");
  const storageColor = s?.storage.state === "read_only" ? WARN : OK;
  return (
    <ProjectContext.Provider value={{ id: project, summary }}>
      <div className="app">
        <header className="topbar">
          <div className="row" style={{ gap: 9 }}>
            <div className="logo" aria-hidden />
            <span style={{ fontWeight: 600 }}>Asset Studio</span>
            <span style={{ color: "#5a5c60" }}>/</span>
            <select className="input" aria-label="project" value={project} style={{ padding: "3px 6px" }}
              onChange={(e) => nav(`/p/${e.target.value}/assets`)}>
              {(projects.data?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            {s?.simulated && <span className="sim-badge" title="Engine outputs are simulated test data">SIMULATED ENGINE</span>}
          </div>
          <div className="grow" />
          <button className="chip-top" onClick={() => nav(`/p/${project}/storage`)}
            title={s ? `${s.storage.backend} · ${s.storage.root}` : ""}>
            <span className="dot" style={{ background: storageColor }} />
            {s ? `${s.storage.root.split("/").slice(-1)[0]} · ${s.storage.state === "read_only" ? "read-only" : "local"}` : "…"}
          </button>
          <button className="chip-top" style={{ background: "#202124", fontFamily: "var(--sans)", fontSize: 12 }}
            onClick={() => nav(`/p/${project}/batches`)}
            title={(s?.waiting.detail ?? []).map((d) => `${d.alias}: ${d.next_action}`).join("\n") || "nothing waiting"}>
            <span className="count-badge">{s?.waiting.batches ?? 0}</span>waiting on you
          </button>
          <button className="row" style={{ gap: 10, font: "500 11px var(--mono)", color: "var(--muted)" }}
            onClick={() => nav(`/p/${project}/runtime`)} aria-label="GPU status">
            <span className="row" style={{ gap: 5 }}><span className="dot" style={{ background: g0.color }} />GPU0 {g0.text}</span>
            <span className="row" style={{ gap: 5 }}><span className="dot" style={{ background: g1.color }} />GPU1 {g1.text}</span>
          </button>
        </header>
        <nav className="sidebar" aria-label="main">
          {NAV.map(([id, label, count], i) => id === "h" ? <div key={i} className="nav-head">{label}</div> : (
            <NavLink key={id} to={`/p/${project}/${id}`} className={({ isActive }) => `nav-item${isActive ? " on" : ""}`}>
              <span className="label-text">{label}</span>
              <span className="n">{s && count ? count(s) : ""}</span>
            </NavLink>
          ))}
          <div className="grow" />
          <div className="sub foot" style={{ padding: 10, lineHeight: 1.5, color: "var(--faint)" }}>
            {s?.simulated ? "Engine: SIMULATED (tests/demo)" : "Self-hosted · no cloud inference"}
          </div>
        </nav>
        <main className="main">
          {summary.error && <div className="banner bad" style={{ margin: 16 }}>{summary.error}</div>}
          <Outlet />
        </main>
      </div>
    </ProjectContext.Provider>
  );
}
