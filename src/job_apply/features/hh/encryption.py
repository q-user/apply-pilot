"""Symmetric encryption for HH OAuth credentials via Fernet.

This module provides a :class:`CredentialEncryptor` that wraps
``cryptography.fernet.Fernet`` with a form-factor suited to the
VSA layout: the key is read from the environment (``APP_HH_ENCRYPTION_KEY``),
sensitive material is redacted from ``__repr__`` / ``__str__``, and key
rotation is a first-class operation.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet


class CredentialEncryptor:
    """Encrypt / decrypt plaintext strings using a Fernet symmetric key.

    Design constraints:

    * The key is a one-time parameter — once constructed the encryptor
      is bound to a single key instance. To rotate, call :meth:`rotate`.
    * ``__repr__`` and ``__str__`` deliberately hide the key material so
      that accidentally logging an encryptor instance does not expose the
      key.
    * Construction requires an explicit ``key``; use :meth:`from_env`
      for the production path that reads ``APP_HH_ENCRYPTION_KEY``.
    """

    def __init__(self, *, key: bytes) -> None:
        if not isinstance(key, bytes) or not key:
            raise ValueError("key must be non-empty bytes")
        self._fernet = Fernet(key)

    @classmethod
    def from_env(cls) -> CredentialEncryptor:
        """Read the encryption key from ``APP_HH_ENCRYPTION_KEY``.

        Raises:
            ValueError: If the env var is unset or empty.
        """
        raw = os.getenv("APP_HH_ENCRYPTION_KEY", "").strip()
        if not raw:
            raise ValueError(
                "APP_HH_ENCRYPTION_KEY environment variable must be set to a "
                "valid Fernet key (generate with Fernet.generate_key())"
            )
        return cls(key=raw.encode())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt *plaintext* and return a Fernet token (base64 string)."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet *token* back to the original plaintext.

        Raises:
            cryptography.fernet.InvalidToken: If *token* is corrupted,
                has been tampered with, or was encrypted with a different
                key.
        """
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    def rotate(self, new_key: bytes) -> None:
        """Replace the internal Fernet instance with one bound to *new_key*.

        This does *not* automatically re-encrypt existing ciphertexts in
        the database — the caller is responsible for reading all rows,
        decrypting with the old key, and re-encrypting with the new key.
        :meth:`rotate` merely updates the encryptor so that subsequent
        :meth:`encrypt` and :meth:`decrypt` calls use the new key.
        """
        self._fernet = Fernet(new_key)

    def __repr__(self) -> str:
        return "CredentialEncryptor(key=REDACTED)"

    def __str__(self) -> str:
        return "CredentialEncryptor(key=REDACTED)"


__all__ = ["CredentialEncryptor"]
