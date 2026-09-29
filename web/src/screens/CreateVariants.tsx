import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ErrorLine, Loading } from "../components/ui";
import { type AssetDetail, KIND_LABEL, P } from "../lib/api";
import { useApi } from "../lib/hooks";
import { useProject } from "../lib/project";
import {
  CANDIDATE_OPTIONS, INTENTS, isDirect, MAX_ROWS, STYLE_CONFLICT_TEXT, workOf,
} from "./variants/model";
import { Field, MethodPicker, ReferencesPanel, RowsTable, SourceCard, SuggestionBox } from "./variants/Parts";
import { useVariantDraft } from "./variants/useVariantDraft";
import "./variants/variants.css";

/** Route /p/:project/assets/:assetId/variants?version=<version_id>&count=<n>. */
export function CreateVariants() {
  const { id } = useProject();
  const { assetId = "" } = useParams();
  const [sp, setSp] = useSearchParams();
  const asset = useApi<AssetDetail>(`${P(id)}/assets/${assetId}`, { project: id });
  const count = Math.max(1, Math.min(MAX_ROWS, Number(sp.get("count") ?? "1") || 1));
  const versionId = sp.get("version") ?? asset.data?.manifest.current_version_id ?? "";
  if (asset.error) return <div className="content"><ErrorLine error={asset.error} /></div>;
  if (!asset.data || !versionId) return <Loading what="asset" />;
  return <Wizard key={`${assetId}:${count}`} assetId={assetId} versionId={versionId} count={count} asset={asset.data}
    onVersion={(v) => setSp({ version: v, count: String(count) }, { replace: true })} />;
}

