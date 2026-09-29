import { useEffect, useState } from "react";

import { BAD, ErrorLine, NONE, OK, WARN } from "../../components/ui";
import {
  artifactUrl, type CandidateView, type CheckResult, type DiversityStatusView, type ItemView, type JobDetail, P, type Round,
} from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { compareSelection } from "../../lib/variantsApi";
import {
  candidateKey, checkColor, checkText, DIM, isApprovedHere, isRefCheck, isVariantCheck, pickLabel, qaText, VERB,
} from "./jobModel";

const VARIANT_NOTE: Record<string, string> = {
  variant_resemblance: "same identity as source", variant_single_object: "one complete isolated object",
  variant_style: "matches the project style",
};

function Dot({ color }: { color: string }) {
  return <span className="dot" style={{ background: color }} aria-hidden />;
}

function Row({ id, note, result, soft, reason }: { id: string; note: string; result: CheckResult["result"]; soft: boolean; reason?: string }) {
  const c = checkColor(result, soft);
  return (
    <div className="row" style={{ gap: 8, fontSize: 11.5, minWidth: 0 }} title={reason}>
      <Dot color={c} />
      <span className="mono">{id}</span>
      <span className="grow ellipsis" style={{ color: DIM }}>{note}</span>
      <span className="mono" style={{ color: c, fontSize: 10.5, fontWeight: 500 }}>{checkText(result)}</span>
    </div>
  );
}

/** Sibling-variant diversity of the plan's approved selection (advisory, computed on demand). */
function DiversityRow({ job }: { job: JobDetail }) {
  const { id } = useProject();
  const planId = job.variant?.plan_id ?? "";
  const div = useApi<DiversityStatusView>(planId ? `${P(id)}/variant-plans/${planId}/diversity` : null, { project: id });
  const a = useAction();
  const [waiting, setWaiting] = useState(false);
  const verdict = div.data?.status === "current" ? div.data.jobs[job.id] : undefined;
  useEffect(() => {
    if (!waiting) return;
    if (div.data?.status === "current") { setWaiting(false); return; }
    const t = window.setInterval(div.reload, 2000);
    const stop = window.setTimeout(() => setWaiting(false), 60000);
    return () => { window.clearInterval(t); window.clearTimeout(stop); };
  }, [waiting, div.data?.status, div.reload]);
  const reason = div.data?.report?.pairs.filter((p) => p.a === job.id || p.b === job.id).map((p) => p.reason).join("; ");
  if (verdict) return <Row id="diversity" note="distinct from sibling variants" result={verdict === "pass" ? "pass" : verdict === "fail" ? "fail" : "unavailable"} soft reason={reason} />;
  return (
    <div className="row" style={{ gap: 8, fontSize: 11.5, flexWrap: "wrap" }}>
      <Dot color={NONE} /><span className="mono">diversity</span>
      <span className="grow" style={{ color: DIM }}>{waiting ? "comparing…" : div.data?.status === "stale" ? "not compared yet · selection changed" : "not compared yet"}</span>
      <button className="btn" style={{ padding: "2px 9px", fontSize: 11.5 }} disabled={a.busy || waiting}
        onClick={() => void a.run(async () => { await compareSelection(id, planId); setWaiting(true); div.reload(); })}>
        Compare selected variants</button>
      <ErrorLine error={a.error} />
    </div>
  );
}

interface Props {
  job: JobDetail; item: ItemView; round: Round; cand: CandidateView; sourceThumb: string | null; busy: boolean;
  onApprove: () => void; onApproveBuild: () => void; error: string | null;
}

