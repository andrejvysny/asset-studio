import { type RefObject, useState } from "react";
import { Link } from "react-router-dom";

import { ErrorLine, INFO, WARN } from "../../components/ui";
import { type EnhancePreset, type ItemView, type JobDetail, type VariantContext } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import * as act from "./jobActions";
import { FAINT, isBusy } from "./jobModel";
import { ReferencesPanel } from "./ReferencesPanel";

export interface PromptView {
  n: number; enhancing: boolean; generating: boolean; busy: boolean; hasPrompt: boolean; pending: boolean;
  text: string; edited: boolean; stale: boolean; label: string; tag: string; readOnly: boolean; dirty: boolean;
  progress: { done?: number; total?: number } | null;
}

/** Everything the left column needs to decide what to show, derived from the item and the local draft. */
export function promptView(item: ItemView, draft: string | null): PromptView {
  const rounds = item.rounds;
  const last = rounds[rounds.length - 1];
  const n = last?.number ?? 0;
  const generating = !!last?.generating || isBusy(item, "generate");
  const enhancing = isBusy(item, "enhance");
  const busy = generating || enhancing;
  const hasPrompt = !!item.prompt;
  const stored = item.prompt?.description ?? "";
  const edited = draft !== null && draft.trim() !== stored.trim();
  const pending = !!item.current_prompt && item.prompt_confirmed !== item.current_prompt;
  const stale = item.prompt_stale;
  const label = generating ? `Prompt · round ${n || 1} · locked`
    : enhancing ? `Prompt · round ${n + 1} · locked`
      : rounds.length === 0 ? (hasPrompt ? "Prompt · round 1 · confirm to generate" : "Prompt preview · enhanced on run")
        : `Prompt for round ${n + 1}`;
  const editedTag = edited || (pending && item.prompt?.origin === "edited");
  const tag = rounds.length === 0 ? (hasPrompt && editedTag ? "edited" : "")
    : editedTag ? "edited · not generated" : stale ? "refs changed" : "";
  return { n, enhancing, generating, busy, hasPrompt, pending, text: draft ?? stored, edited, stale, label, tag,
    readOnly: !hasPrompt || busy || !item.legal.edit_prompt, dirty: edited || (rounds.length > 0 && stale),
    progress: last?.generating ? last.progress ?? item.tasks.generate?.progress ?? null : item.tasks.generate?.progress ?? null };
}

/** The other preview slots' prompts (slot 1 is the main prompt above). Each is editable on its own. */
function VariantPrompts({ item, readOnly, onSave }:
  { item: ItemView; readOnly: boolean; onSave: (index: number, text: string) => void }) {
  const [open, setOpen] = useState(false);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <button className="jw-link" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Show"} the other {item.prompt_variants.length - 1} preview prompts</button>
      {open && item.prompt_variants.slice(1).map((v, k) => {
        const i = k + 1;
        const text = drafts[i] ?? v.description;
        const dirty = text.trim() !== v.description.trim();
        return (
          <div key={v.id} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span className="sub">Preview {i + 1} prompt{v.origin === "edited" ? " · edited" : ""}</span>
            <textarea className={`jw-textarea${dirty ? " dirty" : ""}`} rows={3} aria-label={`prompt for preview ${i + 1}`}
              value={text} readOnly={readOnly} onChange={(e) => setDrafts({ ...drafts, [i]: e.target.value })} />
            {dirty && !readOnly && <div className="row" style={{ gap: 8 }}>
              <button className="btn" onClick={() => { onSave(i, text.trim()); setDrafts(({ [i]: _, ...rest }) => rest); }}>Save prompt {i + 1}</button>
              <button className="btn-link" onClick={() => setDrafts(({ [i]: _, ...rest }) => rest)}>Discard</button></div>}
          </div>);
      })}
    </div>
  );
}

const methodLabel = { image_edit_reconstruct: "Structural reconstruction", image_edit: "Image edit",
  direct_transform: "Direct transform" } as const;
const intentLabel = { subtle: "Subtle", related: "Related", exploratory: "Exploratory" } as const;

