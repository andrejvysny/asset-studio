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
    st = sub.add_parser("storage").add_subparsers(dest="sub", required=True)
    for name, fn in (("reindex", cmd_storage_reindex), ("verify", cmd_storage_verify)):
        sp = st.add_parser(name)
        sp.add_argument("project")
        sp.set_defaults(fn=fn)
    args = p.parse_args(argv)
    if args.cmd == "serve":
        from .main import run

        run()
        return 0
    return args.fn(Settings(), args)


if __name__ == "__main__":
    sys.exit(main())
