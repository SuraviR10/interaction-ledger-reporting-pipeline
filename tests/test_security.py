"""
Unit tests for PII protection: Fernet encryption/decryption, HMAC phone hashing,
and key management validation.
Compatible with standard library `unittest` and `pytest`.
"""

import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken

from solution.config import PHONE_HASH_KEY_ENV, PII_ENCRYPTION_KEY_ENV
from solution.security import (
    decrypt_string,
    encrypt_string,
    generate_encryption_key,
    generate_hash_key,
    get_encryption_key,
    get_hash_key,
    hash_phone,
)


class SecurityModuleTests(unittest.TestCase):
    def setUp(self):
        # Generate clean test keys for each test run
        self.enc_key = generate_encryption_key().encode("utf-8")
        self.hash_key = generate_hash_key().encode("utf-8")

    def test_encrypt_and_decrypt_string(self):
        """Verifies that encrypting and then decrypting recovers the original string."""
        original_name = "Aarav Sharma"
        ciphertext = encrypt_string(original_name, key=self.enc_key)

        # Confirm encrypted output is not identical to plaintext
        self.assertNotEqual(ciphertext, original_name)
        self.assertNotIn(original_name, ciphertext)

        # Confirm decryption recovers original plaintext
        decrypted = decrypt_string(ciphertext, key=self.enc_key)
        self.assertEqual(decrypted, original_name)

    def test_encrypt_canonical_phone(self):
        """Verifies encryption and decryption of a canonical Indian phone number."""
        canonical_phone = "+919876543210"
        ciphertext = encrypt_string(canonical_phone, key=self.enc_key)

        self.assertNotEqual(ciphertext, canonical_phone)
        self.assertEqual(decrypt_string(ciphertext, key=self.enc_key), canonical_phone)

    def test_deterministic_phone_hash(self):
        """Verifies that hashing the same phone with the same key produces the identical hash."""
        phone = "+919876543210"
        hash_1 = hash_phone(phone, key=self.hash_key)
        hash_2 = hash_phone(phone, key=self.hash_key)

        self.assertEqual(hash_1, hash_2, "Same phone and key must generate identical HMAC-SHA256 hashes")
        self.assertEqual(len(hash_1), 64, "SHA-256 hex digest must be 64 characters long")

    def test_different_phones_produce_different_hashes(self):
        """Verifies that two distinct phone numbers produce distinct hashes."""
        phone_1 = "+919876543210"
        phone_2 = "+919876543211"
        hash_1 = hash_phone(phone_1, key=self.hash_key)
        hash_2 = hash_phone(phone_2, key=self.hash_key)

        self.assertNotEqual(hash_1, hash_2)

    def test_encryption_and_hmac_use_separate_keys(self):
        """Verifies that encryption and hashing use completely distinct keys."""
        # Using a raw HMAC key as a Fernet key should fail validation if not valid base64
        with self.assertRaises(ValueError):
            encrypt_string("Test", key=b"simple_non_base64_hmac_key")

    def test_tampered_ciphertext_detection(self):
        """Verifies that tampering with ciphertext triggers InvalidToken on decryption."""
        plaintext = "Confidential Name"
        ciphertext = encrypt_string(plaintext, key=self.enc_key)

        # Tamper with the ciphertext by flipping characters in the payload
        tampered_bytes = bytearray(ciphertext.encode("utf-8"))
        tampered_bytes[-5] = ord("X") if tampered_bytes[-5] != ord("X") else ord("Y")
        tampered_ciphertext = tampered_bytes.decode("utf-8")

        with self.assertRaises(InvalidToken):
            decrypt_string(tampered_ciphertext, key=self.enc_key)

    def test_decrypt_with_wrong_key_fails(self):
        """Decrypting with a different valid key must fail authentication."""
        different_key = generate_encryption_key().encode("utf-8")
        ciphertext = encrypt_string("Private Data", key=self.enc_key)

        with self.assertRaises(InvalidToken):
            decrypt_string(ciphertext, key=different_key)

    def test_missing_environment_keys_raise_error(self):
        """Functions without explicit key argument must raise ValueError if env vars are unset."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx_enc:
                get_encryption_key()
            self.assertIn(PII_ENCRYPTION_KEY_ENV, str(ctx_enc.exception))

            with self.assertRaises(ValueError) as ctx_hash:
                get_hash_key()
            self.assertIn(PHONE_HASH_KEY_ENV, str(ctx_hash.exception))

    def test_invalid_fernet_key_format_raises_error(self):
        """Providing a key that is not a 32-byte urlsafe base64 string must raise ValueError."""
        with patch.dict(os.environ, {PII_ENCRYPTION_KEY_ENV: "invalid_short_key"}):
            with self.assertRaises(ValueError):
                get_encryption_key()

    def test_none_input_validation(self):
        """Passing None to encrypt, decrypt, or hash must raise ValueError."""
        with self.assertRaises(ValueError):
            encrypt_string(None, key=self.enc_key)
        with self.assertRaises(ValueError):
            decrypt_string(None, key=self.enc_key)
        with self.assertRaises(ValueError):
            hash_phone(None, key=self.hash_key)


if __name__ == "__main__":
    unittest.main()
