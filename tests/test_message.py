import pytest

from parley.sip import Headers, NameAddr, Request, Response, SipParseError, SipUri, Via
from parley.sip.message import parse_message, split_commas

INVITE = (
    b"INVITE sip:101@pbx.test SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 10.0.0.5:5062;branch=z9hG4bKabc;rport\r\n"
    b"Max-Forwards: 70\r\n"
    b'From: "Alice" <sip:100@pbx.test>;tag=a1\r\n'
    b"To: <sip:101@pbx.test>\r\n"
    b"Call-ID: call-1@10.0.0.5\r\n"
    b"CSeq: 7 INVITE\r\n"
    b"Content-Type: application/sdp\r\n"
    b"Content-Length: 4\r\n"
    b"\r\n"
    b"v=0\n"
)


def test_parse_request():
    msg = parse_message(INVITE)
    assert isinstance(msg, Request)
    assert (msg.method, msg.uri) == ("INVITE", "sip:101@pbx.test")
    assert msg.call_id == "call-1@10.0.0.5"
    assert msg.cseq == (7, "INVITE")
    assert msg.from_.display == "Alice"
    assert msg.from_.tag == "a1"
    assert msg.to.tag is None
    assert msg.top_via.branch == "z9hG4bKabc"
    assert msg.body == b"v=0\n"


def test_parse_response():
    data = (
        b"SIP/2.0 486 Busy Here\r\n"
        b"Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bKx\r\n"
        b"From: <sip:100@pbx.test>;tag=a\r\nTo: <sip:101@pbx.test>;tag=b\r\n"
        b"Call-ID: c\r\nCSeq: 1 INVITE\r\n\r\n"
    )
    msg = parse_message(data)
    assert isinstance(msg, Response)
    assert (msg.status, msg.reason) == (486, "Busy Here")


def test_round_trip_preserves_content():
    again = parse_message(parse_message(INVITE).serialize())
    assert again.serialize() == parse_message(INVITE).serialize()
    assert again.headers.get("Content-Length") == "4"


def test_compact_header_names_are_expanded():
    data = (
        b"OPTIONS sip:pbx.test SIP/2.0\r\n"
        b"v: SIP/2.0/UDP h;branch=z9hG4bK1\r\nf: <sip:a@h>;tag=1\r\nt: <sip:b@h>\r\n"
        b"i: abc\r\nCSeq: 1 OPTIONS\r\nl: 0\r\n\r\n"
    )
    msg = parse_message(data)
    assert msg.call_id == "abc"
    assert msg.headers.get("via") == "SIP/2.0/UDP h;branch=z9hG4bK1"


def test_header_names_are_case_insensitive():
    data = INVITE.replace(b"Call-ID:", b"call-id:").replace(b"CSeq:", b"CSEQ:")
    msg = parse_message(data)
    assert msg.call_id == "call-1@10.0.0.5"
    assert msg.cseq == (7, "INVITE")


def test_folded_header_is_joined():
    data = INVITE.replace(b"To: <sip:101@pbx.test>", b"To:\r\n <sip:101@pbx.test>")
    assert parse_message(data).to.uri.user == "101"


def test_comma_separated_via_becomes_separate_headers():
    data = INVITE.replace(
        b";rport\r\n", b";rport, SIP/2.0/UDP 10.0.0.9;branch=z9hG4bKdef\r\n"
    )
    vias = parse_message(data).headers.get_all("Via")
    assert len(vias) == 2
    assert Via.parse(vias[1]).host == "10.0.0.9"


def test_split_commas_respects_quotes_and_brackets():
    value = '"Doe, Jane" <sip:j@h>;q=1, <sip:k@h?subject=a,b>'
    assert split_commas(value) == ['"Doe, Jane" <sip:j@h>;q=1', "<sip:k@h?subject=a,b>"]


def test_body_is_cut_to_content_length():
    assert parse_message(INVITE + b"trailing junk").body == b"v=0\n"


def test_leading_blank_lines_are_ignored():
    assert parse_message(b"\r\n\r\n" + INVITE).call_id == "call-1@10.0.0.5"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\r\n\r\n",
        b"hello",
        b"INVITE sip:a@b\r\n\r\n",
        b"INVITE sip:a@b SIP/3.0\r\nVia: x\r\n\r\n",
        b"SIP/2.0 99 Low\r\n\r\n",
        INVITE.replace(b"Call-ID: call-1@10.0.0.5\r\n", b""),
        INVITE.replace(b"Content-Length: 4", b"Content-Length: 400"),
        INVITE.replace(b"Content-Length: 4", b"Content-Length: four"),
        INVITE.replace(b"Max-Forwards: 70", b"no colon here"),
        b"\xff\xfe\r\n\r\n",
    ],
)
def test_malformed_messages_are_rejected(data):
    with pytest.raises(SipParseError):
        parse_message(data)


def test_invalid_cseq_raises_on_access():
    msg = parse_message(INVITE.replace(b"CSeq: 7 INVITE", b"CSeq: seven"))
    with pytest.raises(SipParseError):
        msg.cseq


def test_headers_set_replaces_all_occurrences():
    headers = Headers([("Via", "a"), ("X-One", "1"), ("via", "b")])
    headers.set("VIA", "c")
    assert list(headers) == [("Via", "c"), ("X-One", "1")]


def test_uri_parse_and_format():
    uri = SipUri.parse("sip:100@10.0.0.5:5062;transport=udp;lr")
    assert (uri.user, uri.host, uri.port) == ("100", "10.0.0.5", 5062)
    assert uri.params == {"transport": "udp", "lr": None}
    assert str(uri) == "sip:100@10.0.0.5:5062;transport=udp;lr"
    assert SipUri.parse("sip:pbx.test").user is None
    assert SipUri.parse("sip:[::1]:5060").host == "[::1]"


@pytest.mark.parametrize("text", ["tel:+123", "sip:", "100@host", "sip:a@h:99999"])
def test_invalid_uris_are_rejected(text):
    with pytest.raises(SipParseError):
        SipUri.parse(text)


def test_name_addr_forms():
    quoted = NameAddr.parse('"Doe, \\"JD\\" Jane" <sip:j@h;x=1>;tag=t')
    assert quoted.display == 'Doe, "JD" Jane'
    assert quoted.uri.params == {"x": "1"}
    assert quoted.tag == "t"
    assert NameAddr.parse(str(quoted)) == quoted

    bare = NameAddr.parse("sip:j@h;tag=t")
    assert bare.uri.params == {}
    assert bare.tag == "t"

    assert NameAddr.parse("Jane <sip:j@h>").display == "Jane"
    assert str(NameAddr.parse("<sip:j@h>").with_tag("z")) == "<sip:j@h>;tag=z"


def test_via_parse_and_format():
    via = Via.parse("SIP/2.0/UDP 10.0.0.5:5062;branch=z9hG4bKabc;rport")
    assert (via.transport, via.host, via.port) == ("UDP", "10.0.0.5", 5062)
    assert "rport" in via.params and via.params["rport"] is None
    assert str(via) == "SIP/2.0/UDP 10.0.0.5:5062;branch=z9hG4bKabc;rport"
    with pytest.raises(SipParseError):
        Via.parse("HTTP/1.1 host")
