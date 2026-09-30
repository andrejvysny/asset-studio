import { Toggle } from "../../components/ui";
import { KIND_LABEL, KINDS, type Kind, type StyleProfile } from "../../lib/api";

interface Props { style: StyleProfile; edit: (fn: (s: StyleProfile) => void) => void }

export function StyleFields({ style, edit }: Props) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.2fr) minmax(0,1fr)", gap: 20 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <span className="label">Style guide</span>
        <textarea className="input" rows={8} aria-label="style guide" value={style.guide} placeholder="Optional. Empty is fine."
          onChange={(e) => edit((s) => { s.guide = e.target.value; })} />
        <span className="sub" style={{ fontFamily: "var(--sans)" }}>Assign the profile to categories in Schema (field “Style”).</span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <span className="label">Palette · reserved colours fail the palette check outside their allowed kinds</span>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8 }}>
          {style.palette.map((p, i) => (
            <div key={i} className="panel" style={{ borderColor: p.reserved ? "var(--warn)" : undefined }}>
              <input type="color" aria-label="colour" value={p.hex} style={{ width: "100%", height: 38, border: 0, padding: 0, background: "none" }}
                onChange={(e) => edit((s) => { s.palette[i]!.hex = e.target.value; })} />
              <div style={{ padding: "5px 7px", display: "flex", flexDirection: "column", gap: 3 }}>
                <span className="mono" style={{ fontSize: 10.5 }}>{p.hex}</span>
                <label className="row" style={{ gap: 5, fontSize: 10.5 }}><Toggle on={p.reserved} label="reserved"
                  onChange={(v) => edit((s) => { s.palette[i]!.reserved = v; })} />reserved</label>
                {p.reserved && <select className="input" style={{ fontSize: 10, padding: 2 }} aria-label="allowed kind"
                  value={p.allowed_kinds[0] ?? ""} onChange={(e) => edit((s) => { s.palette[i]!.allowed_kinds = e.target.value ? [e.target.value as Kind] : []; })}>
                  <option value="">allowed nowhere</option>{KINDS.map((k) => <option key={k} value={k}>only {KIND_LABEL[k]}</option>)}</select>}
                <button className="btn-link" style={{ padding: 0, fontSize: 10.5 }} onClick={() => edit((s) => { s.palette.splice(i, 1); })}>remove</button>
              </div>
            </div>
          ))}
          <button className="panel dim" style={{ minHeight: 80 }} onClick={() => edit((s) => { s.palette.push({ hex: "#888888", label: "",
            reserved: false, allowed_kinds: [], allowed_categories: [], tolerance_delta_e: 8 }); })}>+ colour</button>
        </div>
      </div>
    </div>
  );
}
