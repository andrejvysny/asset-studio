import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { ErrorLine, Loading, PageHead } from "../components/ui";
import { type ProjectRow, send } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";

/** Entry: open the first project, or create an empty generic one (no catalog, style or categories preinstalled). */
export function Home() {
  const projects = useApi<{ projects: ProjectRow[]; project_roots: string[] }>("/api/v1/projects");
  const [name, setName] = useState("");
  const act = useAction();
  const nav = useNavigate();
  if (!projects.data) return projects.error ? <ErrorLine error={projects.error} /> : <Loading what="projects" />;
  const first = projects.data.projects[0];
  if (first) return <Navigate to={`/p/${first.id}/assets`} replace />;
  return (
    <div style={{ maxWidth: 560, margin: "12vh auto", padding: 24, display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHead sub="Asset Studio · no project yet" title="Create a project" />
      <p className="muted">A project is a portable folder: schema, shot list, manifests and content-addressed files.
        It starts empty — you define categories, style and QA rules yourself.</p>
      <label className="field"><span>Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Product samples" />
      </label>
      <div className="sub">Stored under {projects.data.project_roots[0] ?? "the configured project root"} (server path)</div>
      <div className="row">
        <button className="btn btn-primary" disabled={!name.trim() || act.busy} onClick={() => void act.run(async () => {
          const p = await send<{ id: string }>("POST", "/api/v1/projects", { name: name.trim() });
          nav(`/p/${p.id}/schema`);
        })}>Create empty project</button>
      </div>
      <ErrorLine error={act.error} />
    </div>
  );
}
