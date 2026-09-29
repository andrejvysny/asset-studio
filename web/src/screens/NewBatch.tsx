import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ErrorLine, Loading, PageHead } from "../components/ui";
import { type Json, key, KIND_LABEL, KINDS, type Kind, P, type RecipeInfo, send } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { useConfig, useProject } from "../lib/project";

function parseLines(text: string): { name: string; brief: string }[] {
  return text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const i = l.indexOf(":");
    return i > 0 ? { name: l.slice(0, i).trim(), brief: l.slice(i + 1).trim() } : { name: l, brief: l };
  });
}

function show(v: Json): string {
  if (v === null) return "—";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

export function NewBatch() {
  const { id } = useProject();
  const nav = useNavigate();
  const [sp] = useSearchParams();
  const cfg = useConfig();
  const caps = useApi<{ recipes: RecipeInfo[] }>("/api/v1/capabilities");
  const target = sp.get("target");
  const [kind, setKind] = useState<Kind>((sp.get("kind") as Kind | null) ?? "concept_art");
  const [cat, setCat] = useState<string | null>(sp.get("cat"));
  const [title, setTitle] = useState(target ? `New version of ${sp.get("name") ?? ""}` : "");
  const [lines, setLines] = useState(target ? `${sp.get("name") ?? ""}: ` : "");
  const act = useAction();
  const idem = useState(key)[0];
  const items = useMemo(() => parseLines(lines), [lines]);
  const cats = cfg.data?.categories ?? [];
  const eff = cat ? cfg.data?.effective[cat] : undefined;
  const catKind = eff?.kind?.value as Kind | null | undefined;
  const effectiveKind: Kind = catKind ?? kind;
  const recipe = caps.data?.recipes.find((r) => r.kind === effectiveKind);
  const blocked = recipe && !["ready", "experimental"].includes(recipe.generation.state);
  if (!cfg.data) return <Loading what="configuration" />;
  return (
    <div className="content" style={{ maxWidth: 1080 }}>
      <PageHead sub="New batch" title="What are you making?" />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(160px,1fr))", gap: 8 }}>
        {KINDS.map((k) => {
          const r = caps.data?.recipes.find((x) => x.kind === k);
          const on = effectiveKind === k;
          const locked = catKind !== undefined && catKind !== null && catKind !== k;
          return (
            <button key={k} disabled={locked} aria-pressed={on} onClick={() => setKind(k)} className="panel"
              style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 2, textAlign: "left",
                borderColor: on ? "var(--dim)" : undefined, background: on ? "var(--active-2)" : undefined, opacity: locked ? 0.4 : 1 }}>
              <span style={{ fontWeight: 500 }}>{KIND_LABEL[k]}</span>
              <span className="sub" style={{ fontFamily: "var(--sans)", fontSize: 11.5 }}>
                {r ? (r.generation.state === "ready" ? "generation ready" : r.generation.state.replaceAll("_", " ")) : "…"}</span>
            </button>
          );
        })}
      </div>
      {blocked && recipe && <div className="banner bad">{KIND_LABEL[effectiveKind]} generation is unavailable: {recipe.generation.reason}.
        {" "}You can still import files of this kind from the Assets screen.</div>}
      {recipe?.build.state !== "ready" && recipe && !blocked &&
        <div className="banner note">Candidates can be generated and reviewed; the {KIND_LABEL[effectiveKind]} build step is not available yet ({recipe.build.reason}).</div>}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.3fr) minmax(0,1fr)", gap: 20 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="muted" style={{ fontSize: 12 }}>Briefs · one asset per line</span>
            <span className="sub">{items.length} items</span></div>
          <textarea className="input" rows={10} aria-label="briefs" value={lines} onChange={(e) => setLines(e.target.value)}
            style={{ font: "400 12.5px/1.7 var(--mono)" }} placeholder={"Oak chair: simple ladder-back chair\nLong table: trestle table"} />
          <span className="sub" style={{ fontFamily: "var(--sans)" }}>Format: <span className="mono">name: brief</span>. Or pick rows in the shot list.</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label className="field"><span>Name</span>
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <div className="field"><span>Category · spec is inherited</span>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              <button className={`chip mono${cat === null ? " on" : ""}`} onClick={() => setCat(null)}>none</button>
              {cats.map((c) => <button key={c.id} className={`chip mono${cat === c.id ? " on" : ""}`}
                onClick={() => setCat(c.id)}>{c.path}</button>)}
            </div></div>
          {eff && (
            <div className="table">
              {Object.entries(eff).filter(([k, v]) => !k.startsWith("_") && !Array.isArray(v) && v.value !== null).map(([k, v]) => {
                const r = v as { value: Json; source: string };
                return (
                  <div key={k} className="td" style={{ gridTemplateColumns: "130px minmax(0,1fr)", padding: "7px 12px" }}>
                    <span className="dim" style={{ fontSize: 12 }}>{k}</span>
                    <div className="row" style={{ justifyContent: "space-between" }}>
                      <span className="mono ellipsis" style={{ fontSize: 12 }}>{show(r.value)}</span>
                      <span style={{ fontSize: 10.5, color: "var(--faint)" }}>{r.source}</span></div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
      <div className="row" style={{ borderTop: "1px solid var(--line)", paddingTop: 16, gap: 14 }}>
        <button className="btn btn-primary" disabled={act.busy || items.length === 0 || !title.trim() || !!blocked}
          onClick={() => void act.run(async () => {
            const out = await send<{ batch: { id: string } }>("POST", `${P(id)}/batches`, {
              title: title.trim(), category_id: cat, kind: catKind ? null : kind, idempotency_key: idem,
              source: target ? "new version" : `pasted · ${items.length} lines`,
              items: items.map((i) => ({ ...i, target_asset_id: target })) });
            nav(`/p/${id}/batches/${out.batch.id}`);
          })}>Create batch + enhance {items.length} prompts</button>
        <span className="sub" style={{ fontFamily: "var(--sans)", fontSize: 12 }}>
          Enhances every brief with the local text model, then stops for your review. No images are generated yet.</span>
      </div>
      <ErrorLine error={act.error} />
    </div>
  );
}
