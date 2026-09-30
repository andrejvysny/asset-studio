"""Pull/push agent loop. Order per attempt (R8): accept -> start_authorized -> persist locally -> engine ->
spool (fsync + sha256) -> report spooled -> upload -> complete -> keep the spool until a disposition receipt."""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import queue
import re
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from assetstudio_client import ApiError, RunnerClient, TransportError, keys
from assetstudio_protocol.errors import ErrorBody
from assetstudio_protocol.execution import (
    AcceptRequest,
    AcquireRequest,
    AttemptReport,
    CompleteRequest,
    DispositionReceipt,
    FileRef,
    Offer,
    ReportRequest,
)
from assetstudio_protocol.runners import (
    Heartbeat,
    HeartbeatResponse,
    Platform,
    RegisterRequest,
    SessionAccepted,
    SessionHello,
    StudioKey,
)
from assetstudio_protocol.versions import PROTOCOL_VERSION
from pydantic import TypeAdapter

from . import __version__
from .barrier import recover_slots
from .config import RunnerConfig
from .executor import ExecutionBlocked, ExecutionCancelled, ExecutionFailed, Executor
from .inventory import build_inventory
from .receipts import session_receipts
from .spool import Spool
from .state import AttemptRow, RunnerState

log = logging.getLogger("assetstudio_node")

_ERROR_CODES = {"input_invalid": "invalid_input", "oom": "resource_exhausted", "internal": "uncertain_execution"}
_STALE_CODES = ("stale_generation", "cancelled_by_operator")
_FEATURE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}")  # "export-feature." + this fits the 63-char Label
_KEYS = TypeAdapter(list[StudioKey])
_BACKOFF_MAX_S = 30.0
_BARRIER_RETRY_S = 30.0


def load_or_create_key(config: RunnerConfig) -> bytes:
    path = config.key_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return keys.load_private_key(path)
    raw = keys.generate_private_key()
    try:
        keys.save_private_key(path, raw)
    except FileExistsError:
        return keys.load_private_key(path)
    return raw


def _is_stale(e: ApiError) -> bool:
    return e.status == 409 and e.code in _STALE_CODES


