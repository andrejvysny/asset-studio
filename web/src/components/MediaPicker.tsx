import { useState } from "react";

import { artifactUrl } from "../lib/api";
import { useApi } from "../lib/hooks";
import { type MediaItem, type MediaList, mediaListPath } from "../lib/mediaApi";
import { Dialog, Empty, ErrorLine, Loading } from "./ui";

/** Tag filter chips shared by the Media page and the picker. */
export function TagChips({ tags, active, onToggle }:
  { tags: { tag: string; count: number }[]; active: string | null; onToggle: (tag: string | null) => void }) {
  return (
    <>
      {tags.map((t) => (
        <button key={t.tag} className={`chip${active === t.tag ? " on" : ""}`} aria-pressed={active === t.tag}
          onClick={() => onToggle(active === t.tag ? null : t.tag)}>{t.tag} · {t.count}</button>))}
    </>
  );
}

/** Pick one non-archived Media item as a reference. */
export function MediaPicker({ project, onPick, onClose }:
  { project: string; onPick: (item: MediaItem) => void; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const list = useApi<MediaList>(mediaListPath(project, { q, tag, archived: false }), { project });
  return (
    <Dialog title="Reference from media" onClose={onClose}>
      <input className="input" aria-label="search media" placeholder="Search name, note, tag" value={q}
        onChange={(e) => setQ(e.target.value)} />
      {(list.data?.tags.length ?? 0) > 0 && (
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <TagChips tags={list.data?.tags ?? []} active={tag} onToggle={setTag} /></div>)}
      <ErrorLine error={list.error} />
      {!list.data ? <Loading what="media" /> : list.data.items.length === 0
        ? <Empty>No media. Upload images on the Media page.</Empty> : (
          <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))", maxHeight: 380,
            overflow: "auto" }}>
            {list.data.items.map((m) => (
              <button key={m.id} className="card" aria-label={`use ${m.name}`} onClick={() => onPick(m)}>
                <div className="media checker"><img src={artifactUrl(project, m.thumb_artifact_id)} alt="" loading="lazy" /></div>
                <div className="meta"><span className="ellipsis" style={{ fontWeight: 500 }}>{m.name}</span>
                  <span className="sub">{m.width}×{m.height} · {m.format.toUpperCase()}</span></div>
              </button>))}
          </div>)}
    </Dialog>
  );
}
