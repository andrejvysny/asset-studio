import { useState } from "react";

import { Dialog, ErrorLine } from "../components/ui";
import { type CategoryNode, type Json, key, KIND_LABEL, type Kind, P, send, upload } from "../lib/api";
import { useAction } from "../lib/hooks";
import { useProject } from "../lib/project";

interface Preview {
  import_id: string; filename: string; size: number; sha256: string; format: string; ok: boolean;
  allowed_kinds: Kind[]; suggested_kind: Kind; suggested_name: string;
  validation: { ok: boolean; checks: { id: string; ok: boolean; detail?: string; required?: boolean }[] };
  image?: Record<string, Json>;
}

/** Reviewed import: safe inspection first, explicit mapping + commit second. Licence stays "unknown" unless given. */
export function ImportDialog({ categories, defaultCategory, onClose, onDone }: {
  categories: CategoryNode[]; defaultCategory: string | null; onClose: () => void; onDone: (assetId: string) => void;
}) {
  const { id } = useProject();
  const [prev, setPrev] = useState<Preview | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind | "">("");
  const [cat, setCat] = useState(defaultCategory ?? "");
  const [licence, setLicence] = useState("unknown");
  const [credit, setCredit] = useState("");
  const act = useAction();
  const commitKey = useState(key)[0];
  return (
    <Dialog title="Import file" onClose={onClose}>
      <input type="file" accept=".png,.jpg,.jpeg,.glb" aria-label="file" onChange={(e) => {
        const f = e.target.files?.[0];
        if (f) void act.run(async () => {
          const p = await upload<Preview>(`${P(id)}/imports:preview`, f);
          setPrev(p);
          setName(p.suggested_name);
          setKind(p.suggested_kind);
        });
      }} />
      <div className="sub">Accepted: PNG, JPEG, self-contained GLB. Files are inspected on the server before anything is stored.</div>
      {prev && (
        <>
          <div className="panel">
            {prev.validation.checks.map((c) => (
              <div key={c.id} className="row tr" style={{ padding: "6px 12px" }}>
                <span className="dot" style={{ background: c.ok ? "var(--ok)" : c.required === false ? "var(--dim)" : "var(--bad)" }} />
                <span className="mono grow" style={{ fontSize: 12 }}>{c.id}</span>
                <span className="sub">{c.ok ? "ok" : c.required === false ? "info" : "FAIL"} {c.detail ?? ""}</span>
              </div>
            ))}
          </div>
          {prev.ok ? (
            <>
              <label className="field"><span>Name</span>
                <input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
              <div className="row" style={{ alignItems: "flex-start" }}>
                <label className="field grow"><span>Kind</span>
                  <select className="input" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
                    {prev.allowed_kinds.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
                  </select></label>
                <label className="field grow"><span>Category</span>
                  <select className="input" value={cat} onChange={(e) => setCat(e.target.value)}>
                    <option value="">— unclassified —</option>
                    {categories.map((c) => <option key={c.id} value={c.id}>{c.path}</option>)}
                  </select></label>
              </div>
              <div className="row" style={{ alignItems: "flex-start" }}>
                <label className="field grow"><span>Licence / rights</span>
                  <input className="input" value={licence} onChange={(e) => setLicence(e.target.value)} /></label>
                <label className="field grow"><span>Credit / source (optional)</span>
                  <input className="input" value={credit} onChange={(e) => setCredit(e.target.value)} /></label>
              </div>
              <div className="row">
                <button className="btn btn-primary" disabled={act.busy || !name.trim() || !kind}
                  onClick={() => void act.run(async () => {
                    const r = await send<{ asset_id: string }>("POST", `${P(id)}/imports:commit`, {
                      import_id: prev.import_id, name: name.trim(), kind, category_id: cat || null,
                      licence: licence || "unknown", credit: credit || null, idempotency_key: commitKey });
                    onDone(r.asset_id);
                  })}>Publish as v1 (imported)</button>
                <span className="sub">{prev.filename} · sha256 {prev.sha256.slice(0, 12)}…</span>
              </div>
            </>
          ) : <div className="banner bad">This file failed structural validation and cannot be published.</div>}
        </>
      )}
      <ErrorLine error={act.error} />
    </Dialog>
  );
}
