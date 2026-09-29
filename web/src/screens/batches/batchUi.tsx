import type { CSSProperties, ReactNode } from "react";
import { Link } from "react-router-dom";

import { BAD, INFO, OK, WARN } from "../../components/progress";

export function runPill(status: string): ReactNode {
  const cls = status === "running" || status === "waiting_for_review" || status === "paused" ? "warn"
    : status === "completed" ? "ok" : status === "completed_with_errors" ? "bad" : "none";
  return <span className={`pill ${cls}`}>{status.replaceAll("_", " ")}</span>;
}

export const MONO_SUB: CSSProperties = { font: "500 10.5px 'Geist Mono', monospace", color: "#8b8c87" };
export const PRIMARY_BTN: CSSProperties = { padding: "5px 12px", fontSize: 12.5 };
export const GHOST_BTN: CSSProperties = { padding: "4px 11px", fontSize: 12.5 };

/** Card with the design's header strip: title, count, spacer, actions. */
export function Card({ title, n, note, actions, children }:
  { title: string; n: number | string; note?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section aria-label={title} style={{ border: "1px solid #26282b", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "10px 14px", background: "#141517", flexWrap: "wrap" }}>
        <span style={{ fontWeight: 500 }}>{title}</span>
        <span style={{ font: "500 11px 'Geist Mono', monospace", color: "#8b8c87" }}>{n}</span>
        <span style={{ flex: 1 }} />
        {note && <span style={{ fontSize: 12, color: "#8b8c87" }}>{note}</span>}
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Row({ cols, children, align = "center" }: { cols: string; children: ReactNode; align?: string }) {
  return <div style={{ display: "grid", gridTemplateColumns: cols, gap: "8px 12px", padding: "10px 14px",
    borderTop: "1px solid #222326", alignItems: align }}>{children}</div>;
}

export function OpenLink({ to, label }: { to: string; label?: string }) {
  return <Link to={to} className="sub" style={{ textAlign: "right", textDecoration: "none", color: "#a3a4a1", fontFamily: "Geist, sans-serif",
    fontSize: 12 }}>{label ?? "Open →"}</Link>;
}

export const TONE = { ok: OK, warn: WARN, bad: BAD, info: INFO };
