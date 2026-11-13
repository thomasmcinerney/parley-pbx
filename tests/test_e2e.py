"""A full call over real UDP sockets on the loopback interface."""

import asyncio

from parley.config import parse_config
from parley.server import Server
from parley.sip import Response, parse_message

from helpers import CONFIG, Phone

HOST = "127.0.0.1"
TIMEOUT = 5


class Endpoint(asyncio.DatagramProtocol):
    """A UDP socket whose received datagrams land in a queue."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    @classmethod
    async def open(cls) -> "Endpoint":
        loop = asyncio.get_running_loop()
        _, endpoint = await loop.create_datagram_endpoint(cls, local_addr=(HOST, 0))
        return endpoint

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data, addr) -> None:
        self.inbox.put_nowait((data, (addr[0], addr[1])))

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    def send(self, data: bytes, addr: tuple[str, int]) -> None:
        self.transport.sendto(data, addr)

    async def receive(self) -> bytes:
        data, _ = await asyncio.wait_for(self.inbox.get(), TIMEOUT)
        return data

    async def receive_sip(self):
        return parse_message(await self.receive())

    def close(self) -> None:
        self.transport.close()


def audio_port(body: bytes) -> int:
    for line in body.decode().splitlines():
        if line.startswith("m=audio "):
            return int(line.split()[1])
    raise AssertionError("no audio line")


async def scenario() -> None:
    config = parse_config(
        {
            **CONFIG,
            "sip": {"bind": HOST, "port": 0},
            "rtp": {"port_min": 42000, "port_max": 42099},
        }
    )
    server = Server(config)
    await server.start()
    alice_sip, alice_rtp, bob_sip, bob_rtp = [await Endpoint.open() for _ in range(4)]
    try:
        pbx = (HOST, server.sip_port)
        alice = Phone("100", (HOST, alice_sip.port), pbx)
        bob = Phone("101", (HOST, bob_sip.port), pbx)

        for phone, sock in ((alice, alice_sip), (bob, bob_sip)):
            sock.send(phone.register().serialize(), pbx)
            assert (await sock.receive_sip()).status == 200

        invite = alice.invite("101", media_port=alice_rtp.port)
        alice_sip.send(invite.serialize(), pbx)
        assert (await alice_sip.receive_sip()).status == 100

        b_invite = await bob_sip.receive_sip()
        assert b_invite.method == "INVITE"
        bob_sip.send(bob.respond(b_invite, 180).serialize(), pbx)
        assert (await alice_sip.receive_sip()).status == 180

        bob_sip.send(bob.respond(b_invite, 200, media_port=bob_rtp.port).serialize(), pbx)
        assert (await bob_sip.receive_sip()).method == "ACK"
        ok = await alice_sip.receive_sip()
        assert isinstance(ok, Response) and ok.status == 200
        alice_sip.send(alice.ack(invite, ok).serialize(), pbx)

        # Audio flows through the relay in both directions.
        relay_for_alice = (HOST, audio_port(ok.body))
        relay_for_bob = (HOST, audio_port(b_invite.body))
        assert relay_for_alice[1] != relay_for_bob[1]
        for n in range(3):
            alice_rtp.send(b"from-alice-%d" % n, relay_for_alice)
            assert await bob_rtp.receive() == b"from-alice-%d" % n
            bob_rtp.send(b"from-bob-%d" % n, relay_for_bob)
            assert await alice_rtp.receive() == b"from-bob-%d" % n

        (call,) = server.core.calls
        assert call.media.packets_from_caller == 3
        assert call.media.packets_from_callee == 3

        alice_sip.send(alice.bye(invite, ok).serialize(), pbx)
        assert (await alice_sip.receive_sip()).status == 200
        assert (await bob_sip.receive_sip()).method == "BYE"
        assert server.core.calls == []
        assert call.media.closed
    finally:
        for endpoint in (alice_sip, alice_rtp, bob_sip, bob_rtp):
            endpoint.close()
        await server.stop()


def test_call_with_audio_over_loopback():
    asyncio.run(scenario())
