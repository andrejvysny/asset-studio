"""Bulk-publish the static GLBs of the fantasy-game-library into one AssetStudio library through the integration API.

Idempotent and resumable: a state manifest (no secrets) records per asset the source hash, idempotency key, preview id
and outcome; the server's `publication-operations/{key}` is consulted before any upload, so a lost or stale state file
never produces a duplicate version. Modes: default import, `--dry-run` (offline checks only), `--verify` (resolve every
committed ref, no publishing).

    uv run python scripts/import_library.py --source ~/fantasy-game-library --library prj_... --token-file TOKEN
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from assetstudio_core.canonical_v1 import decimal_str
from assetstudio_core.naming import slug
from assetstudio_core.publication_draft import draft_bytes, parse_draft
from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.glb_budget import glb_budget, glb_json
from assetstudio_processing.transforms import TransformRejected, inspect_static_glb

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "godot-integration" / "v1"
DEFAULT_STATE = Path("~/.local/state/assetstudio-import/fantasy-base.json")
LICENCE, CREDIT = "own work", "fantasy-game procedural kit"
NATURE_GROUPS = ("rocks", "trees", "groundcover", "understory", "debris")
FOLIAGE_WORDS = ("foliage", "leaf", "needle", "grass")
DONE = ("committed", "verified")
RETRIES, RETRY_WAIT_S = 3, 2.0
RESOLVE_BATCH = 200


class ImportFailure(Exception):
    """One asset cannot be published (bad GLB, bad draft); the run continues with the next asset."""


@dataclass
class Entry:
    relpath: str
    kind: str  # nature | spruce | prop | grass | character
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GlbFacts:
    doc: dict[str, Any]
    bounds_min: list[float]
    bounds_max: list[float]
    triangles: int | None
    materials: int
    warnings: list[str]


@dataclass
class Plan:
    entry: Entry
    sha256: str
    glb: bytes
    facts: GlbFacts
    draft: bytes
    commit: dict[str, Any]


# --- discovery --------------------------------------------------------------------------------------------------------
def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def discover(source: Path) -> list[Entry]:
    """Static model GLBs only: previews, materials, sources, .import files, scripts and tools are never listed."""
    entries: list[Entry] = []
    for glb in sorted(source.glob("assets/nature/*/*.glb")):
        if glb.parent.name not in NATURE_GROUPS:
            continue
        meta_path = glb.with_suffix(".json")
        entries.append(Entry(glb.relative_to(source).as_posix(), "nature",
                             _read_json(meta_path) if meta_path.is_file() else {}))
    variants_path = source / "spruce_trees/models/variants.json"
    variants = {v["file"]: v for v in _read_json(variants_path)["variants"]} if variants_path.is_file() else {}
    for glb in sorted(source.glob("spruce_trees/models/*.glb")):
        entries.append(Entry(glb.relative_to(source).as_posix(), "spruce", variants.get(glb.name, {})))
    others = (("props/*.glb", "prop"), ("grass/glb/*.glb", "grass"), ("characters/character.glb", "character"))
    for pattern, kind in others:
        entries += [Entry(g.relative_to(source).as_posix(), kind) for g in sorted(source.glob(pattern))]
    entries.sort(key=lambda e: e.relpath)
    keys = [idempotency_key(e) for e in entries]
    if len(set(keys)) != len(keys):
        raise ImportFailure("idempotency keys collide; rename a source file")
    return entries


def idempotency_key(entry: Entry) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", entry.relpath.removesuffix(".glb").lower()).strip("-")
    key = f"fgl-{stem}-v1"
    if not 8 <= len(key) <= 100:
        raise ImportFailure(f"idempotency key length out of range for {entry.relpath}")
    return key


def asset_name(entry: Entry) -> str:
    return str(entry.metadata.get("asset_id") or entry.metadata.get("name") or Path(entry.relpath).stem)


def tags_for(entry: Entry) -> list[str]:
    meta = entry.metadata
    raw: list[Any] = [entry.kind, meta.get("group"), meta.get("family"), *(meta.get("habitats") or []),
                      meta.get("kind") if meta.get("kind") in ("live", "dead") else None]
    return sorted({slug(str(t), 32) for t in raw if t})[:50]


# --- GLB checks and descriptor draft ----------------------------------------------------------------------------------
def load_limits() -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / "capabilities.json").read_text())["limits"]


def analyze_glb(data: bytes, limits: dict[str, Any]) -> GlbFacts:
    """Same checks the server repeats at preview: static, self-contained, bounds measured here."""
    result = validate_glb_bytes(data, require_texture=False)
    if not result["ok"]:
        failed = [f"{c.get('id')}: {str(c.get('detail', ''))[:120]}" for c in result["checks"] if not c.get("ok")]
        raise ImportFailure("GLB failed validation: " + "; ".join(failed[:5]))
    try:
        info = inspect_static_glb(data)
    except TransformRejected as e:
        raise ImportFailure(f"not a static model: {str(e)[:200]}") from None
    budget = glb_budget(data, limits)
    warnings = [] if budget["within_ipad_budget"] else ["portable_over_ipad_budget"]
    return GlbFacts(glb_json(data)[0], info["bounds"]["min"], info["bounds"]["max"], budget.get("triangles"),
                    int(info["materials"]), warnings)


def material_role(name: str) -> str:
    return "foliage" if any(w in name.lower() for w in FOLIAGE_WORDS) else "solid"


def material_slots(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """One slot per glTF material that a primitive uses; primitives without a material share a `default` slot."""
    materials = doc.get("materials") or []
    used: dict[int | None, list[dict[str, int]]] = {}
    for mi, mesh in enumerate(doc.get("meshes") or []):
        for pi, prim in enumerate(mesh.get("primitives") or []):
            used.setdefault(prim.get("material"), []).append({"mesh": mi, "primitive": pi})
    slots, taken = [], set()
    for idx in sorted(used, key=lambda i: (i is None, i or 0)):
        name = "default" if idx is None else str(materials[idx].get("name") or f"material_{idx}")
        base = slot_id = slug(name, 60)
        n = 2
        while slot_id in taken:
            slot_id, n = f"{base}_{n}", n + 1
        taken.add(slot_id)
        slots.append({"slot_id": slot_id, "role": material_role(name),
                      "surfaces": {"portable_glb_v1": used[idx]}})
    return slots


def footprint_radius(entry: Entry, facts: GlbFacts) -> float:
    meta = entry.metadata
    if entry.kind == "spruce" and meta.get("crown_radius_m"):
        radius = float(meta["crown_radius_m"])
    else:
        lo = meta.get("bbox_min") if entry.kind == "nature" and meta.get("bbox_min") else facts.bounds_min
        hi = meta.get("bbox_max") if entry.kind == "nature" and meta.get("bbox_max") else facts.bounds_max
        radius = max(abs(lo[0]), abs(hi[0]), abs(lo[2]), abs(hi[2]))
    return max(radius, 0.001)


def scale_range(entry: Entry) -> list[str]:
    if entry.kind == "nature" and entry.metadata.get("scale_range"):
        lo, hi = entry.metadata["scale_range"]
        return [decimal_str(lo), decimal_str(hi)]
    return ["0.8", "1.25"] if entry.kind == "spruce" else ["0.5", "2"]


def build_draft(entry: Entry, facts: GlbFacts) -> bytes:
    """Canonical descriptor-draft bytes; validated by the server's own model and, when available, the JSON schema."""
    draft = {"schema_version": 1, "placement_anchor": ["0", "0", "0"],
             "footprint_radius_m": decimal_str(footprint_radius(entry, facts), "ceil"),
             "scale_range": scale_range(entry), "height_offset_range_m": ["-0.5", "0.5"],
             "default_grounding": "FOLLOW_TERRAIN", "material_slots": material_slots(facts.doc), "collision": None,
             "preview_warnings": []}
    try:
        parsed = parse_draft(json.dumps(draft).encode())
    except ValueError as e:
        raise ImportFailure(f"descriptor draft invalid: {str(e)[:300]}") from None
    raw = draft_bytes(parsed)
    _check_schema(json.loads(raw))
    return raw


