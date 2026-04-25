from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class EncryptedCredential:
    encrypted_value: str
    kms_key_id: str


class CredentialCipher:
    """Development envelope encryption helper for merchant credentials.

    Production can swap the master-key wrapping implementation with AWS KMS while
    preserving the stored envelope format and call sites.
    """

    def __init__(self, master_key: str, kms_key_id: str) -> None:
        self._master = Fernet(master_key.encode("utf-8"))
        self._kms_key_id = kms_key_id

    def encrypt(self, merchant_id: UUID, plaintext: str) -> EncryptedCredential:
        data_key = Fernet.generate_key()
        value_cipher = Fernet(data_key)
        envelope = {
            "v": 1,
            "merchant_id": str(merchant_id),
            "encrypted_data_key": self._master.encrypt(data_key).decode("utf-8"),
            "ciphertext": value_cipher.encrypt(plaintext.encode("utf-8")).decode("utf-8"),
        }
        return EncryptedCredential(
            encrypted_value=base64.urlsafe_b64encode(
                json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            ).decode("utf-8"),
            kms_key_id=self._kms_key_id,
        )

    def decrypt(self, merchant_id: UUID, encrypted_value: str) -> str:
        try:
            envelope = json.loads(base64.urlsafe_b64decode(encrypted_value.encode("utf-8")))
            if envelope.get("merchant_id") != str(merchant_id):
                raise ValueError("Credential belongs to another merchant.")
            encrypted_data_key = _required_string(envelope, "encrypted_data_key")
            ciphertext = _required_string(envelope, "ciphertext")
            data_key = self._master.decrypt(encrypted_data_key.encode("utf-8"))
            return Fernet(data_key).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except (InvalidToken, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Unable to decrypt credential envelope.") from exc


def _required_string(envelope: dict[object, object], key: str) -> str:
    value = envelope.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Credential envelope missing {key}.")
    return value

