import { useNavigate } from "react-router-dom";

import { ErrorLine } from "../components/ui";
import type { BiomeSummary } from "../lib/api";
import { useApi } from "../lib/hooks";

interface CoverageData {
  layers: string[];
  rows: (BiomeSummary & { cells: { base: number; assigned: number }[] })[];
}

export function Coverage() {
  const navigate = useNavigate();
  const cov = useApi<CoverageData>("/api/coverage");
  const cols = `200px repeat(${cov.data?.layers.length ?? 7},minmax(84px,1fr)) 110px 70px`;
  const head = { padding: "10px 8px", background: "var(--card)" };

  return (
    <div className="page">
      <div className="sub">Production planner</div>
      <div className="h1" style={{ margin: "2px 0 4px" }}>Catalog coverage</div>
      <div className="muted" style={{ maxWidth: 720, marginBottom: 20 }}>
        Base meshes per biome and layer, from the kit catalog. The right-hand total is the catalog estimate including
        variants and states. Cells fill as validated 3D attempts are assigned to slots.
      </div>
      <ErrorLine error={cov.error} />
      {cov.data && (
        <div className="panel" style={{ overflow: "auto" }}>
          <div style={{ display: "grid", gridTemplateColumns: cols, minWidth: 1040 }}>
            <div className="label" style={{ ...head, padding: "10px 14px" }}>Biome</div>
            {cov.data.layers.map((l) => <div key={l} className="label" style={head}>{l}</div>)}
            <div className="label" style={head}>Catalogued</div>
            <div className="label" style={head}>Order</div>
            {[...cov.data.rows].sort((a, b) => a.order - b.order).map((r) => [
              <div key={`${r.id}-n`} className="tr clickable" onClick={() => navigate(`/library/${r.id}`)}
                style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 2 }}>
                <span style={{ fontWeight: 500 }}>{r.name}</span>
                <span className="sub">{r.prefix} · {r.families} families</span>
              </div>,
              ...r.cells.map((c, i) => {
                const inten = Math.min(c.base / 140, 1);
                return (
                  <div key={`${r.id}-${i}`} className="tr" style={{ padding: 6 }}>
                    <div style={{ minHeight: 44, height: "100%", borderRadius: 5, padding: "6px 8px",
                      background: `oklch(${0.2 + inten * 0.1} 0.01 250)`, display: "flex", flexDirection: "column",
                      justifyContent: "space-between" }}>
                      <span className="mono" style={{ fontSize: 12 }}>{c.base}</span>
                      <span className="mono" style={{ fontSize: 10.5, color: c.assigned ? "var(--ok)" : "var(--faint)" }}>
                        {c.assigned ? `${c.assigned} done` : "—"}</span>
                    </div>
                  </div>
                );
              }),
              <div key={`${r.id}-c`} className="tr mono" style={{ padding: "12px 8px", fontSize: 12 }}>≈ {r.catalog_estimate}</div>,
              <div key={`${r.id}-o`} className="tr mono muted" style={{ padding: "12px 8px", fontSize: 12 }}>{r.order}</div>,
            ])}
          </div>
        </div>
      )}
      <div className="muted" style={{ marginTop: 14, fontSize: 12 }}>
        Order follows the kit's production sequence: 1 forest → 3 farmland + mountains → 4 desert + marsh → 5 elven + volcanic.
      </div>
    </div>
  );
}
