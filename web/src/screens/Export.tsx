import { PageHead } from "../components/ui";

const TARGETS: [string, string, [string, string][]][] = [
  ["Plain files + manifest", "on publish", [["Folder", "server path (mounted)"], ["Path pattern", "{category}/{asset_id}/{filename}"], ["Manifest", "manifest.json per asset"]]],
  ["Godot 4 project", "optional", [["Project path", "mounted Godot project"], ["Folder", "res://assets"], ["Import settings", "tested importer keys only"]]],
  ["Folder / Git", "manual", [["Repository", "existing repo, explicit branch"], ["Staging", "managed files only, never git add ."], ["Push", "separate explicit action"]]],
];

/** Export targets are Phase 4. Shown for orientation only; nothing here writes files or pretends to. */
export function Export() {
  return (
    <div className="content narrow" style={{ padding: "20px 26px 48px", gap: 18 }}>
      <PageHead sub="Storage is content-addressed. Exports will write readable paths from the naming rule." title="Export targets" />
      <div className="banner note">Export targets arrive in Phase 4 (frozen export plans, collision checks, receipts, retries).
        Publishing today commits versions to the library only; download files from an asset's detail page.</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(320px,1fr))", gap: 12, opacity: 0.6 }}>
        {TARGETS.map(([label, when, fields]) => (
          <div key={label} className="panel" aria-disabled="true">
            <div className="row" style={{ padding: "11px 14px", borderBottom: "1px solid #222326" }}>
              <span className="toggle" aria-hidden /><span style={{ fontWeight: 600 }} className="grow">{label}</span>
              <span className="sub">{when}</span></div>
            {fields.map(([k, v]) => <div key={k} className="row tr" style={{ padding: "7px 14px" }}>
              <span className="muted" style={{ width: 130, fontSize: 12 }}>{k}</span><span className="mono dim" style={{ fontSize: 11.5 }}>{v}</span></div>)}
          </div>
        ))}
      </div>
    </div>
  );
}
