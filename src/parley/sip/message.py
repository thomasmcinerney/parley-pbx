"""Parsing and serialising SIP requests and responses (RFC 3261 section 7)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from .errors import SipParseError
from .uri import NameAddr, Via

COMPACT_FORMS = {
    "i": "Call-ID",
    "m": "Contact",
    "e": "Content-Encoding",
    "l": "Content-Length",
    "c": "Content-Type",
    "f": "From",
    "s": "Subject",
    "k": "Supported",
    "t": "To",
    "v": "Via",
}

_SPECIAL_CASE = {
    "call-id": "Call-ID",
    "cseq": "CSeq",
    "www-authenticate": "WWW-Authenticate",
    "mime-version": "MIME-Version",
}

# Headers whose values may be given as one comma-separated line.
_LIST_HEADERS = {"Via", "Contact", "Route", "Record-Route"}

_REQUIRED = ("Via", "From", "To", "Call-ID", "CSeq")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-.!%*_+`'~]+$")

REASONS = {
    100: "Trying",
    180: "Ringing",
    183: "Session Progress",
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    423: "Interval Too Brief",
    480: "Temporarily Unavailable",
    481: "Call/Transaction Does Not Exist",
    482: "Loop Detected",
    484: "Address Incomplete",
    486: "Busy Here",
    487: "Request Terminated",
    488: "Not Acceptable Here",
    500: "Server Internal Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    600: "Busy Everywhere",
    603: "Decline",
}


def canonical_name(name: str) -> str:
    """Expand compact header forms and normalise capitalisation."""
    lower = name.strip().lower()
    if lower in COMPACT_FORMS:
        return COMPACT_FORMS[lower]
    if lower in _SPECIAL_CASE:
        return _SPECIAL_CASE[lower]
    return "-".join(part.capitalize() for part in lower.split("-"))


def split_commas(value: str) -> list[str]:
    """Split on top-level commas, ignoring those inside quotes or ``<...>``."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    depth = 0
    i = 0
    while i < len(value):
        ch = value[i]
        if in_quotes:
            current.append(ch)
            if ch == "\\" and i + 1 < len(value):
                current.append(value[i + 1])
                i += 1
            elif ch == '"':
                in_quotes = False
        elif ch == '"':
            in_quotes = True
            current.append(ch)
        elif ch == "<":
            depth += 1
            current.append(ch)
        elif ch == ">":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current).strip())
    return [p for p in parts if p]


class Headers:
    """An ordered, case-insensitive multimap of SIP headers."""

    def __init__(self, items: Iterable[tuple[str, str]] = ()) -> None:
        self._items: list[tuple[str, str]] = []
        for name, value in items:
            self.add(name, value)

    def add(self, name: str, value: str) -> None:
        self._items.append((canonical_name(name), str(value)))

    def set(self, name: str, value: str) -> None:
        """Replace every occurrence of ``name`` with a single value."""
        name = canonical_name(name)
        for i, (existing, _) in enumerate(self._items):
            if existing == name:
                self._items[i] = (name, str(value))
                self._items = self._items[: i + 1] + [
                    item for item in self._items[i + 1 :] if item[0] != name
                ]
                return
        self._items.append((name, str(value)))

    def remove(self, name: str) -> None:
        name = canonical_name(name)
        self._items = [item for item in self._items if item[0] != name]

    def get(self, name: str, default: str | None = None) -> str | None:
        name = canonical_name(name)
        for existing, value in self._items:
            if existing == name:
                return value
        return default

    def get_all(self, name: str) -> list[str]:
        name = canonical_name(name)
        return [value for existing, value in self._items if existing == name]

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._items)

    def copy(self) -> Headers:
        return Headers(self._items)


class Message:
    def __init__(self, headers: Headers | None = None, body: bytes = b"") -> None:
        self.headers = headers if headers is not None else Headers()
        self.body = body

    def start_line(self) -> str:
        raise NotImplementedError

    def serialize(self) -> bytes:
        lines = [self.start_line()]
        for name, value in self.headers:
            if name != "Content-Length":
                lines.append(f"{name}: {value}")
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + self.body

    def _require(self, name: str) -> str:
        value = self.headers.get(name)
        if value is None:
            raise SipParseError(f"missing {name} header")
        return value

    @property
    def call_id(self) -> str:
        return self._require("Call-ID")

    @property
    def cseq(self) -> tuple[int, str]:
        parts = self._require("CSeq").split()
        if len(parts) != 2 or not parts[0].isdigit():
            raise SipParseError(f"invalid CSeq: {self.headers.get('CSeq')!r}")
        return int(parts[0]), parts[1].upper()

    @property
    def from_(self) -> NameAddr:
        return NameAddr.parse(self._require("From"))

    @property
    def to(self) -> NameAddr:
        return NameAddr.parse(self._require("To"))

    @property
    def top_via(self) -> Via:
        return Via.parse(self._require("Via"))


class Request(Message):
    def __init__(
        self, method: str, uri: str, headers: Headers | None = None, body: bytes = b""
    ) -> None:
        super().__init__(headers, body)
        self.method = method.upper()
        self.uri = uri

    def start_line(self) -> str:
        return f"{self.method} {self.uri} SIP/2.0"

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.uri}>"


class Response(Message):
    def __init__(
        self,
        status: int,
        reason: str | None = None,
        headers: Headers | None = None,
        body: bytes = b"",
    ) -> None:
        super().__init__(headers, body)
        self.status = status
        self.reason = reason if reason is not None else REASONS.get(status, "Unknown")

    def start_line(self) -> str:
        return f"SIP/2.0 {self.status} {self.reason}"

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.reason}>"


def parse_message(data: bytes) -> Request | Response:
    """Parse one datagram into a request or response.

    Raises :class:`SipParseError` for anything that is not a well-formed SIP
    message carrying the headers every message must have.
    """
    data = data.lstrip(b"\r\n")
    if not data:
        raise SipParseError("empty datagram")
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        raise SipParseError("no end of headers")
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SipParseError("headers are not valid UTF-8") from exc

    raw_lines = text.split("\r\n")
    lines: list[str] = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and len(lines) > 1:
            lines[-1] += " " + line.strip()  # folded header continuation
        else:
            lines.append(line)

    start = lines[0]
    message: Request | Response
    if start.startswith("SIP/2.0 "):
        parts = start.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit() or not 100 <= int(parts[1]) <= 699:
            raise SipParseError(f"invalid status line: {start!r}")
        message = Response(int(parts[1]), parts[2] if len(parts) > 2 else "")
    else:
        parts = start.split(" ")
        if len(parts) != 3 or parts[2] != "SIP/2.0" or not _TOKEN_RE.match(parts[0]):
            raise SipParseError(f"invalid request line: {start!r}")
        message = Request(parts[0], parts[1])

    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not _TOKEN_RE.match(name.strip()):
            raise SipParseError(f"invalid header line: {line!r}")
        name = canonical_name(name)
        value = value.strip()
        if name in _LIST_HEADERS:
            for item in split_commas(value):
                message.headers.add(name, item)
        else:
            message.headers.add(name, value)

    for name in _REQUIRED:
        if name not in message.headers:
            raise SipParseError(f"missing {name} header")

    length = message.headers.get("Content-Length")
    if length is not None:
        if not length.isdigit():
            raise SipParseError(f"invalid Content-Length: {length!r}")
        if int(length) > len(body):
            raise SipParseError("body shorter than Content-Length")
        body = body[: int(length)]
    message.body = body
    return message
