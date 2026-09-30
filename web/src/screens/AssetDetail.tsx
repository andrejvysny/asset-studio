import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { OutputView } from "../components/outputs";
import { bytes, ErrorLine, Loading, OK, relTime } from "../components/ui";
import { type AssetDetail as Detail, artifactUrl, type Derivation, key, P, send, type VariantMethod } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { useProject } from "../lib/project";

const FACT_LABEL: Record<string, string> = {
  kind: "Asset type", recipe_id: "Pipeline", naming: "Naming rule", budget: "Budget", build_profile: "Build profile (no effect yet)",
  qa_ruleset: "QA rule set", reference_set: "Reference set", style: "Style", style_lora: "Style LoRA",
  export_presets: "Export presets", candidate_count: "Candidates",
};

const METHOD_LABEL: Record<VariantMethod, string> = {
  image_edit_reconstruct: "Structural reconstruction", image_edit: "Design variant", direct_transform: "Direct size transform",
};

/** The manifest file plus the shown version's lineage (kept in the version record, shown here as in the design). */
function manifestText(json: string, deriv: Derivation | null): string {
  if (!deriv) return json;
  try {
    return JSON.stringify({ ...(JSON.parse(json) as Record<string, unknown>), derivation: {
      source_asset_id: deriv.source.asset_id, source_version: deriv.source.display_version, method: deriv.method } }, null, 2);
  } catch { return json; }
}

function downloadAll(project: string, files: { artifact_id: string }[]): void {
  files.forEach((f, i) => setTimeout(() => {
    const a = document.createElement("a");
    a.href = `${artifactUrl(project, f.artifact_id)}?download=1`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }, i * 300));
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    if ("triangles" in o || "size_px" in o) {
      return Object.entries(o).filter(([, r]) => r).map(([k, r]) => {
        const x = r as { min: number | null; max: number | null; advisory: boolean };
        return `${x.min ?? "…"}–${x.max ?? "…"} ${k.replace("_px", " px")}${x.advisory ? " (advisory)" : ""}`;
      }).join(" · ") || "—";
    }
    if ("model_id" in o) return `${o.model_id as string} @ ${o.strength as number}`;
    return JSON.stringify(v);
  }
  return String(v);
}

function variantNote(d: Derivation | null | undefined): string {
  return d ? `Variant of ${d.source.display_name} v${d.source.display_version}` : "";
}

