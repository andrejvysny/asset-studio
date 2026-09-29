// State machine of the Create-variants wizard. The form is the source of edits; every change is PATCHed to the
// draft (debounced, strictly serialised, always with the latest known revision). A 409 reloads the draft.

import { useCallback, useEffect, useRef, useState } from "react";

import {
  type Capabilities, type CreateJobsResult, key, type Kind, type ReferenceSet, type VariantDraft,
  type VariantDraftDetail, type VariantIntent, type VariantMethod,
} from "../../lib/api";
import {
  applySuggestion, type CreateDraftBody, createDraft, createVariantJobs, getDraft, getVariantCapabilities,
  type PatchDraftBody, patchDraft, prepareReferences, suggestPlan,
} from "../../lib/variantsApi";
import {
  blankRow, DEFAULT_PRESERVE, describeError, isDirect, type LocalRow, MAX_ROWS, preserveFromApi, preserveToApi,
  type Problem, rowFromServer, rowProblem, rowToApi,
} from "./model";

export interface Form {
  method: VariantMethod; intent: VariantIntent; preserve: string; request: string; familyName: string; cands: number;
  rows: LocalRow[]; styleAck: boolean; primary: string | null;
}
type Field = "method" | "intent" | "preserve" | "request" | "family" | "cands" | "rows" | "primary" | "ack";
export type Boot = { phase: "loading" } | { phase: "ready" } | { phase: "unavailable" } | { phase: "error"; problem: Problem };
export type RefsState = "idle" | "preparing" | "ready" | "error";

const RUNNING = new Set(["queued", "running", "cancel_requested", "reconciling"]);
const hash = (s: string): string => {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h.toString(36);
};
export const relevantMethods = (kind: Kind): VariantMethod[] =>
  kind === "model3d" ? ["image_edit_reconstruct", "direct_transform"] : ["image_edit", "direct_transform"];

function chooseMethod(caps: Capabilities, prev: VariantMethod | undefined): VariantMethod | null {
  const ok = (m: VariantMethod) => caps.methods.find((x) => x.method === m)?.available === true;
  if (prev && ok(prev) && relevantMethods(caps.source.kind).includes(prev)) return prev;
  return relevantMethods(caps.source.kind).find(ok) ?? null;
}

