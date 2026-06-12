"""Tests for the Tornado-byte-compatible signed cookies (issue #381).

The point of ``baselayer.app.handlers.secure_cookie`` is to read/write cookies
in Tornado's exact v2 format so a Tornado->ASGI cutover doesn't log everyone
out. The golden-vector test pins that byte-compatibility *without* importing
tornado (so it still guards us after tornado is removed); the cross-check test
verifies against a live tornado when one is installed.
"""

import pytest

from baselayer.app.handlers import secure_cookie as sc

SECRET = "golden-secret"

# Produced by tornado.web.create_signed_value(SECRET, "user_id", "4815",
# clock=lambda: 1700000000.0) on tornado 6.x. Hardcoded on purpose.
GOLDEN_V2 = (
    "2|1:0|10:1700000000|7:user_id|8:NDgxNQ==|"
    "f6c442f06024c5e21b58203c2630a143449a8a80ae97becb419d01a84bc8a763"
)


def _fixed_clock(t=1700000000.0 + 10):
    return lambda: t


def test_decode_golden_tornado_vector():
    # Reading a real Tornado-minted cookie must yield the original value.
    assert (
        sc.decode_signed_value(SECRET, "user_id", GOLDEN_V2, clock=_fixed_clock())
        == b"4815"
    )


def test_round_trip():
    clock = _fixed_clock()
    signed = sc.create_signed_value(SECRET, "user_id", "4815", clock=clock)
    assert sc.decode_signed_value(SECRET, "user_id", signed, clock=clock) == b"4815"
    # And our own output matches the golden Tornado bytes (minus the timestamp,
    # which we fix here to the golden value).
    fixed = lambda: 1700000000.0
    assert (
        sc.create_signed_value(SECRET, "user_id", "4815", clock=fixed).decode()
        == GOLDEN_V2
    )


def test_wrong_secret_rejected():
    assert sc.decode_signed_value("nope", "user_id", GOLDEN_V2, clock=_fixed_clock()) is None


def test_wrong_name_rejected():
    assert sc.decode_signed_value(SECRET, "other", GOLDEN_V2, clock=_fixed_clock()) is None


def test_tampered_rejected():
    bad = bytearray(GOLDEN_V2.encode())
    bad[22] ^= 1
    assert sc.decode_signed_value(SECRET, "user_id", bytes(bad), clock=_fixed_clock()) is None


def test_expired_rejected():
    # Default max_age_days=31; the golden cookie is from 2023.
    assert sc.decode_signed_value(SECRET, "user_id", GOLDEN_V2) is None


def test_matches_tornado_byte_for_byte():
    tornado_web = pytest.importorskip("tornado.web")
    clock = lambda: 1700000000.0
    assert sc.create_signed_value(
        SECRET, "user_id", "4815", clock=clock
    ) == tornado_web.create_signed_value(SECRET, "user_id", "4815", clock=clock)
    # cross-decode, both directions, at "now"
    minted_by_tornado = tornado_web.create_signed_value(SECRET, "n", "v")
    assert sc.decode_signed_value(SECRET, "n", minted_by_tornado) == b"v"
    minted_by_us = sc.create_signed_value(SECRET, "n", "v")
    assert tornado_web.decode_signed_value(SECRET, "n", minted_by_us) == b"v"
