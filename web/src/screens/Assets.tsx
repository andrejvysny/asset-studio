import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Empty, ErrorLine, INFO, Loading, PageHead, Toggle } from "../components/ui";
import { type AssetList, type CategoryNode, type Family, type Kind, KIND_LABEL, KINDS, type Origin, P } from "../lib/api";
import { useApi } from "../lib/hooks";
import { useProject } from "../lib/project";
import type { AssetQuery } from "../lib/variantsApi";
import { AssetCard, FamilyTile, useGroupedAssets } from "./AssetsGrouped";
import { ImportDialog } from "./ImportDialog";

const PAGE = 60;

export function Assets() {
  const { id } = useProject();
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const [importing, setImporting] = useState(false);
  const cat = sp.get("cat");
  const kind = sp.get("kind");
  const origin = sp.get("origin");
  const q = sp.get("q") ?? "";
  const planned = sp.get("planned") !== "0";
  const page = Number(sp.get("page") ?? "0");
  const group = sp.get("group") === "family";
  const familyId = sp.get("family");
  const grouped = group && !familyId;
  const setMany = (kv: Record<string, string | null>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(kv)) if (v === null || v === "") next.delete(k); else next.set(k, v);
    if (!("page" in kv)) next.delete("page");
    setSp(next, { replace: true });
  };
  const set = (k: string, v: string | null) => setMany({ [k]: v });
  const filters: AssetQuery = { ...(cat ? { category_id: cat } : {}), ...(kind ? { kind: kind as Kind } : {}),
    ...(origin ? { origin: origin as Origin } : {}), ...(q ? { q } : {}) };
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE), planned: planned && !familyId ? "1" : "0" });
  for (const [k, v] of Object.entries(filters)) params.set(k, String(v));
  if (familyId) params.set("family_id", familyId);
  const list = useApi<AssetList>(grouped ? null : `${P(id)}/assets?${params}`, { project: id });
  const gp = useGroupedAssets(id, filters, grouped, PAGE);
  const family = useApi<Family>(familyId ? `${P(id)}/families/${familyId}` : null, { project: id });
  const anyFilter = !!(cat || kind || origin || q);
  const cats = useApi<{ categories: CategoryNode[] }>(`${P(id)}/categories`, { project: id });
  const current = cats.data?.categories.find((c) => c.id === cat);
  const d = list.data;

  return (
    <div className="split">
      <aside className="side-list" aria-label="categories">
        <div className="row" style={{ justifyContent: "space-between", padding: "0 8px 8px" }}>
          <span className="label">Categories</span>
          <Link to={`/p/${id}/schema`} className="dim" style={{ fontSize: 11.5 }}>Edit schema</Link>
        </div>
        <button className={`side-row${!cat ? " on" : ""}`} onClick={() => set("cat", null)}>
          <span>All assets</span><span className="n">{d?.all_assets_total ?? (grouped ? gp.allAssets : "")}</span></button>
        {(cats.data?.categories ?? []).map((c) => (
          <button key={c.id} className={`side-row${cat === c.id ? " on" : ""}`}
            style={{ paddingLeft: 8 + c.depth * 16 }} onClick={() => set("cat", c.id)}>
            <span>{c.label}</span><span className="n">{c.count}</span></button>
        ))}
        {cats.data && cats.data.categories.length === 0 &&
          <div className="sub" style={{ padding: 8 }}>No categories yet. Assets can still be imported unclassified.</div>}
      </aside>
      <section className="content">
        <PageHead sub={current ? current.path : "all categories"} title={current ? current.label : "All assets"}>
          <button className="btn" onClick={() => setImporting(true)}>Import files</button>
          <button className="btn btn-primary" onClick={() => nav(`/p/${id}/jobs/new${cat ? `?cat=${cat}` : ""}`)}>
            New Job</button>
        </PageHead>
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <input className="input" aria-label="search" value={q} placeholder="Search name, id, tag…"
            onChange={(e) => set("q", e.target.value)} style={{ width: 220 }} />
          <button className={`chip${!kind && !origin ? " on" : ""}`} onClick={() => { set("kind", null); set("origin", null); }}>
            All types</button>
          {KINDS.map((k) => (
            <button key={k} className={`chip${kind === k ? " on" : ""}`} onClick={() => set("kind", kind === k ? null : k)}>
              {KIND_LABEL[k]}</button>
          ))}
          <button className={`chip${origin === "imported" ? " on" : ""}`}
            onClick={() => set("origin", origin === "imported" ? null : "imported")} title="origin filter">Imported</button>
          <span className="grow" />
          {familyId && <button className="chip on" aria-label="remove family filter" onClick={() => set("family", null)}>
            Family: {family.data?.name ?? familyId} ×</button>}
          <label className="row" style={{ gap: 7, fontSize: 12 }}>
            <Toggle on={planned} label="show planned" disabled={group || !!familyId} onChange={(v) => set("planned", v ? null : "0")} />Show planned
          </label>
          <label className="row" style={{ gap: 7, fontSize: 12, color: "var(--text-2)" }}>
            <Toggle on={group} label="Group by family" onChange={(v) => setMany({ group: v ? "family" : null, family: null })} />Group by family
          </label>
        </div>
        <ErrorLine error={grouped ? gp.error : list.error} />
        {grouped ? (
          gp.loading ? <Loading what="assets" /> : (
            <>
              <div className="sub">{gp.matchingAssets} asset{gp.matchingAssets === 1 ? "" : "s"} · grouped by family</div>
              {gp.groups.length === 0 ? (
                <Empty>{gp.allAssets === 0
                  ? <>No assets yet. <button className="btn-link" onClick={() => setImporting(true)}>Import files</button>
                    or <Link to={`/p/${id}/jobs/new`}>create a Job</Link>.</>
                  : "Nothing matches these filters."}</Empty>
              ) : (
                <div className="grid-cards">
                  {gp.groups.map((g) => g.type === "family"
                    ? <FamilyTile key={g.family_id} project={id} g={g} filtered={anyFilter} onOpen={() => set("family", g.family_id)} />
                    : <AssetCard key={g.asset.asset_id} project={id} a={g.asset} />)}
                </div>
              )}
              {gp.nextCursor && <div className="row"><button className="btn" disabled={gp.loadingMore} onClick={gp.more}>
                {gp.loadingMore ? "Loading…" : "Load more"}</button></div>}
            </>
          )
        ) : !d ? <Loading what="assets" /> : (
          <>
            <div className="sub">{d.total} asset{d.total === 1 ? "" : "s"}{planned && !familyId ? ` · ${d.planned_total} planned (not counted)` : ""}</div>
            {d.total === 0 && (familyId || d.planned.length === 0) ? (
              <Empty>{d.all_assets_total === 0
                ? <>No assets yet. <button className="btn-link" onClick={() => setImporting(true)}>Import files</button>
                  or <Link to={`/p/${id}/jobs/new`}>create a Job</Link>.</>
                : "Nothing matches these filters."}</Empty>
            ) : (
              <div className="grid-cards">
                {d.items.map((a) => <AssetCard key={a.asset_id} project={id} a={a} />)}
                {planned && !familyId && d.planned.map((s) => (
                  <Link key={s.id} className="card planned"
                    to={s.membership ? `/p/${id}/jobs/${s.membership.batch_id}` : `/p/${id}/shots`}>
                    <div className="media" style={{ flexDirection: "column", gap: 4 }}>
                      <span className="sub" style={{ fontSize: 10 }}>planned</span>
                      <span className="sub" style={{ fontSize: 10, color: s.membership ? INFO : "var(--faint)" }}>
                        {s.membership ? `in ${s.membership.batch_alias}` : "not started"}</span>
                    </div>
                    <div className="meta"><span className="ellipsis muted">{s.name}</span>
                      <span className="sub" style={{ fontSize: 10, color: "var(--faint)" }}>shot list</span></div>
                  </Link>
                ))}
              </div>
            )}
            {d.total > PAGE && (
              <div className="row">
                <button className="btn" disabled={page === 0} onClick={() => set("page", String(page - 1))}>Previous</button>
                <span className="sub">page {page + 1} / {Math.ceil(d.total / PAGE)}</span>
                <button className="btn" disabled={(page + 1) * PAGE >= d.total}
                  onClick={() => set("page", String(page + 1))}>Next</button>
              </div>
            )}
          </>
        )}
      </section>
      {importing && <ImportDialog categories={cats.data?.categories ?? []} defaultCategory={cat}
        onClose={() => setImporting(false)} onDone={(assetId) => { setImporting(false); nav(`/p/${id}/assets/${assetId}`); }} />}
    </div>
  );
}
