import { BAD, INFO, OK } from "../../components/progress";
import { Bar, ErrorLine } from "../../components/ui";
import { type ModelPass, send, type StageTask, V2 } from "../../lib/api";
import { useAction, useApi } from "../../lib/hooks";
import { type BatchCtx, plural } from "./batchModel";
import { runPill } from "./batchUi";

const TERMINAL = ["succeeded", "failed", "cancelled"];
const LANES = [{ lane: "gpu0", label: "GPU0 · image" }, { lane: "gpu1", label: "GPU1 · text, QA, 3D" }];
const QUEUE_LIMIT = 10;

/** Title of a pass from its residency string (model group), never from a hard-coded schedule. */
export function passTitle(p: ModelPass | undefined, stages: string[]): string {
  if (!p) return "Idle · no model resident";
  const r = p.residency;
  if (r.includes("qwen_image_edit")) return "Image edit pass · Qwen-Image-Edit-2511 resident";
  if (r.startsWith("comfyui:")) return "Image pass · Qwen-Image-2512 resident";
  if (r === "cpu") return `CPU pass · ${[...new Set(stages)].join(", ") || "no model"}`;
  if (r.startsWith("aux.vlm")) return `${stages.includes("enhance") ? "Enhance" : stages.length ? "QA" : "Text"} pass · Qwen3-VL-8B resident`;
  if (r.startsWith("aux.birefnet")) return "Mask pass · BiRefNet resident";
  if (r.startsWith("worker3d.bake")) return "3D bake pass · exporter resident";
  if (r.startsWith("worker3d")) return "3D pass · TRELLIS.2 resident";
  return `Pass · ${r.split(":")[0]} resident`;
}

const loads = (p: ModelPass | undefined): string => {
  if (!p) return "model loads: —";
  const m = p.measured.model_loads;
  return m ? `model loads this pass: ${Object.values(m).reduce((a, b) => a + b, 0)}` : "model loads this pass: unavailable";
};