export function ProvenanceCard({ job, v, thumb }: { job: JobDetail; v: VariantContext; thumb: string | null }) {
  const { id } = useProject();
  const three = job.kind === "model3d";
  const line = `${methodLabel[v.method]}${v.intent ? ` · ${intentLabel[v.intent]}` : ""}${v.final_height_m ? ` · height ${v.final_height_m} m` : ""}`;
  const badges = job.direct ? [`Same source ${three ? "mesh" : "image"} · direct transform`]
    : ["Source-conditioned new image", ...(three ? ["Reconstructed mesh · geometry not preserved"] : [])];
  return (
    <div className="jw-box" style={{ display: "grid", gridTemplateColumns: "64px minmax(0,1fr)", gap: 10 }}>
      {thumb ? <img src={`/api/v1/projects/${id}/artifacts/${thumb}/content`} alt="source preview" className="jw-thumb"
        style={{ width: 64, height: 64 }} /> : <div className="stripes" style={{ width: 64, height: 64, borderRadius: 5 }} />}
      <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
        <span className="sub">Created from</span>
        <Link to={`/p/${id}/assets/${v.source_asset_id}?version=${v.source_version_id}`} style={{ fontWeight: 500 }}>
          {v.source_name} · v{v.source_display_version}</Link>
        <span className="sub">{line}</span>
      </div>
      <div className="row" style={{ gridColumn: "1 / 3", gap: 5, flexWrap: "wrap" }}>
        {badges.map((b) => <span key={b} className="tag" style={{ fontSize: 10.5, color: "var(--text-2)" }}>{b}</span>)}
      </div>
    </div>
  );
}

interface Props {
  job: JobDetail; item: ItemView; pv: PromptView; draft: string | null; setDraft: (d: string | null) => void;
  reload: () => void; area: RefObject<HTMLTextAreaElement | null>; onGenerated: () => void;
}

/** Image mode: the source image is the first candidate; there is no prompt to review. */
function SourceImageCard({ job, item, reload }: { job: JobDetail; item: ItemView; reload: () => void }) {
  const { id } = useProject();
  const a = useAction();
  const src = item.source_image;
  const started = item.rounds.length > 0 || isBusy(item, "generate");
  const origin = src?.origin === "media" ? "Media Library" : src?.origin === "library" ? "library asset" : "upload";
  return (
    <div className="jw-col">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="label">Source image</span><span className="tag">from image</span></div>
      {src && <img src={`/api/v1/projects/${id}/artifacts/${src.artifact_id}/content`} alt="source image" className="jw-thumb"
        style={{ width: "100%", maxHeight: 260, objectFit: "contain" }} />}
      <span className="sub">{src?.label ?? "source image"} · {origin}
        {src?.library ? ` · ${src.library.asset_id}` : src?.media_id ? ` · ${src.media_id}` : ""}</span>
      <span className="muted" style={{ fontSize: 11.5 }}>Used as given. Prompt enhancement and preview generation are skipped:
        this image is round 1, goes through QA and approval, then the build step.</span>
      {!started && <button className="btn btn-primary jw-cta" disabled={a.busy || !!job.active_run}
        onClick={() => void a.run(async () => { await act.runEnhance(id, job.id); reload(); })}>Run · use source image</button>}
      <ErrorLine error={a.error} />
    </div>
  );
}

