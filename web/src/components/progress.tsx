import type { JobSummary, ProgressState } from "../lib/api";

// Colours of the design (Asset Studio v2): done grey; current amber (waiting on you), blue (running), red (failed).
export const OK = "oklch(0.78 0.12 155)", WARN = "oklch(0.8 0.12 80)", BAD = "oklch(0.72 0.14 25)",
  INFO = "oklch(0.76 0.1 250)", W = "#e8e8e6", INK = "#111213";

function current(state: ProgressState): string {
  return state === "bad" ? BAD : state === "run" ? INFO : state === "wait" ? WARN : "#3a3c40";
}

/** Five stage pills (prompt, cands, approve, build, publish). Direct transforms have no prompt/candidate stages. */
export function ProgressPills({ job }: { job: Pick<JobSummary, "progress" | "direct" | "build_label"> }) {
  const p = job.progress;
  const labels = ["prompt", "cands", "approve", job.build_label, "publish"];
  return <div className="pills" style={{ display: "flex", gap: 3 }}>
    {labels.map((label, i) => {
      const done = i < p.stage, cur = i === p.stage;
      return <span key={label} title={label} style={{
        flex: 1, minWidth: 34, textAlign: "center", font: "500 10px 'Geist Mono', monospace", padding: "2px 4px",
        borderRadius: 3, background: done ? "#6e7075" : cur ? current(p.state) : "#26282b",
        color: cur ? W : "#8b8c87" }}>{job.direct && i < 3 ? "—" : label}</span>;
    })}
  </div>;
}

/** The "Next" action pill: filled when waiting on you, red outline when failed, blue text while running. */
export function NextPill({ progress, onClick }: { progress: JobSummary["progress"]; onClick?: () => void }) {
  const s = progress.state;
  const style = {
    background: s === "bad" ? "oklch(0.72 0.14 25 / 0.16)" : s === "wait" ? W : "transparent",
    color: s === "bad" ? BAD : s === "wait" ? INK : s === "run" ? INFO : "#8b8c87",
    border: `1px solid ${s === "bad" ? BAD : s === "wait" ? W : "#2e3033"}`,
    borderRadius: 5, padding: "3px 9px", font: "500 11.5px Geist, sans-serif", whiteSpace: "nowrap" as const,
    cursor: onClick ? "pointer" : "default",
  };
  return <span role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined} style={style}
    onClick={onClick} onKeyDown={(e) => { if (onClick && (e.key === "Enter" || e.key === " ")) onClick(); }}>
    {progress.label}</span>;
}
