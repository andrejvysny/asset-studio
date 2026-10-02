import { useState } from "react";

import { Dialog, ErrorLine, Loading } from "../components/ui";
import { type CategoryNode, P } from "../lib/api";
import { useAction, useApi } from "../lib/hooks";
import { setAssetsCategory } from "../lib/variantsApi";

interface Props {
  project: string;
  assetIds: string[];
  /** Category the (single) asset is in now; null = Uncategorized; undefined when several assets are selected. */
  current?: string | null;
  onClose: () => void;
  onDone: () => void;
}

/** Searchable category picker for one or many assets. The list is fetched on open, so a category created a moment
 *  ago is already there. Quick and reversible: no destructive wording, a preview line states what will happen. */
export function MoveToCategory({ project, assetIds, current, onClose, onDone }: Props) {
  const cats = useApi<{ categories: CategoryNode[] }>(`${P(project)}/categories`, { project });
  const act = useAction();
  const [q, setQ] = useState("");
  const [target, setTarget] = useState<string | null | undefined>(undefined);  // undefined = nothing chosen yet
  const list = (cats.data?.categories ?? []).filter((c) => `${c.label} ${c.path}`.toLowerCase().includes(q.toLowerCase()));
  const label = target === undefined ? "" : target === null ? "Uncategorized"
    : cats.data?.categories.find((c) => c.id === target)?.label ?? target;
  const n = assetIds.length;
  const apply = () => void act.run(async () => {
    if (target === undefined) return;
    const out = await setAssetsCategory(project, assetIds, target);
    const [first, ...rest] = out.results.filter((r) => !r.ok);
    if (first) throw new Error(`${rest.length + 1} of ${n} could not be moved: ${first.message ?? first.code}`);
    onDone();
  });
  return (
    <Dialog title={n === 1 ? "Change category" : `Move ${n} assets`} onClose={onClose}>
      <input className="input" aria-label="search categories" placeholder="Search categories…" value={q}
        onChange={(e) => setQ(e.target.value)} autoFocus />
      {cats.error ? <ErrorLine error={cats.error} /> : !cats.data ? <Loading what="categories" /> : (
        <div className="panel" role="listbox" aria-label="categories" style={{ maxHeight: 280, overflow: "auto" }}>
          {"uncategorized".includes(q.toLowerCase()) && (
            <button role="option" aria-selected={target === null} className={`side-row${target === null ? " on" : ""}`}
              style={{ width: "100%" }} onClick={() => setTarget(null)}>
              <span>Uncategorized</span>{current === null && <span className="n">current</span>}</button>
          )}
          {list.map((c) => (
            <button key={c.id} role="option" aria-selected={target === c.id}
              className={`side-row${target === c.id ? " on" : ""}`}
              style={{ width: "100%", paddingLeft: 8 + c.depth * 16 }} onClick={() => setTarget(c.id)}>
              <span>{c.label}</span>{current === c.id && <span className="n">current</span>}</button>
          ))}
          {list.length === 0 && !"uncategorized".includes(q.toLowerCase()) && <div className="sub" style={{ padding: 8 }}>No match.</div>}
        </div>
      )}
      <div className="sub">{target === undefined ? "Pick a category."
        : `${n} asset${n === 1 ? "" : "s"} will move to ${label}. You can move them again at any time.`}</div>
      <ErrorLine error={act.error} />
      <div className="row" style={{ gap: 8 }}>
        <button className="btn btn-primary" disabled={act.busy || target === undefined || (n === 1 && target === current)}
          onClick={apply}>{n === 1 ? "Apply" : `Move ${n} assets`}</button>
        <button className="btn" onClick={onClose}>Cancel</button>
      </div>
    </Dialog>
  );
}