export function QaPanel({ job, item, round, cand, sourceThumb, busy, onApprove, onApproveBuild, error }: Props) {
  const { id } = useProject();
  const results = cand.qa?.results ?? [];
  const main = results.filter((r) => !isRefCheck(r.rule_id) && !isVariantCheck(r.rule_id));
  const refs = results.filter((r) => isRefCheck(r.rule_id));
  const vars = results.filter((r) => isVariantCheck(r.rule_id));
  const v = job.variant && !job.direct ? job.variant : null;
  const label = `R${round.number} #${candidateKey(cand)}`;
  const [status, color] = qaText(cand);
  const here = isApprovedHere(item, round, cand);
  const canApprove = item.legal.approve && !busy;
  const apLabel = here ? "Approved ✓ · click to undo" : item.approved ? `Switch approval to ${label}` : `Approve ${label}`;
  const verb = VERB[job.kind];
  return (
    <div className="jw-qa">
      <div>
        <span className="label">QA · {label} · advisory</span>
        {cand.qa && <span className="sub">coverage {cand.qa.coverage.completed}/{cand.qa.coverage.applicable}
          {cand.qa.not_evaluated ? " · no applicable checks" : ""}</span>}
        {!cand.qa && <span className="sub">QA has not finished for this candidate.</span>}
        <div className="jw-checks">
          {main.map((k) => (
            <div key={k.rule_id} className="jw-check" title={k.reason}>
              <Dot color={checkColor(k.result, false)} /><span className="id">{k.rule_id}</span>
              <span className="sev">{k.severity}</span>
              <span className="res" style={{ color: checkColor(k.result, false) }}>{checkText(k.result)}</span>
            </div>))}
          {cand.qa?.policy.disabled.map((d) => <div key={d} className="jw-check"><Dot color={NONE} />
            <span className="id" style={{ color: DIM }}>{d} · disabled</span></div>)}
        </div>
        {refs.length > 0 && (
          <div className="jw-sect">
            <span className="label">Against references</span>
            {refs.map((k) => {
              const i = Number(k.rule_id.slice(4));
              const r = item.references[i];
              return <Row key={k.rule_id} id={r?.label ?? k.rule_id} note={r?.note || "overall style"} result={k.result} soft reason={k.reason} />;
            })}
          </div>)}
        {v && (
          <div className="jw-sect" style={{ gap: 6 }}>
            <span className="label">Variant · vs source v{v.source_display_version}</span>
            <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
              <div style={{ position: "relative" }}>
                {sourceThumb ? <img className="jw-thumb" src={artifactUrl(id, sourceThumb)} alt="source" />
                  : <div className="stripes jw-thumb" />}
                <span className="sub" style={{ position: "absolute", left: 3, bottom: 3, fontSize: 9.5 }}>source</span>
              </div>
              <div style={{ position: "relative" }}>
                <img className="jw-thumb" src={artifactUrl(id, cand.artifact_id)} alt={`candidate ${label}`} style={{ border: "1px solid var(--line-3)" }} />
                <span className="sub" style={{ position: "absolute", left: 3, bottom: 3, fontSize: 9.5, color: "var(--text-2)" }}>R{round.number}#{candidateKey(cand)}</span>
              </div>
              <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
                {vars.map((k) => {
                  const short = k.rule_id.replace("variant_", "");
                  const note = k.rule_id === "variant_change" ? v.change_request || "requested change" : VARIANT_NOTE[k.rule_id] ?? k.reason;
                  return <Row key={k.rule_id} id={short} note={note} result={k.result} soft reason={k.reason} />;
                })}
                {vars.length === 0 && <span className="sub">no source comparison for this candidate</span>}
                <DiversityRow job={job} />
              </div>
            </div>
          </div>)}
      </div>
      <div>
        <div className="row" style={{ gap: 8 }}>
          <span style={{ fontWeight: 500 }} className="grow">{label}</span>
          <span className="mono" style={{ fontSize: 11, padding: "2px 7px", borderRadius: 4, color,
            background: color === OK ? "var(--ok-bg)" : color === BAD ? "var(--bad-bg)" : "var(--none-bg)" }}>{status}</span>
        </div>
        <button className={`btn jw-cta${here ? "" : " btn-primary"}`} style={here ? { color: OK, borderColor: OK } : undefined}
          disabled={!canApprove} onClick={onApprove}>{apLabel}</button>
        <button className="btn jw-cta" disabled={!canApprove || !job.recipe.build_available} onClick={onApproveBuild}
          title={job.recipe.build_available ? "" : job.recipe.build_blocked_reason}>{here ? "" : "Approve + "}{verb} →</button>
        {!job.recipe.build_available && <span className="sub" style={{ color: WARN }}>{job.recipe.build_label} build unavailable: {job.recipe.build_blocked_reason}</span>}
        <span className="muted" style={{ fontSize: 11.5 }}>
          {item.approved ? `Approved: ${pickLabel(item, false, job.kind)}. Approving another candidate replaces it; built attempts stay in history.`
            : "Click a card to see its checks. Clicking does not approve it."}</span>
        <ErrorLine error={error} />
      </div>
    </div>
  );
}
