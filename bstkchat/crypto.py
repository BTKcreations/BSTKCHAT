"""Authenticated end-to-end encryption helpers for BSTK Chat."""

import base64
import hashlib
import os
import re
from typing import Tuple, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_SIZE = 12
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def sanitize_room_id(room_id: str) -> str:
    """Sanitizes a room ID into a safe lowercase filename/topic component."""
    cleaned = re.sub(r"[^a-z0-9_-]", "_", room_id.strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "default"


def derive_room_key(room_id: str, password: str = "") -> bytes:
    """Derives a 256-bit AES-GCM key from the room ID and optional password using scrypt."""
    normalized_room = room_id.strip().lower()
    salt = hashlib.sha256(f"bstkchat-v2:{normalized_room}".encode("utf-8")).digest()
    secret = f"{normalized_room}:{password}".encode("utf-8")
    return hashlib.scrypt(
        secret,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )


def _normalize_key(key: Union[bytes, str]) -> bytes:
    if isinstance(key, bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("Encryption key must be 16, 24, or 32 bytes.")
        return key
    return derive_room_key("default", key)


def encrypt_payload(data: str, key: Union[bytes, str], aad: bytes = b"") -> str:
    """Encrypts UTF-8 text using AES-256-GCM with a fresh random 96-bit nonce."""
    key_bytes = _normalize_key(key)
    nonce = os.urandom(NONCE_SIZE)
    aesgcm = AESGCM(key_bytes)
    ciphertext = aesgcm.encrypt(nonce, data.encode("utf-8"), aad or None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_message(
    encrypted_token: str, key: Union[bytes, str], aad: bytes = b""
) -> Tuple[bool, str]:
    """Decrypts an AES-256-GCM token and returns (success, plaintext_or_error)."""
    try:
        raw = base64.urlsafe_b64decode(encrypted_token.encode("ascii"))
    except Exception:
        return False, "[Decryption Failed - Bad Payload]"

    if len(raw) <= NONCE_SIZE:
        return False, "[Decryption Failed - Bad Payload]"

    nonce = raw[:NONCE_SIZE]
    ciphertext = raw[NONCE_SIZE:]
    key_bytes = _normalize_key(key)

    try:
        plaintext_bytes = AESGCM(key_bytes).decrypt(nonce, ciphertext, aad or None)
        return True, plaintext_bytes.decode("utf-8")
    except InvalidTag:
        return False, "[Decryption Failed - Check Password]"
    except UnicodeDecodeError:
        return False, "[Decryption Failed - Invalid UTF-8]"


def decrypt_payload(
    encrypted_token: str, key: Union[bytes, str], aad: bytes = b""
) -> str:
    """Convenience wrapper returning decrypted text or an error placeholder."""
    _, text = decrypt_message(encrypted_token, key, aad=aad)
    return text
