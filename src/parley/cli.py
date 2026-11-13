"""Command-line interface: ``parley serve``, ``parley check``, ``parley route``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .config import Config, ConfigError, load_config
from .dialplan import Dial, Reject
from .server import Server


def describe_route(config: Config, number: str) -> str:
    """Explain in one line what dialling ``number`` would do."""
    decision = config.dialplan.resolve(number)
    if decision is None:
        return f"{number}: no matching rule (404 Not Found)"
    if isinstance(decision, Reject):
        return f"{number}: rejected with status {decision.status}"
    assert isinstance(decision, Dial)
    if decision.target not in config.users:
        return f"{number}: dial {decision.target}, which is not a configured extension (404)"
    name = config.users[decision.target].name
    return f"{number}: dial extension {decision.target}" + (f" ({name})" if name else "")


def _serve(config: Config) -> int:
    try:
        asyncio.run(Server(config).serve_forever())
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"parley: cannot start: {exc}", file=sys.stderr)
        return 1
    return 0


def _check(config: Config) -> int:
    print(f"SIP: {config.bind}:{config.port} (advertised as {config.advertise_host})")
    print(f"RTP ports: {config.rtp_port_min}-{config.rtp_port_max}")
    print(f"Extensions: {', '.join(config.users) or 'none'}")
    print(f"Dialplan rules: {len(config.dialplan.rules)}")
    print("Configuration OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parley", description="A small SIP PBX server.")
    parser.add_argument("--version", action="version", version=f"parley {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_config(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-c", "--config", default="pbx.toml", help="path to the TOML config file")

    serve = commands.add_parser("serve", help="run the PBX")
    add_config(serve)
    serve.add_argument("-v", "--verbose", action="store_true", help="log debugging detail")

    check = commands.add_parser("check", help="validate the config file and summarise it")
    add_config(check)

    route = commands.add_parser("route", help="show what dialling a number would do")
    add_config(route)
    route.add_argument("number", nargs="+")

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"parley: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        return _serve(config)
    if args.command == "check":
        return _check(config)
    for number in args.number:
        print(describe_route(config, number))
    return 0