export function useVariantDraft(project: string, assetId: string, versionId: string, count: number) {
  const [mount] = useState(() => key());
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [detail, setDetail] = useState<VariantDraftDetail | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [refs, setRefs] = useState<ReferenceSet | null>(null);
  const [refsState, setRefsState] = useState<RefsState>("idle");
  const [refsProblem, setRefsProblem] = useState<Problem | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [appliedTask, setAppliedTask] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const formRef = useRef<Form | null>(null);
  const dirty = useRef(new Set<Field>());
  const rev = useRef(0);
  const draftId = useRef<string | null>(null);
  const kind = useRef<Kind>("model3d");
  const familyFixed = useRef(false);
  const refsFor = useRef<string | null>(null);
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const enqueue = useCallback(<T,>(fn: () => Promise<T>): Promise<T> => {
    const p = chain.current.then(fn);
    chain.current = p.then(() => undefined, () => undefined);
    return p;
  }, []);

  const setFormBoth = useCallback((f: Form) => { formRef.current = f; setForm(f); }, []);

  /** Re-read the draft (tasks, work summary, capabilities) and adopt the server revision. Runs inside the chain. */
  const refresh = useCallback(async () => {
    const id = draftId.current;
    if (!id) return;
    const d = await getDraft(project, id);
    if (draftId.current !== id) return;
    rev.current = d.revision;
    setDetail(d);
  }, [project]);

  const adopt = useCallback((d: VariantDraft, keep?: Form | null) => {
    const k = d.source.kind;
    setFormBoth({
      method: d.method, intent: d.intent ?? keep?.intent ?? "related",
      preserve: d.preserve.length || !keep ? preserveFromApi(d.preserve) : keep.preserve,
      request: d.request, familyName: d.family.new_name ?? keep?.familyName ?? "", cands: d.candidates_per_row,
      rows: d.rows.map((r) => rowFromServer(r, k, keep?.rows.find((x) => x.id === r.id))),
      styleAck: d.style_ack, primary: d.primary_view,
    });
  }, [setFormBoth]);

  const reload = useCallback(async (why: string) => {
    const id = draftId.current;
    if (!id) return;
    const d = await getDraft(project, id);
    dirty.current.clear();
    rev.current = d.revision;
    setDetail(d);
    adopt(d);
    setNotice(why);
  }, [project, adopt]);

  const doFlush = useCallback(async () => {
    const f = formRef.current, id = draftId.current;
    const fields = dirty.current;
    if (!f || !id || fields.size === 0) { if (fields.size === 0) setPending(false); return; }
    dirty.current = new Set();
    const body: PatchDraftBody = { expected_revision: rev.current };
    const withRows = fields.has("rows") || fields.has("method") || fields.has("cands");
    if (fields.has("method")) body.method = f.method;
    if (fields.has("intent") || fields.has("method")) { if (!isDirect(f.method)) body.intent = f.intent; }
    if (fields.has("preserve")) body.preserve = preserveToApi(f.preserve);
    if (fields.has("request")) body.request = f.request;
    if (fields.has("cands")) body.candidates_per_row = f.cands;
    if (fields.has("family") && !familyFixed.current && f.familyName.trim()) body.family_name = f.familyName.trim();
    if (fields.has("primary") && f.primary) body.primary_view = f.primary;
    if (fields.has("ack")) body.style_ack = f.styleAck;
    const sent = withRows ? f.rows : [];
    if (withRows) body.rows = sent.map((r, i) => rowToApi(r, i, f.method, kind.current));
    setSaving(true);
    try {
      const r = await patchDraft(project, id, body);
      rev.current = r.revision;
      if (withRows) {
        const ids = new Map(sent.map((s, i) => [s.key, r.rows[i]?.id]));
        const cur = formRef.current;
        if (cur) setFormBoth({ ...cur, rows: cur.rows.map((x) => (!x.id && ids.get(x.key) ? { ...x, id: ids.get(x.key) } : x)) });
      }
      setProblem(null);
    } catch (e) {
      const p = describeError(e);
      if (p.code === "stale_variant_plan") await reload(p.text);
      else { fields.forEach((x) => dirty.current.add(x)); setProblem(p); }
    } finally { setSaving(false); }
    if (dirty.current.size === 0) { setPending(false); await refresh(); }
  }, [project, refresh, reload, setFormBoth]);

  const flush = useCallback(() => { clearTimeout(timer.current); return enqueue(doFlush).catch(() => undefined); }, [enqueue, doFlush]);

  const update = useCallback((patch: Partial<Form>, ...fields: Field[]) => {
    const cur = formRef.current;
    if (!cur) return;
    setFormBoth({ ...cur, ...patch });
    fields.forEach((x) => dirty.current.add(x));
    setPending(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => { void flush(); }, 400);
  }, [flush, setFormBoth]);

  /** Prepare the source reference images once per draft. Resolves true when they exist. */
  const ensureRefs = useCallback((): Promise<boolean> => enqueue(async () => {
    const id = draftId.current;
    if (!id) return false;
    if (refsFor.current === id) return true;
    setRefsState("preparing");
    setRefsProblem(null);
    try {
      const r = await prepareReferences(project, id);
      refsFor.current = id;
      setRefs(r);
      setRefsState("ready");
      const cur = formRef.current;
      if (cur && cur.primary !== r.primary_view) setFormBoth({ ...cur, primary: r.primary_view });
      await refresh();
      return true;
    } catch (e) {
      setRefsProblem(describeError(e));
      setRefsState("error");
      return false;
    }
  }), [enqueue, project, refresh, setFormBoth]);

  // --- boot: capabilities + draft for the requested version ---------------------------------------------------------
  useEffect(() => {
    let alive = true;
    setBoot({ phase: "loading" });
    setDetail(null); setRefs(null); setRefsState("idle"); setSuggesting(false); setProblem(null); setNotice(null);
    (async () => {
      try {
        const c = await getVariantCapabilities(project, assetId, versionId);
        if (!alive) return;
        setCaps(c);
        const prev = formRef.current;
        const method = chooseMethod(c, prev?.method);
        if (!method) { setBoot({ phase: "unavailable" }); return; }
        const k = c.source.kind;
        const carry = prev?.rows.map((r, i) => rowToApi({ ...r, id: undefined }, i, method, k));
        const body: CreateDraftBody = {
          asset_id: assetId, version_id: versionId, method, intent: prev?.intent ?? "related", requested_variants: count,
          candidates_per_variant: prev?.cands ?? 4,
          ...(carry?.length ? { rows: carry } : {}), ...(c.family || !prev?.familyName.trim() ? {} : { family_name: prev.familyName.trim() }),
        };
        const d = await createDraft(project, body, `${mount}-${versionId}-${hash(JSON.stringify(body))}`);
        if (!alive) return;
        draftId.current = d.id; rev.current = d.revision; kind.current = k; familyFixed.current = c.family !== null;
        refsFor.current = null;
        dirty.current = new Set<Field>(["preserve"]);
        const base: Form = {
          method: d.method, intent: d.intent ?? prev?.intent ?? "related", preserve: prev?.preserve ?? DEFAULT_PRESERVE,
          request: prev?.request ?? d.request, familyName: d.family.new_name ?? "", cands: d.candidates_per_row,
          rows: d.rows.map((r, i) => {
            const p = prev?.rows.length === d.rows.length ? prev.rows[i] : undefined;
            const l = rowFromServer(r, k);
            return p ? { ...l, t: p.t, height: p.height } : l;
          }),
          styleAck: false, primary: d.primary_view,
        };
        if (base.request) dirty.current.add("request");
        setFormBoth(base);
        setBoot({ phase: "ready" });
        setPending(true);
        timer.current = setTimeout(() => { void flush(); }, 400);
        if (!isDirect(d.method)) void ensureRefs();
        else void enqueue(refresh);
      } catch (e) {
        if (alive) setBoot({ phase: "error", problem: describeError(e) });
      }
    })();
    return () => { alive = false; clearTimeout(timer.current); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, assetId, versionId]);

  // --- suggestion polling -------------------------------------------------------------------------------------------
  useEffect(() => {
    if (!suggesting) return;
    let alive = true;
    let t: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const id = draftId.current;
      if (!id) return;
      try {
        const d = await getDraft(project, id);
        if (!alive) return;
        setDetail(d);
        rev.current = Math.max(rev.current, d.revision);
        if (RUNNING.has(d.tasks.suggest.state)) t = setTimeout(() => { void tick(); }, 1000);
        else setSuggesting(false);
      } catch (e) {
        if (alive) { setProblem(describeError(e)); setSuggesting(false); }
      }
    };
    void tick();
    return () => { alive = false; clearTimeout(t); };
  }, [suggesting, project]);

  // --- actions ------------------------------------------------------------------------------------------------------
  const settled = useCallback(async (): Promise<boolean> => { await flush(); return dirty.current.size === 0; }, [flush]);

  const setMethod = useCallback((m: VariantMethod) => {
    const cur = formRef.current;
    if (!cur || cur.method === m) return;
    update({ method: m }, "method", "intent", "rows");
    if (!isDirect(m)) void ensureRefs();
  }, [update, ensureRefs]);

  const suggest = useCallback(async () => {
    setProblem(null);
    if (!(await settled())) return;
    const id = draftId.current, f = formRef.current;
    if (!id || !f) return;
    if (!(await ensureRefs())) return;
    try {
      await suggestPlan(project, id, { count: Math.min(Math.max(count, 1), MAX_ROWS), ...(f.request.trim() ? { request: f.request } : {}) });
      setSuggesting(true);
    } catch (e) { setProblem(describeError(e)); }
  }, [settled, ensureRefs, project, count]);

  const useSuggestion = useCallback(async () => {
    setProblem(null);
    if (!(await settled())) return;
    const id = draftId.current, f = formRef.current;
    const taskId = detail?.suggestion?.task_id ?? null;
    if (!id || !f) return;
    try {
      const r = await enqueue(() => applySuggestion(project, id, { expected_revision: rev.current,
        mode: f.rows.length === 0 ? "replace_empty" : "append" }));
      rev.current = r.revision;
      const cur = formRef.current;
      if (cur) setFormBoth({ ...cur, rows: r.rows.map((sr) => rowFromServer(sr, kind.current, cur.rows.find((x) => x.id === sr.id))) });
      setAppliedTask(taskId);
      await enqueue(refresh);
    } catch (e) {
      const p = describeError(e);
      if (p.code === "stale_variant_plan") await enqueue(() => reload(p.text)); else setProblem(p);
    }
  }, [settled, detail, enqueue, project, setFormBoth, refresh, reload]);

  const save = useCallback(async (): Promise<CreateJobsResult | null> => {
    setProblem(null);
    setSaving(true);
    try {
      if (!(await settled())) return null;
      const id = draftId.current, f = formRef.current;
      if (!id || !f) return null;
      if (!isDirect(f.method) && !(await ensureRefs())) return null;
      return await createVariantJobs(project, id, rev.current, `${mount}-save-${id}-${rev.current}`);
    } catch (e) {
      const p = describeError(e);
      setProblem(p);
      if (p.code === "references_missing") { refsFor.current = null; void ensureRefs(); }
      if (p.code === "stale_variant_plan") await enqueue(() => reload(p.text));
      return null;
    } finally { setSaving(false); }
  }, [settled, ensureRefs, project, mount, enqueue, reload]);

  const selectView = useCallback((view: string) => {
    setRefs((r) => (r ? { ...r, primary_view: view, images: r.images.map((i) => ({ ...i, role: i.view === view ? "primary" : "auxiliary" })) } : r));
    update({ primary: view }, "primary");
  }, [update]);

  const addRow = useCallback(() => {
    const cur = formRef.current;
    if (cur && cur.rows.length < MAX_ROWS) update({ rows: [...cur.rows, blankRow(kind.current, cur.rows.length + 1)] }, "rows");
  }, [update]);
  const editRow = useCallback((k: string, patch: Partial<LocalRow>) => {
    const cur = formRef.current;
    if (cur) update({ rows: cur.rows.map((r) => (r.key === k ? { ...r, ...patch } : r)) }, "rows");
  }, [update]);
  const removeRow = useCallback((k: string) => {
    const cur = formRef.current;
    if (cur) update({ rows: cur.rows.filter((r) => r.key !== k) }, "rows");
  }, [update]);

  const problems = form ? form.rows.map((r) => rowProblem(r, form.method, kind.current)) : [];
  return { boot, caps, detail, form, refs, refsState, refsProblem, problem, notice, pending, saving, suggesting, appliedTask,
    problems, kind: kind.current, update, setMethod, suggest, useSuggestion, save, selectView, addRow, editRow, removeRow,
    dismissNotice: () => setNotice(null), retryRefs: () => { refsFor.current = null; void ensureRefs(); } };
}
