import { useCallback, useEffect, useState } from "react";

import { get, J, type JobDetail, type JobSummary, type PublishPreviewRow } from "../../lib/api";
import { useChanges } from "../../lib/hooks";

/** Jobs whose items may need a decision (drafts and finished Jobs have nothing to review). */
export const reviewable = (j: JobSummary): boolean =>
  j.direct ? j.progress.state !== "done" : j.progress.state !== "draft" && j.progress.state !== "done";

/** Full Job details (items, rounds, legal actions) for the given Jobs; re-read on server change hints. */
export function useJobDetails(project: string, ids: string[]): { details: JobDetail[]; error: string | null; reload: () => void } {
  const [details, setDetails] = useState<JobDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useChanges((e) => e.project_id === project, reload);
  const sig = ids.join(",");
  useEffect(() => {
    let alive = true;
    const want = sig ? sig.split(",") : [];
    Promise.all(want.map((id) => get<JobDetail>(`${J(project)}/${id}`)))
      .then((d) => { if (alive) { setDetails(d); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [project, sig, tick]);
  return { details, error, reload };
}

/** Publish targets of the given Jobs (unpublished rows only). */
export function usePublishRows(project: string, ids: string[], bump: string): { rows: PublishPreviewRow[]; error: string | null } {
  const [rows, setRows] = useState<PublishPreviewRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const sig = ids.join(",");
  useEffect(() => {
    let alive = true;
    const want = sig ? sig.split(",") : [];
    Promise.all(want.map((id) => get<{ items: PublishPreviewRow[] }>(`${J(project)}/${id}/publish-preview`)))
      .then((r) => { if (alive) { setRows(r.flatMap((x) => x.items).filter((x) => !x.published)); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [project, sig, bump]);
  return { rows, error };
}
