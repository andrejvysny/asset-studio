import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";

export interface Loaded<T> { data: T | null; error: string | null; reload: () => void }

/** GET a JSON endpoint; re-fetch every `pollMs` (0 = once). Keeps last good data on transient errors. */
export function useApi<T>(path: string | null, pollMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const pathRef = useRef(path);

  useEffect(() => {
    if (pathRef.current !== path) {
      pathRef.current = path;
      setData(null);
    }
    if (!path) return;
    let alive = true;
    api<T>(path)
      .then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message); });
    const timer = pollMs ? setTimeout(reload, pollMs) : undefined;
    return () => { alive = false; clearTimeout(timer); };
  }, [path, pollMs, tick, reload]);

  return { data, error, reload };
}

/** Run an async action with busy/error state (for buttons). */
export function useAction(): { busy: boolean; error: string | null; run: (fn: () => Promise<void>) => Promise<void> } {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }, []);
  return { busy, error, run };
}
