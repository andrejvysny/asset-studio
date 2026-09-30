import { type DragEvent, useRef, useState } from "react";

import { TagChips } from "../components/MediaPicker";
import { Empty, ErrorLine, Loading, PageHead } from "../components/ui";
import { artifactUrl } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { type MediaItem, type MediaList, mediaListPath, uploadMedia, type UploadResult } from "../lib/mediaApi";
import { useProject } from "../lib/project";
import { MediaPreview } from "./MediaPreview";

const IMAGE = /^image\/(png|jpeg|webp)$/;
const MAX_FILES = 20;

function summarize(results: UploadResult[]): string {
  const dup = results.filter((r) => r.ok && r.duplicate).length;
  const added = results.filter((r) => r.ok).length - dup;
  return `${added} added · ${dup} duplicate · ${results.length - added - dup} rejected`;
}

function MediaCard({ project, m, onOpen }: { project: string; m: MediaItem; onOpen: () => void }) {
  return (
    <button className="card" aria-label={`open ${m.name}`} onClick={onOpen}
      style={{ opacity: m.archived_at ? 0.55 : 1, textAlign: "left", color: "inherit" }}>
      <div className="media checker">
        <img src={artifactUrl(project, m.thumb_artifact_id)} alt="" loading="lazy" />
        {m.archived_at && <span className="corner" style={{ left: 7 }}>archived</span>}
      </div>
      <div className="meta">
        <span className="ellipsis" style={{ fontWeight: 500 }}>{m.name}</span>
        <span className="sub">{m.width}×{m.height} · {m.format.toUpperCase()}</span>
        {m.tags.length > 0 && (
          <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>
            {m.tags.slice(0, 3).map((t) => <span key={t} className="badge">{t}</span>)}</span>)}
      </div>
    </button>
  );
}

export function Media() {
  const { id } = useProject();
  const [q, setQ] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [results, setResults] = useState<UploadResult[] | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const act = useAction();
  const list = useApi<MediaList>(mediaListPath(id, { q, tag, archived }), { project: id });
  const opened = list.data?.items.find((m) => m.id === openId) ?? null;

  const send = (files: File[]) => {
    const images = files.filter((f) => IMAGE.test(f.type));
    if (images.length === 0) return;
    void act.run(async () => {
      const out: UploadResult[] = [];
      for (let i = 0; i < images.length; i += MAX_FILES) out.push(...(await uploadMedia(id, images.slice(i, i + MAX_FILES))).results);
      setResults(out);
      list.reload();
    });
  };
  const onDrop = (e: DragEvent<HTMLElement>) => { e.preventDefault(); setOver(false); send([...e.dataTransfer.files]); };
  const rejected = (results ?? []).filter((r) => !r.ok);

  return (
    <section className="content" aria-label="media library" style={{ outline: over ? "2px dashed var(--dim)" : undefined, outlineOffset: -6 }}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setOver(false); }} onDrop={onDrop}>
      <PageHead title={<>Media {list.data && <span className="dim" style={{ fontWeight: 400 }}>· {list.data.items.length}</span>}</>}
        sub="Brainstorm images · usable as guidance references">
        <button className="btn btn-primary" disabled={act.busy} onClick={() => file.current?.click()}>
          {act.busy ? "Uploading…" : "+ Upload"}</button>
        <input ref={file} type="file" multiple accept="image/png,image/jpeg,image/webp" hidden aria-label="upload media"
          onChange={(e) => { const fs = [...(e.target.files ?? [])]; e.target.value = ""; send(fs); }} />
      </PageHead>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        <input className="input" aria-label="search media" placeholder="Search name, note, tag…" value={q}
          onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} />
        <TagChips tags={list.data?.tags ?? []} active={tag} onToggle={setTag} />
        <label className="row" style={{ gap: 5, fontSize: 12, marginLeft: "auto" }}>
          <input type="checkbox" aria-label="show archived" checked={archived} onChange={(e) => setArchived(e.target.checked)} />
          Show archived</label>
      </div>
      <ErrorLine error={act.error ?? list.error} />
      {results && (
        <div className="sub" role="status">{summarize(results)}
          {rejected.length > 0 && (
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {rejected.map((r, i) => <li key={i}>{r.filename}: {r.error?.message ?? "rejected"}</li>)}</ul>)}
        </div>)}
      {!list.data ? <Loading what="media" /> : list.data.items.length === 0
        ? <Empty>No media yet. Drop images here or use Upload.</Empty> : (
          <div className="grid-cards">
            {list.data.items.map((m) => <MediaCard key={m.id} project={id} m={m} onOpen={() => setOpenId(m.id)} />)}
          </div>)}
      {/* keyed on revision: after a save the form restarts from the stored (normalized) record */}
      {opened && <MediaPreview key={`${opened.id}:${opened.revision}`} project={id} item={opened} onClose={() => setOpenId(null)} onChanged={list.reload} />}
    </section>
  );
}
