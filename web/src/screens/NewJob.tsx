import { useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ReferenceEditor, type RefDraft } from "../components/references";
import { ErrorLine, Loading } from "../components/ui";
import { type EnhancePreset, J, type Json, key, type Kind, KIND_LABEL, KINDS, type RecipeInfo, send } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { addReference, getJob, runJob } from "../lib/jobsApi";
import { useConfig, useProject } from "../lib/project";

const SUB: Record<Kind, string> = {
  model3d: "image → cut-out → mesh → GLB", sprite: "image → cut-out → PNG", icon: "inner art → fixed frame",
  vfx_flipbook: "keyframes → in-betweens → atlas", material: "tileable → PBR maps", sprite_sheet: "pose keys → frames → sheet",
  concept_art: "image → upscale",
};
const PRESETS: [EnhancePreset, string, string][] = [
  ["conservative", "Conservative", "Clarifies wording and adds the project style. Keeps every fact in your brief and invents nothing."],
  ["creative", "Creative", "Keeps your facts but proposes details for what you left open. Additions are marked for review."],
];

function show(v: Json | undefined): string {
  if (v === undefined || v === null) return "—";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

interface Pending { jobId: string; lib: RefDraft[] }

export function NewJob() {
  const { id } = useProject();
  const nav = useNavigate();
  const [sp] = useSearchParams();
  const cfg = useConfig();
  const caps = useApi<{ recipes: RecipeInfo[] }>("/api/v1/capabilities");
  const target = sp.get("target");
  const [kind, setKind] = useState<Kind>((sp.get("kind") as Kind | null) ?? "concept_art");
  const [cat, setCat] = useState<string | null>(sp.get("cat"));
  const [title, setTitle] = useState(target ? `New version of ${sp.get("name") ?? ""}` : "");
  const [brief, setBrief] = useState("");
  const [refs, setRefs] = useState<RefDraft[]>([]);
  const [preset, setPreset] = useState<EnhancePreset>("conservative");
  const act = useAction();
  const idem = useState(key)[0];
  const pending = useRef<Pending | null>(null);
  const cats = cfg.data?.categories ?? [];
  const config = cfg.data?.config;
  const catKindOf = (cid: string): Kind | null => (cfg.data?.effective[cid]?.kind?.value as Kind | null | undefined) ?? null;
  const eff = cat ? cfg.data?.effective[cat] : undefined;
  const effectiveKind: Kind = (cat ? catKindOf(cat) : null) ?? kind;
  const recipe = caps.data?.recipes.find((r) => r.kind === effectiveKind);
  const blocked = recipe && !["ready", "experimental"].includes(recipe.generation.state);
  if (!cfg.data) return <Loading what="configuration" />;

  const pickKind = (k: Kind) => {
    setKind(k);
    if (cat && catKindOf(cat) && catKindOf(cat) !== k) setCat(cats.find((c) => catKindOf(c.id) === k)?.id ?? null);
  };
  const shownCats = cats.filter((c) => !catKindOf(c.id) || catKindOf(c.id) === effectiveKind);

  const styleId = eff?.style?.value as string | null | undefined;
  const qaId = eff?.qa_ruleset?.value as string | null | undefined;
  const candDefault = recipe?.params.find((p) => p.key === "candidate_count")?.default;
  const params: [string, string, string][] = [
    ["Pipeline", show(eff?.recipe_id?.value ?? recipe?.id), eff?.recipe_id?.value ? eff.recipe_id.source : "built-in"],
    ["Candidates / round", show(eff?.candidate_count?.value ?? candDefault ?? 4),
      eff?.candidate_count?.value ? eff.candidate_count.source : "pipeline default"],
    ["Budget", show(eff?.budget?.value), eff?.budget?.value ? `from ${eff.budget.source}` : ""],
    ["Project style", styleId ? (config?.styles[styleId]?.label ?? styleId) : "—", styleId ? eff?.style?.source ?? "" : ""],
    ["QA rule set", qaId ? `${qaId} · ${config?.qa_rulesets[qaId]?.rules.filter((r) => r.enabled).length ?? 0} checks` : "—",
      qaId ? eff?.qa_ruleset?.source ?? "schema" : ""],
  ];

  /** Creates the Job (once; a retry resumes), adds library references, then optionally runs it. */
  const submit = (run: boolean) => void act.run(async () => {
    let st = pending.current;
    if (!st) {
      const uploads = refs.filter((r) => r.origin === "upload");
      const out = await send<{ job: { id: string } }>("POST", J(id), {
        title: title.trim(), category_id: cat, kind: (cat && catKindOf(cat)) ? null : kind, enhance_preset: preset,
        idempotency_key: idem, source: target ? "new version" : "manual",
        items: [{ name: title.trim(), brief: brief.trim() || title.trim(), target_asset_id: target, enhance_preset: preset,
          references: uploads.map((r) => ({ artifact_id: r.artifact_id, note: r.note, crop: r.crop, label: r.label })) }] });
      st = { jobId: out.job.id, lib: refs.filter((r) => r.origin === "library") };
      pending.current = st;
    }
    for (let r = st.lib[0]; r; r = st.lib[0]) {
      const item = (await getJob(id, st.jobId)).items[0];
      if (!item) throw new Error("the new Job has no item");
      await addReference(id, st.jobId, item.id, { library: { asset_id: r.library?.asset_id ?? "",
        version_id: r.library?.version_id ?? "" }, note: r.note, crop: r.crop ?? undefined, label: r.label,
        expected_item_revision: item.revision });
      st.lib.shift();
    }
    if (run) await runJob(id, st.jobId);
    nav(`/p/${id}/jobs/${st.jobId}`);
  });

  return (
    <div className="content" style={{ maxWidth: 1120, gap: 18 }}>
      <div>
        <Link to={`/p/${id}/jobs`} className="sub" style={{ textDecoration: "none" }}>← Jobs / New Job · one asset</Link>
        <h1 className="h1" style={{ margin: "2px 0 0" }}>What are you making?</h1>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(160px,1fr))", gap: 8 }}>
        {KINDS.map((k) => {
          const r = caps.data?.recipes.find((x) => x.kind === k);
          const on = effectiveKind === k;
          const state = r && r.generation.state !== "ready" ? r.generation.state.replaceAll("_", " ") : null;
          return (
            <button key={k} aria-pressed={on} onClick={() => pickKind(k)} className="panel"
              style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 2, textAlign: "left",
                borderColor: on ? "var(--dim)" : undefined, background: on ? "var(--card)" : undefined }}>
              <span style={{ fontWeight: 500 }}>{KIND_LABEL[k]}</span>
              <span className="dim" style={{ fontSize: 11.5 }}>{SUB[k]}</span>
              {state && <span className="sub" style={{ color: "var(--warn)" }}>{state}</span>}
            </button>
          );
        })}
      </div>
      {blocked && recipe && <div className="banner bad">{KIND_LABEL[effectiveKind]} generation is unavailable: {recipe.generation.reason}.
        {" "}You can still import files of this kind from the Assets screen.</div>}
      {recipe?.build.state !== "ready" && recipe && !blocked &&
        <div className="banner note">Candidates can be generated and reviewed; the {KIND_LABEL[effectiveKind]} build step is not available yet ({recipe.build.reason}).</div>}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.3fr) minmax(0,1fr)", gap: 22 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <label className="field"><span>Name</span>
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <label className="field"><span>Brief · what you want, in your words</span>
            <textarea className="input" rows={3} value={brief} onChange={(e) => setBrief(e.target.value)}
              style={{ font: "400 12.5px/1.55 var(--sans)" }} /></label>
          <ReferenceEditor project={id} refs={refs} onChange={setRefs} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <div className="field"><span>Category · spec is inherited</span>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              <button className={`chip mono${cat === null ? " on" : ""}`} aria-pressed={cat === null} onClick={() => setCat(null)}>none</button>
              {shownCats.map((c) => <button key={c.id} className={`chip mono${cat === c.id ? " on" : ""}`}
                aria-pressed={cat === c.id} onClick={() => setCat(c.id)}>{c.path}</button>)}
            </div></div>
          <div className="field"><span>Prompt enhancement</span>
            <div className="seg" role="group" aria-label="prompt enhancement" style={{ alignSelf: "flex-start" }}>
              {PRESETS.map(([k, label]) => (
                <button key={k} className={preset === k ? "on" : ""} aria-pressed={preset === k}
                  onClick={() => setPreset(k)}>{label}</button>))}
            </div>
            <span className="dim" style={{ fontSize: 11.5 }}>{PRESETS.find(([k]) => k === preset)?.[2]}</span>
          </div>
          <div className="table">
            {params.map(([k, v, src]) => (
              <div key={k} className="td" style={{ gridTemplateColumns: "130px minmax(0,1fr)", padding: "7px 12px" }}>
                <span className="dim" style={{ fontSize: 12 }}>{k}</span>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="mono ellipsis" style={{ fontSize: 12 }} title={v}>{v}</span>
                  <span style={{ fontSize: 10.5, color: "var(--faint)" }}>{src}</span></div>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="row" style={{ borderTop: "1px solid var(--line)", paddingTop: 16, gap: 10, flexWrap: "wrap" }}>
        <button className="btn btn-primary" disabled={act.busy || !title.trim()} onClick={() => submit(false)}>Save Job</button>
        <button className="btn" disabled={act.busy || !title.trim() || !!blocked} onClick={() => submit(true)}>Save and run</button>
        <span className="dim" style={{ fontSize: 12 }}>Save keeps it as a draft for a Batch. Save and run enhances the prompt now,
          then stops for you to confirm it.</span>
      </div>
      <ErrorLine error={act.error} />
    </div>
  );
}
