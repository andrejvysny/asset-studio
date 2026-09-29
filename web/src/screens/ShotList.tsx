import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Box, Empty, ErrorLine, INFO, Loading, OK, PageHead, Toggle } from "../components/ui";
import { type CategoryNode, J, key, type Kind, KIND_LABEL, KINDS, P, send, type ShotRow, upload, V2 } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { useProject } from "../lib/project";

interface Draft { id: string | null; name: string; category_id: string | null; kind: Kind | null; brief: string;
  priority: "low" | "med" | "high"; notes: string; target_asset_id: string | null; external_id: string | null;
  archived: boolean }
interface ImportPreview { preview_id: string; filename: string; format: string; columns: string[];
  mapping: Record<string, string | null>; errors: string[]; revision: number;
  rows: { line: number; values: Record<string, string>; errors: string[]; default_action: string;
    matches_id: string | null; possible_duplicate_of: string | null }[] }

const toDraft = (s: ShotRow): Draft => ({ id: s.id, name: s.name, category_id: s.category_id, kind: s.kind,
  brief: s.brief, priority: s.priority, notes: s.notes, target_asset_id: s.target_asset_id,
  external_id: s.external_id, archived: s.archived });

export function ShotList() {
  const { id } = useProject();
  const nav = useNavigate();
  const shots = useApi<{ revision: number; items: ShotRow[] }>(`${P(id)}/shot-list`, { project: id });
  const cats = useApi<{ categories: CategoryNode[] }>(`${P(id)}/categories`, { project: id });
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [imp, setImp] = useState<ImportPreview | null>(null);
  const [asBatch, setAsBatch] = useState(true);
  const [actions, setActions] = useState<Record<string, string>>({});
  const act = useAction();
  const dirty = drafts !== null;
  useEffect(() => { if (!dirty) setSel(new Set()); }, [shots.data, dirty]);

  const rows = shots.data?.items ?? [];
  const catLabel = (c: string | null) => cats.data?.categories.find((x) => x.id === c)?.path ?? "—";
  const selected = rows.filter((r) => sel.has(r.id));

  /** One Job per selected row (any kind); optionally group the new Jobs into one Batch. Save only, nothing runs. */
  const createJobs = () => void act.run(async () => {
    const ids: string[] = [];
    const failed: string[] = [];
    for (const r of selected) {
      try {
        const out = await send<{ job: { id: string } }>("POST", J(id), {
          title: r.name, idempotency_key: key(), kind: r.effective_kind, category_id: r.category_id, source: "shot list",
          items: [{ name: r.name, brief: r.brief, category_id: r.category_id, kind: r.kind, shot_id: r.id,
            target_asset_id: r.target_asset_id }] });
        ids.push(out.job.id);
      } catch (e) {
        failed.push(`${r.name}: ${(e as Error).message}`);
      }
    }
    if (failed.length) {
      shots.reload();
      throw new Error(`${ids.length} Job(s) created, ${failed.length} failed:\n${failed.join("\n")}`);
    }
    if (asBatch && ids.length > 1) {
      const b = await send<{ batch: { id: string } }>("POST", `${V2(id)}/batches`, {
        title: `Shot list · ${ids.length} Jobs`, job_ids: ids, idempotency_key: key() });
      nav(`/p/${id}/batches/${b.batch.id}`);
      return;
    }
    nav(`/p/${id}/jobs`);
  });

  if (!shots.data) return shots.error ? <div className="content"><ErrorLine error={shots.error} /></div> : <Loading what="shot list" />;
  const eligible = selected.length > 0;
  const cols = "34px minmax(150px,1fr) minmax(140px,0.9fr) 110px minmax(240px,2fr) 70px 150px";
  return (
    <div className="content narrow" style={{ maxWidth: 1320 }}>
      <PageHead sub={`shotlist.yaml · revision ${shots.data.revision} · ${rows.filter((r) => r.status === "planned").length} planned`}
        title="Shot list">
        <label className="btn" style={{ cursor: "pointer" }}>Import CSV / YAML / Markdown
          <input type="file" accept=".csv,.yaml,.yml,.md" hidden onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void act.run(async () => { setImp(await upload<ImportPreview>(`${P(id)}/shot-list:preview-import`, f)); setActions({}); });
          }} /></label>
        {!dirty ? <button className="btn" onClick={() => setDrafts(rows.map(toDraft))}>Edit rows</button> : <>
          <button className="btn" onClick={() => setDrafts(null)}>Discard changes</button>
          <button className="btn btn-primary" disabled={act.busy} onClick={() => void act.run(async () => {
            await send("PUT", `${P(id)}/shot-list`, { expected_revision: shots.data?.revision, items: drafts });
            setDrafts(null);
            shots.reload();
          })}>Save shot list</button></>}
        {!dirty && <label className="row" style={{ gap: 7, fontSize: 12.5, color: "var(--text-2)" }}>
          <Toggle on={asBatch} onChange={setAsBatch} label="Group into a new Batch" />Group into a new Batch</label>}
        {!dirty && <button className="btn btn-primary" disabled={!eligible || act.busy} onClick={createJobs}>
          {selected.length === 0 ? "Select rows" : `Create ${selected.length} Job${selected.length > 1 ? "s" : ""}${
            asBatch && selected.length > 1 ? " + Batch" : ""}`}</button>}
      </PageHead>
      {imp && (
        <div className="panel" style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="row"><span className="mono">{imp.filename}</span>
            <span className="sub">{imp.format} · {imp.rows.length} rows · {imp.columns.length} columns</span></div>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {Object.entries(imp.mapping).map(([c, f]) => (
              <span key={c} className="tag">{c} → <span style={{ color: f ? OK : "var(--faint)" }}>{f ?? "ignored"}</span></span>
            ))}
          </div>
          {imp.errors.map((e) => <div key={e} className="error">{e}</div>)}
          <div className="table" style={{ maxHeight: 280 }}>
            {imp.rows.map((r) => {
              const action = actions[String(r.line)] ?? r.default_action;
              return (
                <div key={r.line} className="td" style={{ gridTemplateColumns: "60px minmax(120px,1fr) minmax(200px,2fr) 170px" }}>
                  <span className="sub">line {r.line}</span>
                  <span>{r.values.name ?? "—"}</span>
                  <span className={r.errors.length ? "error" : "sub"}>{r.errors.join("; ") ||
                    (r.possible_duplicate_of ? "same name as an existing row: choose create or update" : r.matches_id ? "matches existing id" : "ok")}</span>
                  <select className="input" value={action} disabled={r.errors.length > 0} aria-label={`action line ${r.line}`}
                    onChange={(e) => setActions({ ...actions, [String(r.line)]: e.target.value })}>
                    <option value="create">create</option>
                    {(r.matches_id || r.possible_duplicate_of) && <option value="update">update existing</option>}
                    <option value="skip">skip</option>
                  </select>
                </div>
              );
            })}
          </div>
          <div className="row">
            <button className="btn btn-primary" disabled={act.busy || imp.errors.length > 0} onClick={() => void act.run(async () => {
              await send("POST", `${P(id)}/shot-list:commit-import`, { preview_id: imp.preview_id,
                expected_revision: imp.revision, actions });
              setImp(null);
              shots.reload();
            })}>Import {imp.rows.filter((r) => (actions[String(r.line)] ?? r.default_action) !== "skip").length} rows</button>
            <button className="btn-link" onClick={() => setImp(null)}>Cancel</button>
            <span className="sub">Rows with errors are skipped. Nothing is changed until you import.</span>
          </div>
        </div>
      )}
      <ErrorLine error={act.error} />
      {rows.length === 0 && !dirty ? <Empty>The shot list is empty. Import a file or use “Edit rows”.</Empty> : (
        <div className="table">
          <div className="th" style={{ gridTemplateColumns: cols, minWidth: 960 }}>
            {dirty ? <span /> : <Box on={sel.size > 0 && sel.size === rows.filter((r) => r.status === "planned").length}
              label="select all planned" onChange={(v) => setSel(new Set(v ? rows.filter((r) => r.status === "planned").map((r) => r.id) : []))} />}
            <span>Name</span><span>Category</span><span>Type</span><span>Brief</span><span>Prio</span><span>Status</span>
          </div>
          {dirty ? drafts.map((d, i) => {
            const upd = (patch: Partial<Draft>) => setDrafts(drafts.map((x, j) => (j === i ? { ...x, ...patch } : x)));
            return (
              <div key={d.id ?? `new-${i}`} className="td" style={{ gridTemplateColumns: cols, minWidth: 960 }}>
                <button className="btn-link" aria-label="remove row" onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}>✕</button>
                <input className="input" value={d.name} aria-label="name" onChange={(e) => upd({ name: e.target.value })} />
                <select className="input" value={d.category_id ?? ""} aria-label="category"
                  onChange={(e) => upd({ category_id: e.target.value || null })}>
                  <option value="">—</option>
                  {(cats.data?.categories ?? []).map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
                </select>
                <select className="input" value={d.kind ?? ""} aria-label="kind" onChange={(e) => upd({ kind: (e.target.value || null) as Kind | null })}>
                  <option value="">from category</option>
                  {KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
                </select>
                <input className="input" value={d.brief} aria-label="brief" onChange={(e) => upd({ brief: e.target.value })} />
                <select className="input" value={d.priority} aria-label="priority" onChange={(e) => upd({ priority: e.target.value as Draft["priority"] })}>
                  <option>low</option><option>med</option><option>high</option></select>
                <span className="sub">{d.id ? "saved row" : "new"}</span>
              </div>
            );
          }) : rows.map((r) => {
            const on = sel.has(r.id);
            const planned = r.status === "planned";
            const toggle = () => { if (!planned) return; const n = new Set(sel); if (on) n.delete(r.id); else n.add(r.id); setSel(n); };
            return (
              <div key={r.id} className={`td${planned ? " clickable" : ""}${on ? " sel" : ""}`}
                style={{ gridTemplateColumns: cols, minWidth: 960 }} onClick={toggle}>
                {planned ? <Box on={on} label={`select ${r.name}`} onChange={toggle} /> : <span />}
                <span style={{ fontWeight: 500 }}>{r.name}</span>
                <span className="mono muted" style={{ fontSize: 11.5 }}>{catLabel(r.category_id)}</span>
                <span className="mono muted" style={{ fontSize: 11 }}>{r.effective_kind ? KIND_LABEL[r.effective_kind] : "?"}</span>
                <span className="muted" style={{ fontSize: 12 }}>{r.brief}</span>
                <span className="sub">{r.priority}</span>
                <span className="sub" style={{ color: r.status === "planned" ? "var(--dim)" : r.status === "published" ? OK : INFO }}>
                  {r.status === "in_batch" ? `job ${r.membership?.batch_alias}` : r.status}</span>
              </div>
            );
          })}
          {dirty && <div className="td"><button className="btn" onClick={() => setDrafts([...drafts, { id: null, name: "",
            category_id: null, kind: null, brief: "", priority: "med", notes: "", target_asset_id: null, external_id: null,
            archived: false }])}>+ Add row</button></div>}
        </div>
      )}
      <div className="dim" style={{ fontSize: 12 }}>Each selected row becomes its own Job; asset types can be mixed. Rows that
        already have a Job are skipped. Creating Jobs never starts generation.</div>
    </div>
  );
}
