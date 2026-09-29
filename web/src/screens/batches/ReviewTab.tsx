import { useState } from "react";
import { Link } from "react-router-dom";

import { INFO, OK, WARN } from "../../components/progress";
import { Box, Dialog, ErrorLine, QaPill } from "../../components/ui";
import { artifactUrl, type CandidateView, type ItemView, type JobDetail } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { getJob } from "../../lib/jobsApi";
import { type BatchCtx } from "./batchModel";
import { GHOST_BTN, PRIMARY_BTN, Card, MONO_SUB, Row } from "./batchUi";
import { reviewable, useJobDetails, usePublishRows } from "./batchData";
import { BuildsCard, buildRowsOf, PublishCard } from "./BuildSections";
import { editPrompts, post, previewBest } from "./reviewApi";

export interface RRow { it: ItemView; job: JobDetail }

const qaText = (c: CandidateView): string => c.qa?.status === "recommended" ? "recommended"
  : c.qa?.status === "not_recommended" ? "not recommended" : "unverified";
const qaTone = (c: CandidateView): string => c.qa?.status === "recommended" ? OK : c.qa?.status === "not_recommended" ? "#e08a7c" : "#8b8c87";

function PromptsCard({ ctx, rows, run }: { ctx: BatchCtx; rows: RRow[]; run: (fn: () => Promise<void>) => void }) {
  const [off, setOff] = useState<Set<string>>(new Set());
  const [edits, setEdits] = useState<Record<string, string>>({});
  const chosen = rows.filter((r) => !off.has(r.it.id));
  const text = (r: RRow) => edits[r.it.id] ?? r.it.prompt?.description ?? "";
  const confirm = () => run(async () => {
    const dirty = chosen.filter((r) => text(r) !== (r.it.prompt?.description ?? ""));
    if (dirty.length) {
      await editPrompts(ctx.project, dirty.map((r) => ({ job_id: r.job.id, item_id: r.it.id,
        expected_item_revision: r.it.revision, description: text(r) })));
    }
    // Re-read so the confirmation binds the exact (possibly just edited) prompt revision.
    const fresh = new Map<string, ItemView>();
    for (const jid of new Set(chosen.map((r) => r.job.id))) {
      (await getJob(ctx.project, jid)).items.forEach((i) => fresh.set(i.id, i));
    }
    const units = chosen.flatMap((r) => {
      const i = fresh.get(r.it.id);
      return i?.current_prompt ? [{ job_id: r.job.id, item_id: i.id, prompt_revision_id: i.current_prompt,
        expected_item_revision: i.revision }] : [];
    });
    await post(ctx.project, ctx.active, "confirm", units);
    setEdits({});
  });
  return (
    <Card title="Prompts to confirm" n={rows.length}
      actions={<button className="btn btn-primary" style={PRIMARY_BTN} disabled={!chosen.length} onClick={confirm}>
        Confirm {chosen.length} selected + generate</button>}>
      {rows.map((r) => (
        <Row key={r.it.id} cols="30px 190px minmax(0,1fr) 70px" align="start">
          <Box on={!off.has(r.it.id)} label={`confirm ${r.it.name}`} onChange={(v) => {
            const n = new Set(off); if (v) n.delete(r.it.id); else n.add(r.it.id); setOff(n); }} />
          <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span style={{ fontWeight: 500 }}>{r.it.name}</span>
            <span style={MONO_SUB}>{r.job.alias} · {r.job.kind_label}</span>
            {r.it.prompt_stale && <span style={{ ...MONO_SUB, color: WARN }}>references changed: re-enhance in the Job</span>}
          </div>
          <textarea aria-label={`prompt for ${r.it.name}`} rows={3} value={text(r)} readOnly={!r.it.legal.edit_prompt}
            onChange={(e) => setEdits({ ...edits, [r.it.id]: e.target.value })}
            style={{ background: "#141517", border: "1px solid #2a2c2f", borderRadius: 6, color: "#dcdcd9", padding: "8px 10px",
              fontSize: 12.5, lineHeight: 1.5, resize: "vertical", width: "100%" }} />
          <Link to={`/p/${ctx.project}/jobs/${r.job.id}/prompts`} className="sub"
            style={{ textAlign: "right", color: "#a3a4a1", textDecoration: "none", fontFamily: "Geist, sans-serif", fontSize: 12 }}>Open →</Link>
        </Row>))}
    </Card>
  );
}

