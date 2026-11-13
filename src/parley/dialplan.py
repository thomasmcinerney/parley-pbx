"""The dialplan: an ordered list of rules mapping dialled numbers to actions.

Patterns are matched against the whole dialled string. The first rule that
matches wins.

    X       any digit 0-9
    Z       any digit 1-9
    N       any digit 2-9
    [15-7]  any one character from the set (digits, ``*``, ``#`` and ranges)
    .       one or more of any character (only as the last character)
    !       zero or more of any character (only as the last character)

Digits, lower-case letters and ``* # + _ -`` match themselves.

Dial targets may use ``${EXTEN}`` for the dialled string and ``${EXTEN:n}``
for the dialled string with its first ``n`` characters removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class DialplanError(ValueError):
    """Raised for an invalid pattern, target template or rule."""


_SET_RE = re.compile(r"^(?:[0-9]-[0-9]|[0-9*#])+$")
_LITERAL_RE = re.compile(r"^[0-9a-z*#+_\-]$")
_EXTEN_RE = re.compile(r"\$\{EXTEN(?::(\d+))?\}")
_WILDCARDS = {"X": "[0-9]", "Z": "[1-9]", "N": "[2-9]"}


def compile_pattern(pattern: str) -> re.Pattern[str]:
    if not pattern:
        raise DialplanError("empty pattern")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch in _WILDCARDS:
            out.append(_WILDCARDS[ch])
        elif ch == "[":
            end = pattern.find("]", i)
            if end < 0:
                raise DialplanError(f"unterminated set in pattern {pattern!r}")
            body = pattern[i + 1 : end]
            if not _SET_RE.match(body):
                raise DialplanError(f"invalid set [{body}] in pattern {pattern!r}")
            for low, high in re.findall(r"([0-9])-([0-9])", body):
                if low > high:
                    raise DialplanError(f"reversed range {low}-{high} in pattern {pattern!r}")
            out.append(f"[{body}]")
            i = end
        elif ch in ".!":
            if i != len(pattern) - 1:
                raise DialplanError(f"{ch!r} must be the last character of pattern {pattern!r}")
            out.append(".+" if ch == "." else ".*")
        elif _LITERAL_RE.match(ch):
            out.append(re.escape(ch))
        else:
            raise DialplanError(f"invalid character {ch!r} in pattern {pattern!r}")
        i += 1
    return re.compile("".join(out))


def validate_template(template: str) -> None:
    if not template:
        raise DialplanError("empty dial target")
    if "$" in _EXTEN_RE.sub("", template):
        raise DialplanError(f"unknown variable in dial target {template!r}")


def expand_template(template: str, exten: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        return exten[int(match[1]) :] if match[1] else exten

    return _EXTEN_RE.sub(substitute, template)


@dataclass(frozen=True)
class Dial:
    """Ring the extension ``target``."""

    target: str


@dataclass(frozen=True)
class Reject:
    """Refuse the call with a SIP status code."""

    status: int


Decision = Dial | Reject


@dataclass(frozen=True)
class Rule:
    pattern: str
    action: str = "dial"
    target: str = "${EXTEN}"
    status: int = 403

    def __post_init__(self) -> None:
        compile_pattern(self.pattern)
        if self.action == "dial":
            validate_template(self.target)
        elif self.action == "reject":
            if not 400 <= self.status <= 699:
                raise DialplanError(f"reject status must be 400-699, got {self.status}")
        else:
            raise DialplanError(f"unknown action {self.action!r}")

    def matches(self, number: str) -> bool:
        return compile_pattern(self.pattern).fullmatch(number) is not None

    def decide(self, number: str) -> Decision:
        if self.action == "reject":
            return Reject(self.status)
        return Dial(expand_template(self.target, number))


class Dialplan:
    def __init__(self, rules: list[Rule] | tuple[Rule, ...] = ()) -> None:
        self.rules = tuple(rules)
        self._compiled = [(compile_pattern(rule.pattern), rule) for rule in self.rules]

    def resolve(self, number: str) -> Decision | None:
        """Return the decision of the first matching rule, or None."""
        for regex, rule in self._compiled:
            if regex.fullmatch(number):
                return rule.decide(number)
        return None
