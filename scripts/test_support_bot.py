import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "support-bot"))

from crypto import encrypt_text, decrypt_text
from storage.database import Database
from config import Settings

settings = Settings()

# Test crypto
master_key = "JulmzvUjiI1ad87cXWSKCbS0NtEnusByiMnH73Txohc="
plaintext = "test_api_key_12345"
encrypted = encrypt_text(master_key, plaintext)
print("Encrypted:", encrypted)
decrypted = decrypt_text(master_key, encrypted["encrypted"], encrypted["iv"])
print("Decrypted:", decrypted)
assert decrypted == plaintext, "Crypto test failed"
print("Crypto: OK")

# Test DB
db = Database(settings.database_path)
asyncio.run(db.init())
print("DB init: OK")
