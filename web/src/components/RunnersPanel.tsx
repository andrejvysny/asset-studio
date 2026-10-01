import { useState } from "react";

import { useAction, useApi } from "../lib/hooks";
import {
  createGroup, createRegistrationToken, declareLost, type DeviceSummary, type GroupList, groupsPath, revokeRunner,
  runnerPath, type RunnerDetail, type RunnerList, type RunnerSummary, runnersPath, setPushUrl, type TokenOut,
} from "../lib/runnersApi";
import { BAD, ErrorLine, INFO, NONE, OK, relTime, WARN } from "./ui";

const NEW_GROUP = "__new__";
const TTL_S = 900;
const CLAIM_TIP = "ownership unknown until the runner re-advertises after recovery";
const claimColor = (c: string) => (c === "free" ? OK : c === "reserved" ? INFO : c === "uncertain" ? BAD : NONE);

function runnerStatus(r: RunnerSummary): { label: string; color: string } {
  if (r.state === "revoked") return { label: "revoked", color: BAD };
  if (!r.session) return { label: "no session", color: WARN };
  return r.session.fresh ? { label: "fresh", color: OK } : { label: "stale", color: WARN };
}

function TokenBox({ token }: { token: TokenOut }) {
  const [copied, setCopied] = useState(false);
  const copy = () => void navigator.clipboard?.writeText(token.token).then(() => setCopied(true)).catch(() => undefined);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div className="row" style={{ gap: 8 }}>
        <code className="mono input grow" data-testid="registration-token" style={{ fontSize: 12, wordBreak: "break-all" }}>
          {token.token}</code>
        <button className="btn" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <span className="sub">Run on the runner host: <span className="mono">assetstudio-node run --config runner.yaml</span></span>
      <span className="sub" style={{ color: WARN }}>valid until {relTime(token.expires_at)}; shown once</span>
    </div>
  );
}

function AddRunner({ groups, onDone }: { groups: GroupList["groups"]; onDone: () => void }) {
  const act = useAction();
  const [group, setGroup] = useState(groups[0]?.id ?? NEW_GROUP);
  const [name, setName] = useState("");
  const [projects, setProjects] = useState("*");
  const [ephemeral, setEphemeral] = useState(false);
  const [token, setToken] = useState<TokenOut | null>(null);
  const creating = group === NEW_GROUP;
  const submit = () => act.run(async () => {
    let id = group;
    if (creating) {
      const list = projects.trim() === "*" ? "*" : projects.split(",").map((p) => p.trim()).filter(Boolean);
      id = (await createGroup({ name: name.trim(), projects: list, operations: "*", labels: [], ephemeral })).id;
      setGroup(id);
    }
    setToken(await createRegistrationToken(id, TTL_S));
    onDone();
  });
  return (
    <div className="panel" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label className="field"><span>Runner group</span>
          <select className="input" value={group} onChange={(e) => { setGroup(e.target.value); setToken(null); }}>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            <option value={NEW_GROUP}>New group…</option>
          </select></label>
        {creating && <>
          <label className="field"><span>Group name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="field"><span>Projects ("*" or comma-separated ids)</span>
            <input className="input" value={projects} onChange={(e) => setProjects(e.target.value)} /></label>
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={ephemeral} onChange={(e) => setEphemeral(e.target.checked)} /> ephemeral</label>
        </>}
        <button className="btn btn-primary" disabled={act.busy || (creating && !name.trim()) || !projects.trim()}
          onClick={() => void submit()}>Create registration token</button>
      </div>
      <ErrorLine error={act.error} />
      {token && <TokenBox token={token} />}
    </div>
  );
}

function Devices({ devices }: { devices: DeviceSummary[] }) {
  if (!devices.length) return <span className="sub">No devices advertised.</span>;
  return (
    <div className="table">
      {devices.map((d) => (
        <div key={d.uuid} className="td" style={{ gridTemplateColumns: "minmax(200px,1.5fr) 50px minmax(120px,1fr) 100px", minWidth: 520 }}>
          <span className="mono ellipsis" style={{ fontSize: 11 }} title={d.uuid}>{d.uuid}</span>
          <span className="sub">#{d.index}</span>
          <span className="muted" style={{ fontSize: 12 }}>{d.name}{d.fallback ? " · fallback" : ""}</span>
          <span className="sub" style={{ color: claimColor(d.claim) }} title={d.claim === "uncertain" ? CLAIM_TIP : undefined}>
            {d.claim}{d.claim_attempt ? ` · ${d.claim_attempt}` : ""}</span>
        </div>))}
    </div>
  );
}

function Slots({ slots }: { slots: RunnerSummary["slots"] }) {
  if (!slots.length) return <span className="sub">No slots.</span>;
  return (
    <div className="table">
      {slots.map((s) => (
        <div key={s.slot_id} className="td" style={{ gridTemplateColumns: "120px 100px minmax(180px,1.5fr) 90px minmax(120px,1fr)", minWidth: 620 }}>
          <span className="mono" style={{ fontSize: 11.5 }}>{s.slot_id}</span>
          <span className="sub">{s.capability}</span>
          <span className="muted" style={{ fontSize: 12 }}>
            {s.engines.map((e) => `${e.engine}: ${e.operations.join(", ")}`).join(" · ") || "—"}</span>
          <span className="sub">{s.state}</span>
          <span className="mono dim ellipsis" style={{ fontSize: 11 }} title={s.loaded_residency ?? undefined}>
            {s.loaded_residency ?? "nothing loaded"}</span>
        </div>))}
    </div>
  );
}

