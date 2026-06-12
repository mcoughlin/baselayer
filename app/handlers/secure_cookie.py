"""Tornado-compatible signed cookies, with no Tornado dependency.

Part of the Tornado -> ASGI migration (issue #381). To survive a deploy
without logging everyone out, the new (Starlette) server must read cookies
minted by the current Tornado server and vice-versa. This is a faithful,
**byte-compatible** port of Tornado's ``create_signed_value`` /
``decode_signed_value`` (v2 format, with v1 decode for old cookies).

Verified round-trip-compatible against ``tornado.web`` in
``tests/test_secure_cookie.py`` -- a value signed by Tornado decodes here and
one signed here decodes in Tornado, for the same secret.

The format (Tornado's docstring, verbatim): a version number and a series of
length-prefixed ``"%d:%s"`` fields, the last of which is a signature, all
separated by pipes. The signature is an HMAC-SHA256 of the whole string up to
that point, including the final pipe. Fields: format version (2), key version
(default 0), timestamp, name (unencoded), value (base64), signature (hex).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from typing import Callable, Optional, Union

DEFAULT_SIGNED_VALUE_VERSION = 2
DEFAULT_SIGNED_VALUE_MIN_VERSION = 1

_signed_value_version_re = re.compile(rb"^([1-9][0-9]*)\|(.*)$")

Secret = Union[str, bytes]


def utf8(value: Union[None, str, bytes]) -> Optional[bytes]:
    if value is None or isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise TypeError(f"Expected bytes, str, or None; got {type(value)!r}")
    return value.encode("utf-8")


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def create_signed_value(
    secret: Secret,
    name: str,
    value: Union[str, bytes],
    version: int = DEFAULT_SIGNED_VALUE_VERSION,
    clock: Callable[[], float] = time.time,
    key_version: int = 0,
) -> bytes:
    timestamp = utf8(str(int(clock())))
    value = base64.b64encode(utf8(value))
    if version == 1:
        signature = _create_signature_v1(secret, name, value, timestamp)
        return b"|".join([value, timestamp, signature])
    elif version == 2:

        def format_field(s: Union[str, bytes]) -> bytes:
            return utf8("%d:" % len(s)) + utf8(s)

        to_sign = b"|".join(
            [
                b"2",
                format_field(str(key_version or 0)),
                format_field(timestamp),
                format_field(name),
                format_field(value),
                b"",
            ]
        )
        return to_sign + _create_signature_v2(secret, to_sign)
    raise ValueError(f"Unsupported version {version}")


def decode_signed_value(
    secret: Secret,
    name: str,
    value: Union[None, str, bytes],
    max_age_days: float = 31,
    clock: Callable[[], float] = time.time,
    min_version: int = DEFAULT_SIGNED_VALUE_MIN_VERSION,
) -> Optional[bytes]:
    if not value:
        return None
    value = utf8(value)
    version = _get_version(value)
    if version < min_version:
        return None
    if version == 1:
        return _decode_signed_value_v1(secret, name, value, max_age_days, clock)
    if version == 2:
        return _decode_signed_value_v2(secret, name, value, max_age_days, clock)
    return None


# --------------------------------------------------------------------------- #
# v2
# --------------------------------------------------------------------------- #
def _create_signature_v2(secret: Secret, s: bytes) -> bytes:
    h = hmac.new(utf8(secret), digestmod=hashlib.sha256)
    h.update(utf8(s))
    return utf8(h.hexdigest())


def _decode_signed_value_v2(secret, name, value, max_age_days, clock):
    try:
        _key_version, timestamp_bytes, name_field, value_field, passed_sig = (
            _decode_fields_v2(value)
        )
    except ValueError:
        return None
    signed_string = value[: -len(passed_sig)]
    expected_sig = _create_signature_v2(secret, signed_string)
    if not hmac.compare_digest(passed_sig, expected_sig):
        return None
    if name_field != utf8(name):
        return None
    if int(timestamp_bytes) < clock() - max_age_days * 86400:
        return None
    try:
        return base64.b64decode(value_field)
    except Exception:
        return None


def _decode_fields_v2(value: bytes):
    def _consume_field(s: bytes):
        length, _, rest = s.partition(b":")
        n = int(length)
        field_value = rest[:n]
        if rest[n : n + 1] != b"|":
            raise ValueError("malformed v2 signed value field")
        return field_value, rest[n + 1 :]

    rest = value[2:]  # remove version number ("2|")
    key_version, rest = _consume_field(rest)
    timestamp, rest = _consume_field(rest)
    name_field, rest = _consume_field(rest)
    value_field, passed_sig = _consume_field(rest)
    return int(key_version), timestamp, name_field, value_field, passed_sig


# --------------------------------------------------------------------------- #
# v1 (decode only, for legacy cookies)
# --------------------------------------------------------------------------- #
def _create_signature_v1(secret: Secret, *parts: Union[str, bytes]) -> bytes:
    h = hmac.new(utf8(secret), digestmod=hashlib.sha1)
    for part in parts:
        h.update(utf8(part))
    return utf8(h.hexdigest())


def _decode_signed_value_v1(secret, name, value, max_age_days, clock):
    parts = utf8(value).split(b"|")
    if len(parts) != 3:
        return None
    signature = _create_signature_v1(secret, name, parts[0], parts[1])
    if not hmac.compare_digest(parts[2], signature):
        return None
    timestamp = int(parts[1])
    if timestamp < clock() - max_age_days * 86400:
        return None
    if timestamp > clock() + 31 * 86400:
        return None
    if parts[1].startswith(b"0"):
        return None
    try:
        return base64.b64decode(parts[0])
    except Exception:
        return None


def _get_version(value: bytes) -> int:
    m = _signed_value_version_re.match(value)
    if m is None:
        return 1
    try:
        version = int(m.group(1))
        if version > 999:
            version = 1
    except ValueError:
        version = 1
    return version