class RunnerAgent:
    def __init__(self, config: RunnerConfig, *, client: RunnerClient, executor: Executor, state: RunnerState,
                 spool: Spool, clock: Callable[[], float] = time.monotonic, idle_s: float = 0.5) -> None:
        self.config = config
        self.client = client
        self.executor = executor
        self.state = state
        self.spool = spool
        self.clock = clock
        self.stopped = False
        self.lifecycle = "active"
        self._idle_s = idle_s
        self._runner_id = ""
        self._session_id = ""
        self._heartbeat_s = 15
        self._next_hb = 0.0
        self._next_receipt_poll = 0.0
        self._catalog_sha = ""
        self._catalog: dict[str, Any] = {}
        self._slot_states: dict[str, str] = {}
        self._inv_revision = 0
        self._next_barrier = 0.0
        self._studio_keys: list[StudioKey] = []
        self._ephemeral = False
        self._taken = False
        self._cancel: set[str] = set()
        self._pushed: queue.Queue[Offer] = queue.Queue()
        self._queued: set[tuple[str, int]] = set()
        self._qlock = threading.Lock()
        self._hb_lock = threading.Lock()

    @property
    def studio_keys(self) -> list[StudioKey]:
        return list(self._studio_keys)

    # -- startup -------------------------------------------------------------------------------------------------

    def bootstrap(self, registration_token: str | None) -> None:
        private = load_or_create_key(self.config)
        runner_id = self.state.get_identity("runner_id")
        if runner_id is None:
            if not registration_token:
                raise RuntimeError("runner is not registered: a registration token is required")
            req = RegisterRequest(
                schema="assetstudio.runner.register.v1", registration_token=registration_token,
                public_key=keys.public_key_b64(private), name=self.config.name,
                platform=Platform(os=platform.system()[:64] or "unknown", arch=platform.machine()[:64] or "unknown",
                                  hostname=socket.gethostname()[:64] or "unknown"))
            out = self.client.register(req)
            self.state.set_identity("runner_id", out.runner_id)
            self.state.set_identity("group_id", out.group_id)
            self.state.set_identity("ephemeral", "1" if out.ephemeral else "0")
            self._set_keys(out.studio_keys)
            runner_id = out.runner_id
        self._runner_id = runner_id
        self._ephemeral = self.state.get_identity("ephemeral") == "1"
        raw = self.state.get_identity("studio_keys")
        self._studio_keys = _KEYS.validate_json(raw) if raw else []
        self.client.runner_id = runner_id

    def _set_keys(self, studio_keys: list[StudioKey]) -> None:
        self._studio_keys = list(studio_keys)
        self.state.set_identity("studio_keys", _KEYS.dump_json(studio_keys).decode())

    def _mark_crashed_executions(self) -> list[str]:
        """Resolve attempts the previous agent process left `executing`. A complete spool only missed its `spooled`
        step: finish it (worker ack) and deliver. Without a spool, an executor that reconciles by engine id (prompt /
        execution id) re-runs the call and picks up the engine's result instead of recomputing; the in-process fake
        executor died with the agent, so its attempt is reported lost (A09)."""
        lost = []
        for a in self.state.list_attempts():
            if a.state != "executing":
                continue
            if self.spool.manifest(a.attempt_id) is not None:
                self.executor.spooled(a.offer)
                self.state.set_state(a.attempt_id, "spooled")
            elif getattr(self.executor, "reconciles", False):
                self.state.set_state(a.attempt_id, "admitted")
            else:
                self.state.set_state(a.attempt_id, "lost")
                lost.append(a.attempt_id)
        return lost

    def open_session(self) -> SessionAccepted:
        lost = self._mark_crashed_executions()
        boot_id = str(uuid.uuid4())
        self.state.set_identity("boot_id", boot_id)
        hello = SessionHello(
            schema="assetstudio.runner.session.v1", runner_id=self._runner_id, boot_id=boot_id,
            protocol_versions=[PROTOCOL_VERSION], software={"assetstudio-node": __version__},
            platform=Platform(os=platform.system()[:64] or "unknown", arch=platform.machine()[:64] or "unknown",
                              hostname=socket.gethostname()[:64] or "unknown"),
            dispatch=self.config.dispatch, local_attempts=self.state.local_attempts(self.spool))
        acc = self.client.open_session(hello)
        self._session_id = acc.session_id
        self._heartbeat_s = acc.heartbeat_s
        self._catalog_sha = acc.catalog_sha256
        self._catalog = acc.catalog
        self._set_keys(acc.studio_keys)
        self.state.set_identity("session", acc.model_dump_json())
        for attempt_id in lost:  # Studio now knows; a lost attempt has no bytes, so no receipt will follow
            self.spool.delete(attempt_id)
            self.state.delete_attempt(attempt_id)
        # R7: engines can outlive the agent, so no slot is advertised before the barrier has run
        self._slot_states = dict(recover_slots(self.config, self.executor, self.state))
        self._next_barrier = self.clock() + _BARRIER_RETRY_S
        self._put_inventory()
        self._next_hb = 0.0
        return acc

    def _engine_labels(self) -> list[str]:
        """Facts the engines report about themselves; best-effort, a label is simply absent on any error."""
        worker3d = getattr(getattr(self.executor, "engines", None), "worker3d", None)
        if worker3d is None:
            return []
        try:
            health = worker3d.health()
        except Exception:  # noqa: BLE001 - an unreachable engine must not block inventory publication
            return []
        labels = ["exporter-research"] if (health.get("exporters") or {}).get("research") else []
        # Label pattern (protocol base.Label) is lowercase [a-z0-9_.-]; anything else would reject the inventory.
        labels += [f"export-feature.{f}" for f in health.get("export_features") or []
                   if isinstance(f, str) and _FEATURE.fullmatch(f)]
        return labels

    def _put_inventory(self) -> None:
        self._inv_revision += 1
        models = session_receipts(self.config, self.state, self._catalog, self._catalog_sha)
        inv = build_inventory(self.config, self._inv_revision, self._catalog_sha, runner_id=self._runner_id,
                              models=models, slot_states=self._slot_states, extra_labels=self._engine_labels())
        self.client.put_inventory(self._session_id, inv)

    def _recheck_slots(self) -> None:
        """A slot that failed the barrier is retried at most every 30 s; a changed state is re-published. Runs
        between attempts only: the barrier unloads workers, which must never happen under running work."""
        bad = {s for s, st in self._slot_states.items() if st != "ready"}
        if not bad or self.clock() < self._next_barrier:
            return
        self._next_barrier = self.clock() + _BARRIER_RETRY_S
        fresh = recover_slots(self.config, self.executor, self.state, only=bad)
        if any(self._slot_states[s] != st for s, st in fresh.items()):
            self._slot_states.update(fresh)
            self._put_inventory()

    # -- loop ----------------------------------------------------------------------------------------------------

    def step(self) -> bool:
        """One loop iteration; True if it did work."""
        worked = self.resume_pending()
        self._recheck_slots()
        self._heartbeat_if_due()
        if self._maybe_finish_ephemeral():
            return True
        offer, pushed = self._next_offer()
        if offer is not None:
            self.handle_offer(offer, pushed=pushed)
            return True
        return worked

    def run_forever(self, stop: threading.Event) -> None:
        beats = threading.Thread(target=self._heartbeat_loop, args=(stop,), name="runner-heartbeat", daemon=True)
        beats.start()
        backoff = 1.0
        while not stop.is_set() and not self.stopped:
            try:
                if not self.step():
                    stop.wait(self._idle_s)
                backoff = 1.0
            except TransportError as e:
                log.warning("studio unreachable: %s", e)
                stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
            except ApiError as e:
                if e.code == "stale_session":
                    self.open_session()
                    continue
                log.error("api error: %s", e)
                stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

    def enqueue_offer(self, offer: Offer) -> bool:
        """Called by the push listener; False when this attempt+generation is already queued."""
        key = (offer.attempt_id, offer.generation)
        with self._qlock:
            if key in self._queued:
                return False
            self._queued.add(key)
        self._pushed.put(offer)
        return True

    def _next_offer(self) -> tuple[Offer | None, bool]:
        try:
            offer = self._pushed.get_nowait()
        except queue.Empty:
            offer = None
        if offer is not None:
            with self._qlock:
                self._queued.discard((offer.attempt_id, offer.generation))
            return offer, True
        if self.lifecycle != "active" or (self._ephemeral and self._taken):
            return None, False
        if self.config.dispatch == "push":
            return None, False
        free = [s.slot_id for s in self.config.slots if self._slot_states.get(s.slot_id, "ready") == "ready"]
        if not free:  # every slot failed the barrier: nothing may be scheduled until a recheck passes
            return None, False
        req = AcquireRequest(request_id=str(uuid.uuid4()), free_slots=free, cached_residencies=[],
                             wait_s=self.config.acquire_wait_s)
        return self.client.acquire(self._session_id, req), False

    # -- heartbeat -----------------------------------------------------------------------------------------------

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        """Leases must keep renewing while an engine call blocks the main loop for minutes (TRELLIS sampling, VLM
        batches); a healthy runner must never turn its own work `uncertain`."""
        while not stop.wait(1.0) and not self.stopped:
            try:
                self._heartbeat_if_due()
            except (TransportError, ApiError) as e:  # the main loop owns recovery (re-session, backoff)
                log.warning("background heartbeat failed: %s", e)

    def _heartbeat_if_due(self, *, force: bool = False, lifecycle: str | None = None) -> None:
        with self._hb_lock:
            self._heartbeat_locked(force, lifecycle)

    def _heartbeat_locked(self, force: bool, lifecycle: str | None) -> None:
        if not self._session_id or (not force and self.clock() < self._next_hb):
            return
        local = self.state.local_attempts(self.spool)[:256]
        reports = [AttemptReport(attempt_id=a.attempt_id, generation=a.generation, state=a.state) for a in local]
        hb = Heartbeat(session_id=self._session_id, inventory_revision=self._inv_revision, attempts=reports,
                       lifecycle=lifecycle or self.lifecycle)
        resp = self.client.heartbeat(self._session_id, hb)
        self._next_hb = self.clock() + self._heartbeat_s
        self._apply_heartbeat(resp)

    def _apply_heartbeat(self, resp: HeartbeatResponse) -> None:
        for c in resp.controls:
            if c.control == "cancel":
                self._cancel.add(c.attempt_id)
        for r in resp.receipts:
            self._on_receipt(r)
        if resp.inventory_wanted:
            self._put_inventory()

    def _on_receipt(self, r: DispositionReceipt) -> None:
        row = self.state.get_attempt(r.attempt_id)
        if row is None or row.generation != r.generation:
            return
        self.spool.delete(r.attempt_id)
        self.state.delete_attempt(r.attempt_id)
        self._cancel.discard(r.attempt_id)
        log.info("attempt %s disposition %s: spool released", r.attempt_id, r.disposition)

    def _maybe_finish_ephemeral(self) -> bool:
        if not (self._ephemeral and self._taken) or self.state.list_attempts():
            return False
        self._heartbeat_if_due(force=True, lifecycle="safe_to_terminate")
        self.client.deregister()
        self.stopped = True
        log.info("ephemeral runner deregistered after its attempt")
        return True

    # -- attempts ------------------------------------------------------------------------------------------------

    def handle_offer(self, offer: Offer, *, pushed: bool = False) -> None:
        if pushed and (offer.runner_id != self._runner_id or not keys.verify_offer(offer, self._studio_keys)):
            log.warning("ignoring pushed offer %s: not addressed to us or bad signature", offer.attempt_id)
            return
        try:
            resp = self.client.accept(offer.attempt_id, AcceptRequest(session_id=self._session_id,
                                                                      generation=offer.generation))
            if not resp.start_authorized:
                log.info("accept for %s did not authorize start", offer.attempt_id)
                return
            if not self.state.record_attempt(offer):
                log.info("duplicate delivery of %s: not executing again", offer.attempt_id)
                return
            self._taken = True
            self._run_admitted(offer)
        except ApiError as e:
            if _is_stale(e):
                self._abandon(offer.attempt_id, e.code)
            else:
                log.error("attempt %s: %s", offer.attempt_id, e)
        except TransportError as e:
            log.warning("attempt %s: transport failure, local state kept: %s", offer.attempt_id, e)

    def _run_admitted(self, offer: Offer) -> None:
        aid = offer.attempt_id
        self._report(aid, offer.generation, "admitted")
        try:
            inputs = self._fetch_inputs(offer)
            self.state.set_state(aid, "executing")
            self._report(aid, offer.generation, "executing")
            work = self.config.state_dir / "work" / aid
            shutil.rmtree(work, ignore_errors=True)
            outputs, meta = self._execute(offer, inputs, work)
        except ExecutionCancelled:
            return self._finish_terminal(offer, "cancelled", None)
        except ExecutionFailed as e:
            body = ErrorBody(code=_ERROR_CODES[e.code], message=str(e)[:500])
            return self._finish_terminal(offer, "failed", body)
        except ExecutionBlocked as e:
            return self._finish_blocked(offer, e)
        refs = [self.spool.write_file(aid, name, path, mime) for name, path, mime in outputs]
        shutil.rmtree(work, ignore_errors=True)
        manifest = self.spool.write_manifest(aid, offer.generation, refs, meta)
        self._notify_spooled(offer)
        self.state.set_state(aid, "spooled", manifest=manifest.model_dump_json())
        self._report(aid, offer.generation, "spooled")
        self._deliver(aid, offer.generation)

    def _notify_spooled(self, offer: Offer) -> None:
        """R8/I07: the engine may release its copy of the result only now that the manifest is durable."""
        try:
            self.executor.spooled(offer)
        except Exception:  # noqa: BLE001 - the spool is safe; a failed release must not fail the attempt
            log.warning("spooled hook failed for %s", offer.attempt_id, exc_info=True)

    def _finish_blocked(self, offer: Offer, e: ExecutionBlocked) -> None:
        """Engine unreachable or GPU ownership unknown: this attempt fails, the slot keeps serving others unless
        ownership is unknown (then it leaves rotation until the barrier passes again)."""
        if e.code == "admission_rejected" and self._slot_states.get(offer.slot_id) == "ready":
            self._slot_states[offer.slot_id] = "unknown"
            self._next_barrier = self.clock() + _BARRIER_RETRY_S
            try:
                self._put_inventory()
            except TransportError as err:
                log.warning("inventory republish failed: %s", err)
        self._finish_terminal(offer, "failed", ErrorBody(code=e.code, message=str(e)[:500]))

    def _execute(self, offer: Offer, inputs: dict[str, Path], work: Path) -> tuple[list[tuple[str, Path, str]],
                                                                                   dict[str, Any]]:
        aid = offer.attempt_id

        def should_cancel() -> bool:
            # Executors block this thread, so control messages are polled from here. A network blip must not kill
            # a running engine; the heartbeat is simply retried on the next poll.
            try:
                self._heartbeat_if_due()
            except TransportError as e:
                log.warning("heartbeat during execution failed: %s", e)
            return aid in self._cancel

        return self.executor.execute(offer, inputs, work, should_cancel)

    def _fetch_inputs(self, offer: Offer) -> dict[str, Path]:
        cache = self.config.state_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        out: dict[str, Path] = {}
        for ref in offer.inputs:
            path = cache / ref.sha256
            if not (path.exists() and _sha256_file(path) == ref.sha256):
                self._download(offer.attempt_id, ref.sha256, path)
            out[ref.sha256] = path
        return out

    def _download(self, attempt_id: str, sha: str, dst: Path) -> None:
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.part")
        try:
            with tmp.open("wb") as f:
                self.client.download_input(attempt_id, sha, f)
            os.replace(tmp, dst)
        except ValueError as e:
            raise ExecutionFailed("input_invalid", str(e)) from e
        except ApiError as e:
            if _is_stale(e) or e.status >= 500:
                raise
            raise ExecutionFailed("input_invalid", f"input {sha} unavailable: {e.code}") from e
        finally:
            tmp.unlink(missing_ok=True)

    def _deliver(self, aid: str, generation: int) -> None:
        manifest = self.spool.manifest(aid)
        if manifest is None:
            return
        self.state.set_state(aid, "uploading")
        self._report(aid, generation, "uploading")
        for f in manifest.files:
            self._upload_one(aid, generation, f)
        self.client.complete(aid, CompleteRequest(session_id=self._session_id, manifest=manifest))
        self.state.set_state(aid, "ingested")

    def _upload_one(self, aid: str, generation: int, f: FileRef) -> None:
        self.client.upload_file(self.spool.dir_for(aid) / f.name, attempt_id=aid, generation=generation,
                                role=f.name, mime=f.mime)

    def _report(self, aid: str, generation: int, state: str, error: ErrorBody | None = None) -> bool:
        """Progress reports are best effort (the heartbeat repeats local state); returns False if not delivered."""
        rep = AttemptReport(attempt_id=aid, generation=generation, state=state, error=error)
        try:
            self.client.report(aid, ReportRequest(session_id=self._session_id, report=rep))
        except TransportError as e:
            log.warning("report %s/%s not delivered: %s", aid, state, e)
            return False
        return True

    def _finish_terminal(self, offer: Offer, state: str, error: ErrorBody | None) -> None:
        aid = offer.attempt_id
        self.state.set_state(aid, state, error=error.message if error else None)
        self._cancel.discard(aid)
        if self._report(aid, offer.generation, state, error) and not self.spool.files(aid):
            self.state.delete_attempt(aid)

    def _abandon(self, aid: str, code: str) -> None:
        """Studio superseded or cancelled this attempt: stop working on it; keep any spool until a receipt."""
        if self.state.get_attempt(aid) is None:
            return
        self.state.set_state(aid, "cancelled" if code == "cancelled_by_operator" else "quarantined")
        self._cancel.discard(aid)
        if not self.spool.files(aid):
            self.state.delete_attempt(aid)

    # -- recovery ------------------------------------------------------------------------------------------------

    def resume_pending(self) -> bool:
        """Retry interrupted work. `admitted` means the engine was never invoked, so re-running is safe; an
        `executing` attempt is never re-run here (its outcome is unknown)."""
        worked = False
        for row in self.state.list_attempts():
            try:
                worked |= self._resume_one(row)
            except ApiError as e:
                if not _is_stale(e):
                    raise
                self._abandon(row.attempt_id, e.code)
        return worked

    def _resume_one(self, row: AttemptRow) -> bool:
        if row.state == "admitted":
            self._run_admitted(row.offer)
            return True
        if row.state in ("spooled", "uploading"):
            self._deliver(row.attempt_id, row.generation)
            return True
        if row.state in ("ingested", "quarantined", "cancelled", "failed") and self.clock() >= self._next_receipt_poll:
            self._next_receipt_poll = self.clock() + self._heartbeat_s
            receipt = self.client.receipt(row.attempt_id)
            if receipt is not None:
                self._on_receipt(receipt)
                return True
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()
