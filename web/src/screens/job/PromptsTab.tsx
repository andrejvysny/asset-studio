import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Box, ErrorLine, taskColor } from "../../components/ui";
import { get, J, type JobDetail, key, send } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { ActionBar, type TabProps } from "./JobWorkspace";

export function PromptsTab({ job, reload }: TabProps) {
  const { id } = useProject();
  const nav = useNavigate();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // Track deselections, so items that finish enhancing later are selected by default.
  const [unsel, setUnsel] = useState<Set<string>>(new Set());
  const [append, setAppend] = useState("");
  const act = useAction();
  const base = `${J(id)}/${job.id}`;
  const open = job.items.filter((i) => i.legal.edit_prompt || i.legal.enhance);
  const edits = Object.entries(drafts).filter(([iid, text]) => text !== job.items.find((i) => i.id === iid)?.prompt?.description);
  const selectable = job.items.filter((i) => i.legal.confirm);
  const chosen = selectable.filter((i) => !unsel.has(i.id));
  const sel = new Set(chosen.map((i) => i.id));
  const setSel = (next: Set<string>) => setUnsel(new Set(selectable.filter((i) => !next.has(i.id)).map((i) => i.id)));

  const saveEdits = async () => {
    if (!edits.length) return;
    const res = await send<{ results: { item_id: string; ok: boolean; message?: string }[] }>("POST", `${base}:edit-prompts`, {
      items: edits.map(([iid, description]) => ({ item_id: iid, description,
        expected_item_revision: job.items.find((i) => i.id === iid)!.revision })) });
    const bad = res.results.filter((r) => !r.ok);
    if (bad.length) throw new Error(bad.map((r) => r.message).join("; "));
    setDrafts({});
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {open.length === 0 && <div className="banner note">Prompts are locked because candidates exist. To change one, use
        “Regenerate” on its row in Approve; it gets a new prompt revision and a new candidate set.</div>}
      {open.length > 0 && (
        <div className="row panel" style={{ padding: "10px 12px", flexWrap: "wrap", background: "var(--panel)" }}>
          <span className="muted" style={{ fontSize: 12 }}>Append to selected prompts</span>
          <input className="input grow" value={append} onChange={(e) => setAppend(e.target.value)} aria-label="append text"
            placeholder="e.g. weathered, soft rim light" style={{ minWidth: 200, fontFamily: "var(--sans)" }} />
          <button className="btn" disabled={!append.trim() || chosen.length === 0} onClick={() => {
            const next = { ...drafts };
            chosen.forEach((i) => { next[i.id] = `${(next[i.id] ?? i.prompt?.description ?? "").replace(/[ .,]+$/, "")}, ${append.trim()}`; });
            setDrafts(next);
          }}>Apply to {chosen.length}</button>
          <button className="btn" disabled={act.busy || chosen.length === 0} onClick={() => void act.run(async () => {
            await send("POST", `${base}:enhance`, { item_ids: chosen.map((i) => i.id), idempotency_key: key() });
            setDrafts({});
            reload();
          })}>Re-enhance selected</button>
          <button className="btn-link" onClick={() => setSel(sel.size === selectable.length ? new Set() : new Set(selectable.map((i) => i.id)))}>
            {sel.size === selectable.length ? "Select none" : "Select all"}</button>
        </div>
      )}
      <div className="table">
        {job.items.map((it) => {
          const editable = it.legal.edit_prompt;
          const text = drafts[it.id] ?? it.prompt?.description ?? "";
          const enh = it.tasks.enhance;
          return (
            <div key={it.id} className="td" style={{ gridTemplateColumns: "30px 190px minmax(0,1fr) 130px", alignItems: "start" }}>
              {it.legal.confirm ? <Box on={sel.has(it.id)} label={`select ${it.name}`} onChange={(v) => {
                const n = new Set(sel); if (v) n.add(it.id); else n.delete(it.id); setSel(n); }} /> : <span />}
              <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                <span style={{ fontWeight: 500 }}>{it.name}</span>
                <span className="dim" style={{ fontSize: 11.5 }}>{it.brief}</span>
              </div>
              {it.prompt ? (
                <textarea className="input" rows={3} readOnly={!editable} value={text} aria-label={`prompt ${it.name}`}
                  onChange={(e) => setDrafts({ ...drafts, [it.id]: e.target.value })}
                  style={{ fontSize: 12.5, width: "100%", opacity: editable ? 1 : 0.8 }} />
              ) : (
                <div className="sub" style={{ padding: 10, border: "1px dashed var(--line-2)", borderRadius: 6,
                  color: enh?.state === "failed" || enh?.state === "blocked" ? "var(--bad)" : undefined }}>
                  {enh?.state === "failed" || enh?.state === "blocked" ? `enhancement ${enh.state}: ${enh.error ?? ""}` : "enhancing…"}</div>
              )}
              <span className="sub" style={{ textAlign: "right", color: taskColor(enh?.state) }}>
                {drafts[it.id] !== undefined && drafts[it.id] !== it.prompt?.description ? "unsaved edit"
                  : it.prompt_locked ? `locked · rev ${it.prompt?.number}` : it.prompt_confirmed ? "confirmed" : it.stage.state}</span>
            </div>
          );
        })}
      </div>
      <div className="muted" style={{ fontSize: 12 }}>Every prompt also gets the recipe's locked technical constraints:{" "}
        <span className="mono" style={{ color: "var(--text-2)" }}>{job.locked_template || "(none for this recipe)"}</span></div>
      <ErrorLine error={act.error} />
      <ActionBar note={open.length ? `${chosen.length} of ${selectable.length} ready prompts selected${edits.length ? ` · ${edits.length} unsaved edits` : ""}`
        : "Prompts confirmed."} sub={job.recipe.generation_available ? `${job.counts.items} items · candidates per item from the recipe`
          : `generation unavailable: ${job.recipe.generation_blocked_reason}`}>
        {edits.length > 0 && <button className="btn" disabled={act.busy} onClick={() => void act.run(async () => { await saveEdits(); reload(); })}>
          Save {edits.length} edits</button>}
        {open.length ? (
          <button className="btn btn-primary" disabled={act.busy || chosen.length === 0 || !job.recipe.generation_available}
            onClick={() => void act.run(async () => {
              await saveEdits();
              const fresh = await get<JobDetail>(base);
              const ids = new Set(chosen.map((i) => i.id));
              const res = await send<{ results: { ok: boolean; message?: string }[] }>("POST", `${base}:confirm-and-generate`, {
                idempotency_key: key(), items: fresh.items.filter((i) => ids.has(i.id) && i.current_prompt).map((i) => ({
                  item_id: i.id, prompt_revision_id: i.current_prompt, expected_item_revision: i.revision })) });
              const bad = res.results.filter((r) => !r.ok);
              reload();
              if (bad.length) throw new Error(bad.map((r) => r.message).join("; "));
              nav(`/p/${id}/jobs/${job.id}/candidates`);
            })}>Confirm {chosen.length} prompts + generate</button>
        ) : <button className="btn btn-primary" onClick={() => nav(`/p/${id}/jobs/${job.id}/approve`)}>Go to approve →</button>}
      </ActionBar>
    </div>
  );
}
