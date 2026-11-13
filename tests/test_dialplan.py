import pytest

from parley.dialplan import Dial, Dialplan, DialplanError, Reject, Rule, compile_pattern


@pytest.mark.parametrize(
    ("pattern", "number", "expected"),
    [
        ("100", "100", True),
        ("100", "1000", False),
        ("1XX", "105", True),
        ("1XX", "1a5", False),
        ("1XX", "15", False),
        ("NXX", "200", True),
        ("NXX", "100", False),
        ("ZXX", "100", True),
        ("ZXX", "000", False),
        ("[13-5]0", "40", True),
        ("[13-5]0", "20", False),
        ("[*#]1", "*1", True),
        ("9.", "9", False),
        ("9.", "95551234", True),
        ("9!", "9", True),
        ("*72", "*72", True),
        ("*72", "172", False),
        ("+1X", "+15", True),
        ("lobby", "lobby", True),
    ],
)
def test_pattern_matching(pattern, number, expected):
    assert (compile_pattern(pattern).fullmatch(number) is not None) is expected


@pytest.mark.parametrize(
    "pattern", ["", "1.2", "!1", "[", "[]", "[a]", "[5-1]", "1 2", "1Q", "(1)", "1\\d"]
)
def test_invalid_patterns_are_rejected(pattern):
    with pytest.raises(DialplanError):
        compile_pattern(pattern)


def test_first_matching_rule_wins():
    plan = Dialplan(
        [
            Rule("199", "reject", status=486),
            Rule("1XX"),
            Rule("1.", "reject"),
        ]
    )
    assert plan.resolve("199") == Reject(486)
    assert plan.resolve("150") == Dial("150")
    assert plan.resolve("1500") == Reject(403)
    assert plan.resolve("200") is None


def test_target_templates():
    plan = Dialplan(
        [
            Rule("0", target="100"),
            Rule("8XXX", target="${EXTEN:1}"),
            Rule("7XX", target="1${EXTEN:1}"),
            Rule("6XX", target="${EXTEN}"),
            Rule("5X", target="${EXTEN:9}"),
        ]
    )
    assert plan.resolve("0") == Dial("100")
    assert plan.resolve("8101") == Dial("101")
    assert plan.resolve("712") == Dial("112")
    assert plan.resolve("612") == Dial("612")
    assert plan.resolve("51") == Dial("")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "transfer"},
        {"target": ""},
        {"target": "${CALLER}"},
        {"target": "$EXTEN"},
        {"action": "reject", "status": 200},
        {"action": "reject", "status": 700},
    ],
)
def test_invalid_rules_are_rejected(kwargs):
    with pytest.raises(DialplanError):
        Rule("1XX", **kwargs)
