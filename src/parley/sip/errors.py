class SipParseError(ValueError):
    """Raised when a SIP message, header or URI is malformed."""


class SdpError(ValueError):
    """Raised when an SDP body cannot be used to set up an audio stream."""
