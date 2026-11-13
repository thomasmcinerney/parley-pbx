"""Just enough SDP (RFC 4566) to anchor one audio stream on the media relay."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import SdpError


@dataclass(frozen=True)
class AudioEndpoint:
    """Where a party said it wants to receive audio."""

    host: str
    port: int

    @property
    def usable(self) -> bool:
        """False when the stream is declined or on hold with a null address."""
        return self.port != 0 and self.host not in ("0.0.0.0", "::")


def _connection_line(host: str) -> str:
    return f"c=IN {'IP6' if ':' in host else 'IP4'} {host}"


def rewrite_audio(sdp: bytes, host: str, port: int) -> tuple[bytes, AudioEndpoint]:
    """Point the first audio stream at ``host:port`` and report where it pointed.

    Any other media stream is declined by setting its port to zero, because the
    relay carries a single audio stream per call.
    """
    try:
        lines = sdp.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SdpError("SDP is not valid UTF-8") from exc

    out: list[str] = []
    session_host: str | None = None
    audio_host: str | None = None
    audio_port: int | None = None
    media_index = -1
    audio_index: int | None = None

    for line in lines:
        if not line.strip():
            continue
        if line.startswith("m="):
            media_index += 1
            fields = line[2:].split()
            if len(fields) < 3:
                raise SdpError(f"invalid media line: {line!r}")
            if fields[0] == "audio" and audio_index is None:
                audio_index = media_index
                port_text = fields[1].split("/")[0]
                if not port_text.isdigit():
                    raise SdpError(f"invalid media port: {line!r}")
                audio_port = int(port_text)
                fields[1] = str(port if audio_port != 0 else 0)
            else:
                fields[1] = "0"
            out.append("m=" + " ".join(fields))
        elif line.startswith("c="):
            fields = line[2:].split()
            if len(fields) != 3:
                raise SdpError(f"invalid connection line: {line!r}")
            address = fields[2].split("/")[0]
            if media_index < 0:
                session_host = address
                out.append(_connection_line(host))
            elif media_index == audio_index:
                audio_host = address
                out.append(_connection_line(host))
            else:
                out.append(line)
        else:
            out.append(line)

    if audio_port is None:
        raise SdpError("no audio stream offered")
    original_host = audio_host or session_host
    if original_host is None:
        raise SdpError("audio stream has no connection address")
    body = ("\r\n".join(out) + "\r\n").encode("utf-8")
    return body, AudioEndpoint(original_host, audio_port)
