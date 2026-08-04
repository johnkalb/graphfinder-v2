"""Ristretto255 DH-OPRF and manifest key derivation."""
from __future__ import annotations
import hashlib
import pysodium

def new_server_secret() -> bytes:
    return pysodium.crypto_core_ristretto255_scalar_random()

def new_blind_scalar() -> bytes:
    return pysodium.crypto_core_ristretto255_scalar_random()

def h1(namespace: str, item: str) -> bytes:
    return pysodium.crypto_core_ristretto255_from_hash(hashlib.sha512(namespace.encode() + b"\0" + item.encode()).digest())

def eval_s(secret_scalar: bytes, point: bytes) -> bytes:
    return pysodium.crypto_scalarmult_ristretto255(secret_scalar, point)

def blind(point: bytes, blind_scalar: bytes) -> bytes:
    return pysodium.crypto_scalarmult_ristretto255(blind_scalar, point)

def unblind(evaluated_point: bytes, blind_scalar: bytes) -> bytes:
    inverse = pysodium.crypto_core_ristretto255_scalar_invert(blind_scalar)
    return pysodium.crypto_scalarmult_ristretto255(inverse, evaluated_point)

def derive_aes_key(key_version: int, oprf_point: bytes) -> bytes:
    return hashlib.sha256(b"contact-psi-v1" + int(key_version).to_bytes(4, "big") + oprf_point).digest()

def full_eval(secret_scalar: bytes, key_version: int, namespace: str, item: str) -> bytes:
    return derive_aes_key(key_version, eval_s(secret_scalar, h1(namespace, item)))
