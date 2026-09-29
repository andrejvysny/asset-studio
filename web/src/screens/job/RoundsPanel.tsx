import { INFO, WARN } from "../../components/ui";
import { artifactUrl, type CandidateView, type ItemView, type JobDetail, type Round } from "../../lib/api";
import { useProject } from "../../lib/project";
import {
  candidateKey, capitalize, DIM, diffWords, FAINT, isApprovedHere, qaText, roundWhy,
} from "./jobModel";
import { QaPanel } from "./QaPanel";

interface Props {
  job: JobDetail; item: ItemView; ri: number; focusIdx: number; sourceThumb: string | null; busy: boolean; error: string | null;
  onRound: (i: number) => void; onFocus: (idx: number) => void; onApprove: (r: Round, c: CandidateView) => void;
  onApproveBuild: (r: Round, c: CandidateView) => void;
}

const EMPTY = "No rounds yet. Run enhances the prompt and stops for you to confirm it. Candidates appear here, one round per generation.";

function chipSub(item: ItemView, r: Round, i: number): [string, string] {
  if (r.generating) return [`gen ${r.progress?.done ?? 0}/${r.progress?.total ?? r.requested ?? "?"}`, INFO];
  const picked = item.approved?.candidate_set_id === r.candidate_set_id
    ? r.candidates.find((c) => c.id === item.approved?.candidate_id) : undefined;
  if (picked) return [`✓ #${candidateKey(picked)}`, "var(--ok)"];
  return [`${r.candidates.length} · ${roundWhy(item.rounds, i)}`, DIM];
}

export function RoundsPanel({ job, item, ri, focusIdx, sourceThumb, busy, error, onRound, onFocus, onApprove, onApproveBuild }: Props) {
  const { id } = useProject();
  const rounds = item.rounds;
  const r = rounds[ri];
  if (!r) {
    // A confirmed-to-be prompt exists but no round was generated yet.
    if (item.prompt) return (
      <div className="jw-col" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 6 }}><span className="label" style={{ marginRight: 4 }}>Rounds</span>
          <span className="jw-chip on" aria-current="true"><b>R1</b><span style={{ color: DIM }}>to confirm</span></span></div>
        <div className="jw-box" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="sub">round 1 · first round · {item.references.length} ref{item.references.length === 1 ? "" : "s"} · {capitalize(item.enhance_preset)}</span>
          <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>{item.prompt.positive}</div>
        </div>
        <div className="empty">Candidates appear after you confirm the prompt.</div>
      </div>);
    return <div className="empty" style={{ padding: "48px 20px" }}>{EMPTY}</div>;
  }
  const prev = ri > 0 ? rounds[ri - 1] : undefined;
  const text = r.prompt?.positive ?? item.prompt?.positive ?? "";
  const tokens = diffWords(prev?.prompt?.positive, text);
  const addN = tokens.filter((t) => t.added).length;
  const nRefs = r.prompt?.reference_count;
  const refsPart = nRefs == null ? "" : `${nRefs} ref${nRefs === 1 ? "" : "s"}`;
  const meta = [`round ${r.number}`, roundWhy(rounds, ri), refsPart, r.prompt?.preset ? capitalize(r.prompt.preset) : ""].filter(Boolean).join(" · ");
  const cand = r.candidates[focusIdx] ?? r.candidates[0];
  const total = r.requested ?? r.progress?.total ?? 4;
  return (
    <div className="jw-col" style={{ gap: 12 }}>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <span className="label" style={{ marginRight: 4 }}>Rounds</span>
        {rounds.map((x, i) => {
          const [sub, color] = chipSub(item, x, i);
          return <button key={x.number} className={`jw-chip${i === ri ? " on" : ""}`} aria-current={i === ri ? "true" : undefined}
            onClick={() => onRound(i)}><b>R{x.number}</b><span style={{ color }}>{sub}</span></button>;
        })}
        <span className="grow" />
        <span className="sub" style={{ color: FAINT }}>← → rounds · 1–4 approve · [ ] Jobs</span>
      </div>
      <div className="jw-box" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          <span className="sub">{meta}</span>
          <span className="sub" style={{ color: WARN }}>{prev?.prompt ? (addN ? `${addN} words added vs R${prev.number}` : `same prompt as R${prev.number}`) : ""}</span>
        </div>
        <div style={{ fontSize: 12.5, lineHeight: 1.6, display: "flex", flexWrap: "wrap", columnGap: 4 }}>
          {tokens.map((t, i) => <span key={i} style={{ color: t.added ? WARN : "var(--text-2)", borderRadius: 3,
            background: t.added ? "var(--warn-bg)" : "transparent" }}>{t.w}</span>)}
        </div>
      </div>
      <div className="jw-cands" role="list" aria-label={`candidates of round ${r.number}`}>
        {r.generating || r.candidates.length === 0
          ? Array.from({ length: total }, (_, k) => (
            <div key={k} role="listitem" style={{ display: "flex", flexDirection: "column", gap: 5, opacity: 0.5 }}>
              <div className="jw-cand"><div className="tile"><span className="key">{k + 1}</span>
                <span className="sub" style={{ color: FAINT }}>{r.generating ? "generating…" : "not generated"}</span></div></div>
              <span className="sub" style={{ color: FAINT }}>—</span>
            </div>))
          : r.candidates.map((c) => {
            const [status, color] = qaText(c);
            const picked = isApprovedHere(item, r, c);
            return (
              <div key={c.id} role="listitem" style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <button className={`jw-cand${picked ? " picked" : ""}${c.index === (cand?.index ?? 0) ? " focus" : ""}`}
                  aria-label={`candidate ${candidateKey(c)} of round ${r.number}, ${status}${picked ? ", approved" : ""}`}
                  aria-pressed={c.index === (cand?.index ?? 0)} onClick={() => onFocus(c.index)}>
                  <div className="tile">
                    <img src={artifactUrl(id, c.artifact_id)} alt="" loading="lazy" />
                    <span className="key">{candidateKey(c)}</span>
                    <span className="dot" style={{ background: color }} />
                    {picked && <span className="flag">approved</span>}
                  </div>
                </button>
                <span className="sub" style={{ color }}>{status}</span>
              </div>);
          })}
      </div>
      {!r.generating && cand && r.candidate_set_id && (
        <QaPanel job={job} item={item} round={r} cand={cand} sourceThumb={sourceThumb} busy={busy} error={error}
          onApprove={() => onApprove(r, cand)} onApproveBuild={() => onApproveBuild(r, cand)} />)}
    </div>
  );
}
