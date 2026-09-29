import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Empty, ErrorLine, INFO, Loading, PageHead, Toggle } from "../components/ui";
import { type AssetList, artifactUrl, type CategoryNode, KIND_LABEL, KINDS, P } from "../lib/api";
import { useApi } from "../lib/hooks";
import { useProject } from "../lib/project";
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
  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(sp);
    if (v === null || v === "") next.delete(k); else next.set(k, v);
    if (k !== "page") next.delete("page");
    setSp(next, { replace: true });
  };
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE), planned: planned ? "1" : "0" });
  if (cat) params.set("category_id", cat);
  if (kind) params.set("kind", kind);
  if (origin) params.set("origin", origin);
  if (q) params.set("q", q);
  const list = useApi<AssetList>(`${P(id)}/assets?${params}`, { project: id });
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
          <span>All assets</span><span className="n">{d?.all_assets_total ?? ""}</span></button>
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
          <button className="btn btn-primary" onClick={() => nav(`/p/${id}/batches/new${cat ? `?cat=${cat}` : ""}`)}>
            New batch</button>
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
          <label className="row" style={{ gap: 7, fontSize: 12 }}>
            <Toggle on={planned} label="show planned" onChange={(v) => set("planned", v ? null : "0")} />Show planned
          </label>
        </div>
        <ErrorLine error={list.error} />
        {!d ? <Loading what="assets" /> : (
          <>
            <div className="sub">{d.total} asset{d.total === 1 ? "" : "s"}{planned ? ` · ${d.planned_total} planned (not counted)` : ""}</div>
            {d.total === 0 && d.planned.length === 0 ? (
              <Empty>{d.all_assets_total === 0
                ? <>No assets yet. <button className="btn-link" onClick={() => setImporting(true)}>Import files</button>
                  or <Link to={`/p/${id}/batches/new`}>start a batch</Link>.</>
                : "Nothing matches these filters."}</Empty>
            ) : (
              <div className="grid-cards">
                {d.items.map((a) => (
                  <Link key={a.asset_id} to={`/p/${id}/assets/${a.asset_id}`} className="card">
                    <div className="media checker">
                      {a.preview_artifact_id
                        ? <img src={artifactUrl(id, a.preview_artifact_id)} alt={a.display_name} loading="lazy" />
                        : <span className="sub">{a.kind === "model3d" ? "GLB · open to view" : "no preview"}</span>}
                      <span className="corner" style={{ left: 7 }}>{a.kind_label}{a.origin === "imported" ? " · imp" : ""}</span>
                      <span className="corner" style={{ right: 7, fontWeight: 600, color: "var(--text)" }}>v{a.display_version}</span>
                    </div>
                    <div className="meta">
                      <span className="ellipsis" style={{ fontWeight: 500 }}>{a.display_name}</span>
                      <span className="sub ellipsis" style={{ fontSize: 10 }}>{a.name_id}</span>
                    </div>
                  </Link>
                ))}
                {planned && d.planned.map((s) => (
                  <Link key={s.id} className="card planned"
                    to={s.membership ? `/p/${id}/batches/${s.membership.batch_id}` : `/p/${id}/shots`}>
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
