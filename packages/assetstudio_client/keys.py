"""Ed25519 helpers (raw 32-byte keys, standard base64 on the wire) and offer signing."""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from assetstudio_protocol.execution import Offer, offer_signing_bytes
from assetstudio_protocol.runners import StudioKey
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_LEN = 32


def generate_private_key() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def public_key_b64(private_raw: bytes) -> str:
    pub = Ed25519PrivateKey.from_private_bytes(private_raw).public_key()
    return base64.b64encode(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def sign(private_raw: bytes, message: bytes) -> str:
    return base64.b64encode(Ed25519PrivateKey.from_private_bytes(private_raw).sign(message)).decode()


def verify(public_b64: str, message: bytes, signature_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64, validate=True))
        pub.verify(base64.b64decode(signature_b64, validate=True), message)
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False
    return True


def save_private_key(path: Path, private_raw: bytes) -> None:
    """Atomic, 0600, never overwrites. os.link fails if the target exists, so concurrent writers cannot clobber."""
    if len(private_raw) != KEY_LEN:
        raise ValueError(f"private key must be {KEY_LEN} bytes")
    if path.exists():
        raise FileExistsError(str(path))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(private_raw)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_private_key(path: Path) -> bytes:
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must not be accessible by group/other (chmod 600)")
    raw = path.read_bytes()
    if len(raw) != KEY_LEN:
        raise ValueError(f"{path}: private key must be {KEY_LEN} bytes, got {len(raw)}")
    return raw


def sign_offer(private_raw: bytes, offer: Offer) -> Offer:
    return offer.model_copy(update={"signature": sign(private_raw, offer_signing_bytes(offer))})


def verify_offer(offer: Offer, keys: list[StudioKey]) -> bool:
    if not offer.signature:
        return False
    message = offer_signing_bytes(offer)
    return any(verify(k.public_key, message, offer.signature) for k in keys if k.status in ("current", "next"))
