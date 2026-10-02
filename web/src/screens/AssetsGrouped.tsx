import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { type AssetRow, artifactUrl, ApiError, type FamilyGroup, type GroupedAssets, type AssetGroupItem } from "../lib/api";
import { useChanges } from "../lib/hooks";
import { type AssetQuery, listAssetsGrouped } from "../lib/variantsApi";

export interface Grouped {
  groups: AssetGroupItem[]; matchingAssets: number; allAssets: number; archivedTotal: number; nextCursor: string | null;
  loading: boolean; error: string | null; more: () => void; loadingMore: boolean;
}

/** Cursor-paged `group_by=family` listing. A changed query or library revision gives 409 stale_cursor: restart. */
export function useGroupedAssets(project: string, query: AssetQuery, enabled: boolean, limit = 60): Grouped {
  const [data, setData] = useState<GroupedAssets | null>(null);
  const [groups, setGroups] = useState<AssetGroupItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [tick, setTick] = useState(0);
  const queryKey = JSON.stringify(query);
  const seq = useRef(0);
  useChanges((e) => enabled && e.project_id === project, () => setTick((t) => t + 1));

  useEffect(() => {
    if (!enabled) return;
    const mine = ++seq.current;
    listAssetsGrouped(project, { ...query, limit })
      .then((d) => { if (seq.current === mine) { setData(d); setGroups(d.groups); setError(null); } })
      .catch((e: Error) => { if (seq.current === mine) setError(e.message); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, queryKey, enabled, limit, tick]);

  const more = useCallback(() => {
    if (!data?.next_cursor || loadingMore) return;
    const mine = seq.current;
    setLoadingMore(true);
    listAssetsGrouped(project, { ...query, limit, cursor: data.next_cursor })
      .then((d) => { if (seq.current === mine) { setData(d); setGroups((g) => [...g, ...d.groups]); } })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.code === "stale_cursor") setTick((t) => t + 1);
        else setError((e as Error).message);
      })
      .finally(() => setLoadingMore(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, queryKey, limit, data, loadingMore]);

  return { groups: enabled ? groups : [], matchingAssets: data?.matching_asset_count ?? 0, allAssets: data?.all_assets_total ?? 0,
    archivedTotal: data?.archived_total ?? 0,
    nextCursor: enabled ? data?.next_cursor ?? null : null, loading: enabled && !data && !error, error: enabled ? error : null,
    more, loadingMore };
}

const stripes = "repeating-linear-gradient(135deg,#1b1c1e 0 6px,#202124 6px 12px)";
const badge = { position: "absolute" as const, top: 7, font: "500 10px 'Geist Mono',monospace", background: "#111213cc",
  color: "#c9cac6", padding: "1px 6px", borderRadius: 3 };

/** Stacked-card tile for a family; clicking it filters the library to the family's members. */
export function FamilyTile({ project, g, filtered, onOpen }:
  { project: string; g: FamilyGroup; filtered: boolean; onOpen: () => void }) {
  const rep: AssetRow | undefined = g.member_preview.find((a) => a.asset_id === g.representative_asset_id) ?? g.member_preview[0];
  const count = filtered && g.matching_count !== g.total_member_count
    ? `${g.matching_count} matching / ${g.total_member_count} members`
    : `${g.total_member_count} ${g.total_member_count === 1 ? "asset" : "assets"}`;
  return (
    <button onClick={onOpen} aria-label={`family ${g.name}, ${count}`} style={{ position: "relative", padding: "0 5px 5px 0", display: "block", width: "100%" }}>
      <div style={{ position: "absolute", left: 5, top: 5, right: 0, bottom: 0, border: "1px solid #2e3033", borderRadius: 7, background: "#151618" }} />
      <div style={{ position: "relative", border: "1px solid #3a3c40", borderRadius: 7, overflow: "hidden", background: "#17181a" }}>
        <div style={{ aspectRatio: "1", background: stripes, position: "relative", display: "flex", alignItems: "center", justifyContent: "center" }}>
          {rep?.preview_artifact_id
            ? <img src={artifactUrl(project, rep.preview_artifact_id)} alt="" loading="lazy" style={{ width: "100%", height: "100%", objectFit: "contain" }} />
            : <span className="sub" style={{ color: "#6e6f6b", fontSize: 10 }}>{rep ? "no preview" : "anchor preview"}</span>}
          <span style={{ ...badge, left: 7 }}>family</span>
          <span style={{ ...badge, right: 7, fontWeight: 600, color: "#e8e8e6" }}>{count}</span>
        </div>
        <div style={{ padding: "7px 9px", display: "flex", flexDirection: "column", gap: 1 }}>
          <span className="ellipsis" style={{ fontWeight: 500 }}>{g.name}</span>
          <span className="sub" style={{ fontSize: 10 }}>{rep?.kind_label ?? "family"} · open family</span>
        </div>
      </div>
    </button>
  );
}

/** Flat asset card shared by the flat and family-filtered views. */
export function AssetCard({ project, a, selecting, selected, onToggle }:
  { project: string; a: AssetRow; selecting?: boolean; selected?: boolean; onToggle?: () => void }) {
  const body = (
    <>
      <div className="media checker">
        {a.preview_artifact_id
          ? <img src={artifactUrl(project, a.preview_artifact_id)} alt={a.display_name} loading="lazy" />
          : <span className="sub">{a.kind === "model3d" ? "GLB · open to view" : "no preview"}</span>}
        <span className="corner" style={{ left: 7 }}>{a.kind_label}{a.origin === "imported" ? " · imp" : ""}</span>
        <span className="corner" style={{ right: 7, fontWeight: 600, color: "var(--text)" }}>v{a.display_version}</span>
        {a.archived === 1 && <span className="corner" style={{ top: 28, right: 7 }}>archived</span>}
        {a.family_name && <span className="corner" style={{ top: "auto", bottom: 7, left: 7, maxWidth: "calc(100% - 14px)" }}
          title={`family · ${a.family_name}`}><span className="ellipsis" style={{ display: "block" }}>family · {a.family_name}</span></span>}
      </div>
      <div className="meta">
        <span className="ellipsis" style={{ fontWeight: 500 }}>{a.display_name}</span>
        <span className="sub ellipsis" style={{ fontSize: 10 }}>{a.name_id}</span>
      </div>
    </>
  );
  if (selecting) {
    return (
      <button className="card" role="checkbox" aria-checked={!!selected} aria-label={`select ${a.display_name}`}
        onClick={onToggle} style={{ textAlign: "left", color: "inherit", outline: selected ? "2px solid var(--text)" : undefined }}>
        {body}
        <span className="corner" style={{ top: 7, left: 7, background: selected ? "var(--text)" : undefined,
          color: selected ? "var(--bg)" : undefined }}>{selected ? "✓ selected" : "select"}</span>
      </button>
    );
  }
  return <Link to={`/p/${project}/assets/${a.asset_id}`} className="card">{body}</Link>;
}
