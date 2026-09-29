import { useCallback, useEffect, useRef, useState } from "react";

import { get } from "./api";

export interface Loaded<T> { data: T | null; error: string | null; reload: () => void }

type Listener = (e: { type: string; project_id?: string; batch_id?: string }) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;

function ensureEvents(): void {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource("/api/v1/events");
  source.addEventListener("change", (ev) => {
    const data = JSON.parse((ev as MessageEvent<string>).data) as Parameters<Listener>[0];
    listeners.forEach((l) => l(data));
  });
  source.addEventListener("reset", () => listeners.forEach((l) => l({ type: "reset" })));
}

/** Server-sent change hints (EventSource reconnects with Last-Event-ID). State is always re-read via the API. */
export function useChanges(match: (e: Parameters<Listener>[0]) => boolean, onChange: () => void): void {
  const matchRef = useRef(match);
  const cbRef = useRef(onChange);
  matchRef.current = match;
  cbRef.current = onChange;
  useEffect(() => {
    ensureEvents();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const l: Listener = (e) => {
      if (e.type !== "reset" && !matchRef.current(e)) return;
      clearTimeout(timer);
      timer = setTimeout(() => cbRef.current(), 150);
    };
    listeners.add(l);
    return () => { listeners.delete(l); clearTimeout(timer); };
  }, []);
}

/** GET JSON; reload on matching change events, plus a slow polling fallback. Keeps last good data on errors. */
export function useApi<T>(path: string | null, opts: { pollMs?: number; project?: string; batch?: string } = {}):
  Loaded<T> {
  const { pollMs = 0, project, batch } = opts;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const pathRef = useRef(path);

  useChanges((e) => project !== undefined && e.project_id === project && (!batch || !e.batch_id || e.batch_id === batch),
    reload);

  useEffect(() => {
    if (pathRef.current !== path) {
      pathRef.current = path;
      setData(null);
      setError(null);
    }
    if (!path) return;
    let alive = true;
    get<T>(path)
      .then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message); });
    const timer = pollMs ? setTimeout(reload, pollMs) : undefined;
    return () => { alive = false; clearTimeout(timer); };
  }, [path, pollMs, tick, reload]);

  return { data, error, reload };
}

/** Run an async action with busy/error state (for buttons). */
export function useAction(): { busy: boolean; error: string | null; run: (fn: () => Promise<void>) => Promise<void>;
  clear: () => void } {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }, []);
  return { busy, error, run, clear: () => setError(null) };
}
