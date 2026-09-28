import { ErrorLine } from "../components/ui";
import { useApi } from "../lib/hooks";

interface Gpu { name: string; vram_used_mb: number; vram_total_mb: number; util_pct: number }
interface WorkerHealth { reachable: boolean; error?: string; ok?: boolean; loaded?: boolean; trellis_loaded?: boolean;
  birefnet_loaded?: boolean; models?: Record<string, boolean>; model_present?: boolean; gpu?: Gpu | null }
export interface RuntimeInfo {
  comfyui: { reachable: boolean; devices?: { name: string; vram_total: number; vram_free: number }[]; system?: { comfyui_version: string } };
  queue: { running: number; pending: number };
  workers: Record<string, WorkerHealth>;
  models: { key: string; repo: string; revision: string; status: string; note: string; optional: boolean; gated: boolean }[];
  licences: { name: string; scope: string; licence: string; status: "cleared" | "not_cleared" | "review"; note: string }[];
}

const STATUS_COLOR: Record<string, string> = { ok: "var(--ok)", cleared: "var(--ok)", missing: "var(--bad)", stale: "var(--bad)",
  invalid: "var(--bad)", incomplete: "var(--bad)", not_cleared: "var(--bad)", review: "var(--warn)" };

function Bar({ used, total }: { used: number; total: number }) {
  return <div style={{ height: 6, background: "var(--line)", borderRadius: 3 }}>
    <div style={{ height: 6, borderRadius: 3, width: `${total ? (used / total) * 100 : 0}%`, background: "var(--info)" }} /></div>;
}

function GpuCard({ title, state, used, total, rows }: { title: string; state: string; used: number; total: number; rows: [string, string][] }) {
  return (
    <div className="panel" style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row"><span style={{ fontWeight: 600 }} className="grow">{title}</span><span className="sub">{state}</span></div>
      <Bar used={used} total={total} />
      <span className="sub">VRAM {(used / 1024).toFixed(1)} / {(total / 1024).toFixed(1)} GB</span>
      {rows.map(([k, v]) => <div key={k} className="row" style={{ fontSize: 12 }}><span className="dim grow">{k}</span><span className="mono">{v}</span></div>)}
    </div>
  );
}

export function Runtime() {
  const rt = useApi<RuntimeInfo>("/api/runtime", 5000);
  const r = rt.data;
  const dev = r?.comfyui.devices?.[0];
  const ps = r?.workers["prompt-service"];
  const tw = r?.workers["trellis-worker"];
  const gpu1 = tw?.gpu ?? ps?.gpu;
  const grid = "220px minmax(200px,1.4fr) 90px 100px minmax(160px,1.6fr)";

  return (
    <div className="page" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <div><div className="sub">compose project line-a · 2 × RTX 4090</div><div className="h1">Runtime</div></div>
      <ErrorLine error={rt.error} />
      {r && <>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))", gap: 14 }}>
          <GpuCard title="GPU0 · image" state={!r.comfyui.reachable ? "ComfyUI down" : r.queue.running ? "busy" : "idle"}
            used={dev ? (dev.vram_total - dev.vram_free) / 2 ** 20 : 0} total={dev ? dev.vram_total / 2 ** 20 : 0}
            rows={[["ComfyUI", r.comfyui.system?.comfyui_version ?? (r.comfyui.reachable ? "up" : "down")],
              ["queue", `${r.queue.running} running · ${r.queue.pending} pending`], ["model", "Qwen-Image-2512 fp8"]]} />
          <GpuCard title="GPU1 · VLM + 3D (time-shared)" state={gpu1 ? `${gpu1.util_pct}% util` : "unknown"}
            used={gpu1?.vram_used_mb ?? 0} total={gpu1?.vram_total_mb ?? 0}
            rows={[["prompt-service", !ps?.reachable ? "down" : ps.loaded ? "Qwen3-VL loaded" : "idle (unloaded)"],
              ["trellis-worker", !tw?.reachable ? "down" : tw.trellis_loaded ? "TRELLIS loaded" : tw.birefnet_loaded ? "BiRefNet loaded" : "idle (unloaded)"],
              ["3D ready", tw?.models ? Object.entries(tw.models).filter(([, ok]) => !ok).map(([k]) => `missing ${k}`).join(", ") || "yes" : "—"]]} />
        </div>

        <div>
          <div className="label" style={{ marginBottom: 8 }}>Models · config/models.yaml vs installed</div>
          <div className="panel">
            {r.models.map((m, i) => (
              <div key={m.key} className={i ? "tr" : ""} style={{ display: "grid", gridTemplateColumns: grid, gap: 12, padding: "8px 14px", fontSize: 12 }}>
                <span className="mono">{m.key}</span><span className="mono dim ellipsis">{m.repo}</span><span className="mono dim">{m.revision}</span>
                <span className="mono" style={{ color: m.status === "missing" && m.optional ? "var(--dim)" : STATUS_COLOR[m.status] }}>{m.status}</span>
                <span className="dim">{m.note}{m.gated ? " · gated" : ""}{m.optional ? " · optional" : ""}</span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="label" style={{ marginBottom: 8 }}>Runtime licence inventory · code actually executed</div>
          <div className="panel">
            {r.licences.map((l, i) => (
              <div key={l.name} className={i ? "tr" : ""} style={{ display: "grid", gridTemplateColumns: "240px 220px 100px minmax(160px,1fr)", gap: 12, padding: "8px 14px", fontSize: 12 }}>
                <span>{l.name}</span><span className="mono dim">{l.licence}</span>
                <span className="mono" style={{ color: STATUS_COLOR[l.status] }}>{l.status.replace("_", " ")}</span><span className="dim">{l.note}</span>
              </div>
            ))}
          </div>
        </div>
      </>}
    </div>
  );
}