function CandidatesCard({ ctx, rows, run, note }:
  { ctx: BatchCtx; rows: RRow[]; run: (fn: () => Promise<void>) => void; note: (m: string | null) => void }) {
  const [pending, setPending] = useState<{ r: RRow; c: CandidateView } | null>(null);
  const [reason, setReason] = useState("");
  const unit = (r: RRow, c: CandidateView, override: boolean, why: string | null) => ({
    job_id: r.job.id, item_id: r.it.id, expected_item_revision: r.it.revision, candidate_set_id: r.it.candidate_set!.id,
    candidate_id: c.id, image_sha256: c.sha256, prompt_revision_id: r.it.candidate_set!.prompt_revision_id,
    qa_evaluation_id: c.qa?.id ?? null, override_qa: override, override_reason: why });
  const pick = (r: RRow, c: CandidateView) => {
    if (c.qa?.status === "recommended") run(() => post(ctx.project, ctx.active, "approve", [unit(r, c, false, null)]));
    else { setReason(""); setPending({ r, c }); }
  };
  const bulk = () => run(async () => {
    note(null);
    const props = await previewBest(ctx.project, ctx.active, rows.map((r) => ({ job_id: r.job.id, item_id: r.it.id })));
    const units = props.flatMap((p) => {
      const r = rows.find((x) => x.it.id === p.item_id);
      const c = r?.it.candidate_set?.candidates.find((x) => x.id === p.candidate_id);
      return r && c ? [unit(r, c, false, null)] : [];
    });
    if (!units.length) { note("No candidate is recommended yet, so nothing was approved. Click a candidate to approve it."); return; }
    await post(ctx.project, ctx.active, "approve", units);
    if (units.length < rows.length) note(`Approved ${units.length} of ${rows.length}; the rest have no recommended candidate.`);
  });
  return (
    <Card title="Candidates to approve" n={rows.length} note="Click a candidate to approve it. Open the Job to iterate."
      actions={<button className="btn" style={GHOST_BTN} onClick={bulk}>Approve best recommended in {rows.length}</button>}>
      {rows.map((r) => {
        const set = r.it.candidate_set!;
        return (
          <Row key={r.it.id} cols="190px minmax(0,1fr) 70px">
            <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
              <span style={{ fontWeight: 500 }}>{r.it.name}</span>
              <span style={MONO_SUB}>{r.job.alias} · R{set.number}{r.it.rounds.length > 1 ? ` · ${r.it.rounds.length} rounds` : ""}</span>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {set.candidates.map((c) => (
                <button key={c.id} onClick={() => pick(r, c)} aria-label={`approve ${r.it.name} candidate ${c.index + 1}, ${qaText(c)}`}
                  style={{ width: 76, display: "flex", flexDirection: "column", gap: 4, background: "none", border: 0, padding: 0, cursor: "pointer" }}>
                  <span className="stripes" style={{ width: 76, height: 76, borderRadius: 6, position: "relative", overflow: "hidden", display: "block" }}>
                    <img src={artifactUrl(ctx.project, c.artifact_id)} alt="" style={{ width: 76, height: 76, objectFit: "cover", display: "block" }} />
                    <span style={{ position: "absolute", left: 5, top: 4, font: "600 10.5px 'Geist Mono', monospace",
                      textShadow: "0 0 3px #000" }}>{c.index + 1}</span>
                    <span aria-hidden style={{ position: "absolute", right: 5, top: 6, width: 8, height: 8, borderRadius: "50%",
                      background: qaTone(c) }} /></span>
                  <span style={{ font: "500 10px 'Geist Mono', monospace", color: qaTone(c), whiteSpace: "nowrap", overflow: "hidden",
                    textOverflow: "ellipsis", textAlign: "left" }}>{qaText(c)}</span>
                </button>))}
            </div>
            <Link to={`/p/${ctx.project}/jobs/${r.job.id}`} className="sub"
              style={{ textAlign: "right", color: "#a3a4a1", textDecoration: "none", fontFamily: "Geist, sans-serif", fontSize: 12 }}>Open →</Link>
          </Row>);
      })}
      {pending && (
        <Dialog title="Approve without a QA recommendation?" onClose={() => setPending(null)}>
          <span style={{ fontSize: 12.5 }}>{pending.r.it.name} · candidate {pending.c.index + 1} is <QaPill status={pending.c.qa?.status} />.
            The approval is recorded as an explicit QA override.</span>
          <input className="input" aria-label="override reason" placeholder="Reason (optional)" value={reason}
            onChange={(e) => setReason(e.target.value)} />
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => setPending(null)}>Cancel</button>
            <button className="btn btn-primary" onClick={() => {
              const p = pending; setPending(null);
              run(() => post(ctx.project, ctx.active, "approve", [unit(p.r, p.c, true, reason.trim() || null)]));
            }}>Approve anyway</button></div>
        </Dialog>)}
    </Card>
  );
}

export function ReviewTab({ ctx }: { ctx: BatchCtx }) {
  const ids = ctx.jobs.filter(reviewable).map((j) => j.id);
  const { details, error, reload } = useJobDetails(ctx.project, ids);
  const act = useAction();
  const [msg, setMsg] = useState<string | null>(null);
  const rows: RRow[] = details.flatMap((job) => job.items.filter((it) => !it.cancelled && !it.published).map((it) => ({ it, job })));
  const pubJobs = details.filter((d) => d.items.some((i) => i.legal.publish)).map((d) => d.id);
  const bump = details.map((d) => d.items.map((i) => i.revision).join(".")).join("|");
  const pub = usePublishRows(ctx.project, pubJobs, bump);
  const run = (fn: () => Promise<void>) => void act.run(async () => {
    try { await fn(); } finally { reload(); ctx.reload(); }
  });
  const prompts = rows.filter((r) => !r.job.direct && r.it.legal.confirm && r.it.prompt);
  const cands = rows.filter((r) => !r.job.direct && r.it.legal.approve && r.it.candidate_set && !r.it.approval && !r.it.stage.busy);
  const builds = buildRowsOf(rows);
  const empty = !prompts.length && !cands.length && !builds.length && !pub.rows.length;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <ErrorLine error={act.error ?? error ?? pub.error} />
      {msg && <div role="status" style={{ fontSize: 12.5, color: INFO }}>{msg}</div>}
      {empty && <div className="empty">{ctx.jobs.length && ctx.stats.drafts === ctx.jobs.length
        ? "Nothing to review yet. Start the Batch to enhance every prompt." : "Nothing waiting on you in this Batch."}</div>}
      {prompts.length > 0 && <PromptsCard ctx={ctx} rows={prompts} run={run} />}
      {cands.length > 0 && <CandidatesCard ctx={ctx} rows={cands} run={run} note={setMsg} />}
      {builds.length > 0 && <BuildsCard ctx={ctx} rows={builds} run={run} />}
      {pub.rows.length > 0 && <PublishCard ctx={ctx} rows={pub.rows} jobs={details} run={run} />}
    </div>
  );
}