function NeedsAttention({ runnerId, onChange }: { runnerId: string; onChange: () => void }) {
  const detail = useApi<RunnerDetail>(runnerPath(runnerId), { pollMs: 5000 });
  const act = useAction();
  const uncertain = detail.data?.attempts.filter((a) => a.state === "uncertain") ?? [];
  const lost = (id: string) => {
    const msg = "Only after confirming the runner stopped this work; the device stays blocked until the runner re-advertises it.";
    if (!window.confirm(`Declare attempt ${id} lost?\n\n${msg}`)) return;
    void act.run(async () => { await declareLost(id); detail.reload(); onChange(); });
  };
  if (!uncertain.length && !act.error) return null;
  return (
    <div className="banner warn" style={{ flexDirection: "column" }}>
      <span style={{ fontWeight: 600 }}>Needs attention</span>
      {uncertain.map((a) => (
        <div key={a.id} className="row" style={{ gap: 10 }}>
          <span className="mono" style={{ fontSize: 11.5 }}>{a.id}</span>
          <span className="sub grow">{a.operation} · task {a.task_id} · uncertain since {relTime(a.updated_at)}</span>
          <button className="btn" disabled={act.busy} onClick={() => lost(a.id)}>Declare lost…</button>
        </div>))}
      <ErrorLine error={act.error} />
    </div>
  );
}

function RunnerCard({ r, onChange }: { r: RunnerSummary; onChange: () => void }) {
  const act = useAction();
  const [editing, setEditing] = useState(false);
  const [url, setUrl] = useState(r.push_url ?? "");
  const st = runnerStatus(r);
  const counts = Object.entries(r.attempt_counts);
  const revoke = () => {
    if (!window.confirm(`Revoke runner ${r.name}? Its tokens stop working immediately; work it holds becomes uncertain.`)) return;
    void act.run(async () => { await revokeRunner(r.id); onChange(); });
  };
  const savePush = () => void act.run(async () => { await setPushUrl(r.id, url.trim() || null); setEditing(false); onChange(); });
  return (
    <div className="panel" data-testid={`runner-${r.name}`} style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
        <span><span style={{ fontWeight: 600 }}>{r.name}</span> <span className="mono dim" style={{ fontSize: 11.5 }}>{r.id}</span></span>
        <span className="sub" style={{ color: st.color }}>{st.label}</span>
      </div>
      <span className="sub">state {r.state} · group <span className="mono">{r.group_id}</span> · {String(r.platform.os ?? "?")}/{String(r.platform.arch ?? "?")}
        {r.platform.hostname ? ` · ${String(r.platform.hostname)}` : ""} · last seen {relTime(r.last_seen_at)}</span>
      <span className="sub">{r.session ? `session: ${r.session.dispatch} · ${r.session.lifecycle} · seen ${relTime(r.session.last_seen_at)}` : "session: none"}
        {r.push_url ? ` · push ${r.push_url}` : ""}</span>
      <Devices devices={r.devices} />
      <Slots slots={r.slots} />
      <span className="sub">attempts: {counts.length ? counts.map(([k, v]) => `${k} ${v}`).join(" · ") : "none"}</span>
      <NeedsAttention runnerId={r.id} onChange={onChange} />
      {editing && (
        <div className="row" style={{ gap: 8 }}>
          <input className="input grow" aria-label={`push URL for ${r.name}`} placeholder="https://runner.example:9000 (empty clears)"
            value={url} onChange={(e) => setUrl(e.target.value)} />
          <button className="btn btn-primary" disabled={act.busy} onClick={savePush}>Save</button>
          <button className="btn" onClick={() => setEditing(false)}>Cancel</button>
        </div>)}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn" disabled={act.busy || r.state === "revoked"} onClick={() => setEditing(true)}>Set push URL</button>
        <button className="btn" disabled={act.busy || r.state === "revoked"} style={{ color: BAD }} onClick={revoke}>Revoke</button>
      </div>
      <ErrorLine error={act.error} />
    </div>
  );
}

export function RunnersPanel() {
  const runners = useApi<RunnerList>(runnersPath, { pollMs: 5000 });
  const groups = useApi<GroupList>(groupsPath);
  const [adding, setAdding] = useState(false);
  const list = runners.data?.runners ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }} aria-label="Compute runners">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="label">Compute runners</div>
        <button className="btn" onClick={() => setAdding((a) => !a)}>Add runner…</button>
      </div>
      {adding && groups.data && <AddRunner groups={groups.data.groups} onDone={() => { groups.reload(); }} />}
      <ErrorLine error={runners.error} />
      {runners.data && !list.length && (
        <div className="empty">No runners registered. Studio runs work locally (direct mode) until runners are added.</div>)}
      {list.map((r) => <RunnerCard key={r.id} r={r} onChange={runners.reload} />)}
    </div>
  );
}