export function AssetDetail() {
  const { id } = useProject();
  const { assetId = "" } = useParams();
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const shown = sp.get("version");
  const d = useApi<Detail>(`${P(id)}/assets/${assetId}${shown ? `?version=${shown}` : ""}`, { project: id });
  const act = useAction();
  const [copied, setCopied] = useState(false);
  if (!d.data) return d.error ? <div className="content"><ErrorLine error={d.error} /></div> : <Loading what="asset" />;
  const { manifest: m, shown_version: v } = d.data;
  const lic = v.licence.status;
  const frameFiles = d.data.files.filter((f) => f.role.startsWith("frame_"));
  const files = [...d.data.files.filter((f) => !f.role.startsWith("frame_")), ...frameFiles.slice(0, 3)];
  const hiddenFrames = frameFiles.length - Math.min(frameFiles.length, 3);
  const src = d.data.derived_from;
  const lineage = new Map(d.data.versions.map((x) => [x.version_id, x.derivation]));
  const variantsUrl = (count: number) => `/p/${id}/assets/${m.asset_id}/variants?version=${v.version_id}&count=${count}`;
  const shownManifest = manifestText(d.data.manifest_json, v.derivation);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 420px", minHeight: "100%" }}>
      <section className="content">
        <div>
          <Link to={`/p/${id}/assets`} className="sub" style={{ textDecoration: "none" }}>← Library / {d.data.category_label ?? "unclassified"}</Link>
          <div className="row" style={{ alignItems: "baseline", gap: 12, marginTop: 6, flexWrap: "wrap" }}>
            <h1 className="h1" style={{ margin: 0 }}>{m.display_name}</h1>
            <span className="mono muted" style={{ fontSize: 12 }}>{m.name_id}</span>
          </div>
          <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            <span className="tag">{d.data.kind_label}</span>
            <span className="tag">{m.origin}</span>
            <span className="pill ok">current v{m.versions.find((x) => x.version_id === m.current_version_id)?.display_version}</span>
            <span className="tag">{m.versions.length} versions</span>
            {d.data.family_id && <Link className="tag" style={{ borderColor: "var(--line-3)", color: "var(--text)", textDecoration: "none" }}
              to={`/p/${id}/assets?family=${d.data.family_id}`} title="Open the family in the library">
              family · {d.data.family_name ?? d.data.family_id}</Link>}
            {src && <Link className="sub" style={{ padding: "1px 2px", color: "var(--muted)" }}
              to={`/p/${id}/assets/${src.asset_id}?version=${src.version_id}`} title={`Open ${src.display_name} v${src.display_version}`}>
              from {src.asset_id} · v{src.display_version} · {METHOD_LABEL[src.method]}</Link>}
            <span className={`pill ${lic === "cleared" ? "ok" : lic === "not_cleared" ? "bad" : "warn"}`}
              title={v.licence.note}>licence: {lic}</span>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(260px,1fr) minmax(260px,1fr)", gap: 16 }}>
          <div className="panel" style={{ display: "flex", flexDirection: "column" }}>
            <OutputView key={v.version_id} project={id} height={300} alt={m.display_name}
              roles={Object.fromEntries(d.data.files.map((f) => [f.role, f.artifact_id]))} />
            <div className="sub tr" style={{ padding: "8px 12px" }}>
              showing v{v.display_version} · {d.data.is_current ? "current" : "older version"}</div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="label">Versions · immutable</span>
            {[...m.versions].reverse().map((ver) => {
              const isCur = ver.version_id === m.current_version_id;
              const isShown = ver.version_id === v.version_id;
              return (
                <div key={ver.version_id} role="button" tabIndex={0} className="panel"
                  style={{ padding: "9px 11px", display: "flex", flexDirection: "column", gap: 3, cursor: "pointer",
                    borderColor: isShown ? "var(--dim)" : undefined, background: isShown ? "#1a1b1d" : undefined }}
                  onClick={() => setSp(isCur ? {} : { version: ver.version_id })}
                  onKeyDown={(e) => { if (e.key === "Enter") setSp(isCur ? {} : { version: ver.version_id }); }}>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="mono" style={{ fontWeight: 600 }}>v{ver.display_version}</span>
                    {isCur && <span className="sub" style={{ color: OK }}>● current</span>}
                    <span className="grow" />
                    {!isCur && <button className="tag" disabled={act.busy} onClick={(e) => {
                      e.stopPropagation();
                      void act.run(async () => {
                        await send("POST", `${P(id)}/assets/${m.asset_id}:set-current`, { version_id: ver.version_id,
                          expected_current_version: m.current_version_id, idempotency_key: key(), reason: "set in UI" });
                        d.reload();
                      });
                    }}>Set current</button>}
                  </div>
                  <span className="muted" style={{ fontSize: 12 }}>{ver.note || variantNote(lineage.get(ver.version_id)) || "—"}</span>
                  <span className="sub">{relTime(ver.published_at)} · {ver.version_id}</span>
                </div>
              );
            })}
            <ErrorLine error={act.error} />
          </div>
        </div>
        {d.data.facts.length > 0 && (
          <div>
            <div className="label" style={{ marginBottom: 8 }}>Spec · current category schema (for new versions)</div>
            <div className="kv">
              {d.data.facts.filter((f) => f.value !== null || f.mode === "disabled").map((f) => (
                <div key={f.key}><span className="dim" style={{ fontSize: 11 }}>{FACT_LABEL[f.key] ?? f.key}</span>
                  <span className="mono" style={{ fontSize: 12 }}>{f.mode === "disabled" ? "disabled" : fmt(f.value)}</span>
                  <span style={{ fontSize: 10.5, color: "var(--faint)" }}>{f.source.startsWith("category:")
                    ? (f.source.slice(9) === m.category_id ? `set on ${f.source.slice(9)}` : `inherited from ${f.source.slice(9)}`)
                    : f.source}</span></div>
              ))}
            </div>
          </div>
        )}
        <div>
          <div className="label" style={{ marginBottom: 8 }}>Files in v{v.display_version}</div>
          <div className="table">
            {files.map((f) => (
              <div key={f.role} className="td" style={{ gridTemplateColumns: "110px minmax(140px,1fr) 80px minmax(160px,1.2fr)" }}>
                <span className="dim">{f.role}</span>
                <a className="mono" href={`${artifactUrl(id, f.artifact_id)}?download=1`}>{f.mime}</a>
                <span className="mono muted">{bytes(f.size)}</span>
                <span className="mono dim ellipsis" title={f.sha256}>blobs/sha256/{f.sha256.slice(0, 2)}/{f.sha256}</span>
              </div>
            ))}
            {hiddenFrames > 0 && <div className="td sub" style={{ gridTemplateColumns: "1fr" }}>
              + {hiddenFrames} more source frames (frame_0003 … ) · all hashed and listed in the manifest</div>}
          </div>
        </div>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <button className="btn btn-primary" onClick={() => nav(variantsUrl(1))}>New variant</button>
          <button className="btn" onClick={() => nav(variantsUrl(6))}>Create variants…</button>
          <button className="btn" onClick={() => nav(`/p/${id}/jobs/new?target=${m.asset_id}&kind=${m.kind}${m.category_id ? `&cat=${m.category_id}` : ""}&name=${encodeURIComponent(m.display_name)}`)}>
            New version…</button>
          <button className="btn" disabled title="Export targets arrive in Phase 4">Export current</button>
          <button className="btn" onClick={() => downloadAll(id, d.data?.files ?? [])}>Download files</button>
        </div>
      </section>
      <aside style={{ borderLeft: "1px solid var(--line)", background: "var(--panel)", display: "flex", flexDirection: "column", minHeight: 0 }}>
        <div className="row" style={{ padding: "12px 14px", borderBottom: "1px solid var(--line)" }}>
          <span className="mono grow" style={{ fontSize: 11 }} title={v.derivation ? "manifest file plus the derivation of the shown version" : undefined}>manifests/{m.asset_id}.json</span>
          <button className="btn" style={{ padding: "3px 9px", fontSize: 12 }} onClick={() => {
            void navigator.clipboard.writeText(shownManifest);
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          }}>{copied ? "Copied" : "Copy JSON"}</button>
        </div>
        <pre className="pre" style={{ flex: 1 }}>{shownManifest}</pre>
        <div className="label" style={{ padding: "8px 14px" }}>version record · provenance</div>
        <pre className="pre" style={{ flex: 1, borderTop: "1px solid var(--line)" }}>{JSON.stringify(
          { sources: v.sources, qa: v.qa, validation: v.validation, licence: v.licence, parameters: v.parameters }, null, 2)}</pre>
      </aside>
    </div>
  );
}
