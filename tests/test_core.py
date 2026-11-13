"""Behaviour of the PBX core, driven with scripted phones and a fake clock."""

import pytest

from parley.sip import NameAddr, Request, Response, Via

from helpers import PBX_HOST, Harness, Phone, make_config, requests, sdp, statuses


@pytest.fixture
def pbx():
    return Harness()


@pytest.fixture
def alice():
    return Phone("100", ("10.0.0.5", 5062))


@pytest.fixture
def bob():
    return Phone("101", ("10.0.0.6", 5064))


# ------------------------------------------------------------- registration


def test_register_returns_the_binding(pbx, alice):
    (response,) = pbx.deliver(alice, alice.register(expires=600))
    assert response.status == 200
    assert response.headers.get_all("Contact") == ["<sip:100@10.0.0.5:5062>;expires=600"]
    assert response.to.tag is not None
    via = response.top_via
    assert via.params["received"] == "10.0.0.5"
    assert via.params["rport"] == "5062"


def test_register_unknown_extension(pbx):
    stranger = Phone("555", ("10.0.0.9", 5060))
    assert statuses(pbx.deliver(stranger, stranger.register())) == [404]


def test_register_expiry_limits(pbx, alice):
    (brief,) = pbx.deliver(alice, alice.register(expires=30))
    assert brief.status == 423
    assert brief.headers.get("Min-Expires") == "60"
    assert pbx.core.registrar.bindings("100") == []

    (long,) = pbx.deliver(alice, alice.register(expires=86400))
    assert long.headers.get("Contact").endswith(";expires=3600")

    (default,) = pbx.deliver(alice, alice.register(expires=None))
    assert default.headers.get("Contact").endswith(";expires=3600")


def test_contact_expires_parameter_overrides_header(pbx, alice):
    request = alice.register(expires=600, contact="<sip:100@10.0.0.5:5062>;expires=120")
    (response,) = pbx.deliver(alice, request)
    assert response.headers.get("Contact").endswith(";expires=120")


def test_unregister_one_contact_and_all(pbx, alice):
    pbx.register(alice)
    pbx.deliver(alice, alice.register(contact="<sip:100@10.0.0.7>"))
    (response,) = pbx.deliver(alice, alice.register(expires=0))
    assert response.headers.get_all("Contact") == ["<sip:100@10.0.0.7>;expires=3600"]

    (response,) = pbx.deliver(alice, alice.register(expires=0, contact="*"))
    assert response.status == 200
    assert response.headers.get_all("Contact") == []

    assert statuses(pbx.deliver(alice, alice.register(expires=60, contact="*"))) == [400]


def test_registration_expires_with_time(pbx, alice, bob):
    pbx.register(alice)
    pbx.deliver(bob, bob.register(expires=60))
    pbx.clock.advance(61)
    pbx.core.tick()
    assert statuses(pbx.deliver(alice, alice.invite("101"))) == [100, 480]


def test_retransmitted_request_gets_the_same_response(pbx, alice):
    request = alice.register()
    (first,) = pbx.deliver(alice, request)
    (second,) = pbx.deliver(alice, request)
    assert second.serialize() == first.serialize()


def test_response_goes_to_via_port_without_rport(pbx, alice):
    request = alice.register()
    request.headers.set("Via", "SIP/2.0/UDP 10.0.0.5:5062;branch=z9hG4bKnorport")
    pbx.core.datagram_received(request.serialize(), ("10.0.0.5", 33333))
    ((response, dest),) = pbx.sent
    assert dest == ("10.0.0.5", 5062)
    assert response.top_via.params == {"branch": "z9hG4bKnorport", "received": "10.0.0.5"}


def test_options_and_unknown_methods(pbx, alice):
    options = alice.register()
    options.method = "OPTIONS"
    options.headers.set("CSeq", "1 OPTIONS")
    (response,) = pbx.deliver(alice, options)
    assert response.status == 200
    assert "INVITE" in response.headers.get("Allow")

    publish = alice.register()
    publish.method = "PUBLISH"
    publish.headers.set("CSeq", "1 PUBLISH")
    (response,) = pbx.deliver(alice, publish)
    assert response.status == 405
    assert response.headers.get("Allow") is not None


def test_garbage_is_ignored(pbx, alice):
    pbx.core.datagram_received(b"\r\n\r\n", alice.addr)
    pbx.core.datagram_received(b"not sip at all", alice.addr)
    assert pbx.sent == []


