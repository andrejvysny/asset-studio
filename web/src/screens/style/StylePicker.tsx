import { useState } from "react";

import { clone } from "../../lib/project";
import { type DraftProps, STYLE_ID_RE } from "./shared";

interface Props extends DraftProps { styleId: string; onSelect: (id: string) => void }

/** Style ids that categories or the project defaults point at; those cannot be removed. */
function usedBy(draft: DraftProps["draft"], styleId: string): string[] {
  const out: string[] = [];
  const is = (o?: { mode: string; value: unknown }) => o?.mode === "value" && o.value === styleId;
  if (is(draft.defaults.style)) out.push("project defaults");
  for (const c of draft.categories) if (is(c.defaults.style)) out.push(c.label || c.id);
  return out;
}

export function StylePicker({ draft, setDraft, styleId, onSelect }: Props) {
  const [err, setErr] = useState<string | null>(null);
  const ids = Object.keys(draft.styles);
  const style = draft.styles[styleId];
  const used = usedBy(draft, styleId);
  const add = () => {
    const name = window.prompt("Style id (a-z, 0-9, _ . -)")?.trim();
    if (!name) return;
    if (!STYLE_ID_RE.test(name)) return setErr(`“${name}” is not a valid id: use ^[a-z0-9][a-z0-9_.-]{0,63}$`);
    if (name in draft.styles) return setErr(`style “${name}” already exists`);
    const n = clone(draft);
    n.styles[name] = { label: name, guide: "", negative: "", palette: [] };
    setErr(null);
    setDraft(n);
    onSelect(name);
  };
  const remove = () => {
    if (!window.confirm(`Remove style “${styleId}” from the draft? Its revision history is kept on the server.`)) return;
    const n = clone(draft);
    delete n.styles[styleId];
    setDraft(n);
    onSelect("");
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }} role="group" aria-label="styles">
        {ids.map((k) => <button key={k} className={`chip${k === styleId ? " on" : ""}`} onClick={() => onSelect(k)}>{k}</button>)}
        <button className="chip" onClick={add}>+ Style</button>
      </div>
      {err && <div className="error">{err}</div>}
      {style && (
        <div className="row" style={{ gap: 8 }}>
          <span className="label">Label</span>
          <input className="input grow" aria-label="style label" value={style.label}
            onChange={(e) => { const n = clone(draft); n.styles[styleId]!.label = e.target.value; setDraft(n); }} />
          <button className="btn" disabled={used.length > 0} onClick={remove}
            title={used.length ? `In use by ${used.join(", ")}: reassign it in Schema first` : "Remove from the draft"}>Delete</button>
        </div>
      )}
    </div>
  );
}
