import pytest

from parley.sip import SdpError
from parley.sip.sdp import AudioEndpoint, rewrite_audio

from helpers import sdp


def test_rewrites_connection_and_port():
    body, original = rewrite_audio(sdp("192.168.1.20", 4000), "10.0.0.1", 20000)
    assert original == AudioEndpoint("192.168.1.20", 4000)
    text = body.decode()
    assert "c=IN IP4 10.0.0.1\r\n" in text
    assert "m=audio 20000 RTP/AVP 0 8\r\n" in text
    # The origin line identifies the session and is left alone.
    assert "o=- 1 1 IN IP4 192.168.1.20\r\n" in text
    assert "a=rtpmap:0 PCMU/8000\r\n" in text


def test_media_level_connection_overrides_session_level():
    offer = (
        b"v=0\r\nc=IN IP4 1.1.1.1\r\nm=audio 5000 RTP/AVP 0\r\nc=IN IP4 2.2.2.2\r\n"
    )
    body, original = rewrite_audio(offer, "10.0.0.1", 20000)
    assert original == AudioEndpoint("2.2.2.2", 5000)
    assert body.count(b"c=IN IP4 10.0.0.1") == 2


def test_other_streams_are_declined():
    offer = (
        b"v=0\r\nc=IN IP4 1.1.1.1\r\n"
        b"m=audio 5000 RTP/AVP 0\r\n"
        b"m=video 5002 RTP/AVP 96\r\nc=IN IP4 3.3.3.3\r\n"
        b"m=audio 5004 RTP/AVP 0\r\n"
    )
    body, original = rewrite_audio(offer, "10.0.0.1", 20000)
    text = body.decode()
    assert original.port == 5000
    assert "m=audio 20000 RTP/AVP 0" in text
    assert "m=video 0 RTP/AVP 96" in text
    assert "m=audio 0 RTP/AVP 0" in text
    assert "c=IN IP4 3.3.3.3" in text


def test_declined_audio_stays_declined():
    body, original = rewrite_audio(sdp("1.1.1.1", 0), "10.0.0.1", 20000)
    assert b"m=audio 0 RTP/AVP" in body
    assert not original.usable


def test_null_address_is_not_usable():
    _, original = rewrite_audio(sdp("0.0.0.0", 4000), "10.0.0.1", 20000)
    assert not original.usable


def test_bare_newlines_are_accepted_and_output_uses_crlf():
    body, _ = rewrite_audio(b"v=0\nc=IN IP4 1.1.1.1\nm=audio 5000 RTP/AVP 0\n", "10.0.0.1", 9)
    assert body == b"v=0\r\nc=IN IP4 10.0.0.1\r\nm=audio 9 RTP/AVP 0\r\n"


@pytest.mark.parametrize(
    "offer",
    [
        b"",
        b"v=0\r\nc=IN IP4 1.1.1.1\r\nm=video 5000 RTP/AVP 96\r\n",
        b"v=0\r\nm=audio 5000 RTP/AVP 0\r\n",
        b"v=0\r\nc=IN IP4 1.1.1.1\r\nm=audio abc RTP/AVP 0\r\n",
        b"v=0\r\nc=IN IP4 1.1.1.1\r\nm=audio\r\n",
        b"\xff\xff",
    ],
)
def test_unusable_sdp_is_rejected(offer):
    with pytest.raises(SdpError):
        rewrite_audio(offer, "10.0.0.1", 20000)
