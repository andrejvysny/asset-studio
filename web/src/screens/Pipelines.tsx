import { useState } from "react";

import { ErrorLine, Loading, OK, PageHead, WARN, BAD } from "../components/ui";
import type { RecipeInfo } from "../lib/api";
import { useApi } from "../lib/hooks";
import { useConfig } from "../lib/project";

const stateColor = (s: string) => (s === "ready" ? OK : s === "experimental" || s === "degraded" ? WARN : BAD);

export function Pipelines() {
  const caps = useApi<{ recipes: RecipeInfo[]; simulated: boolean }>("/api/v1/capabilities", { pollMs: 30000 });
  const cfg = useConfig();
  const [sel, setSel] = useState<string | null>(null);
  if (!caps.data) return caps.error ? <ErrorLine error={caps.error} /> : <Loading what="pipelines" />;
  const r = caps.data.recipes.find((x) => x.id === sel) ?? caps.data.recipes[0]!;
  const overrides = cfg.data?.config.pipelines[r.id]?.parameters ?? {};
  return (
    <div className="split">
      <aside className="side-list">
        <div className="label" style={{ padding: "0 8px 8px" }}>Built-in pipelines</div>
        {caps.data.recipes.map((x) => (
          <button key={x.id} className={`side-row${x.id === r.id ? " on" : ""}`} onClick={() => setSel(x.id)}
            style={{ flexDirection: "column", alignItems: "flex-start", gap: 1 }}>
            <span>{x.label}</span><span className="sub" style={{ fontSize: 10.5, color: stateColor(x.generation.state) }}>{x.id} · {x.generation.state.replaceAll("_", " ")}</span>
          </button>
        ))}
      </aside>
      <section className="content" style={{ maxWidth: 1100 }}>
        <PageHead sub={`${r.id} v${r.version} · stages are fixed, parameters are typed`} title={r.label} />
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {(["generation", "build", "qa"] as const).map((k) => (
            <span key={k} className="panel" style={{ padding: "6px 10px", fontSize: 12 }}>
              <span className="sub">{k}</span> <span style={{ color: stateColor(r[k].state) }}>{r[k].state.replaceAll("_", " ")}</span>
              {r[k].reason && <span className="muted"> · {r[k].reason}</span>}</span>
          ))}
        </div>
        <div className="row" style={{ gap: 8, alignItems: "stretch", flexWrap: "wrap" }}>
          {r.stages.map((st, i) => {
            const gate = st.tag === "gate";
            return (
              <div key={i} className="panel" style={{ padding: "10px 12px", minWidth: 150, flex: 1, display: "flex", flexDirection: "column", gap: 3,
                borderColor: gate ? "oklch(0.8 0.12 80 / 0.4)" : undefined, background: gate ? "oklch(0.8 0.12 80 / 0.06)" : undefined }}>
                <span className="sub" style={{ textTransform: "uppercase", color: gate ? WARN : undefined }}>{gate ? "review gate" : st.tag}</span>
                <span style={{ fontWeight: 500 }}>{st.name}</span>
                <span className="sub">{st.backend}</span>
              </div>
            );
          })}
        </div>
        <div className="table">
          <div className="th" style={{ gridTemplateColumns: "200px 180px minmax(0,1fr)" }}><span>Parameter</span><span>Effective</span><span>Notes</span></div>
          {r.params.map((p) => (
            <div key={p.key} className="td" style={{ gridTemplateColumns: "200px 180px minmax(0,1fr)" }}>
              <span className="mono">{p.key}</span>
              <span className="mono">{String(overrides[p.key] ?? p.default)}{overrides[p.key] !== undefined ? " (project)" : ""}</span>
              <span className="muted" style={{ fontSize: 12 }}>{p.note}{p.choices.length ? ` · ${p.choices.join(" / ")}` : ""}
                {p.min !== null ? ` · ${p.min}–${p.max}` : ""}</span>
            </div>
          ))}
        </div>
        <div className="panel" style={{ padding: "10px 14px" }}>
          <div className="label">Locked technical constraints</div>
          <div className="mono" style={{ fontSize: 12, marginTop: 6 }}>{cfg.data?.config.pipelines[r.id]?.template ?? r.template ?? "—"}</div>
        </div>
        <div className="sub" style={{ fontFamily: "var(--sans)", fontSize: 12 }}>Parameter values are edited in studio.yaml
          (Schema → YAML, <span className="mono">pipelines.{r.id}.parameters</span>) and validated against these types.
          An inline editor is planned; no executable graphs are ever accepted.</div>
      </section>
    </div>
  );
}
