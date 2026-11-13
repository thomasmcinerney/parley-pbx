"""Loading and validating the TOML configuration file."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dialplan import Dialplan, DialplanError, Rule


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


_EXTENSION_RE = re.compile(r"^[0-9A-Za-z*#_.\-]+$")
_WILDCARD_BINDS = {"0.0.0.0", "::", ""}


@dataclass(frozen=True)
class User:
    extension: str
    name: str = ""


@dataclass(frozen=True)
class Config:
    bind: str = "127.0.0.1"
    port: int = 5060
    advertise: str = ""
    rtp_port_min: int = 10000
    rtp_port_max: int = 10099
    ring_timeout: float = 30.0
    users: dict[str, User] = field(default_factory=dict)
    dialplan: Dialplan = field(default_factory=Dialplan)

    @property
    def advertise_host(self) -> str:
        """The address phones are told to use for signalling and media."""
        return self.advertise or self.bind


def _table(data: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    table = data.get(key, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[{key}] must be a table")
    _reject_unknown(table, allowed, f"[{key}]")
    return table


def _reject_unknown(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key in {where}: {', '.join(unknown)}")


def _int(table: dict[str, Any], key: str, default: int, low: int, high: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be an integer")
    if not low <= value <= high:
        raise ConfigError(f"{where}.{key} must be between {low} and {high}")
    return value


def _str(table: dict[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return value


def parse_config(data: dict[str, Any]) -> Config:
    _reject_unknown(data, {"sip", "rtp", "calls", "users", "dialplan"}, "the top level")

    sip = _table(data, "sip", {"bind", "port", "advertise"})
    bind = _str(sip, "bind", "127.0.0.1", "sip")
    port = _int(sip, "port", 5060, 0, 65535, "sip")
    advertise = _str(sip, "advertise", "", "sip")
    if bind in _WILDCARD_BINDS and not advertise:
        raise ConfigError(
            "sip.advertise is required when sip.bind is a wildcard address, "
            "so phones know where to send signalling and audio"
        )

    rtp = _table(data, "rtp", {"port_min", "port_max"})
    rtp_min = _int(rtp, "port_min", 10000, 1024, 65535, "rtp")
    rtp_max = _int(rtp, "port_max", 10099, 1024, 65535, "rtp")
    if rtp_max - rtp_min < 1:
        raise ConfigError("rtp.port_max must be at least rtp.port_min + 1 (two ports per call)")

    calls = _table(data, "calls", {"ring_timeout"})
    ring_timeout = calls.get("ring_timeout", 30)
    if isinstance(ring_timeout, bool) or not isinstance(ring_timeout, (int, float)):
        raise ConfigError("calls.ring_timeout must be a number")
    if ring_timeout <= 0:
        raise ConfigError("calls.ring_timeout must be positive")

    users: dict[str, User] = {}
    entries = data.get("users", [])
    if not isinstance(entries, list):
        raise ConfigError("users must be an array of tables ([[users]])")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("each [[users]] entry must be a table")
        _reject_unknown(entry, {"extension", "name"}, "[[users]]")
        if "extension" not in entry:
            raise ConfigError("[[users]] entry is missing extension")
        raw = entry["extension"]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ConfigError("users.extension must be a string")
        extension = str(raw)
        if not _EXTENSION_RE.match(extension):
            raise ConfigError(f"invalid extension {extension!r}")
        if extension in users:
            raise ConfigError(f"duplicate extension {extension!r}")
        users[extension] = User(extension, _str(entry, "name", "", "users"))

    rules: list[Rule] = []
    entries = data.get("dialplan", [])
    if not isinstance(entries, list):
        raise ConfigError("dialplan must be an array of tables ([[dialplan]])")
    for index, entry in enumerate(entries, start=1):
        where = f"dialplan rule {index}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(entry, {"match", "action", "target", "status"}, where)
        if "match" not in entry:
            raise ConfigError(f"{where} is missing match")
        raw = entry["match"]
        pattern = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
        if not isinstance(pattern, str):
            raise ConfigError(f"{where}: match must be a string")
        action = _str(entry, "action", "dial", where)
        kwargs: dict[str, Any] = {}
        if action == "dial":
            if "status" in entry:
                raise ConfigError(f"{where}: status only applies to action = \"reject\"")
            if "target" in entry:
                raw = entry["target"]
                kwargs["target"] = (
                    str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
                )
                if not isinstance(kwargs["target"], str):
                    raise ConfigError(f"{where}: target must be a string")
                if "$" not in kwargs["target"] and kwargs["target"] not in users:
                    raise ConfigError(
                        f"{where}: target {kwargs['target']!r} is not a configured extension"
                    )
        elif action == "reject":
            if "target" in entry:
                raise ConfigError(f"{where}: target only applies to action = \"dial\"")
            if "status" in entry:
                kwargs["status"] = _int(entry, "status", 403, 400, 699, where)
        try:
            rules.append(Rule(pattern, action, **kwargs))
        except DialplanError as exc:
            raise ConfigError(f"{where}: {exc}") from exc

    return Config(
        bind=bind,
        port=port,
        advertise=advertise,
        rtp_port_min=rtp_min,
        rtp_port_max=rtp_max,
        ring_timeout=float(ring_timeout),
        users=users,
        dialplan=Dialplan(rules),
    )


def load_config(path: str | Path) -> Config:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(data)
