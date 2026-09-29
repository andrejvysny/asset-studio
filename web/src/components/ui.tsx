import { createElement, type ReactNode, useEffect, useState } from "react";

import type { QaStatus } from "../lib/api";

export const OK = "var(--ok)";
export const WARN = "var(--warn)";
export const BAD = "var(--bad)";
export const INFO = "var(--info)";
export const NONE = "var(--dim)";

const QA_LABEL: Record<QaStatus, string> = {
  recommended: "recommended", not_recommended: "not recommended", unverified: "unverified",
};
export const qaColor = (s: QaStatus | null | undefined): string =>
  s === "recommended" ? OK : s === "not_recommended" ? BAD : NONE;

export function QaPill({ status }: { status: QaStatus | null | undefined }) {
  const cls = status === "recommended" ? "ok" : status === "not_recommended" ? "bad" : "none";
  return <span className={`pill ${cls}`}>{status ? QA_LABEL[status] : "QA pending"}</span>;
}

export function PageHead({ sub, title, children }: { sub: ReactNode; title: ReactNode; children?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
      <div style={{ flex: 1, minWidth: 220 }}>
        <div className="sub">{sub}</div>
        <h1 className="h1" style={{ margin: "2px 0 0" }}>{title}</h1>
      </div>
      {children}
    </div>
  );
}

export function Toggle({ on, onChange, label, disabled }:
  { on: boolean; onChange?: (v: boolean) => void; label: string; disabled?: boolean }) {
  return <button role="switch" aria-checked={on} aria-label={label} disabled={disabled}
    className={`toggle${on ? " on" : ""}`} style={disabled ? { opacity: 0.4 } : undefined}
    onClick={(e) => { e.stopPropagation(); onChange?.(!on); }} />;
}

export function Box({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button role="checkbox" aria-checked={on} aria-label={label} className={`box${on ? " on" : ""}`}
    onClick={(e) => { e.stopPropagation(); onChange(!on); }}>{on ? "✓" : ""}</button>;
}

export function Bar({ pct, color }: { pct: number; color: string }) {
  return <div className="bar" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
    <div style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: color }} /></div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorLine({ error }: { error: string | null | undefined }) {
  return error ? <div className="error" role="alert">{error}</div> : null;
}

export function Loading({ what }: { what: string }) {
  return <div className="sub" style={{ padding: 24 }}>loading {what}…</div>;
}

export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="dialog-bg" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="row"><span style={{ fontWeight: 600, fontSize: 15 }} className="grow">{title}</span>
          <button className="btn-link" onClick={onClose} aria-label="close">✕</button></div>
        {children}
      </div>
    </div>
  );
}

declare global {
  interface Window { ModelViewerElement?: Record<string, string> }
}

let viewerLoad: Promise<unknown> | null = null;

function loadViewer(): Promise<unknown> {
  // Must be set before the module loads: model-viewer otherwise defaults its decoders to public CDNs.
  window.ModelViewerElement = { dracoDecoderLocation: "/decoders/draco/", ktx2TranscoderLocation: "/decoders/basis/",
    lottieLoaderLocation: "/decoders/lottie-unavailable.js" };
  return import("@google/model-viewer");
}

/** <model-viewer> bundled from npm (offline); three.js is large, so it loads on first use. */
export function ModelViewer({ src, height = 300 }: { src: string; height?: number | string }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    viewerLoad ??= loadViewer();
    let alive = true;
    void viewerLoad.then(() => { if (alive) setReady(true); });
    return () => { alive = false; };
  }, []);
  if (!ready) {
    return <div className="stripes sub" style={{ height, display: "flex", alignItems: "center",
      justifyContent: "center" }}>loading 3D viewer…</div>;
  }
  return createElement("model-viewer", {
    src, "camera-controls": true, "auto-rotate": true, "shadow-intensity": "0.6", "interaction-prompt": "none",
    style: { width: "100%", height, background: "radial-gradient(circle at 50% 40%,#2a2b2e,#141517 70%)",
      display: "block" },
  });
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export function StateText({ state, color }: { state: string; color: string }) {
  return <span className="sub" style={{ color }}>{state}</span>;
}

export function taskColor(state: string | undefined): string {
  if (!state) return NONE;
  if (state === "failed" || state === "blocked") return BAD;
  if (state === "succeeded") return OK;
  if (state === "cancelled") return NONE;
  return INFO;
}
