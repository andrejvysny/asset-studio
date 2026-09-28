import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ErrorLine, PageHead, Segmented } from "../components/ui";
import { attemptFile, type BiomeSummary, type BiomeTree, type Family, type SlotRef } from "../lib/api";
import { useApi } from "../lib/hooks";

const MAX_TILES = 8;

function SideItem({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <div onClick={onClick} className="clickable" style={{ padding: "7px 8px", borderRadius: 5,
      background: active ? "var(--active)" : "transparent", color: active ? "#fff" : "var(--text-2)" }}>
      {children}
    </div>
  );
}

function Bar({ pct, height = 2 }: { pct: number; height?: number }) {
  return (
    <div style={{ height, background: "var(--line)", borderRadius: 1, marginTop: 6 }}>
      <div style={{ height, borderRadius: 1, background: "var(--ok)", width: `${pct}%` }} />
    </div>
  );
}

function SlotTile({ slot }: { slot: SlotRef }) {
  const navigate = useNavigate();
  return (
    <div onClick={() => navigate(`/slots/${slot.id}`)} className="clickable" style={{ borderRadius: 6, overflow: "hidden",
      border: `1px solid ${slot.assigned ? "#34463a" : "var(--line)"}`, background: "var(--card)" }}>
      <div className="stripes" style={{ aspectRatio: "1", position: "relative", display: "flex", alignItems: "center",
        justifyContent: "center" }}>
        {slot.assigned && slot.job_id && slot.attempt_id
          ? <img className="thumb" alt="" src={attemptFile(slot.job_id, slot.attempt_id, "selected.png")} />
          : <span className="mono" style={{ fontSize: 9.5, color: "var(--faint)" }}>planned</span>}
        <span style={{ position: "absolute", top: 6, left: 6, width: 7, height: 7, borderRadius: 2,
          background: slot.assigned ? "var(--ok)" : "transparent", border: `1px solid ${slot.assigned ? "var(--ok)" : "#5a5c60"}` }} />
      </div>
      <div className="mono ellipsis" style={{ padding: "6px 7px", fontSize: 10, color: "var(--text-2)" }}>{slot.id}</div>
    </div>
  );
}

function FamilyGrid({ fam, layerName }: { fam: Family; layerName: string }) {
  const shown = fam.slots.slice(0, MAX_TILES);
  return (
    <div>
      <div className="row" style={{ alignItems: "baseline", marginBottom: 10, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600 }}>{fam.name}</span>
        <span className="sub">{layerName} · {fam.assigned}/{fam.count} assigned</span>
        <span className="grow" />
        <span className="dim" style={{ fontSize: 12, maxWidth: 520 }}>{fam.variants}</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(112px,1fr))", gap: 8 }}>
        {shown.map((s) => <SlotTile key={s.id} slot={s} />)}
        {fam.slots.length > shown.length && (
          <div className="sub" style={{ border: "1px dashed var(--line-2)", borderRadius: 6, display: "flex",
            alignItems: "center", justifyContent: "center", minHeight: 80 }}>+{fam.slots.length - shown.length} more</div>
        )}
      </div>
    </div>
  );
}

function FamilyTable({ tree }: { tree: BiomeTree }) {
  const navigate = useNavigate();
  const cols = "minmax(160px,1.1fr) 90px minmax(200px,2.4fr) 70px 140px";
  return (
    <div className="panel">
      <div className="label panel-head" style={{ display: "grid", gridTemplateColumns: cols, gap: 14 }}>
        <span>Family</span><span>Layer</span><span>Variants · states</span><span>Meshes</span><span>Assigned</span>
      </div>
      {tree.layers.flatMap((l) => l.families.map((f) => (
        <div key={f.id} className="tr clickable" onClick={() => f.slots[0] && navigate(`/slots/${f.slots[0].id}`)}
          style={{ display: "grid", gridTemplateColumns: cols, gap: 14, padding: "10px 14px", alignItems: "start" }}>
          <span style={{ fontWeight: 500 }}>{f.name}</span>
          <span className="sub" style={{ color: "var(--muted)" }}>{l.name}</span>
          <span className="muted" style={{ fontSize: 12 }}>{f.variants}</span>
          <span className="mono">{f.count}</span>
          <div style={{ paddingTop: 2 }}>
            <Bar pct={f.count ? (f.assigned / f.count) * 100 : 0} height={4} />
            <span className="sub">{f.assigned} / {f.count}</span>
          </div>
        </div>
      )))}
    </div>
  );
}

