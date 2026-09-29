import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Dialog, ErrorLine, OK, QaPill, qaColor, WARN } from "../../components/ui";
import { artifactUrl, type CandidateView, type ItemView, key, P, send } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { useProject } from "../../lib/project";
import { ActionBar, type TabProps } from "./BatchWorkspace";

type Filter = "all" | "undecided" | "approved" | "regen";
interface Proposal { item_id: string; name: string; candidate_set_id?: string; candidate_id?: string; image_sha256?: string;
  prompt_revision_id?: string; qa_evaluation_id?: string; expected_item_revision?: number; index?: number; skip?: string }
interface Outcome { item_id: string; ok: boolean; code?: string; message?: string }

const LETTERS = "12345678"; // same labels as the 1–8 approval keys
const matches = (f: Filter, i: ItemView) => f === "all" || (f === "undecided" && !i.approval && !i.regen_requested)
  || (f === "approved" && !!i.approval) || (f === "regen" && i.regen_requested);

function approvedCandidate(i: ItemView): string | null {
  const b = i.approval_detail?.bound as { candidate_id?: string } | undefined;
  return b?.candidate_id ?? null;
}

export function ApproveTab({ batch, reload }: TabProps) {
  const { id } = useProject();
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const filter = (sp.get("filter") as Filter | null) ?? "all";
  const rows = useMemo(() => batch.items.filter((i) => i.candidate_set && matches(filter, i)), [batch.items, filter]);
  const activeId = sp.get("item") ?? rows[0]?.id;
  const active = rows.find((r) => r.id === activeId) ?? rows[0];
  const focusIdx = Number(sp.get("cand") ?? "0");
  const focus: CandidateView | undefined = active?.candidate_set?.candidates[focusIdx] ?? active?.candidate_set?.candidates[0];
  const [best, setBest] = useState<{ proposals: Proposal[]; skipped: Proposal[]; policy: string } | null>(null);
  const [override, setOverride] = useState<{ item: ItemView; cand: CandidateView } | null>(null);
  const [reason, setReason] = useState("");
  const act = useAction();
  const base = `${P(id)}/batches/${batch.id}`;
  const setFocus = (item: string, cand: number) => setSp((p) => { const n = new URLSearchParams(p); n.set("item", item);
    n.set("cand", String(cand)); return n; }, { replace: true });

  const approve = (item: ItemView, cand: CandidateView, overrideQa: boolean, why?: string) => act.run(async () => {
    if (!item.legal.approve || !item.candidate_set) throw new Error(`${item.name} cannot be approved now`);
    const res = await send<{ results: Outcome[] }>("POST", `${base}:approve-candidates`, { idempotency_key: key(), items: [{
      item_id: item.id, expected_item_revision: item.revision, candidate_set_id: item.candidate_set.id,
      candidate_id: cand.id, image_sha256: cand.sha256, prompt_revision_id: item.candidate_set.prompt_revision_id,
      qa_evaluation_id: cand.qa?.id ?? null, override_qa: overrideQa, override_reason: why || null }] });
    reload();
    const r = res.results[0];
    if (r && !r.ok) {
      if (r.code === "override_required") { setOverride({ item, cand }); return; }
      throw new Error(r.message);
    }
    const pos = rows.findIndex((x) => x.id === item.id);
    const next = rows[pos + 1];
    if (next) setFocus(next.id, 0);
  });
  const request = (item: ItemView, cand: CandidateView) => {
    if (approvedCandidate(item) === cand.id) {
      void act.run(async () => { await send("POST", `${base}:clear-approval`, { items: [
        { item_id: item.id, expected_item_revision: item.revision }] }); reload(); });
    } else if (cand.qa?.status === "recommended") void approve(item, cand, false);
    else { setReason(""); setOverride({ item, cand }); }
  };
  const toggleRegen = (item: ItemView) => void act.run(async () => {
    await send("POST", `${base}:mark-regenerate`, { mark: !item.regen_requested, items: [
      { item_id: item.id, expected_item_revision: item.revision }] });
    reload();
  });
  const openBest = () => void act.run(async () => {
    setBest(await send("POST", `${base}:preview-best`, { item_ids: rows.map((r) => r.id) }));
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (/INPUT|TEXTAREA|SELECT/.test((document.activeElement as HTMLElement | null)?.tagName ?? "") || !active) return;
      const pos = rows.findIndex((r) => r.id === active.id);
      if (/^[1-8]$/.test(e.key)) {
        const c = active.candidate_set?.candidates[Number(e.key) - 1];
        if (c) { setFocus(active.id, c.index); request(active, c); }
      } else if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault(); const n = rows[Math.min(pos + 1, rows.length - 1)]; if (n) setFocus(n.id, 0);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault(); const n = rows[Math.max(pos - 1, 0)]; if (n) setFocus(n.id, 0);
      } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const count = active.candidate_set?.candidates.length ?? 1;
        setFocus(active.id, (focusIdx + (e.key === "ArrowRight" ? 1 : count - 1)) % count);
      } else if (e.key === "r") toggleRegen(active);
      else if (e.key === "a") openBest();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const approved = batch.items.filter((i) => i.approval && !i.regen_requested);
  const regen = batch.items.filter((i) => i.regen_requested && !i.tasks.generate?.state.match(/queued|running/));
  const undecided = batch.items.filter((i) => i.candidate_set && !i.approval && !i.regen_requested);
  const buildable = batch.items.filter((i) => i.legal.build);
  const counts: Record<Filter, number> = { all: batch.items.filter((i) => i.candidate_set).length,
    undecided: undecided.length, approved: approved.length, regen: regen.length };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(240px,320px)", gap: 16, alignItems: "start" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          {(["all", "undecided", "approved", "regen"] as Filter[]).map((f) => (
            <button key={f} className={`chip${filter === f ? " on" : ""}`} onClick={() => setSp({ filter: f })}>
              {f === "regen" ? "regenerate" : f} <span className="mono dim">{counts[f]}</span></button>
          ))}
          <span className="grow" />
          <button className="btn" style={{ padding: "5px 11px", fontSize: 12 }} onClick={openBest}>
            Approve best recommended in undecided rows…</button>
        </div>
        {rows.length === 0 && <div className="empty">No rows here yet. Candidates appear when generation finishes.</div>}
        <div className="table">
          {rows.map((it) => {
            const on = it.id === active?.id;
            const picked = approvedCandidate(it);
            return (
              <div key={it.id} className="td" onClick={() => setFocus(it.id, focusIdx)}
                style={{ display: "flex", flexWrap: "wrap", gap: "10px 14px", background: on ? "#18191b" : undefined,
                  boxShadow: `inset 3px 0 0 ${on ? "var(--text)" : it.regen_requested ? WARN : picked ? OK : "transparent"}` }}>
                <div className="row" style={{ width: "100%", justifyContent: "space-between" }}>
                  <div className="row" style={{ alignItems: "baseline" }}><span style={{ fontWeight: 500 }}>{it.name}</span>
                    <span className="sub" style={{ color: it.regen_requested ? WARN : picked ? OK : undefined }}>
                      {it.regen_requested ? "marked for regeneration" : picked ? "approved" : it.stage.state}</span></div>
                  <button className="tag" disabled={!it.legal.mark_regenerate} onClick={(e) => { e.stopPropagation(); toggleRegen(it); }}
                    style={{ color: it.regen_requested ? WARN : undefined }}>{it.regen_requested ? "Undo regenerate" : "Regenerate"}</button>
                </div>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  {it.candidate_set?.candidates.map((c) => {
                    const isFocus = on && focus?.id === c.id;
                    return (
                      <div key={c.id} className={`cand${picked === c.id ? " picked" : ""}${isFocus ? " focus" : ""}`}>
                        <button className="img" aria-label={`${it.name} candidate ${LETTERS[c.index]} ${c.qa?.status ?? "QA pending"}${picked === c.id ? " approved" : ""}`}
                          onClick={(e) => { e.stopPropagation(); setFocus(it.id, c.index); }}
                          onDoubleClick={(e) => { e.stopPropagation(); request(it, c); }}
                          style={{ opacity: it.regen_requested ? 0.45 : 1 }}>
                          <img src={artifactUrl(id, c.artifact_id)} alt="" loading="lazy" />
                          <span className="cand-key">{c.index + 1}</span>
                          <span className="dot" style={{ position: "absolute", right: 5, top: 6, width: 8, height: 8, background: qaColor(c.qa?.status) }} />
                          {picked === c.id && <span className="cand-flag">approved</span>}
                        </button>
                        <span className="sub" style={{ fontSize: 10, color: qaColor(c.qa?.status) }}>
                          {c.qa ? c.qa.status.replace("_", " ") : "QA pending"}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
        <div className="sub">Keys: 1–8 approve · ←/→ focus · J/K rows · R regenerate · A best recommended · click = focus, double-click = approve</div>
        <ErrorLine error={act.error} />
      </div>
      {active && focus && (
        <aside className="inspector" aria-label="candidate inspector">
          <div className="checker" style={{ aspectRatio: "1", position: "relative" }}>
            <a href={artifactUrl(id, focus.artifact_id)} target="_blank" rel="noreferrer" title="open full resolution">
              <img src={artifactUrl(id, focus.artifact_id)} alt={`${active.name} candidate ${LETTERS[focus.index]}`}
                style={{ width: "100%", height: "100%", objectFit: "contain" }} /></a>
            <span className="corner" style={{ left: 10, top: 10, fontWeight: 600, fontSize: 12 }}>{LETTERS[focus.index]} · seed {focus.seed}</span>
          </div>
          <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="row"><span style={{ fontWeight: 500 }} className="grow">{active.name}</span><QaPill status={focus.qa?.status} /></div>
            {focus.qa && <span className="sub">coverage {focus.qa.coverage.completed}/{focus.qa.coverage.applicable}
              {focus.qa.not_evaluated ? " · no applicable checks" : ""}</span>}
            <div className="muted" style={{ fontSize: 12 }}>{active.prompt?.positive}</div>
            <div className="panel" style={{ maxHeight: 300, overflowY: "auto" }}>
              {(focus.qa?.results ?? []).map((k) => {
                const col = k.result === "pass" ? OK : k.result === "fail" ? "var(--bad)" : "var(--dim)";
                return (
                  <div key={k.rule_id} className="row tr" style={{ padding: "5px 9px", gap: 8 }} title={k.reason}>
                    <span className="dot" style={{ background: col }} />
                    <span className="mono grow" style={{ fontSize: 11 }}>{k.rule_id}</span>
                    <span className="sub" style={{ fontSize: 10, color: "var(--faint)" }}>{k.severity}</span>
                    <span className="sub" style={{ color: col, width: 64, textAlign: "right" }}>{k.result.replace("_", " ")}</span>
                  </div>
                );
              })}
              {focus.qa?.policy.disabled.map((d) => <div key={d} className="sub tr" style={{ padding: "5px 9px" }}>{d} · disabled</div>)}
            </div>
            <button className="btn btn-primary" disabled={act.busy || !active.legal.approve}
              onClick={() => request(active, focus)}>{approvedCandidate(active) === focus.id ? "Clear approval"
                : focus.qa?.status === "recommended" ? `Approve ${LETTERS[focus.index]}` : `Approve ${LETTERS[focus.index]} anyway…`}</button>
          </div>
        </aside>
      )}
      {override && (
        <Dialog title={`Approve ${override.item.name} · ${LETTERS[override.cand.index]} anyway?`} onClose={() => setOverride(null)}>
          <div className="banner bad">QA is advisory. This candidate is <b>{override.cand.qa?.status.replace("_", " ") ?? "unchecked"}</b>.
            The failed and missing checks are recorded with the approval.</div>
          <div className="sub">failed: {[...(override.cand.qa?.policy.failed_major ?? []), ...(override.cand.qa?.policy.failed_minor ?? [])].join(", ") || "—"}
            {" · "}unavailable: {override.cand.qa?.policy.unavailable.join(", ") || "—"}</div>
          <label className="field"><span>Reason (optional)</span>
            <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <div className="row"><button className="btn btn-primary" onClick={() => { const o = override; setOverride(null);
            void approve(o.item, o.cand, true, reason); }}>Approve anyway</button>
            <button className="btn-link" onClick={() => setOverride(null)}>Cancel</button></div>
        </Dialog>
      )}
      {best && (
        <Dialog title="Approve best recommended" onClose={() => setBest(null)}>
          <div className="sub">{best.policy}</div>
          <div className="table">
            {best.proposals.map((p) => <div key={p.item_id} className="td" style={{ gridTemplateColumns: "1fr auto" }}>
              <span>{p.name}</span><span className="mono">{LETTERS[p.index ?? 0]}</span></div>)}
            {best.skipped.map((p) => <div key={p.item_id} className="td" style={{ gridTemplateColumns: "1fr auto" }}>
              <span className="muted">{p.name}</span><span className="sub">skipped · {p.skip}</span></div>)}
          </div>
          <div className="row">
            <button className="btn btn-primary" disabled={!best.proposals.length || act.busy} onClick={() => void act.run(async () => {
              const res = await send<{ results: Outcome[] }>("POST", `${base}:approve-candidates`, { idempotency_key: key(),
                items: best.proposals.map((p) => ({ item_id: p.item_id, expected_item_revision: p.expected_item_revision,
                  candidate_set_id: p.candidate_set_id, candidate_id: p.candidate_id, image_sha256: p.image_sha256,
                  prompt_revision_id: p.prompt_revision_id, qa_evaluation_id: p.qa_evaluation_id, override_qa: false })) });
              setBest(null);
              reload();
              const bad = res.results.filter((r) => !r.ok);
              if (bad.length) throw new Error(`${bad.length} rows changed meanwhile and were not approved: ${bad.map((b) => b.message).join("; ")}`);
            })}>Approve {best.proposals.length} proposals</button>
            <button className="btn-link" onClick={() => setBest(null)}>Cancel</button>
          </div>
        </Dialog>
      )}
      <div style={{ gridColumn: "1 / -1", margin: "0 -24px -24px" }}>
        <ActionBar note={`${approved.length} approved · ${regen.length} to regenerate · ${undecided.length} undecided`}
          sub={batch.recipe.build_available ? "Undecided rows stay here; build them in a later pass."
            : `${batch.recipe.build_label} build unavailable: ${batch.recipe.build_blocked_reason}`}>
          {regen.length > 0 && <button className="btn" disabled={act.busy} onClick={() => void act.run(async () => {
            const res = await send<{ results: Outcome[] }>("POST", `${base}:regenerate`, { idempotency_key: key(),
              items: regen.map((i) => ({ item_id: i.id, expected_item_revision: i.revision })) });
            reload();
            const bad = res.results.filter((r) => !r.ok);
            if (bad.length) throw new Error(bad.map((b) => b.message).join("; "));
          })}>Regenerate {regen.length}</button>}
          <button className="btn btn-primary" disabled={act.busy || !batch.recipe.build_available || buildable.length === 0}
            title={batch.recipe.build_available ? "" : batch.recipe.build_blocked_reason}
            onClick={() => void act.run(async () => {
              await send("POST", `${base}:build-approved`, { idempotency_key: key(), items: buildable.map((i) => ({
                item_id: i.id, approval_id: i.approval, expected_item_revision: i.revision })) });
              reload();
              nav(`/p/${id}/batches/${batch.id}/build`);
            })}>{batch.recipe.build_label} for {buildable.length} approved</button>
        </ActionBar>
      </div>
    </div>
  );
}
