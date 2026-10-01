"""StageTasks and ModelPasses in the durable journal: the ONLY authority for dispatch state.

A StageTask is one (item, stage, exact inputs) unit. Its logical key makes creation idempotent; conditional state
transitions make cancel/retry/restart races safe; `control` (cancel/pause intent) is separate from execution state
and is never cleared by a retry. Items (project files) hold product outcomes only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import derived_id, new_id

from . import runcontrol
from .runcontrol import NOT_HELD_SQL, RunControlMixin, RunNotOpen

ADMISSION_PAUSED_KEY = "admission_paused"  # "1" while `execution switch` drains or holds the journal (R15)
EXECUTION_GENERATION_KEY = "execution_generation"  # bumped by every serving-process activation


def fence_clause(generation: int | None, *, paused: bool = True) -> tuple[str, list[Any]]:
    """SQL predicate (journal_meta lives in the same database) that makes an UPDATE fail once the pause flag is set
    or a newer serving process has activated. generation=None skips the generation check (never activated)."""
    parts: list[str] = []
    args: list[Any] = []
    if paused:
        parts.append("COALESCE((SELECT value FROM journal_meta WHERE key=?),'0') != '1'")
        args.append(ADMISSION_PAUSED_KEY)
    if generation is not None:
        parts.append("CAST(COALESCE((SELECT value FROM journal_meta WHERE key=?),'0') AS INTEGER) = ?")
        args += [EXECUTION_GENERATION_KEY, generation]
    return " AND ".join(parts) or "1", args

TASK_ACTIVE = ("queued", "running", "reconciling", "blocked")
# Attempt states where a runner may still be working (or its outcome is unknown); `ingested` is delivered.
INFLIGHT_ATTEMPTS = ("offered", "leased", "admitted", "executing", "spooled", "uploading", "uncertain")
TASK_TERMINAL = ("succeeded", "failed", "cancelled")
__all__ = ["Busy", "NewTask", "RunNotOpen", "StageTask", "TaskStore", "TASK_ACTIVE", "TASK_TERMINAL"]

DDL = """
CREATE TABLE IF NOT EXISTS stage_tasks (
  id TEXT PRIMARY KEY, seq INTEGER NOT NULL, project_id TEXT NOT NULL, job_id TEXT NOT NULL, item_id TEXT NOT NULL,
  run_id TEXT, wave_id TEXT, command_id TEXT NOT NULL, stage TEXT NOT NULL, family TEXT NOT NULL,
  stage_version INTEGER NOT NULL DEFAULT 1, logical_key TEXT NOT NULL UNIQUE, inputs TEXT NOT NULL,
  lane TEXT NOT NULL, residency TEXT NOT NULL, microbatch TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL,
  deps TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL, control TEXT NOT NULL DEFAULT 'run',
  progress TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  downstream_ok INTEGER NOT NULL DEFAULT 0, pass_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS stk_ready ON stage_tasks(lane, state, control);
CREATE INDEX IF NOT EXISTS stk_item ON stage_tasks(project_id, item_id, family);
CREATE INDEX IF NOT EXISTS stk_run ON stage_tasks(run_id);
CREATE INDEX IF NOT EXISTS stk_job ON stage_tasks(project_id, job_id);
CREATE TABLE IF NOT EXISTS passes (
  id TEXT PRIMARY KEY, lane TEXT NOT NULL, residency TEXT NOT NULL, worker TEXT, session TEXT, epoch INTEGER,
  task_ids TEXT NOT NULL DEFAULT '[]', jobs TEXT NOT NULL DEFAULT '[]', started_at TEXT NOT NULL, ended_at TEXT,
  close_reason TEXT, loads_before TEXT, loads_after TEXT, measured TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS command_intents (
  project_id TEXT NOT NULL, action TEXT NOT NULL, key TEXT NOT NULL, command_id TEXT NOT NULL,
  intent TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, action, key));
""" + runcontrol.DDL


class Busy(Exception):
    """Another active task already owns this item stage (one execution owner per item stage)."""

    code = "busy"

    def __init__(self, item_id: str, family: str, owner: str) -> None:
        super().__init__(f"{family} already active for {item_id} ({owner})")
        self.item_id, self.family, self.owner = item_id, family, owner


@dataclass
class NewTask:
    project_id: str
    job_id: str
    item_id: str
    stage: str
    family: str  # what the item-level UI calls it: enhance | generate | qa | build | publish
    input_key: str  # exact inputs this task consumes (prompt revision, candidate set, build run, ...)
    inputs: dict[str, Any]
    lane: str
    residency: str
    priority: int = 100
    deps: list[str] = field(default_factory=list)
    run_id: str | None = None
    wave_id: str | None = None
    microbatch: str = ""

    @property
    def logical_key(self) -> str:
        return f"{self.project_id}|{self.item_id}|{self.stage}|{self.input_key}"

    @property
    def id(self) -> str:
        return derived_id("stk", self.logical_key)


@dataclass
class StageTask:
    id: str
    seq: int
    project_id: str
    job_id: str
    item_id: str
    run_id: str | None
    wave_id: str | None
    command_id: str
    stage: str
    family: str
    logical_key: str
    inputs: dict[str, Any]
    lane: str
    residency: str
    microbatch: str
    priority: int
    deps: list[str]
    state: str
    control: str
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int
    pass_id: str | None
    created_at: str
    updated_at: str
    downstream_pending: bool = False  # succeeded, but its required follow-up tasks do not exist yet

    def public(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _task(r: sqlite3.Row) -> StageTask:
    return StageTask(
        id=r["id"], seq=r["seq"], project_id=r["project_id"], job_id=r["job_id"], item_id=r["item_id"],
        run_id=r["run_id"], wave_id=r["wave_id"], command_id=r["command_id"], stage=r["stage"], family=r["family"],
        logical_key=r["logical_key"], inputs=json.loads(r["inputs"]), lane=r["lane"], residency=r["residency"],
        microbatch=r["microbatch"], priority=r["priority"], deps=json.loads(r["deps"]), state=r["state"],
        control=r["control"], progress=json.loads(r["progress"]),
        result=json.loads(r["result"]) if r["result"] else None, error=json.loads(r["error"]) if r["error"] else None,
        attempts=r["attempts"], pass_id=r["pass_id"], created_at=r["created_at"], updated_at=r["updated_at"],
        downstream_pending=r["state"] == "succeeded" and not r["downstream_ok"])


class TaskStore(RunControlMixin):
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock, changed: threading.Condition) -> None:
        self._db, self._lock, self.changed = db, lock, changed
        self._db.executescript(DDL)

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """One atomic journal transaction (tasks + intents commit together or not at all)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            self.changed.notify_all()

    # --- creation ----------------------------------------------------------------------------------------------
    def create(self, tasks: list[NewTask], command_id: str, db: sqlite3.Connection | None = None) -> list[str]:
        """Idempotent by logical key. Raises Busy when another active task already owns the item stage family
        with different inputs. Call inside txn() when combined with other journal writes."""
        if db is None:
            with self.txn() as conn:
                return self.create(tasks, command_id, conn)
        now, ids = now_iso(), []
        run_ctl = self._admit_runs(db, tasks)
        chain = {t.id for t in tasks}  # tasks created together (e.g. segment -> sample -> bake) form one owner
        for t in tasks:
            row = db.execute("SELECT id FROM stage_tasks WHERE logical_key=?", (t.logical_key,)).fetchone()
            if row is not None:
                ids.append(row["id"])
                continue
            for owner in db.execute(
                    f"SELECT id FROM stage_tasks WHERE project_id=? AND item_id=? AND family=? "
                    f"AND state IN ({','.join('?' * len(TASK_ACTIVE))})",
                    (t.project_id, t.item_id, t.family, *TASK_ACTIVE)).fetchall():
                if owner["id"] not in chain:
                    raise Busy(t.item_id, t.family, owner["id"])
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM stage_tasks").fetchone()[0]
            db.execute(
                "INSERT INTO stage_tasks (id, seq, project_id, job_id, item_id, run_id, wave_id, command_id, stage, "
                "family, logical_key, inputs, lane, residency, microbatch, priority, deps, state, control, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, seq, t.project_id, t.job_id, t.item_id, t.run_id, t.wave_id, command_id, t.stage, t.family,
                 t.logical_key, json.dumps(t.inputs), t.lane, t.residency, t.microbatch, t.priority,
                 json.dumps(t.deps), "queued", "paused" if run_ctl.get(t.run_id) == "paused" else "run", now, now))
            ids.append(t.id)
        return ids

    # --- reads -------------------------------------------------------------------------------------------------
    def get(self, task_id: str) -> StageTask | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM stage_tasks WHERE id=?", (task_id,)).fetchone()
        return _task(r) if r else None

    def many(self, ids: list[str]) -> list[StageTask]:
        if not ids:
            return []
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM stage_tasks WHERE id IN ({','.join('?' * len(ids))})",
                                    ids).fetchall()
        return [_task(r) for r in rows]

    def list(self, *, project_id: str | None = None, job_id: str | None = None, item_id: str | None = None,
             run_id: str | None = None, lane: str | None = None, states: tuple[str, ...] | None = None,
             command_id: str | None = None, limit: int | None = None) -> list[StageTask]:
        where, args = [], []
        for col, val in (("project_id", project_id), ("job_id", job_id), ("item_id", item_id), ("run_id", run_id),
                         ("lane", lane), ("command_id", command_id)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if states:
            where.append(f"state IN ({','.join('?' * len(states))})")
            args += list(states)
        sql = "SELECT * FROM stage_tasks" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY seq" + ("" if limit is None else " LIMIT ?"),
                                    [*args, *([] if limit is None else [limit])]).fetchall()
        return [_task(r) for r in rows]

    def latest_by_family(self, project_id: str, item_id: str) -> dict[str, StageTask]:
        """Per item family: the active task if any, else the most recent one (what the item view shows)."""
        out: dict[str, StageTask] = {}
        for t in self.list(project_id=project_id, item_id=item_id):
            cur = out.get(t.family)
            if cur is None or cur.state not in TASK_ACTIVE or t.state in TASK_ACTIVE:
                out[t.family] = t
        return out

    def ready(self, lane: str) -> list[StageTask]:
        """Queued, not paused/cancelled, every dependency succeeded. Dependents of failed/cancelled work fail."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM stage_tasks WHERE lane=? AND state='queued' AND control='run' AND "
                f"{NOT_HELD_SQL} ORDER BY priority, seq",
                (lane,)).fetchall()
            tasks = [_task(r) for r in rows]
            out = []
            for t in tasks:
                if not t.deps:
                    out.append(t)
                    continue
                states = {r["id"]: r["state"] for r in self._db.execute(
                    f"SELECT id, state FROM stage_tasks WHERE id IN ({','.join('?' * len(t.deps))})", t.deps)}
                soft = bool(t.inputs.get("soft_deps"))  # advisory inputs: any terminal outcome will do
                if all(states.get(d) == "succeeded" or (soft and states.get(d) in TASK_TERMINAL) for d in t.deps):
                    out.append(t)
                elif not soft and any(states.get(d) in ("failed", "cancelled") for d in t.deps):
                    bad = next(d for d in t.deps if states.get(d) in ("failed", "cancelled"))
                    self._set(t.id, ("queued",), "failed", error={
                        "code": "dependency_failed", "message": f"upstream task {bad} did not succeed",
                        "retryable": False})
        return out

    # --- transitions (all conditional) --------------------------------------------------------------------------
    def _set(self, task_id: str, from_states: tuple[str, ...], state: str, **cols: Any) -> bool:
        sets, args = ["state=?", "updated_at=?", "revision=revision+1"], [state, now_iso()]
        for k, v in cols.items():
            sets.append(f"{k}=?")
            args.append(json.dumps(v) if k in ("result", "error", "progress") and v is not None else v)
        with self._lock:
            marks = ",".join("?" * len(from_states))
            n = self._db.execute(f"UPDATE stage_tasks SET {', '.join(sets)} WHERE id=? AND state IN ({marks})",
                                 [*args, task_id, *from_states]).rowcount
            self.changed.notify_all()
        return n == 1

    def claim(self, task_id: str, pass_id: str, generation: int | None = None) -> bool:
        """The pause/generation fence is part of the UPDATE: a stale process cannot win a claim from a cache."""
        fence, fence_args = fence_clause(generation)
        with self._lock:
            n = self._db.execute(
                "UPDATE stage_tasks SET state='running', pass_id=?, attempts=attempts+1, updated_at=?, "
                "revision=revision+1 WHERE id=? AND state='queued' AND control='run' AND " + NOT_HELD_SQL + " AND "
                "NOT EXISTS (SELECT 1 FROM stage_tasks o WHERE o.project_id=stage_tasks.project_id "
                "AND o.item_id=stage_tasks.item_id AND o.family=stage_tasks.family "
                "AND o.state IN ('running','reconciling') "
                "AND o.command_id != stage_tasks.command_id) AND " + fence,
                (pass_id, now_iso(), task_id, *fence_args)).rowcount
        return n == 1

    def progress(self, task_id: str, progress: dict[str, Any], touch: bool = True) -> None:
        """`touch=False` keeps updated_at: the scheduler's backoff is anchored on it."""
        with self._lock:
            self._db.execute("UPDATE stage_tasks SET progress=?, updated_at=CASE WHEN ? THEN ? ELSE updated_at END "
                             "WHERE id=?", (json.dumps(progress), touch, now_iso(), task_id))
            self.changed.notify_all()

    def finish(self, task_id: str, state: str, *, result: dict[str, Any] | None = None,
               error: dict[str, Any] | None = None) -> str:
        """running -> state. A pending cancellation turns a non-success outcome into `cancelled`; a completed
        task is never relabelled cancelled."""
        with self._lock:
            cur = self.get(task_id)
            if cur is None:
                return "missing"
            if cur.control == "cancel_requested" and state != "succeeded":
                state, error = "cancelled", {"code": "cancelled", "message": "cancelled by operator"}
            self._set(task_id, ("running",), state, result=result, error=error)
            return state

    def complete(self, task_id: str, result: dict[str, Any], downstream: list[NewTask] | None) -> str:
        """running -> succeeded AND its downstream tasks, in ONE transaction: there is never a moment where the
        upstream is done but its required follow-up does not exist. A pending cancel wins (nothing downstream).
        `downstream=None` means the follow-up could not be planned: the task is left for reconciliation."""
        with self.txn() as db:
            row = db.execute("SELECT state, control, command_id FROM stage_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "running":
                return row["state"] if row else "missing"
            if row["control"] == "cancel_requested":
                db.execute("UPDATE stage_tasks SET state='cancelled', error=?, updated_at=? WHERE id=?",
                           (json.dumps({"code": "cancelled", "message": "cancelled by operator"}), now_iso(),
                            task_id))
                return "cancelled"
            ok = 0 if downstream is None else 1
            if downstream:
                ok, result = self._create_downstream(db, downstream, row["command_id"], result)
            db.execute("UPDATE stage_tasks SET state='succeeded', result=?, downstream_ok=?, updated_at=?, "
                       "revision=revision+1 WHERE id=?", (json.dumps(result), ok, now_iso(), task_id))
            return "succeeded"

    def _create_downstream(self, db: sqlite3.Connection, downstream: list[NewTask], command_id: str,
                           result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Follow-up chain all-or-nothing. Busy defers it (re-admitted once the older owner finishes); a run that
        was cancelled/closed meanwhile skips it for good."""
        db.execute("SAVEPOINT downstream")
        try:
            self.create(downstream, command_id, db)
        except Busy as e:
            db.execute("ROLLBACK TO downstream")
            return 0, {**result, "downstream_deferred": {"owner": e.owner}}
        except RunNotOpen as e:
            db.execute("ROLLBACK TO downstream")
            return 1, {**result, "downstream_skipped": f"run {e.control}"}
        finally:
            db.execute("RELEASE downstream")
        return 1, result

    def request_cancel(self, ids: list[str]) -> list[str]:
        """Intent first (survives restarts), then immediate cancellation of anything not running."""
        done = []
        with self._lock:
            for tid in ids:
                self._db.execute("UPDATE stage_tasks SET control='cancel_requested', updated_at=? WHERE id=? "
                                 f"AND state IN ({','.join('?' * len(TASK_ACTIVE))})",
                                 (now_iso(), tid, *TASK_ACTIVE))
                if self._reconciling_inflight(tid):  # the runner must acknowledge; maintenance settles it
                    continue
                if self._set(tid, ("queued", "blocked", "reconciling"), "cancelled",
                             error={"code": "cancelled", "message": "cancelled before start"}):
                    done.append(tid)
            self.changed.notify_all()
        return done

    def _reconciling_inflight(self, task_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM stage_tasks WHERE id=? AND state='reconciling' AND EXISTS (SELECT 1 FROM attempts "
            f"WHERE task_id=stage_tasks.id AND state IN ({','.join('?' * len(INFLIGHT_ATTEMPTS))}))",
            (task_id, *INFLIGHT_ATTEMPTS)).fetchone()
        return row is not None

    def set_control(self, ids: list[str], control: str, only_from: tuple[str, ...]) -> int:
        n = 0
        with self._lock:
            for tid in ids:
                n += self._db.execute(
                    f"UPDATE stage_tasks SET control=?, updated_at=? WHERE id=? AND control IN "
                    f"({','.join('?' * len(only_from))}) AND state IN ({','.join('?' * len(TASK_ACTIVE))})",
                    (control, now_iso(), tid, *only_from, *TASK_ACTIVE)).rowcount
            self.changed.notify_all()
        return n

    def retry(self, task_id: str) -> bool:
        """Explicit retry of the same logical inputs; never resurrects cancelled work. Raises Busy when another
        command's task still owns the item stage, RunNotOpen when its run was cancelled/closed."""
        with self.txn() as db:
            r = db.execute("SELECT * FROM stage_tasks WHERE id=?", (task_id,)).fetchone()
            if r is None or r["state"] not in ("failed", "blocked") or r["control"] != "run":
                return False
            control = "run"
            if r["run_id"]:
                run_ctl = runcontrol.read_control(db, r["run_id"])[0]
                if run_ctl in ("cancelled", "closed"):
                    raise RunNotOpen(r["run_id"], run_ctl)
                control = "paused" if run_ctl == "paused" else "run"
            owner = db.execute(
                f"SELECT id FROM stage_tasks WHERE project_id=? AND item_id=? AND family=? AND command_id != ? "
                f"AND state IN ({','.join('?' * len(TASK_ACTIVE))}) LIMIT 1",
                (r["project_id"], r["item_id"], r["family"], r["command_id"], *TASK_ACTIVE)).fetchone()
            if owner is not None:
                raise Busy(r["item_id"], r["family"], owner["id"])
            db.execute("UPDATE stage_tasks SET state='queued', control=?, error=NULL, updated_at=?, "
                       "revision=revision+1 WHERE id=?", (control, now_iso(), task_id))
        return True

    def block_queued(self, ids: list[str], error: dict[str, Any]) -> int:
        """Conditional queued -> blocked (admission gave up); running/cancelled work is untouched."""
        return sum(self._set(tid, ("queued",), "blocked", error=error) for tid in ids)

    def exhaust(self, task_id: str, err: dict[str, Any], attempts: int) -> None:
        """Stop automatic retries: the task stays blocked for an explicit operator decision."""
        self._set(task_id, ("blocked",), "blocked", error={
            **err, "retryable": False, "retry_budget_exhausted": True,
            "message": f"{err.get('message', '')} (automatic retries exhausted after {attempts} attempts; "
                       "retry explicitly)"[:500]})

    def block(self, task_id: str, error: dict[str, Any]) -> str:
        return self.finish(task_id, "blocked", error=error)

    def recover_after_restart(self, keep: frozenset[str] | set[str] = frozenset()) -> dict[str, int]:
        """Never assume work stopped or finished because this process restarted: running tasks are requeued
        (handlers reconcile engine work by deterministic ids); pending cancellations complete as cancelled.
        Tasks in `keep` (node mode: their runner attempts are still live) become `reconciling` instead, and their
        pending cancel stays pending until the runner acknowledges it (R8)."""
        kept = tuple(keep)
        skip = f" AND id NOT IN ({','.join('?' * len(kept))})" if kept else ""
        with self._lock:
            # Engine work a crashed process had in flight for a task whose cancel was pending: the caller asks the
            # engine to stop exactly these prompts (never a global interrupt).
            orphans = [{"id": r["id"], "stage": r["stage"], "progress": json.loads(r["progress"] or "{}")}
                       for r in self._db.execute(
                           "SELECT id, stage, progress FROM stage_tasks WHERE state IN ('running','reconciling') "
                           "AND control='cancel_requested'").fetchall()]
            c = self._db.execute(
                "UPDATE stage_tasks SET state='cancelled', error=?, updated_at=? WHERE state IN "
                "('running','reconciling','queued','blocked') AND control='cancel_requested'" + skip,
                (json.dumps({"code": "cancelled", "message": "cancelled (confirmed after restart)"}),
                 now_iso(), *kept)).rowcount
            if kept:
                self._db.execute(
                    "UPDATE stage_tasks SET state='reconciling', updated_at=? WHERE state IN ('running','reconciling')"
                    f" AND id IN ({','.join('?' * len(kept))})", (now_iso(), *kept))
            r = self._db.execute(
                "UPDATE stage_tasks SET state='queued', progress=json_set(progress, '$.reconciled_after_restart', 1), "
                "updated_at=? WHERE state IN ('running','reconciling')" + skip, (now_iso(), *kept)).rowcount
            self.changed.notify_all()
        return {"cancelled": c, "requeued": r, "orphans": orphans}

    def settle_reconciling(self, task_id: str, to: str) -> bool:
        """A reconciling task whose attempts are all settled: `queued` re-enters the stage (it replays ingested
        results), `cancelled` completes a cancel the runner has acknowledged."""
        if to == "cancelled":
            return self._set(task_id, ("reconciling",), "cancelled",
                             error={"code": "cancelled", "message": "cancelled (runner acknowledged)"})
        cur = self.get(task_id)
        progress = {**(cur.progress if cur else {}), "reconciled_after_restart": 1}
        return self._set(task_id, ("reconciling",), "queued", progress=progress)

    def pending_downstream(self, project_id: str | None = None, item_id: str | None = None) -> list[StageTask]:
        where, args = "", []
        for col, val in (("project_id", project_id), ("item_id", item_id)):
            if val is not None:
                where += f" AND {col}=?"
                args.append(val)
        with self._lock:
            rows = self._db.execute("SELECT * FROM stage_tasks WHERE state='succeeded' AND downstream_ok=0"
                                    + where + " ORDER BY seq", args).fetchall()
        return [_task(r) for r in rows]

    def mark_downstream(self, task_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE stage_tasks SET downstream_ok=1 WHERE id=?", (task_id,))

    # --- passes --------------------------------------------------------------------------------------------------
    def open_pass(self, lane: str, residency: str, worker: str | None, loads_before: dict[str, Any] | None) -> str:
        pid = new_id("pas")
        with self._lock:
            self._db.execute("INSERT INTO passes (id, lane, residency, worker, started_at, loads_before) "
                             "VALUES (?,?,?,?,?,?)", (pid, lane, residency, worker, now_iso(),
                                                      json.dumps(loads_before) if loads_before is not None else None))
        return pid

    def close_pass(self, pass_id: str, *, task_ids: list[str], jobs: list[str], reason: str,
                   loads_after: dict[str, Any] | None, measured: dict[str, Any], session: str | None,
                   epoch: int | None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE passes SET task_ids=?, jobs=?, ended_at=?, close_reason=?, loads_after=?, measured=?, "
                "session=?, epoch=? WHERE id=?",
                (json.dumps(task_ids), json.dumps(jobs), now_iso(), reason,
                 json.dumps(loads_after) if loads_after is not None else None, json.dumps(measured), session, epoch,
                 pass_id))

    def passes(self, *, lane: str | None = None, limit: int = 100, since_seq: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM passes", []
        if lane:
            sql, args = sql + " WHERE lane=?", [lane]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY started_at DESC LIMIT ?", [*args, limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("task_ids", "jobs", "loads_before", "loads_after", "measured"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    # --- command intents (outbox for multi-step commands) --------------------------------------------------------
    def intent(self, project_id: str, action: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM command_intents WHERE project_id=? AND action=? AND key=?",
                                 (project_id, action, key)).fetchone()
        return None if r is None else {**dict(r), "intent": json.loads(r["intent"])}

    def record_intent(self, project_id: str, action: str, key: str, command_id: str, intent: dict[str, Any],
                      db: sqlite3.Connection | None = None) -> None:
        now = now_iso()
        (db or self._db).execute("INSERT OR IGNORE INTO command_intents VALUES (?,?,?,?,?,?,?,?)",
                                 (project_id, action, key, command_id, json.dumps(intent), "intent", now, now))

    def complete_intent(self, project_id: str, action: str, key: str, db: sqlite3.Connection | None = None) -> None:
        (db or self._db).execute("UPDATE command_intents SET state='committed', updated_at=? WHERE project_id=? "
                                 "AND action=? AND key=?", (now_iso(), project_id, action, key))

    def open_intents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM command_intents WHERE state='intent' ORDER BY created_at").fetchall()
        return [{**dict(r), "intent": json.loads(r["intent"])} for r in rows]
