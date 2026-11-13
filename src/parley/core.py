"""The PBX core: registration and two-party calls, with no I/O of its own.

The core is a back-to-back user agent. A call from A to B is two separate SIP
dialogs, A <-> PBX and PBX <-> B, each with its own Call-ID and tags, and the
audio of both is anchored on the media relay.

All input arrives through :meth:`Core.datagram_received` and :meth:`Core.tick`;
all output leaves through the ``send`` callable. Time comes from an injectable
clock. That makes every behaviour here testable without sockets or sleeping.
"""

from __future__ import annotations

import enum
import logging
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import __version__
from .config import Config
from .dialplan import Dial, Reject
from .registrar import Addr, Registrar
from .rtp import MediaRelay, MediaSession, RelayError
from .sip.errors import SdpError, SipParseError
from .sip.message import Headers, Request, Response, parse_message
from .sip.sdp import rewrite_audio
from .sip.uri import NameAddr, SipUri, Via

log = logging.getLogger(__name__)

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, REGISTER"
USER_AGENT = f"Parley/{__version__}"
BRANCH_MAGIC = "z9hG4bK"

MIN_EXPIRES = 60
MAX_EXPIRES = 3600
DEFAULT_EXPIRES = 3600

# How long a completed transaction's response is kept for retransmitted
# requests (RFC 3261 timer J: 64*T1), and how long an unfinished one is kept.
TRANSACTION_LIFETIME = 32.0
PENDING_TRANSACTION_LIFETIME = 300.0
# How long to wait for the callee's final response after cancelling its leg.
CANCEL_LIFETIME = 64.0


class CallState(enum.Enum):
    RINGING = "ringing"
    ANSWERED = "answered"
    CANCELLING = "cancelling"
    ENDED = "ended"


@dataclass
class _Transaction:
    data: bytes | None
    dest: Addr | None
    final: bool
    updated: float


TransactionKey = tuple[str, str, "int | None", str]


@dataclass
class Call:
    state: CallState
    created: float
    media: MediaSession
    caller: str
    dialled: str
    target: str

    # Leg A: the caller's dialog, where the PBX is the server.
    invite: Request
    a_addr: Addr
    a_call_id: str
    a_remote: NameAddr  # the caller's From, with its tag
    a_local: NameAddr  # the caller's To, with our tag
    a_target: str  # Request-URI for requests we send to the caller
    a_cseq: int

    # Leg B: the callee's dialog, where the PBX is the client.
    b_addr: Addr
    b_call_id: str
    b_local: NameAddr  # our From, with our tag
    b_remote: NameAddr  # the callee's To; gains a tag once it responds
    b_invite_uri: str
    b_target: str  # Request-URI for in-dialog requests to the callee
    b_branch: str
    b_cseq: int


def _token(nbytes: int = 8) -> str:
    return secrets.token_hex(nbytes)


