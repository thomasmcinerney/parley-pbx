"""The location service: which contacts each extension is reachable at."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .sip.uri import SipUri

Addr = tuple[str, int]


@dataclass
class Binding:
    aor: str
    contact: SipUri
    source: Addr  # where the REGISTER came from; requests are sent back there
    expires_at: float
    sequence: int


def _contact_key(contact: SipUri) -> tuple[str | None, str, int]:
    return (contact.user, contact.host.lower(), contact.port or 5060)


class Registrar:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._bindings: dict[str, dict[tuple[str | None, str, int], Binding]] = {}
        self._sequence = 0

    def register(self, aor: str, contact: SipUri, source: Addr, expires: float) -> Binding:
        """Add a binding, or refresh the existing one for the same contact."""
        self._sequence += 1
        binding = Binding(aor, contact, source, self._clock() + expires, self._sequence)
        self._bindings.setdefault(aor, {})[_contact_key(contact)] = binding
        return binding

    def unregister(self, aor: str, contact: SipUri) -> bool:
        return self._bindings.get(aor, {}).pop(_contact_key(contact), None) is not None

    def unregister_all(self, aor: str) -> None:
        self._bindings.pop(aor, None)

    def bindings(self, aor: str) -> list[Binding]:
        """Unexpired bindings for an extension, most recently registered first."""
        now = self._clock()
        live = [b for b in self._bindings.get(aor, {}).values() if b.expires_at > now]
        return sorted(live, key=lambda b: b.sequence, reverse=True)

    def remaining(self, binding: Binding) -> int:
        """Whole seconds until the binding expires, rounded up."""
        return max(0, math.ceil(binding.expires_at - self._clock()))

    def purge(self) -> int:
        """Drop expired bindings and return how many were removed."""
        now = self._clock()
        removed = 0
        for aor in list(self._bindings):
            contacts = self._bindings[aor]
            for key in [k for k, b in contacts.items() if b.expires_at <= now]:
                del contacts[key]
                removed += 1
            if not contacts:
                del self._bindings[aor]
        return removed