def _check_schema(draft: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:  # dev dependency: the pydantic model above is the authoritative check
        return
    docs = [json.loads(p.read_text()) for p in CONTRACT_DIR.glob("*.schema.json")]
    resources = [(d["$id"], Resource.from_contents(d)) for d in docs]
    schema = json.loads((CONTRACT_DIR / "publication-descriptor-draft.schema.json").read_text())
    validator = Draft202012Validator(schema, registry=Registry().with_resources(resources))
    errors = sorted(validator.iter_errors(draft), key=lambda e: list(e.path))
    if errors:
        raise ImportFailure(f"draft violates schema: {errors[0].message[:200]}")


def make_plan(source: Path, entry: Entry, limits: dict[str, Any]) -> Plan:
    glb = (source / entry.relpath).read_bytes()
    facts = analyze_glb(glb, limits)
    commit = {"name": asset_name(entry), "tags": tags_for(entry), "licence": LICENCE,
              "source_uri": f"fantasy-game-library/{entry.relpath}", "credit": CREDIT,
              "idempotency_key": idempotency_key(entry)}
    return Plan(entry, hashlib.sha256(glb).hexdigest(), glb, facts, build_draft(entry, facts), commit)


# --- state ------------------------------------------------------------------------------------------------------------
class State:
    """JSON manifest, rewritten atomically after every asset; holds no credentials."""

    def __init__(self, path: Path, library: str) -> None:
        self.path, self.library = path, library
        self.assets: dict[str, dict[str, Any]] = {}
        if path.is_file():
            data = json.loads(path.read_text())
            if data.get("library") not in (None, library):
                raise ImportFailure(f"state file belongs to library {data.get('library')}, not {library}")
            self.assets = data.get("assets", {})

    def get(self, relpath: str) -> dict[str, Any]:
        return self.assets.get(relpath, {})

    def update(self, relpath: str, **fields: Any) -> None:
        self.assets.setdefault(relpath, {}).update(fields)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fp:
            json.dump({"version": 1, "library": self.library, "assets": self.assets}, fp, indent=1, sort_keys=True)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, self.path)


