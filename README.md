# Parley

A small SIP PBX server written from scratch in Python. It has no runtime
dependencies beyond the standard library.

Parley lets SIP phones (desk phones or softphones) register as extensions and
call each other. It implements the SIP signalling itself rather than wrapping
an existing stack.

## What works today

- **Registration** (`REGISTER`): extensions from the config file can register,
  refresh and unregister contacts, with expiry.
- **Two-party calls** (`INVITE`, `ACK`, `CANCEL`, `BYE`): ringing, answer,
  busy/decline, caller cancel, hang-up from either side, and a ring timeout.
- **Dialplan**: ordered pattern rules in the config file that either dial an
  extension or reject the call.
- **Audio relay**: each call's RTP audio is relayed through the PBX, so phones
  never need to reach each other directly.
- **CLI**: run the server, validate a config file, or ask what dialling a
  number would do.

## Quick start

Requires Python 3.11 or newer.

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp examples/pbx.toml pbx.toml
parley check --config pbx.toml
parley serve --config pbx.toml
```

To use real phones, set `sip.bind` in `pbx.toml` to this machine's LAN
address, then point each phone at that address with an extension from the
config as its username. No password is needed.

Ask what the dialplan would do with a number, without placing a call:

```bash
parley route --config pbx.toml 0 101 95551234
```

## Configuration

See [examples/pbx.toml](examples/pbx.toml) for a commented example. Dialplan
rules are tried in order and the first match wins.

| Pattern | Matches |
|---|---|
| `X` | any digit |
| `Z` | any digit 1-9 |
| `N` | any digit 2-9 |
| `[15-7]` | one character from the set |
| `.` | one or more of any character (last position only) |
| `!` | zero or more of any character (last position only) |

A dial target may use `${EXTEN}` (the dialled number) and `${EXTEN:n}` (the
dialled number with its first `n` characters removed).

## Tests

```bash
python -m pytest
```

Tests run without installing the package and without any network access
beyond the loopback interface.

## How it is organised

| Module | Role |
|---|---|
| `parley/sip/message.py` | Parse and serialise SIP requests and responses |
| `parley/sip/uri.py` | SIP URIs, `From`/`To`/`Contact` values, `Via` |
| `parley/sip/sdp.py` | Rewrite the audio stream of an SDP body |
| `parley/config.py` | Load and validate the TOML config |
| `parley/dialplan.py` | Pattern rules and dial targets |
| `parley/registrar.py` | Which contacts each extension is reachable at |
| `parley/core.py` | Registration and call handling; no I/O |
| `parley/rtp.py` | UDP audio relay |
| `parley/server.py` | asyncio sockets and the housekeeping timer |
| `parley/cli.py` | Command-line interface |

The core is a back-to-back user agent: a call from A to B is two independent
SIP dialogs, A to PBX and PBX to B. The core does no I/O and takes its time
from an injectable clock, so `tests/test_core.py` drives it with scripted
phones and a fake clock. `tests/test_e2e.py` runs one full call with audio
over real UDP sockets.

## Licence

MIT. See [LICENSE](LICENSE).
