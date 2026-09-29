import { useNavigate } from "react-router-dom";

import { Bar, ErrorLine, INFO, qaColor, taskColor } from "../../components/ui";
import { artifactUrl, send } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { ActionBar, type TabProps } from "./BatchWorkspace";

export function CandidatesTab({ batch, reload }: TabProps) {
  const { id } = useProject();
  const nav = useNavigate();
  const act = useAction();
  const gen = batch.items.map((i) => i.tasks.generate).filter(Boolean);
  const running = gen.filter((t) => t && ["held", "queued", "running", "reconciling"].includes(t.state));
  const totals = batch.items.reduce((acc, i) => {
    const t = i.tasks.generate;
    const total = t?.progress.total ?? i.candidate_set?.requested ?? 0;
    const done = i.candidate_set && !(t && ["queued", "running"].includes(t.state)) ? i.candidate_set.candidates.length : (t?.progress.done ?? 0);
    return { done: acc.done + done, total: acc.total + total };
  }, { done: 0, total: 0 });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="row panel" style={{ padding: "10px 14px", gap: 16, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 500 }}>{running.length ? `Generating ${running.length} items` : "Candidates"}</span>
        <div className="grow" style={{ minWidth: 160 }}><Bar pct={totals.total ? (totals.done / totals.total) * 100 : 0} color={INFO} /></div>
        <span className="sub">{totals.done}/{totals.total} candidates</span>
      </div>
      <div className="table">
        {batch.items.map((it) => {
          const t = it.tasks.generate;
          const qa = it.tasks.qa;
          const cands = it.candidate_set && !(t && ["queued", "running"].includes(t.state)) ? it.candidate_set.candidates : [];
          const n = t?.progress.total ?? it.candidate_set?.requested ?? 4;
          const done = t?.progress.done ?? 0;
          return (
            <div key={it.id} className="td" style={{ gridTemplateColumns: "190px minmax(0,1fr) 170px" }}>
              <span style={{ fontWeight: 500 }}>{it.name}</span>
              <div className="row" style={{ gap: 6 }}>
                {cands.length ? cands.map((c) => (
                  <img key={c.id} src={artifactUrl(id, c.artifact_id)} alt={`${it.name} candidate ${c.index + 1}`} loading="lazy"
                    style={{ width: 56, height: 56, objectFit: "cover", borderRadius: 5, border: `1px solid ${qaColor(c.qa?.status)}` }} />
                )) : Array.from({ length: n }, (_, i) => (
                  <div key={i} className={i < done ? "" : "stripes"} style={{ width: 56, height: 56, borderRadius: 5,
                    border: `1px solid ${i < done ? INFO : "var(--line)"}`, background: i < done ? "var(--info-bg)" : undefined }} />
                ))}
              </div>
              <div style={{ textAlign: "right", display: "flex", flexDirection: "column", gap: 2 }}>
                <span className="sub" style={{ color: taskColor(t?.state) }}>
                  {t ? (t.state === "running" ? `generating ${done}/${n}` : t.state) : "not generated"}</span>
                {qa && <span className="sub" style={{ color: taskColor(qa.state) }}>QA {qa.state}</span>}
                {t?.error && <span className="error" style={{ fontSize: 11 }}>{t.error}</span>}
                {(t?.state === "blocked" || t?.state === "failed") && <button className="tag" onClick={() => void act.run(async () => {
                  await send("POST", `/api/v1/operations/${t.op_id}:retry`); reload(); })}>Retry same inputs</button>}
              </div>
            </div>
          );
        })}
      </div>
      <ErrorLine error={act.error} />
      <ActionBar note={running.length ? "Generating. You can leave this page; work continues on the server."
        : `${batch.counts.candidates}/${batch.counts.items} items have candidates.`}
        sub="Candidates and QA are stored as soon as each item finishes. Completed items never resample.">
        <button className="btn btn-primary" onClick={() => nav(`/p/${id}/batches/${batch.id}/approve`)}>Go to approve →</button>
      </ActionBar>
    </div>
  );
}
