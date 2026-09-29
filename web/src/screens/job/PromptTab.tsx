import { useEffect, useRef, useState } from "react";

import { Dialog, ErrorLine } from "../../components/ui";
import { type CandidateView, type Round, P, type AssetDetail } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import * as act from "./jobActions";
import { ActionError, isApprovedHere, transformSentence, typing } from "./jobModel";
import type { TabProps } from "./JobWorkspace";
import { promptView, PromptPanel, ProvenanceCard } from "./PromptPanel";
import { RoundsPanel } from "./RoundsPanel";

interface Pending { round: Round; cand: CandidateView; build: boolean }

/** Preview artifact of the variant plan's source version (for the provenance card and the QA comparison). */
function useSourceThumb(variant: TabProps["job"]["variant"]): string | null {
  const { id } = useProject();
  const d = useApi<AssetDetail>(variant ? `${P(id)}/assets/${variant.source_asset_id}?version=${variant.source_version_id}` : null,
    { project: id });
  const files = d.data?.files ?? [];
  return (files.find((f) => f.role === "preview") ?? files.find((f) => f.role === "image")
    ?? files.find((f) => f.mime.startsWith("image/")))?.artifact_id ?? null;
}

function DirectCard({ job, item, reload, goBuild }: Pick<TabProps, "job" | "item" | "reload"> & { goBuild: () => void }) {
  const { id } = useProject();
  const a = useAction();
  const v = job.variant;
  const three = job.kind === "model3d";
  const row = job.variant_row;
  const t = row?.glb_transform ?? row?.raster_transform ?? (item.approval_detail?.bound.transform as Record<string, never> | undefined);
  const info = `${t ? transformSentence(t as unknown as Record<string, never>) : `Transforms the source ${three ? "GLB" : "image"}${v ? ` (${v.source_name} v${v.source_display_version})` : ""} deterministically.`}`
    + ` ${three ? "Topology, UVs and materials are kept." : "Pixels are resampled, nothing is regenerated."} No prompt or candidates.`;
  return (
    <div className="jw-box" style={{ display: "flex", flexDirection: "column", gap: 8, padding: 12 }}>
      <span style={{ fontWeight: 500 }}>Direct size transform</span>
      <span className="muted" style={{ fontSize: 12 }}>{info}</span>
      {item.legal.run_transform && (
        <button className="btn btn-primary jw-cta" disabled={a.busy}
          onClick={() => void a.run(async () => { await act.startTransform(id, job.id, item.id); reload(); goBuild(); })}>Run transform</button>)}
      <button className="btn jw-cta" onClick={goBuild}>Go to transform →</button>
      <ErrorLine error={a.error} />
    </div>
  );
}

export function PromptTab({ job, item, reload, goTab }: TabProps) {
  const { id } = useProject();
  const a = useAction();
  const area = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [viewN, setViewN] = useState<number | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [reason, setReason] = useState("");
  const thumb = useSourceThumb(job.variant);
  const pv = promptView(item, draft);
  const rounds = item.rounds;
  const found = viewN == null ? -1 : rounds.findIndex((r) => r.number === viewN);
  const ri = found >= 0 ? found : rounds.length - 1;
  const round = rounds[ri];
  const approvedIdx = round && item.approved?.candidate_set_id === round.candidate_set_id
    ? round.candidates.findIndex((c) => c.id === item.approved?.candidate_id) : -1;
  const focusIdx = focus != null && focus < (round?.candidates.length ?? 0) ? focus : Math.max(approvedIdx, 0);

  const follow = () => { setViewN(null); setFocus(null); };
  const approveNow = (r: Round, c: CandidateView, override: string | null, build: boolean) => a.run(async () => {
    try {
      if (!isApprovedHere(item, r, c)) await act.approve(id, job.id, item.id, r, c, override === null ? null : { reason: override });
      if (build) await act.startBuild(id, job.id, item.id, "build");
    } catch (e) {
      if (e instanceof ActionError && e.code === "override_required") { setReason(""); setPending({ round: r, cand: c, build }); return; }
      throw e;
    }
    reload();
    if (build) goTab("build");
  });
  const request = (r: Round, c: CandidateView, build: boolean) => {
    if (a.busy) return;
    if (isApprovedHere(item, r, c)) {
      if (build) void approveNow(r, c, null, true);
      else void a.run(async () => { await act.clearApproval(id, job.id, item.id); reload(); });
    } else if (c.qa?.status === "recommended") void approveNow(r, c, null, build);
    else { setReason(""); setPending({ round: r, cand: c, build }); }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e) || e.metaKey || e.ctrlKey || e.altKey || document.querySelector("[role=dialog]") || !round) return;
      if (/^[1-8]$/.test(e.key)) {
        const c = round.candidates[Number(e.key) - 1];
        if (c && !round.generating && item.legal.approve) { setFocus(c.index); request(round, c, false); }
      } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const next = Math.min(rounds.length - 1, Math.max(0, ri + (e.key === "ArrowRight" ? 1 : -1)));
        setViewN(rounds[next]?.number ?? null);
        setFocus(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const v = job.variant;
  return (
    <div className="jw-two">
      <div className="jw-col">
        {v && <ProvenanceCard job={job} v={v} thumb={thumb} />}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span className="label">Brief</span>
          <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>{item.brief || "—"}</span>
        </div>
        {job.direct && <DirectCard job={job} item={item} reload={reload} goBuild={() => goTab("build")} />}
        {!job.direct && <PromptPanel job={job} item={item} pv={pv} draft={draft} setDraft={setDraft} reload={reload} area={area}
          onGenerated={follow} />}
      </div>
      {!job.direct && (
        <RoundsPanel job={job} item={item} ri={ri} focusIdx={focusIdx} sourceThumb={thumb} busy={a.busy} error={a.error}
          onRound={(i) => { setViewN(rounds[i]?.number ?? null); setFocus(null); }} onFocus={setFocus}
          onApprove={(r, c) => request(r, c, false)} onApproveBuild={(r, c) => request(r, c, true)} />)}
      {pending && (
        <Dialog title={`Approve R${pending.round.number} #${pending.cand.index + 1} anyway?`} onClose={() => setPending(null)}>
          <div className="banner bad">QA is advisory. This candidate is <b>{pending.cand.qa?.status.replace("_", " ") ?? "unchecked"}</b>.
            The failed and missing checks are recorded with the approval.</div>
          <div className="sub">failed: {[...(pending.cand.qa?.policy.failed_major ?? []), ...(pending.cand.qa?.policy.failed_minor ?? [])].join(", ") || "—"}
            {" · "}unavailable: {pending.cand.qa?.policy.unavailable.join(", ") || "—"}</div>
          <label className="field"><span>Reason (optional)</span>
            <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <div className="row">
            <button className="btn btn-primary" onClick={() => { const p = pending; setPending(null); void approveNow(p.round, p.cand, reason, p.build); }}>
              Approve anyway</button>
            <button className="btn-link" onClick={() => setPending(null)}>Cancel</button>
          </div>
        </Dialog>)}
    </div>
  );
}
