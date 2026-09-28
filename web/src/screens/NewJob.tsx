import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ErrorLine } from "../components/ui";
import type { JobDetail, StudioConfig } from "../lib/api";
import { outputByTitle, queueWorkflow, waitForPrompt } from "../lib/comfy";
import { useAction, useApi } from "../lib/hooks";

const ASSET_TYPES = ["none", "small_prop", "medium_prop", "large_prop", "rock", "tree_trunk", "plant", "weapon"];

function Steps({ step }: { step: number }) {
  const steps = [["1", "created → prompt_enhanced", "Brief + enhance"], ["2", "prompt_confirmed", "Review + confirm prompt"],
    ["3", "candidates_generated", "Generate candidates"]];
  return (
    <div className="panel" style={{ display: "flex", marginBottom: 22 }}>
      {steps.map(([n, state, label], i) => (
        <div key={n} style={{ flex: 1, padding: "10px 14px", borderRight: i < 2 ? "1px solid var(--line)" : undefined,
          background: i + 1 === step ? "#1c1d20" : "transparent", display: "flex", flexDirection: "column", gap: 2 }}>
          <span className="mono" style={{ fontSize: 10.5, color: i + 1 <= step ? "var(--text-2)" : "var(--faint)" }}>{n} · {state}</span>
          <span style={{ color: i + 1 <= step ? "var(--text)" : "var(--dim)" }}>{label}</span>
        </div>
      ))}
    </div>
  );
}

function BriefStep() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const action = useAction();
  const [brief, setBrief] = useState(params.get("brief") ?? "wooden medieval barrel, iron hoops");
  const [f, setF] = useState({ asset_type: "small_prop", target_triangles: 0, candidate_count: 4, seed_family: 0 });
  const set = (k: keyof typeof f, v: string) => setF({ ...f, [k]: k === "asset_type" ? v : Number(v) || 0 });

  const submit = () => action.run(async () => {
    const { promptId, wf } = await queueWorkflow("line_a_enhance", { create_job: { prompt: brief, ...f } });
    const entry = await waitForPrompt(promptId);
    const jobId = String(outputByTitle(entry, wf, "create_job")[0] ?? "");
    if (!jobId) throw new Error("ComfyUI finished without a job id");
    navigate(`/new/${jobId}`);
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      {params.get("slot") && <div className="banner note">For slot <span className="mono">{params.get("slot")}</span>.
        Assign the finished attempt to it from the 3D attempts screen.</div>}
      <label className="field"><span>Line</span>
        <div style={{ border: "1px solid #8b8c87", background: "#1c1d20", borderRadius: 7, padding: "10px 14px", maxWidth: 320 }}>
          <div style={{ fontWeight: 500 }}>3D asset</div>
          <div className="dim" style={{ fontSize: 12 }}>image → BiRefNet → TRELLIS.2 → GLB</div>
        </div>
      </label>
      <label className="field"><span>Brief</span>
        <textarea className="input" rows={3} value={brief} onChange={(e) => setBrief(e.target.value)} /></label>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(180px,1fr))", gap: 12 }}>
        <label className="field"><span>asset_type</span>
          <select className="input" value={f.asset_type} onChange={(e) => set("asset_type", e.target.value)}>
            {ASSET_TYPES.map((t) => <option key={t}>{t}</option>)}</select>
          <span className="dim" style={{ fontSize: 11 }}>shapes the enhancer wording</span></label>
        <label className="field"><span>target_triangles</span>
          <input className="input" value={f.target_triangles} onChange={(e) => set("target_triangles", e.target.value)} />
          <span className="dim" style={{ fontSize: 11 }}>advisory · 0 = none · floor applies</span></label>
        <label className="field"><span>candidate_count</span>
          <input className="input" value={f.candidate_count} onChange={(e) => set("candidate_count", e.target.value)} />
          <span className="dim" style={{ fontSize: 11 }}>1–8</span></label>
        <label className="field"><span>seed_family</span>
          <input className="input" value={f.seed_family} onChange={(e) => set("seed_family", e.target.value)} />
          <span className="dim" style={{ fontSize: 11 }}>0 = random</span></label>
      </div>
      <div className="row" style={{ gap: 14, borderTop: "1px solid var(--line)", paddingTop: 16 }}>
        <button className="btn btn-primary" disabled={action.busy || !brief.trim()} onClick={submit}>
          {action.busy ? "Enhancing…" : "Create job + enhance prompt"}</button>
        <span className="dim" style={{ fontSize: 12 }}>Runs Qwen3-VL-8B on GPU1 and stops at prompt_enhanced. No images are generated.</span>
      </div>
      <ErrorLine error={action.error} />
    </div>
  );
}

