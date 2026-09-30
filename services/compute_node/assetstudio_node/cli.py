"""assetstudio-node command line: `run`, `check-config` and `models verify`."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from assetstudio_client import RunnerClient
from assetstudio_core.canonical import sha256_json
from assetstudio_core.safeyaml import load_yaml

from .admission import nvidia_smi
from .agent import RunnerAgent, load_or_create_key
from .config import ConfigError, RunnerConfig, load_config
from .engine_executor import EngineExecutor, build_engines
from .executor import Executor, FakeExecutor
from .hostlock import HostLock, HostLockBusy
from .inventory import resolve_devices
from .models import HashCache
from .push import PushListener
from .receipts import model_receipts
from .spool import Spool
from .state import RunnerState

log = logging.getLogger("assetstudio_node")


def _make_executor(config: RunnerConfig, state: RunnerState) -> Executor:
    if config.simulated:
        return FakeExecutor()
    engines = build_engines(config)
    resolve_devices(config, "runner", nvidia_smi())  # refuse to start on a device map nvidia-smi contradicts
    return EngineExecutor(config, state, engines)


def _run(config: RunnerConfig) -> int:
    if not config.simulated and config.models_root is None:
        print("invalid config: models_root is required to run real engines", file=sys.stderr)
        return 2
    token = None
    if config.registration_token_file:
        token = config.registration_token_file.read_text().strip()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with HostLock(config.host_lock):
            return _serve(config, token, stop)
    except HostLockBusy as e:
        print(str(e), file=sys.stderr)
        return 3


def _serve(config: RunnerConfig, token: str | None, stop: threading.Event) -> int:
    state = RunnerState(config.state_dir)
    try:
        executor = _make_executor(config, state)
    except ConfigError as e:
        state.close()
        print(f"invalid config: {e}", file=sys.stderr)
        return 2
    client = RunnerClient(config.studio_url, private_key=load_or_create_key(config))
    agent = RunnerAgent(config, client=client, executor=executor, state=state,
                        spool=Spool(config.state_dir / "spool"))
    listener = PushListener(config.push_listen, agent) if config.dispatch == "push" and config.push_listen else None
    try:
        agent.bootstrap(token)
        agent.open_session()
        if listener:
            listener.start()
        agent.run_forever(stop)
    finally:
        if listener:
            listener.stop()
        client.close()
        state.close()
    return 0


def _models_verify(config: RunnerConfig, catalog_path: Path) -> int:
    if config.models_root is None and not config.simulated:
        print("invalid config: models_root is required", file=sys.stderr)
        return 1
    try:
        catalog = load_yaml(catalog_path.read_bytes())
    except (OSError, ValueError) as e:
        print(f"cannot read catalog {catalog_path}: {e}", file=sys.stderr)
        return 1
    services = {e for sc in config.slots for e in sc.engines}
    cache = HashCache(config.state_dir / "hash-cache.json")
    receipts = model_receipts(catalog, sha256_json(catalog), config.models_root, services, cache,
                              simulated=config.simulated)
    for r in receipts:
        print(json.dumps(r.model_dump(), sort_keys=True))
    return 0 if all(r.status == "ok" for r in receipts) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assetstudio-node")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "check-config"):
        sub.add_parser(name).add_argument("--config", type=Path, required=True)
    verify = sub.add_parser("models").add_subparsers(dest="models_cmd", required=True).add_parser("verify")
    verify.add_argument("--config", type=Path, required=True)
    verify.add_argument("--catalog", type=Path, default=Path("config/models.lock.yaml"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"invalid config: {e}", file=sys.stderr)
        return 1
    if args.cmd == "check-config":
        print(f"ok: {len(config.slots)} slot(s), dispatch={config.dispatch}")
        return 0
    if args.cmd == "models":
        return _models_verify(config, args.catalog)
    return _run(config)


if __name__ == "__main__":
    raise SystemExit(main())