# --- API --------------------------------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


class Api(Protocol):
    def server_id(self) -> str: ...
    def operation(self, key: str) -> dict[str, Any]: ...
    def preview(self, glb: bytes, draft: bytes) -> dict[str, Any]: ...
    def commit(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def resolve(self, refs: list[dict[str, str]]) -> list[dict[str, Any]]: ...


class HttpApi:
    """Bearer-token client; the token lives only in the client headers and is never printed."""

    def __init__(self, base_url: str, library: str, token: str) -> None:
        self.lib = f"/api/integration/v1/libraries/{library}"
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                                 timeout=httpx.Timeout(600.0, connect=10.0))

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            res = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ApiError(0, "transport_error", type(e).__name__, retryable=True) from None
        if res.status_code >= 400:
            try:
                err = res.json()["error"]
                raise ApiError(res.status_code, str(err["code"]), str(err["message"])[:300], bool(err.get("retryable")))
            except (ValueError, KeyError, TypeError):
                raise ApiError(res.status_code, "http_error", f"HTTP {res.status_code}") from None
        return res.json()

    def server_id(self) -> str:
        return self._call("GET", "/api/integration/v1/capabilities")["server_id"]

    def operation(self, key: str) -> dict[str, Any]:
        return self._call("GET", f"{self.lib}/publication-operations/{key}")

    def preview(self, glb: bytes, draft: bytes) -> dict[str, Any]:
        files = {"portable": ("portable.glb", glb, "model/gltf-binary"),
                 "descriptor": ("descriptor.json", draft, "application/json")}
        return self._call("POST", f"{self.lib}/publications:preview", files=files)

    def commit(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{self.lib}/publications:commit", json=body)

    def resolve(self, refs: list[dict[str, str]]) -> list[dict[str, Any]]:
        body = {"refs": refs, "target": {"representations": ["portable_glb_v1"]}}
        return self._call("POST", f"{self.lib}/resolve", json=body)["entries"]


def _retrying(fn: Any, *args: Any) -> Any:
    for attempt in range(RETRIES + 1):
        try:
            return fn(*args)
        except ApiError as e:
            if not e.retryable or attempt == RETRIES:
                raise
            time.sleep(RETRY_WAIT_S * (attempt + 1))


# --- import -----------------------------------------------------------------------------------------------------------
def _committed(state: State, relpath: str, ref: dict[str, str], **extra: Any) -> None:
    state.update(relpath, state="committed", asset_ref=ref, error=None, **extra)


def _recover(api: Api, state: State, plan: Plan) -> bool:
    """True when the server already holds this publication (lost state, or a commit that landed before a crash)."""
    op = _retrying(api.operation, plan.commit["idempotency_key"])
    if op.get("state") != "committed":
        return False
    ref = {"server_id": api.server_id(), "library_id": state.library, "asset_id": op["asset_id"],
           "version_id": op["version_id"]}
    _committed(state, plan.entry.relpath, ref, display_version=op.get("display_version"))
    return True


def _preview(api: Api, state: State, plan: Plan) -> str:
    receipt = _retrying(api.preview, plan.glb, plan.draft)
    if receipt["portable_sha256"] != plan.sha256:
        raise ApiError(0, "integrity_mismatch", "server hash of the upload differs from the local hash")
    state.update(plan.entry.relpath, state="previewed", preview_id=receipt["preview_id"],
                 draft_sha256=receipt["descriptor_draft_sha256"], error=None)
    return receipt["preview_id"]


def _commit(api: Api, state: State, plan: Plan, preview_id: str) -> None:
    saved = state.get(plan.entry.relpath)
    body = {**plan.commit, "preview_id": preview_id, "portable_sha256": plan.sha256,
            "descriptor_draft_sha256": saved["draft_sha256"], "package_sha256": None}
    res = _retrying(api.commit, body)
    _committed(state, plan.entry.relpath, res["asset_ref"], display_version=res.get("display_version"),
               descriptor_sha256=res.get("descriptor_sha256"))


def publish_one(api: Api, state: State, plan: Plan) -> str:
    """Returns the final state of one asset. Order: skip -> ask the server -> reuse live preview -> new preview."""
    rel, saved = plan.entry.relpath, state.get(plan.entry.relpath)
    base = {"source_sha256": plan.sha256, "idempotency_key": plan.commit["idempotency_key"]}
    if saved.get("source_sha256") not in (None, plan.sha256) and saved.get("state") != "failed":
        state.update(rel, state="conflict", error="source changed since it was published under this idempotency key; "
                                                  "bump the key version deliberately")
        return "conflict"
    if saved.get("state") in DONE:
        return "skipped"
    state.update(rel, **base)
    try:
        if _recover(api, state, plan):
            return "committed"
        if saved.get("state") == "previewed" and saved.get("preview_id") and saved.get("draft_sha256"):
            try:
                _commit(api, state, plan, saved["preview_id"])
                return "committed"
            except ApiError as e:
                if e.code != "preview_expired":
                    raise
        _commit(api, state, plan, _preview(api, state, plan))
        return "committed"
    except ApiError as e:
        outcome = "conflict" if e.code == "idempotency_conflict" else "failed"
        state.update(rel, state=outcome, error=f"{e.code}: {e.message}")
        return outcome


def run_import(source: Path, api: Api, state: State, only: str | None,
               out: Any = sys.stdout) -> dict[str, dict[str, int]]:
    limits, tally = load_limits(), {}
    for entry in _select(discover(source), only):
        try:
            outcome = publish_one(api, state, make_plan(source, entry, limits))
        except ImportFailure as e:
            state.update(entry.relpath, state="failed", error=str(e))
            outcome = "failed"
        tally.setdefault(entry.kind, {}).setdefault(outcome, 0)
        tally[entry.kind][outcome] += 1
        err = state.get(entry.relpath).get("error")
        print(f"{outcome:10} {entry.relpath}" + (f"  [{err}]" if err and outcome in ("failed", "conflict") else ""),
              file=out)
    return tally


def _select(entries: list[Entry], only: str | None) -> list[Entry]:
    return [e for e in entries if not only or fnmatch.fnmatch(e.relpath, only)]


# --- verify -----------------------------------------------------------------------------------------------------------
def run_verify(api: Api, state: State, only: str | None, out: Any = sys.stdout) -> int:
    """Resolve every committed ref; returns the number of assets that are not ready."""
    todo = [(rel, a) for rel, a in sorted(state.assets.items())
            if a.get("state") in DONE and (not only or fnmatch.fnmatch(rel, only))]
    bad = 0
    for i in range(0, len(todo), RESOLVE_BATCH):
        batch = todo[i:i + RESOLVE_BATCH]
        entries = _retrying(api.resolve, [a["asset_ref"] for _, a in batch])
        for (rel, _), res in zip(batch, entries, strict=True):
            delivery = next((d for d in res.get("deliveries", []) if d["representation"] == "portable_glb_v1"), None)
            if res["state"] == "ready" and delivery and res.get("descriptor_sha256"):
                state.update(rel, state="verified", descriptor_sha256=res["descriptor_sha256"],
                             portable_delivery_id=delivery["delivery_id"], error=None)
                continue
            err = res.get("error") or {}
            state.update(rel, error=f"resolve {res['state']}: {err.get('code')} {err.get('message', '')}"[:300])
            print(f"NOT READY  {rel}  [{state.get(rel)['error']}]", file=out)
            bad += 1
    print(f"verified {len(todo) - bad}/{len(todo)}", file=out)
    return bad


# --- dry run ----------------------------------------------------------------------------------------------------------
def run_dry(source: Path, state: State, only: str | None, out: Any = sys.stdout) -> int:
    limits, failures, rows = load_limits(), 0, []
    for entry in _select(discover(source), only):
        saved = state.get(entry.relpath)
        try:
            plan = make_plan(source, entry, limits)
            if saved.get("source_sha256") not in (None, plan.sha256):
                action = "conflict"
            else:
                action = "skip" if saved.get("state") in DONE else "would-publish"
            warn = ",".join(plan.facts.warnings) or "-"
            rows.append((entry.relpath, plan.facts.triangles, plan.facts.materials, warn, action))
            failures += action == "conflict"
        except (ImportFailure, OSError, ValueError) as e:
            rows.append((entry.relpath, None, None, f"FAIL {e}"[:100], "fail"))
            failures += 1
    width = max((len(r[0]) for r in rows), default=10)
    print(f"{'relpath':{width}}  {'tris':>7} {'mats':>4}  {'action':13} warnings", file=out)
    for rel, tris, mats, warn, action in rows:
        print(f"{rel:{width}}  {tris if tris is not None else '-':>7} {mats if mats is not None else '-':>4}  "
              f"{action:13} {warn}", file=out)
    counts = {a: sum(r[4] == a for r in rows) for a in ("would-publish", "skip", "conflict", "fail")}
    print(f"total {len(rows)}: " + ", ".join(f"{k} {v}" for k, v in counts.items()), file=out)
    return failures


# --- cli --------------------------------------------------------------------------------------------------------------
def _token(args: argparse.Namespace) -> str:
    raw = Path(args.token_file).expanduser().read_text() if args.token_file else os.environ.get(
        "ASSETSTUDIO_IMPORT_TOKEN", "")
    token = raw.strip()
    if not token:
        raise SystemExit("error: no token (use --token-file or ASSETSTUDIO_IMPORT_TOKEN)")
    return token


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", required=True, type=Path, help="fantasy-game-library directory (read-only)")
    p.add_argument("--library", required=True, help="target library (project) id, prj_...")
    p.add_argument("--base-url", default="http://127.0.0.1:8192")
    p.add_argument("--token-file", help="file holding the bearer token (default: env ASSETSTUDIO_IMPORT_TOKEN)")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--only", help="glob on the source-relative path")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="offline checks and a plan table; no network")
    mode.add_argument("--verify", action="store_true", help="resolve every committed ref; no publishing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    try:
        state = State(args.state.expanduser(), args.library)
        if args.dry_run:
            return 1 if run_dry(source, state, args.only) else 0
        api = HttpApi(args.base_url, args.library, _token(args))
        if args.verify:
            return 1 if run_verify(api, state, args.only) else 0
        tally = run_import(source, api, state, args.only)
    except ImportFailure as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ApiError as e:
        print(f"error: {e.code}: {e.message}", file=sys.stderr)
        return 2
    print(json.dumps(tally, sort_keys=True))
    return 1 if any(o in ("failed", "conflict") for k in tally.values() for o in k) else 0


if __name__ == "__main__":
    sys.exit(main())
