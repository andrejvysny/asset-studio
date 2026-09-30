import { useEffect, useState } from "react";

import { BAD, INFO, NONE, OK, WARN } from "../../components/ui";
import { ApiError, type Effect, type EffectClass, type EffectsView, KIND_LABEL, KINDS, type Kind } from "../../lib/api";
import { useProject } from "../../lib/project";
import { getConfigEffects } from "../../lib/styleApi";

const ORDER: EffectClass[] = ["applied", "conditioning_only", "advisory_only", "not_applicable", "unsupported"];
const COLOR: Record<EffectClass, string> = { applied: OK, conditioning_only: INFO, advisory_only: NONE, not_applicable: NONE, unsupported: WARN };
const val = (v: Effect["value"]): string => (v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));

interface Props { categories: { id: string; label: string }[]; configRevision: number; dirty: boolean }

export function EffectsPanel({ categories, configRevision, dirty }: Props) {
  const { id } = useProject();
  const [cat, setCat] = useState("");
  const [kind, setKind] = useState<Kind | "">("");
  const [view, setView] = useState<EffectsView | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    if (!cat && !kind) { setView(null); setHint("Project defaults name no kind: pick a category or a kind"); return; }  // would 422
    getConfigEffects(id, { categoryId: cat, kind }).then((v) => { if (live) { setView(v); setHint(null); } }).catch((e: unknown) => {
      if (!live) return;
      setView(null);
      setHint(e instanceof ApiError && e.code === "unresolved" ? `${e.message} — pick a kind` : (e as Error).message);
    });
    return () => { live = false; };
  }, [id, cat, kind, configRevision]);
  return (
    <div className="panel" style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">How this style is applied (planned, for new Jobs)</span>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <select className="input" style={{ width: "auto" }} aria-label="effects category" value={cat} onChange={(e) => setCat(e.target.value)}>
          <option value="">project defaults</option>{categories.map((c) => <option key={c.id} value={c.id}>{c.label || c.id}</option>)}</select>
        <select className="input" style={{ width: "auto" }} aria-label="effects kind" value={kind} onChange={(e) => setKind(e.target.value as Kind | "")}>
          <option value="">kind from scope</option>{KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}</select>
        {view && <span className="sub">recipe {view.recipe.id} v{view.recipe.version} · {view.mode}</span>}
      </div>
      {dirty && <span className="sub" style={{ color: WARN }}>Reflects the saved configuration — save to update</span>}
      {hint && <span className="sub" aria-label="effects hint">{hint}</span>}
      {view && ORDER.map((cls) => {
        const rows = view.effects.filter((e) => e.classification === cls);
        return rows.map((e, i) => (
          <div key={`${cls}-${i}`} className="row" style={{ gap: 8, alignItems: "flex-start", borderTop: "1px solid var(--line)", paddingTop: 6 }}
            aria-label={`effect ${e.field} ${cls}`}>
            <span className="tag" style={{ color: COLOR[cls], borderColor: COLOR[cls], flex: "none" }}>{cls.replace("_", " ")}</span>
            <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
              <span style={{ fontSize: 12 }}><span className="mono">{e.field}</span> <span className="dim">→ {e.consumer}</span></span>
              {val(e.value) && <span className="mono ellipsis" style={{ fontSize: 11, color: cls === "unsupported" ? BAD : "var(--muted)" }}>{val(e.value)}</span>}
              {e.note && <span className="sub" style={{ fontFamily: "var(--sans)" }}>{e.note}</span>}
            </div>
          </div>
        ));
      })}
      {view && view.effects.length === 0 && <span className="sub">Nothing is configured for this scope.</span>}
    </div>
  );
}
