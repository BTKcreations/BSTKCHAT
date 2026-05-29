import pytest
from bstkchat.crypto import derive_room_key, encrypt_payload, decrypt_payload, DmCryptoManager

def test_derive_room_key():
    key1 = derive_room_key("test_room", "password123")
    key2 = derive_room_key("test_room", "password123")
    assert key1 == key2
    assert len(key1) == 32

    key3 = derive_room_key("test_room", "wrongpassword")
    assert key1 != key3

def test_encrypt_decrypt_payload():
    key = derive_room_key("test_room", "password123")
    original_text = "Hello, world!"

    encrypted = encrypt_payload(original_text, key)
    assert encrypted != original_text

    decrypted = decrypt_payload(encrypted, key)
    assert decrypted == original_text

def test_decrypt_payload_wrong_key():
    key1 = derive_room_key("test_room", "password123")
    key2 = derive_room_key("test_room", "wrongpassword")
    original_text = "Hello, world!"

    encrypted = encrypt_payload(original_text, key1)
    decrypted = decrypt_payload(encrypted, key2)
    assert decrypted == "[Decryption Failed - Check Password or Corrupt Data]"

def test_dm_crypto():
    alice = DmCryptoManager()
    bob = DmCryptoManager()

    alice_pub = alice.get_my_public_key_b64()
    bob_pub = bob.get_my_public_key_b64()

    alice.add_peer_public_key("Bob", bob_pub)
    bob.add_peer_public_key("Alice", alice_pub)

    original_text = "Top secret message from Alice to Bob"

    # Alice encrypts a message for Bob
    encrypted_for_bob = alice.encrypt_dm("Bob", original_text)

    # Bob decrypts the message from Alice
    decrypted_by_bob = bob.decrypt_dm("Alice", encrypted_for_bob)
    assert decrypted_by_bob == original_text

    # Charlie tries to decrypt
    charlie = DmCryptoManager()
    charlie.add_peer_public_key("Alice", alice_pub)
    decrypted_by_charlie = charlie.decrypt_dm("Alice", encrypted_for_bob)
    assert decrypted_by_charlie == "[Decryption Failed - Check Password or Corrupt Data]"
