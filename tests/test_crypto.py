import base64

from bstkchat.crypto import (
    decrypt_message,
    decrypt_payload,
    derive_room_key,
    encrypt_payload,
    sanitize_room_id,
)


def test_encrypt_decrypt_roundtrip_with_aad():
    key = derive_room_key("quantum-beacon-55", "s3cr3t!")
    aad = b"bstkchat/rooms/quantum-beacon-55/messages"
    plaintext = "Hello **world**! 🔒"

    token = encrypt_payload(plaintext, key, aad=aad)
    ok, result = decrypt_message(token, key, aad=aad)

    assert ok is True
    assert result == plaintext
    assert decrypt_payload(token, key, aad=aad) == plaintext


def test_encrypt_uses_unique_nonce_per_message():
    key = derive_room_key("room-1", "pass")
    t1 = encrypt_payload("identical message", key)
    t2 = encrypt_payload("identical message", key)
    assert t1 != t2


def test_decrypt_fails_with_wrong_password_or_aad():
    key_right = derive_room_key("room-1", "correct")
    key_wrong = derive_room_key("room-1", "wrong")
    token = encrypt_payload("secret", key_right, aad=b"topic-a")

    ok_pw, msg_pw = decrypt_message(token, key_wrong, aad=b"topic-a")
    assert ok_pw is False
    assert "Decryption Failed" in msg_pw

    ok_aad, _ = decrypt_message(token, key_right, aad=b"topic-b")
    assert ok_aad is False


def test_decrypt_rejects_tampered_ciphertext():
    key = derive_room_key("room-1", "pass")
    token = encrypt_payload("transfer $10", key)
    raw = bytearray(base64.urlsafe_b64decode(token))
    raw[-1] ^= 0xFF
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode("ascii")

    ok, _ = decrypt_message(tampered, key)
    assert ok is False


def test_sanitize_room_id_prevents_path_traversal():
    assert sanitize_room_id("../../etc/passwd") == "etc_passwd"
    assert sanitize_room_id("  My Room!@# ") == "my_room"
    assert sanitize_room_id("...") == "default"
    assert sanitize_room_id("quantum-beacon-55") == "quantum-beacon-55"