function ConfirmStep({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const job = useApi<JobDetail>(`/api/jobs/${jobId}`, 3000);
  const cfg = useApi<StudioConfig>("/api/config");
  const action = useAction();
  const [edited, setEdited] = useState<string | null>(null);
  const [speed, setSpeed] = useState("quality");
  const j = job.data;
  const original = j?.enhancement?.description ?? "";
  useEffect(() => { if (j && j.state !== "prompt_enhanced" && !j.active_operation && j.state !== "created") navigate(`/review/${jobId}`); },
    [j, jobId, navigate]);

  if (!j) return <ErrorLine error={job.error} />;
  if (j.state === "created" || j.active_operation) return <div className="dim">Enhancing prompt on GPU1…</div>;
  if (j.state === "failed_prompt") return <ErrorLine error={j.history.at(-1)?.error ?? "enhancement failed"} />;
  const text = edited ?? original;
  const presets = ["quality", ...Object.keys(cfg.data?.image.speed_presets ?? {})];

  const confirm = () => action.run(async () => {
    await queueWorkflow("line_a_generate", {
      confirm_prompt: { job_id: jobId, edited_description: text.trim() === original.trim() ? "" : text },
      generate: { speed_preset: speed },
    });
    navigate(`/review/${jobId}`);
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="banner note"><span className="dot" style={{ background: "var(--warn)", marginTop: 6 }} />
        <span>Saved to <span className="mono" style={{ color: "var(--text)" }}>enhanced-prompt.original.txt</span>.
          You can close this page; the job waits here.</span></div>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 260px", gap: 18 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div className="row muted" style={{ fontSize: 12, justifyContent: "space-between" }}>
            <span>Enhanced prompt (editable)</span>
            <button className="btn-link" style={{ padding: 0, textDecoration: "underline" }} onClick={() => setEdited(null)}>
              {edited !== null && edited !== original ? "Edited · reset" : "Unedited"}</button>
          </div>
          <textarea className="input" rows={7} value={text} onChange={(e) => setEdited(e.target.value)}
            style={{ borderColor: "var(--line-3)" }} />
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>Model-sheet constraints, appended by the pipeline. You can't edit these here.</div>
          <div className="mono" style={{ background: "var(--panel)", border: "1px dashed var(--line-3)", borderRadius: 7,
            padding: "11px 12px", fontSize: 12, lineHeight: 1.55, color: "var(--muted)" }}>{cfg.data?.template ?? "…"}</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="panel" style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            <span className="label">Enhancer output</span>
            {[["short_title", j.enhancement?.short_title], ["asset_tags", j.enhancement?.asset_tags.join(", ")],
              ["camera_hint", j.enhancement?.camera_hint], ["model", `${j.enhancement?.meta.model.repo.split("/")[1]} · ${j.enhancement?.meta.seconds}s`]]
              .map(([k, v]) => <div key={k}><div className="dim" style={{ fontSize: 11 }}>{k}</div><div className="mono" style={{ fontSize: 12 }}>{v || "—"}</div></div>)}
          </div>
          <div className="panel" style={{ padding: 12, fontSize: 12, display: "flex", flexDirection: "column", gap: 4 }}>
            <span className="label">Stored separately</span>
            <span className="mono muted">enhanced_original</span><span className="mono muted">user_edited</span>
            <span className="mono muted">effective = edit + constraints</span>
          </div>
          <label className="field"><span>Speed preset</span>
            <select className="input" value={speed} onChange={(e) => setSpeed(e.target.value)}>
              {presets.map((p) => <option key={p}>{p}</option>)}</select></label>
        </div>
      </div>
      <div className="row" style={{ gap: 14, borderTop: "1px solid var(--line)", paddingTop: 16 }}>
        <button className="btn btn-primary" disabled={action.busy || !text.trim()} onClick={confirm}>
          Confirm prompt + generate {j.request.candidate_count} candidates</button>
        <span className="grow" />
        <span className="dim" style={{ fontSize: 12 }}>Same job id · GPU0 · {speed === "quality" ? "~2 min / variant" : "~10 s / variant"}</span>
      </div>
      <ErrorLine error={action.error} />
    </div>
  );
}

export function NewJob() {
  const { jobId } = useParams();
  return (
    <div className="page" style={{ maxWidth: 980 }}>
      <div className="sub">{jobId ?? "job id assigned on create"}</div>
      <div className="h1" style={{ margin: "2px 0 16px" }}>New job</div>
      <Steps step={jobId ? 2 : 1} />
      {jobId ? <ConfirmStep jobId={jobId} /> : <BriefStep />}
    </div>
  );
}
