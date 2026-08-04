import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _master_key_bytes(master_key: str) -> bytes:
    key = base64.b64decode(master_key)
    if len(key) != 32:
        raise ValueError("SUPPORT_BOT_MASTER_KEY must be base64-encoded 32 bytes")
    return key


def encrypt_text(master_key: str, plaintext: str) -> dict:
    key = _master_key_bytes(master_key)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return {
        "encrypted": base64.b64encode(ciphertext).decode("utf-8"),
        "iv": base64.b64encode(nonce).decode("utf-8"),
    }


def decrypt_text(master_key: str, encrypted_b64: str, iv_b64: str) -> str:
    key = _master_key_bytes(master_key)
    aesgcm = AESGCM(key)
    ciphertext = base64.b64decode(encrypted_b64)
    nonce = base64.b64decode(iv_b64)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
