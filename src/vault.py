"""Envelope encryption for `connections.credentials_enc` (PRD §7.3, §12
decision 4): a random data key per record, itself encrypted ("wrapped") by
one master key, instead of encrypting every record directly with the master
key. If a record's wrapped key is ever exposed alone it's useless without the
master key; rotating the master key means only re-wrapping data keys, not
re-encrypting every record's plaintext.

AES-256-GCM via `cryptography` (already a transitive dep, now a direct one) —
no hand-rolled cipher.

ponytail: master key is a single static value from an env var, unwrapped in
process memory. Ceiling: anyone with process memory access or the env var
gets every credential. Upgrade path (PRD §12 decision 4 option B/real KMS):
swap `_master_key()` for a call to a cloud KMS Decrypt/GenerateDataKey API —
nothing else in this module or its callers needs to change, since callers
only see `encrypt()`/`decrypt()`.
"""
import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(Exception):
    pass


def _master_key() -> bytes:
    raw = os.environ.get("VAULT_MASTER_KEY")
    if not raw:
        raise VaultError("VAULT_MASTER_KEY env var is not set — required to encrypt/decrypt credentials")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:
        raise VaultError("VAULT_MASTER_KEY is not valid base64") from exc
    if len(key) != 32:
        raise VaultError("VAULT_MASTER_KEY must decode to exactly 32 bytes (AES-256)")
    return key


def encrypt(plaintext: dict) -> bytes:
    """Encrypts a credentials dict for storage in `credentials_enc`. Returns
    one opaque blob: wrapped data key + both GCM nonces + ciphertext, each
    length-prefixed so decrypt() doesn't need a second stored field."""
    master_key = _master_key()
    data_key = os.urandom(32)

    wrap_nonce = os.urandom(12)
    wrapped_data_key = AESGCM(master_key).encrypt(wrap_nonce, data_key, None)

    data_nonce = os.urandom(12)
    ciphertext = AESGCM(data_key).encrypt(data_nonce, json.dumps(plaintext).encode(), None)

    parts = [wrap_nonce, wrapped_data_key, data_nonce, ciphertext]
    blob = b"".join(len(p).to_bytes(4, "big") + p for p in parts)
    return blob


def decrypt(blob: bytes) -> dict:
    master_key = _master_key()
    pos = 0
    parts = []
    for _ in range(4):
        length = int.from_bytes(blob[pos:pos + 4], "big")
        pos += 4
        parts.append(blob[pos:pos + length])
        pos += length
    wrap_nonce, wrapped_data_key, data_nonce, ciphertext = parts

    try:
        data_key = AESGCM(master_key).decrypt(wrap_nonce, wrapped_data_key, None)
        plaintext = AESGCM(data_key).decrypt(data_nonce, ciphertext, None)
    except Exception as exc:
        raise VaultError("decryption failed: wrong master key or corrupted/tampered blob") from exc
    return json.loads(plaintext)


def _demo():
    os.environ["VAULT_MASTER_KEY"] = base64.b64encode(os.urandom(32)).decode()
    secret = {"api_key": "abc123", "domain": "foo.freshdesk.com"}
    blob = encrypt(secret)
    assert decrypt(blob) == secret
    assert b"abc123" not in blob

    tampered = blob[:-1] + bytes([blob[-1] ^ 0xFF])
    try:
        decrypt(tampered)
        assert False, "should have raised VaultError on tampered ciphertext"
    except VaultError:
        pass

    del os.environ["VAULT_MASTER_KEY"]
    try:
        encrypt(secret)
        assert False, "should have raised VaultError on missing master key"
    except VaultError:
        pass

    print("vault: all checks passed")


if __name__ == "__main__":
    _demo()
