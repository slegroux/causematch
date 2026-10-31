"""Signatures for hour certificates: HMAC-SHA256 over a canonical encoding of what the
certificate says (its snapshot) and its id, keyed by config.certificate_key.

Pure functions, no database. A certificate is genuine when the signature recomputed from
its stored snapshot and id matches the one issued; one changed character anywhere in the
snapshot, or a different id, makes it not match.
"""

import hashlib
import hmac
import json

from app.config import certificate_key


def canonical(snapshot, cert_id):
    """The bytes that are signed: the id and the snapshot as JSON with sorted keys, no
    spaces and ASCII only, so the same content always encodes the same way."""
    return json.dumps(
        {"id": cert_id, "snapshot": snapshot},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def sign(snapshot, cert_id, key=None):
    """The signature, as 64 hex characters."""
    key = certificate_key() if key is None else key
    return hmac.new(
        key.encode(), canonical(snapshot, cert_id), hashlib.sha256
    ).hexdigest()


def verify(snapshot, cert_id, signature, key=None):
    """True when `signature` is the one sign() gives this snapshot and id."""
    return hmac.compare_digest(sign(snapshot, cert_id, key), str(signature or ""))


def code(signature):
    """What a person reads out or compares: the first 16 hex characters in groups of four,
    upper case ("A1B2-C3D4-E5F6-0718")."""
    return "-".join(signature[i : i + 4] for i in range(0, 16, 4)).upper()
