"""assetstudio-node command line: `run` and `check-config`."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from assetstudio_client import RunnerClient

from .agent import RunnerAgent, load_or_create_key
from .config import ConfigError, RunnerConfig, load_config
from .executor import FakeExecutor
from .hostlock import HostLock, HostLockBusy
from .push import PushListener
from .spool import Spool
from .state import RunnerState

log = logging.getLogger("assetstudio_node")


def _run(config: RunnerConfig) -> int:
    if not config.simulated:
        print("real engine adapters arrive in Phase 2; set simulated: true to run the fake executor", file=sys.stderr)
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
    client = RunnerClient(config.studio_url, private_key=load_or_create_key(config))
    agent = RunnerAgent(config, client=client, executor=FakeExecutor(), state=state,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assetstudio-node")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "check-config"):
        sub.add_parser(name).add_argument("--config", type=Path, required=True)
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
    return _run(config)


if __name__ == "__main__":
    raise SystemExit(main())