function Wizard({ assetId, versionId, count, asset, onVersion }:
  { assetId: string; versionId: string; count: number; asset: AssetDetail; onVersion: (v: string) => void }) {
  const { id } = useProject();
  const nav = useNavigate();
  const v = useVariantDraft(id, assetId, versionId, count);
  const [creating, setCreating] = useState(false);
  const back = `/p/${id}/assets/${assetId}${versionId === asset.manifest.current_version_id ? "" : `?version=${versionId}`}`;
  const title = count > 1 ? "Create variants" : "New variant";
  const head = (
    <div>
      <Link to={back} className="sub" style={{ textDecoration: "none" }}>← {asset.manifest.display_name} / {title}</Link>
      <h1 className="h1" style={{ margin: "2px 0 0" }}>{title}</h1>
    </div>
  );
  if (v.boot.phase === "error") return <div className="vz-page">{head}<div className="banner bad" role="alert">
    <span>{v.boot.problem.text}{v.boot.problem.details.map((d) => <div key={d}>{d}</div>)}</span></div></div>;
  if (v.boot.phase === "unavailable" && v.caps) return (
    <div className="vz-page">{head}
      <div className="banner warn" role="alert"><div>No variant method is available for this asset version.
        {v.caps.methods.map((m) => <div key={m.method} className="sub">{m.label}: {m.message || m.reason}</div>)}</div></div>
    </div>);
  const { caps, form, detail } = v;
  if (!caps || !form) return <div className="vz-page">{head}<Loading what="variant draft" /></div>;

  const kind = caps.source.kind;
  const direct = isDirect(form.method);
  const n = form.rows.length;
  const work = detail && !v.pending && !v.saving && detail.work.rows === n ? detail.work : workOf(form.method, n, form.cands);
  const srcTag = `${caps.source.asset_id} v${caps.source.display_version}`;
  const styleBlock = !direct && caps.style.conflict;
  const blocked = v.problems.find((p) => p !== null) ?? null;
  const refsBlock = !direct && v.refsState !== "ready";
  const canSave = n > 0 && !blocked && !v.suggesting && !v.saving && !refsBlock && (!styleBlock || form.styleAck) && !creating;
  const suggestion = detail?.suggestion && detail.suggestion.task_id !== v.appliedTask ? detail.suggestion : null;
  const suggestTask = detail?.tasks.suggest;
  const thumb = caps.source.artifacts.find((a) => a.role === "preview" && a.mime.startsWith("image/"))
    ?? caps.source.artifacts.find((a) => a.role === caps.source.primary_role && a.mime.startsWith("image/"))
    ?? caps.source.artifacts.find((a) => a.mime.startsWith("image/"));
  const primaryRef = v.refs?.images.find((i) => i.role === "primary");

  const save = async () => {
    setCreating(true);
    const res = await v.save();
    setCreating(false);
    if (res) nav(res.batch_id ? `/p/${id}/batches/${res.batch_id}` : `/p/${id}/jobs/${res.job_ids[0]}`);
  };

  return (
    <div className="vz-page" aria-busy={v.boot.phase === "loading"}>
      {head}
      <div className="vz-grid" style={v.boot.phase === "loading" ? { opacity: 0.6 } : undefined}>
        <SourceCard project={id} caps={caps} versions={asset.manifest.versions} onVersion={onVersion}
          thumbId={primaryRef?.artifact_id ?? thumb?.artifact_id ?? null}
          family={caps.family} familyName={form.familyName} onFamilyName={(s) => v.update({ familyName: s }, "family")} />
        <div className="vz-col">
          {v.notice && <div className="banner warn" role="status"><span className="grow">{v.notice}</span>
            <button className="btn-link" style={{ padding: 0 }} onClick={v.dismissNotice}>Dismiss</button></div>}
          <MethodPicker caps={caps} value={form.method} onChange={v.setMethod} />
          {!direct && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="vz-lab" id="vz-intent-label">Variation intent</span>
              <div className="vz-intents" role="radiogroup" aria-labelledby="vz-intent-label">
                {INTENTS.map((i) => <button key={i.id} role="radio" aria-checked={form.intent === i.id}
                  onClick={() => v.update({ intent: i.id }, "intent")}>{i.label}</button>)}
              </div>
              <span className="vz-note">{INTENTS.find((i) => i.id === form.intent)?.note}</span>
            </div>
          )}
          {!direct && (
            <Field label="Preserve · applies to every row">
              <textarea className="input" rows={2} aria-label="preserve" value={form.preserve}
                onChange={(e) => v.update({ preserve: e.target.value }, "preserve")} maxLength={500} />
            </Field>
          )}
          {!direct && <ReferencesPanel project={id} state={v.refsState} refs={v.refs} error={v.refsProblem?.text ?? null}
            onSelect={v.selectView} onRetry={v.retryRefs} />}
          {styleBlock && (
            <div className="banner warn" role="alert" style={{ flexDirection: "column" }}>
              <span>{STYLE_CONFLICT_TEXT}</span>
              <label className="row" style={{ gap: 8 }}>
                <input type="checkbox" checked={form.styleAck} aria-label="acknowledge style change"
                  onChange={(e) => v.update({ styleAck: e.target.checked }, "ack")} />
                I understand; generate with the current project style
              </label>
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
              <span className="vz-lab">Plan · one row per new asset</span>
              <span className="sub">{n} row{n === 1 ? "" : "s"}{v.pending || v.saving ? " · saving…" : ""}</span>
            </div>
            {count > 1 && !direct && (
              <div className="row" style={{ flexWrap: "wrap" }}>
                <input className="input grow" aria-label="describe the set" style={{ minWidth: 240, fontFamily: "var(--sans)", fontSize: 12.5 }}
                  value={form.request} placeholder="Describe the set, e.g. six variants: taller, squatter, damaged…"
                  onChange={(e) => v.update({ request: e.target.value }, "request")} maxLength={4000} />
                <button className="btn" disabled={v.suggesting || v.saving} onClick={() => void v.suggest()}>
                  {v.suggesting ? "Suggesting…" : "Suggest rows"}</button>
              </div>
            )}
            {count > 1 && direct && <span className="vz-note">Direct transforms are not suggested: add one row per transform.</span>}
            {suggestTask && (suggestTask.state === "failed" || suggestTask.state === "blocked") && (
              <div className="banner bad" role="alert"><span>Suggestion failed: {suggestTask.error ?? suggestTask.state}
                {suggestTask.code ? ` (${suggestTask.code})` : ""}. You can still write the rows by hand.</span></div>
            )}
            {suggestion && <SuggestionBox s={suggestion} busy={v.saving} onUse={() => void v.useSuggestion()} />}
            <RowsTable method={form.method} kind={kind} rows={form.rows} problems={v.problems}
              onEdit={v.editRow} onRemove={v.removeRow} />
            <div className="row" style={{ flexWrap: "wrap" }}>
              <button className="vz-small" onClick={v.addRow} disabled={n >= MAX_ROWS}>+ Add row</button>
              {!direct && (
                <div className="row" style={{ gap: 6 }} role="group" aria-label="candidates per row">
                  <span style={{ fontSize: 12, color: "var(--dim)" }}>Candidates per row</span>
                  {CANDIDATE_OPTIONS.map((c) => <button key={c} className="vz-cand" aria-pressed={form.cands === c}
                    onClick={() => v.update({ cands: c }, "cands")}>{c}</button>)}
                </div>
              )}
            </div>
          </div>
          <div className="vz-summary" aria-label="work summary">
            <span className="mono" style={{ fontSize: 12 }}>
              {direct ? `${work.transforms} transform${work.transforms === 1 ? "" : "s"} · no image-model calls`
                : `${work.rows} variant${work.rows === 1 ? "" : "s"} × ${form.cands} candidates = ${work.image_edits} image edits · up to ${work.builds} ${kind === "model3d" ? "3D builds" : "builds"} after approval`}
            </span>
            <span style={{ fontSize: 12, color: "var(--dim)", textWrap: "pretty" }}>
              {n > 1 ? `Each row becomes its own Job, conditioned on ${srcTag}. The Jobs are grouped into a new draft Batch. Nothing runs until you start it.`
                : `Saved as one Job, conditioned on ${srcTag}. Nothing runs until you run it.`}
            </span>
            <span className="sub" style={{ color: "var(--faint)" }}>{KIND_LABEL[kind]} · {caps.source.asset_id}</span>
          </div>
          {v.problem && (
            <div className="banner bad" role="alert"><div>{v.problem.text}
              {v.problem.details.map((d) => <div key={d} className="sub" style={{ color: "inherit" }}>{d}</div>)}
              {["source_family_changed", "source_version_mismatch", "draft_materialized"].includes(v.problem.code) &&
                <div><button className="btn-link" style={{ padding: 0 }} onClick={() => window.location.reload()}>Start a new draft</button></div>}
            </div></div>
          )}
          {blocked && n > 0 && <span className="vz-note" style={{ color: "var(--warn)" }}>Fix the marked rows to save.</span>}
          {styleBlock && !form.styleAck && n > 0 && <span className="vz-note" style={{ color: "var(--warn)" }}>Acknowledge the style change to save.</span>}
          <div className="row" style={{ flexWrap: "wrap" }}>
            <button className="btn btn-primary" style={{ padding: "8px 16px" }} disabled={!canSave} onClick={() => void save()}>
              {n > 1 ? `Save ${n} Jobs + draft Batch` : n === 1 ? "Save Job" : "Add a row first"}</button>
            <button className="btn-link" onClick={() => nav(back)}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}