def test_unparseable_header_gets_400(pbx, alice):
    request = alice.register()
    request.headers.set("To", "this is not an address")
    assert statuses(pbx.deliver(alice, request)) == [400]


# -------------------------------------------------------------------- calls


def start_call(pbx, alice, bob, number="101"):
    """Alice dials; returns her INVITE and the INVITE the PBX sends to Bob."""
    pbx.register(alice, bob)
    invite = alice.invite(number, media_port=4000)
    assert statuses(pbx.deliver(alice, invite)) == [100]
    (b_invite,) = requests(pbx.take(bob), "INVITE")
    return invite, b_invite


def answer_call(pbx, alice, bob, invite, b_invite):
    """Bob rings and answers; returns the 200 Alice receives."""
    assert statuses(pbx.deliver(bob, bob.respond(b_invite, 180))) == []
    (ringing,) = pbx.take(alice)
    assert ringing.status == 180
    (ack,) = pbx.deliver(bob, bob.respond(b_invite, 200, media_port=6000))
    assert ack.method == "ACK"
    (ok,) = pbx.take(alice)
    assert ok.status == 200
    assert pbx.deliver(alice, alice.ack(invite, ok)) == []
    return ok


def test_invite_is_bridged_to_the_callee(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)

    assert b_invite.uri == "sip:101@10.0.0.6:5064"
    assert b_invite.call_id != invite.call_id
    assert b_invite.cseq == (1, "INVITE")
    assert b_invite.from_.display == "Alice"
    assert str(b_invite.from_.uri) == f"sip:100@{PBX_HOST}"
    assert b_invite.from_.tag not in (None, alice.tag)
    assert b_invite.to.tag is None
    assert Via.parse(b_invite.headers.get("Via")).host == PBX_HOST
    assert len(b_invite.headers.get_all("Via")) == 1
    assert NameAddr.parse(b_invite.headers.get("Contact")).uri.host == PBX_HOST

    # Bob is told to send audio to the relay, not to Alice.
    session = pbx.relay.sessions[0]
    assert f"c=IN IP4 {PBX_HOST}".encode() in b_invite.body
    assert f"m=audio {session.callee_port} RTP/AVP 0 8".encode() in b_invite.body
    assert session.caller == ("10.0.0.5", 4000)


