"""Test doubles: a scripted SIP phone, a fake media relay and a fake clock."""

from __future__ import annotations

from parley.config import Config, parse_config
from parley.core import Core
from parley.sip.message import Headers, Message, Request, Response, parse_message
from parley.sip.uri import NameAddr

Addr = tuple[str, int]

PBX_HOST = "10.0.0.1"
PBX_PORT = 5060

CONFIG = {
    "sip": {"bind": PBX_HOST, "port": PBX_PORT},
    "calls": {"ring_timeout": 30},
    "users": [
        {"extension": "100", "name": "Alice"},
        {"extension": "101", "name": "Bob"},
        {"extension": "102"},
    ],
    "dialplan": [
        {"match": "0", "target": "100"},
        {"match": "1XX"},
        {"match": "9.", "action": "reject", "status": 403},
    ],
}


def make_config(**overrides: object) -> Config:
    return parse_config({**CONFIG, **overrides})


def sdp(host: str, port: int) -> bytes:
    return (
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {host}\r\n"
        "s=-\r\n"
        f"c=IN IP4 {host}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/AVP 0 8\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    ).encode()


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSession:
    def __init__(self, caller_port: int, callee_port: int) -> None:
        self.caller_port = caller_port
        self.callee_port = callee_port
        self.caller: Addr | None = None
        self.callee: Addr | None = None
        self.closed = False

    def set_caller(self, addr: Addr) -> None:
        self.caller = addr

    def set_callee(self, addr: Addr) -> None:
        self.callee = addr

    def close(self) -> None:
        self.closed = True


class FakeRelay:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def allocate(self) -> FakeSession:
        base = 20000 + 2 * len(self.sessions)
        session = FakeSession(base, base + 1)
        self.sessions.append(session)
        return session


class Phone:
    """Builds the SIP messages a phone would send. Holds no sockets."""

    def __init__(self, ext: str, addr: Addr, pbx: Addr = (PBX_HOST, PBX_PORT)) -> None:
        self.ext = ext
        self.addr = addr
        self.pbx = pbx
        self.tag = f"tag-{ext}"
        self._cseq = 0
        self._serial = 0

    def _next(self, prefix: str) -> str:
        self._serial += 1
        return f"{prefix}-{self.ext}-{self._serial}"

    def via(self, branch: str | None = None) -> str:
        branch = branch or self._next("z9hG4bK")
        return f"SIP/2.0/UDP {self.addr[0]}:{self.addr[1]};branch={branch};rport"

    @property
    def contact(self) -> str:
        return f"<sip:{self.ext}@{self.addr[0]}:{self.addr[1]}>"

    @property
    def address(self) -> str:
        return f"<sip:{self.ext}@{self.pbx[0]}>"

    def _request(
        self,
        method: str,
        uri: str,
        *,
        from_: str,
        to: str,
        call_id: str,
        cseq: int,
        via: str | None = None,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> Request:
        h = Headers()
        h.add("Via", via or self.via())
        h.add("Max-Forwards", "70")
        h.add("From", from_)
        h.add("To", to)
        h.add("Call-ID", call_id)
        h.add("CSeq", f"{cseq} {method}")
        for name, value in headers:
            h.add(name, value)
        if body:
            h.add("Content-Type", "application/sdp")
        return Request(method, uri, h, body)

    def register(self, expires: int | None = 3600, contact: str | None = None) -> Request:
        self._cseq += 1
        headers = [("Contact", contact or self.contact)]
        if expires is not None:
            headers.append(("Expires", str(expires)))
        return self._request(
            "REGISTER",
            f"sip:{self.pbx[0]}",
            from_=f"{self.address};tag={self.tag}",
            to=self.address,
            call_id=self._next("reg"),
            cseq=self._cseq,
            headers=tuple(headers),
        )

    def invite(self, number: str, media_port: int = 4000, body: bytes | None = None) -> Request:
        self._cseq += 1
        return self._request(
            "INVITE",
            f"sip:{number}@{self.pbx[0]}",
            from_=f"{self.address};tag={self.tag}",
            to=f"<sip:{number}@{self.pbx[0]}>",
            call_id=self._next("call"),
            cseq=self._cseq,
            headers=(("Contact", self.contact),),
            body=sdp(self.addr[0], media_port) if body is None else body,
        )

    def cancel(self, invite: Request) -> Request:
        return self._request(
            "CANCEL",
            invite.uri,
            from_=invite.headers.get("From"),
            to=invite.headers.get("To"),
            call_id=invite.call_id,
            cseq=invite.cseq[0],
            via=invite.headers.get("Via"),
        )

    def ack(self, invite: Request, response: Response) -> Request:
        return self._request(
            "ACK",
            invite.uri,
            from_=invite.headers.get("From"),
            to=response.headers.get("To"),
            call_id=invite.call_id,
            cseq=invite.cseq[0],
        )

    def bye(self, invite: Request, response: Response) -> Request:
        """Hang up a call this phone placed; ``response`` is the 200 it got."""
        self._cseq += 1
        return self._request(
            "BYE",
            invite.uri,
            from_=invite.headers.get("From"),
            to=response.headers.get("To"),
            call_id=invite.call_id,
            cseq=self._cseq,
        )

    def bye_incoming(self, invite: Request) -> Request:
        """Hang up a call this phone received and answered."""
        self._cseq += 1
        return self._request(
            "BYE",
            str(NameAddr.parse(invite.headers.get("Contact")).uri),
            from_=str(invite.to.with_tag(self.tag)),
            to=invite.headers.get("From"),
            call_id=invite.call_id,
            cseq=self._cseq,
        )

    def respond(
        self, request: Request, status: int, *, media_port: int | None = None
    ) -> Response:
        h = Headers()
        for via in request.headers.get_all("Via"):
            h.add("Via", via)
        h.add("From", request.headers.get("From"))
        to = request.to
        if to.tag is None and status != 100:
            to = to.with_tag(self.tag)
        h.add("To", str(to))
        h.add("Call-ID", request.call_id)
        h.add("CSeq", request.headers.get("CSeq"))
        body = b""
        if request.method == "INVITE" and status < 300:
            h.add("Contact", self.contact)
        if media_port is not None:
            body = sdp(self.addr[0], media_port)
            h.add("Content-Type", "application/sdp")
        return Response(status, headers=h, body=body)


class Harness:
    """A Core wired to fakes, recording everything it sends."""

    def __init__(self, config: Config | None = None) -> None:
        self.clock = FakeClock()
        self.relay = FakeRelay()
        self.sent: list[tuple[Message, Addr]] = []
        self.core = Core(
            config or make_config(),
            lambda data, addr: self.sent.append((parse_message(data), addr)),
            self.relay,
            (PBX_HOST, PBX_PORT),
            clock=self.clock,
        )

    def deliver(self, phone: Phone, message: Message) -> list[Message]:
        """Send a message from a phone and return what the PBX sends back to it."""
        self.core.datagram_received(message.serialize(), phone.addr)
        return self.take(phone)

    def take(self, phone: Phone) -> list[Message]:
        """Remove and return the messages sent to a phone so far."""
        mine = [message for message, addr in self.sent if addr == phone.addr]
        self.sent = [(message, addr) for message, addr in self.sent if addr != phone.addr]
        return mine

    def register(self, *phones: Phone) -> None:
        for phone in phones:
            (response,) = self.deliver(phone, phone.register())
            assert response.status == 200


def statuses(messages: list[Message]) -> list[int]:
    return [m.status for m in messages if isinstance(m, Response)]


def requests(messages: list[Message], method: str | None = None) -> list[Request]:
    return [
        m for m in messages if isinstance(m, Request) and (method is None or m.method == method)
    ]
