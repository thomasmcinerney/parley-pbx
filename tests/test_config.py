import copy
from pathlib import Path

import pytest

from parley.cli import describe_route, main
from parley.config import ConfigError, load_config, parse_config
from parley.dialplan import Dial

from helpers import CONFIG

EXAMPLE = Path(__file__).parent.parent / "examples" / "pbx.toml"


def test_example_config_loads():
    config = load_config(EXAMPLE)
    assert config.bind == "127.0.0.1"
    assert config.port == 5060
    assert list(config.users) == ["100", "101", "102"]
    assert config.users["100"].name == "Front Desk"
    assert config.dialplan.resolve("8101") == Dial("101")


def test_defaults():
    config = parse_config({})
    assert (config.bind, config.port, config.advertise_host) == ("127.0.0.1", 5060, "127.0.0.1")
    assert (config.rtp_port_min, config.rtp_port_max) == (10000, 10099)
    assert config.ring_timeout == 30.0
    assert config.users == {}


def test_integer_extensions_and_patterns_are_accepted():
    config = parse_config(
        {"users": [{"extension": 100}], "dialplan": [{"match": 0, "target": 100}]}
    )
    assert "100" in config.users
    assert config.dialplan.resolve("0") == Dial("100")


def test_advertise_overrides_bind():
    config = parse_config({"sip": {"bind": "0.0.0.0", "advertise": "192.168.1.10"}})
    assert config.advertise_host == "192.168.1.10"


def _with(path: str, value: object) -> dict:
    data = copy.deepcopy(CONFIG)
    node = data
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node.setdefault(key, {})
    node[leaf] = value
    return data


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("sip.bind", "0.0.0.0", "advertise is required"),
        ("sip.port", 70000, "between 0 and 65535"),
        ("sip.port", "5060", "must be an integer"),
        ("sip.prot", 5060, "unknown key in [sip]: prot"),
        ("rtp.port_min", 80, "between 1024 and 65535"),
        ("rtp.port_max", 10000, "two ports per call"),
        ("calls.ring_timeout", 0, "must be positive"),
        ("calls.ring_timeout", "30", "must be a number"),
        ("logging", {}, "unknown key in the top level: logging"),
        ("users.1.extension", "100", "duplicate extension '100'"),
        ("users.1.extension", "1 0 1", "invalid extension"),
        ("users.1.password", "x", "unknown key in [[users]]: password"),
        ("dialplan.0.target", "555", "not a configured extension"),
        ("dialplan.0.status", 404, "status only applies"),
        ("dialplan.2.target", "100", "target only applies"),
        ("dialplan.2.status", 200, "between 400 and 699"),
        ("dialplan.1.match", "1.X", "dialplan rule 2: '.' must be the last character"),
        ("dialplan.1.action", "hangup", "unknown action 'hangup'"),
        ("dialplan.1.target", "${NOPE}", "unknown variable"),
    ],
)
def test_invalid_config_is_rejected(path, value, message):
    with pytest.raises(ConfigError) as excinfo:
        parse_config(_with(path, value))
    assert message in str(excinfo.value)


def test_missing_required_keys():
    with pytest.raises(ConfigError, match="missing extension"):
        parse_config({"users": [{"name": "x"}]})
    with pytest.raises(ConfigError, match="missing match"):
        parse_config({"dialplan": [{"action": "dial"}]})


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[sip\n")
    with pytest.raises(ConfigError, match="bad.toml"):
        load_config(bad)


def test_describe_route():
    config = load_config(EXAMPLE)
    assert describe_route(config, "0") == "0: dial extension 100 (Front Desk)"
    assert describe_route(config, "150") == "150: dial 150, which is not a configured extension (404)"
    assert describe_route(config, "95551234") == "95551234: rejected with status 403"
    assert describe_route(config, "777") == "777: no matching rule (404 Not Found)"


def test_cli_check_and_route(capsys, tmp_path):
    assert main(["check", "--config", str(EXAMPLE)]) == 0
    assert "Configuration OK" in capsys.readouterr().out
    assert main(["route", "--config", str(EXAMPLE), "101", "9"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["101: dial extension 101 (Workshop)", "9: no matching rule (404 Not Found)"]
    assert main(["check", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "cannot read" in capsys.readouterr().err
