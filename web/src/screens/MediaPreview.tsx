import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Dialog, ErrorLine } from "../components/ui";
import { ApiError, artifactUrl } from "../lib/api";
import { useAction } from "../lib/hooks";
import { archiveMedia, type MediaItem, patchMedia, restoreMedia } from "../lib/mediaApi";

const size = (n: number): string => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const parseTags = (s: string): string[] => [...new Set(s.split(",").map((t) => t.trim()).filter(Boolean))];

interface Props { project: string; item: MediaItem; onClose: () => void; onChanged: () => void }

export function MediaPreview({ project, item, onClose, onChanged }: Props) {
  const nav = useNavigate();
  const act = useAction();
  const [stale, setStale] = useState(false);
  const [name, setName] = useState(item.name);
  const [note, setNote] = useState(item.note);
  const [tags, setTags] = useState(item.tags.join(", "));
  const [rights, setRights] = useState(item.source_rights);
  const [url, setUrl] = useState(item.source_url);
  const dirty = name !== item.name || note !== item.note || tags !== item.tags.join(", ") || rights !== item.source_rights
    || url !== item.source_url;
  const archived = item.archived_at !== null;
  const content = artifactUrl(project, item.artifact_id);

  const mutate = (fn: () => Promise<unknown>) => void act.run(async () => {
    setStale(false);
    try { await fn(); } catch (e) {
      if (e instanceof ApiError && e.status === 409) { setStale(true); onChanged(); }
      throw e;
    }
    onChanged();
  });
  const save = () => mutate(() => patchMedia(project, item.id, { expected_revision: item.revision, name: name.trim(),
    note, tags: parseTags(tags), source_rights: rights.trim(), source_url: url.trim() }));

  return (
    <Dialog title={item.name} onClose={onClose}>
      <div className="checker" style={{ display: "flex", justifyContent: "center", borderRadius: 6 }}>
        <img src={content} alt={item.name} style={{ maxWidth: "100%", maxHeight: "50vh", objectFit: "contain" }} /></div>
      <div className="sub">{item.width}×{item.height} · {item.format} · {size(item.size)} · added {item.created_at.slice(0, 10)}
        {archived && " · archived"}</div>
      <label className="field"><span>Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="field"><span>Note</span>
        <textarea className="input" rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></label>
      <label className="field"><span>Tags · comma-separated</span>
        <input className="input" value={tags} onChange={(e) => setTags(e.target.value)} /></label>
      <div className="row" style={{ gap: 10 }}>
        <label className="field grow"><span>Source rights</span>
          <input className="input" value={rights} onChange={(e) => setRights(e.target.value)} /></label>
        <label className="field grow"><span>Source URL</span>
          <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} /></label>
      </div>
      <ErrorLine error={act.error} />
      {stale && <div className="banner note">This item changed elsewhere. The list was reloaded; close and reopen it to edit the latest.</div>}
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <button className="btn btn-primary" disabled={!dirty || act.busy || !name.trim()} onClick={save}>Save</button>
        <a className="btn" href={`${content}?download=1&name=${encodeURIComponent(item.name)}`} download>Download</a>
        {!archived && <button className="btn" onClick={() => nav(`/p/${project}/jobs/new?media=${item.id}`)}>New Job from this</button>}
        <button className="btn" disabled={act.busy} style={{ marginLeft: "auto" }}
          onClick={() => mutate(() => (archived ? restoreMedia : archiveMedia)(project, item.id, item.revision))}>
          {archived ? "Restore" : "Archive"}</button>
      </div>
    </Dialog>
  );
}
