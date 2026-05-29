import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import serialization

def derive_room_key(room_id: str, password: str) -> bytes:
    """Derives a strong 256-bit symmetric key from room details."""
    salt = room_id.encode('utf-8')
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return kdf.derive(password.encode('utf-8'))

def encrypt_payload(data: str, key_bytes: bytes) -> str:
    """Encrypts data using AES-GCM for authenticated encryption."""
    aesgcm = AESGCM(key_bytes)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, data.encode('utf-8'), None)
    payload = nonce + ct
    return base64.b64encode(payload).decode('utf-8')

def decrypt_payload(encrypted_base64: str, key_bytes: bytes) -> str:
    """Decrypts AES-GCM encrypted data."""
    try:
        payload = base64.b64decode(encrypted_base64.encode('utf-8'))
        nonce = payload[:12]
        ct = payload[12:]
        aesgcm = AESGCM(key_bytes)
        data = aesgcm.decrypt(nonce, ct, None)
        return data.decode('utf-8')
    except Exception:
        return "[Decryption Failed - Check Password or Corrupt Data]"

class DmCryptoManager:
    """Manages X25519 key pairs and shared secrets for DMs."""
    def __init__(self):
        self.private_key = x25519.X25519PrivateKey.generate()
        self.public_key_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        self.peer_public_keys = {}

    def get_my_public_key_b64(self) -> str:
        return base64.b64encode(self.public_key_bytes).decode('utf-8')

    def add_peer_public_key(self, peer_name: str, pub_key_b64: str):
        if not pub_key_b64:
            return
        try:
            pub_key_bytes = base64.b64decode(pub_key_b64.encode('utf-8'))
            self.peer_public_keys[peer_name] = x25519.X25519PublicKey.from_public_bytes(pub_key_bytes)
        except Exception:
            pass

    def _derive_shared_key(self, peer_pub_key: x25519.X25519PublicKey) -> bytes:
        shared_secret = self.private_key.exchange(peer_pub_key)
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'bstkchat dm encryption',
        ).derive(shared_secret)
        return derived_key

    def encrypt_dm(self, target_name: str, data: str) -> str:
        if target_name not in self.peer_public_keys:
            raise ValueError(f"No public key found for user {target_name}")
        peer_pub_key = self.peer_public_keys[target_name]
        shared_key = self._derive_shared_key(peer_pub_key)
        return encrypt_payload(data, shared_key)

    def decrypt_dm(self, sender_name: str, encrypted_base64: str) -> str:
        if sender_name not in self.peer_public_keys:
             return "[DM Decryption Failed - Missing Sender Key]"
        peer_pub_key = self.peer_public_keys[sender_name]
        shared_key = self._derive_shared_key(peer_pub_key)
        return decrypt_payload(encrypted_base64, shared_key)
