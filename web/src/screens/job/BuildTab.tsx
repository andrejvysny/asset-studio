import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { OutputView } from "../../components/outputs";
import { Bar, BAD, ErrorLine, INFO, NONE, OK, WARN } from "../../components/ui";
import { artifactUrl, J, key, send } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { ActionBar, type TabProps } from "./JobWorkspace";
import { ReexportDialog } from "./ReexportDialog";

export function BuildTab({ job, reload }: TabProps) {
  const { id } = useProject();
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const act = useAction();
  const base = `${J(id)}/${job.id}`;
  const rows = job.items.filter((i) => i.current_build || (i.tasks.build && i.approval));
  const active = rows.find((r) => r.id === sp.get("item")) ?? rows[0];
  const accepted = job.items.filter((i) => i.accepted_build);
  const built = job.items.filter((i) => i.build?.result === "valid");
  const [reexporting, setReexporting] = useState<string | null>(null);
  const blocked = !job.recipe.build_available && (
    <div className="banner bad">{job.recipe.build_label} build is not available right now:{" "}
      {job.recipe.build_blocked_reason}. Approved candidates and earlier builds are kept.</div>);
  if (blocked && rows.length === 0) return blocked;
  const accept = (itemId: string, run: string, rev: number, on: boolean) => void act.run(async () => {
    const res = await send<{ results: { ok: boolean; message?: string }[] }>("POST", `${base}:accept-builds`, {
      idempotency_key: key(), items: [{ item_id: itemId, build_run_id: run, expected_item_revision: rev, accept: on }] });
    reload();
    if (res.results[0] && !res.results[0].ok) throw new Error(res.results[0].message);
  });
  const view = active?.build;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {blocked}
      {rows.length === 0 ? <div className="empty">Nothing built yet. Approve candidates, then run the build from Approve.</div> : (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(320px,440px)", gap: 16, alignItems: "start" }}>
          <div className="table">
            {rows.map((it) => {
              const b = it.build;
              const t = it.tasks.build;
              const state = b ? (b.status === "succeeded" ? b.result ?? "done" : b.status) : t?.state ?? "—";
              const color = state === "valid" ? OK : state === "invalid" || state === "failed" || state === "blocked" ? BAD : INFO;
              const pct = b?.status === "succeeded" ? 100 : t?.state === "running" ? 50 : t?.state === "queued" ? 5 : 0;
              return (
                <div key={it.id} className="td clickable" onClick={() => setSp({ item: it.id })}
                  style={{ gridTemplateColumns: "52px minmax(120px,1fr) minmax(120px,1fr) 110px",
                    boxShadow: `inset 3px 0 0 ${it.id === active?.id ? "var(--text)" : "transparent"}` }}>
                  {b?.artifacts.preview ? <img src={artifactUrl(id, b.artifacts.preview)} alt="" style={{ width: 52, height: 52,
                    objectFit: "cover", borderRadius: 5 }} /> : <div className="stripes" style={{ width: 52, height: 52, borderRadius: 5 }} />}
                  <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}><span style={{ fontWeight: 500 }}>{it.name}</span>
                    <span className="sub" style={{ fontSize: 10.5 }}>from approved candidate</span></div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}><Bar pct={pct} color={color} />
                    <span className="sub" style={{ color }}>{state}{t?.error ? ` · ${t.error}` : ""}</span></div>
                  {b?.result === "valid" ? (
                    <button className={`btn${it.accepted_build ? " btn-primary" : ""}`} style={{ padding: "3px 9px", fontSize: 12 }}
                      disabled={act.busy || !!it.published} onClick={(e) => { e.stopPropagation();
                        accept(it.id, b.id, it.revision, !it.accepted_build); }}>
                      {it.published ? "published" : it.accepted_build ? "Accepted ✓" : "Accept"}</button>
                  ) : <span className="sub" style={{ color: NONE }}>—</span>}
                </div>
              );
            })}
          </div>
          {active && (
            <aside className="inspector">
              <div className="row sub" style={{ padding: "8px 12px", justifyContent: "space-between" }}>
                <span>{active.name}</span><span>{view?.status ?? active.tasks.build?.state}</span></div>
              <OutputView key={view?.id ?? "none"} project={id} roles={view?.artifacts ?? {}} alt={`${active.name} final`} />
              <div style={{ padding: "8px 12px", display: "flex", flexDirection: "column", gap: 4 }}>
                <span className="label">Structural validation (mandatory)</span>
                {(view?.validation.checks ?? []).map((c) => <div key={c.id} className="row" style={{ gap: 8, alignItems: "baseline" }}>
                  <span className="dot" style={{ background: c.ok ? OK : c.advisory ? WARN : BAD, flex: "none" }} />
                  <span className="mono" style={{ fontSize: 11, flex: "none" }}>{c.id}</span>
                  <span className="sub" style={{ flex: 1, minWidth: 0, textAlign: "right", overflowWrap: "anywhere" }}>
                    {c.ok ? "ok" : c.advisory ? "advisory" : "FAIL"} {c.detail ?? ""}</span></div>)}
              </div>
              {view?.result === "valid" && view.preview === "failed" && (
                <div className="banner note" style={{ margin: "0 12px" }}>Preview failed ({view.preview_error}). The model
                  itself is valid and available; retrying renders the preview from the delivered file only.
                  <button className="btn" style={{ marginLeft: 8 }} disabled={act.busy || !active.legal.retry_preview}
                    onClick={() => void act.run(async () => {
                      await send("POST", `${base}:retry-preview`, { idempotency_key: key(),
                        items: [{ item_id: active.id, expected_item_revision: active.revision }] });
                      reload();
                    })}>Retry preview</button></div>)}
              {view?.status === "failed" && <div className="banner bad" style={{ margin: "0 12px" }}>
                {view.validation.failed_stage ?? "build"} failed ({view.validation.failure_code}): {view.error}
                {view.artifacts.raw ? " · the raw TRELLIS.2 output is kept: building again or re-exporting reuses it." : ""}</div>}
              {active.build_history.length > 1 && (
                <div style={{ padding: "8px 12px", display: "flex", flexDirection: "column", gap: 4 }}>
                  <span className="label">Build history</span>
                  {active.build_history.slice().reverse().map((h) => (
                    <div key={h.id} className="row" style={{ gap: 8, fontSize: 12 }}>
                      <span className="mono" style={{ fontSize: 11 }}>{h.id.slice(-8)}</span>
                      <span className="sub">{h.kind}{h.derived_from ? ` of ${h.derived_from.slice(-6)}` : ""}</span>
                      <span className="sub" style={{ color: h.result === "valid" ? OK : h.status === "failed" ? BAD : INFO }}>
                        {h.status === "succeeded" ? h.result : h.status}</span>
                      {h.accepted && <span className="pill ok">accepted</span>}
                      <span className="grow" />
                      {h.has_raw && <button className="btn-link" disabled={!job.recipe.build_available}
                        onClick={() => setReexporting(h.id)}>re-export</button>}
                    </div>))}
                </div>)}
              <div className="row" style={{ padding: "9px 12px", flexWrap: "wrap" }}>
                <button className="btn" disabled={!view?.artifacts.raw || !job.recipe.build_available || act.busy}
                  title={view?.artifacts.raw ? "New GLB from the stored TRELLIS.2 output" : "Only 3D builds keep a raw intermediate"}
                  onClick={() => setReexporting(view?.id ?? null)}>Re-export…</button>
                <button className="btn" disabled={!active.legal.mark_regenerate} onClick={() => nav(`/p/${id}/jobs/${job.id}/approve?item=${active.id}`)}>
                  Back to candidates</button>
              </div>
            </aside>
          )}
        </div>
      )}
      {reexporting && active && <ReexportDialog base={base} itemId={active.id} runId={reexporting}
        revision={active.revision} onClose={() => setReexporting(null)} onDone={() => { setReexporting(null); reload(); }} />}
      <ErrorLine error={act.error} />
      <ActionBar note={`${built.length} built · ${accepted.length} accepted`}
        sub="Only accepted, structurally valid results can be published. Rejected results keep their candidate set.">
        <button className="btn btn-primary" disabled={!accepted.length} onClick={() => nav(`/p/${id}/jobs/${job.id}/publish`)}>
          Go to publish →</button>
      </ActionBar>
    </div>
  );
}
