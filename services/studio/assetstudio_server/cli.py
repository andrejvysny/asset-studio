"""assetstudio CLI: doctor, projects, storage, models. Works without the server running (same instance dir)."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .gpu import nvidia_smi
from .models import HashCache, verify_all
from .registry import Registry
from .settings import Settings


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_doctor(s: Settings, _: argparse.Namespace) -> int:
    ok = True
    print(f"instance dir   {s.instance_dir}")
    print(f"project roots  {', '.join(map(str, s.project_roots))}")
    gpus = nvidia_smi()
    print(f"gpus           {len(gpus)}" + "".join(f"\n  {g['index']}: {g['name']} {g['vram_total_mb']} MiB"
                                               for g in gpus))
    if len(gpus) < 2:
        print("  note: generation needs 2 GPUs in the default profile; library-only mode still works")
    free = shutil.disk_usage(s.instance_dir if s.instance_dir.exists() else Path.home()).free // 2**30
    print(f"free disk      {free} GiB (instance dir volume)")
    models = verify_all(s.config_dir, s.models_root)
    need = sum(m.bytes_expected for m in models.values() if not m.ready) // 2**30
    for k, m in models.items():
        print(f"  {k:28} {m.status:16} {m.detail}")
        ok &= m.ready or m.optional or m.status == "pending_access"
    print(f"model bytes still to download: ~{need} GiB")
    for root in s.project_roots:
        writable = root.exists() and root.is_dir() and shutil.os.access(root, shutil.os.W_OK)  # type: ignore
        print(f"project root   {root}: {'writable' if writable else 'missing/not writable'}")
        ok &= writable
    return 0 if ok else 1


def cmd_models_verify(s: Settings, a: argparse.Namespace) -> int:
    cache = HashCache(s.instance_dir / "model-hashes.json") if a.full else None
    res = verify_all(s.config_dir, s.models_root, full=a.full, cache=cache)
    bad = False
    for k, m in res.items():
        print(f"{k:28} {m.status:16} {m.licence_status:12} {m.detail}")
        bad |= not m.ready and not m.optional and m.status != "pending_access"
    pending = [k for k, m in res.items() if m.status == "pending_access"]
    if pending:
        print(f"\npending access (gated, not downloaded): {', '.join(pending)} — dependent recipes stay disabled")
    return 1 if bad else 0


def cmd_project_create(s: Settings, a: argparse.Namespace) -> int:
    s.ensure()
    ctx = Registry(s).create(a.name, Path(a.root) if a.root else s.project_roots[0] / a.name.lower().replace(" ", "-"),
                             starter_qa=not a.no_starter_qa)
    _print({"id": ctx.id, "root": str(ctx.root)})
    return 0


def cmd_project_list(s: Settings, _: argparse.Namespace) -> int:
    s.ensure()
    _print(Registry(s).list())
    return 0


def cmd_project_register(s: Settings, a: argparse.Namespace) -> int:
    s.ensure()
    ctx = Registry(s).register(Path(a.root))
    _print({"id": ctx.id, "root": str(ctx.root), "read_only": ctx.read_only})
    return 0


def _exclusive(s: Settings, project: str):  # noqa: ANN202 - ProjectContext
    """Offline tools need the single-writer lock: a running Studio must be stopped (or drained) first."""
    ctx = Registry(s).get(project)
    if ctx.read_only:
        print(f"project {project} is owned by a running Studio ({ctx.owner.get('instance_id')}); stop it first",
              file=sys.stderr)
        raise SystemExit(2)
    return ctx


def cmd_project_backup(s: Settings, a: argparse.Namespace) -> int:
    from assetstudio_storage.backup import create_backup

    s.ensure()
    ctx = _exclusive(s, a.project)
    out = Path(a.out) if a.out else s.instance_dir / "backups"
    path = create_backup(ctx.root, ctx.id, out, s.instance_dir / "journal" / "operations.sqlite")
    _print({"backup": str(path), "size": path.stat().st_size})
    return 0


def cmd_project_restore_verify(s: Settings, a: argparse.Namespace) -> int:
    from assetstudio_storage.backup import verify_backup

    rep = verify_backup(Path(a.backup))
    _print(rep.__dict__)
    return 0 if rep.ok else 1


def _api(method: str, path: str, body: dict | None = None) -> object:
    """Production commands go through the running Studio (its command layer), never direct file mutations."""
    import os

    import httpx

    base = os.environ.get("STUDIO_URL", "http://127.0.0.1:8190").rstrip("/")
    try:
        r = httpx.request(method, f"{base}{path}", json=body, headers={"x-assetstudio": "1"}, timeout=60)
    except httpx.HTTPError as e:
        print(f"Studio not reachable at {base} ({type(e).__name__}); start it or set STUDIO_URL", file=sys.stderr)
        raise SystemExit(2) from e
    if r.status_code >= 400:
        print(r.text, file=sys.stderr)
        raise SystemExit(1)
    return r.json()


def cmd_jobs_list(_: Settings, a: argparse.Namespace) -> int:
    for j in _api("GET", f"/api/v2/projects/{a.project}/jobs")["jobs"]:  # type: ignore[index]
        print(f"{j['id']}  {j['alias']:8} {j['kind']:12} {j['counts']['items']:3} items  {j['next_action']:24} "
              f"{j['title']}{'  (in run ' + j['active_run'] + ')' if j['active_run'] else ''}")
    return 0


def cmd_batches_plan(_: Settings, a: argparse.Namespace) -> int:
    _print(_api("POST", f"/api/v2/projects/{a.project}/batches/{a.batch}:plan", {}))
    return 0


def cmd_batches_start(_: Settings, a: argparse.Namespace) -> int:
    import secrets

    _print(_api("POST", f"/api/v2/projects/{a.project}/batches/{a.batch}:start", {
        "plan_id": a.plan_id, "plan_sha256": a.plan_sha256, "idempotency_key": a.key or f"cli-{secrets.token_hex(8)}"}))
    return 0


def cmd_operations(_: Settings, a: argparse.Namespace) -> int:
    """Stage tasks (stk_...) through the v2 task API."""
    if a.sub == "inspect":
        _print(_api("GET", f"/api/v2/tasks/{a.task}"))
    else:
        _print(_api("POST", f"/api/v2/tasks/{a.task}:{a.sub}"))
    return 0


def cmd_storage_reindex(s: Settings, a: argparse.Namespace) -> int:
    s.ensure()
    ctx = Registry(s).get(a.project)
    _print(ctx.index.rebuild(ctx.store))
    return 0


def cmd_storage_verify(s: Settings, a: argparse.Namespace) -> int:
    """Every manifest/version/artifact parses and every referenced blob exists with the recorded size and hash."""
    import hashlib

    from assetstudio_core.domain import AssetManifest, AssetVersion
    from assetstudio_storage.project import manifest_key, version_key

    s.ensure()
    ctx = Registry(s).get(a.project)
    problems, checked = [], 0
    for asset_id in ctx.store.list_ids("manifests"):
        m, _ = ctx.store.get(manifest_key(asset_id), AssetManifest)
        for v in m.versions:
            rec, _ = ctx.store.get(version_key(asset_id, v.version_id), AssetVersion)
            for role, ref in rec.artifacts.items():
                checked += 1
                try:
                    with ctx.store.repo.open_blob(ref["sha256"]) as f:
                        digest = hashlib.file_digest(f, "sha256").hexdigest()
                    if digest != ref["sha256"]:
                        problems.append(f"{asset_id} {v.version_id} {role}: hash mismatch")
                except Exception as e:
                    problems.append(f"{asset_id} {v.version_id} {role}: {e}")
    _print({"artifacts_checked": checked, "problems": problems})
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="assetstudio")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("serve").set_defaults(fn=None)
    m = sub.add_parser("models").add_subparsers(dest="sub", required=True)
    mv = m.add_parser("verify")
    mv.add_argument("--full", action="store_true", help="sha256 every file (cached by path/size/mtime)")
    mv.set_defaults(fn=cmd_models_verify)
    pr = sub.add_parser("project").add_subparsers(dest="sub", required=True)
    pc = pr.add_parser("create")
    pc.add_argument("name")
    pc.add_argument("--root")
    pc.add_argument("--no-starter-qa", action="store_true")
    pc.set_defaults(fn=cmd_project_create)
    pr.add_parser("list").set_defaults(fn=cmd_project_list)
    prr = pr.add_parser("register")
    prr.add_argument("root")
    prr.set_defaults(fn=cmd_project_register)
    pb = pr.add_parser("backup", help="tar of the project root + a consistent journal copy (Studio stopped)")
    pb.add_argument("project")
    pb.add_argument("--out", help="directory (default: <instance>/backups)")
    pb.set_defaults(fn=cmd_project_backup)
    pv = pr.add_parser("restore-verify", help="verify a backup's inventory, hashes and journal (CPU only)")
    pv.add_argument("backup")
    pv.set_defaults(fn=cmd_project_restore_verify)
    st = sub.add_parser("storage").add_subparsers(dest="sub", required=True)
    for name, fn in (("reindex", cmd_storage_reindex), ("verify", cmd_storage_verify)):
        sp = st.add_parser(name)
        sp.add_argument("project")
        sp.set_defaults(fn=fn)
    jb = sub.add_parser("jobs").add_subparsers(dest="sub", required=True)
    jl = jb.add_parser("list")
    jl.add_argument("project")
    jl.set_defaults(fn=cmd_jobs_list)
    bt = sub.add_parser("batches").add_subparsers(dest="sub", required=True)
    bp = bt.add_parser("plan", help="frozen run plan (no inference)")
    bp.add_argument("project")
    bp.add_argument("batch")
    bp.set_defaults(fn=cmd_batches_plan)
    bs = bt.add_parser("start", help="start a planned run (idempotent per plan)")
    for arg in ("project", "batch", "plan_id", "plan_sha256"):
        bs.add_argument(arg)
    bs.add_argument("--key", help="idempotency key (default: random)")
    bs.set_defaults(fn=cmd_batches_start)
    op = sub.add_parser("operations").add_subparsers(dest="sub", required=True)
    for name in ("inspect", "retry", "cancel"):
        o = op.add_parser(name)
        o.add_argument("task")
        o.set_defaults(fn=cmd_operations)
    args = p.parse_args(argv)
    if args.cmd == "serve":
        from .main import run

        run()
        return 0
    return args.fn(Settings(), args)


if __name__ == "__main__":
    sys.exit(main())