export function Library() {
  const navigate = useNavigate();
  const { biome = "forest" } = useParams();
  const [layer, setLayer] = useState<number | null>(null);
  const [view, setView] = useState<"grid" | "table">("grid");
  const summary = useApi<{ biomes: BiomeSummary[] }>("/api/catalog");
  const tree = useApi<BiomeTree>(`/api/catalog/${biome}${layer === null ? "" : `?layer=${layer}`}`);
  const current = summary.data?.biomes.find((b) => b.id === biome);
  const [params, setParams] = useSearchParams();
  const show = params.get("show") === "assigned" ? "assigned" : "all";
  const setShow = (v: "all" | "assigned") => setParams(v === "all" ? {} : { show: v }, { replace: true });
  // "Assigned only": hide planned slots and families with nothing assigned.
  const visible: BiomeTree | null = tree.data && show === "assigned"
    ? { ...tree.data, layers: tree.data.layers.map((l) => ({ ...l, families: l.families
        .map((f) => ({ ...f, slots: f.slots.filter((s) => s.assigned) })).filter((f) => f.slots.length > 0) })) }
    : tree.data;
  const visibleCount = (visible?.layers ?? []).reduce((n, l) => n + l.families.length, 0);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "230px minmax(0,1fr)", minHeight: "100%" }}>
      <div style={{ borderRight: "1px solid var(--line)", padding: "16px 10px", display: "flex", flexDirection: "column", gap: 2 }}>
        <div className="label" style={{ padding: "0 8px 8px" }}>Biome kits</div>
        {(summary.data?.biomes ?? []).map((b) => (
          <SideItem key={b.id} active={b.id === biome} onClick={() => { setLayer(null); navigate(`/library/${b.id}${show === "assigned" ? "?show=assigned" : ""}`); }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span>{b.name}</span><span className="sub">{b.assigned}/{b.base}</span>
            </div>
            <Bar pct={b.base ? Math.max((b.assigned / b.base) * 100, b.assigned ? 2 : 0) : 0} />
          </SideItem>
        ))}
        <div className="label" style={{ padding: "18px 8px 8px" }}>Layers</div>
        <SideItem active={layer === null} onClick={() => setLayer(null)}>
          <div className="row" style={{ justifyContent: "space-between" }}><span>All layers</span>
            <span className="sub">{current?.families ?? ""}</span></div>
        </SideItem>
        {(current?.layers ?? []).map((l) => (
          <SideItem key={l.index} active={layer === l.index} onClick={() => setLayer(l.index)}>
            <div className="row" style={{ justifyContent: "space-between" }}><span>{l.index + 1} · {l.name}</span>
              <span className="sub">{l.families}</span></div>
          </SideItem>
        ))}
      </div>

      <div style={{ padding: "20px 24px 40px", minWidth: 0 }}>
        <PageHead sub={`${current?.prefix ?? ""}* · ${current?.base ?? "…"} base meshes · ≈ ${current?.catalog_estimate ?? "…"} incl. variants`}
          title={current?.name ?? biome}>
          <div className="row" style={{ fontSize: 12, color: "var(--muted)" }}>
            <span className="row" style={{ gap: 5 }}><span style={{ width: 7, height: 7, borderRadius: 2, background: "var(--ok)" }} />Assigned</span>
            <span className="row" style={{ gap: 5 }}><span style={{ width: 7, height: 7, borderRadius: 2, border: "1px solid #5a5c60" }} />Planned</span>
          </div>
          <Segmented value={show} onChange={setShow}
            options={[{ id: "all", key: "all", label: "All slots" }, { id: current ? String(current.assigned) : "", key: "assigned", label: "Assigned only" }]} />
          <Segmented value={view} onChange={setView}
            options={[{ id: "1a", key: "grid", label: "Slot grid" }, { id: "1b", key: "table", label: "Family table" }]} />
        </PageHead>
        <ErrorLine error={summary.error ?? tree.error} />
        {!tree.data && !tree.error && <div className="sub" style={{ padding: "40px 0" }}>Loading catalog…</div>}
        {visible && visibleCount === 0 && (
          <div className="banner note" style={{ marginTop: 8 }}>No assigned assets in {current?.name ?? biome}
            {layer === null ? "" : " for this layer"} yet. Approve a candidate, then assign its 3D attempt to a slot.</div>
        )}
        {visible && view === "grid" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
            {visible.layers.flatMap((l) => l.families.map((f) => <FamilyGrid key={f.id} fam={f} layerName={l.name} />))}
          </div>
        )}
        {visible && view === "table" && visibleCount > 0 && <FamilyTable tree={visible} />}
      </div>
    </div>
  );
}