def test_answer_is_relayed_and_media_is_anchored(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    ok = answer_call(pbx, alice, bob, invite, b_invite)

    assert ok.call_id == invite.call_id
    assert ok.cseq == invite.cseq
    assert ok.to.tag not in (None, bob.tag)
    assert ok.headers.get("Via") is not None
    assert NameAddr.parse(ok.headers.get("Contact")).uri.host == PBX_HOST
    session = pbx.relay.sessions[0]
    assert f"m=audio {session.caller_port} RTP/AVP 0 8".encode() in ok.body
    assert f"c=IN IP4 {PBX_HOST}".encode() in ok.body
    assert session.callee == ("10.0.0.6", 6000)
    assert not session.closed
    assert len(pbx.core.calls) == 1


def test_ringing_uses_the_same_tag_as_the_answer(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    pbx.deliver(bob, bob.respond(b_invite, 180))
    (ringing,) = pbx.take(alice)
    pbx.deliver(bob, bob.respond(b_invite, 200, media_port=6000))
    (ok,) = pbx.take(alice)
    assert ringing.to.tag == ok.to.tag


def test_ack_to_callee_uses_its_contact_and_tag(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    (ack,) = pbx.deliver(bob, bob.respond(b_invite, 200, media_port=6000))
    assert ack.uri == "sip:101@10.0.0.6:5064"
    assert ack.cseq == (1, "ACK")
    assert ack.to.tag == bob.tag
    assert ack.call_id == b_invite.call_id
    assert ack.top_via.branch != b_invite.top_via.branch


def test_caller_hangs_up(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    ok = answer_call(pbx, alice, bob, invite, b_invite)

    assert statuses(pbx.deliver(alice, alice.bye(invite, ok))) == [200]
    (bye,) = pbx.take(bob)
    assert (bye.method, bye.uri) == ("BYE", "sip:101@10.0.0.6:5064")
    assert bye.call_id == b_invite.call_id
    assert bye.cseq == (2, "BYE")
    assert bye.to.tag == bob.tag
    assert bye.from_.tag == b_invite.from_.tag
    assert pbx.relay.sessions[0].closed
    assert pbx.core.calls == []


def test_callee_hangs_up(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    ok = answer_call(pbx, alice, bob, invite, b_invite)

    assert statuses(pbx.deliver(bob, bob.bye_incoming(b_invite))) == [200]
    (bye,) = pbx.take(alice)
    assert (bye.method, bye.uri) == ("BYE", "sip:100@10.0.0.5:5062")
    assert bye.call_id == invite.call_id
    assert bye.from_.tag == ok.to.tag
    assert bye.to.tag == alice.tag
    assert pbx.relay.sessions[0].closed
    assert pbx.core.calls == []


def test_bye_for_unknown_or_mismatched_dialog(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    ok = answer_call(pbx, alice, bob, invite, b_invite)

    wrong = alice.bye(invite, ok)
    wrong.headers.set("To", str(ok.to.with_tag("someone-else")))
    assert statuses(pbx.deliver(alice, wrong)) == [481]
    assert len(pbx.core.calls) == 1

    assert statuses(pbx.deliver(alice, alice.bye(invite, ok))) == [200]
    pbx.take(bob)
    assert statuses(pbx.deliver(alice, alice.bye(invite, ok))) == [481]
    assert pbx.take(bob) == []


def test_callee_busy(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    (ack,) = pbx.deliver(bob, bob.respond(b_invite, 486))
    # A failure is acknowledged within the INVITE transaction.
    assert ack.method == "ACK"
    assert ack.uri == b_invite.uri
    assert ack.top_via.branch == b_invite.top_via.branch
    assert ack.to.tag == bob.tag
    assert statuses(pbx.take(alice)) == [486]
    assert pbx.relay.sessions[0].closed
    assert pbx.core.calls == []


@pytest.mark.parametrize(("callee_status", "caller_status"), [(302, 480), (407, 503), (603, 603)])
def test_callee_failures_are_translated(pbx, alice, bob, callee_status, caller_status):
    invite, b_invite = start_call(pbx, alice, bob)
    pbx.deliver(bob, bob.respond(b_invite, callee_status))
    assert statuses(pbx.take(alice)) == [caller_status]


def test_caller_cancels_while_ringing(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    pbx.deliver(bob, bob.respond(b_invite, 180))
    pbx.take(alice)

    replies = pbx.deliver(alice, alice.cancel(invite))
    assert [(r.status, r.cseq[1]) for r in replies] == [(200, "CANCEL"), (487, "INVITE")]
    (cancel,) = pbx.take(bob)
    assert cancel.method == "CANCEL"
    assert cancel.uri == b_invite.uri
    assert cancel.top_via.branch == b_invite.top_via.branch
    assert cancel.cseq == (1, "CANCEL")
    assert pbx.relay.sessions[0].closed
    assert pbx.core.calls == []

    # Bob confirms; the PBX acknowledges his 487 and tells Alice nothing more.
    (ack,) = pbx.deliver(bob, bob.respond(b_invite, 487))
    assert ack.method == "ACK"
    assert pbx.take(alice) == []


def test_answer_crossing_a_cancel_is_hung_up(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    pbx.deliver(alice, alice.cancel(invite))
    pbx.take(bob)
    sent = pbx.deliver(bob, bob.respond(b_invite, 200, media_port=6000))
    assert [m.method for m in sent] == ["ACK", "BYE"]
    assert pbx.take(alice) == []


def test_cancel_for_unknown_call(pbx, alice, bob):
    pbx.register(alice, bob)
    invite = alice.invite("101")
    assert statuses(pbx.deliver(alice, alice.cancel(invite))) == [481]


def test_ring_timeout(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    pbx.clock.advance(29)
    pbx.core.tick()
    assert pbx.sent == []
    pbx.clock.advance(1)
    pbx.core.tick()
    assert statuses(pbx.take(alice)) == [480]
    assert [m.method for m in pbx.take(bob)] == ["CANCEL"]
    assert pbx.core.calls == []


def test_answered_call_does_not_time_out(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    answer_call(pbx, alice, bob, invite, b_invite)
    pbx.clock.advance(3600)
    pbx.core.tick()
    assert pbx.sent == []
    assert len(pbx.core.calls) == 1


def test_retransmitted_invite_does_not_start_a_second_call(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    assert statuses(pbx.deliver(alice, invite)) == [100]
    assert pbx.take(bob) == []
    assert len(pbx.relay.sessions) == 1

    pbx.deliver(bob, bob.respond(b_invite, 180))
    pbx.take(alice)
    assert statuses(pbx.deliver(alice, invite)) == [180]


def test_retransmitted_answer_is_acknowledged_again(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    answer_call(pbx, alice, bob, invite, b_invite)
    (ack,) = pbx.deliver(bob, bob.respond(b_invite, 200, media_port=6000))
    assert ack.method == "ACK"
    assert pbx.take(alice) == []


def test_newest_registration_is_called(pbx, alice, bob):
    pbx.register(alice, bob)
    desk = Phone("101", ("10.0.0.7", 5070))
    pbx.register(desk)
    pbx.deliver(alice, alice.invite("101"))
    assert pbx.take(bob) == []
    assert [m.method for m in pbx.take(desk)] == ["INVITE"]


def test_dialplan_target_and_rejection(pbx, alice, bob):
    pbx.register(alice, bob)
    pbx.deliver(bob, bob.invite("0"))
    (b_invite,) = pbx.take(alice)
    assert b_invite.uri == "sip:100@10.0.0.5:5062"
    assert b_invite.from_.display == "Bob"

    assert statuses(pbx.deliver(alice, alice.invite("95551234"))) == [100, 403]
    assert statuses(pbx.deliver(alice, alice.invite("777"))) == [100, 404]
    assert statuses(pbx.deliver(alice, alice.invite("150"))) == [100, 404]
    assert statuses(pbx.deliver(alice, alice.invite("102"))) == [100, 480]


def test_unknown_caller_is_refused(pbx, bob):
    pbx.register(bob)
    stranger = Phone("555", ("10.0.0.9", 5060))
    assert statuses(pbx.deliver(stranger, stranger.invite("101"))) == [403]
    assert pbx.take(bob) == []


@pytest.mark.parametrize("body", [b"", b"v=0\r\nm=video 1 RTP/AVP 96\r\n", b"not sdp"])
def test_unusable_offer_is_refused(pbx, alice, bob, body):
    pbx.register(alice, bob)
    assert statuses(pbx.deliver(alice, alice.invite("101", body=body))) == [100, 488]
    assert pbx.take(bob) == []
    assert all(session.closed for session in pbx.relay.sessions)


def test_unusable_answer_ends_the_call(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    sent = pbx.deliver(bob, bob.respond(b_invite, 200))  # no SDP
    assert [m.method for m in sent] == ["ACK", "BYE"]
    assert statuses(pbx.take(alice)) == [502]
    assert pbx.core.calls == []


def test_reinvite_is_refused_but_call_survives(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    ok = answer_call(pbx, alice, bob, invite, b_invite)
    reinvite = alice.invite("101")
    reinvite.headers.set("Call-ID", invite.call_id)
    reinvite.headers.set("To", ok.headers.get("To"))
    assert statuses(pbx.deliver(alice, reinvite)) == [488]
    assert len(pbx.core.calls) == 1


def test_two_calls_use_separate_relay_sessions(pbx, alice, bob):
    carol = Phone("102", ("10.0.0.8", 5066))
    pbx.register(alice, bob, carol)
    pbx.deliver(alice, alice.invite("101"))
    pbx.deliver(carol, carol.invite("100"))
    (to_bob,) = pbx.take(bob)
    (to_alice,) = requests(pbx.take(alice), "INVITE")
    first, second = pbx.relay.sessions
    assert f"m=audio {first.callee_port} ".encode() in to_bob.body
    assert f"m=audio {second.callee_port} ".encode() in to_alice.body
    assert to_bob.call_id != to_alice.call_id
    assert len(pbx.core.calls) == 2


def test_configured_ring_timeout_is_used(alice, bob):
    pbx = Harness(make_config(calls={"ring_timeout": 5}))
    start_call(pbx, alice, bob)
    pbx.clock.advance(5)
    pbx.core.tick()
    assert statuses(pbx.take(alice)) == [480]


def test_messages_are_requests_or_responses(pbx, alice, bob):
    invite, b_invite = start_call(pbx, alice, bob)
    assert isinstance(b_invite, Request)
    assert isinstance(pbx.deliver(bob, bob.respond(b_invite, 486))[0], Request)
    assert isinstance(pbx.take(alice)[0], Response)
    assert sdp("h", 1).startswith(b"v=0")
