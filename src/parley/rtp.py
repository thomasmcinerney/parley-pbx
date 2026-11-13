"""The media relay: every call's audio flows through a pair of UDP ports here.

Each call gets two ports. The caller sends RTP to ``caller_port`` and the
callee sends RTP to ``callee_port``; whatever arrives on one is forwarded out
of the other to the opposite party.

A party's address is first taken from its SDP, then replaced by the source of
the first packet it actually sends ("symmetric RTP"), which is what makes
audio work for phones behind NAT. The cost is that whoever sends the first
packet to a port claims that side of the call, so the relay ports should only
be reachable from networks you trust.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol

Addr = tuple[str, int]


class RelayError(RuntimeError):
    """Raised when no relay ports are available."""


class MediaSession(Protocol):
    caller_port: int
    callee_port: int

    def set_caller(self, addr: Addr) -> None: ...

    def set_callee(self, addr: Addr) -> None: ...

    def close(self) -> None: ...


class MediaRelay(Protocol):
    def allocate(self) -> MediaSession: ...


class _Leg(asyncio.DatagramProtocol):
    """One of a session's two sockets, facing one party."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.port: int = sock.getsockname()[1]
        self.transport: asyncio.DatagramTransport | None = None
        self.peer: _Leg | None = None
        self.remote: Addr | None = None
        self.latched = False
        self.closed = False
        self.forwarded = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        if self.closed:
            transport.close()

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        addr = (addr[0], addr[1])
        if not self.latched:
            self.remote = addr
            self.latched = True
        elif addr != self.remote:
            return
        peer = self.peer
        if peer is None or peer.closed or peer.transport is None or peer.remote is None:
            return
        peer.transport.sendto(data, peer.remote)
        self.forwarded += 1

    def error_received(self, exc: Exception) -> None:
        pass  # e.g. ICMP port unreachable from a phone that has hung up

    def close(self) -> None:
        self.closed = True
        if self.transport is not None:
            self.transport.close()


class UdpMediaSession:
    def __init__(self, caller: _Leg, callee: _Leg) -> None:
        self._caller = caller
        self._callee = callee
        caller.peer = callee
        callee.peer = caller
        self.caller_port = caller.port
        self.callee_port = callee.port
        self.closed = False

    def set_caller(self, addr: Addr) -> None:
        if not self._caller.latched:
            self._caller.remote = addr

    def set_callee(self, addr: Addr) -> None:
        if not self._callee.latched:
            self._callee.remote = addr

    @property
    def packets_from_caller(self) -> int:
        return self._caller.forwarded

    @property
    def packets_from_callee(self) -> int:
        return self._callee.forwarded

    def close(self) -> None:
        self.closed = True
        self._caller.close()
        self._callee.close()


class UdpRelay:
    """Allocates relay sessions from a fixed UDP port range.

    Must be used from inside a running asyncio event loop.
    """

    def __init__(self, host: str, port_min: int, port_max: int) -> None:
        self.host = host
        self.port_min = port_min
        self.port_max = port_max
        self._next = port_min
        self._tasks: set[asyncio.Task[None]] = set()

    def _bind(self) -> socket.socket:
        for _ in range(self.port_max - self.port_min + 1):
            port = self._next
            self._next = port + 1 if port < self.port_max else self.port_min
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((self.host, port))
            except OSError:
                sock.close()
                continue
            sock.setblocking(False)
            return sock
        raise RelayError(f"no free RTP port in {self.port_min}-{self.port_max}")

    async def _attach(self, leg: _Leg) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: leg, sock=leg.sock)

    def allocate(self) -> UdpMediaSession:
        first = self._bind()
        try:
            second = self._bind()
        except RelayError:
            first.close()
            raise
        session = UdpMediaSession(_Leg(first), _Leg(second))
        loop = asyncio.get_running_loop()
        for leg in (session._caller, session._callee):
            task = loop.create_task(self._attach(leg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return session
