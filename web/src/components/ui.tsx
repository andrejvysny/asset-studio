import { createElement, type ReactNode, useEffect, useState } from "react";

import type { Status } from "../lib/api";

export const STAGES = ["brief", "prompt", "candidates", "QA", "review", "cut-out", "3D", "export"];

const STATUS_CLASS: Record<Status, string> = { recommended: "ok", not_recommended: "bad", unverified: "none" };
const STATUS_LABEL: Record<Status, string> = {
  recommended: "recommended", not_recommended: "not recommended", unverified: "unverified",
};

export function StatusPill({ status }: { status: Status | null | undefined }) {
  if (!status) return <span className="pill none">no QA</span>;
  return <span className={`pill ${STATUS_CLASS[status]}`}>{STATUS_LABEL[status]}</span>;
}

export function statusColor(status: Status | null | undefined): string {
  return status === "recommended" ? "var(--ok)" : status === "not_recommended" ? "var(--bad)" : "var(--dim)";
}

export function stateColor(state: string): string {
  if (state.startsWith("failed")) return "var(--bad)";
  if (state === "completed") return "var(--ok)";
  if (state === "prompt_enhanced" || state === "waiting_for_selection") return "var(--warn)";
  return "var(--info)";
}

export function StageStrip({ stage, failed, waiting }: { stage: number; failed: boolean; waiting: boolean }) {
  return (
    <div style={{ display: "flex", gap: 3 }}>
      {STAGES.map((name, i) => {
        const bg = failed && i === stage ? "var(--bad)" : i < stage ? "#6e7075"
          : i === stage ? (waiting ? "var(--warn)" : stage >= 8 ? "var(--ok)" : "var(--info)") : "var(--line)";
        return <div key={name} title={name} style={{ flex: 1, height: 4, borderRadius: 2, background: bg }} />;
      })}
    </div>
  );
}

export function Segmented<T extends string>({ options, value, onChange }:
  { options: { id: string; key: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.key} className={value === o.key ? "on" : ""} onClick={() => onChange(o.key)}>
          <span className="id">{o.id}</span>{o.label}
        </button>
      ))}
    </div>
  );
}

export function PageHead({ sub, title, children }: { sub: ReactNode; title: ReactNode; children?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap", marginBottom: 18 }}>
      <div style={{ flex: 1, minWidth: 260 }}>
        <div className="sub">{sub}</div>
        <div className="h1">{title}</div>
      </div>
      {children}
    </div>
  );
}

let viewerLoad: Promise<unknown> | null = null;

/** <model-viewer> web component (bundled from npm, works offline). three.js is ~1 MB, so it is loaded on first use. */
export function ModelViewer({ src, height = 420 }: { src: string; height?: number | string }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    viewerLoad ??= import("@google/model-viewer");
    let alive = true;
    void viewerLoad.then(() => { if (alive) setReady(true); });
    return () => { alive = false; };
  }, []);
  if (!ready) {
    return <div className="stripes sub" style={{ height, borderRadius: 8, display: "flex", alignItems: "center",
      justifyContent: "center" }}>loading 3D viewer…</div>;
  }
  return createElement("model-viewer", {
    src, "camera-controls": true, "auto-rotate": true, "shadow-intensity": "0.6", "interaction-prompt": "none",
    style: { width: "100%", height, background: "#0d0e0f", borderRadius: 8, display: "block" },
  });
}

export function ErrorLine({ error }: { error: string | null | undefined }) {
  return error ? <div className="error">{error}</div> : null;
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
