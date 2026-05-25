"""Encrypted-at-rest secrets for AIFactory (Epic #26 P2).

Public API:
    EncryptedString       SQLAlchemy TypeDecorator for credential columns.
    get_backend()         Factory returning the active KMS backend.
"""

from .encrypted_string import EncryptedString
from .kms import get_backend

__all__ = ["EncryptedString", "get_backend"]
