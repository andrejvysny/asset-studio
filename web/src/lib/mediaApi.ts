// Typed endpoints for the per-project Media Library: /api/v1/projects/{project}/media...
// Edits need `expected_revision` (MediaItem.revision); a stale value gives 409.

import { get, P, send, uploadMany } from "./api";

export interface MediaItem {
  id: string; artifact_id: string; sha256: string; thumb_artifact_id: string; name: string; note: string; tags: string[];
  source_rights: string; source_url: string; format: string; width: number; height: number; size: number;
  has_alpha: boolean; created_at: string; updated_at: string; archived_at: string | null; revision: number;
}
export interface MediaList { items: MediaItem[]; tags: { tag: string; count: number }[] }
export interface UploadResult {
  filename: string; ok: boolean; item?: MediaItem; duplicate?: boolean; error?: { code: string; message: string };
}
export interface MediaPatch {
  expected_revision: number; name?: string; note?: string; tags?: string[]; source_rights?: string; source_url?: string;
}

/** Path for useApi. */
export function mediaListPath(project: string, f: { q?: string; tag?: string | null; archived?: boolean } = {}): string {
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  if (f.tag) p.set("tag", f.tag);
  p.set("archived", f.archived ? "1" : "0");
  return `${P(project)}/media?${p}`;
}

export const uploadMedia = (project: string, files: File[]) =>
  uploadMany<{ results: UploadResult[] }>(`${P(project)}/media:upload`, files);
export const getMedia = (project: string, id: string) => get<MediaItem>(`${P(project)}/media/${id}`);
export const patchMedia = (project: string, id: string, body: MediaPatch) =>
  send<MediaItem>("PATCH", `${P(project)}/media/${id}`, body);
export const archiveMedia = (project: string, id: string, expectedRevision: number) =>
  send<MediaItem>("POST", `${P(project)}/media/${id}:archive`, { expected_revision: expectedRevision });
export const restoreMedia = (project: string, id: string, expectedRevision: number) =>
  send<MediaItem>("POST", `${P(project)}/media/${id}:restore`, { expected_revision: expectedRevision });
