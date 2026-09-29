import { useState } from "react";

import { Dialog, ErrorLine } from "../../components/ui";
import { key, send } from "../../lib/api";
import { useAction } from "../../lib/hooks";

interface Props { base: string; itemId: string; runId: string; revision: number; onClose: () => void; onDone: () => void }

/** New GLB from the stored TRELLIS.2 raw output; no resampling, the approval binding is unchanged. */
export function ReexportDialog({ base, itemId, runId, revision, onClose, onDone }: Props) {
  const [exporter, setExporter] = useState("clean");
  const [texture, setTexture] = useState(2048);
  const [triangles, setTriangles] = useState(100000);
  const [remesh, setRemesh] = useState(false);
  const act = useAction();
  const submit = () => void act.run(async () => {
    const res = await send<{ results: { ok: boolean; message?: string }[] }>("POST", `${base}:reexport`, {
      idempotency_key: key(), items: [{ item_id: itemId, build_run_id: runId, expected_item_revision: revision,
        overrides: { exporter, texture_size: texture, triangles, remesh } }] });
    if (res.results[0] && !res.results[0].ok) throw new Error(res.results[0].message);
    onDone();
  });
  return (
    <Dialog title="Re-export from raw" onClose={onClose}>
      <div className="sub">Reuses the stored TRELLIS.2 output of this build. Only export settings change; the result is a
        new build that must be accepted again.</div>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <label className="field grow"><span>Exporter</span>
          <select className="input" value={exporter} onChange={(e) => setExporter(e.target.value)}>
            <option value="clean">clean (default, MIT)</option>
            <option value="research">research (nvdiffrast, evaluation only)</option>
          </select></label>
        <label className="field grow"><span>Texture</span>
          <select className="input" value={texture} onChange={(e) => setTexture(Number(e.target.value))}>
            {[1024, 2048, 4096].map((s) => <option key={s} value={s}>{s}²</option>)}
          </select></label>
      </div>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <label className="field grow"><span>Triangles (used when the category sets no budget)</span>
          <input className="input" type="number" min={1000} max={2000000} value={triangles}
            onChange={(e) => setTriangles(Number(e.target.value))} /></label>
        <label className="row grow" style={{ gap: 6, marginTop: 20 }}>
          <input type="checkbox" checked={remesh} onChange={(e) => setRemesh(e.target.checked)} />
          <span className="sub">remesh (rebuilds topology)</span></label>
      </div>
      {exporter === "research" && <div className="banner warn">Research exporter output is licensed for research and
        evaluation only and is recorded as not cleared on the published version.</div>}
      <div className="row"><button className="btn btn-primary" disabled={act.busy} onClick={submit}>Re-export</button></div>
      <ErrorLine error={act.error} />
    </Dialog>
  );
}
