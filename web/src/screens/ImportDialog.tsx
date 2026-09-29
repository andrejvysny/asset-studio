import { useState } from "react";

import { Dialog, ErrorLine } from "../components/ui";
import { type CategoryNode, type Json, key, KIND_LABEL, type Kind, P, send, upload, uploadMany } from "../lib/api";
import { useAction } from "../lib/hooks";
import { useProject } from "../lib/project";
import { type AtlasParams, atlasPayload, DEFAULT_ATLAS, type FramesInfo, FramesOptions, type MapInfo,
  MaterialMapping, mappingReady } from "./importSets";

type Mode = "file" | "frames" | "material";
interface ModeInfo { id: Mode; label: string; accept: string; hint: string }

interface Preview {
  import_id: string; filename: string; size: number; sha256?: string; format: string; ok: boolean;
  allowed_kinds: Kind[]; suggested_kind: Kind; suggested_name: string;
  validation: { ok: boolean; checks: { id: string; ok: boolean; detail?: string; required?: boolean }[] };
  image?: Record<string, Json>; frames?: FramesInfo; maps?: MapInfo[]; map_roles?: string[];
}

const MODES: [ModeInfo, ...ModeInfo[]] = [
  { id: "file", label: "Single file", accept: ".png,.jpg,.jpeg,.glb", hint: "PNG, JPEG or self-contained GLB." },
  { id: "frames", label: "Frame sequence", accept: ".png,.zip",
    hint: "Several PNG frames, or one .zip of PNG frames → sprite sheet or VFX flipbook atlas." },
  { id: "material", label: "Material bundle", accept: ".png,.jpg,.jpeg",
    hint: "Texture maps of one size (base colour required); you assign each file a map role." },
];

async function previewFor(project: string, mode: Mode, files: File[]): Promise<Preview> {
  const [first] = files;
  if (first && files.length === 1 && (mode === "file" || first.name.toLowerCase().endsWith(".zip"))) {
    return upload<Preview>(`${P(project)}/imports:preview`, first);
  }
  return uploadMany<Preview>(`${P(project)}/imports:preview-set?mode=${mode}`, files);
}

function Checks({ prev }: { prev: Preview }) {
  return (
    <div className="panel">
      {prev.validation.checks.map((c) => (
        <div key={c.id} className="row tr" style={{ padding: "6px 12px" }}>
          <span className="dot" style={{ background: c.ok ? "var(--ok)" : c.required === false ? "var(--dim)" : "var(--bad)" }} />
          <span className="mono grow" style={{ fontSize: 12 }}>{c.id}</span>
          <span className="sub">{c.ok ? "ok" : c.required === false ? "info" : "FAIL"} {c.detail ?? ""}</span>
        </div>
      ))}
    </div>
  );
}

/** Reviewed import: safe inspection first, explicit mapping + commit second. Licence stays "unknown" unless given. */
export function ImportDialog({ categories, defaultCategory, onClose, onDone }: {
  categories: CategoryNode[]; defaultCategory: string | null; onClose: () => void; onDone: (assetId: string) => void;
}) {
  const { id } = useProject();
  const [mode, setMode] = useState<Mode>("file");
  const [prev, setPrev] = useState<Preview | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind | "">("");
  const [cat, setCat] = useState(defaultCategory ?? "");
  const [licence, setLicence] = useState("unknown");
  const [credit, setCredit] = useState("");
  const [atlas, setAtlas] = useState<AtlasParams>(DEFAULT_ATLAS);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const act = useAction();
  const commitKey = useState(key)[0];
  const modeInfo = MODES.find((m) => m.id === mode) ?? MODES[0];
  const pick = (files: File[]) => void act.run(async () => {
    const p = await previewFor(id, mode, files);
    setPrev(p);
    setName(p.suggested_name);
    setKind(p.suggested_kind ?? p.allowed_kinds[0]);
    setMapping(Object.fromEntries((p.maps ?? []).filter((m) => m.suggested_role).map((m) => [m.filename, m.suggested_role ?? ""])));
  });
  const ready = !!prev?.ok && !!name.trim() && !!kind && (prev.format !== "material_bundle" || mappingReady(prev.maps ?? [], mapping));
  const commit = () => void act.run(async () => {
    if (!prev || !kind) return;
    const r = await send<{ asset_id: string }>("POST", `${P(id)}/imports:commit`, {
      import_id: prev.import_id, name: name.trim(), kind, category_id: cat || null, licence: licence || "unknown",
      credit: credit || null, idempotency_key: commitKey,
      parameters: prev.format === "frames" ? atlasPayload(atlas, kind) : {},
      map_roles: prev.format === "material_bundle" ? mapping : {} });
    onDone(r.asset_id);
  });
  return (
    <Dialog title="Import" onClose={onClose}>
      <div className="row" role="tablist" style={{ gap: 6 }}>
        {MODES.map((m) => <button key={m.id} role="tab" aria-selected={m.id === mode} className={`chip${m.id === mode ? " on" : ""}`}
          onClick={() => { setMode(m.id); setPrev(null); }}>{m.label}</button>)}
      </div>
      <input key={mode} type="file" accept={modeInfo.accept} multiple={mode !== "file"} aria-label="file"
        onChange={(e) => { const fs = Array.from(e.target.files ?? []); if (fs.length) pick(fs); }} />
      <div className="sub">{modeInfo.hint} Files are inspected on the server before anything is stored.</div>
      {prev && (
        <>
          <Checks prev={prev} />
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
              {prev.frames && kind && <FramesOptions info={prev.frames} kind={kind} value={atlas} onChange={setAtlas} />}
              {prev.maps && <MaterialMapping maps={prev.maps} roles={prev.map_roles ?? []} value={mapping} onChange={setMapping} />}
              <div className="row" style={{ alignItems: "flex-start" }}>
                <label className="field grow"><span>Licence / rights</span>
                  <input className="input" value={licence} onChange={(e) => setLicence(e.target.value)} /></label>
                <label className="field grow"><span>Credit / source (optional)</span>
                  <input className="input" value={credit} onChange={(e) => setCredit(e.target.value)} /></label>
              </div>
              <div className="row">
                <button className="btn btn-primary" disabled={act.busy || !ready} onClick={commit}>Publish as v1 (imported)</button>
                <span className="sub">{prev.filename}{prev.sha256 ? ` · sha256 ${prev.sha256.slice(0, 12)}…` : ""}</span>
              </div>
            </>
          ) : <div className="banner bad">This import failed structural validation and cannot be published.</div>}
        </>
      )}
      <ErrorLine error={act.error} />
    </Dialog>
  );
}
