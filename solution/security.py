"""
Security module for Assignment A: PII Protection and Deterministic Phone Hashing.

Provides:
- Authenticated symmetric encryption (Fernet / AES-128-CBC + HMAC-SHA256) for PII fields.
- Deterministic keyed hashing (HMAC-SHA256) for canonical phone numbers.
- Safe key retrieval from environment variables with format validation.
"""

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from solution.config import PHONE_HASH_KEY_ENV, PII_ENCRYPTION_KEY_ENV


def get_encryption_key() -> bytes:
    """
    Retrieves and validates the PII encryption key from the environment.
    Must be a valid 32-byte url-safe base64 string required by Fernet.
    """
    key_str = os.getenv(PII_ENCRYPTION_KEY_ENV)
    if not key_str or not key_str.strip():
        raise ValueError(
            f"Missing required environment variable '{PII_ENCRYPTION_KEY_ENV}'. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    key_bytes = key_str.strip().encode("utf-8")
    try:
        raw_key = base64.urlsafe_b64decode(key_bytes)
        if len(raw_key) != 32:
            raise ValueError(f"Decoded key must be exactly 32 bytes, got {len(raw_key)}")
    except Exception as exc:
        raise ValueError(
            f"Invalid Fernet key format in '{PII_ENCRYPTION_KEY_ENV}': {exc}"
        ) from exc

    return key_bytes


def get_hash_key() -> bytes:
    """
    Retrieves and validates the HMAC phone hashing secret key from the environment.
    Requires a non-empty secret key.
    """
    key_str = os.getenv(PHONE_HASH_KEY_ENV)
    if not key_str or not key_str.strip():
        raise ValueError(
            f"Missing required environment variable '{PHONE_HASH_KEY_ENV}'. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return key_str.strip().encode("utf-8")


def encrypt_string(plaintext: str, key: Optional[bytes] = None) -> str:
    """
    Encrypts a plaintext string using Fernet authenticated symmetric encryption.
    Returns the ciphertext formatted as a url-safe base64 string.
    """
    if plaintext is None:
        raise ValueError("Cannot encrypt None")

    active_key = key if key is not None else get_encryption_key()
    fernet = Fernet(active_key)
    encrypted_bytes = fernet.encrypt(plaintext.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def decrypt_string(ciphertext: str, key: Optional[bytes] = None) -> str:
    """
    Decrypts a Fernet ciphertext back to its original plaintext string.
    Raises InvalidToken if the key does not match or if the ciphertext was tampered with.
    """
    if ciphertext is None:
        raise ValueError("Cannot decrypt None")

    active_key = key if key is not None else get_encryption_key()
    fernet = Fernet(active_key)
    decrypted_bytes = fernet.decrypt(ciphertext.encode("utf-8"))
    return decrypted_bytes.decode("utf-8")


def hash_phone(canonical_phone: str, key: Optional[bytes] = None) -> str:
    """
    Generates a deterministic HMAC-SHA256 hex digest for a canonical phone number.
    Ensures that identical phone numbers yield identical hashes while preventing
    rainbow table and dictionary attacks.
    """
    if canonical_phone is None:
        raise ValueError("Cannot hash None")

    active_key = key if key is not None else get_hash_key()
    # Normalize by stripping whitespace before computing the hash
    normalized = canonical_phone.strip().encode("utf-8")
    return hmac.new(active_key, normalized, hashlib.sha256).hexdigest()


def generate_encryption_key() -> str:
    """Utility helper to generate a new valid Fernet 32-byte url-safe base64 key."""
    return Fernet.generate_key().decode("utf-8")


def generate_hash_key() -> str:
    """Utility helper to generate a secure random 256-bit (32-byte) hex key for HMAC."""
    return secrets.token_hex(32)
