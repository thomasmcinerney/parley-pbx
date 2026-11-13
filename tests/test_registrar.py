from parley.registrar import Registrar
from parley.sip import SipUri

from helpers import FakeClock

A = SipUri.parse("sip:100@10.0.0.5:5062")
B = SipUri.parse("sip:100@10.0.0.6")
SRC = ("10.0.0.5", 5062)


def test_binding_expires():
    clock = FakeClock()
    registrar = Registrar(clock)
    registrar.register("100", A, SRC, 60)
    clock.advance(59.5)
    (binding,) = registrar.bindings("100")
    assert registrar.remaining(binding) == 1
    clock.advance(0.5)
    assert registrar.bindings("100") == []


def test_refresh_replaces_the_same_contact():
    clock = FakeClock()
    registrar = Registrar(clock)
    registrar.register("100", A, SRC, 60)
    clock.advance(30)
    # Same contact, written differently: host case and the default port.
    registrar.register("100", SipUri.parse("sip:100@10.0.0.6:5060"), SRC, 60)
    registrar.register("100", SipUri.parse("sip:100@10.0.0.6"), ("10.0.0.6", 9), 60)
    registrar.register("100", A, ("10.0.0.5", 7000), 60)
    bindings = registrar.bindings("100")
    assert len(bindings) == 2
    assert bindings[0].source == ("10.0.0.5", 7000)
    assert registrar.remaining(bindings[0]) == 60


def test_newest_binding_is_listed_first():
    registrar = Registrar(FakeClock())
    registrar.register("100", A, SRC, 60)
    registrar.register("100", B, SRC, 60)
    assert [b.contact for b in registrar.bindings("100")] == [B, A]
    registrar.register("100", A, SRC, 60)
    assert [b.contact for b in registrar.bindings("100")] == [A, B]


def test_unregister():
    registrar = Registrar(FakeClock())
    registrar.register("100", A, SRC, 60)
    registrar.register("100", B, SRC, 60)
    registrar.register("101", A, SRC, 60)
    assert registrar.unregister("100", A) is True
    assert registrar.unregister("100", A) is False
    assert [b.contact for b in registrar.bindings("100")] == [B]
    registrar.unregister_all("100")
    assert registrar.bindings("100") == []
    assert len(registrar.bindings("101")) == 1


def test_purge_removes_only_expired_bindings():
    clock = FakeClock()
    registrar = Registrar(clock)
    registrar.register("100", A, SRC, 60)
    registrar.register("101", A, SRC, 120)
    clock.advance(60)
    assert registrar.purge() == 1
    assert registrar.purge() == 0
    assert len(registrar.bindings("101")) == 1
