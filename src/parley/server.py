"""The asyncio shell around the core: UDP sockets and the housekeeping timer."""

from __future__ import annotations

import asyncio
import logging

from .config import Config
from .core import Core
from .rtp import UdpRelay

log = logging.getLogger(__name__)

Addr = tuple[str, int]


class _SipProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: Server) -> None:
        self._server = server

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        core = self._server.core
        if core is None:
            return
        try:
            core.datagram_received(data, addr)
        except Exception:
            # One bad packet must never take the whole PBX down.
            log.exception("error handling datagram from %s:%s", addr[0], addr[1])

    def error_received(self, exc: Exception) -> None:
        log.debug("SIP socket error: %s", exc)


class Server:
    def __init__(self, config: Config, tick_interval: float = 0.5) -> None:
        self.config = config
        self.core: Core | None = None
        self.relay: UdpRelay | None = None
        self._tick_interval = tick_interval
        self._transport: asyncio.DatagramTransport | None = None
        self._ticker: asyncio.Task[None] | None = None

    @property
    def sip_port(self) -> int:
        """The UDP port actually bound (useful when the config asks for port 0)."""
        if self._transport is None:
            raise RuntimeError("server is not running")
        return self._transport.get_extra_info("sockname")[1]

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SipProtocol(self), local_addr=(self.config.bind, self.config.port)
        )
        self.relay = UdpRelay(self.config.bind, self.config.rtp_port_min, self.config.rtp_port_max)
        self.core = Core(
            self.config,
            self._transport.sendto,
            self.relay,
            (self.config.advertise_host, self.sip_port),
        )
        self._ticker = loop.create_task(self._tick())
        log.info(
            "listening on %s:%s (advertised as %s), %d extensions, %d dialplan rules",
            self.config.bind,
            self.sip_port,
            self.config.advertise_host,
            len(self.config.users),
            len(self.config.dialplan.rules),
        )

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval)
            try:
                if self.core is not None:
                    self.core.tick()
            except Exception:
                log.exception("error during housekeeping")

    async def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass
            self._ticker = None
        if self.core is not None:
            for call in self.core.calls:
                call.media.close()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()