class Core:
    def __init__(
        self,
        config: Config,
        send: Callable[[bytes, Addr], None],
        relay: MediaRelay,
        advertise: Addr,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._send = send
        self.relay = relay
        self.host, self.port = advertise
        self.clock = clock
        self.registrar = Registrar(clock)
        self._transactions: dict[TransactionKey, _Transaction] = {}
        self._calls_a: dict[str, Call] = {}
        self._calls_b: dict[str, Call] = {}

    # ------------------------------------------------------------------ input

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        addr = (addr[0], addr[1])
        try:
            message = parse_message(data)
        except SipParseError as exc:
            if data.strip():
                log.debug("dropping malformed datagram from %s:%s: %s", *addr, exc)
            return
        try:
            if isinstance(message, Request):
                self._on_request(message, addr)
            else:
                self._on_response(message)
        except SipParseError as exc:
            log.debug("bad message from %s:%s: %s", *addr, exc)
            if isinstance(message, Request) and message.method != "ACK":
                try:
                    self._reply(message, addr, 400)
                except SipParseError:
                    pass

    def tick(self) -> None:
        """Run time-based housekeeping. Call this about once a second."""
        now = self.clock()
        self.registrar.purge()

        for call in list(self._calls_b.values()):
            age = now - call.created
            if call.state is CallState.RINGING and age >= self.config.ring_timeout:
                log.info("call %s -> %s: no answer", call.caller, call.target)
                self._abandon(call, 480)
            elif call.state is CallState.CANCELLING and age >= CANCEL_LIFETIME:
                self._forget(call)

        for key, txn in list(self._transactions.items()):
            lifetime = TRANSACTION_LIFETIME if txn.final else PENDING_TRANSACTION_LIFETIME
            if now - txn.updated >= lifetime:
                del self._transactions[key]

    @property
    def calls(self) -> list[Call]:
        """Calls that are ringing or answered."""
        return list(self._calls_a.values())

    # --------------------------------------------------------------- requests

    @staticmethod
    def _transaction_key(request: Request) -> TransactionKey | None:
        via = request.top_via
        branch = via.branch
        if branch is None or not branch.startswith(BRANCH_MAGIC):
            return None
        return (branch, via.host.lower(), via.port, request.method)

    def _on_request(self, request: Request, addr: Addr) -> None:
        if request.method != "ACK":
            key = self._transaction_key(request)
            if key is not None:
                existing = self._transactions.get(key)
                if existing is not None:
                    # A retransmission: answer it the same way as before.
                    if existing.data is not None and existing.dest is not None:
                        self._send(existing.data, existing.dest)
                    return
                self._transactions[key] = _Transaction(None, None, False, self.clock())

        handler = {
            "REGISTER": self._on_register,
            "INVITE": self._on_invite,
            "ACK": self._on_ack,
            "CANCEL": self._on_cancel,
            "BYE": self._on_bye,
            "OPTIONS": self._on_options,
        }.get(request.method)
        if handler is None:
            self._reply(request, addr, 405, headers=[("Allow", ALLOW)])
            return
        handler(request, addr)

    def _reply(
        self,
        request: Request,
        addr: Addr,
        status: int,
        *,
        to_tag: str | None = None,
        headers: Iterable[tuple[str, str]] = (),
        body: bytes = b"",
    ) -> None:
        """Send a response to ``request``, which arrived from ``addr``."""
        vias = request.headers.get_all("Via")
        top = Via.parse(vias[0])
        use_source_port = "rport" in top.params
        top.params["received"] = addr[0]
        if use_source_port:
            top.params["rport"] = str(addr[1])

        out = Headers()
        out.add("Via", str(top))
        for via in vias[1:]:
            out.add("Via", via)
        out.add("From", request.headers.get("From", ""))
        try:
            to = request.to
            if to.tag is None and status != 100:
                to = to.with_tag(to_tag or _token())
            out.add("To", str(to))
        except SipParseError:
            out.add("To", request.headers.get("To", ""))  # echo what we could not parse
        out.add("Call-ID", request.call_id)
        out.add("CSeq", request.headers.get("CSeq", ""))
        out.add("Server", USER_AGENT)
        for name, value in headers:
            out.add(name, value)
        if body:
            out.add("Content-Type", "application/sdp")

        data = Response(status, headers=out, body=body).serialize()
        dest = addr if use_source_port else (addr[0], top.port or 5060)
        self._send(data, dest)

        key = self._transaction_key(request)
        if key is not None and request.method != "ACK":
            self._transactions[key] = _Transaction(data, dest, status >= 200, self.clock())

    def _on_options(self, request: Request, addr: Addr) -> None:
        self._reply(request, addr, 200, headers=[("Allow", ALLOW), ("Accept", "application/sdp")])

    def _on_register(self, request: Request, addr: Addr) -> None:
        aor = request.to.uri.user
        if aor is None or aor not in self.config.users:
            self._reply(request, addr, 404)
            return

        expires_header = request.headers.get("Expires")
        if expires_header is None:
            default_expires = DEFAULT_EXPIRES
        elif expires_header.strip().isdigit():
            default_expires = int(expires_header)
        else:
            self._reply(request, addr, 400)
            return

        contacts = request.headers.get_all("Contact")
        if any(contact.strip() == "*" for contact in contacts):
            if len(contacts) != 1 or default_expires != 0:
                self._reply(request, addr, 400)
                return
            self.registrar.unregister_all(aor)
        else:
            updates: list[tuple[SipUri, int]] = []
            for contact in contacts:
                parsed = NameAddr.parse(contact)
                expires_param = parsed.params.get("expires")
                if expires_param is None:
                    expires = default_expires
                elif expires_param.isdigit():
                    expires = int(expires_param)
                else:
                    self._reply(request, addr, 400)
                    return
                updates.append((parsed.uri, expires))
            # Validate everything before changing anything.
            if any(0 < expires < MIN_EXPIRES for _, expires in updates):
                self._reply(request, addr, 423, headers=[("Min-Expires", str(MIN_EXPIRES))])
                return
            for uri, expires in updates:
                if expires == 0:
                    self.registrar.unregister(aor, uri)
                else:
                    self.registrar.register(aor, uri, addr, min(expires, MAX_EXPIRES))
                    log.info("registered %s at %s:%s", aor, *addr)

        current = [
            ("Contact", f"<{binding.contact}>;expires={self.registrar.remaining(binding)}")
            for binding in self.registrar.bindings(aor)
        ]
        self._reply(request, addr, 200, headers=current)

    def _on_invite(self, request: Request, addr: Addr) -> None:
        if request.to.tag is not None:
            # A re-INVITE inside an existing call is refused.
            call = self._dialog_a(request) or self._dialog_b(request)
            self._reply(request, addr, 488 if call is not None else 481)
            return
        if request.call_id in self._calls_a:
            self._reply(request, addr, 482)
            return

        caller = request.from_.uri.user
        if caller is None or caller not in self.config.users:
            self._reply(request, addr, 403)
            return
        dialled = SipUri.parse(request.uri).user
        if not dialled:
            self._reply(request, addr, 484)
            return

        self._reply(request, addr, 100)

        decision = self.config.dialplan.resolve(dialled)
        if decision is None:
            self._reply(request, addr, 404)
            return
        if isinstance(decision, Reject):
            self._reply(request, addr, decision.status)
            return
        assert isinstance(decision, Dial)
        target = decision.target
        if target not in self.config.users:
            self._reply(request, addr, 404)
            return
        bindings = self.registrar.bindings(target)
        if not bindings:
            self._reply(request, addr, 480)
            return
        binding = bindings[0]

        if not request.body:
            # An INVITE must carry an SDP offer.
            self._reply(request, addr, 488)
            return
        try:
            media = self.relay.allocate()
        except RelayError as exc:
            log.warning("cannot place call: %s", exc)
            self._reply(request, addr, 503)
            return
        try:
            offer, caller_audio = rewrite_audio(request.body, self.host, media.callee_port)
        except SdpError as exc:
            log.debug("rejecting offer from %s: %s", caller, exc)
            media.close()
            self._reply(request, addr, 488)
            return
        if caller_audio.usable:
            media.set_caller((caller_audio.host, caller_audio.port))

        a_tag = _token()
        a_contacts = request.headers.get_all("Contact")
        a_target = str(NameAddr.parse(a_contacts[0]).uri) if a_contacts else str(request.from_.uri)
        b_uri = str(binding.contact)
        call = Call(
            state=CallState.RINGING,
            created=self.clock(),
            media=media,
            caller=caller,
            dialled=dialled,
            target=target,
            invite=request,
            a_addr=addr,
            a_call_id=request.call_id,
            a_remote=request.from_,
            a_local=request.to.with_tag(a_tag),
            a_target=a_target,
            a_cseq=0,
            b_addr=binding.source,
            b_call_id=f"{_token(12)}@{self.host}",
            b_local=NameAddr(
                SipUri(self.host, caller),
                self.config.users[caller].name or None,
                {"tag": _token()},
            ),
            b_remote=NameAddr(SipUri(self.host, target)),
            b_invite_uri=b_uri,
            b_target=b_uri,
            b_branch=BRANCH_MAGIC + _token(),
            b_cseq=1,
        )
        self._calls_a[call.a_call_id] = call
        self._calls_b[call.b_call_id] = call
        log.info("call %s -> %s (dialled %s)", caller, target, dialled)
        self._send_b(call, "INVITE", call.b_invite_uri, call.b_branch, 1, body=offer)

    def _on_ack(self, request: Request, addr: Addr) -> None:
        # ACKs for our own final responses need no action: each leg is
        # acknowledged independently, and nothing is forwarded.
        return

    def _on_cancel(self, request: Request, addr: Addr) -> None:
        call = self._calls_a.get(request.call_id)
        if call is None or request.from_.tag != call.a_remote.tag:
            self._reply(request, addr, 481)
            return
        self._reply(request, addr, 200, to_tag=call.a_local.tag)
        if call.state is CallState.RINGING:
            log.info("call %s -> %s: cancelled by caller", call.caller, call.target)
            self._abandon(call, 487)

    def _on_bye(self, request: Request, addr: Addr) -> None:
        call = self._dialog_a(request)
        if call is not None:
            self._reply(request, addr, 200)
            if call.state is CallState.RINGING:
                self._abandon(call, 487)
            else:
                log.info("call %s -> %s: caller hung up", call.caller, call.target)
                self._bye_b(call)
                self._forget(call)
            return
        call = self._dialog_b(request)
        if call is not None and call.state is CallState.ANSWERED:
            self._reply(request, addr, 200)
            log.info("call %s -> %s: callee hung up", call.caller, call.target)
            self._bye_a(call)
            self._forget(call)
            return
        self._reply(request, addr, 481)

    # -------------------------------------------------------------- responses

    def _on_response(self, response: Response) -> None:
        call = self._calls_b.get(response.call_id)
        if call is None:
            return
        number, method = response.cseq
        if method != "INVITE" or number != 1 or response.status == 100:
            return  # responses to our BYE and CANCEL need no action
        if response.from_.tag != call.b_local.tag:
            return
        status = response.status

        if call.state is CallState.RINGING:
            if status < 200:
                self._on_progress(call, response)
            elif status < 300:
                self._on_answer(call, response)
            else:
                self._ack_b(call, response)
                log.info("call %s -> %s: callee answered %s", call.caller, call.target, status)
                self._reply(call.invite, call.a_addr, self._failure_status(status))
                self._forget(call)
        elif call.state is CallState.CANCELLING:
            if status >= 200:
                self._learn_b(call, response)
                self._ack_b(call, response)
                if status < 300:
                    # The callee answered before our CANCEL reached it.
                    self._bye_b(call)
                self._forget(call)
        elif call.state is CallState.ANSWERED and 200 <= status < 300:
            self._ack_b(call, response)  # our ACK was lost; send it again

    @staticmethod
    def _failure_status(status: int) -> int:
        """Translate the callee's failure into what the caller should see."""
        if status < 400:
            return 480  # redirects are not followed
        if status in (401, 407):
            return 503  # the PBX cannot authenticate to a phone
        return status

    def _learn_b(self, call: Call, response: Response) -> None:
        call.b_remote = response.to
        contacts = response.headers.get_all("Contact")
        if contacts:
            call.b_target = str(NameAddr.parse(contacts[0]).uri)

    def _answer_body(self, call: Call, response: Response) -> bytes:
        """Rewrite the callee's SDP for the caller and learn the callee's address."""
        answer, callee_audio = rewrite_audio(response.body, self.host, call.media.caller_port)
        if callee_audio.usable:
            call.media.set_callee((callee_audio.host, callee_audio.port))
        return answer

    def _contact(self, user: str) -> tuple[str, str]:
        return ("Contact", f"<sip:{user}@{self.host}:{self.port}>")

    def _on_progress(self, call: Call, response: Response) -> None:
        body = b""
        if response.body:
            try:
                body = self._answer_body(call, response)  # early media
            except SdpError:
                body = b""
        self._reply(
            call.invite,
            call.a_addr,
            response.status,
            to_tag=call.a_local.tag,
            headers=[self._contact(call.dialled)],
            body=body,
        )

    def _on_answer(self, call: Call, response: Response) -> None:
        self._learn_b(call, response)
        self._ack_b(call, response)
        try:
            body = self._answer_body(call, response)
        except SdpError as exc:
            log.warning("call %s -> %s: unusable answer: %s", call.caller, call.target, exc)
            self._bye_b(call)
            self._reply(call.invite, call.a_addr, 502)
            self._forget(call)
            return
        call.state = CallState.ANSWERED
        log.info("call %s -> %s: answered", call.caller, call.target)
        self._reply(
            call.invite,
            call.a_addr,
            200,
            to_tag=call.a_local.tag,
            headers=[self._contact(call.dialled), ("Allow", ALLOW)],
            body=body,
        )

    # ------------------------------------------------------- outgoing requests

    def _via(self, branch: str) -> str:
        return f"SIP/2.0/UDP {self.host}:{self.port};branch={branch};rport"

    def _send_b(
        self, call: Call, method: str, uri: str, branch: str, cseq: int, body: bytes = b""
    ) -> None:
        headers = Headers()
        headers.add("Via", self._via(branch))
        headers.add("Max-Forwards", "70")
        headers.add("From", str(call.b_local))
        headers.add("To", str(call.b_remote))
        headers.add("Call-ID", call.b_call_id)
        headers.add("CSeq", f"{cseq} {method}")
        headers.add(*self._contact(call.caller))
        headers.add("User-Agent", USER_AGENT)
        if method == "INVITE":
            headers.add("Allow", ALLOW)
        if body:
            headers.add("Content-Type", "application/sdp")
        self._send(Request(method, uri, headers, body).serialize(), call.b_addr)

    def _ack_b(self, call: Call, response: Response) -> None:
        if response.status < 300:
            # The ACK for a 2xx is a new transaction sent to the callee's Contact.
            self._send_b(call, "ACK", call.b_target, BRANCH_MAGIC + _token(), 1)
        else:
            # The ACK for a failure belongs to the INVITE transaction.
            saved = call.b_remote
            call.b_remote = response.to
            self._send_b(call, "ACK", call.b_invite_uri, call.b_branch, 1)
            call.b_remote = saved

    def _bye_b(self, call: Call) -> None:
        call.b_cseq += 1
        self._send_b(call, "BYE", call.b_target, BRANCH_MAGIC + _token(), call.b_cseq)

    def _bye_a(self, call: Call) -> None:
        call.a_cseq += 1
        headers = Headers()
        headers.add("Via", self._via(BRANCH_MAGIC + _token()))
        headers.add("Max-Forwards", "70")
        headers.add("From", str(call.a_local))
        headers.add("To", str(call.a_remote))
        headers.add("Call-ID", call.a_call_id)
        headers.add("CSeq", f"{call.a_cseq} BYE")
        headers.add("User-Agent", USER_AGENT)
        self._send(Request("BYE", call.a_target, headers).serialize(), call.a_addr)

    # ------------------------------------------------------------ call teardown

    def _dialog_a(self, request: Request) -> Call | None:
        call = self._calls_a.get(request.call_id)
        if (
            call is not None
            and request.from_.tag == call.a_remote.tag
            and request.to.tag == call.a_local.tag
        ):
            return call
        return None

    def _dialog_b(self, request: Request) -> Call | None:
        call = self._calls_b.get(request.call_id)
        if (
            call is not None
            and call.b_remote.tag is not None
            and request.from_.tag == call.b_remote.tag
            and request.to.tag == call.b_local.tag
        ):
            return call
        return None

    def _abandon(self, call: Call, status: int) -> None:
        """Give up on a ringing call: cancel the callee and fail the caller."""
        self._send_b(call, "CANCEL", call.b_invite_uri, call.b_branch, 1)
        self._reply(call.invite, call.a_addr, status, to_tag=call.a_local.tag)
        call.media.close()
        self._calls_a.pop(call.a_call_id, None)
        # Stay findable by the callee's leg until it confirms the cancellation.
        call.state = CallState.CANCELLING
        call.created = self.clock()

    def _forget(self, call: Call) -> None:
        call.state = CallState.ENDED
        call.media.close()
        self._calls_a.pop(call.a_call_id, None)
        self._calls_b.pop(call.b_call_id, None)
