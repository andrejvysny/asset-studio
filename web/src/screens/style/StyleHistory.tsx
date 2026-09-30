import { useEffect } from "react";

import { ErrorLine, Loading, relTime } from "../../components/ui";
import { P, type StyleProfile, type StyleRevision } from "../../lib/api";
import { useApi } from "../../lib/hooks";
import { useProject } from "../../lib/project";

interface Props { styleId: string; saved: boolean; configRevision: number; onRestore: (content: StyleProfile) => void }

export function StyleHistory({ styleId, saved, configRevision, onRestore }: Props) {
  const { id } = useProject();
  const hist = useApi<{ style_id: string; revisions: StyleRevision[] }>(
    saved ? `${P(id)}/styles/${encodeURIComponent(styleId)}/revisions` : null, { project: id });
  const { reload } = hist;
  useEffect(() => { reload(); }, [configRevision, reload]);
  return (
    <div className="panel" style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">History · {styleId}</span>
      {!saved ? <span className="sub">save to start history</span> : !hist.data ? <Loading what="history" /> : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 320, overflow: "auto" }} aria-label="style revisions">
          {hist.data.revisions.map((r) => (
            <div key={r.sha256} className="row" style={{ gap: 8, alignItems: "flex-start", borderTop: "1px solid var(--line)", paddingTop: 6 }}
              aria-label={`revision ${r.sha256.slice(0, 8)}`}>
              <div className="grow" style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
                <span className="sub">{relTime(r.created_at)} · config r{r.config_revision} · {r.actor}
                  {r.current && <span className="tag" style={{ marginLeft: 6, color: "var(--ok)" }}>current</span>}</span>
                <span style={{ fontSize: 12 }} className="ellipsis">{r.content.guide.slice(0, 80) || <span className="dim">(empty guide)</span>}</span>
                <span className="row" style={{ gap: 3 }}>{r.content.palette.map((p, i) =>
                  <span key={i} title={p.hex} style={{ width: 12, height: 12, borderRadius: 3, background: p.hex, border: "1px solid var(--line-3)" }} />)}</span>
              </div>
              {!r.current && <button className="btn" onClick={() => onRestore(r.content)}>Restore into draft</button>}
            </div>
          ))}
          {hist.data.revisions.length === 0 && <span className="sub">No revisions yet.</span>}
        </div>
      )}
      <ErrorLine error={hist.error} />
    </div>
  );
}
