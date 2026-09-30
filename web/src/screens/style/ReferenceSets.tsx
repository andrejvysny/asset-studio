import { useState } from "react";

import { MediaPicker } from "../../components/MediaPicker";
import { ErrorLine } from "../../components/ui";
import { artifactUrl, P, upload } from "../../lib/api";
import { useAction } from "../../lib/hooks";
import { clone, useProject } from "../../lib/project";
import type { DraftProps } from "./shared";

export function ReferenceSets({ draft, setDraft }: DraftProps) {
  const { id } = useProject();
  const act = useAction();
  const [pickFor, setPickFor] = useState<string | null>(null);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row"><span className="label grow">Reference sets · up to 4 images · mode says exactly how they are used</span>
        <button className="btn" onClick={() => { const name = window.prompt("Reference set id"); if (!name) return;
          const n = clone(draft); n.reference_sets[name] = { label: name, mode: "prompt_guidance", images: [] }; setDraft(n); }}>+ Set</button></div>
      <span className="sub" style={{ fontFamily: "var(--sans)" }}>Applies to Jobs created from now on; existing Jobs keep their frozen configuration.</span>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))", gap: 12 }}>
        {Object.entries(draft.reference_sets).map(([rid, rs]) => (
          <div key={rid} className="panel" style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="row"><span style={{ fontWeight: 500 }} className="grow">{rs.label || rid}</span><span className="sub">{rs.images.length}/4</span></div>
            <select className="input" value={rs.mode} aria-label="reference mode" onChange={(e) => { const n = clone(draft); n.reference_sets[rid]!.mode = e.target.value; setDraft(n); }}>
              <option value="prompt_guidance">prompt guidance — enhancer reads the images (after item refs, 4-image limit)</option>
              <option value="qa_reference">QA reference — compared against candidates</option>
              <option value="image_conditioning">image conditioning — not available; generation is blocked</option></select>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 6 }}>
              {rs.images.map((im, j) => <button key={im.artifact_id} title="remove" onClick={() => { const n = clone(draft); n.reference_sets[rid]!.images.splice(j, 1); setDraft(n); }}>
                <img src={artifactUrl(id, im.artifact_id)} alt={im.label} style={{ width: "100%", aspectRatio: "1", objectFit: "cover", borderRadius: 5 }} /></button>)}
              {rs.images.length < 4 && <label className="panel dim" style={{ aspectRatio: "1", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", borderStyle: "dashed" }}>+
                <input type="file" accept=".png,.jpg,.jpeg,.webp" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void act.run(async () => {
                  const r = await upload<{ artifact_id: string }>(`${P(id)}/references:upload`, f);
                  const n = clone(draft); n.reference_sets[rid]!.images.push({ artifact_id: r.artifact_id, label: f.name, role: "", source_rights: "unknown" }); setDraft(n); }); }} /></label>}
              {rs.images.length < 4 && <button className="panel dim" aria-label={`add media to ${rid}`} style={{ aspectRatio: "1", fontSize: 11, cursor: "pointer" }}
                onClick={() => setPickFor(rid)}>media</button>}
            </div>
          </div>
        ))}
        {Object.keys(draft.reference_sets).length === 0 && <div className="empty">No reference sets.</div>}
      </div>
      <ErrorLine error={act.error} />
      {pickFor && <MediaPicker project={id} onClose={() => setPickFor(null)} onPick={(m) => {
        const n = clone(draft); n.reference_sets[pickFor]?.images.push({ artifact_id: m.artifact_id, label: m.name, role: "",
          source_rights: m.source_rights }); setDraft(n); setPickFor(null); }} />}
    </div>
  );
}