/** Prompt block, references and the run / confirm / iterate action stack (hidden for direct transforms). */
export function PromptPanel({ job, item, pv, draft, setDraft, reload, area, onGenerated }: Props) {
  const { id } = useProject();
  const a = useAction();
  const run = (fn: () => Promise<unknown>, after?: () => void) => void a.run(async () => { await fn(); reload(); after?.(); });
  if (item.generation_mode === "image") return <SourceImageCard job={job} item={item} reload={reload} />;
  const rounds = item.rounds.length;
  const enhanceFailed = item.tasks.enhance?.state === "failed" || item.tasks.enhance?.state === "blocked";
  const gen = job.recipe.generation_available;
  const locked = pv.busy || !item.legal.enhance;
  const refsChanged = pv.stale;
  const parts = [pv.edited || (pv.pending && !pv.stale) ? (pv.edited ? "edited prompt" : "new prompt") : "", refsChanged ? "new refs" : ""].filter(Boolean);
  const iterLabel = pv.dirty || pv.pending ? `Generate round ${pv.n + 1} · ${parts.join(" + ") || "edited prompt"}` : `Edit prompt or refs for round ${pv.n + 1}`;
  const iterReady = pv.dirty || pv.pending;
  const busyLine = pv.generating
    ? `Generating round ${pv.n} · ${pv.progress?.done ?? 0}${pv.progress?.total ? `/${pv.progress.total}` : ""}`
    : pv.enhancing ? "Enhancing prompt…" : "";
  return (
    <div className="jw-col">
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
          <span className="label">{pv.label}</span>
          <span className="sub" style={{ color: WARN }}>{pv.tag}</span>
        </div>
        <textarea ref={area} className={`jw-textarea${pv.dirty ? " dirty" : ""}`} rows={7} aria-label="prompt" value={pv.text}
          readOnly={pv.readOnly} placeholder={pv.hasPrompt ? "" : "The prompt is written from the brief when you press Run."}
          onChange={(e) => setDraft(e.target.value)} />
        {item.prompt_variants.length > 1 && (
          <VariantPrompts item={item} readOnly={pv.readOnly} onSave={(i, text) => run(() => act.editVariant(id, job.id, item.id, i, text))} />)}
        {enhanceFailed && <span className="error">Enhancement {item.tasks.enhance?.state}: {item.tasks.enhance?.error ?? ""}</span>}
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <div className="jw-seg" role="group" aria-label="enhancement preset">
            {(["conservative", "creative"] as EnhancePreset[]).map((p) => (
              <button key={p} className={item.enhance_preset === p ? "on" : ""} aria-pressed={item.enhance_preset === p} disabled={locked}
                onClick={() => { if (item.enhance_preset !== p) run(() => act.changePreset(id, job.id, item.id, p)); }}>
                {p === "conservative" ? "Conservative" : "Creative"}</button>
            ))}
          </div>
          <button className="jw-link" disabled={locked || a.busy} onClick={() => run(() => act.reEnhance(id, job.id, item.id), () => setDraft(null))}>
            Re-enhance</button>
          <span className="grow" />
          <button className="jw-link" style={{ textDecoration: "none", color: FAINT, font: "500 10.5px var(--mono)" }}
            title={job.locked_template || "(none for this recipe)"} aria-label={`locked parts: ${job.locked_template || "none"}`}>
            + locked parts</button>
        </div>
      </div>
      <ReferencesPanel item={item} jobId={job.id} locked={pv.busy || !!item.accepted_build} changed={refsChanged} reload={reload} />
      <div style={{ display: "flex", flexDirection: "column", gap: 8, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
        {rounds === 0 && !pv.hasPrompt && !pv.busy && (
          <button className="btn btn-primary jw-cta" disabled={a.busy || !item.legal.enhance || !!job.active_run}
            onClick={() => run(() => act.runEnhance(id, job.id))}>Run · enhance prompt</button>)}
        {rounds === 0 && pv.hasPrompt && !pv.busy && (
          <button className="btn btn-primary jw-cta" disabled={a.busy || !item.legal.confirm || !gen}
            onClick={() => run(() => act.confirmAndGenerate(id, job.id, item.id, draft), () => { setDraft(null); onGenerated(); })}>
            Confirm prompt + generate {job.candidate_count ? `${job.candidate_count} ` : ""}candidates</button>)}
        {rounds > 0 && !pv.busy && (
          <>
            <button className={`btn jw-cta${iterReady ? " btn-primary" : ""}`} disabled={a.busy || !gen || (iterReady && !item.legal.confirm && !item.legal.regenerate)}
              onClick={() => {
                if (!iterReady) { area.current?.focus(); return; }
                run(() => act.generateNextRound(id, job.id, item.id, draft), () => { setDraft(null); onGenerated(); });
              }}>{iterLabel}</button>
            <button className="btn jw-cta" disabled={a.busy || !gen || !item.legal.regenerate}
              onClick={() => run(() => act.regenerateSameSeeds(id, job.id, item.id), onGenerated)}>
              Regenerate · same prompt, new seeds</button>
          </>)}
        {busyLine && <div role="status" className="mono" style={{ padding: "8px 12px", border: "1px dashed var(--line-2)", borderRadius: 6,
          fontSize: 11.5, color: INFO }}>{busyLine}</div>}
        {!gen && <span className="error">generation unavailable: {job.recipe.generation_blocked_reason}</span>}
        <ErrorLine error={a.error} />
        <span className="muted" style={{ fontSize: 11.5 }}>
          {item.accepted_build ? "A build is accepted. Undo the acceptance in the build tab to change the prompt or the approval."
            : rounds === 0 ? (job.batch ? `This Job is in ${job.batch.id}. Running it here uses the same scheduler; the Batch then skips it.`
              : "Nothing runs until you press Run.")
              : "Every round is kept. You can approve a candidate from any round, then build it."}</span>
        {job.active_run && rounds === 0 && !pv.hasPrompt && <Link className="sub" to={`/p/${id}/runs/${job.active_run}`}>
          in run {job.active_run.slice(-6)} →</Link>}
      </div>
    </div>
  );
}
