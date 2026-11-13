"""SIP URIs and the header values built around them (name-addr, Via)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import SipParseError

Params = dict[str, "str | None"]

_URI_RE = re.compile(
    r"^(?P<scheme>sips?):"
    r"(?:(?P<user>[^@]*)@)?"
    r"(?P<host>\[[^\]]+\]|[^:;?\s]+)"
    r"(?::(?P<port>\d+))?"
    r"(?P<params>(?:;[^?]*)?)"
    r"(?:\?(?P<headers>.*))?$",
    re.IGNORECASE,
)

_VIA_RE = re.compile(
    r"^SIP\s*/\s*2\.0\s*/\s*(?P<transport>\w+)\s+"
    r"(?P<host>\[[^\]]+\]|[^:;\s]+)"
    r"(?:\s*:\s*(?P<port>\d+))?"
    r"\s*(?P<params>(?:;.*)?)$",
    re.IGNORECASE,
)


def parse_params(text: str) -> Params:
    """Parse ``;key=value;flag`` into a dict. Flags map to ``None``."""
    params: Params = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        key, eq, value = part.partition("=")
        params[key.strip().lower()] = value.strip() if eq else None
    return params


def format_params(params: Params) -> str:
    return "".join(f";{k}" if v is None else f";{k}={v}" for k, v in params.items())


def _parse_port(text: str | None) -> int | None:
    if text is None:
        return None
    port = int(text)
    if not 0 < port < 65536:
        raise SipParseError(f"port out of range: {text}")
    return port


@dataclass
class SipUri:
    host: str
    user: str | None = None
    port: int | None = None
    scheme: str = "sip"
    params: Params = field(default_factory=dict)
    headers: str | None = None

    @classmethod
    def parse(cls, text: str) -> SipUri:
        match = _URI_RE.match(text.strip())
        if match is None:
            raise SipParseError(f"invalid SIP URI: {text!r}")
        return cls(
            host=match["host"],
            user=match["user"] or None,
            port=_parse_port(match["port"]),
            scheme=match["scheme"].lower(),
            params=parse_params(match["params"]),
            headers=match["headers"],
        )

    def __str__(self) -> str:
        out = f"{self.scheme}:"
        if self.user is not None:
            out += f"{self.user}@"
        out += self.host
        if self.port is not None:
            out += f":{self.port}"
        out += format_params(self.params)
        if self.headers:
            out += f"?{self.headers}"
        return out


@dataclass
class NameAddr:
    """A From, To or Contact value: optional display name, URI and parameters."""

    uri: SipUri
    display: str | None = None
    params: Params = field(default_factory=dict)

    @property
    def tag(self) -> str | None:
        return self.params.get("tag")

    def with_tag(self, tag: str) -> NameAddr:
        return NameAddr(self.uri, self.display, {**self.params, "tag": tag})

    @classmethod
    def parse(cls, value: str) -> NameAddr:
        value = value.strip()
        display: str | None = None
        if value.startswith('"'):
            chars: list[str] = []
            i = 1
            while i < len(value):
                ch = value[i]
                if ch == "\\" and i + 1 < len(value):
                    chars.append(value[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    break
                chars.append(ch)
                i += 1
            else:
                raise SipParseError(f"unterminated display name: {value!r}")
            display = "".join(chars)
            value = value[i + 1 :].lstrip()
            if not value.startswith("<"):
                raise SipParseError(f"expected '<' after display name: {value!r}")

        lt = value.find("<")
        if lt >= 0:
            gt = value.find(">", lt)
            if gt < 0:
                raise SipParseError(f"missing '>': {value!r}")
            if display is None:
                display = value[:lt].strip() or None
            uri = SipUri.parse(value[lt + 1 : gt])
            params = parse_params(value[gt + 1 :])
        else:
            # Without angle brackets, parameters belong to the header, not the URI.
            uri_text, _, rest = value.partition(";")
            uri = SipUri.parse(uri_text)
            params = parse_params(rest)
        return cls(uri, display, params)

    def __str__(self) -> str:
        out = ""
        if self.display:
            escaped = self.display.replace("\\", "\\\\").replace('"', '\\"')
            out = f'"{escaped}" '
        return f"{out}<{self.uri}>{format_params(self.params)}"


@dataclass
class Via:
    host: str
    port: int | None = None
    transport: str = "UDP"
    params: Params = field(default_factory=dict)

    @property
    def branch(self) -> str | None:
        return self.params.get("branch")

    @classmethod
    def parse(cls, value: str) -> Via:
        match = _VIA_RE.match(value.strip())
        if match is None:
            raise SipParseError(f"invalid Via: {value!r}")
        return cls(
            host=match["host"],
            port=_parse_port(match["port"]),
            transport=match["transport"].upper(),
            params=parse_params(match["params"]),
        )

    def __str__(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        return f"SIP/2.0/{self.transport} {self.host}{port}{format_params(self.params)}"