function Lane({ label, tasks, passes, titleOf }:
  { label: string; tasks: StageTask[]; passes: ModelPass[]; titleOf: (id: string) => string }) {
  const cur = passes.find((p) => !p.ended_at);
  const inPass = cur ? tasks.filter((t) => cur.task_ids.includes(t.id)) : [];
  const done = inPass.filter((t) => TERMINAL.includes(t.state)).length;
  const queue = tasks.filter((t) => !TERMINAL.includes(t.state) || (cur && inPass.includes(t)));
  const busy = !!cur;
  const color = busy ? INFO : OK;
  const jobs = new Set((cur ? inPass : queue).map((t) => t.job_id)).size;
  return (
    <div style={{ border: "1px solid #26282b", borderRadius: 9, padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
        <span className="label">{label}</span>
        <span style={{ font: "500 11px 'Geist Mono', monospace", color }}>{busy ? "busy" : "idle"}</span></div>
      <span style={{ fontWeight: 500 }}>{passTitle(cur, inPass.map((t) => t.stage))}</span>
      <Bar pct={inPass.length ? (done / inPass.length) * 100 : 0} color={color} />
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", font: "500 11px 'Geist Mono', monospace", color: "#a3a4a1" }}>
        <span>{inPass.length ? `${done}/${inPass.length} tasks · ${plural(jobs, "Job")}` : queue.length ? `${plural(queue.length, "task")} queued · ${plural(jobs, "Job")}` : "no tasks"}</span>
        <span>{loads(cur)}</span></div>
      <div style={{ border: "1px solid #222326", borderRadius: 6, overflow: "hidden" }}>
        {queue.length === 0 && <div style={{ padding: "5px 9px", fontSize: 12, color: "#8b8c87" }}>Nothing queued</div>}
        {queue.slice(0, QUEUE_LIMIT).map((t) => (
          <div key={t.id} style={{ display: "flex", gap: 10, padding: "5px 9px", borderTop: "1px solid #1f2023", fontSize: 12 }}>
            <span style={{ flex: 1 }}>{titleOf(t.job_id)}</span>
            <span style={{ font: "500 10.5px 'Geist Mono', monospace", color: "#8b8c87" }}>{t.stage}</span>
            <span style={{ font: "500 10.5px 'Geist Mono', monospace", width: 80, textAlign: "right",
              color: t.state === "failed" || t.state === "blocked" ? BAD : INFO }}>{t.state}</span>
          </div>))}
        {queue.length > QUEUE_LIMIT && <div style={{ padding: "5px 9px", fontSize: 12, color: "#8b8c87" }}>+{queue.length - QUEUE_LIMIT} more</div>}
      </div>
    </div>
  );
}

export function ExecutionTab({ ctx }: { ctx: BatchCtx }) {
  const { project, jobs, active } = ctx;
  const ids = new Set(jobs.map((j) => j.id));
  const t = useApi<{ tasks: StageTask[] }>(`/api/v2/tasks?project_id=${project}`, { project, pollMs: 3000 });
  const p = useApi<{ passes: ModelPass[] }>("/api/v2/passes?limit=200", { project, pollMs: 3000 });
  const act = useAction();
  const mine = (t.data?.tasks ?? []).filter((x) => ids.has(x.job_id));
  const mineIds = new Set(mine.map((x) => x.id));
  const title = (id: string) => jobs.find((j) => j.id === id)?.title ?? id;
  const control = (action: string) => void act.run(async () => {
    await send("POST", `${V2(project)}/runs/${active!.id}:${action}`); t.reload(); ctx.reload();
  });
  const paused = (active?.counts.paused_tasks ?? 0) > 0;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {active && (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span className="sub">Run {active.id.slice(-8)}</span>{runPill(active.status)}
          <span className="sub">{active.counts.active_tasks} active · {active.counts.failed_tasks} failed tasks</span>
          <span style={{ flex: 1 }} />
          <button className="btn" disabled={act.busy} onClick={() => control(paused ? "resume" : "pause")}>{paused ? "Resume" : "Pause"}</button>
          <button className="btn" disabled={act.busy || !active.counts.active_tasks} onClick={() => control("cancel")}>Cancel run's work</button>
          <button className="btn" disabled={act.busy || active.counts.active_tasks > 0} onClick={() => control("close")}
            title="Keeps undecided items in their Jobs for a later run">Close run</button>
        </div>)}
      <ErrorLine error={act.error ?? t.error ?? p.error} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(320px,1fr))", gap: 12 }}>
        {LANES.map((l) => (
          <Lane key={l.lane} label={l.label} titleOf={title}
            tasks={mine.filter((x) => x.lane === l.lane)}
            passes={(p.data?.passes ?? []).filter((x) => x.lane === l.lane && x.task_ids.some((id) => mineIds.has(id)))} />))}
      </div>
      <PassHistory tasks={mine} passes={(p.data?.passes ?? []).filter((x) => x.task_ids.some((id) => mineIds.has(id)))} />
      <div style={{ fontSize: 12, color: "#8b8c87", maxWidth: 820 }}>
        Stage-first: the same stage is drained across every Job in the Batch before the model is released. GPU0 and GPU1 run independently.
        Load counts are measured per pass; values not reported by an engine show as unavailable.</div>
    </div>
  );
}

function PassHistory({ passes, tasks }: { passes: ModelPass[]; tasks: StageTask[] }) {
  if (!passes.length) return null;
  return (
    <section aria-label="Recent passes" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span className="label">Recent passes</span>
      <div className="table">
        {passes.slice(0, 8).map((p) => (
          <div key={p.id} className="td" style={{ gridTemplateColumns: "60px minmax(220px,1.6fr) 80px 60px minmax(140px,1fr) 110px", padding: "6px 14px" }}>
            <span className="mono" style={{ fontSize: 11.5 }}>{p.lane}</span>
            <span className="ellipsis" title={p.residency}>{passTitle(p, tasks.filter((t) => p.task_ids.includes(t.id)).map((t) => t.stage))}</span>
            <span className="sub">{plural(p.task_ids.length, "task")}</span>
            <span className="sub">{plural(p.jobs.length, "Job")}</span>
            <span className="sub">{loads(p)}</span>
            <span className="sub" style={{ color: p.ended_at ? undefined : INFO }}>{p.close_reason ?? (p.ended_at ? "closed" : "running")}</span>
          </div>))}
      </div>
    </section>
  );
}
